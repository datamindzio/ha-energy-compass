from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine.consumption import (
    analyze_consumption,
    analyze_flexible_loads,
    classify_cost,
    consumption_outlook,
    machine_snapshot,
    machine_state,
    merge_windows,
    next_transition,
    next_window,
    serialize_opportunity,
)
from custom_components.energy_compass.engine.models import (
    CompassSettings,
    Flow,
    InputError,
    Plan,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
    plan_monetary_cost,
)
from custom_components.energy_compass.engine.optimize import solve

START = datetime(2026, 9, 17, tzinfo=UTC)


def grid_problem(prices, *, start=START, slot_minutes=60):
    slots = tuple(
        Slot(
            start + timedelta(minutes=index * slot_minutes),
            start + timedelta(minutes=(index + 1) * slot_minutes),
            price,
            -0.10,
            0.0,
            0.25,
        )
        for index, price in enumerate(prices)
    )
    return Problem(
        slots,
        SiteLimits(0.0, 10.0, 0.0, False),
        None,
        "preserve_initial",
        0.0,
        (),
        "Europe/Warsaw",
    )


def test_flexible_load_depth_stops_at_first_block_above_fifteen_percent():
    source = grid_problem((1.0, 1.1, 1.2, 1.2, 1.2, 1.2, 1.2))

    result = analyze_flexible_loads(
        source,
        solve(source),
        settings=CompassSettings(),
    )

    assert tuple(profile.energy_kwh for profile in result.profiles) == (
        3,
        5,
        10,
        15,
        20,
    )
    assert tuple(profile.group for profile in result.profiles) == (
        "small",
        "small",
        "medium",
        "medium",
        "large",
    )
    assert result.anchor_price_per_kwh == pytest.approx(1.0)
    assert result.allowed_block_price_per_kwh == pytest.approx(1.15)
    assert result.profiles[1].block_cost_per_kwh == pytest.approx(1.1)
    assert result.profiles[2].block_cost_per_kwh == pytest.approx(1.18)
    assert tuple(profile.price_status for profile in result.profiles[:3]) == (
        "ANCHOR",
        "STABLE",
        "DEGRADED",
    )
    assert result.depth_kwh == 5


def test_flexible_load_negative_anchor_uses_symmetric_fifteen_percent_limit():
    source = grid_problem((-1.0, -0.9, -0.8, -0.8, -0.8, -0.8, -0.8))

    result = analyze_flexible_loads(
        source,
        solve(source),
        settings=CompassSettings(),
    )

    assert result.anchor_price_per_kwh == pytest.approx(-1.0)
    assert result.allowed_block_price_per_kwh == pytest.approx(-0.85)
    assert result.profiles[1].block_cost_per_kwh == pytest.approx(-0.9)
    assert result.profiles[1].price_status == "STABLE"
    assert result.profiles[2].price_status == "DEGRADED"
    assert result.depth_kwh == 5


def test_flexible_load_anchor_unavailable_when_three_kwh_cannot_fit():
    source = grid_problem((0.5,), slot_minutes=30)

    result = analyze_flexible_loads(
        source,
        solve(source),
        settings=CompassSettings(),
    )

    assert result.depth_kwh is None
    assert result.anchor_price_per_kwh is None
    assert result.allowed_block_price_per_kwh is None
    assert all(profile.price_status == "UNKNOWN" for profile in result.profiles)
    assert result.profiles[0].reason == "insufficient_time"


def test_flexible_load_analysis_can_be_disabled():
    source = grid_problem((0.5,) * 8)

    result = analyze_flexible_loads(
        source,
        solve(source),
        settings=CompassSettings(flexible_load_enabled=False),
    )

    assert result.depth_kwh is None
    assert result.profiles == ()


def test_plan_monetary_cost_ignores_the_solver_objective():
    plan = Plan((), 999.0, 12.5, 0.5, 2.0)
    assert plan_monetary_cost(plan) == pytest.approx(11.0)


