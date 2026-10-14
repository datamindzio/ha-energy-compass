"""Full-anchor rule (DD-3): synthetic cases and recorded episodes."""

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
SETTINGS = EstimatorSettings(
    capacity_kwh=24.0,
    empty_voltage_v=480.0,
    eta_charge=0.9,
    max_charge_current_a=14.0,
    rated_pv_w=8000.0,
    anchor_on_candidates=True,
    export_limit_w=0.0,
)
EXPORTING = replace(SETTINGS, export_limit_w=5000.0)
TAPER = ControllerFacts(charge_limit_a=14.0, state="CHARGE_PV")


def _state(settings=SETTINGS):
    return EstimatorState(Anchor("initial", T0, 20.0, 1000.0, 500.0), T0)


def _inputs(
    seconds,
    *,
    voltage=546.0,
    current=-0.3,
    pv=4000.0,
    load=800.0,
    grid=None,
    facts=TAPER,
    charge=1000.0,
    discharge=500.0,
):
    return Inputs(
        T0 + dt.timedelta(seconds=seconds),
        charge,
        discharge,
        current * voltage,
        voltage,
        current,
        pv,
        load,
        grid,
        facts=facts,
    )


def _hold(state, settings=SETTINGS, *, seconds=600, **kwargs):
    """One reading a minute for `seconds`; returns the last estimate."""
    estimate = None
    for second in range(0, seconds + 1, 60):
        estimate = step(state, settings, _inputs(second, **kwargs))
    return estimate


def test_a_stop_with_pv_surplus_anchors_full_after_the_hold():
    state = _state()
    before = _hold(state, seconds=540)
    assert before.full_candidate
    assert before.anchor_kind == "initial"
    assert state.candidates == []
    estimate = step(state, SETTINGS, _inputs(600))
    assert estimate.events == ("full",)
    assert estimate.anchor_kind == "full"
    assert estimate.energy_kwh == 24.0
    assert estimate.soc_fraction == 1.0
    assert not estimate.unanchored_top
    assert state.last_full is state.anchor
    [candidate] = state.candidates
    assert candidate["path"] == "surplus"
    assert candidate["anchored"] is True
    assert candidate["surplus_w"] == pytest.approx(3200 - 0.3 * 546)
    assert candidate["started_at"] == T0.isoformat()
    assert candidate["energy_before_kwh"] == pytest.approx(20.0, abs=0.1)


def test_the_anchor_fires_once_per_candidate():
    state = _state()
    _hold(state)
    later = step(state, SETTINGS, _inputs(660))
    assert later.events == ()
    assert len(state.candidates) == 1


def test_a_break_restarts_the_hold():
    state = _state()
    _hold(state, seconds=540)
    step(state, SETTINGS, _inputs(570, current=-2.4))
    estimate = step(state, SETTINGS, _inputs(600))
    assert estimate.events == ()
    assert state.candidates == []
    for second in range(660, 1260, 60):
        estimate = step(state, SETTINGS, _inputs(second))
    assert estimate.events == ("full",)


def test_unavailable_counters_break_the_hold():
    state = _state()
    _hold(state, seconds=540)
    step(state, SETTINGS, _inputs(570, charge=None))
    assert not step(state, SETTINGS, _inputs(600)).events
    assert state.full_candidate_since == T0 + dt.timedelta(seconds=600)


def test_candidates_only_for_the_first_days_unless_switched_on():
    settings = replace(SETTINGS, anchor_on_candidates=False)
    state = _state()
    estimate = _hold(state, settings)
    assert estimate.events == ()
    assert estimate.anchor_kind == "initial"
    [candidate] = state.candidates
    assert candidate["anchored"] is False
    state = EstimatorState(
        Anchor("initial", T0, 20.0, 1000.0, 500.0), T0 - dt.timedelta(days=14)
    )
    assert _hold(state, settings).events == ("full",)


def test_no_anchor_without_the_sun_at_dusk():
    state = _state()
    _hold(state, pv=300.0, load=800.0)
    assert state.candidates == []
    assert state.anchor.kind == "initial"


def test_what_the_battery_still_takes_is_not_surplus():
    state = _state()
    _hold(state, current=-0.9, pv=1400.0, load=800.0)
    assert state.candidates == []


