"""Tests for the core logic _real_last_changed.

Run: `pytest` with `homeassistant` installed (e.g. via
`pytest-homeassistant-custom-component`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from custom_components.last_changed_keeper import _real_last_changed

BASE = datetime(2026, 6, 23, 4, 0, tzinfo=UTC)


@dataclass
class FakeState:
    state: str | None
    last_changed: datetime
    last_updated: datetime


def s(value: str | None, minutes: int) -> FakeState:
    ts = BASE + timedelta(minutes=minutes)
    return FakeState(value, ts, ts)


def test_simple_real_change_bounded():
    # on -> off (real change), nothing after
    history = [s("on", 40), s("off", 51)]
    ts, bounded, _ = _real_last_changed(history, "off")
    assert bounded is True
    assert ts == s("off", 51).last_changed


def test_skips_restart_recovery():
    # real off at 51, then restarts: unavailable + recovery-off
    history = [
        s("on", 40),
        s("off", 51),          # real last change
        s("unavailable", 114),
        s("off", 116),         # recovery
        s("unavailable", 149),
        s("off", 150),         # recovery (current value)
    ]
    ts, bounded, _ = _real_last_changed(history, "off")
    assert bounded is True
    assert ts == s("off", 51).last_changed  # not 116 or 150!


def test_unbounded_when_history_exhausted():
    # only recovery-offs in the window, the real change is before it
    history = [
        s("unavailable", 100),
        s("off", 102),
        s("unavailable", 130),
        s("off", 132),
    ]
    ts, bounded, _ = _real_last_changed(history, "off")
    assert bounded is False          # no other valid value -> uncertain
    assert ts == s("off", 102).last_changed  # oldest in run (best effort)


def test_state_changed_back_on():
    # off -> on -> off : the current run starts at the last off
    history = [s("off", 10), s("on", 20), s("off", 51)]
    ts, bounded, _ = _real_last_changed(history, "off")
    assert bounded is True
    assert ts == s("off", 51).last_changed


def test_no_valid_states():
    history = [s("unavailable", 10), s("unknown", 20)]
    ts, bounded, _ = _real_last_changed(history, "off")
    assert ts is None
    assert bounded is False


def test_empty_history():
    ts, bounded, _ = _real_last_changed([], "off")
    assert ts is None
    assert bounded is False


def test_unavailable_in_middle_is_skipped():
    # off(real) -> unavailable -> off(recovery): unavailable is skipped,
    # the real off time stays authoritative.
    history = [s("on", 30), s("off", 51), s("unavailable", 120), s("off", 122)]
    ts, bounded, _ = _real_last_changed(history, "off")
    assert bounded is True
    assert ts == s("off", 51).last_changed


def test_many_restart_recoveries_collapse_to_real():
    history = [
        s("on", 40),
        s("off", 51),            # real last change
        s("unavailable", 100), s("off", 101),
        s("unavailable", 140), s("off", 141),
        s("unavailable", 175), s("off", 176),   # current value
    ]
    ts, bounded, _ = _real_last_changed(history, "off")
    assert bounded is True
    assert ts == s("off", 51).last_changed


def test_current_state_on():
    history = [s("off", 10), s("on", 51)]
    ts, bounded, _ = _real_last_changed(history, "on")
    assert bounded is True
    assert ts == s("on", 51).last_changed


def test_removal_row_bounds_the_run_but_is_flagged():
    """A None-state row (entity removal - e.g. Entity.async_remove() on a
    config-entry reload/device rejoin) still bounds the run like any other
    differing value, but is flagged as bounded_by_removal so _resolve can
    treat a too-recent instance of it as inconclusive rather than a hard
    block (see _resolve's docstring)."""
    history = [s("on", 40), s(None, 51), s("on", 52)]
    ts, bounded, bounded_by_removal = _real_last_changed(history, "on")
    assert bounded is True
    assert bounded_by_removal is True
    assert ts == s("on", 52).last_changed


def test_genuine_value_bound_is_not_flagged_as_removal():
    history = [s("on", 40), s("off", 51), s("on", 52)]
    ts, bounded, bounded_by_removal = _real_last_changed(history, "on")
    assert bounded is True
    assert bounded_by_removal is False
    assert ts == s("on", 52).last_changed
