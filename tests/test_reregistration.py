"""Tests for the runtime re-registration path: an already-watched entity
that gets fully re-created (e.g. its owning config entry reloads, or a
Zigbee/Z-Wave device rejoins) resets last_changed to "now" the same way a
full HA restart does. A persistent, entry-lifetime listener (independent of
the boot-time pending/listener machinery) catches this and re-patches just
that one entity, respecting the same grace window and retry_delays.
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
    DOMAIN,
    RETRY_DELAYS,
    STORAGE_KEY,
)


async def _add_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ALL_ENTITIES: False,
            CONF_DOMAINS: ["light"],
            CONF_ENTITIES: [],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_reregistration_patches_from_snapshot(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage
) -> None:
    stale = dt_util.utcnow() - timedelta(days=2)
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"light.kitchen": {"s": "on", "t": stale.isoformat()}},
    }
    hass.states.async_set("light.kitchen", "on")
    await _add_entry(hass)

    # Simulate a runtime re-registration: the entity disappears and comes
    # back with a fresh last_changed, same as a config-entry reload or a
    # Zigbee device rejoin.
    hass.states.async_remove("light.kitchen")
    await hass.async_block_till_done()
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()

    live = hass.states.get("light.kitchen")
    assert live.last_changed == stale


async def test_reregistration_ignored_when_grace_exceeded(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage
) -> None:
    """A re-registration long after boot (e.g. simulated by a state that is
    already older than grace) must not be touched."""
    stale = dt_util.utcnow() - timedelta(days=2)
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"light.kitchen": {"s": "on", "t": stale.isoformat()}},
    }
    hass.states.async_set("light.kitchen", "on")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_DOMAINS: ["light"], CONF_ENTITIES: [], "grace_seconds": 60},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_remove("light.kitchen")
    await hass.async_block_till_done()
    old_ts = dt_util.utcnow() - timedelta(seconds=120)
    hass.states.async_set("light.kitchen", "on", timestamp=old_ts.timestamp())
    await hass.async_block_till_done()

    live = hass.states.get("light.kitchen")
    assert live.last_changed == old_ts  # untouched: already older than grace


async def test_reregistration_retries_when_not_immediately_resolvable(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = await _add_entry(hass)
    job = entry.runtime_data

    hass.states.async_remove("light.kitchen")
    await hass.async_block_till_done()
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()

    # Nothing resolvable yet (no recorder history, no snapshot) -> retry
    # timers scheduled for this entity instead of giving up immediately.
    assert "light.kitchen" in job._reregister_retry_timers

    # Make it resolvable before the first retry fires.
    stale = dt_util.utcnow() - timedelta(days=1)
    job._snapshot["light.kitchen"] = {"s": "on", "t": stale.isoformat()}

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RETRY_DELAYS[0] + 1)
    )
    await hass.async_block_till_done()

    live = hass.states.get("light.kitchen")
    assert live.last_changed == stale


async def test_reregistration_retry_ladder_does_not_grow_on_repeated_failure(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, monkeypatch
) -> None:
    """Regression: a retry that fails *again* must not arm a fresh ladder.

    _attempt_reregister_patch runs both as the first attempt and as every
    retry attempt. When it armed the retry ladder itself, each failing retry
    scheduled len(RETRY_DELAYS) more timers without cancelling the ones
    already armed, so the number of in-flight attempts multiplied every
    round until the grace window closed. Each attempt issues its own
    recorder query, so on a large installation this exhausted memory within
    minutes of any entity that kept failing to resolve.
    """
    hass.states.async_set("light.kitchen", "on")
    entry = await _add_entry(hass)
    job = entry.runtime_data

    attempts: list[str] = []
    original = job._attempt_reregister_patch

    async def _counting_attempt(entity_id: str, grace: float) -> bool:
        attempts.append(entity_id)
        return await original(entity_id, grace)

    monkeypatch.setattr(job, "_attempt_reregister_patch", _counting_attempt)

    hass.states.async_remove("light.kitchen")
    await hass.async_block_till_done()
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()

    # Nothing resolvable -> the first attempt failed and armed one ladder.
    assert len(job._reregister_retry_timers["light.kitchen"]) == len(RETRY_DELAYS)

    # Drive every rung while the entity stays unresolvable. The ladder must
    # never exceed its original size, however many rungs have already failed.
    start = dt_util.utcnow()
    for delay in RETRY_DELAYS:
        async_fire_time_changed(hass, start + timedelta(seconds=delay + 1))
        await hass.async_block_till_done()
        armed = job._reregister_retry_timers.get("light.kitchen", [])
        assert len(armed) <= len(RETRY_DELAYS)

    # Exactly one initial attempt plus one per retry delay - not a multiple.
    assert len(attempts) == 1 + len(RETRY_DELAYS)
    # Ladder exhausted: the entry is pruned rather than left behind holding
    # already-fired timers (one stale entry per entity, forever, otherwise).
    assert "light.kitchen" not in job._reregister_retry_timers


async def test_successful_retry_cancels_the_rest_of_the_ladder(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """Once a retry resolves the entity, the remaining rungs are dropped
    instead of running out against an already-patched state."""
    hass.states.async_set("light.kitchen", "on")
    entry = await _add_entry(hass)
    job = entry.runtime_data

    hass.states.async_remove("light.kitchen")
    await hass.async_block_till_done()
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    assert len(job._reregister_retry_timers["light.kitchen"]) == len(RETRY_DELAYS)

    stale = dt_util.utcnow() - timedelta(days=1)
    job._snapshot["light.kitchen"] = {"s": "on", "t": stale.isoformat()}

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RETRY_DELAYS[0] + 1)
    )
    await hass.async_block_till_done()

    assert hass.states.get("light.kitchen").last_changed == stale
    assert "light.kitchen" not in job._reregister_retry_timers


async def test_new_reregistration_replaces_armed_ladder(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """A flapping entity replaces its armed ladder instead of stacking a
    second one alongside it."""
    hass.states.async_set("light.kitchen", "on")
    entry = await _add_entry(hass)
    job = entry.runtime_data

    for _ in range(3):
        hass.states.async_remove("light.kitchen")
        await hass.async_block_till_done()
        hass.states.async_set("light.kitchen", "on")
        await hass.async_block_till_done()
        assert len(job._reregister_retry_timers["light.kitchen"]) == len(RETRY_DELAYS)


async def test_reregistration_of_untargeted_entity_is_ignored(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """Only entities within the resolved target set are subscribed to; a
    re-registration of anything else must not be touched or raise."""
    hass.states.async_set("light.kitchen", "on")
    hass.states.async_set("sensor.not_targeted", "5")
    entry = await _add_entry(hass)  # domains=["light"] only
    job = entry.runtime_data

    hass.states.async_remove("sensor.not_targeted")
    await hass.async_block_till_done()
    hass.states.async_set("sensor.not_targeted", "5")
    await hass.async_block_till_done()

    # Never subscribed to (not in targets) -> no attempt, no retry scheduled.
    assert "sensor.not_targeted" not in job._reregister_retry_timers


async def test_unload_cancels_reregister_listener_and_retries(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = await _add_entry(hass)
    job = entry.runtime_data

    hass.states.async_remove("light.kitchen")
    await hass.async_block_till_done()
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    assert "light.kitchen" in job._reregister_retry_timers

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert job._unsub_reregister_listener is None
    assert job._reregister_retry_timers == {}


async def test_cleanup_if_done_does_not_tear_down_reregister_listener(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Regression: _cleanup_if_done() (called when the boot pending set
    drains) must only cancel the boot-pass-specific listener/timers, not
    the persistent re-registration listener which lives for the whole
    entry lifetime."""
    from homeassistant.helpers.storage import Store

    from custom_components.last_changed_keeper import _RestoreJob
    from custom_components.last_changed_keeper.const import STORAGE_VERSION

    entry = MockConfigEntry(domain=DOMAIN, data={})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    job = _RestoreJob(hass, entry, store, {})

    torn_down: list[str] = []
    job._unsub_reregister_listener = lambda: torn_down.append("reregister")
    job._pending = set()

    job._cleanup_if_done()

    assert torn_down == []
