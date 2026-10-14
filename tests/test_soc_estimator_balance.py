"""A balance charge is confirmed by the estimator's full evidence (candidate or anchor)."""

import datetime as dt
from dataclasses import replace

import pytest

from custom_components.energy_compass.balance_tracker import (
    balance_settings,
    confirmed_percent,
    empty_state,
    observe,
    status,
)
from custom_components.energy_compass.soc_estimator import (
    Anchor,
    ControllerFacts,
    EstimatorSettings,
    EstimatorState,
    Inputs,
    step,
)

T0 = dt.datetime(2026, 10, 14, 11, tzinfo=dt.UTC)
SETTINGS = EstimatorSettings(
    capacity_kwh=24.0,
    empty_voltage_v=496.0,
    full_min_voltage_v=530.0,
    eta_charge=0.9,
    max_charge_current_a=14.0,
    export_limit_w=0.0,
    full_hold_seconds=600.0,
)
HOUR = replace(SETTINGS, full_hold_seconds=3600.0)
BALANCE = ControllerFacts(
    charge_limit_a=14.0,
    grid_charge_a=14.0,
    state="CHARGE_GRID",
    program_direction="Grid",
    program_voltage_v=552.0,
    balance_hold=True,
)


def _state(started=T0):
    return EstimatorState(Anchor("initial", T0, 22.0, 1000.0, 500.0), started)


def _inputs(minute, *, facts=BALANCE, voltage=552.0, current=-0.3, grid=300.0):
    return Inputs(
        T0 + dt.timedelta(minutes=minute),
        1000.0,
        500.0,
        current * voltage,
        voltage,
        current,
        0.0,
        500.0,
        grid,
        facts,
    )


def _run(state, settings, minutes, **kwargs):
    estimate = None
    for minute in range(minutes + 1):
        estimate = step(state, settings, _inputs(minute, **kwargs))
    return estimate


def test_a_balance_row_that_reaches_its_voltage_and_tapers_is_full_evidence():
    state = _state()
    settings = replace(SETTINGS, anchor_on_candidates=True)
    estimate = _run(state, settings, 10)
    assert estimate.events == ("full_balance",)
    assert estimate.anchor_kind == "full_balance"
    assert estimate.top_confirmed
    assert state.candidates[0]["path"] == "balance"


def test_the_same_stop_outside_a_balance_row_is_only_the_target():
    state = _state()
    settings = replace(SETTINGS, anchor_on_candidates=True)
    _run(state, settings, 10, facts=replace(BALANCE, balance_hold=False))
    assert state.candidates == []


@pytest.mark.parametrize("name", ["HOLD", "CURTAIL", "BASE", "DISCHARGE_GRID"])
def test_a_balance_flag_does_not_override_a_non_charging_state(name):
    state = _state()
    _run(state, SETTINGS, 10, facts=replace(BALANCE, state=name))
    assert state.candidates == []


def test_a_balance_row_short_of_its_voltage_keeps_the_ordinary_rules():
    state = _state()
    _run(state, SETTINGS, 10, voltage=540.0)
    [candidate] = state.candidates
    assert candidate["path"] == "grid"


def test_a_candidate_in_the_candidates_only_window_is_not_applied_but_qualifies():
    state = _state()
    early = _run(state, SETTINGS, 9)
    assert not early.full_qualified
    estimate = _run(state, SETTINGS, 10)
    assert estimate.anchor_kind == "initial"
    assert estimate.full_qualified
    assert not estimate.top_confirmed
    assert state.candidates[0]["outcome"] == "candidate"
    # Evidence that ends is no longer qualified.
    assert not step(state, SETTINGS, _inputs(11, current=-8.0)).full_qualified


def test_a_candidate_on_low_counters_qualifies_too():
    state = EstimatorState(Anchor("initial", T0, 10.0, 1000.0, 500.0), T0)
    estimate = _run(state, SETTINGS, 12)
    assert state.candidates[0]["outcome"] == "candidate"
    assert estimate.full_qualified


def test_a_qualified_full_confirms_the_balance_reading():
    assert confirmed_percent(90.0, False) == float("-inf")
    assert confirmed_percent(90.0, False, qualified=True) == 100.0
    assert confirmed_percent(99.9, True) == 99.9


def test_one_balance_window_in_the_candidates_only_window_completes_the_balance():
    settings = balance_settings({"balance_hold_minutes": 60})
    state, tracker = _state(), empty_state()
    for minute in range(100):
        estimate = step(state, SETTINGS, _inputs(minute))
        tracker = observe(
            tracker,
            confirmed_percent(
                estimate.soc_fraction * 100,
                estimate.top_confirmed,
                estimate.full_qualified,
            ),
            T0 + dt.timedelta(minutes=minute),
            settings,
        )
    assert estimate.anchor_kind == "initial"
    assert tracker["last_completed_at"] is not None
    tomorrow = T0 + dt.timedelta(days=1)
    report = status(tracker, tomorrow, settings)
    assert report["phase"] == "ok"
    assert report["due_phase"] == "ok"


def _balance_run(minutes, row_minutes, *, hold_minutes=60):
    """Tracker state after `minutes`, the balance row lasting `row_minutes`."""
    settings = balance_settings({"balance_hold_minutes": hold_minutes})
    state, tracker = _state(), empty_state()
    for minute in range(minutes):
        facts = replace(BALANCE, balance_hold=minute < row_minutes)
        estimate = step(state, HOUR, _inputs(minute, facts=facts, grid=-1500.0))
        tracker = observe(
            tracker,
            confirmed_percent(
                estimate.soc_fraction * 100,
                estimate.top_confirmed,
                estimate.full_qualified,
            ),
            T0 + dt.timedelta(minutes=minute),
            settings,
        )
    return tracker, settings


def test_the_tracker_starts_its_hold_only_after_the_one_hour_full_hold():
    tracker, settings = _balance_run(90, 125)
    assert tracker["hold_started_at"] == (T0 + dt.timedelta(minutes=60)).isoformat()
    report = status(tracker, T0 + dt.timedelta(minutes=89), settings)
    assert report["phase"] == "holding"
    assert report["hold_remaining_minutes"] == pytest.approx(31.0)
    assert tracker["last_completed_at"] is None


def test_a_balance_window_of_hold_plus_full_hold_completes():
    tracker, settings = _balance_run(125, 125)
    assert tracker["last_completed_at"] == (T0 + dt.timedelta(minutes=120)).isoformat()
    report = status(tracker, T0 + dt.timedelta(days=1), settings)
    assert report["phase"] == "ok"


def test_a_window_of_the_balance_hold_alone_cannot_complete():
    # The row ends before the full hold has qualified: the planner must ask
    # for the hold plus the full hold, which `balance_lead_minutes` adds.
    tracker, _ = _balance_run(180, 60)
    assert tracker["last_completed_at"] is None
    assert tracker["hold_started_at"] is None
