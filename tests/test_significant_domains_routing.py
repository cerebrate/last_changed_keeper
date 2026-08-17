"""Prototype validation: routing SIGNIFICANT_DOMAINS entities around the
bulk query entirely (see _iter_bulk_batches) instead of adopting upstream's
raw-SQL per-entity row cap.

Mirrors the validation methodology of upstream's own
tests/test_bulk_per_entity_cap.py (real recorder, freeze_time-written
history) and this fork's tests/test_resolve_equivalence.py, but targets the
specific bug upstream's 0.9.4 found: get_significant_states returns
attribute-only rows unfiltered for climate/device_tracker/humidifier/
thermostat/water_heater, which can drown a genuine value change in noise.
"""
from __future__ import annotations

from datetime import timedelta

from freezegun import freeze_time
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.last_changed_keeper import _RestoreJob, get_instance
from custom_components.last_changed_keeper.const import (
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)


def _make_job(hass: HomeAssistant) -> _RestoreJob:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    return _RestoreJob(hass, entry, store, {})


def _simulate_boot_reset(hass: HomeAssistant, entity_id: str, value: str) -> State:
    """Same technique as test_resolve_equivalence.py: pop the state machine's
    in-memory entry (no genuine event, matching what a real process restart
    looks like) so the next async_set is treated as brand new - last_changed
    = now, exactly like a real restart - rather than preserving the
    attribute-write-only last_changed already sitting from history setup."""
    hass.states._states_data.pop(entity_id, None)
    hass.states.async_set(entity_id, value)
    return hass.states.get(entity_id)


async def _write_device_tracker_history(
    hass: HomeAssistant, went_home_at, chatter_rows: int
) -> None:
    """A device_tracker with a genuine "arrived home" transition several
    days ago, then chatter_rows of attribute-only chatter (GPS jitter, same
    state) right up to "now" - the exact shape upstream found pathological."""
    with freeze_time(went_home_at - timedelta(hours=1)):
        hass.states.async_set("device_tracker.phone", "not_home")
    with freeze_time(went_home_at):
        hass.states.async_set("device_tracker.phone", "home", {"lat": 0})
    now = dt_util.utcnow()
    for i in range(chatter_rows):
        with freeze_time(now - timedelta(minutes=chatter_rows - i)):
            hass.states.async_set(
                "device_tracker.phone", "home", {"lat": i, "lon": i}
            )
    await async_wait_recording_done(hass)


async def test_raw_get_significant_states_leaks_attribute_noise(
    recorder_mock, hass: HomeAssistant
) -> None:
    """Control: reproduces upstream's own finding directly against this
    fork's recorder test setup - the unmodified public API returns every
    attribute-only chatter row for device_tracker, not just the genuine
    "home"/"not_home" transition. This is the bug being routed around,
    not fixed at the query level (contrast with upstream's SQL filter)."""
    went_home_at = dt_util.utcnow() - timedelta(days=3)
    await _write_device_tracker_history(hass, went_home_at, chatter_rows=300)

    start = dt_util.utcnow() - timedelta(days=30)

    def _query():
        return get_significant_states(
            hass,
            start,
            None,
            ["device_tracker.phone"],
            include_start_time_state=False,
            significant_changes_only=True,
            no_attributes=True,
        )

    result = await get_instance(hass).async_add_executor_job(_query)
    rows = result.get("device_tracker.phone", [])
    # 300 chatter rows + the genuine not_home->home transition rows.
    assert len(rows) >= 300


async def test_device_tracker_resolves_correctly_when_chatter_stays_under_history_depth(
    recorder_mock, hass: HomeAssistant
) -> None:
    """The prototype's success case: with SIGNIFICANT_DOMAINS excluded from
    the bulk query, this entity's bulk_states is always None, _resolve
    falls through to the existing HISTORY_DEPTH-capped deep query, and the
    genuine "home" transition is found correctly - as long as the chatter
    since that transition stays under HISTORY_DEPTH (100) rows, the chatter
    never gets a chance to pollute the answer, without ever changing the
    bulk SQL. See the _but_fails_when_chatter_exceeds_it case below for
    where this stops holding."""
    went_home_at = dt_util.utcnow() - timedelta(days=3)
    await _write_device_tracker_history(hass, went_home_at, chatter_rows=40)

    live = _simulate_boot_reset(hass, "device_tracker.phone", "home")
    await hass.async_block_till_done()
    job = _make_job(hass)

    saw_entity_in_bulk = False
    result = None
    async for _chunk, bulk in job._iter_bulk_batches(["device_tracker.phone"]):
        if "device_tracker.phone" in bulk:
            saw_entity_in_bulk = True
        result = await job._resolve(
            "device_tracker.phone", live, bulk.get("device_tracker.phone")
        )

    assert saw_entity_in_bulk is False
    assert result is not None
    assert abs((result - went_home_at).total_seconds()) < 1


async def test_device_tracker_declines_when_chatter_exceeds_history_depth(
    recorder_mock, hass: HomeAssistant
) -> None:
    """The prototype's real limitation, found empirically rather than
    assumed: get_last_state_changes (unlike upstream's custom SQL) has no
    way to filter to genuine value-changes only via public API, so chatter
    eats into the SAME HISTORY_DEPTH=100 budget as genuine signal. A
    device_tracker chattering faster than ~100 attribute updates since its
    last real transition (very plausible for GPS polling every few minutes)
    exhausts the deep query's cap on pure noise before ever reaching the
    real "home" transition - _resolve correctly declines (HISTORY_DEPTH
    exhaustion guard, same rule that already protects the pre-existing
    deep-query path) rather than guessing wrong, but unlike upstream's fix,
    it does not recover the real answer either. This is the concrete gap
    between "public API, reusing existing safe paths" and upstream's SQL
    filter, which only ever counts genuine changes against its cap."""
    went_home_at = dt_util.utcnow() - timedelta(days=3)
    await _write_device_tracker_history(hass, went_home_at, chatter_rows=300)

    live = _simulate_boot_reset(hass, "device_tracker.phone", "home")
    await hass.async_block_till_done()
    job = _make_job(hass)

    result = None
    async for _chunk, bulk in job._iter_bulk_batches(["device_tracker.phone"]):
        result = await job._resolve(
            "device_tracker.phone", live, bulk.get("device_tracker.phone")
        )

    assert result is None  # declines safely; does not find the real answer