def test_flexible_load_average_cost_uses_monetary_sum_not_raw_objective(monkeypatch):
    source = grid_problem((0.40,) * 7)
    baseline = solve(source)
    from custom_components.energy_compass.engine import consumption

    original = consumption.solve_flexible_load

    def weighted_solve(problem, request, *, time_limit_s):
        # Simulate a strategy weight inflating the raw objective; the real
        # PLN/kWh cost the advice reports must not move.
        result = original(problem, request, time_limit_s=time_limit_s)
        return replace(
            result,
            plan=replace(
                result.plan, objective=result.plan.objective + 5.0 * request.energy_kwh
            ),
        )

    monkeypatch.setattr(consumption, "solve_flexible_load", weighted_solve)
    result = analyze_flexible_loads(source, baseline, settings=CompassSettings())

    assert result.anchor_price_per_kwh == pytest.approx(0.40)


@pytest.mark.parametrize(
    "cost,expected",
    [
        (-0.20, "BOOST"),
        (-1e-14, "BOOST"),
        (1e-14, "BOOST"),
    ],
)
def test_near_zero_and_negative_costs_are_boost(cost, expected):
    assert (
        classify_cost(
            cost,
            q25=-1.0,
            q75=0.90,
            minimum_purchase_price=-1.0,
        )
        == expected
    )


@pytest.mark.parametrize(
    "cost,expected",
    [
        (0.01 - 1e-14, "BOOST"),
        (0.01, "NORMAL"),
        (0.01 + 1e-14, "NORMAL"),
    ],
)
def test_default_boost_ceiling_is_strictly_below_one_cent(cost, expected):
    assert (
        classify_cost(cost, q25=-1.0, q75=0.90, minimum_purchase_price=-1.0) == expected
    )


@pytest.mark.parametrize("cost", [0.20, 0.20 + 1e-14])
def test_cheap_includes_percentile_equality_and_solver_residue(cost):
    assert (
        classify_cost(cost, q25=0.20, q75=0.90, minimum_purchase_price=0.10) == "CHEAP"
    )


@pytest.mark.parametrize("cost", [0.30, 0.30 + 1e-14])
def test_cheap_includes_minimum_purchase_tariff_and_solver_residue(cost):
    assert (
        classify_cost(cost, q25=0.20, q75=0.90, minimum_purchase_price=0.30) == "CHEAP"
    )


def test_classification_precedence_and_remaining_levels():
    assert (
        classify_cost(0.009, q25=0.0, q75=0.90, minimum_purchase_price=0.0) == "BOOST"
    )
    assert (
        classify_cost(
            1.0,
            q25=0.20,
            q75=0.90,
            minimum_purchase_price=1.0,
            limit_floor=0.80,
        )
        == "CHEAP"
    )
    assert (
        classify_cost(0.40, q25=0.20, q75=0.90, minimum_purchase_price=0.10) == "NORMAL"
    )
    assert (
        classify_cost(0.90, q25=0.20, q75=0.90, minimum_purchase_price=0.10) == "NORMAL"
    )
    assert (
        classify_cost(0.91, q25=0.20, q75=0.90, minimum_purchase_price=0.10) == "LIMIT"
    )
    assert classify_cost(0.80, q25=None, q75=None) == "NORMAL"
    assert classify_cost(0.90, q25=None, q75=None) == "LIMIT"


@pytest.mark.parametrize("minimum", [float("nan"), float("inf"), -float("inf")])
def test_minimum_purchase_price_must_be_finite(minimum):
    with pytest.raises(InputError, match="minimum_purchase_price"):
        classify_cost(0.40, q25=0.20, q75=0.90, minimum_purchase_price=minimum)


def test_display_beyond_flat_reference_uses_inclusive_cheap_comparisons():
    source = grid_problem((1.20, 0.90))
    result = analyze_consumption(
        source,
        solve(source),
        settings=CompassSettings(display_horizon_hours=2, reference_horizon_hours=1),
    )
    assert result.reference_complete
    assert result.classification_mode == "percentile"
    assert tuple(item.level for item in result.opportunities) == ("CHEAP", "CHEAP")


def test_negative_sell_does_not_make_grid_consumption_free(grid_only_problem):
    outlook = consumption_outlook(
        grid_only_problem,
        solve(grid_only_problem),
        settings=CompassSettings(display_horizon_hours=2, reference_horizon_hours=2),
    )
    assert abs(outlook[0].cost_per_kwh - 0.60) < 1e-6