def test_the_export_threshold_follows_the_rated_pv_power():
    state = _state()
    _hold(state, EXPORTING, grid=-450.0)
    assert len(state.candidates) == 1
    state = _state()
    _hold(state, EXPORTING, grid=-350.0)
    assert state.candidates == []
    state = _state()
    _hold(state, replace(EXPORTING, rated_pv_w=20000.0), grid=-450.0)
    assert state.candidates == []
    state = _state()
    _hold(state, replace(EXPORTING, rated_pv_w=2000.0), grid=-350.0)
    assert len(state.candidates) == 1


def test_measured_export_is_the_primary_evidence():
    state = _state()
    _hold(state, EXPORTING, grid=-1500.0, pv=4000.0, load=800.0)
    [candidate] = state.candidates
    assert candidate["path"] == "export"
    assert candidate["export_w"] == 1500.0


def test_an_apparent_surplus_is_no_evidence_while_export_is_possible():
    # DC PV minus AC load overstates the surplus by the conversion losses.
    state = _state()
    _hold(state, EXPORTING, pv=7000.0, load=6600.0, grid=0.0)
    assert state.candidates == []
    state = _state()
    _hold(state, EXPORTING, pv=7000.0, load=6600.0, grid=150.0)
    assert state.candidates == []
    state = _state()
    _hold(state, EXPORTING, pv=7000.0, load=6600.0)
    assert state.candidates == []


def test_the_derived_surplus_counts_where_export_is_capped():
    capped = replace(SETTINGS, export_limit_w=2000.0)
    state = _state()
    _hold(state, capped, pv=5000.0, load=800.0, grid=-150.0)
    assert state.candidates == []
    state = _state()
    _hold(state, capped, pv=5000.0, load=800.0, grid=-1950.0)
    [candidate] = state.candidates
    assert candidate["path"] == "export"
    state = _state()
    _hold(state, capped, pv=2500.0, load=800.0, grid=-300.0)
    assert state.candidates == []


def test_the_derived_surplus_margin_scales_with_pv_on_a_zero_export_site():
    state = _state()
    _hold(state, pv=8000.0, load=7000.0)
    assert state.candidates == []
    state = _state()
    _hold(state, pv=8000.0, load=6600.0)
    [candidate] = state.candidates
    assert candidate["path"] == "surplus"
    assert candidate["surplus_w"] == pytest.approx(1400 - 0.3 * 546)
    state = _state()
    _hold(state, replace(SETTINGS, export_limit_w=None), pv=8000.0, load=6000.0)
    assert state.candidates == []


@pytest.mark.parametrize(
    "facts",
    [
        None,
        ControllerFacts(charge_limit_a=None),
        ControllerFacts(charge_limit_a=1.0, state="CHARGE_PV"),
        ControllerFacts(charge_limit_a=0.0, state="HOLD"),
        ControllerFacts(charge_limit_a=14.0, state="CHARGE_GRID", target_reached=True),
        ControllerFacts(charge_limit_a=14.0, state="DISCHARGE_GRID"),
    ],
    ids=["unknown", "no-limit", "one-amp", "hold", "target-reached", "discharging"],
)
def test_the_controller_explains_the_stop_or_charging_was_not_possible(facts):
    state = _state()
    _hold(state, facts=facts)
    assert state.candidates == []
    assert state.anchor.kind == "initial"


def test_the_taper_current_floor_and_the_two_percent_rule():
    state = _state()
    _hold(state, current=-1.2)
    assert state.candidates == []
    state = _state()
    wide = replace(SETTINGS, max_charge_current_a=100.0)
    _hold(state, wide, current=-1.9)
    assert len(state.candidates) == 1
    state = _state()
    _hold(state, wide, current=-2.2)
    assert state.candidates == []


def test_a_battery_that_is_discharging_is_not_full():
    state = _state()
    _hold(state, current=3.0)
    assert state.candidates == []


def test_grid_charging_that_stops_at_the_program_voltage_is_a_target_not_full():
    facts = ControllerFacts(
        charge_limit_a=14.0,
        grid_charge_a=14.0,
        state="CHARGE_GRID",
        program_direction="Grid",
        program_voltage_v=552.0,
    )
    state = _state()
    _hold(state, facts=facts, voltage=548.5, current=-0.2, pv=0.0, load=500.0)
    assert state.candidates == []
    state = _state()
    _hold(state, facts=facts, voltage=552.0, current=-0.2, pv=5000.0, load=500.0)
    assert state.candidates == []


