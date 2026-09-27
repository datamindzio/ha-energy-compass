"""Solver-level tests for the strategy weights, autonomy floor, peak and soft caps.

`export_benefit.py` is untouched by Step 5; its tests live in `tests/test_export_benefit.py`.
`tests/test_strategy_golden.py` is the cost_min contract and is exercised separately.
"""

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.energy_compass.engine import optimize
from custom_components.energy_compass.engine.models import (
    Battery,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
    plan_monetary_cost,
)
from custom_components.energy_compass.engine.optimize import solve

_GOLDEN_DIR = Path(__file__).parent / "golden"
sys.path.insert(0, str(_GOLDEN_DIR))

from fixtures import golden_problems


def problem(
    rows,
    *,
    battery=None,
    site=None,
    mode="preserve_initial",
    terminal_value=0.0,
    budgets=(),
    start=None,
    timezone_name="UTC",
    **overrides,
):
    start = start or datetime(2026, 9, 17, tzinfo=UTC)
    slots = tuple(
        Slot(
            start + timedelta(hours=index),
            start + timedelta(hours=index + 1),
            buy,
            sell,
            pv,
            load,
        )
        for index, (buy, sell, pv, load) in enumerate(rows)
    )
    return Problem(
        slots,
        site or SiteLimits(8, 8, 8, True),
        battery,
        mode,
        terminal_value,
        budgets,
        timezone_name,
        **overrides,
    )


def battery(**changes):
    return replace(Battery(10, 0.2, 0.8, 5, 4, 4, 1, 1, 0.1, True, True), **changes)


def _capture_cost(monkeypatch):
    """Spy on `_Model.solve` (the Step 0 mechanism); returns the captured cost list."""
    captured = {}
    original_solve = optimize._Model.solve

    def spy(self, time_limit_s):
        captured["cost"] = list(self.cost)
        return original_solve(self, time_limit_s)

    monkeypatch.setattr(optimize._Model, "solve", spy)
    return captured


def _reduce_variable(monkeypatch, cost_value):
    """Solve normally, then understate the one variable with the given (unique) cost."""
    original_solve = optimize._Model.solve

    def spy(self, time_limit_s):
        result = original_solve(self, time_limit_s)
        index = self.cost.index(cost_value)
        corrupted = result.copy()
        corrupted[index] = max(0.0, corrupted[index] - 10 * optimize._TOL)
        return corrupted

    monkeypatch.setattr(optimize._Model, "solve", spy)


def test_cost_min_creates_no_strategy_variables(monkeypatch):
    captured = _capture_cost(monkeypatch)
    solve(golden_problems()["battery_24h"])
    snapshot = json.loads((_GOLDEN_DIR / "cost_min.json").read_text())
    assert len(captured["cost"]) == len(snapshot["battery_24h"]["cost"])


def test_import_kwh_weight_avoids_import_at_negative_price():
    rows = [(-0.50, 0.0, 0.0, 0.0)]
    b = battery()
    baseline = solve(problem(rows, battery=b))
    assert baseline.flows[0].grid_import_kwh > 0.5

    weighted = solve(problem(rows, battery=b, import_kwh_weight=1.0))
    assert weighted.flows[0].grid_import_kwh == pytest.approx(0.0, abs=1e-6)


def test_import_penalty_prefers_self_consumption_over_a_marginal_hold():
    """A battery held for a later export only slightly above the buy price
    loses to covering the house once import carries a small shadow price."""
    rows = [(1.25, 0.70, 0.0, 1.0), (1.25, 1.35, 0.0, 0.0)]
    b = battery(initial_kwh=3.0, wear_per_kwh=0.0, allow_grid_charge=False)
    source = problem(
        rows,
        battery=b,
        mode="value",
        limit_export_to_pv=False,
        minimum_export_episode_benefit=0.0,
    )
    baseline = solve(source)
    assert baseline.flows[0].dispatch_mode == "HOLD"
    assert baseline.flows[0].grid_import_kwh == pytest.approx(1.0)
    penalised = solve(replace(source, import_kwh_weight=0.15))
    assert penalised.flows[0].dispatch_mode == "SELF_CONSUME"
    assert penalised.flows[0].grid_import_kwh == pytest.approx(0.0, abs=1e-6)
    # The reported cost stays real currency: the penalty never enters it.
    assert plan_monetary_cost(baseline) == pytest.approx(-0.10)
    assert plan_monetary_cost(penalised) == pytest.approx(0.0, abs=1e-6)