def test_negative_import_price_produces_negative_marginal_cost():
    source = grid_problem((-0.20,))
    outlook = consumption_outlook(
        source,
        solve(source),
        settings=CompassSettings(display_horizon_hours=1, reference_horizon_hours=1),
    )
    assert outlook[0].cost_per_kwh == pytest.approx(-0.20)
    assert outlook[0].level == "BOOST"


def test_transition_windows_and_active_window():
    source = grid_problem((0.40, 0.00, 0.40, 1.00))
    outlook = consumption_outlook(
        source,
        solve(source),
        settings=CompassSettings(display_horizon_hours=4, reference_horizon_hours=4),
    )
    assert tuple(item.level for item in outlook) == (
        "NORMAL",
        "BOOST",
        "NORMAL",
        "LIMIT",
    )
    assert next_transition(outlook, START) == outlook[1]
    assert (
        next_transition(outlook, START + timedelta(hours=1, minutes=30)) == outlook[2]
    )
    windows = merge_windows(outlook, frozenset({"BOOST", "CHEAP"}))
    assert len(windows) == 1
    assert windows[0].start == START + timedelta(hours=1)
    assert next_window(windows, START + timedelta(hours=1, minutes=30)) == windows[0]
    assert next_window(windows, START + timedelta(hours=2)) is None


def test_no_future_window_or_transition_across_unknown_gap():
    source = grid_problem((0.40, 0.00))
    result = analyze_consumption(
        source,
        solve(source),
        settings=CompassSettings(display_horizon_hours=4, reference_horizon_hours=4),
    )
    assert len(result.opportunities) == 4
    assert tuple(item.level for item in result.opportunities) == (
        "NORMAL",
        "BOOST",
        None,
        None,
    )
    assert result.classification_mode == "percentile"
    assert result.coverage_reason == "available_reference_horizon"
    assert not result.reference_complete
    assert next_transition(result.opportunities, START + timedelta(hours=1)) is None
    assert (
        next_window(merge_windows(result.opportunities, frozenset({"LIMIT"})), START)
        is None
    )


def test_reference_extends_past_display_and_custom_percentiles():
    source = grid_problem((0.40, 0.60, 0.10, 1.00))
    result = analyze_consumption(
        source,
        solve(source),
        settings=CompassSettings(
            display_horizon_hours=2,
            reference_horizon_hours=4,
            cheap_percentile=50,
            limit_percentile=50,
            limit_floor=0.50,
        ),
    )
    assert len(result.opportunities) == 2
    assert result.classification_mode == "percentile"
    assert result.coverage_reason == "complete"
    assert tuple(item.level for item in result.opportunities) == ("CHEAP", "LIMIT")


def test_short_coverage_policy_does_not_suppress_successful_available_reference():
    source = grid_problem((0.30, 0.90))
    result = analyze_consumption(
        source,
        solve(source),
        settings=CompassSettings(
            display_horizon_hours=2,
            reference_horizon_hours=3,
            short_coverage="unavailable",
        ),
    )
    assert result.classification_mode == "percentile"
    assert result.coverage_reason == "available_reference_horizon"
    assert not result.reference_complete
    assert tuple(item.level for item in result.opportunities) == ("CHEAP", "LIMIT")
    assert result.opportunities[0].cost_per_kwh == pytest.approx(0.30)


def test_final_partial_interval_is_probed_only_through_source_coverage():
    source = grid_problem((0.30, 0.50, 0.70), slot_minutes=30)
    coverage_end = START + timedelta(minutes=90)
    result = analyze_consumption(
        source,
        solve(source),
        settings=CompassSettings(
            display_horizon_hours=3,
            reference_horizon_hours=3,
            short_coverage="unavailable",
        ),
    )
    assert result.classification_mode == "percentile"
    assert result.coverage_reason == "available_reference_horizon"
    assert tuple(item.end for item in result.opportunities[:2]) == (
        START + timedelta(hours=1),
        coverage_end,
    )
    assert result.opportunities[1].cost_per_kwh == pytest.approx(0.70)
    assert result.opportunities[1].level is not None
    assert result.opportunities[2].start == START + timedelta(hours=2)
    assert result.opportunities[2].cost_per_kwh is None
    assert result.opportunities[2].level is None


