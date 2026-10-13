"""A flexible-load level with no solution on the released problem is priced on a later rung (T-067)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine import consumption
from custom_components.energy_compass.engine.consumption import analyze_flexible_loads
from custom_components.energy_compass.engine.models import (
    Battery,
    CompassSettings,
    FlexibleLoadRequest,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
    plan_monetary_cost,
)
from custom_components.energy_compass.engine.optimize import (
    later_rungs,
    solve,
    solve_flexible_load,
    solve_released,
)

START = datetime(2026, 12, 18, 11, tzinfo=UTC)
QUARTER = timedelta(minutes=15)
# (buy, sell, pv, load): curtail_sweep.py `deprobe` seed 44, draw 10.
SLOTS = (
    (1.23, 0.55, 1.5, 0.05),
    (0.68, 0.57, 0.6, 0.4),
    (0.73, 0.11, 1.5, 0.05),
    (1.42, 0.35, 1.5, 0.02),
    (1.47, 0.69, 1.5, 0.4),
    (0.81, 0.52, 1.5, 0.02),
)


def edge_problem() -> Problem:
    """A SELF_CONSUME commitment released at the reserve whose base plan fills the pack just before a CURTAIL.

    The plan is CHARGE_PV five times, then CURTAIL. The model lets a probe stand where the
    pack is full only by the largest draw the probe may make in the next interval, so a
    2 kW load of 0.25 kWh has no solution on this problem, anywhere in the horizon.
    """
    slots = tuple(
        Slot(START + i * QUARTER, START + (i + 1) * QUARTER, buy, sell, pv, load)
        for i, (buy, sell, pv, load) in enumerate(SLOTS)
    )
    return Problem(
        slots,
        SiteLimits(8, 8, 2, True),
        Battery(5, 0.1, 1, 0.5, 4, 1, 0.95, 0.95, 0, True, False, 0),
        "preserve_initial",
        0.3,
        (),
        "UTC",
        minimum_mode_minutes=120,
        minimum_mode_power_kw=0.1,
        initial_dispatch_mode="SELF_CONSUME",
        initial_dispatch_mode_since=START - timedelta(minutes=1),
    )


def test_the_level_has_no_solution_on_the_released_problem_and_one_on_the_next_rung():
    plan, problem = solve_released(edge_problem())
    assert [flow.dispatch_mode for flow in plan.flows][-1] == "CURTAIL"
    request = FlexibleLoadRequest(0.25, 2)
    with pytest.raises(SolveError) as error:
        solve_flexible_load(problem, request)
    assert error.value.reason == "infeasible"
    rungs = later_rungs(problem)
    assert [rung.initial_dispatch_reserve_hold for rung in rungs] == [True, False]
    assert solve_flexible_load(rungs[0], request).plan


def test_the_later_rungs_are_those_a_released_solve_would_take():
    assert later_rungs(replace(edge_problem(), initial_dispatch_mode=None)) == ()
    _, problem = solve_released(edge_problem())
    held, free = later_rungs(problem)
    assert free.initial_dispatch_mode is None
    # The ladder goes on from the HOLD with the commitment's clock, and ends without it.
    assert later_rungs(held) == (free,)
    assert later_rungs(free) == ()


def test_a_level_without_a_solution_is_priced_against_the_plan_of_the_rung_it_is_solved_on(
    monkeypatch,
):
    monkeypatch.setattr(consumption, "_FLEXIBLE_LOAD_TARGETS", (0.25, 0.5))
    plan, problem = solve_released(edge_problem())
    settings = CompassSettings(flexible_load_max_power_kw=2)

    result = analyze_flexible_loads(problem, plan, settings=settings)

    first = result.profiles[0]
    assert first.price_status == "ANCHOR"
    assert first.reason is None
    rung = later_rungs(problem)[0]
    expected = plan_monetary_cost(
        solve_flexible_load(rung, FlexibleLoadRequest(0.25, 2)).plan
    ) - plan_monetary_cost(solve(rung))
    assert first.incremental_cost == pytest.approx(expected, abs=1e-6)
    assert result.anchor_price_per_kwh == pytest.approx(expected / 0.25, abs=1e-5)


def test_levels_on_different_rungs_have_no_block_cost(monkeypatch):
    # 0.25 kWh has a solution on the second rung only, 0.5 kWh on the problem itself:
    # their costs are against different plans, so the block between them is unknown.
    monkeypatch.setattr(consumption, "_FLEXIBLE_LOAD_TARGETS", (0.25, 0.5))
    plan, problem = solve_released(edge_problem())
    solve_flexible_load(problem, FlexibleLoadRequest(0.5, 2))

    result = analyze_flexible_loads(
        problem, plan, settings=CompassSettings(flexible_load_max_power_kw=2)
    )

    second = result.profiles[1]
    assert second.incremental_cost == pytest.approx(
        plan_monetary_cost(
            solve_flexible_load(problem, FlexibleLoadRequest(0.5, 2)).plan
        )
        - plan_monetary_cost(plan),
        abs=1e-6,
    )
    assert second.block_cost_per_kwh is None
    assert second.price_status == "UNKNOWN"


def test_a_level_without_a_solution_on_any_rung_stays_infeasible(monkeypatch):
    monkeypatch.setattr(consumption, "_FLEXIBLE_LOAD_TARGETS", (0.25,))
    plan, problem = solve_released(edge_problem())

    def refuse(candidate, request, **_):
        raise SolveError("infeasible", "no plan")

    monkeypatch.setattr(consumption, "solve_flexible_load", refuse)
    result = analyze_flexible_loads(
        problem, plan, settings=CompassSettings(flexible_load_max_power_kw=2)
    )

    assert result.profiles[0].price_status == "UNKNOWN"
    assert result.profiles[0].reason == "infeasible"