def test_grid_charging_that_stops_short_of_the_program_voltage_is_full():
    facts = ControllerFacts(
        charge_limit_a=14.0,
        grid_charge_a=14.0,
        state="CHARGE_GRID",
        program_direction="Grid",
        program_voltage_v=552.0,
    )
    state = _state()
    estimate = _hold(
        state, facts=facts, voltage=540.0, current=-0.2, pv=0.0, load=500.0
    )
    assert estimate.events == ("full",)
    assert state.candidates[0]["path"] == "grid"


def test_a_grid_current_setpoint_without_a_grid_program_is_not_requested_charging():
    facts = ControllerFacts(
        charge_limit_a=14.0,
        grid_charge_a=14.0,
        state="HOLD",
        program_direction="Disabled",
    )
    state = _state()
    _hold(state, facts=facts, voltage=540.0, current=-0.2, pv=0.0, load=500.0)
    assert state.candidates == []


def test_without_the_native_controller_the_inverter_limit_decides():
    facts = ControllerFacts(charge_limit_a=14.0)
    state = _state()
    assert _hold(state, facts=facts).events == ("full",)


def test_a_single_sample_after_a_grid_pulse_does_not_anchor():
    state = _state()
    for second in range(0, 600, 60):
        step(state, SETTINGS, _inputs(second, current=-13.7, pv=0.0))
    step(
        state,
        SETTINGS,
        _inputs(600, voltage=546.4, current=-2.4, pv=0.0, load=400.0),
    )
    estimate = step(state, SETTINGS, _inputs(660, current=0.4, pv=0.0, load=400.0))
    assert estimate.events == ()
    assert state.candidates == []
    assert state.anchor.kind == "initial"


@pytest.fixture(scope="module")
def recording():
    return Recording()


def _replay(recording, settings, start, end):
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
        if not start <= epoch <= end:
            continue
        estimate = step(state, settings, recording.inputs(epoch))
        events += [(epoch, event) for event in estimate.events]
    return state, events


def test_the_recorded_false_anchor_of_10_11_does_not_anchor(recording):
    start = dt.datetime(2026, 10, 11, 19, tzinfo=dt.UTC).timestamp()
    end = dt.datetime(2026, 10, 11, 22, tzinfo=dt.UTC).timestamp()
    state, events = _replay(recording, SETTINGS, start, end)
    assert events == []
    assert state.candidates == []
    assert state.anchor.kind == "full"
    assert state.anchor.at == at(recording.start)


@pytest.mark.parametrize("export_limit_w", [None, 0.0, 5000.0])
def test_the_week_holds_no_full_candidate(recording, export_limit_w):
    site = replace(
        SETTINGS,
        max_charge_current_a=14.0,
        rated_pv_w=8600.0,
        anchor_on_candidates=False,
        export_limit_w=export_limit_w,
    )
    state, events = _replay(recording, site, 0, 1e12)
    assert events == []
    assert state.candidates == []


def test_the_near_miss_of_10_12_fails_both_surplus_rules(recording):
    start = dt.datetime(2026, 10, 12, 13, 20, tzinfo=dt.UTC).timestamp()
    end = dt.datetime(2026, 10, 12, 13, 46, tzinfo=dt.UTC).timestamp()
    exports, margins = [], []
    for epoch in range(int(start), int(end), 60):
        grid = recording.sample("grid_power", epoch)
        pv = recording.sample("pv_power", epoch)
        spare = (
            pv
            - recording.sample("load_power", epoch)
            - max(-recording.sample("battery_current", epoch), 0.0)
            * recording.sample("battery_voltage", epoch)
        )
        exports.append(max(-grid, 0.0))
        margins.append(spare - (300.0 + 0.1 * pv))
    # No power left to the grid, and the derived surplus never reached its line.
    assert max(exports) == 0.0
    assert max(margins) < 0
    for limit in (None, 0.0, 5000.0):
        site = replace(
            SETTINGS,
            max_charge_current_a=14.0,
            rated_pv_w=8600.0,
            anchor_on_candidates=False,
            export_limit_w=limit,
        )
        state, _ = _replay(recording, site, start - 3600, end + 3600)
        assert state.candidates == []