@pytest.mark.parametrize("short_coverage", ["absolute_fallback", "unavailable"])
def test_failed_one_minute_tail_keeps_successful_reference_percentiles(short_coverage):
    slots = (
        Slot(START, START + timedelta(hours=1), 0.20, -0.10, 0.0, 0.25),
        Slot(
            START + timedelta(hours=1),
            START + timedelta(hours=2),
            0.80,
            -0.10,
            0.0,
            0.25,
        ),
        Slot(
            START + timedelta(hours=2),
            START + timedelta(hours=2, minutes=1),
            0.60,
            -0.10,
            0.0,
            0.005,
        ),
    )
    source = Problem(
        slots,
        SiteLimits(0.0, 10.0, 0.0, False),
        None,
        "preserve_initial",
        0.0,
        (),
        "Europe/Warsaw",
    )
    result = analyze_consumption(
        source,
        solve(source),
        settings=CompassSettings(
            display_horizon_hours=3,
            reference_horizon_hours=3,
            short_coverage=short_coverage,
        ),
    )
    assert result.classification_mode == "percentile"
    assert result.coverage_reason == "reference_probe_failed"
    assert not result.reference_complete
    assert tuple(item.cost_per_kwh for item in result.opportunities[:2]) == (
        pytest.approx(0.20),
        pytest.approx(0.80),
    )
    assert tuple(item.level for item in result.opportunities) == (
        "CHEAP",
        "NORMAL",
        None,
    )
    assert all(
        item.end <= source.slots[-1].end
        for item in result.opportunities
        if item.cost_per_kwh is not None
    )
    assert result.opportunities[2].end == START + timedelta(hours=2, minutes=1)


def test_probe_cost_uses_monetary_sum_not_raw_objective(monkeypatch):
    source = grid_problem((0.40,))
    baseline = solve(source)
    from custom_components.energy_compass.engine import consumption

    def weighted_probe(problem, **kwargs):
        actual = sum(
            slot.load_kwh - original.load_kwh
            for slot, original in zip(problem.slots, source.slots, strict=True)
        )
        # objective carries an extra shadow-price term a weighted strategy
        # would add; only the grid_cost delta (real PLN) may reach advice.
        return replace(
            baseline,
            objective=baseline.objective + 5.0 * actual,
            grid_cost=baseline.grid_cost + 0.40 * actual,
        )

    monkeypatch.setattr(consumption, "solve", weighted_probe)
    result = analyze_consumption(
        source,
        baseline,
        settings=CompassSettings(display_horizon_hours=1, reference_horizon_hours=1),
    )
    assert result.opportunities[0].cost_per_kwh == pytest.approx(0.40)


def test_minimum_purchase_tariff_is_bounded_to_available_reference(monkeypatch):
    source = grid_problem((0.80, 1.00, 0.10))
    baseline = solve(source)
    from custom_components.energy_compass.engine import consumption

    costs = (0.80, 0.20, 0.60)

    def controlled_probe(problem, **kwargs):
        changed = next(
            index
            for index, (slot, original) in enumerate(
                zip(problem.slots, source.slots, strict=True)
            )
            if slot.load_kwh > original.load_kwh
        )
        actual = sum(
            slot.load_kwh - original.load_kwh
            for slot, original in zip(problem.slots, source.slots, strict=True)
        )
        return replace(baseline, grid_cost=baseline.grid_cost + costs[changed] * actual)

    monkeypatch.setattr(consumption, "solve", controlled_probe)
    result = analyze_consumption(
        source,
        baseline,
        settings=CompassSettings(
            display_horizon_hours=3,
            reference_horizon_hours=2,
            boost_ceiling=0.01,
        ),
    )
    assert result.opportunities[0].cost_per_kwh == pytest.approx(0.80)
    assert result.opportunities[0].level == "CHEAP"


