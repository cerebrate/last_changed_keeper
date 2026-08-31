"""Tests for _wait_for_recorder_ready: the boot pass must not run its
recorder-heavy bulk/deep queries until the recorder is genuinely ready
(Recorder.async_recorder_ready), not merely "set up" (Recorder.async_db_ready
- the only thing manifest.json's recorder dependency actually guarantees).
A live schema migration is deliberately deferred by the recorder itself
until after HA reports "started", i.e. exactly when this integration's own
boot trigger fires, so without this wait the boot pass would compete with
an in-progress migration for the same executor/DB.
"""
from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.last_changed_keeper as lck
from custom_components.last_changed_keeper import _RestoreJob
from custom_components.last_changed_keeper.const import (
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)


def _make_job(hass: HomeAssistant, snapshot: dict | None = None) -> _RestoreJob:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    return _RestoreJob(hass, entry, store, snapshot or {})


async def test_boot_pass_waits_for_recorder_ready(
    recorder_mock, hass: HomeAssistant
) -> None:
    """async_run() must not start the boot pass until the recorder signals
    it's genuinely ready, not just connected/set up."""
    hass.states.async_set("light.kitchen", "on")
    job = _make_job(hass)
    instance = lck.get_instance(hass)
    instance.async_recorder_ready.clear()

    task = hass.async_create_task(job.async_run())

    # Still blocked on the readiness wait - the boot pass hasn't touched
    # job.stats yet.
    _, pending = await asyncio.wait({task}, timeout=0.05)
    assert task in pending
    assert job.stats == {}

    instance.async_recorder_ready.set()
    await task

    assert job.stats


async def test_boot_pass_proceeds_after_recorder_ready_timeout(
    recorder_mock, hass: HomeAssistant, monkeypatch
) -> None:
    """If the recorder never signals readiness (e.g. a failed live
    migration, which never sets async_recorder_ready at all), the boot
    pass must still eventually run rather than hang forever."""
    monkeypatch.setattr(lck, "RECORDER_READY_TIMEOUT_SECONDS", 0.05)
    hass.states.async_set("light.kitchen", "on")
    job = _make_job(hass)
    instance = lck.get_instance(hass)
    instance.async_recorder_ready.clear()

    patched = await job.async_run()

    # No usable history exists for a freshly-created entity, so nothing
    # resolves - the point is that the boot pass ran to completion at all,
    # rather than hanging on a readiness signal that never arrives.
    assert patched == 0
    assert job.stats
