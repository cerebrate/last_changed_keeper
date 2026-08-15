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
   only being inferable from wall-clock duration.
4. Per entity, `_resolve()` picks the real timestamp in this priority order:
   **bulk result → incremental/periodic snapshot store (if newer for the same
   value) → deep per-entity recorder query → best-effort unbounded run**. A
   "bounded" run (recorder history shows an older, different value) is
   definitive; an "unbounded" one (history exhausted) is only trusted under
   specific conditions — see the docstrings on `_resolve` and `_real_last_changed`.
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

**Two persistent listeners run for the whole entry lifetime (not just boot),
set up once from `_async_run_impl`:**
- `_setup_reregister_listener` — an already-watched entity that fully
  disappears and reappears later (config entry reload, device rejoin) gets
  the same "now" reset a restart causes; this re-patches just that entity,
  independent of the boot pending/listener machinery (`_stop_boot_machinery`
  vs. the full `shutdown()` intentionally only tears down the boot-specific
  half).
- `_setup_incremental_listener` — every genuine value change of a watched
  entity is debounce-merged (`INCREMENTAL_DEBOUNCE_SECONDS`, capped by
  `INCREMENTAL_MAX_WAIT_SECONDS`) into the same store used for the periodic/
  shutdown snapshot, so it stays close to real-time instead of only updating
  every `snapshot_interval` or at shutdown.

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
