"""HiGHS's presolve called a mode-constrained model infeasible that has a plan (T-074).

A site that cannot curtail, with no PV above its export cap or inverter rating, has the
`CHARGE_PV` commitment it carried over at 12:00 (its 60 minutes ended at 11:59), a pack one
standby loss below full and the `preserve_initial` rule that leaves the do-nothing trajectory
as the only way through: a plan of all HOLD. The model is feasible and HiGHS's presolve said it
was not; without presolve it returns that plan (rolled sweep `negroll` seed 12, problem 205,
fifth plan). The fallback that solves such a model again without presolve belongs to every
model with mode rows, not only to the forced-spill ones.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine import optimize
from custom_components.energy_compass.engine.models import (
    Battery,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import solve, solve_released

START = datetime(2026, 12, 18, 12, tzinfo=UTC)
QUARTER = timedelta(minutes=15)

# (buy, sell, pv, load) per quarter hour, 12:00 to 14:00.
ROWS = (
    (0.74, -0.46, 0.29, 0.05),
    (0.41, -0.47, 0.33, 0.2),
    (1.22, 0.79, 0.27, 0.02),
    (0.8, 0.47, 0.24, 0.2),
    (0.48, -0.57, 0.3, 0.05),
    (0.72, 0.12, 0.25, 0.4),
    (1.06, -0.55, 0.27, 0.4),
    (1.12, 0.02, 0.36, 0.4),
    (0.3, 0.66, 0.0, 0.2),
)


def state(rows=ROWS) -> Problem:
    return Problem(
        tuple(
            Slot(START + i * QUARTER, START + (i + 1) * QUARTER, *row)
            for i, row in enumerate(rows)
        ),
        SiteLimits(3, 8, 2, False),
        Battery(10, 0, 1, 9.995, 3, 3, 0.95, 0.95, 0, False, True, 0.02),
        "preserve_initial",
        0,
        (),
        "UTC",
        minimum_mode_minutes=60,
        initial_dispatch_mode="CHARGE_PV",
        initial_dispatch_mode_since=START - timedelta(minutes=61),
        minimum_export_episode_benefit=0.0,
    )


def counted(monkeypatch):
    runs = []
    run = optimize._Model._run

    def record(self, matrix, time_limit_s, **options):
        runs.append(options)
        return run(self, matrix, time_limit_s, **options)

    monkeypatch.setattr(optimize._Model, "_run", record)
    return runs


def test_pack_one_standby_loss_below_full_after_a_carried_charge_pv_has_its_plan(
    monkeypatch,
):
    runs = counted(monkeypatch)
    plan = solve(state())
    # The pack cannot take the 0.005 kWh it lost (below the minimum activity) and may not
    # spend any of it: the plan is the do-nothing trajectory.
    assert [flow.dispatch_mode for flow in plan.flows] == ["HOLD"] * len(ROWS)
    assert plan.flows[-1].end_soc_kwh == pytest.approx(9.995 - 0.005 * len(ROWS))
    # Solved once where the solver is sound; twice, the second without presolve, where not.
    assert runs in ([{}], [{}, {"presolve": False}])


def test_the_runtime_entry_point_plans_it_without_a_fallback_rung():
    plan, solved_for = solve_released(state())
    assert solved_for == state()
    assert {flow.dispatch_mode for flow in plan.flows} == {"HOLD"}


@pytest.mark.parametrize("count", [7, 8, 9])
def test_the_horizon_does_not_decide_it(count):
    # From seven intervals HiGHS was sound, from eight it was not: the plan is the same.
    plan = solve(state(ROWS[:count]))
    assert [flow.dispatch_mode for flow in plan.flows] == ["HOLD"] * count


def test_a_model_with_mode_rows_that_is_infeasible_is_still_reported(monkeypatch):
    # A load above what the grid and the pack deliver has no plan: it is solved again
    # without presolve, and both runs say so.
    runs = counted(monkeypatch)
    rows = tuple((buy, sell, pv, 10.0) for buy, sell, pv, _ in ROWS)
    with pytest.raises(SolveError) as error:
        solve(
            replace(
                state(rows),
                initial_dispatch_mode=None,
                initial_dispatch_mode_since=None,
            )
        )
    assert error.value.reason == "infeasible"
    assert runs == [{}, {"presolve": False}]
