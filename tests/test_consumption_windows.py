"""Consumption probes under the load-following rule (T-027).

A full solve that times out hands the probes to windows: the operating modes
stay those of the base plan except near the probed intervals.
"""

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


def _settings(hours, interval=60, **extra):
    return CompassSettings(
        display_horizon_hours=hours,
        reference_horizon_hours=hours,
        display_interval_minutes=interval,
        **extra,
    )


def _times_out_in_full(monkeypatch):
    """Make every solve that is not pinned to the base plan hit its time limit."""
    original = consumption.solve

    def solve_(problem, **kwargs):
        if kwargs.get("pinned_to") is None:
            raise SolveError("timeout")
        return original(problem, **kwargs)

    monkeypatch.setattr(consumption, "solve", solve_)


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


def test_the_full_solve_stays_the_probe_while_it_finishes(monkeypatch):
    problem = _problem(48, 60, "cost_min")
    base = solve(problem)
    original = consumption.solve
    pinned = []

    def spy(problem_, **kwargs):
        pinned.append(kwargs.get("pinned_to"))
        return original(problem_, **kwargs)

    monkeypatch.setattr(consumption, "solve", spy)
    result = analyze_consumption(problem, base, settings=_settings(12))
    assert pinned and all(plan is None for plan in pinned)
    assert all(item.cost_per_kwh is not None for item in result.opportunities)


@pytest.mark.parametrize(
    "strategy,count,minutes,hours",
    [
        ("cost_min", 96, 60, 24),
        ("cost_min", 96, 0, 24),
        ("pv_swap", 96, 60, 24),
        # the load-following full solve costs 4-5 s per probe on a loaded host:
        # the first six hours of the 48 intervals keep the case under CI's 45 s
        ("self_sufficiency", 48, 60, 6),
        ("self_sufficiency", 48, 0, 6),
        ("backup_ready", 48, 60, 12),
    ],
)
def test_windowed_probes_equal_the_full_solve(
    monkeypatch, strategy, count, minutes, hours
):
    """Where the full solve finishes, the widest window gives its cost and level.

    One hourly probe per hour: the dearest hours of the day need a window of 16
    intervals or more, so a probe that stopped at the first window that works
    would differ here (by up to 1.3 PLN/kWh).
    """
    problem = _problem(count, minutes, strategy)
    base = solve(problem, time_limit_s=30)
    settings = _settings(hours, probe_time_limit_s=30)
    full = analyze_consumption(problem, base, settings=settings, budget_s=300)
    _times_out_in_full(monkeypatch)
    windowed = analyze_consumption(problem, base, settings=settings, budget_s=300)
    assert len(full.opportunities) == hours
    assert all(item.cost_per_kwh is not None for item in full.opportunities)
    for exact, near in zip(full.opportunities, windowed.opportunities, strict=True):
        assert near.start == exact.start
        assert near.cost_per_kwh == pytest.approx(exact.cost_per_kwh, abs=1e-4)
        assert near.level == exact.level
    assert windowed.classification_mode == full.classification_mode


@pytest.mark.parametrize("strategy", ["self_sufficiency", "backup_ready"])
@pytest.mark.parametrize("minutes", [60, 0])
def test_load_following_probes_finish_at_96_intervals(strategy, minutes):
    problem = _problem(96, minutes, strategy)
    base = solve(problem, time_limit_s=3, accept_incumbent=True)
    started = perf_counter()
    result = analyze_consumption(problem, base, settings=_settings(12), budget_s=30)
    assert perf_counter() - started < 40
    assert result.reference_complete
    assert result.classification_mode == "percentile"
    assert all(item.cost_per_kwh is not None for item in result.opportunities)


class _Windows:
    """A stand-in solver: the full solve times out, a window costs `by_window`."""

    def __init__(self, source, baseline, by_window, clock=None, step_s=0.0):
        self.source = source
        self.baseline = baseline
        self.by_window = by_window
        self.clock = clock
        self.step_s = step_s
        self.full = []
        self.windows = []
        self.references = []

    def __call__(self, problem, *, time_limit_s, pinned_to=None, unpinned_slots=None):
        if self.clock is not None:
            self.clock[0] += self.step_s
        if pinned_to is None:
            self.full.append(time_limit_s)
            raise SolveError("timeout")
        if problem is self.source:
            self.references.append(unpinned_slots)
            return replace(self.baseline, grid_cost=self.baseline.grid_cost - 0.10)
        size = len(unpinned_slots)
        self.windows.append(size)
        outcome = self.by_window[size]
        if isinstance(outcome, SolveError):
            raise outcome
        actual = sum(
            slot.load_kwh - original.load_kwh
            for slot, original in zip(problem.slots, self.source.slots, strict=True)
        )
        return replace(
            self.baseline, grid_cost=self.baseline.grid_cost + outcome * actual
        )


