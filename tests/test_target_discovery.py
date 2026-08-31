"""Tests for the periodic target-discovery sweep: _async_run_impl resolves
targets exactly once, at boot, from whatever entities already have a live
state at that instant. An entity whose owning integration is still
mid-setup at that moment (a coordinator doing its first data fetch before
creating entities) is invisible to that one-time snapshot, and therefore
invisible to every patch mechanism this integration has, until the next
full restart. _async_scan_for_new_targets re-resolves targets on a timer
and folds newly-matching entities into self._known_targets, driving them
through the same batched _drain_reregister_burst pipeline a runtime
re-registration burst already uses, then resubscribes both persistent
listeners so the entity is covered going forward without waiting for
another sweep.
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.last_changed_keeper.const import (
    CONF_ALL_ENTITIES,
    CONF_DOMAINS,
    CONF_ENTITIES,
    CONF_EXCLUDE,
    DOMAIN,
    EVENT_RESTORED,
    NEW_TARGET_SCAN_INTERVAL_SECONDS,
    PENDING_RECOVERY_DEBOUNCE_SECONDS,
    REREGISTER_DEBOUNCE_SECONDS,
    STORAGE_KEY,
)


async def _flush_discovery_sweep(hass: HomeAssistant) -> None:
    """Advance past the discovery sweep interval so
    _async_scan_for_new_targets runs, then let its drain settle."""
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + timedelta(seconds=NEW_TARGET_SCAN_INTERVAL_SECONDS + 1),
    )
    await hass.async_block_till_done()


async def _flush_reregister_burst(hass: HomeAssistant) -> None:
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=REREGISTER_DEBOUNCE_SECONDS + 1)
    )
    await hass.async_block_till_done()


async def _flush_pending_recovery_burst(hass: HomeAssistant) -> None:
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + timedelta(seconds=PENDING_RECOVERY_DEBOUNCE_SECONDS + 1),
    )
    await hass.async_block_till_done()


async def _add_entry(
    hass: HomeAssistant, exclude: list[str] | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ALL_ENTITIES: False,
            CONF_DOMAINS: ["light"],
            CONF_ENTITIES: [],
            CONF_EXCLUDE: exclude or [],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_new_entity_discovered_and_patched_from_snapshot(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage
) -> None:
    """An entity that doesn't exist yet when the boot pass takes its
    one-time targets snapshot must still get picked up, once its owning
    integration finally creates it, by the next discovery sweep."""
    stale = dt_util.utcnow() - timedelta(days=2)
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"light.late_bulb": {"s": "on", "t": stale.isoformat()}},
    }
    await _add_entry(hass)  # boot pass runs; light.late_bulb doesn't exist yet

    hass.states.async_set("light.late_bulb", "on")
    await hass.async_block_till_done()
    await _flush_discovery_sweep(hass)

    live = hass.states.get("light.late_bulb")
    assert live.last_changed == stale


async def test_discovered_entity_re_registration_caught_without_another_sweep(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage
) -> None:
    """Once a sweep discovers an entity, the persistent re-registration
    listener must actually be resubscribed to include it - a later
    re-registration is caught immediately, not only on the next sweep."""
    stale = dt_util.utcnow() - timedelta(days=2)
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"light.late_bulb": {"s": "on", "t": stale.isoformat()}},
    }
    await _add_entry(hass)
    hass.states.async_set("light.late_bulb", "on")
    await hass.async_block_till_done()
    await _flush_discovery_sweep(hass)
    assert hass.states.get("light.late_bulb").last_changed == stale

    # Its owning config entry reloads again - old_state is None, same as a
    # runtime re-registration of an entity that was already known at boot.
    hass.states.async_remove("light.late_bulb")
    await hass.async_block_till_done()
    hass.states.async_set("light.late_bulb", "on")
    await hass.async_block_till_done()
    await _flush_reregister_burst(hass)

    live = hass.states.get("light.late_bulb")
    assert live.last_changed == stale


async def test_excluded_entity_never_gets_swept_in(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    entry = await _add_entry(hass, exclude=["light.excluded_bulb"])
    job = entry.runtime_data

    hass.states.async_set("light.excluded_bulb", "on")
    await hass.async_block_till_done()
    await _flush_discovery_sweep(hass)

    assert "light.excluded_bulb" not in job._known_targets


async def test_noop_sweep_does_not_drain_or_resubscribe(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, monkeypatch
) -> None:
    """Nothing new since the last check -> the sweep must be a cheap no-op,
    not a redundant drain/resubscribe cycle."""
    entry = await _add_entry(hass)
    job = entry.runtime_data

    drain_calls: list[set[str]] = []
    orig_drain = job._drain_reregister_burst

    async def fake_drain(entity_ids: set[str]) -> None:
        drain_calls.append(set(entity_ids))
        await orig_drain(entity_ids)

    monkeypatch.setattr(job, "_drain_reregister_burst", fake_drain)

    resubscribe_calls: list[bool] = []
    orig_resubscribe = job._resubscribe_persistent_listeners

    def fake_resubscribe() -> None:
        resubscribe_calls.append(True)
        orig_resubscribe()

    monkeypatch.setattr(job, "_resubscribe_persistent_listeners", fake_resubscribe)

    await _flush_discovery_sweep(hass)

    assert drain_calls == []
    assert resubscribe_calls == []


async def test_discovered_entity_still_unavailable_not_force_patched(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage
) -> None:
    """An entity discovered while still unavailable is added to
    self._known_targets (so it's subscribed going forward) but left alone
    by the drain - its eventual recovery is caught by the already-tested
    re-registration listener path, exactly like any other tracked entity."""
    stale = dt_util.utcnow() - timedelta(days=2)
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"light.slow_bulb": {"s": "on", "t": stale.isoformat()}},
    }
    entry = await _add_entry(hass)
    job = entry.runtime_data

    hass.states.async_set("light.slow_bulb", "unavailable")
    await hass.async_block_till_done()
    await _flush_discovery_sweep(hass)

    assert "light.slow_bulb" in job._known_targets
    assert hass.states.get("light.slow_bulb").state == "unavailable"

    hass.states.async_set("light.slow_bulb", "on")
    await hass.async_block_till_done()
    await _flush_reregister_burst(hass)

    live = hass.states.get("light.slow_bulb")
    assert live.last_changed == stale


async def test_unload_cancels_target_discovery_timer(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    entry = await _add_entry(hass)
    job = entry.runtime_data
    assert job._unsub_target_discovery_timer is not None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert job._unsub_target_discovery_timer is None


async def test_stale_unconfirmed_entity_retried_and_patched_on_later_sweep(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage
) -> None:
    """A candidate that exists at boot but whose resolve attempt fails (no
    usable snapshot entry, no recorder history yet) must not be abandoned
    forever - the periodic sweep keeps retrying it, and picks it up the
    moment a real answer becomes available, e.g. once the incremental/
    periodic snapshot store gains an entry for it. This also proves the
    retry is not grace-gated: several sweep ticks push the entity's live
    last_changed well past the default grace window before it resolves."""
    hass.states.async_set("light.stuck_bulb", "on")
    entry = await _add_entry(hass)
    job = entry.runtime_data
    await hass.async_block_till_done()

    boot_last_changed = hass.states.get("light.stuck_bulb").last_changed
    assert "light.stuck_bulb" in job._unconfirmed

    # Several sweep ticks with nothing new to resolve it from - still
    # stuck, and by now well past the default grace window (1800s).
    for _ in range(7):
        await _flush_discovery_sweep(hass)
    assert hass.states.get("light.stuck_bulb").last_changed == boot_last_changed
    assert "light.stuck_bulb" in job._unconfirmed

    real = boot_last_changed - timedelta(days=2)
    job._snapshot["light.stuck_bulb"] = {"s": "on", "t": real.isoformat()}

    await _flush_discovery_sweep(hass)

    live = hass.states.get("light.stuck_bulb")
    assert live.last_changed == real
    assert "light.stuck_bulb" not in job._unconfirmed


async def test_pending_entity_retried_and_patched_by_later_sweep(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """An entity unavailable at boot goes into self._pending and gets one
    recovery-triggered resolve attempt once it comes back (via the
    boot-pending listener/_drain_pending_recovery_burst) - if that one
    attempt fails (no usable snapshot/recorder answer yet), it must not be
    abandoned forever: the periodic sweep no longer excludes self._pending,
    so a later sweep tick gets another chance once a real answer becomes
    available, e.g. once the snapshot store gains a usable entry. Success
    there must also clear both self._pending and self._unconfirmed, and -
    since this is the only pending entity - fire the final restored event
    and tear down the boot-pending listener/timers."""
    hass.states.async_set("light.slow_join", "unavailable")
    entry = await _add_entry(hass)
    job = entry.runtime_data
    await hass.async_block_till_done()

    assert "light.slow_join" in job._pending
    assert "light.slow_join" in job._unconfirmed

    events: list[dict] = []
    hass.bus.async_listen(EVENT_RESTORED, lambda e: events.append(e.data))

    # Recovers, but with nothing yet to resolve it against - the one
    # recovery-triggered attempt fails and leaves it stuck.
    hass.states.async_set("light.slow_join", "on")
    await hass.async_block_till_done()
    await _flush_pending_recovery_burst(hass)

    assert "light.slow_join" in job._pending
    assert "light.slow_join" in job._unconfirmed
    assert job._unsub_listener is not None

    real = dt_util.utcnow() - timedelta(days=2)
    job._snapshot["light.slow_join"] = {"s": "on", "t": real.isoformat()}

    await _flush_discovery_sweep(hass)

    live = hass.states.get("light.slow_join")
    assert live.last_changed == real
    assert "light.slow_join" not in job._pending
    assert "light.slow_join" not in job._unconfirmed
    assert job._unsub_listener is None
    assert job._unsub_timers == []
    assert events[-1]["final"] is True
    assert events[-1]["pending"] == 0


async def test_still_unavailable_pending_entity_not_force_patched_by_sweep(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """An entity that never recovered from unavailable is left alone by the
    periodic sweep - not force-patched, stays self._pending and
    self._unconfirmed - its eventual recovery is still the boot-pending
    listener's job, this sweep just no longer refuses to also help once it
    has recovered."""
    hass.states.async_set("light.never_joins", "unavailable")
    entry = await _add_entry(hass)
    job = entry.runtime_data
    await hass.async_block_till_done()

    assert "light.never_joins" in job._pending
    assert "light.never_joins" in job._unconfirmed

    await _flush_discovery_sweep(hass)  # must not raise or force-patch

    assert hass.states.get("light.never_joins").state == "unavailable"
    assert "light.never_joins" in job._pending
    assert "light.never_joins" in job._unconfirmed


async def test_unconfirmed_entity_gone_unavailable_is_skipped_not_crashed(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """An entity that was unconfirmed but has since gone unavailable must
    be skipped by the unconfirmed sweep (not force-patched, not crashed)
    and left in self._unconfirmed - its eventual recovery is the existing
    re-registration listener's job, not this sweep's."""
    hass.states.async_set("light.flaky_bulb", "on")
    entry = await _add_entry(hass)
    job = entry.runtime_data
    await hass.async_block_till_done()
    assert "light.flaky_bulb" in job._unconfirmed

    hass.states.async_set("light.flaky_bulb", "unavailable")
    await hass.async_block_till_done()

    await _flush_discovery_sweep(hass)  # must not raise

    assert "light.flaky_bulb" in job._unconfirmed
