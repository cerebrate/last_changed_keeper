# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration (HACS-distributed, `custom_components/last_changed_keeper/`).
It restores the real `last_changed` timestamp of selected entities after a Home
Assistant restart (HA normally resets it to the restart time). Single config
entry, no entities of its own besides one diagnostic status sensor.

## Commands

```bash
pip install pytest -r requirements_test.txt   # test deps (pytest-homeassistant-custom-component)
pytest -q                                     # run the full test suite
pytest tests/test_resolve.py -q               # run a single test file
pytest tests/test_resolve.py::test_name -q    # run a single test
ruff check .                                  # lint (also runs in CI via astral-sh/ruff-action)
```

CI (`.github/workflows/validate.yml`) also runs `hassfest` (HA manifest/translation
validation) and the `hacs/action` HACS repo check — these aren't runnable locally
in a meaningful way, so don't try to reproduce them; just keep `manifest.json`,
`strings.json`/`translations/`, and `hacs.json` consistent when changing config.

Tests use `pytest-homeassistant-custom-component`. Fixtures are NOT autouse
(see `tests/conftest.py`): tests that touch the recorder or config/options flow
must request `recorder_mock` before `hass`, then `enable_custom_integrations`,
in that order — copy the pattern from an existing test in the relevant file
rather than guessing the fixture order.

## Architecture

Everything lives in `__init__.py` (`_RestoreJob` class) — read its module
docstring first, it's the best map of the resolve-source priority. The other
files are thin: `config_flow.py` (GUI schema, shared target-count preview via
`resolve_targets`), `sensor.py` (one diagnostic status sensor), `diagnostics.py`,
`const.py` (all tunables/keys/defaults).

**Before the boot pass runs at all**, `async_run()` (the wrapper the
`async_at_started` boot trigger calls — not `_async_run_impl` directly,
which `restore_now`'s service handler calls unwrapped) awaits
`_wait_for_recorder_ready()`. `manifest.json`'s `"recorder"` dependency
only guarantees recorder's own `async_setup()` has returned, which is
gated on `Recorder.async_db_ready` — resolved once the DB is connected and
any *non-live* migration is done. A genuinely *live* migration (needed
after most HA core upgrades) is deliberately deferred by the recorder
itself until *after* HA reports "started", specifically so it doesn't
compete with startup CPU load — which is exactly the same moment
`async_at_started` fires. Without this wait, the boot pass's own
recorder-heavy bulk/deep queries would run directly against a
mid-migration recorder, competing for the same executor/DB the migration
was deferred to avoid competing with — both slowing the boot pass down
dramatically (observed: an 11-batch/~150K-row bulk pass taking ~30 minutes
on the field install) and making a late (`>grace`) `_async_run_impl`
invocation, and the snapshot poisoning that follows from it (see
`self._confirmed` below), a real, recurring condition rather than a rare
edge case. `Recorder.async_recorder_ready` (an `asyncio.Event`, distinct
from `async_db_ready`) is only set once all migration steps genuinely
finish; it's awaited with a generous but bounded timeout
(`RECORDER_READY_TIMEOUT_SECONDS`) rather than unconditionally, since a
*failed* live migration never sets it at all — an unbounded wait would
then hang the boot pass forever over an unrelated recorder failure. On
timeout, the boot pass proceeds anyway (logged as a warning): querying a
possibly-busy recorder is still better than never running at all.

**Every individual recorder history query** — each bulk batch
(`_iter_bulk_batches`) and each per-entity deep/`last_triggered` query
(`_resolve` step 3, `_resolve_last_triggered`) — is preceded by
`_wait_for_recorder_commit()`, a *separate* concern from
`_wait_for_recorder_ready()` above: that one is a one-time boot gate for
migration completion; this one guards ordinary recorder **commit lag** on
every single query, boot or not. Recorder writes are queued and committed
asynchronously, so a state change can already be visible via
`hass.states.get()` before it's queryable via `history`/
`get_last_state_changes()`. `_resolve`'s "bounded" trust (see its
docstring) treats the instant a query returns any older, differently-valued
row as definitive, with no protection — unlike the unbounded path's
`_near_purge_boundary` — against the query having simply run ahead of the
recorder's own write queue; if the most recent transition genuinely hasn't
committed yet, a bounded query confidently returns the *previous* boundary
instead, wrong but indistinguishable from a correct bounded result at the
call site. Field evidence: a real door sensor's `verify`-computed
`expected_last_changed` matched a transition from the previous evening,
even though the door had genuinely, physically reopened and closed again 3
seconds before the query that produced that answer — and because a bounded
resolve is trusted unconditionally, an entity hit by this at boot time gets
`_apply()`'d once, marked `_confirmed`, and (since only `_unconfirmed`
entities are ever revisited — see the periodic sweep below) is never
looked at again, sitting wrong until the next full restart.
`_wait_for_recorder_commit()` calls `Recorder.async_get_commit_future()`
(the same mechanism HA core's own live-history websocket API uses before
querying) and awaits it with a short, bounded timeout
(`RECORDER_COMMIT_WAIT_TIMEOUT_SECONDS`) — short rather than generous like
the readiness wait above, since this can be hit many times within a single
pass (once per batch, once per deep query) and a long per-call timeout
would risk compounding into a much longer pass under sustained load,
whereas this guards an ordinary sub-second-to-few-second race, not a
one-time migration. It costs nothing in the common case: the write queue
is usually already empty, and `async_get_commit_future()` returns `None`
immediately when so.

