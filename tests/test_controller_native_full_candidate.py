"""A running SOC-estimator full candidate keeps the charge path open (native only).

The frozen blueprint knows nothing of the fact: it closes the charge path where
the profile says so, which can break the estimator's hold there. With the fact
unset `decide` is unchanged, which the parity suites pin against the blueprint.
"""

from dataclasses import replace

import pytest
from deye_native_harness import Harness
from deye_oracle import P
from deye_oracle_v0137 import INPUTS

from custom_components.energy_compass import controller_profile as profile

CAPS = INPUTS | {"commissioned_battery_modes": ["Capacity", "Voltage"]}


def _site(state, *, soc="90", operation="Capacity", **row):
    h = Harness(CAPS)
    h.data[h.ctx["operation_entity"]]["state"] = operation
    h.data[P]["attributes"]["intervals"][0].update(
        {"state": state, "charge_kwh": 0, "discharge_kwh": 0, "end_soc_kwh": 25, **row}
    )
    h.data[h.ctx["soc_entity"]]["state"] = soc
    h.accept()
    return h


def _decide(h, candidate, *, limits=None):
    snapshot = replace(h.snapshot(), full_candidate=candidate)
    return profile.decide(snapshot, limits or h.limits())


def _charge(h, decision):
    return decision.desired[h.ctx["charge_entity"]]


def _grid(h, decision):
    return decision.desired[h.ctx["grid_entity"]]


def _program(decision):
    return [
        value
        for entity, value in decision.desired.items()
        if entity.startswith("select.") and entity.endswith("_charging")
    ]


def test_the_fact_defaults_to_false_and_changes_nothing():
    h = _site("CHARGE_PV")
    assert h.snapshot().full_candidate is False
    assert profile.decide(h.snapshot(), h.limits()) == _decide(h, False)


@pytest.mark.parametrize("state", ["CHARGE_PV", "SELF_CONSUME", "HOLD"])
@pytest.mark.parametrize("operation", ["Capacity", "Voltage"])
@pytest.mark.parametrize("soc", ["90", "99.5", "100"])
def test_a_candidate_keeps_the_charge_current_at_the_cap(state, operation, soc):
    h = _site(state, soc=soc, operation=operation)
    open_ = _decide(h, True)
    plain = _decide(h, False)
    assert open_.valid and open_.state == state
    assert _charge(h, open_) == 14
    # Only the charge current may differ; the program direction and the grid
    # current are the profile's own.
    assert _grid(h, open_) == _grid(h, plain)
    assert _program(open_) == _program(plain)
    assert (
        open_.desired[h.ctx["discharge_entity"]]
        == plain.desired[h.ctx["discharge_entity"]]
    )
    differing = {k for k in open_.desired if open_.desired[k] != plain.desired[k]}
    assert differing <= {h.ctx["charge_entity"]}


def test_the_closed_profiles_without_a_candidate_are_what_the_candidate_opens():
    cases = {
        ("CHARGE_PV", "Capacity", "100"): 1,
        ("SELF_CONSUME", "Capacity", "100"): 1,
        ("HOLD", "Capacity", "90"): 1,
        ("HOLD", "Voltage", "99"): 1,
    }
    for (state, operation, soc), closed in cases.items():
        h = _site(state, soc=soc, operation=operation)
        assert _charge(h, _decide(h, False)) == closed
        assert _charge(h, _decide(h, True)) == 14


def test_hold_with_a_candidate_never_opens_grid_charging_the_plan_did_not_ask_for():
    h = _site("HOLD")
    open_, plain = _decide(h, True), _decide(h, False)
    # The TOU floor holds the SOC: no grid charge current (DD-2).
    assert _grid(h, open_) == _grid(h, plain) == 0
    assert _program(open_) == _program(plain)
    for state in ("CHARGE_PV", "SELF_CONSUME"):
        site = _site(state, soc="100", operation="Voltage")
        decision = _decide(site, True)
        assert _grid(site, decision) == 0
        assert set(_program(decision)) == {"Disabled"}


def test_the_charge_stop_at_a_full_grid_charge_row_is_kept_open_for_the_candidate():
    h = _site("CHARGE_GRID", soc="100", charge_kwh=0.5, pv_kwh=0.3, load_kwh=0.05)
    plain, open_ = _decide(h, False), _decide(h, True)
    assert _charge(h, plain) == 1
    assert _grid(h, plain) == 0
    assert _charge(h, open_) == 14
    assert _grid(h, open_) == 0
    assert _program(open_) == _program(plain)


@pytest.mark.parametrize("state", ["CHARGE_PV", "SELF_CONSUME", "HOLD"])
def test_the_cap_follows_the_power_limit_and_the_current_ceiling_hysteresis(state):
    h = _site(state, soc="100", operation="Voltage")
    limits = replace(h.limits(), max_power_w=4000)
    voltage = float(h.data[h.ctx["voltage_entity"]]["state"])
    cap = min(h.ctx["max_current"], 4000 / voltage)
    assert _charge(h, _decide(h, True, limits=limits)) == pytest.approx(cap, abs=1)
    assert _charge(h, _decide(h, True, limits=limits)) < 14
    # The held current inside the hysteresis band stays, as in the plain profile.
    h.data[h.ctx["charge_entity"]]["state"] = "14"
    for volts in ("535.0", "533.0"):
        h.data[h.ctx["voltage_entity"]]["state"] = volts
        assert _charge(h, _decide(h, True)) == 14


@pytest.mark.parametrize("state", ["DISCHARGE_GRID", "CURTAIL"])
def test_other_states_are_not_touched(state):
    h = _site(state, soc="100")
    assert _decide(h, True) == _decide(h, False)


def test_a_balance_row_is_not_touched():
    h = _site("CHARGE_GRID", soc="100", charge_kwh=0.5, balance_hold=True)
    assert _decide(h, True) == _decide(h, False)


def test_nothing_opens_while_the_controller_is_not_acting():
    h = _site("HOLD")
    h.data[h.ctx["mode_entity"]]["state"] = "Off"
    off = _decide(h, True)
    assert off.state == "BASE"
    assert off == _decide(h, False)
    invalid = _site("HOLD")
    invalid.data[invalid.ctx["operation_entity"]]["state"] = "Backup"
    assert not _decide(invalid, True).valid
    assert _decide(invalid, True) == _decide(invalid, False)