def test_pv_export_margin_reduces_export_value(monkeypatch):
    rows = [(0.30, 0.20, 1.0, 0.5)]
    captured = _capture_cost(monkeypatch)
    solve(problem(rows, pv_export_margin=0.05))
    assert captured["cost"][1] == pytest.approx(-(0.20 - 0.05))


def test_battery_export_penalty_does_not_penalise_direct_pv_export(monkeypatch):
    rows = [(0.30, 0.20, 2.0, 0.5)]
    captured = _capture_cost(monkeypatch)
    solve(problem(rows, battery=battery(), battery_export_penalty_per_kwh=3.0))
    # Variable order for one battery slot: gin, gout, curt, pv_load, pv_grid,
    # grid_load, grid_mode, bc, bd, energy, pv_battery, grid_battery,
    # battery_load, battery_grid, battery_mode.
    assert captured["cost"][4] == 0.0
    assert captured["cost"][13] == pytest.approx(3.0)


def test_floor_windows_groups_consecutive_equal_ids():
    assert optimize._floor_windows((0, 0, 1, 1, 1, 2)) == ((0, 1), (2, 3, 4), (5,))


def _idle_battery(**changes):
    return battery(
        initial_kwh=0.0,
        minimum_soc_fraction=0.0,
        allow_grid_charge=False,
        allow_battery_export=False,
        **changes,
    )


def test_autonomy_floor_charges_deepest_shortfall_once():
    rows = [(0.30, 0.20, 0.0, 0.0) for _ in range(3)]
    prob = problem(
        rows,
        battery=_idle_battery(),
        soc_target_kwh=(1.0, 2.0, 1.5),
        soc_target_window=(0, 0, 0),
        soc_target_weight=10.0,
    )
    plan = solve(prob)
    assert plan.autonomy_shortfall_kwh == pytest.approx(2.0)
    assert all(flow.end_soc_kwh == pytest.approx(0.0) for flow in plan.flows)


def test_autonomy_floor_creates_one_slack_per_window(monkeypatch):
    rows = [(0.30, 0.20, 0.0, 0.0) for _ in range(4)]
    prob = problem(
        rows,
        battery=_idle_battery(),
        soc_target_kwh=(1.0, 1.0, 1.0, 1.0),
        soc_target_window=(0, 0, 1, 1),
        soc_target_weight=7.0,
    )
    captured = _capture_cost(monkeypatch)
    solve(prob)
    assert captured["cost"].count(7.0) == 2


def test_autonomy_floor_spans_a_zero_deficit_slot_in_one_window(monkeypatch):
    rows = [(0.30, 0.20, 0.0, 0.0) for _ in range(3)]
    prob = problem(
        rows,
        battery=_idle_battery(),
        soc_target_kwh=(1.0, 0.0, 1.0),
        soc_target_window=(0, 0, 0),
        soc_target_weight=5.0,
    )
    captured = _capture_cost(monkeypatch)
    plan = solve(prob)
    assert captured["cost"].count(5.0) == 1
    assert plan.autonomy_shortfall_kwh == pytest.approx(1.0)


def test_backup_clamped_targets_keep_their_windows(monkeypatch):
    rows = [(0.30, 0.20, 0.0, 0.0) for _ in range(4)]
    window = (0, 0, 1, 1)

    def slack_count(targets):
        prob = problem(
            rows,
            battery=_idle_battery(),
            soc_target_kwh=targets,
            soc_target_window=window,
            soc_target_weight=9.0,
        )
        captured = _capture_cost(monkeypatch)
        solve(prob)
        return captured["cost"].count(9.0)

    # A `backup_ready` clamp raises every target above the reserve; the window
    # count must stay the same either way, because ids are target-independent.
    assert slack_count((1.0, 1.5, 1.0, 2.0)) == 2
    assert slack_count((6.0, 6.5, 6.0, 7.0)) == 2