**Core flow (`_RestoreJob._async_run_impl`, boot pass):**
1. Resolve targets via `resolve_targets()` (domains/entities/labels/areas minus
   exclude, or literally every entity if "all_entities" is on) — this function is
   shared with `config_flow.py` so the live match-count preview and the actual
   run can never disagree.
2. Split into candidates (state is fresh/a restart artifact, within `grace`)
   vs. skip (already genuinely used since boot).
3. Recorder history for all candidates, fetched in chunks of
   `_bulk_batch_size` (per-entry override of the `BULK_BATCH_SIZE` default,
   `CONF_BULK_BATCH_SIZE` in the config/options flow) to avoid a huge
   `IN (...)` and **streamed** one batch at a time (`_iter_bulk_batches`).
   Steps 3 and 4 are interleaved per batch:
   the caller resolves a batch and drops it before the next query runs, so
   peak memory is one batch rather than the whole installation's 30-day
   history. Both the generator and the caller must release their reference —
   see the `del bulk` in each consumer. Because streaming means the pass can
   itself take a while, the per-batch re-validation compares each entity's
   `last_changed` against the value snapshotted when the candidate list was
   built, not against elapsed time vs. `grace` again — an untouched entity's
   `last_changed` doesn't move just because the pass is slow, so an
   elapsed-time re-check would silently drop the tail of a slow pass
   (skipped, and never added to `_pending`, so nothing ever retries them).
   `stats["bulk_rows_fetched"]`/`["bulk_batches"]`/`["deep_queries"]` (also
   returned by `async_verify`, prefixed `verify_*` in the persisted
   post-boot self-check) expose the recorder cost of a pass — how many
   rows it fetched and how many entities fell through to the expensive
   per-entity query — via the status sensor/diagnostics, instead of that
   only being inferable from wall-clock duration. `_iter_bulk_batches` also
   pushes a zero-delay `asyncio.sleep(0)` after each batch — a large "track
   all entities" install can mean many batches back to back, each a
   synchronous query on the recorder's own single-worker executor, and
   without a yield point there our own coroutine is immediately ready
   again the instant one batch resolves, crowding out other ready
   callbacks on the main event loop for the whole pass. `sleep(0)` costs
   nothing in wall-clock time (unlike a real delay); it only gives other
   already-ready work a fair turn between batches, shared by every caller
   of this generator (boot pass, verify, and the re-registration/pending-
   recovery burst drains alike).
