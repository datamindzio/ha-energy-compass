"""Full-anchor rule (DD-3): synthetic cases and recorded episodes."""

import datetime as dt
from dataclasses import replace

import pytest
from soc_estimator_support import Recording, at

from custom_components.energy_compass.engine.models import InputError
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
    full_min_voltage_v=530.0,
    eta_charge=0.9,
    max_charge_current_a=14.0,
    rated_pv_w=8000.0,
    anchor_on_candidates=True,
    export_limit_w=0.0,
    full_hold_seconds=600.0,
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
    fresh=True,
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
        anchor_inputs_fresh=fresh,
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
    assert candidate["charge_limit_a"] == 14.0


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


@pytest.mark.parametrize("state_name", ["HOLD", "CURTAIL", "BASE", "DISCHARGE_GRID"])
def test_these_controller_states_never_anchor_full(state_name):
    facts = ControllerFacts(charge_limit_a=14.0, state=state_name)
    state = _state()
    _hold(state, EXPORTING, facts=facts, grid=-1500.0)
    assert state.candidates == []


def _grid_facts(**changes):
    values = {
        "charge_limit_a": 14.0,
        "grid_charge_a": 14.0,
        "state": "CHARGE_GRID",
        "program_direction": "Grid",
        "program_voltage_v": 552.0,
    }
    return ControllerFacts(**{**values, **changes})


@pytest.mark.parametrize(
    "facts",
    [
        _grid_facts(program_direction="Disabled"),
        _grid_facts(program_direction=None),
        _grid_facts(grid_charge_a=1.0),
        _grid_facts(program_voltage_v=None),
    ],
    ids=["disabled", "no-program", "one-amp-grid", "no-voltage"],
)
def test_grid_charging_without_a_requested_grid_charge_never_anchors_full(facts):
    state = _state()
    _hold(state, EXPORTING, facts=facts, grid=-1500.0, voltage=540.0)
    assert state.candidates == []


def test_grid_charging_below_its_program_voltage_may_anchor_full():
    state = _state()
    _hold(state, EXPORTING, facts=_grid_facts(), grid=-1500.0, voltage=540.0)
    assert len(state.candidates) == 1
    state = _state()
    _hold(state, EXPORTING, facts=_grid_facts(), grid=-1500.0, voltage=547.0)
    assert state.candidates == []


def test_without_the_native_controller_only_export_evidence_counts():
    # No dispatch state and no TOU program are visible: no grid path.
    facts = ControllerFacts(
        charge_limit_a=14.0,
        grid_charge_a=14.0,
        program_direction="Grid",
        program_voltage_v=552.0,
    )
    state = _state()
    _hold(state, EXPORTING, facts=facts, voltage=540.0, pv=0.0, grid=300.0)
    assert state.candidates == []
    state = _state()
    _hold(state, EXPORTING, facts=ControllerFacts(14.0), grid=-1500.0)
    [candidate] = state.candidates
    assert candidate["path"] == "export"


def test_a_stale_anchor_input_breaks_the_hold_and_cannot_start_one():
    state = _state()
    _hold(state, seconds=540)
    assert state.full_candidate_since is not None
    step(state, SETTINGS, _inputs(570, fresh=False))
    assert state.full_candidate_since is None
    assert not step(state, SETTINGS, _inputs(600)).events
    assert state.candidates == []
    state = _state()
    for second in range(0, 1200, 60):
        step(state, SETTINGS, _inputs(second, fresh=False))
    assert state.candidates == []
    assert state.full_candidate_since is None


def test_the_counters_far_below_full_do_not_keep_a_full_pack_from_anchoring():
    # The counters may have drifted low; the pack voltage is the evidence, so
    # an anchor must be able to correct them.
    state = EstimatorState(Anchor("initial", T0, 4.0, 1000.0, 500.0), T0)
    estimate = _hold(state)
    assert estimate.events == ("full",)
    assert estimate.energy_kwh == 24.0
    [candidate] = state.candidates
    assert candidate["outcome"] == "anchored"
    assert candidate["energy_before_kwh"] == pytest.approx(4.0, abs=0.1)
    assert "rejected_low_energy" not in repr(state.candidates)


def test_a_low_counter_candidate_still_qualifies_the_balance():
    state = EstimatorState(Anchor("initial", T0, 4.0, 1000.0, 500.0), T0)
    estimate = _hold(state, replace(SETTINGS, anchor_on_candidates=False))
    assert estimate.full_qualified
    assert state.candidates[0]["outcome"] == "candidate"


def test_the_minimum_full_voltage_gates_the_start():
    gated = replace(SETTINGS, full_min_voltage_v=550.0)
    state = _state()
    estimate = _hold(state, gated, voltage=549.9)
    assert not estimate.full_candidate
    assert state.candidates == []
    state = _state()
    assert _hold(state, gated, voltage=550.0).events == ("full",)


SITE_552 = replace(SETTINGS, full_min_voltage_v=552.0, full_hold_seconds=3600.0)