def test_autonomy_floor_slack_is_zero_when_floor_met():
    rows = [(0.30, 0.20, 0.0, 0.0) for _ in range(3)]
    b = battery(allow_grid_charge=False, allow_battery_export=False)
    prob = problem(
        rows,
        battery=b,
        soc_target_kwh=(2.0, 2.0, 2.0),
        soc_target_window=(0, 0, 0),
        soc_target_weight=3.0,
    )
    plan = solve(prob)
    assert plan.autonomy_shortfall_kwh == pytest.approx(0.0)


def test_peak_import_weight_flattens_import_profile():
    rows = [(0.30, 0.20, 0.0, 0.0), (0.30, 0.20, 0.0, 4.0)]
    b = battery()
    baseline = solve(problem(rows, battery=b))
    assert baseline.flows[0].grid_import_kwh == pytest.approx(0.0, abs=1e-6)
    assert baseline.flows[1].grid_import_kwh == pytest.approx(4.0, abs=1e-6)
    assert baseline.peak_import_kw == pytest.approx(0.0)

    flattened = solve(problem(rows, battery=b, peak_import_weight=5.0))
    assert flattened.flows[0].grid_import_kwh == pytest.approx(2.0, abs=1e-3)
    assert flattened.flows[1].grid_import_kwh == pytest.approx(2.0, abs=1e-3)
    assert flattened.peak_import_kw == pytest.approx(2.0, abs=1e-3)


def test_soft_import_cap_reports_violation_instead_of_infeasible():
    rows = [(0.30, 0.20, 0.0, 5.0)]
    prob = problem(rows, soft_import_cap_kw=2.0, cap_violation_weight=1.0)
    plan = solve(prob)
    assert plan.flows[0].grid_import_kwh == pytest.approx(5.0)
    assert plan.cap_violation_kwh == pytest.approx(3.0)


def test_soft_export_cap_reports_violation_instead_of_curtailing():
    rows = [(0.30, 0.20, 5.0, 0.0)]
    prob = problem(rows, soft_export_cap_kw=1.0, cap_violation_weight=0.05)
    plan = solve(prob)
    assert plan.flows[0].grid_export_kwh == pytest.approx(5.0)
    assert plan.flows[0].curtail_kwh == pytest.approx(0.0)
    assert plan.cap_violation_kwh == pytest.approx(4.0)


def test_soft_caps_are_feasible_with_curtailment_disabled():
    rows = [(0.30, 0.20, 5.0, 0.0)]
    b = battery(initial_kwh=8.0)  # already at the 10 * 0.8 ceiling: no room to charge
    site = SiteLimits(8, 8, 8, False)
    prob = problem(
        rows,
        battery=b,
        site=site,
        soft_export_cap_kw=0.0,
        cap_violation_weight=0.1,
    )
    plan = solve(prob)
    assert plan.flows[0].grid_export_kwh == pytest.approx(5.0)
    assert plan.flows[0].curtail_kwh == pytest.approx(0.0)
    assert plan.cap_violation_kwh == pytest.approx(5.0)


def test_monetary_report_excludes_strategy_penalties():
    rows = [(0.30, 0.20, 2.0, 1.0), (0.35, 0.15, 0.0, 1.5)]
    prob = problem(
        rows,
        battery=battery(),
        strategy="self_sufficiency",
        import_kwh_weight=2.0,
        battery_export_penalty_per_kwh=1.0,
        pv_export_margin=0.05,
        peak_import_weight=0.5,
        soft_import_cap_kw=1.0,
        cap_violation_weight=0.2,
    )
    plan = solve(prob)
    assert plan.objective == plan.grid_cost + plan.wear_cost - plan.terminal_credit
    assert plan.objective == plan_monetary_cost(plan)


def test_validate_solution_rejects_understated_floor_slack(monkeypatch):
    rows = [(0.30, 0.20, 0.0, 0.0) for _ in range(3)]
    prob = problem(
        rows,
        battery=_idle_battery(),
        soc_target_kwh=(1.0, 2.0, 1.5),
        soc_target_window=(0, 0, 0),
        soc_target_weight=10.0,
    )
    _reduce_variable(monkeypatch, 10.0)
    with pytest.raises(SolveError, match="invalid solver result"):
        solve(prob)