4. Per entity, `_resolve()` picks the real timestamp in this priority order:
   **bulk result → incremental/periodic snapshot store (if newer for the same
   value) → deep per-entity recorder query → best-effort unbounded run**. A
   "bounded" run (recorder history shows an older, different value) is
   definitive; an "unbounded" one (history exhausted) is only trusted under
   specific conditions — see the docstrings on `_resolve` and `_real_last_changed`.
   `_real_last_changed` also reports *why* a run is bounded: a genuine
   differing value proves the value really did just change and is trusted
   unconditionally even when too recent to clear `MARGIN_SECONDS` (no other
   source may override it); a `None`-state (entity removal) row — written
   on every `Entity.async_remove()`, i.e. every config-entry reload/device
   rejoin, exactly the case the re-registration listener exists to fix —
   only proves the entity briefly didn't exist, not that its value
   changed, so a boundary that's *both* removal-caused *and* too recent
   (this session's own re-registration, not some older entity-id-reuse
   boundary) is inconclusive rather than a hard block, and `_resolve` falls
   through to the snapshot/deep/best-effort sources below instead. Without
   this distinction a fast reload would leave the entity permanently
   `_unconfirmed` — the bulk history behind the block never changes, so
   every later retry recomputes the identical wrong answer — worse off
   than never fixing the query race in the first place.
   One of those conditions is `_near_purge_boundary`: every HA restart writes
   a fresh recorder row for each entity even when its value hasn't changed
   (the live in-place `last_changed` patch never gets written back to the
   recorder DB), so a same-value run walking past a string of restarts looks
   identical to one that's genuinely never changed — until it runs out of
   retained rows. Once `purge_keep_days` has erased the older restarts, "ran
   out of rows" and "reached the real origin" are indistinguishable from
   inside that entity's own history, so an unbounded result landing at or
   before the recorder's purge boundary (`Recorder.keep_days`, read directly
   rather than inferred from the database, plus `PURGE_BOUNDARY_MARGIN_DAYS`
   to absorb purge running periodically rather than continuously) is
   discarded rather than trusted. This trades a class of confidently-wrong
   backdates (and ones that silently drift to a new wrong value every time
   purge erodes further) for those entities being left unpatched — same as
   an uninstalled integration — for as long as their true last change
   predates what the recorder retains.
5. `_apply()` sets `State.last_changed` directly (no public HA API for this)
   and clears the state's internal `_cache` dict so the new value is visible;
   if the cache shape is unrecognized (future HA internals change) it raises a
   repair issue instead of crashing (`_degraded` flag, `ISSUE_INCOMPATIBLE`).
6. Entities still unavailable/unknown go into `self._pending` and get a second
   chance via a state-change listener plus delayed retries (`RETRY_DELAYS`,
   default +30/90/180 s) — catches Zigbee/Z-Wave devices that boot slowly.
   Individual unavailable→real transitions are debounce-coalesced
   (`PENDING_RECOVERY_DEBOUNCE_SECONDS`, capped by
   `PENDING_RECOVERY_MAX_WAIT_SECONDS`) into a burst set and drained
   together by `_drain_pending_recovery_burst`, reusing the boot pass's
   streaming bulk-query machinery (`_iter_bulk_batches`) instead of one
   targeted query per entity — a mass unavailable→real transition early in
   boot (a Zigbee/Z-Wave mesh finishing formation and announcing many
   devices within the same few seconds) would otherwise fire one recorder
   query per entity all at once, the same thundering-herd risk P3.1 fixed
   for the re-registration listener. Unlike that listener there's no
   per-entity retry ladder here: an entity the drain can't resolve simply
   stays in `_pending` for the existing scheduled retry passes
   (`_schedule_retries` / `_patch_all_pending`) to pick up later.
   `_patch_all_pending` snapshots `_pending` at the start of its sweep but
   re-checks membership immediately before each `_patch_pending` call, not
   just once at snapshot time — a scheduled retry pass and
   `_drain_pending_recovery_burst` can run concurrently (both are
   coroutines that yield control at their own await points), and without
   the re-check the retry pass would issue a redundant recorder query for
   an entity the drain already resolved and discarded moments earlier.