def _charge_then_rest(state, settings, *, peak, rest=545.7, gap=0):
    """Charge at 8 A up to `peak`, then rest at about 0 A (live, 2026-10-17)."""
    for second, voltage in ((-1200, 544.1), (-600, 546.7), (-60, peak)):
        step(state, settings, _inputs(second, voltage=voltage, current=-7.9))
    estimate = None
    for second in range(gap, gap + 3601, 60):
        estimate = step(state, settings, _inputs(second, voltage=rest))
    return estimate


def test_a_pack_that_reached_the_full_voltage_and_rests_below_it_is_full():
    # 556 V under 8 A at the stop, 545.7 V at rest with the sun still up.
    state = _state()
    estimate = _charge_then_rest(state, SITE_552, peak=556.4)
    assert estimate.events == ("full",)
    [candidate] = state.candidates
    assert candidate["voltage_v"] == 545.7


def test_a_charge_that_stops_short_of_the_full_voltage_is_not_full():
    state = _state()
    estimate = _charge_then_rest(state, SITE_552, peak=551.9)
    assert not estimate.full_candidate
    assert state.candidates == []


def test_the_full_voltage_peak_must_be_recent():
    state = _state()
    estimate = _charge_then_rest(state, SITE_552, peak=556.4, gap=1800)
    assert not estimate.full_candidate
    assert state.candidates == []


def test_a_stale_reading_at_the_full_voltage_is_no_peak():
    state = _state()
    step(state, SITE_552, _inputs(-60, voltage=556.4, current=-7.9, fresh=False))
    estimate = _hold(state, SITE_552, seconds=3600, voltage=545.7)
    assert not estimate.full_candidate


def test_the_minimum_full_voltage_is_required_and_positive():
    with pytest.raises(InputError):
        EstimatorSettings(
            capacity_kwh=24.0, empty_voltage_v=480.0, full_min_voltage_v=0
        )
    with pytest.raises(TypeError):
        EstimatorSettings(capacity_kwh=24.0, empty_voltage_v=480.0)


def test_the_default_full_hold_is_one_hour():
    site = EstimatorSettings(
        capacity_kwh=24.0, empty_voltage_v=480.0, full_min_voltage_v=530.0
    )
    assert site.full_hold_seconds == 3600.0
    state = _state()
    hour = replace(
        site, anchor_on_candidates=True, export_limit_w=0.0, rated_pv_w=8000.0
    )
    almost = _hold(state, hour, seconds=3540)
    assert almost.full_candidate
    assert state.candidates == []
    assert almost.anchor_kind == "initial"
    estimate = step(state, hour, _inputs(3600))
    assert estimate.events == ("full",)
    [candidate] = state.candidates
    assert candidate["started_at"] == T0.isoformat()
    assert candidate["qualified_at"] == (T0 + dt.timedelta(hours=1)).isoformat()


def test_a_dip_below_the_rest_voltage_breaks_the_hold_and_is_logged(caplog):
    gated = replace(SETTINGS, full_min_voltage_v=540.0, full_hold_seconds=3600.0)
    state = _state()
    _hold(state, gated, seconds=3300)
    assert state.full_candidate_since == T0
    with caplog.at_level("INFO", logger="custom_components.energy_compass"):
        step(state, gated, _inputs(3360, voltage=546.0))
    # Resting a few volts below the full voltage keeps the hold.
    assert state.full_candidate_since == T0
    with caplog.at_level("INFO", logger="custom_components.energy_compass"):
        step(state, gated, _inputs(3380, voltage=523.7))
    assert state.full_candidate_since is None
    assert "below_full_voltage" in caplog.text
    # The hold restarts from the first reading back at the gate.
    for second in range(3420, 3420 + 3600, 60):
        estimate = step(state, gated, _inputs(second))
    assert estimate.events == ()
    assert estimate.full_candidate
    assert step(state, gated, _inputs(3420 + 3600)).events == ("full",)


def test_a_qualified_candidate_ends_when_the_voltage_drops():
    state = _state()
    estimate = _hold(state, replace(SETTINGS, anchor_on_candidates=False))
    assert estimate.full_qualified
    after = step(state, SETTINGS, _inputs(660, voltage=514.0))
    assert not after.full_qualified
    assert not after.full_candidate


def test_a_low_voltage_alone_is_not_logged_as_a_broken_hold(caplog):
    state = _state()
    with caplog.at_level("INFO", logger="custom_components.energy_compass"):
        _hold(state, voltage=500.0)
    assert "below_full_voltage" not in caplog.text


HOLDING = ControllerFacts(charge_limit_a=14.0, state="HOLD", program_direction="Grid")


def test_a_candidate_continues_in_hold_while_the_charge_path_is_open():
    state = _state()
    _hold(state, EXPORTING, grid=-1500.0, seconds=300)
    assert state.full_candidate_since == T0
    for second in range(360, 601, 60):
        estimate = step(state, EXPORTING, _inputs(second, facts=HOLDING, grid=-1500.0))
    assert estimate.events == ("full",)
    assert state.candidates[0]["path"] == "export"
    assert state.candidates[0]["controller_state"] == "HOLD"