def test_validate_solution_rejects_understated_peak_import(monkeypatch):
    rows = [(0.30, 0.20, 0.0, 0.0), (0.30, 0.20, 0.0, 4.0)]
    prob = problem(rows, battery=battery(), peak_import_weight=5.0)
    _reduce_variable(monkeypatch, 5.0)
    with pytest.raises(SolveError, match="invalid solver result"):
        solve(prob)


def test_validate_solution_rejects_understated_import_cap_slack(monkeypatch):
    rows = [(0.30, 0.20, 0.0, 5.0)]
    prob = problem(rows, soft_import_cap_kw=2.0, cap_violation_weight=1.0)
    _reduce_variable(monkeypatch, 1.0)
    with pytest.raises(SolveError, match="invalid solver result"):
        solve(prob)


def test_validate_solution_rejects_understated_export_cap_slack(monkeypatch):
    rows = [(0.30, 0.20, 5.0, 0.0)]
    prob = problem(rows, soft_export_cap_kw=1.0, cap_violation_weight=0.05)
    _reduce_variable(monkeypatch, 0.05)
    with pytest.raises(SolveError, match="invalid solver result"):
        solve(prob)


def test_peak_floor_reports_its_own_shortfall():
    rows = [(0.30, 0.20, 0.0, 0.0) for _ in range(3)]
    prob = problem(
        rows,
        battery=_idle_battery(),
        peak_reserve_kwh=(1.0, 2.0, 0.0),
        peak_reserve_window=(0, 0, -1),
        peak_reserve_weight=10.0,
    )
    plan = solve(prob)
    assert plan.peak_reserve_shortfall_kwh == pytest.approx(2.0)
    assert plan.autonomy_shortfall_kwh == 0.0


def test_peak_floor_skips_unconstrained_slots(monkeypatch):
    rows = [(0.30, 0.20, 0.0, 0.0) for _ in range(4)]
    prob = problem(
        rows,
        battery=_idle_battery(),
        peak_reserve_kwh=(0.0, 1.0, 1.0, 0.0),
        peak_reserve_window=(-1, 0, 0, -1),
        peak_reserve_weight=7.0,
    )
    captured = _capture_cost(monkeypatch)
    solve(prob)
    assert captured["cost"].count(7.0) == 1


def test_peak_floor_charges_cheap_energy_to_meet_target():
    # cheap slot then expensive slot with 1 kWh load; floor asks 1 kWh + margin
    rows = [(0.30, 0.0, 0.0, 0.0), (2.00, 0.0, 0.0, 1.0)]
    prob = problem(
        rows,
        battery=battery(initial_kwh=2.0, minimum_soc_fraction=0.2),
        peak_reserve_kwh=(4.0, 3.0),
        peak_reserve_window=(0, 0),
        peak_reserve_weight=1.70,
    )
    plan = solve(prob)
    assert plan.flows[0].end_soc_kwh >= 4.0 - 1e-6
    assert plan.peak_reserve_shortfall_kwh == pytest.approx(0.0, abs=1e-6)


def test_autonomy_and_peak_floors_are_independent():
    rows = [(0.30, 0.20, 0.0, 0.0) for _ in range(2)]
    prob = problem(
        rows,
        battery=_idle_battery(),
        soc_target_kwh=(1.0, 1.0),
        soc_target_window=(0, 0),
        soc_target_weight=10.0,
        peak_reserve_kwh=(3.0, 0.0),
        peak_reserve_window=(0, -1),
        peak_reserve_weight=10.0,
    )
    plan = solve(prob)
    assert plan.autonomy_shortfall_kwh == pytest.approx(1.0)
    assert plan.peak_reserve_shortfall_kwh == pytest.approx(3.0)


def test_validate_solution_rejects_understated_peak_slack(monkeypatch):
    rows = [(0.30, 0.20, 0.0, 0.0) for _ in range(2)]
    prob = problem(
        rows,
        battery=_idle_battery(),
        peak_reserve_kwh=(2.0, 1.0),
        peak_reserve_window=(0, 0),
        peak_reserve_weight=10.0,
    )
    _reduce_variable(monkeypatch, 10.0)
    with pytest.raises(SolveError, match="invalid solver result"):
        solve(prob)
