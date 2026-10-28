"""No state of the native controller writes 0 A to the battery current limits (DD-1).

Islanded, the battery is the inverter's only buffer for load and PV steps: a charge or
discharge limit of 0 A there means the LOAD port has no source (the 2026-10-27 blackout).
The frozen blueprint keeps the 0 A behaviour; only the native engine is held to this.
"""

import itertools
from dataclasses import replace

import pytest
from deye_native_harness import Harness
from deye_oracle import P
from deye_oracle_v0137 import INPUTS

from custom_components.energy_compass import controller_profile as profile
from custom_components.energy_compass.controller_setup import derive_limits, resolve

CAPS = INPUTS | {"commissioned_battery_modes": ["Capacity", "Voltage"]}
ROW_STATES = (
    "CHARGE_PV",
    "SELF_CONSUME",
    "CHARGE_GRID",
    "DISCHARGE_GRID",
    "HOLD",
    "CURTAIL",
)
# Every place that used to write 0 A: the defaults of the non-forcing states, a sell row
# without energy, a reached target, a charging CURTAIL row, HOLD/CURTAIL, the reached
# latch and a full pack.
SOCS = ("50", "5", "100")
OPERATIONS = ("Capacity", "Voltage")
CHARGES = (0, 0.5)
DISCHARGES = (0, 0.5)


def _site(state, *, soc, operation, charge_kwh, discharge_kwh, end_soc_kwh):
    h = Harness(CAPS)
    h.data[h.ctx["operation_entity"]]["state"] = operation
    h.data[P]["attributes"]["intervals"][0].update(
        state=state,
        charge_kwh=charge_kwh,
        discharge_kwh=discharge_kwh,
        end_soc_kwh=end_soc_kwh,
        pv_kwh=0,
        load_kwh=0.05,
        curtail_kwh=0,
    )
    h.data[h.ctx["soc_entity"]]["state"] = soc
    h.accept()
    return h


def _cases():
    return itertools.product(ROW_STATES, SOCS, OPERATIONS, CHARGES, DISCHARGES, (5, 20))


def _currents(h, decision):
    return (
        decision.desired[h.ctx["charge_entity"]],
        decision.desired[h.ctx["discharge_entity"]],
    )


def test_the_default_buffer_is_one_ampere():
    assert profile.Limits(1, 1, 1, 1).min_buffer_current == 1
    resolved = resolve([])
    limits = derive_limits(
        None,
        charge_kw=8,
        discharge_kw=8,
        charge_max=18,
        grid_max=16,
        resolved=resolved,
    )
    assert limits.min_buffer_current == 1
    limits = derive_limits(
        {"min_buffer_current": 2.5},
        charge_kw=8,
        discharge_kw=8,
        charge_max=18,
        grid_max=16,
        resolved=resolved,
    )
    assert limits.min_buffer_current == 2.5


@pytest.mark.parametrize(
    "state,soc,operation,charge_kwh,discharge_kwh,end_soc_kwh", list(_cases())
)
def test_no_state_writes_zero_to_the_battery_currents(
    state, soc, operation, charge_kwh, discharge_kwh, end_soc_kwh
):
    h = _site(
        state,
        soc=soc,
        operation=operation,
        charge_kwh=charge_kwh,
        discharge_kwh=discharge_kwh,
        end_soc_kwh=end_soc_kwh,
    )
    # Every HOLD/CURTAIL and reached-target current the user can set to 0.
    limits = replace(h.limits(), hold_grid_current=0, reached_discharge_current=0)
    decision = profile.decide(h.snapshot(), limits)
    assert decision.valid, decision.reason_code
    charge, discharge = _currents(h, decision)
    assert decision.state == state
    assert charge >= 1 and discharge >= 1
    assert decision.buffer_current == 1


def test_the_reached_latch_keeps_the_buffer():
    h = _site(
        "CHARGE_GRID",
        soc="50",
        operation="Capacity",
        charge_kwh=0.5,
        discharge_kwh=0,
        end_soc_kwh=20,
    )
    first = profile.decide(h.snapshot(), h.limits())
    h.data["sensor.inverter_deye_battery"]["state"] = "81"
    reached = profile.decide(h.snapshot(), h.limits())
    assert reached.target_reached
    # The latch itself: a runtime that already holds the reached key.
    latched = profile.decide(
        replace(h.snapshot(), runtime={"reached_key": reached.reached_key}),
        h.limits(),
    )
    for decision in (first, reached, latched):
        charge, discharge = _currents(h, decision)
        assert charge >= 1 and discharge >= 1


def test_base_still_relinquishes_instead_of_buffering():
    h = _site(
        "HOLD",
        soc="50",
        operation="Capacity",
        charge_kwh=0,
        discharge_kwh=0,
        end_soc_kwh=20,
    )
    decision = profile.decide(h.snapshot(), h.limits(), base=True)
    assert decision.state == "BASE"
    relinquish = h.limits().relinquish_current
    assert _currents(h, decision) == (relinquish, relinquish)


def test_zero_is_an_explicit_expert_opt_out():
    h = _site(
        "HOLD",
        soc="50",
        operation="Capacity",
        charge_kwh=0,
        discharge_kwh=0,
        end_soc_kwh=20,
    )
    limits = replace(h.limits(), hold_grid_current=0, min_buffer_current=0)
    decision = profile.decide(h.snapshot(), limits)
    assert decision.buffer_current == 0
    assert _currents(h, decision)[0] == 0


def test_a_larger_buffer_raises_the_floor():
    h = _site(
        "DISCHARGE_GRID",
        soc="50",
        operation="Capacity",
        charge_kwh=0,
        discharge_kwh=0,
        end_soc_kwh=20,
    )
    decision = profile.decide(h.snapshot(), replace(h.limits(), min_buffer_current=3))
    charge, discharge = _currents(h, decision)
    assert charge >= 3 and discharge >= 3
