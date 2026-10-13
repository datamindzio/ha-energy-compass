"""A depleted battery must not invalidate otherwise feasible household plans."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_optimize import battery, problem
from test_task5_runtime import battery_configuration

from custom_components.energy_compass.engine.dispatch_policy import safety_exception
from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.engine.optimize import solve
from custom_components.energy_compass.runtime import compute


@pytest.mark.parametrize("initial", [0, 0.5, 1])
@pytest.mark.parametrize("dwell", [0, 60])
def test_empty_or_low_battery_can_wait_without_grid_charging(initial, dwell):
    source = replace(
        problem(
            ((5, 0, 0, 1),) * 2,
            battery=battery(
                initial_kwh=initial, minimum_soc_fraction=0.1, allow_grid_charge=False
            ),
        ),
        minimum_mode_minutes=dwell,
    )
    plan = solve(source)
    for flow in plan.flows:
        assert flow.end_soc_kwh == pytest.approx(initial)
        assert flow.charge_kwh == pytest.approx(0)
        assert flow.discharge_kwh == pytest.approx(0)
        assert flow.grid_import_kwh == pytest.approx(1)


@pytest.mark.parametrize("initial", [0, 0.5])
@pytest.mark.parametrize("dwell", [0, 60])
def test_partial_recharge_cannot_be_spent_below_reserve(initial, dwell):
    source = replace(
        problem(
            ((-1, 0, 0, 0), (10, 0, 0, 1), (10, 0, 0, 1)),
            battery=battery(
                initial_kwh=initial, minimum_soc_fraction=0.1, charge_kw=0.25
            ),
        ),
        minimum_mode_minutes=dwell,
    )
    plan = solve(source)
    assert plan.flows[0].charge_kwh == pytest.approx(0.25)
    for flow in plan.flows:
        assert flow.end_soc_kwh == pytest.approx(initial + 0.25)
        assert flow.discharge_kwh == pytest.approx(0)


@pytest.mark.parametrize("dwell", [0, 60])
def test_discharge_resumes_after_recharge_but_preserves_full_reserve(dwell):
    source = replace(
        problem(
            ((5, 0, 2, 0), (10, 0, 0, 1.5), (10, 0, 0, 1.5)),
            battery=battery(
                initial_kwh=0, minimum_soc_fraction=0.1, allow_grid_charge=False
            ),
        ),
        minimum_mode_minutes=dwell,
    )
    plan = solve(source)
    assert plan.flows[0].charge_kwh == pytest.approx(2)
    assert sum(f.discharge_kwh for f in plan.flows) == pytest.approx(1)
    assert plan.flows[-1].end_soc_kwh == pytest.approx(1)
    assert all(f.end_soc_kwh >= 1 - 1e-6 for f in plan.flows)


@pytest.mark.parametrize("mode", ["SELF_CONSUME", "DISCHARGE_GRID"])
def test_low_soc_interrupts_carried_discharge_commitment(mode):
    source = problem(
        ((5, 0, 0, 1),) * 2,
        battery=battery(
            initial_kwh=0, minimum_soc_fraction=0.1, allow_grid_charge=False
        ),
    )
    source = replace(
        source,
        initial_dispatch_mode=mode,
        initial_dispatch_mode_since=source.slots[0].start - timedelta(minutes=20),
    )
    plan = solve(source)
    assert all(f.dispatch_mode == "HOLD" for f in plan.flows)
    exception = safety_exception(source)
    if mode == "SELF_CONSUME":
        # A PV-follow commitment at the reserve is released, not interrupted (T-058).
        assert exception is None
    else:
        assert exception["reason"] == "observed_soc_minimum"


@pytest.mark.parametrize("initial", [-0.01, float("nan"), float("inf"), 8.1])
def test_invalid_initial_energy_still_rejected(initial):
    with pytest.raises(InputError):
        solve(problem(((1, 0, 0, 1),), battery=battery(initial_kwh=initial)))


@pytest.mark.parametrize("soc", [0, 5, 10])
def test_runtime_publishes_plan_with_real_low_soc(soc):
    now = datetime(2026, 9, 20, tzinfo=UTC)
    config = battery_configuration()
    config["settings"].update(
        operating_floor=10, hardware_floor=5, allow_grid_charge=False
    )
    states = {"sensor.soc": {"state": str(soc), "attributes": {}, "last_updated": now}}
    result = compute(config, states, now)
    assert result["status"] == "ready"
    assert result["valid"]
    assert result["intervals"]
    for row in result["intervals"]:
        assert row["end_soc_kwh"] == pytest.approx(soc * 0.2)
        assert row["discharge_kwh"] == pytest.approx(0)
