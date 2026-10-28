"""HOLD and CURTAIL hold the pack by the TOU floor, not by a grid charge current (DD-2).

Direction Grid with a SOC target at or below the live level makes the inverter discharge
no lower than that level while the discharge limit stays open, so a grid outage finds the
battery able to carry the house. The frozen blueprint keeps the old 0 A hold; only the
native engine is held to this.
"""

from dataclasses import replace

import pytest
from deye_harness import CACHE
from deye_native_harness import RT, Harness, Runner
from deye_oracle import P
from deye_oracle_v0137 import INPUTS

from custom_components.energy_compass import controller_profile as profile

CAPS = INPUTS | {"commissioned_battery_modes": ["Capacity", "Voltage"]}
CAP = 14


def _site(state="HOLD", *, soc="51", operation="Capacity", end_soc_kwh=20, **row):
    h = Harness(CAPS)
    h.data[h.ctx["operation_entity"]]["state"] = operation
    h.data[P]["attributes"]["intervals"][0].update(
        {
            "state": state,
            "charge_kwh": 0,
            "discharge_kwh": 0,
            "pv_kwh": 0,
            "load_kwh": 0.05,
            "curtail_kwh": 0,
            "end_soc_kwh": end_soc_kwh,
            **row,
        }
    )
    h.data[h.ctx["soc_entity"]]["state"] = soc
    h.accept()
    return h


def _knot(soc):
    return profile._knot_voltage(soc, profile.DISCHARGE_KNOTS, "floor")


def _decide(h, runtime=None):
    snapshot = h.snapshot()
    if runtime is not None:
        snapshot = replace(snapshot, runtime=runtime)
    return profile.decide(snapshot, h.limits())


@pytest.mark.parametrize("state", ["HOLD", "CURTAIL"])
def test_the_hold_values_leave_the_discharge_open_and_the_grid_closed(state):
    h = _site(state)
    d = _decide(h)
    assert d.valid and d.state == state
    assert d.desired[h.ctx["discharge_entity"]] == CAP
    assert d.desired[h.ctx["grid_entity"]] == 0
    assert d.desired[h.ctx["charge_entity"]] == h.ctx["hold_grid_current"]
    assert d.desired[f"select.inverter_deye_program_{d.active_tou}_charging"] == "Grid"


@pytest.mark.parametrize(
    "end_soc_kwh,soc,expected",
    [
        (20, 51, 51),  # the plan is above the pack: never target a grid charge
        (25, 51, 51),
        (12, 51, 48),  # the plan is below the pack: the floor follows the plan
        (0, 51, 10),
        (20, 100, 80),
    ],
)
def test_capacity_target_is_the_lower_of_plan_and_live_level(
    end_soc_kwh, soc, expected
):
    h = _site(soc=str(soc), end_soc_kwh=end_soc_kwh)
    d = _decide(h)
    assert d.target_soc == expected
    assert d.target_voltage == _knot(expected)
    assert d.hold_level["soc"] == soc


def test_voltage_target_never_exceeds_the_live_voltage():
    h = _site(soc="51", operation="Voltage", end_soc_kwh=25)
    d = _decide(h)
    assert d.hold_level["voltage"] == d.target_voltage
    # Dispatch floor of the voltage curve: the live pack voltage (rounded down to 0.1 V).
    live = float(h.data[h.ctx["voltage_entity"]]["state"]) / 10
    assert d.target_voltage <= live
    assert d.target_voltage <= _knot(100)


def test_the_hold_level_latches_while_the_hold_lasts():
    h = _site(soc="51", end_soc_kwh=25)
    first = _decide(h)
    runtime = {"state": "HOLD", "hold_level": first.hold_level}
    for soc in ("52", "60", "50", "35"):
        h.data[h.ctx["soc_entity"]]["state"] = soc
        again = _decide(h, runtime)
        assert again.hold_level == first.hold_level
        assert again.target_soc == first.target_soc
        assert again.target_voltage == first.target_voltage


def test_a_new_hold_after_another_state_takes_the_live_level():
    h = _site(soc="51", end_soc_kwh=25)
    first = _decide(h)
    h.data[h.ctx["soc_entity"]]["state"] = "70"
    after_pv = _decide(h, {"state": "CHARGE_PV", "hold_level": first.hold_level})
    assert after_pv.hold_level["soc"] == 70
    assert after_pv.target_soc == 70


def test_the_hold_level_reaches_the_runtime_and_clears_with_the_state():
    h = _site(soc="51", end_soc_kwh=25)
    r = Runner(h)
    r.run()
    assert h.data[RT]["attributes"]["runtime"]["hold_level"]["soc"] == 51
    h.data[CACHE]["attributes"]["snapshot"]["intervals"][0].update(
        state="CHARGE_PV", charge_kwh=0.2, end_soc_kwh=26
    )
    r.run()
    assert not h.data[RT]["attributes"]["runtime"].get("hold_level")


@pytest.mark.parametrize("state", ["CHARGE_PV", "SELF_CONSUME", "DISCHARGE_GRID"])
def test_other_states_carry_no_hold_level(state):
    h = _site(state, charge_kwh=0.1, discharge_kwh=0.1)
    assert not _decide(h).hold_level


def test_a_charging_curtail_row_keeps_the_buffer_not_the_cap():
    h = _site("CURTAIL", charge_kwh=0.2, end_soc_kwh=22)
    d = _decide(h)
    assert d.state == "CURTAIL"
    assert d.desired[h.ctx["discharge_entity"]] >= 1
    assert d.desired[h.ctx["grid_entity"]] == 0
    assert not d.hold_level


@pytest.mark.parametrize("operation", ["Capacity", "Voltage"])
def test_grid_charging_keeps_the_discharge_cap_open(operation):
    h = _site("CHARGE_GRID", operation=operation, charge_kwh=0.5, end_soc_kwh=20)
    d = _decide(h)
    assert d.state == "CHARGE_GRID"
    assert d.desired[h.ctx["discharge_entity"]] == CAP
    reached = _decide(h, {"reached_key": d.reached_key})
    assert reached.desired[h.ctx["discharge_entity"]] == CAP


def test_a_full_candidate_hold_still_opens_the_charge_cap():
    h = _site("HOLD", soc="90", end_soc_kwh=25)
    snapshot = replace(h.snapshot(), full_candidate=True)
    d = profile.decide(snapshot, h.limits())
    assert d.desired[h.ctx["charge_entity"]] == CAP
    assert d.desired[h.ctx["grid_entity"]] == 0
