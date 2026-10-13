"""A CURTAIL row that charges (T-064) charges the pack from PV; any other CURTAIL holds.

The scenarios run against the generated blueprint and the native controller. They live
outside `test_deye_controller.py`, whose scenarios the frozen 0.1.37 blueprint replays:
that blueprint holds every CURTAIL row, which is the divergence pinned here.
"""

import pytest
from deye_engines import Harness, use
from deye_harness import INPUTS, P

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


def test_charging_curtail_charges_from_pv_at_the_charge_cap():
    h = Harness()
    d = decide(h, state="CURTAIL", charge_kwh=0.5)
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
