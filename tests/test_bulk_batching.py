"""Tests for the batched bulk recorder query and the post-boot self-check."""
from __future__ import annotations

import weakref

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.last_changed_keeper as lck
from custom_components.last_changed_keeper import _RestoreJob
from custom_components.last_changed_keeper.const import (
    CONF_ALL_ENTITIES,
    CONF_DOMAINS,
    CONF_ENTITIES,
    CONF_VERIFY_AFTER_BOOT,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)


def _make_job(hass: HomeAssistant) -> _RestoreJob:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    return _RestoreJob(hass, entry, store, {})


async def test_bulk_batches_split_by_batch_size(
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
    batches = [(chunk, result) async for chunk, result in job._iter_bulk_batches(ids)]

    assert [len(c) for c in calls] == [2, 2, 1]
    assert [len(chunk) for chunk, _ in batches] == [2, 2, 1]
    # Every entity is covered exactly once across the batches.
    assert [eid for chunk, _ in batches for eid in chunk] == ids


async def test_failed_bulk_batch_only_loses_its_own_entities(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    def fake_bulk_query(_hass, _start, entity_ids):
        if "light.l2" in entity_ids:
            raise RuntimeError("boom")
        return {eid: [] for eid in entity_ids}

    monkeypatch.setattr(lck, "_bulk_query", fake_bulk_query)
    monkeypatch.setattr(lck, "BULK_BATCH_SIZE", 2)

    job = _make_job(hass)
    ids = [f"light.l{i}" for i in range(5)]

    delivered: dict[str, list] = {}
    chunked: list[str] = []
    async for chunk, result in job._iter_bulk_batches(ids):
        chunked.extend(chunk)
        delivered.update(result)

    # Batch [l2, l3] failed; the other two batches still delivered.
    assert set(delivered) == {"light.l0", "light.l1", "light.l4"}
    # ...but the failed batch still yields its chunk, so its entities are
    # resolved with no bulk result rather than skipped entirely - they fall
    # back to the snapshot/per-entity path in _resolve, as before.
    assert chunked == ids


async def test_bulk_batches_are_not_retained_across_queries(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, monkeypatch
) -> None:
    """Regression: peak memory must stay at roughly one batch.

    The bulk window is 30 days wide and rows come back as full state
    objects, so on a large installation the merged result is every
    significant state change of every entity over a month. Accumulating
    that into a single dict - and holding it live for the whole resolve
    pass - was the acute cause of multi-gigabyte spikes, most visibly on
    the verify service, which checks every target rather than just the
    fresh boot candidates.

    Each batch must therefore be unreachable by the time the next query
    runs: neither the generator nor the caller may still hold it.
    """
    class _Batch(dict):
        """Plain dicts cannot be weak-referenced; a subclass can."""

    refs: list[weakref.ref] = []
    alive_at_query: list[int] = []

    def fake_bulk_query(_hass, _start, entity_ids):
        # Count previously yielded batches still reachable right now.
        alive_at_query.append(sum(1 for ref in refs if ref() is not None))
        result = _Batch({eid: [] for eid in entity_ids})
        refs.append(weakref.ref(result))
        return result

    monkeypatch.setattr(lck, "_bulk_query", fake_bulk_query)
    monkeypatch.setattr(lck, "BULK_BATCH_SIZE", 1)

    for i in range(4):
        hass.states.async_set(f"light.l{i}", "on")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ALL_ENTITIES: False, CONF_DOMAINS: ["light"], CONF_ENTITIES: []},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    refs.clear()
    alive_at_query.clear()
    await entry.runtime_data.async_verify()

    assert len(alive_at_query) == 4  # one query per entity at batch size 1
    # No previously fetched batch is still reachable when the next query
    # runs. Before streaming this would climb 0, 1, 2, 3 as the merged dict
    # accumulated.
    assert alive_at_query == [0, 0, 0, 0]


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
