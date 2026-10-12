"""Consumption probes keep the base plan's operating modes (T-027)."""

import sys
from dataclasses import replace
from pathlib import Path
from time import perf_counter

import pytest

from custom_components.energy_compass.engine import consumption
from custom_components.energy_compass.engine.consumption import analyze_consumption
from custom_components.energy_compass.engine.models import (
    CompassSettings,
    InputError,
    SolveError,
    plan_monetary_cost,
)
from custom_components.energy_compass.engine.optimize import solve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.optimizer import make_problem


def _problem(count=48, minutes=60, strategy="self_sufficiency"):
    return replace(make_problem(count, strategy=strategy), minimum_mode_minutes=minutes)


def _loaded(problem, indexes, extra):
    return replace(
        problem,
        slots=tuple(
            replace(slot, load_kwh=slot.load_kwh + (extra if i in indexes else 0))
            for i, slot in enumerate(problem.slots)
        ),
    )


@pytest.mark.parametrize("minutes", [60, 0])
def test_pinned_solve_reproduces_the_base_plan_cost(minutes):
    problem = _problem(minutes=minutes)
    base = solve(problem)
    pinned = solve(problem, pinned_to=base)
    assert plan_monetary_cost(pinned) == pytest.approx(plan_monetary_cost(base))
    assert [f.dispatch_mode for f in pinned.flows] == [
        f.dispatch_mode for f in base.flows
    ]


def test_pinned_solve_rejects_a_plan_of_another_length():
    problem = _problem()
    base = solve(_problem(24))
    with pytest.raises(InputError, match="pinned plan"):
        solve(problem, pinned_to=base)


@pytest.mark.parametrize("minutes", [60, 0])
def test_pinned_probe_never_undercuts_the_full_solve(minutes):
    problem = _problem(minutes=minutes)
    base = solve(problem)
    loaded = _loaded(problem, {4, 5, 6, 7}, 0.25)
    full = plan_monetary_cost(solve(loaded))
    pinned = plan_monetary_cost(
        solve(
            loaded,
            pinned_to=base,
            unpinned_slots=frozenset({4, 5, 6, 7}),
        )
    )
    assert pinned >= full - 1e-7
    assert pinned - plan_monetary_cost(base) < 4 * 0.85 * 0.25 + 1e-7


def test_probe_falls_back_to_the_full_solve_when_no_pinned_window_works(monkeypatch):
    problem = _problem()
    base = solve(problem)
    original = consumption.solve
    pinned_calls = []

    def stub(problem_, **kwargs):
        if kwargs.get("pinned_to") is not None:
            pinned_calls.append(kwargs.get("unpinned_slots"))
            if "unpinned_slots" in kwargs:
                raise SolveError("infeasible")
        return original(problem_, **kwargs)

    monkeypatch.setattr(consumption, "solve", stub)
    settings = CompassSettings(display_horizon_hours=1, reference_horizon_hours=1)
    result = analyze_consumption(problem, base, settings=settings)
    assert len([c for c in pinned_calls if c is not None]) == len(
        consumption._UNPIN_MARGINS
    ) * len(result.opportunities)
    expected = plan_monetary_cost(solve(_loaded(problem, {0, 1, 2, 3}, 0.25)))
    expected -= plan_monetary_cost(base)
    assert result.opportunities[0].cost_per_kwh == pytest.approx(expected)


@pytest.mark.parametrize("strategy", ["self_sufficiency", "backup_ready"])
@pytest.mark.parametrize("minutes", [60, 0])
def test_load_following_probes_finish_at_96_intervals(strategy, minutes):
    problem = _problem(96, minutes, strategy)
    base = solve(problem, time_limit_s=30)
    settings = CompassSettings(
        display_horizon_hours=24,
        reference_horizon_hours=24,
        display_interval_minutes=60,
    )
    started = perf_counter()
    result = analyze_consumption(problem, base, settings=settings, budget_s=60)
    assert perf_counter() - started < 60
    assert result.reference_complete
    assert result.classification_mode == "percentile"
    assert all(item.cost_per_kwh is not None for item in result.opportunities)
