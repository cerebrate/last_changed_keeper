"""Tests for the status sensor and the rolling run history."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.last_changed_keeper.const import (
    CONF_ALL_ENTITIES,
    CONF_DOMAINS,
    CONF_ENTITIES,
    DOMAIN,
    MAX_RUN_HISTORY,
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


async def test_status_sensor_created_with_stats_attributes(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = await _add_entry(hass)

    states = [
        s for s in hass.states.async_all("sensor")
        if s.attributes.get("last_run") is not None
    ]
    assert len(states) == 1
    sensor = states[0]
    assert sensor.state.isdigit()
    assert sensor.attributes["degraded"] is False
    assert sensor.attributes["last_run"]["targets"] >= 1
    # The boot pass was recorded in the run history too.
    assert len(entry.runtime_data.run_history) == 1
    assert sensor.attributes["run_history"] == entry.runtime_data.run_history


async def test_run_history_trimmed_to_max(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = await _add_entry(hass)
    job = entry.runtime_data

    for _ in range(MAX_RUN_HISTORY + 5):
        job._persist_run_stats(new_run=True)
    assert len(job.run_history) == MAX_RUN_HISTORY


async def test_persist_run_stats_replaces_last_record_in_place(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = await _add_entry(hass)
    job = entry.runtime_data

    before = len(job.run_history)
    job.stats["patched_total"] = 42
    job._persist_run_stats(new_run=False)
    assert len(job.run_history) == before
    assert job.run_history[-1]["patched_total"] == 42


async def test_sensor_unloaded_with_entry(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = await _add_entry(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    remaining = [
        s for s in hass.states.async_all("sensor")
        if s.attributes.get("last_run") is not None and s.state != "unavailable"
    ]
    assert remaining == []
