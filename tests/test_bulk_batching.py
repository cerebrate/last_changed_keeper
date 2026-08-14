"""Tests for the batched, streamed bulk recorder query and the post-boot
self-check."""
from __future__ import annotations

import weakref
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.last_changed_keeper as lck
from custom_components.last_changed_keeper import _RestoreJob
from custom_components.last_changed_keeper.const import (
    CONF_ALL_ENTITIES,
    CONF_BULK_BATCH_SIZE,
    CONF_DOMAINS,
    CONF_ENTITIES,
    CONF_VERIFY_AFTER_BOOT,
    DEFAULT_GRACE,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)


@dataclass
class FakeRow:
    state: str
    last_changed: object
    last_updated: object


def _make_job(hass: HomeAssistant, options: dict | None = None) -> _RestoreJob:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options or {})
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
    that into a single dict — and holding it live for the whole resolve
    pass — was the acute cause of multi-gigabyte spikes, most visibly on
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


async def test_bulk_batches_use_configured_batch_size(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def fake_bulk_query(_hass, _start, entity_ids):
        calls.append(list(entity_ids))
        return {eid: [] for eid in entity_ids}

    monkeypatch.setattr(lck, "_bulk_query", fake_bulk_query)
    # Module default is untouched; the per-entry option must win over it.
    job = _make_job(hass, options={CONF_BULK_BATCH_SIZE: 3})
    ids = [f"light.l{i}" for i in range(7)]
    delivered: dict[str, list] = {}
    async for _chunk, result in job._iter_bulk_batches(ids):
        delivered.update(result)

    assert [len(c) for c in calls] == [3, 3, 1]
    assert set(delivered) == set(ids)


async def test_bulk_batch_size_falls_back_to_default_when_invalid(
    recorder_mock, hass: HomeAssistant
) -> None:
    job = _make_job(hass, options={CONF_BULK_BATCH_SIZE: 0})
    assert job._bulk_batch_size == lck.BULK_BATCH_SIZE

    job = _make_job(hass, options={CONF_BULK_BATCH_SIZE: "not-a-number"})
    assert job._bulk_batch_size == lck.BULK_BATCH_SIZE


async def test_boot_pass_reports_bulk_and_deep_query_instrumentation(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, monkeypatch
) -> None:
    """stats after a boot pass expose bulk_rows_fetched/bulk_batches/
    deep_queries so the recorder cost of a pass is visible without having
    to infer it from wall-clock duration alone - see also the equivalent
    verify coverage in test_verify_service.py."""
    hass.states.async_set("light.bulk_hit", "on")
    hass.states.async_set("light.falls_through", "on")
    await hass.async_block_till_done()

    now = hass.states.get("light.bulk_hit").last_changed
    two_rows = [
        FakeRow("off", now - timedelta(hours=1), now - timedelta(hours=1)),
        FakeRow("on", now - timedelta(minutes=10), now - timedelta(minutes=10)),
    ]

    def fake_bulk_query(_hass, _start, entity_ids):
        return {"light.bulk_hit": two_rows} if "light.bulk_hit" in entity_ids else {}

    monkeypatch.setattr(lck, "_bulk_query", fake_bulk_query)
    monkeypatch.setattr(lck, "BULK_BATCH_SIZE", 1)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ALL_ENTITIES: False, CONF_DOMAINS: ["light"], CONF_ENTITIES: []},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    stats = entry.runtime_data.stats
    # light.bulk_hit resolves from its bounded bulk result (2 rows, no deep
    # query needed); light.falls_through has none, so it falls through.
    assert stats["bulk_rows_fetched"] == 2
    assert stats["bulk_batches"] == 2  # one per entity at batch size 1
    assert stats["deep_queries"] == 1


async def test_unchanged_candidates_are_patched_even_when_the_pass_runs_past_grace(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, hass_storage,
    monkeypatch,
) -> None:
    """Regression: streaming turned pass 0 into several awaited batches, so
    a large installation's pass can itself take a large fraction of (or
    longer than) `grace`. The per-batch re-validation used to compare
    elapsed time since boot against `grace` a second time - but an entity
    that never changed keeps the same last_changed the whole time, so that
    check was really just measuring how long our own pass had been running,
    not whether the entity changed. Once elapsed time crossed `grace`, every
    remaining candidate got silently skipped: no patch, and not added to
    `_pending` either, so nothing ever retried them - gone until the next
    real restart.

    Two entities land in separate batches (BULK_BATCH_SIZE=1); the fake
    clock jumps past `grace` before the second batch's query returns, as if
    the pass had genuinely taken that long to reach it. Neither entity's
    state actually changes. Both must still be patched from the snapshot
    regardless of which one landed in the second batch - the fix compares
    against the last_changed captured when the candidate list was built,
    not against elapsed real time.
    """
    stale_a = dt_util.utcnow() - timedelta(days=2)
    stale_b = dt_util.utcnow() - timedelta(days=3)
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "light.a": {"s": "on", "t": stale_a.isoformat()},
            "light.b": {"s": "on", "t": stale_b.isoformat()},
        },
    }

    base = dt_util.utcnow()
    hass.states.async_set("light.a", "on")
    hass.states.async_set("light.b", "on")
    await hass.async_block_till_done()

    clock = {"now": base}
    monkeypatch.setattr(lck.dt_util, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(lck, "BULK_BATCH_SIZE", 1)

    calls = {"n": 0}

    def fake_bulk_query(_hass, _start, entity_ids):
        calls["n"] += 1
        if calls["n"] >= 2:
            # By the time the second batch's query returns, simulate the
            # pass having run long enough that elapsed time since boot now
            # exceeds the default grace window - even though neither
            # entity's own state ever changed.
            clock["now"] = base + timedelta(seconds=DEFAULT_GRACE + 60)
        return {eid: [] for eid in entity_ids}

    monkeypatch.setattr(lck, "_bulk_query", fake_bulk_query)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ALL_ENTITIES: False, CONF_DOMAINS: ["light"], CONF_ENTITIES: []},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("light.a").last_changed == stale_a
    assert hass.states.get("light.b").last_changed == stale_b


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
