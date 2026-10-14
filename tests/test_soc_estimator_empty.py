"""Empty-anchor rule (DD-4)."""

import datetime as dt
from dataclasses import replace

import pytest
from soc_estimator_support import Recording, at

from custom_components.energy_compass.soc_estimator import (
    Anchor,
    ControllerFacts,
    EstimatorSettings,
    EstimatorState,
    Inputs,
    step,
)

T0 = dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC)
SETTINGS = EstimatorSettings(capacity_kwh=24.0, empty_voltage_v=496.0, eta_charge=0.9)
ALLOWED = ControllerFacts(charge_limit_a=14.0, discharge_limit_a=14.0, state="BASE")


def _state():
    return EstimatorState(Anchor("initial", T0, 3.0, 1000.0, 500.0), T0)


def _inputs(
    seconds,
    *,
    voltage=495.0,
    current=3.0,
    power=None,
    grid=0.0,
    facts=None,
    charge=1000.0,
    discharge=500.0,
):
    return Inputs(
        T0 + dt.timedelta(seconds=seconds),
        charge,
        discharge,
        current * voltage if power is None else power,
        voltage,
        current,
        0.0,
        1500.0,
        grid,
        facts,
    )


def _hold(state, settings=SETTINGS, *, seconds=60, **kwargs):
    estimate = None
    for second in range(0, seconds + 1, 30):
        estimate = step(state, settings, _inputs(second, **kwargs))
    return estimate


def test_light_load_at_the_zero_voltage_anchors_empty_after_the_hold():
    state = _state()
    early = _hold(state, seconds=30)
    assert early.empty_candidate
    assert early.events == ()
    estimate = step(state, SETTINGS, _inputs(60))
    assert estimate.events == ("empty",)
    assert estimate.anchor_kind == "empty"
    assert estimate.energy_kwh == 0.0
    assert state.anchor.charge_kwh == 1000.0
    again = step(state, SETTINGS, _inputs(90))
    assert again.events == ()


def test_voltage_sag_under_heavy_load_is_not_empty():
    state = _state()
    _hold(state, voltage=488.0, current=14.0, power=6800.0)
    assert state.anchor.kind == "initial"
    state = _state()
    _hold(state, voltage=488.0, current=14.0, power=2300.0)
    assert state.anchor.kind == "empty"
    state = _state()
    _hold(state, voltage=488.0, current=14.0, power=2500.0)
    assert state.anchor.kind == "initial"


def test_a_voltage_above_the_zero_point_is_not_empty():
    state = _state()
    _hold(state, voltage=496.5)
    assert state.anchor.kind == "initial"


def test_an_idle_pack_at_a_low_voltage_is_not_proof():
    state = _state()
    _hold(state, current=0.1, power=50.0)
    assert state.anchor.kind == "initial"


def test_a_break_restarts_the_hold():
    state = _state()
    step(state, SETTINGS, _inputs(0))
    step(state, SETTINGS, _inputs(30, voltage=520.0))
    assert not step(state, SETTINGS, _inputs(60)).events
    assert step(state, SETTINGS, _inputs(120)).events == ("empty",)


def test_the_energy_since_the_last_full_anchor_is_measured_not_applied():
    state = _state()
    full = Anchor("full", T0, 24.0, 1000.0, 500.0)
    state.anchor = full
    state.last_full = full
    estimate = _hold(state, charge=1010.0, discharge=525.0)
    assert estimate.measured_capacity_kwh == pytest.approx(25 - 0.9 * 10)
    assert state.last_full is None
    assert SETTINGS.capacity_kwh == 24.0


def test_without_a_full_anchor_nothing_is_measured():
    state = _state()
    assert _hold(state).measured_capacity_kwh is None


def test_a_counter_reset_forgets_the_full_anchor():
    state = _state()
    state.last_full = Anchor("full", T0, 24.0, 1000.0, 500.0)
    step(state, SETTINGS, _inputs(0))
    step(state, SETTINGS, _inputs(60, charge=2.0, voltage=520.0))
    assert state.last_full is None


STOPPED = {
    "voltage": 499.0,
    "current": 0.1,
    "power": 40.0,
    "grid": 1500.0,
    "facts": ALLOWED,
}


def test_a_stopped_discharge_with_grid_import_anchors_empty():
    state = _state()
    estimate = _hold(state, **STOPPED)
    assert estimate.events == ("empty",)
    assert state.anchor.kind == "empty"


@pytest.mark.parametrize(
    "change",
    [
        {"facts": replace(ALLOWED, discharge_limit_a=0.0)},
        {"facts": replace(ALLOWED, discharge_limit_a=None)},
        {"facts": replace(ALLOWED, state="HOLD")},
        {"facts": replace(ALLOWED, target_reached=True)},
        {"facts": None},
        {"grid": 100.0},
        {"voltage": 502.0},
        {"current": -2.0},
        {"current": 1.0},
    ],
    ids=[
        "limit-zero",
        "limit-unknown",
        "hold",
        "target",
        "no-facts",
        "no-import",
        "above-margin",
        "charging",
        "discharging-light-load-needs-low-voltage",
    ],
)
def test_a_stopped_discharge_needs_every_condition(change):
    state = _state()
    _hold(state, **{**STOPPED, **change})
    assert state.anchor.kind == "initial"


def test_charging_after_an_empty_anchor_counts_from_zero():
    state = _state()
    _hold(state)
    estimate = step(state, SETTINGS, _inputs(120, voltage=520.0, charge=1002.0))
    assert estimate.energy_kwh == pytest.approx(1.8)


@pytest.fixture(scope="module")
def recording():
    return Recording()


def test_the_recorded_week_never_reaches_the_zero_voltage(recording):
    first = recording.yaml["anchors"][0]
    state = EstimatorState(
        Anchor(
            "full",
            at(first["at"]),
            first["energy_kwh"],
            first["charge_kwh"],
            first["discharge_kwh"],
        ),
        at(recording.start),
    )
    events = []
    for epoch in recording.event_times():
        events += step(state, SETTINGS, recording.inputs(epoch)).events
    assert "empty" not in events
    assert state.measured_capacity_kwh is None