def _stubbed(monkeypatch, by_window, *, clock=None, step_s=0.0, **plan):
    source = _problem(24, 60, "cost_min")
    baseline = replace(solve(source), **plan)
    stub = _Windows(source, baseline, by_window, clock, step_s)
    monkeypatch.setattr(consumption, "solve", stub)
    if clock is not None:
        monkeypatch.setattr(consumption, "perf_counter", lambda: clock[0])
    return source, baseline, stub


# a probed hour is the first four of 24 intervals: margins 0, 4, 16 and 48 leave
# 4, 8, 20 and 24 of them free
_COSTS = {4: 1.75, 8: 0.70, 12: 0.70, 20: 0.45, 24: 0.45}


def test_widening_goes_on_after_the_first_window_that_works(monkeypatch):
    source, baseline, stub = _stubbed(monkeypatch, _COSTS)
    result = analyze_consumption(source, baseline, settings=_settings(1))
    assert stub.windows == [4, 8, 20, 24]
    assert result.opportunities[0].cost_per_kwh == pytest.approx(0.45)


def test_an_infeasible_window_widens_instead_of_failing_the_probe(monkeypatch):
    infeasible = SolveError("infeasible")
    source, baseline, stub = _stubbed(
        monkeypatch, {4: infeasible, 8: infeasible, 20: 0.45, 24: 0.45}
    )
    result = analyze_consumption(source, baseline, settings=_settings(1))
    assert stub.windows == [4, 8, 20, 24]
    assert result.opportunities[0].cost_per_kwh == pytest.approx(0.45)


def test_a_window_that_times_out_keeps_the_last_one_that_finished(monkeypatch):
    source, baseline, stub = _stubbed(
        monkeypatch, {4: 1.75, 8: 0.70, 20: SolveError("timeout"), 24: 0.45}
    )
    result = analyze_consumption(source, baseline, settings=_settings(1))
    assert stub.windows == [4, 8, 20]
    assert result.opportunities[0].cost_per_kwh == pytest.approx(0.70)


def test_every_window_infeasible_leaves_the_probe_unknown(monkeypatch):
    infeasible = SolveError("infeasible")
    source, baseline, stub = _stubbed(
        monkeypatch, {4: infeasible, 8: infeasible, 20: infeasible, 24: infeasible}
    )
    result = analyze_consumption(source, baseline, settings=_settings(1))
    assert stub.windows == [4, 8, 20, 24]
    assert result.opportunities[0].cost_per_kwh is None


def test_one_timed_out_full_solve_sends_the_later_probes_to_windows(monkeypatch):
    source, baseline, stub = _stubbed(monkeypatch, _COSTS)
    result = analyze_consumption(source, baseline, settings=_settings(3))
    assert len(stub.full) == 1
    assert len(stub.windows) == 3 * 4
    assert all(
        item.cost_per_kwh == pytest.approx(0.45) for item in result.opportunities
    )


def test_a_full_solve_that_fails_otherwise_does_not_start_windows(monkeypatch):
    source = _problem(24, 60, "cost_min")
    baseline = solve(source)
    calls = []

    def infeasible(problem, **kwargs):
        calls.append(kwargs.get("pinned_to"))
        raise SolveError("infeasible")

    monkeypatch.setattr(consumption, "solve", infeasible)
    result = analyze_consumption(source, baseline, settings=_settings(1))
    assert calls == [None]
    assert result.opportunities[0].cost_per_kwh is None


@pytest.mark.parametrize(
    "step_s,windows",
    [
        # a 0.5 s window predicts 2 s for the next, more than the 1.5 s left
        (0.5, [4]),
        (0.1, [4, 8, 20, 24]),
    ],
)
def test_widening_stops_when_the_next_window_cannot_fit(monkeypatch, step_s, windows):
    clock = [0.0]
    source, baseline, stub = _stubbed(monkeypatch, _COSTS, clock=clock, step_s=step_s)
    analyze_consumption(
        source, baseline, settings=_settings(1, probe_time_limit_s=2.0), budget_s=50
    )
    assert stub.windows == windows


def test_an_incumbent_base_plan_is_compared_within_the_same_window(monkeypatch):
    source, baseline, stub = _stubbed(monkeypatch, _COSTS, time_limited=True)
    result = analyze_consumption(source, baseline, settings=_settings(1))
    assert stub.references == [frozenset(range(size)) for size in (4, 8, 20, 24)]
    # the reference solve costs 0.10 less than the incumbent: cost is measured from it
    assert result.opportunities[0].cost_per_kwh == pytest.approx(0.45 + 0.10)
