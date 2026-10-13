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


SETTINGS = CompassSettings(flexible_load_max_power_kw=2)


def fail_on(monkeypatch, name, reserve_hold=None, free=False, reason="timeout"):
    """Make `consumption.<name>` raise `reason` on one rung, the real function elsewhere."""
    real = getattr(consumption, name)

    def solver(candidate, *args, **kwargs):
        if (reserve_hold and candidate.initial_dispatch_reserve_hold) or (
            free and candidate.initial_dispatch_mode is None
        ):
            raise SolveError(reason, "refused")
        return real(candidate, *args, **kwargs)

    monkeypatch.setattr(consumption, name, solver)


def test_a_rung_without_a_base_plan_is_skipped_and_the_next_one_prices_the_level(
    monkeypatch,
):
    monkeypatch.setattr(consumption, "_FLEXIBLE_LOAD_TARGETS", (0.25,))
    plan, problem = solve_released(edge_problem())
    fail_on(monkeypatch, "solve", reserve_hold=True, reason="infeasible")

    result = analyze_flexible_loads(problem, plan, settings=SETTINGS)

    free = later_rungs(problem)[1]
    expected = plan_monetary_cost(
        solve_flexible_load(free, FlexibleLoadRequest(0.25, 2)).plan
    ) - plan_monetary_cost(solve(free))
    assert result.profiles[0].price_status == "ANCHOR"
    assert result.profiles[0].incremental_cost == pytest.approx(expected, abs=1e-6)


def test_a_level_whose_every_rung_is_without_a_base_plan_is_infeasible(monkeypatch):
    monkeypatch.setattr(consumption, "_FLEXIBLE_LOAD_TARGETS", (0.25,))
    plan, problem = solve_released(edge_problem())
    fail_on(monkeypatch, "solve", reserve_hold=True, free=True, reason="infeasible")

    result = analyze_flexible_loads(problem, plan, settings=SETTINGS)

    assert result.profiles[0].price_status == "UNKNOWN"
    assert result.profiles[0].reason == "infeasible"


def test_a_timeout_on_a_later_rung_is_what_a_level_is_published_with(monkeypatch):
    # Infeasible on the problem itself, then the second rung's probe times out: the
    # ladder was not finished, so the level is a timeout, not "no solution", and the
    # third rung is not tried.
    monkeypatch.setattr(consumption, "_FLEXIBLE_LOAD_TARGETS", (0.25,))
    plan, problem = solve_released(edge_problem())
    fail_on(monkeypatch, "solve_flexible_load", reserve_hold=True)
    tried = []
    real = consumption.solve_flexible_load
    monkeypatch.setattr(
        consumption,
        "solve_flexible_load",
        lambda candidate, *args, **kwargs: (
            tried.append(candidate) or real(candidate, *args, **kwargs)
        ),
    )

    result = analyze_flexible_loads(problem, plan, settings=SETTINGS)

    assert result.profiles[0].price_status == "UNKNOWN"
    assert result.profiles[0].reason == "timeout"
    assert all(candidate.initial_dispatch_mode for candidate in tried)


def test_another_solver_error_on_a_later_rung_ends_the_level_with_its_reason(
    monkeypatch,
):
    monkeypatch.setattr(consumption, "_FLEXIBLE_LOAD_TARGETS", (0.25,))
    plan, problem = solve_released(edge_problem())
    fail_on(
        monkeypatch, "solve_flexible_load", reserve_hold=True, reason="solver_failure"
    )

    result = analyze_flexible_loads(problem, plan, settings=SETTINGS)

    assert result.profiles[0].reason == "solver_failure"


def test_a_base_plan_that_times_out_on_a_rung_ends_the_level_as_a_timeout(monkeypatch):
    monkeypatch.setattr(consumption, "_FLEXIBLE_LOAD_TARGETS", (0.25,))
    plan, problem = solve_released(edge_problem())
    fail_on(monkeypatch, "solve", reserve_hold=True)

    result = analyze_flexible_loads(problem, plan, settings=SETTINGS)

    assert result.profiles[0].price_status == "UNKNOWN"
    assert result.profiles[0].reason == "timeout"


def test_a_base_plan_that_lands_past_the_deadline_leaves_no_probe_to_start(monkeypatch):
    # The rung's base plan is solved on what is left of the budget and may return
    # after it is gone: the level is a timeout, and the probe is never given a
    # limit of zero (an InputError the coordinator would answer by dropping the plan).
    monkeypatch.setattr(consumption, "_FLEXIBLE_LOAD_TARGETS", (0.25,))
    plan, problem = solve_released(edge_problem())
    clock = [0.0]
    monkeypatch.setattr(consumption, "perf_counter", lambda: clock[0])
    real_solve = consumption.solve
    real_probe = consumption.solve_flexible_load
    limits = []

    def slow_solve(candidate, **kwargs):
        result = real_solve(candidate, **kwargs)
        clock[0] += 10
        return result

    def probe(candidate, request, **kwargs):
        limits.append(kwargs["time_limit_s"])
        return real_probe(candidate, request, **kwargs)

    monkeypatch.setattr(consumption, "solve", slow_solve)
    monkeypatch.setattr(consumption, "solve_flexible_load", probe)

    result = analyze_flexible_loads(problem, plan, settings=SETTINGS, budget_s=5)

    assert result.profiles[0].price_status == "UNKNOWN"
    assert result.profiles[0].reason == "timeout"
    assert len(limits) == 1
    assert all(limit > 0 for limit in limits)


def test_a_level_on_another_rung_than_the_anchor_is_not_judged_against_it(
    monkeypatch,
):
    # The anchor (0.25 kWh) is priced on the second rung, the other two levels on the
    # problem itself. The third has a block cost (against the second, same rung) and
    # still no verdict: the allowed price it would be held to is of another problem.
    monkeypatch.setattr(consumption, "_FLEXIBLE_LOAD_TARGETS", (0.25, 0.5, 0.75))
    plan, problem = solve_released(edge_problem())

    result = analyze_flexible_loads(problem, plan, settings=SETTINGS)

    assert [profile.price_status for profile in result.profiles] == [
        "ANCHOR",
        "UNKNOWN",
        "UNKNOWN",
    ]
    assert result.profiles[1].block_cost_per_kwh is None
    assert result.profiles[2].block_cost_per_kwh is not None
    assert result.depth_kwh == 0.25