**Two persistent listeners plus a periodic discovery sweep run for the whole
entry lifetime (not just boot), set up once from `_async_run_impl`:**
- `_setup_reregister_listener` — fires on two distinct HA-level artifacts,
  both of which reset an entity's `last_changed` to "now" without the value
  having genuinely changed: (1) an already-watched entity fully disappearing
  and reappearing (config entry reload, device rejoin — `old_state is
  None`), and (2) an entity merely recovering from `unavailable`/`unknown`
  to a real value during normal runtime (a brief network/Zigbee/Z-Wave mesh
  blip — `old_state.state in INVALID_STATES`). HA's state machine treats
  `unavailable` as a distinct value, so recovering from it bumps
  `last_changed` the same way a restart does, but unlike a restart nothing
  else ever revisits that entity afterwards — left uncorrected, an entity
  that flaps `unavailable` periodically (common for exactly this class of
  device) drifts wrong forever, self-healing only at the next full restart.
  Both conditions are patched the same way, independent of the boot
  pending/listener machinery (`_stop_boot_machinery` vs. the full
  `shutdown()` intentionally only tears down the boot-specific half).
  Qualifying events are debounce-coalesced (`REREGISTER_DEBOUNCE_SECONDS`,
  capped by `REREGISTER_MAX_WAIT_SECONDS`) into a burst set and drained
  together by `_drain_reregister_burst`, which reuses the same streaming
  bulk-query machinery as the boot pass (`_iter_bulk_batches`) rather than
  issuing one targeted recorder query per entity — otherwise a mass event (a
  hub's config entry reloading with hundreds of entities, a coordinator
  reconnecting after an outage) would fire one recorder query per entity all
  at once. An entity is marked `_unconfirmed` the moment it's queued for the
  drain (not just on a failed attempt), since `live.last_changed` is the raw
  reset artifact for as long as it sits in the debounce window. Entities the
  drain can't resolve still fall back to the existing per-entity retry
  ladder (`_attempt_reregister_patch` / `RETRY_DELAYS`), which stays
  untouched since retries are already spread out in time and aren't the
  burst risk.
- `_setup_incremental_listener` — every genuine value change of a watched
  entity is debounce-merged (`INCREMENTAL_DEBOUNCE_SECONDS`, capped by
  `INCREMENTAL_MAX_WAIT_SECONDS`) into the same store used for the periodic/
  shutdown snapshot, so it stays close to real-time instead of only updating
  every `snapshot_interval` or at shutdown. Excludes both re-registrations
  (`old_state is None`) and recoveries from `unavailable`/`unknown`
  (`old_state.state in INVALID_STATES`) — both are the re-registration
  listener's job above, and merging either one in here would write the raw
  reset artifact into the snapshot store as if it were a real value change,
  poisoning it the same way `async_write_snapshot` guards against elsewhere.

Both listeners above subscribe against `self._known_targets`, a *fixed*
`entity_id` set — `async_track_state_change_event` has no API to add IDs to
an existing subscription. `_async_run_impl` only sets `self._known_targets`
once, from whatever `resolve_targets()` returns at that instant (entities
that already have a live state). An entity whose owning integration is
still mid-setup at boot (a coordinator doing its first data fetch before
creating entities — common for cloud/hub/mesh integrations enumerating many
child devices, observed taking anywhere from seconds to ~17 minutes after
HA "started" on a real installation) is invisible to that snapshot, and
therefore to *every* patch mechanism this integration has, for the rest of
the session — it only gets a chance again at the next full restart, where
the same race can recur. `async_verify` doesn't have this gap since it
re-resolves targets fresh on every call, which is why `verify` can report a
correct answer for an entity nothing ever actually corrects live.