def test_hold_never_starts_a_candidate():
    state = _state()
    _hold(state, EXPORTING, facts=HOLDING, grid=-1500.0)
    assert state.candidates == []
    assert state.full_candidate_since is None


def test_a_candidate_in_hold_ends_when_the_charge_path_stays_closed():
    state = _state()
    _hold(state, EXPORTING, grid=-1500.0, seconds=300)
    closed = replace(HOLDING, charge_limit_a=1.0)
    step(state, EXPORTING, _inputs(360, facts=closed, grid=-1500.0))
    assert state.full_candidate_since == T0
    step(state, EXPORTING, _inputs(480, facts=closed, grid=-1500.0))
    assert state.full_candidate_since == T0
    step(state, EXPORTING, _inputs(481, facts=closed, grid=-1500.0))
    assert state.full_candidate_since is None


def test_a_low_charge_limit_that_returns_within_the_grace_keeps_the_candidate():
    state = _state()
    _hold(state, EXPORTING, grid=-1500.0, seconds=300)
    closed = replace(HOLDING, charge_limit_a=0.0)
    step(state, EXPORTING, _inputs(360, facts=closed, grid=-1500.0))
    step(state, EXPORTING, _inputs(361, facts=HOLDING, grid=-1500.0))
    step(state, EXPORTING, _inputs(420, facts=closed, grid=-1500.0))
    step(state, EXPORTING, _inputs(540, facts=closed, grid=-1500.0))
    assert state.full_candidate_since == T0
    estimate = step(state, EXPORTING, _inputs(601, facts=HOLDING, grid=-1500.0))
    assert estimate.events == ("full",)


def test_the_charge_limit_grace_never_starts_a_candidate():
    state = _state()
    closed = replace(TAPER, charge_limit_a=0.0)
    for second in range(0, 121, 30):
        step(state, EXPORTING, _inputs(second, facts=closed, grid=-1500.0))
    assert state.full_candidate_since is None


def test_the_grace_does_not_cover_a_low_voltage_or_a_stale_input():
    closed = replace(HOLDING, charge_limit_a=0.0)
    state = _state()
    _hold(state, EXPORTING, grid=-1500.0, seconds=300)
    step(state, EXPORTING, _inputs(360, facts=closed, grid=-1500.0, voltage=500.0))
    assert state.full_candidate_since is None
    state = _state()
    _hold(state, EXPORTING, grid=-1500.0, seconds=300)
    step(state, EXPORTING, _inputs(360, facts=closed, grid=-1500.0, fresh=False))
    assert state.full_candidate_since is None
    state = _state()
    _hold(state, EXPORTING, grid=-1500.0, seconds=300)
    discharging = ControllerFacts(charge_limit_a=0.0, state="DISCHARGE_GRID")
    step(state, EXPORTING, _inputs(360, facts=discharging, grid=-1500.0))
    assert state.full_candidate_since is None


def test_hold_does_not_turn_its_grid_program_into_evidence():
    # In HOLD the Grid program only holds the SOC: neither its voltage target
    # nor requested grid charging says anything about a full pack.
    state = _state()
    _hold(state, EXPORTING, grid=-1500.0, seconds=300)
    facts = replace(HOLDING, grid_charge_a=14.0, program_voltage_v=552.0)
    estimate = step(state, EXPORTING, _inputs(360, facts=facts, grid=-1500.0))
    assert estimate.full_candidate
    assert state.full_candidate_since == T0
    weak = step(state, EXPORTING, _inputs(420, facts=facts, grid=300.0, pv=0.0))
    assert not weak.full_candidate


@pytest.mark.parametrize("state_name", ["CURTAIL", "BASE", "DISCHARGE_GRID"])
def test_the_other_excluded_states_still_end_a_running_candidate(state_name):
    state = _state()
    _hold(state, EXPORTING, grid=-1500.0, seconds=300)
    facts = ControllerFacts(charge_limit_a=14.0, state=state_name)
    step(state, EXPORTING, _inputs(360, facts=facts, grid=-1500.0))
    assert state.full_candidate_since is None


# The recording keeps real voltages: this site's full voltage and the one-hour hold.
SITE = replace(SETTINGS, full_min_voltage_v=552.0, full_hold_seconds=3600.0)


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
    state, events = _replay(recording, SITE, start, end)
    assert events == []
    assert state.candidates == []
    assert state.anchor.kind == "full"
    assert state.anchor.at == at(recording.start)


@pytest.mark.parametrize("export_limit_w", [None, 0.0, 5000.0])
def test_the_week_holds_no_full_candidate(recording, export_limit_w):
    site = replace(
        SITE,
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
            SITE,
            max_charge_current_a=14.0,
            rated_pv_w=8600.0,
            anchor_on_candidates=False,
            export_limit_w=limit,
        )
        state, _ = _replay(recording, site, start - 3600, end + 3600)
        assert state.candidates == []
