"""Tests for the last_changed_keeper.verify service: a diagnostic-only pass
that compares live last_changed against the recorder/store-derived real
value for every current target, without patching anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.last_changed_keeper as lck
from custom_components.last_changed_keeper.const import (
    CONF_ALL_ENTITIES,
    CONF_DOMAINS,
    CONF_ENTITIES,
    DOMAIN,
    SERVICE_VERIFY,
)


@dataclass
class FakeRow:
    state: str
    last_changed: object
    last_updated: object


async def _add_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_DOMAINS: ["light"], CONF_ENTITIES: []}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_verify_reports_mismatch_when_live_differs_from_expected(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = await _add_entry(hass)
    job = entry.runtime_data

    stale = dt_util.utcnow() - timedelta(days=2)
    job._snapshot["light.kitchen"] = {"s": "on", "t": stale.isoformat()}

    result = await job.async_verify()

    assert result["checked"] >= 1
    mismatch = next(
        m for m in result["mismatches"] if m["entity_id"] == "light.kitchen"
    )
    assert mismatch["expected_last_changed"] == stale.isoformat()
    assert mismatch["diff_seconds"] > 0


async def test_verify_reports_bulk_and_deep_query_instrumentation(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, monkeypatch
) -> None:
    """bulk_rows_fetched/bulk_batches/deep_queries exist so the recorder
    cost of a verify pass - historically the worst offender for memory,
    since it checks every target rather than just the fresh boot
    candidates - is visible from the service response instead of only
    inferable from how long the call took."""
    hass.states.async_set("light.bulk_hit", "on")
    hass.states.async_set("light.falls_through", "on")
    # CONF_ALL_ENTITIES: False (unlike _add_entry, which defaults to the
    # component's own True default) - the assertions below need an exact
    # target set of just these two entities, since _add_entry's default
    # would also pull in this entry's own diagnostic status sensor.
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ALL_ENTITIES: False, CONF_DOMAINS: ["light"], CONF_ENTITIES: []},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    job = entry.runtime_data

    live = hass.states.get("light.bulk_hit")
    now = live.last_changed
    two_rows = [
        FakeRow("off", now - timedelta(hours=1), now - timedelta(hours=1)),
        FakeRow("on", now - timedelta(minutes=10), now - timedelta(minutes=10)),
    ]

    def fake_bulk_query(_hass, _start, entity_ids):
        return {"light.bulk_hit": two_rows} if "light.bulk_hit" in entity_ids else {}

    monkeypatch.setattr(lck, "_bulk_query", fake_bulk_query)
    monkeypatch.setattr(lck, "BULK_BATCH_SIZE", 1)

    result = await job.async_verify()

    # light.bulk_hit resolves from its bounded bulk result (2 rows, no deep
    # query needed); light.falls_through has none, so it falls through.
    assert result["bulk_rows_fetched"] == 2
    assert result["bulk_batches"] == 2  # one per entity at batch size 1
    assert result["deep_queries"] == 1


async def test_verify_no_mismatches_when_nothing_resolvable(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = await _add_entry(hass)
    job = entry.runtime_data

    result = await job.async_verify()

    assert result["mismatches"] == []


async def test_verify_never_patches_anything(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = await _add_entry(hass)
    job = entry.runtime_data

    stale = dt_util.utcnow() - timedelta(days=2)
    job._snapshot["light.kitchen"] = {"s": "on", "t": stale.isoformat()}
    before = hass.states.get("light.kitchen").last_changed

    await job.async_verify()

    after = hass.states.get("light.kitchen").last_changed
    assert after == before  # diagnostic only - nothing was patched


async def test_verify_service_registered_and_returns_response(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    await _add_entry(hass)

    assert hass.services.has_service(DOMAIN, SERVICE_VERIFY)
    result = await hass.services.async_call(
        DOMAIN, SERVICE_VERIFY, {}, blocking=True, return_response=True
    )
    assert "mismatches" in result
    assert "checked" in result


async def test_verify_raises_when_no_entry_loaded(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    entry = await _add_entry(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    try:
        await hass.services.async_call(
            DOMAIN, SERVICE_VERIFY, {}, blocking=True, return_response=True
        )
    except ServiceValidationError:
        pass
    else:
        raise AssertionError("expected ServiceValidationError")


async def test_verify_wraps_failure_as_home_assistant_error(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, monkeypatch
) -> None:
    entry = await _add_entry(hass)

    async def _boom(self):
        raise RuntimeError("recorder exploded")

    monkeypatch.setattr(type(entry.runtime_data), "async_verify", _boom)

    try:
        await hass.services.async_call(
            DOMAIN, SERVICE_VERIFY, {}, blocking=True, return_response=True
        )
    except HomeAssistantError as err:
        assert not isinstance(err, ServiceValidationError)
    else:
        raise AssertionError("expected HomeAssistantError")