`_async_scan_for_new_targets` closes this: a periodic timer
(`NEW_TARGET_SCAN_INTERVAL_SECONDS`) re-resolves targets and diffs against
`self._known_targets`. Any newly-matching entities are folded in, both
persistent listeners are cancelled and recreated via
`_resubscribe_persistent_listeners` against the enlarged set, and the new
entities are driven through `_drain_reregister_burst` — the exact same
batched resolve/patch path a runtime re-registration burst already uses,
since "just discovered" and "just re-registered" both start from a live
`last_changed` that's a raw reset/creation artifact rather than a real
value. A sweep rather than an event-driven listener is deliberate: the
failure mode is minutes-scale (matching the existing `RETRY_DELAYS`
timescale), so a periodic re-check closes essentially the whole gap without
adding a new *permanently* subscribed global `state_changed` listener that
would run on every state change instance-wide, forever, to shave a few
minutes off an already-minutes-scale problem. Because `_async_run_impl`
must set up this sweep (and the two listeners above) even when
`resolve_targets()` currently returns nothing — a domain/label/area-based
config where that domain's integration hasn't created any entities yet at
boot is precisely the case the sweep exists to cover — the `if not targets:
return 0` short-circuit only skips the boot candidate-patching loop below
it, not this setup.

The same timer also drives a second, independent sweep over entities that
already existed (or were already discovered) but whose resolve attempt
simply *failed* — an equally permanent dead end that predates the
discovery sweep. Inside the boot pass, a candidate with no live state at
all becomes a boot-pending entity (`self._pending`) and gets both a
listener and the `RETRY_DELAYS` retry ladder; but a candidate that *does*
have a live value and simply fails `_resolve()` (e.g. a recorder
cold-start race on a large install, where the true answer only becomes
queryable a few minutes after boot — the same race the discovery sweep
covers for missing entities, just for a value the recorder hasn't caught
up on yet rather than an entity that doesn't exist yet) is marked
`self._unconfirmed` and then abandoned: no retry ladder, no listener,
nothing revisits it again unless its value genuinely changes or it's fully
re-registered — neither of which happens for the largely-static
diagnostic/config entities this hits hardest. The exact same gap exists in
`_drain_reregister_burst` for a re-registration/discovery-sweep candidate
that fails to resolve on its first attempt (that path at least arms
`_schedule_reregister_retries`, but once that ladder also exhausts, the
entity is abandoned the same way) — and, until P7, for a boot-pending
entity too: once its one recovery-triggered resolve attempt (or the
`RETRY_DELAYS` ladder) exhausted without success, `self._pending` had no
further retry mechanism of its own, and the periodic sweep explicitly
excluded anything still in `self._pending` from its own retry set,
recreating the identical dead end for a different population.
`self._unconfirmed` is already the durable, always-correct signal for
"this entity's live `last_changed` is still a fake artifact" — set on
every resolve failure, reliably cleared the moment an entity is genuinely
patched (`_apply`) or genuinely changes value (the incremental listener) —
so `_async_scan_for_new_targets` also re-attempts everything still in
`self._unconfirmed` via `_drain_unconfirmed_burst`, **including** entities
still in `self._pending`: `NEW_TARGET_SCAN_INTERVAL_SECONDS` (300s) is
longer than the boot-time retry ladder's maximum delay (`RETRY_DELAYS`
tops out at 180s), so this sweep's first tick can never fire while that
ladder is still active, and `_drain_unconfirmed_burst`'s own per-entity
live-state check already skips anything genuinely still unavailable at
zero recorder cost. A resolve that succeeds here for a still-`_pending`
entity also clears its `_pending` bookkeeping (see `_resolve_candidates`)
— otherwise that entity's boot-pending job could never reach the "final"
finalization that only fires once `_pending` is fully empty
(`_cleanup_if_done`).

Critically, `_drain_unconfirmed_burst` does **not** grace-filter its
candidates the way `_drain_reregister_burst` does: that filter asks "does
this look like a fresh restart artifact right now?", which is exactly
wrong for an entity whose artifact is now hours or days stale by
wall-clock time — that staleness is the bug, not a reason to skip it.
Membership in `self._unconfirmed` is already the trust signal regardless
of age. The two drains otherwise share their streaming resolve/apply loop
via `_resolve_candidates`, parameterized by an optional `on_unresolved`
callback — `_drain_reregister_burst` uses it to arm the reregistration
retry ladder, `_drain_unconfirmed_burst` passes none, since anything still
unresolved simply stays in `self._unconfirmed` for the next periodic sweep
tick to retry.

