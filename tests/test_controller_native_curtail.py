"""A CURTAIL row that charges (T-064) charges the pack from PV; any other CURTAIL holds.

The scenarios run against the generated blueprint and the native controller. They live
outside `test_deye_controller.py`, whose scenarios the frozen 0.1.37 blueprint replays:
that blueprint holds every CURTAIL row, which is the divergence pinned here.
"""

from pathlib import Path

import pytest
from deye_engines import Harness, use
from deye_harness import INPUTS, P

from custom_components.energy_compass.controller_profile import CURTAIL_CHARGE_MIN_KWH
from custom_components.energy_compass.engine import machine_state

BLUEPRINT = (
    Path(__file__).parents[1]
    / "blueprints/automation/energy_compass/deye_solarman_controller.yaml"
)

HOLD_WARNING = "CURTAIL nieobsługiwany: zastosowano HOLD"


@pytest.fixture(autouse=True, params=["blueprint", "native"])
def engine(request):
    use(request.param)
    yield request.param
    use("blueprint")


def decide(h, **row):
    h.data[P]["attributes"]["intervals"][0].update(
        pv_kwh=0, load_kwh=0.05, curtail_kwh=0, end_soc_kwh=20, discharge_kwh=0, **row
    )
    h.accept()
    return h.decision()


def program(d, field):
    return d["desired"][
        f"{field.split(':')[0]}.inverter_deye_program_{d['active_tou']}_{field.split(':')[1]}"
    ]


QUARTER = {
    "start": "2026-09-19T11:30:00+02:00",
    "end": "2026-09-19T11:45:00+02:00",
}


def planned_kwh(h, amps):
    """The charge_kwh of a quarter hour that takes `amps` into the pack."""
    return (
        amps
        * float(h.data[h.ctx["voltage_entity"]]["state"])
        * 0.25
        / 1000
        / h.battery["eta_charge"]
    )


def test_charging_curtail_at_charge_power_runs_the_charge_cap():
    h = Harness()
    d = decide(h, state="CURTAIL", charge_kwh=planned_kwh(h, 13.9), **QUARTER)
    reference = decide(Harness(), state="CHARGE_PV", charge_kwh=0.5)
    values = d["desired"]
    assert d["valid"] and d["state"] == "CURTAIL" and not d["target_reached"]
    assert values[h.ctx["charge_entity"]] > h.ctx["hold_grid_current"]
    assert (
        values[h.ctx["charge_entity"]] == reference["desired"][h.ctx["charge_entity"]]
    )
    assert values[h.ctx["discharge_entity"]] == 0
    assert values[h.ctx["grid_entity"]] == 0
    assert program(d, "select:charging") == "Disabled"
    assert d["target_soc"] == 10 and d["target_voltage"] == 49.6
    assert d["warning"] == ""


@pytest.mark.parametrize(
    "amps,written",
    [(3.2, 4), (4, 4), (7.01, 8), (13.99, 14), (30, 14)],
)
def test_charging_curtail_charges_at_the_planned_current_rounded_up_to_the_step(
    amps, written
):
    """A row planning less than charge_kw x h charges less than the cap (T-076 review)."""
    h = Harness()
    d = decide(h, state="CURTAIL", charge_kwh=planned_kwh(h, amps), **QUARTER)
    assert d["desired"][h.ctx["charge_entity"]] == written
    assert d["desired"][h.ctx["grid_entity"]] == 0
    assert d["desired"][h.ctx["discharge_entity"]] == 0
    assert program(d, "select:charging") == "Disabled"
    assert d["warning"] == ""


def test_charging_curtail_never_charges_below_the_hold_current():
    h = Harness()
    d = decide(h, state="CURTAIL", charge_kwh=planned_kwh(h, 0.05), **QUARTER)
    assert d["desired"][h.ctx["charge_entity"]] == h.ctx["hold_grid_current"]
    assert d["desired"][h.ctx["grid_entity"]] == 0
    assert program(d, "select:charging") == "Disabled"
    assert d["warning"] == ""


def test_planned_current_rounds_up_to_the_charge_entity_step():
    h = Harness()
    h.data[h.ctx["charge_entity"]]["attributes"]["step"] = 5
    d = decide(h, state="CURTAIL", charge_kwh=planned_kwh(h, 3.2), **QUARTER)
    assert d["desired"][h.ctx["charge_entity"]] == 5


@pytest.mark.parametrize(
    "charge_kwh", [0, -1e-9, 1e-34, 4.7e-17, 8.5e-15, 4.7e-13, CURTAIL_CHARGE_MIN_KWH]
)
def test_solver_noise_in_charge_kwh_holds_the_planned_soc(charge_kwh):
    """The plan publishes flows unrounded: up to 1e-6 kWh is the classifier's zero."""
    h = Harness()
    d = decide(h, state="CURTAIL", charge_kwh=charge_kwh, **QUARTER)
    band = h.ctx["hold_grid_current"]
    assert d["desired"][h.ctx["charge_entity"]] == band
    assert d["desired"][h.ctx["grid_entity"]] == band
    assert program(d, "select:charging") == "Grid"
    assert d["warning"] == HOLD_WARNING


@pytest.mark.parametrize("charge_kwh", [1.000001e-6, 2e-6, 1e-3])
def test_charge_kwh_above_the_noise_threshold_charges(charge_kwh):
    h = Harness()
    d = decide(h, state="CURTAIL", charge_kwh=charge_kwh, **QUARTER)
    assert d["desired"][h.ctx["grid_entity"]] == 0
    assert program(d, "select:charging") == "Disabled"
    assert d["warning"] == ""


def test_the_threshold_is_the_engine_classifier_zero():
    assert CURTAIL_CHARGE_MIN_KWH == machine_state._TOL
    assert "charge_kwh|float(0) > 0.000001" in BLUEPRINT.read_text()


def test_a_curtail_that_does_not_charge_holds_the_planned_soc():
    h = Harness()
    d = decide(h, state="CURTAIL", charge_kwh=0)
    band = h.ctx["hold_grid_current"]
    assert d["valid"] and d["state"] == "CURTAIL"
    assert d["desired"][h.ctx["charge_entity"]] == band
    assert d["desired"][h.ctx["grid_entity"]] == band
    assert d["desired"][h.ctx["discharge_entity"]] == 0
    assert program(d, "select:charging") == "Grid"
    assert d["warning"] == HOLD_WARNING


@pytest.mark.parametrize(
    "operation,charge",
    [("Voltage", 14), ("Capacity", 0)],
)
def test_charging_curtail_on_a_full_pack_follows_charge_pv(operation, charge):
    """Full pack: Voltage keeps the PV charge cap (the SOC estimate drifts), Capacity stops."""
    h = Harness(INPUTS | {"commissioned_battery_modes": ["Capacity", "Voltage"]})
    h.data[h.ctx["operation_entity"]]["state"] = operation
    h.data[h.ctx["soc_entity"]]["state"] = "100"
    d = decide(h, state="CURTAIL", charge_kwh=0.5)
    assert d["valid"] and d["state"] == "CURTAIL"
    assert d["desired"][h.ctx["charge_entity"]] == charge
    assert d["desired"][h.ctx["grid_entity"]] == 0
    assert d["desired"][h.ctx["discharge_entity"]] == 0
    assert program(d, "select:charging") == "Disabled"
