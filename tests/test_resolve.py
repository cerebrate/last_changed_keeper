"""Tests for _RestoreJob._resolve: snapshot/bulk/deep priority, the
bounded-but-not-ok short-circuit, and the exhausted-deep-window guard.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.last_changed_keeper as lck
from custom_components.last_changed_keeper import _RestoreJob
from custom_components.last_changed_keeper.const import (
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)


@dataclass
class FakeRow:
    state: str
    last_changed: object
    last_updated: object


def _make_job(hass: HomeAssistant, snapshot: dict | None = None) -> _RestoreJob:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    return _RestoreJob(hass, entry, store, snapshot or {})


async def test_snapshot_ignored_when_state_value_differs(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Regression: a snapshot taken while the entity held a different value
    must not be applied — the stored timestamp belongs to that other value."""
    hass.states.async_set("light.kitchen", "off")
    await hass.async_block_till_done()
    live = hass.states.get("light.kitchen")

    stale = dt_util.utcnow() - timedelta(days=3)
    job = _make_job(
        hass, snapshot={"light.kitchen": {"s": "on", "t": stale.isoformat()}}
    )

    result = await job._resolve("light.kitchen", live, None)
    assert result is None


async def test_snapshot_used_when_state_value_matches(
    recorder_mock, hass: HomeAssistant
) -> None:
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    live = hass.states.get("light.kitchen")

    stale = dt_util.utcnow() - timedelta(days=3)
    job = _make_job(
        hass, snapshot={"light.kitchen": {"s": "on", "t": stale.isoformat()}}
    )

    result = await job._resolve("light.kitchen", live, None)
    assert result == stale


