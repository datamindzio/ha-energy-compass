"""`benchmarks.optimizer` reports what docs/runtime-validation.md quotes.

The money cost of a plan (`objective`) and the weighted objective the solver minimises
(`weighted_objective`, bounded by `mip_gap`) are different quantities; the benchmark
reports both, CPU time, and, for a time-limited plan, the difference to its proof.
"""

from dataclasses import replace

import pytest

from benchmarks.optimizer import (
    _accepted,
    _compare_to_proof,
    _plan_difference,
    _solve_once,
    make_problem,
    run_one,
)


def _run(strategy="cost_min", slots=48, time_limit_s=30.0, **options):
    return run_one(
        strategy,
        slots=slots,
        time_limit_s=time_limit_s,
        battery_export=False,
        probes=0,
        balance=False,
        **options,
    )


def test_run_reports_money_and_weighted_objective_and_cpu_time():
    outcome = _run()
    assert outcome["result"] == "optimal"
    assert outcome["time_limited"] is False
    assert outcome["mip_gap"] is not None
    assert isinstance(outcome["objective"], float)
    assert isinstance(outcome["weighted_objective"], float)
    assert outcome["cpu_s"] > 0
    assert outcome["variable_count"] > 0
    assert outcome["row_count"] > 0
    assert "proven" not in outcome
    assert _accepted(outcome)


def test_accept_incumbent_with_no_incumbent_is_a_timeout_without_a_plan():
    outcome = _run(slots=192, time_limit_s=0.000001, accept_incumbent=True)
    assert outcome["result"] == "timeout"
    assert outcome["accept_incumbent"] is True
    assert outcome["weighted_objective"] is None
    assert "objective" not in outcome
    assert not _accepted(outcome)


def test_a_time_limited_result_is_accepted_like_the_runtime_accepts_it():
    outcome = _run()
    outcome.update(result="time_limited", time_limited=True)
    assert _accepted(outcome)


def test_plan_difference_counts_the_changed_intervals():
    plan, _ = _solve_once(make_problem(48), time_limit_s=30.0, accept_incumbent=False)
    assert plan is not None
    same = _plan_difference(plan, plan)
    assert same == {
        "intervals_changed": 0,
        "grid_difference_kwh": 0.0,
        "end_soc_difference_kwh": 0.0,
    }
    first = plan.flows[0]
    moved = replace(
        plan,
        flows=(
            replace(first, grid_import_kwh=first.grid_import_kwh + 0.5),
            *plan.flows[1:],
        ),
    )
    changed = _plan_difference(moved, plan)
    assert changed["intervals_changed"] == 1
    assert changed["grid_difference_kwh"] == pytest.approx(0.5)


def test_compare_to_proof_reports_the_excess_over_the_proven_weighted_objective():
    problem = make_problem(48)
    plan, outcome = _solve_once(problem, time_limit_s=30.0, accept_incumbent=False)
    assert plan is not None
    worse = {**outcome, "weighted_objective": outcome["weighted_objective"] + 1.0}
    comparison = _compare_to_proof(problem, plan, worse, 30.0)
    assert comparison["result"] == "optimal"
    assert comparison["weighted_objective"] == pytest.approx(
        outcome["weighted_objective"]
    )
    assert comparison["weighted_excess"] == pytest.approx(1.0)
    # Relative to the incumbent, the way HiGHS reports `mip_gap`.
    assert comparison["weighted_excess_relative"] == pytest.approx(
        1.0 / abs(worse["weighted_objective"])
    )
    assert comparison["objective_difference"] == pytest.approx(0.0, abs=1e-6)
    assert comparison["intervals_changed"] == 0