@pytest.mark.parametrize(
    "short_coverage,expected_mode,expected_first",
    [
        ("absolute_fallback", "absolute_fallback", "CHEAP"),
        ("unavailable", "unavailable", None),
    ],
)
def test_failed_full_probe_uses_policy_and_preserves_minimum_tariff(
    monkeypatch, short_coverage, expected_mode, expected_first
):
    source = grid_problem((0.30, 0.90))
    baseline = solve(source)
    from custom_components.energy_compass.engine import consumption

    original = consumption.solve

    def fail_second(problem, **kwargs):
        if problem.slots[1].load_kwh > source.slots[1].load_kwh:
            raise SolveError("timeout")
        return original(problem, **kwargs)

    monkeypatch.setattr(consumption, "solve", fail_second)
    result = analyze_consumption(
        source,
        baseline,
        settings=CompassSettings(
            display_horizon_hours=2,
            reference_horizon_hours=2,
            short_coverage=short_coverage,
        ),
    )
    assert result.classification_mode == expected_mode
    assert result.coverage_reason == "reference_probe_failed"
    assert result.opportunities[1].cost_per_kwh is None
    assert result.opportunities[1].level is None
    assert result.opportunities[0].level == expected_first


@pytest.mark.parametrize("short_coverage", ["absolute_fallback", "unavailable"])
def test_all_reference_probes_failed_stay_unknown(monkeypatch, short_coverage):
    source = grid_problem((0.30, 0.90))
    baseline = solve(source)
    from custom_components.energy_compass.engine import consumption

    def fail_all(problem, **kwargs):
        raise SolveError("timeout")

    monkeypatch.setattr(consumption, "solve", fail_all)
    result = analyze_consumption(
        source,
        baseline,
        settings=CompassSettings(
            display_horizon_hours=2,
            reference_horizon_hours=2,
            short_coverage=short_coverage,
        ),
    )
    assert result.classification_mode == short_coverage
    assert result.coverage_reason == "reference_probe_failed"
    assert not result.reference_complete
    assert all(item.cost_per_kwh is None for item in result.opportunities)
    assert all(item.level is None for item in result.opportunities)


