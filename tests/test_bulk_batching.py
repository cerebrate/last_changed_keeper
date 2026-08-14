"""Tests for the batched bulk recorder query and the post-boot self-check."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.last_changed_keeper as lck
from custom_components.last_changed_keeper import _RestoreJob
from custom_components.last_changed_keeper.const import (
    CONF_ALL_ENTITIES,
    CONF_BULK_BATCH_SIZE,
    CONF_DOMAINS,
    CONF_ENTITIES,
    CONF_VERIFY_AFTER_BOOT,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)


def _make_job(hass: HomeAssistant, options: dict | None = None) -> _RestoreJob:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options or {})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    return _RestoreJob(hass, entry, store, {})


async def test_bulk_fetch_splits_into_batches(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def fake_bulk_query(_hass, _start, entity_ids):
        calls.append(list(entity_ids))
        return {eid: [] for eid in entity_ids}

    monkeypatch.setattr(lck, "_bulk_query", fake_bulk_query)
    monkeypatch.setattr(lck, "BULK_BATCH_SIZE", 2)

    job = _make_job(hass)
    ids = [f"light.l{i}" for i in range(5)]
    out = await job._bulk_fetch(ids)

    assert [len(c) for c in calls] == [2, 2, 1]
    assert set(out) == set(ids)


async def test_bulk_fetch_failed_batch_only_loses_its_own_entities(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    def fake_bulk_query(_hass, _start, entity_ids):
        if "light.l2" in entity_ids:
            raise RuntimeError("boom")
        return {eid: [] for eid in entity_ids}

    monkeypatch.setattr(lck, "_bulk_query", fake_bulk_query)
    monkeypatch.setattr(lck, "BULK_BATCH_SIZE", 2)

    job = _make_job(hass)
    out = await job._bulk_fetch([f"light.l{i}" for i in range(5)])

    # Batch [l2, l3] failed; the other two batches still delivered.
    assert set(out) == {"light.l0", "light.l1", "light.l4"}


async def test_bulk_fetch_uses_configured_batch_size(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def fake_bulk_query(_hass, _start, entity_ids):
        calls.append(list(entity_ids))
        return {eid: [] for eid in entity_ids}

    monkeypatch.setattr(lck, "_bulk_query", fake_bulk_query)
    # Module default stays at 500; the per-entry option must win over it.
    job = _make_job(hass, options={CONF_BULK_BATCH_SIZE: 3})
    out = await job._bulk_fetch([f"light.l{i}" for i in range(7)])

    assert [len(c) for c in calls] == [3, 3, 1]
    assert set(out) == {f"light.l{i}" for i in range(7)}


async def test_bulk_batch_size_falls_back_to_default_when_invalid(
    recorder_mock, hass: HomeAssistant
) -> None:
    job = _make_job(hass, options={CONF_BULK_BATCH_SIZE: 0})
    assert job._bulk_batch_size == lck.BULK_BATCH_SIZE

    job = _make_job(hass, options={CONF_BULK_BATCH_SIZE: "not-a-number"})
    assert job._bulk_batch_size == lck.BULK_BATCH_SIZE


async def test_verify_after_boot_disabled_by_default(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ALL_ENTITIES: False, CONF_DOMAINS: ["light"], CONF_ENTITIES: []},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data._unsub_verify_timer is None


async def test_verify_after_boot_schedules_timer_when_enabled(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ALL_ENTITIES: False,
            CONF_DOMAINS: ["light"],
            CONF_ENTITIES: [],
            CONF_VERIFY_AFTER_BOOT: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    job = entry.runtime_data
    assert job._unsub_verify_timer is not None

    # Unload must cancel it (shutdown path).
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert job._unsub_verify_timer is None