**`last_triggered`** (for `automation.*`/`script.*`) is a *separate* patch
path (`_maybe_restore_last_triggered` / `_resolve_last_triggered` /
`_apply_last_triggered`) — it's an attribute, not the state value, so it needs
its own recorder read and its own apply mechanism instead of the
`last_changed`/state-value logic above.

**Snapshot store** (`Store`-backed, `STORAGE_KEY`): written on clean shutdown,
optionally periodically (`snapshot_interval`), and incrementally as above.
Only usable as a resolve source if the entity still holds the *same* value
recorded in the snapshot, otherwise the timestamp belongs to a different
value. A separate small store (`STORAGE_KEY_RUNS`) keeps a rolling history
(`MAX_RUN_HISTORY`) of boot-pass stats, feeding the status sensor/diagnostics.
`async_write_snapshot` (periodic timer and clean-shutdown write) trusts an
entity's *current* `live.state`/`live.last_changed` as ground truth only if
it's in `self._confirmed` — an **allowlist**, not a blocklist. Being merely
absent from `self._unconfirmed` is not enough: a boot candidate the
candidate-building loop classified as "skip" (elapsed since
`live.last_changed` > `grace`, assumed already genuinely used since boot)
is never added to `self._unconfirmed` at all — it gets no tracking,
positive or negative. That classification only holds if `_async_run_impl`
itself runs shortly after the true HA restart; if it runs late (a slow
recorder migration, general startup congestion — plausible right after an
HA/integration upgrade), a still-pristine restart-artifact entity slips
through untracked. Trusting `live.last_changed` for such an entity would
poison the snapshot with that artifact: on every future boot, `_resolve()`'s
snapshot step sees the same still-unchanged value and a stored timestamp
that's comfortably older than the *new* restart's cutoff, clears the margin
check, and confidently reapplies that wrong value — permanently, since
nothing before this ever re-checks an already-"confirmed" entity.
`self._confirmed` tracks exactly the opposite, narrower set: entities
positively verified this session, either via a successful `_resolve()`+
`_apply()` or a genuine value transition observed by the incremental
listener. It's populated/revoked only through two small helpers,
`_mark_confirmed`/`_mark_unconfirmed`, which keep it and `self._unconfirmed`
from drifting out of sync (every place that used to touch one now touches
both) — an entity that re-registers or otherwise becomes suspect again
after having been confirmed earlier this session has its trust revoked
immediately, before whatever drain resolves it completes, since a snapshot
write could race in during that window. Anything not in `self._confirmed`
has its *existing* stored entry (if any) carried forward unchanged instead
of being freshly trusted off an unverified assumption — no worse than
before, and not actively made wrong. Upgrading past the fix does not
retroactively clean an *already*-poisoned on-disk snapshot — clear
`.storage/last_changed_keeper.snapshot` once after upgrading to let it
rebuild through the now-safe resolve chain.

**Service `last_changed_keeper.restore_now`** and event `last_changed_keeper_restored`
(fired `final=False` after the initial pass, `final=True` once `_pending`
drains) let automations trigger/await a pass without racing it. Service
`last_changed_keeper.verify` (`async_verify`) is diagnostic-only — runs the
same resolve chain but never patches, used to explain "why is entity X wrong"
without side effects.

When changing resolve/apply logic, check `_resolve`'s docstring comments for
the reasoning behind each source's priority and the "bounded vs. best-effort"
distinction — it's not obvious from the code alone and getting the order
wrong risks silently backdating an entity's *new* value using its *old*
timestamp.

`tests/test_resolve_equivalence.py` is the one place in the suite that
exercises a real recorder round trip (genuine historical rows written via
`freeze_time`, fetched by an unmocked `_bulk_query`) rather than synthetic
`FakeRow` history handed to `_resolve` directly — everything else in the
suite tests the pure decision logic. It pins final resolved timestamps at a
few canonical real distances into the past (inside the bulk window, beyond
it, a restart-recovery pattern, within the margin), so it's the regression
net for any change to *how* history is fetched (e.g. querying a short
window first and escalating only for entities still unbounded) — those
tests should keep passing unchanged across such a change, since they assert
on outcomes rather than on the fetch strategy itself.