@pytest.mark.parametrize(
    "budget_s,probe_limit_s,elapsed_s",
    [
        (1.0, 2.0, 2.0),
        (5.0, 0.5, 1.0),
    ],
)
def test_probe_completed_after_deadline_stays_unknown(
    monkeypatch, budget_s, probe_limit_s, elapsed_s
):
    source = grid_problem((0.40,))
    baseline = solve(source)
    from custom_components.energy_compass.engine import consumption

    clock = [0.0]
    calls = []

    def late_solve(problem, *, time_limit_s):
        calls.append(time_limit_s)
        clock[0] += elapsed_s
        return replace(baseline, grid_cost=baseline.grid_cost + 0.40)

    monkeypatch.setattr(consumption, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(consumption, "solve", late_solve)
    result = analyze_consumption(
        source,
        baseline,
        settings=CompassSettings(
            display_horizon_hours=1,
            reference_horizon_hours=1,
            probe_time_limit_s=probe_limit_s,
        ),
        budget_s=budget_s,
    )
    assert len(calls) == 1
    assert calls[0] <= min(budget_s, probe_limit_s)
    assert not result.reference_complete
    assert result.coverage_reason == "reference_probe_failed"
    assert result.opportunities[0].cost_per_kwh is None
    assert result.opportunities[0].level is None


def test_probe_spreads_load_by_utc_overlap_and_uses_actual_probe_size(monkeypatch):
    source = grid_problem(
        (0.60, 0.60), start=START + timedelta(minutes=30), slot_minutes=30
    )
    baseline = solve(source)
    from custom_components.energy_compass.engine import consumption

    original = consumption.solve
    observed = []

    def record(problem, **kwargs):
        assert problem.battery == source.battery
        assert problem.terminal_mode == source.terminal_mode
        assert problem.terminal_value_per_kwh == source.terminal_value_per_kwh
        assert problem.slots[0].start == source.slots[0].start
        observed.append(
            tuple(
                slot.load_kwh - prior.load_kwh
                for slot, prior in zip(problem.slots, source.slots, strict=True)
            )
        )
        return original(problem, **kwargs)

    monkeypatch.setattr(consumption, "solve", record)
    outlook = consumption_outlook(
        source,
        baseline,
        settings=CompassSettings(
            display_horizon_hours=1,
            display_interval_minutes=60,
            reference_horizon_hours=1,
            probe_kwh=0.4,
        ),
    )
    assert len(observed) == 1
    assert observed[0] == pytest.approx((0.2, 0.2))
    assert tuple(item.cost_per_kwh for item in outlook) == pytest.approx((0.6,))
    assert tuple((item.end - item.start).total_seconds() for item in outlook) == (3600,)


def test_repeated_local_hour_and_offset_serialization():
    start = datetime(2026, 10, 25, 0, tzinfo=UTC)
    source = grid_problem((0.20, 0.30, 0.40), start=start)
    outlook = consumption_outlook(
        source,
        solve(source),
        settings=CompassSettings(display_horizon_hours=3, reference_horizon_hours=3),
    )
    assert len(outlook) == 3
    assert serialize_opportunity(outlook[0])["start"].endswith("+02:00")
    assert serialize_opportunity(outlook[1])["start"].endswith("+01:00")
    assert outlook[0].start.hour == outlook[1].start.hour == 2
    assert all(
        (item.end.astimezone(UTC) - item.start.astimezone(UTC)).total_seconds() == 3600
        for item in outlook
    )


@pytest.mark.parametrize(
    "settings",
    [
        CompassSettings(display_horizon_hours=49),
        CompassSettings(display_interval_minutes=7),
        CompassSettings(probe_kwh=0),
        CompassSettings(cheap_percentile=80, limit_percentile=20),
        CompassSettings(reference_horizon_hours=48, display_interval_minutes=15),
        CompassSettings(flexible_load_max_power_kw=0),
        CompassSettings(flexible_price_degradation_percent=101),
    ],
)
def test_unsupported_settings_fail_before_probing(settings):
    source = grid_problem((0.20,))
    with pytest.raises(InputError):
        consumption_outlook(source, solve(source), settings=settings)


@pytest.mark.parametrize(
    "flow,pv,load,expected",
    [
        (Flow(0, 0, 0, 0, 1, 0), 2, 1, "CURTAIL"),
        (Flow(1, 0, 1, 0, 0, 0), 1, 1, "CHARGE_GRID"),
        (Flow(0, 0, 1, 0, 0, 0), 2, 1, "CHARGE_PV"),
        (Flow(0, 1, 0, 1, 0, 0), 1, 1, "DISCHARGE_GRID"),
        (Flow(0, 0, 0, 1, 0, 0), 1, 2, "SELF_CONSUME"),
        (Flow(1, 0, 0, 0, 0, 0), 1, 2, "HOLD"),
    ],
)
def test_machine_state_precedence(flow, pv, load, expected):
    slot = Slot(START, START + timedelta(hours=1), 0.20, 0.10, pv, load)
    assert machine_state(slot, flow) == expected
    snapshot = machine_snapshot(slot, flow)
    assert snapshot["state"] == expected
    assert snapshot["grid_import_kwh"] == flow.grid_import_kwh
    assert snapshot["discharge_kwh"] == flow.discharge_kwh


@pytest.mark.parametrize("hours,minutes", [(24, 60), (48, 30)])
def test_rolling_bins_preserve_complete_reference_at_microsecond_start(hours, minutes):
    start = START + timedelta(minutes=7, microseconds=7438)
    source = grid_problem(
        (0.2,) * (hours * 60 // minutes), start=start, slot_minutes=minutes
    )
    result = analyze_consumption(
        source,
        solve(source),
        settings=CompassSettings(
            display_horizon_hours=hours,
            reference_horizon_hours=hours,
            display_interval_minutes=minutes,
            probe_kwh=0.2,
        ),
    )
    assert len(result.opportunities) == hours * 60 // minutes
    assert result.reference_complete
    assert result.classification_mode == "percentile"
    assert result.opportunities[0].start == start
    assert result.opportunities[-1].end == start + timedelta(hours=hours)
    assert all(
        (item.end - item.start).total_seconds() == minutes * 60
        for item in result.opportunities
    )
