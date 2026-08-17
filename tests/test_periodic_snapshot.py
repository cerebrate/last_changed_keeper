"""Tests for the periodic snapshot timer: a snapshot is written not only on
clean shutdown but also every snapshot_interval seconds, hedging against a
crash/power-loss where EVENT_HOMEASSISTANT_STOP never fires.
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
    CONF_DOMAINS,
    CONF_ENTITIES,
    CONF_SNAPSHOT_INTERVAL,
    DOMAIN,
    STORAGE_KEY,
)


async def test_periodic_snapshot_writes_on_interval(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_DOMAINS: ["light"],
            CONF_ENTITIES: [],
            CONF_SNAPSHOT_INTERVAL: 300,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert STORAGE_KEY not in hass_storage

    # A genuine value change confirms the entity (see _unconfirmed in
    # __init__.py) so the periodic write is free to persist it — a fresh
    # boot candidate with no recorder history never gets confirmed, and
    # asserting on one here would just be re-testing the poisoning bug
    # fixed by test_periodic_snapshot_does_not_poison_unconfirmed_entity
    # below.
    hass.states.async_set("light.kitchen", "off")
    await hass.async_block_till_done()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=310))
    await hass.async_block_till_done()

    assert STORAGE_KEY in hass_storage
    assert "light.kitchen" in hass_storage[STORAGE_KEY]["data"]


async def test_periodic_snapshot_does_not_poison_unconfirmed_entity(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage
) -> None:
    """Regression: a fresh boot candidate with no recorder history that
    _resolve() never manages to patch must not have its raw restart-reset
    last_changed written into the snapshot store by the periodic timer —
    that value would then be wrongly trusted as real on every future boot
    (see the _unconfirmed docstring on async_write_snapshot)."""
    hass.states.async_set("light.kitchen", "on")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_DOMAINS: ["light"],
            CONF_ENTITIES: [],
            CONF_SNAPSHOT_INTERVAL: 300,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=310))
    await hass.async_block_till_done()

    assert STORAGE_KEY in hass_storage
    assert "light.kitchen" not in hass_storage[STORAGE_KEY]["data"]


async def test_snapshot_interval_zero_disables_periodic_write(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_DOMAINS: ["light"],
            CONF_ENTITIES: [],
            CONF_SNAPSHOT_INTERVAL: 0,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3600))
    await hass.async_block_till_done()

    assert STORAGE_KEY not in hass_storage