async def test_old_plain_string_snapshot_format_is_discarded(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Pre-0.5.9 snapshots stored a bare isoformat string per entity, with no
    state value. That format must be ignored, not crash or misapply."""
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    live = hass.states.get("light.kitchen")

    stale = dt_util.utcnow() - timedelta(days=3)
    job = _make_job(hass, snapshot={"light.kitchen": stale.isoformat()})

    result = await job._resolve("light.kitchen", live, None)
    assert result is None


async def test_bounded_bulk_result_short_circuits_without_ok(
    recorder_mock, hass: HomeAssistant
) -> None:
    """A bounded bulk result that fails the margin check means the value
    genuinely just changed — no snapshot/deep fallback may override it."""
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    live = hass.states.get("light.kitchen")

    now = live.last_changed
    bulk_states = [
        FakeRow("off", now - timedelta(minutes=5), now - timedelta(minutes=5)),
        FakeRow("on", now, now),
    ]

    stale = now - timedelta(days=3)
    job = _make_job(
        hass, snapshot={"light.kitchen": {"s": "on", "t": stale.isoformat()}}
    )

    result = await job._resolve("light.kitchen", live, bulk_states)
    assert result is None  # not the stale snapshot value either


async def test_deep_query_best_effort_discarded_when_window_exhausted_by_count(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """HISTORY_DEPTH rows all showing the same value (attribute noise, e.g.
    climate current_temperature updates) must not be treated as a reliable
    'oldest of window' answer — the true change could be far older."""
    hass.states.async_set("climate.living_room", "heat")
    await hass.async_block_till_done()
    live = hass.states.get("climate.living_room")

    now = live.last_changed
    rows = [
        FakeRow("heat", now - timedelta(minutes=i), now - timedelta(minutes=i))
        for i in range(1, lck.HISTORY_DEPTH + 1)
    ]

    def fake_get_last_state_changes(_hass, _number_of_states, entity_id):
        return {entity_id: rows}

    monkeypatch.setattr(lck, "get_last_state_changes", fake_get_last_state_changes)

    job = _make_job(hass)
    result = await job._resolve("climate.living_room", live, None)
    assert result is None


async def test_deep_query_best_effort_used_when_window_not_exhausted(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """Fewer rows than HISTORY_DEPTH with no older differing value found is a
    genuinely unbounded-but-short history — still usable as best effort."""
    hass.states.async_set("light.kitchen", "on")
    await hass.async_block_till_done()
    live = hass.states.get("light.kitchen")

    now = live.last_changed
    oldest = now - timedelta(hours=2)
    rows = [FakeRow("on", oldest, oldest), FakeRow("on", now, now)]

    def fake_get_last_state_changes(_hass, _number_of_states, entity_id):
        return {entity_id: rows}

    monkeypatch.setattr(lck, "get_last_state_changes", fake_get_last_state_changes)

    job = _make_job(hass)
    result = await job._resolve("light.kitchen", live, None)
    assert result == oldest


async def test_unbounded_deep_result_discarded_near_purge_boundary(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """A same-value run that runs out of rows right at (or past) the
    recorder's purge boundary is indistinguishable from one whose earlier,
    same-value restarts simply aged out of the database - it must not be
    trusted as a genuine origin (see _near_purge_boundary)."""
    hass.states.async_set("automation.stable", "on")
    await hass.async_block_till_done()
    live = hass.states.get("automation.stable")

    now = live.last_changed
    oldest = now - timedelta(days=20)
    rows = [FakeRow("on", oldest, oldest), FakeRow("on", now, now)]

    def fake_get_last_state_changes(_hass, _number_of_states, entity_id):
        return {entity_id: rows}

    monkeypatch.setattr(lck, "get_last_state_changes", fake_get_last_state_changes)
    monkeypatch.setattr(lck.get_instance(hass), "keep_days", 10)

    job = _make_job(hass)
    result = await job._resolve("automation.stable", live, None)
    assert result is None


async def test_unbounded_deep_result_used_when_well_inside_purge_boundary(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """The purge-boundary guard only rejects results at/near the boundary -
    a plausible surviving origin comfortably inside the retention window is
    still trusted as best effort, same as before the guard existed."""
    hass.states.async_set("automation.stable", "on")
    await hass.async_block_till_done()
    live = hass.states.get("automation.stable")

    now = live.last_changed
    oldest = now - timedelta(days=3)
    rows = [FakeRow("on", oldest, oldest), FakeRow("on", now, now)]

    def fake_get_last_state_changes(_hass, _number_of_states, entity_id):
        return {entity_id: rows}

    monkeypatch.setattr(lck, "get_last_state_changes", fake_get_last_state_changes)
    monkeypatch.setattr(lck.get_instance(hass), "keep_days", 10)

    job = _make_job(hass)
    result = await job._resolve("automation.stable", live, None)
    assert result == oldest


async def test_purge_boundary_guard_skipped_when_auto_purge_disabled(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """With auto_purge off there is no enforced retention boundary to
    distrust results near, so the pre-existing best-effort behavior applies
    even to a result that would otherwise look boundary-adjacent."""
    hass.states.async_set("automation.stable", "on")
    await hass.async_block_till_done()
    live = hass.states.get("automation.stable")

    now = live.last_changed
    oldest = now - timedelta(days=20)
    rows = [FakeRow("on", oldest, oldest), FakeRow("on", now, now)]

    def fake_get_last_state_changes(_hass, _number_of_states, entity_id):
        return {entity_id: rows}

    monkeypatch.setattr(lck, "get_last_state_changes", fake_get_last_state_changes)
    recorder_instance = lck.get_instance(hass)
    monkeypatch.setattr(recorder_instance, "keep_days", 10)
    monkeypatch.setattr(recorder_instance, "auto_purge", False)

    job = _make_job(hass)
    result = await job._resolve("automation.stable", live, None)
    assert result == oldest


async def test_counters_track_deep_queries_only_when_step_3_runs(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """The optional counters dict (see _async_run_impl/async_verify, which
    report it in stats) must count an entity only when _resolve actually
    falls through to the deep per-entity query - not on a bulk/snapshot
    short-circuit, which is the case the counter exists to distinguish
    from ('how many entities needed the expensive fallback')."""
    monkeypatch.setattr(
        lck, "get_last_state_changes", lambda *a, **k: {a[-1]: []}
    )

    hass.states.async_set("light.bulk_hit", "on")
    hass.states.async_set("light.snapshot_hit", "on")
    hass.states.async_set("light.falls_through", "on")
    await hass.async_block_till_done()

    stale = dt_util.utcnow() - timedelta(days=3)
    job = _make_job(
        hass, snapshot={"light.snapshot_hit": {"s": "on", "t": stale.isoformat()}}
    )

    bulk_live = hass.states.get("light.bulk_hit")
    now = bulk_live.last_changed
    bulk_states = [
        FakeRow("off", now - timedelta(hours=1), now - timedelta(hours=1)),
        FakeRow("on", now - timedelta(minutes=10), now - timedelta(minutes=10)),
    ]

    counters: dict[str, int] = {}
    await job._resolve("light.bulk_hit", bulk_live, bulk_states, counters)
    assert counters.get("deep_queries", 0) == 0  # bounded bulk result short-circuits

    await job._resolve(
        "light.snapshot_hit", hass.states.get("light.snapshot_hit"), None, counters
    )
    assert counters.get("deep_queries", 0) == 0  # snapshot match short-circuits

    await job._resolve(
        "light.falls_through", hass.states.get("light.falls_through"), None, counters
    )
    assert counters.get("deep_queries", 0) == 1  # neither available -> step 3 ran
