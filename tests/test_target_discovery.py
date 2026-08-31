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
    NEW_TARGET_SCAN_INTERVAL_SECONDS,
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
