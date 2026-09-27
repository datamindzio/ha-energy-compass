from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from types import SimpleNamespace

import numpy as np
import pytest

from custom_components.energy_compass.engine.models import (
    Battery,
    InputError,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import solve


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
    )


def battery(**changes):
    return replace(Battery(10, 0.2, 0.8, 5, 4, 4, 1, 1, 0.1, True, True), **changes)


def assert_physical(plan, source):
    assert len(plan.flows) == len(source.slots)
    previous = source.battery.initial_kwh if source.battery else 0.0
    for slot, flow in zip(source.slots, plan.flows, strict=True):
        dt = (
            slot.end.astimezone(UTC) - slot.start.astimezone(UTC)
        ).total_seconds() / 3600
        assert all(
            isfinite(value) and value >= -1e-6
            for value in (
                flow.grid_import_kwh,
                flow.grid_export_kwh,
                flow.charge_kwh,
                flow.discharge_kwh,
                flow.curtail_kwh,
            )
        )
        assert (
            slot.pv_kwh - flow.curtail_kwh + flow.grid_import_kwh + flow.discharge_kwh
            == pytest.approx(
                slot.load_kwh + flow.grid_export_kwh + flow.charge_kwh, abs=1e-6
            )
        )
        assert flow.grid_import_kwh <= source.site.grid_import_kw * dt + 1e-6
        assert flow.grid_export_kwh <= source.site.grid_export_kw * dt + 1e-6
        assert flow.curtail_kwh <= slot.pv_kwh + 1e-6
        assert (
            abs(slot.pv_kwh - flow.curtail_kwh + flow.discharge_kwh - flow.charge_kwh)
            <= source.site.inverter_kw * dt + 1e-6
        )
        assert min(flow.grid_import_kwh, flow.grid_export_kwh) <= 1e-6
        assert min(flow.charge_kwh, flow.discharge_kwh) <= 1e-6
        if source.battery:
            cell = source.battery
            previous += (
                cell.eta_charge * flow.charge_kwh
                - flow.discharge_kwh / cell.eta_discharge
            )
            assert flow.end_soc_kwh == pytest.approx(previous, abs=1e-6)
            assert (
                cell.capacity_kwh * cell.minimum_soc_fraction - 1e-6
                <= previous
                <= cell.capacity_kwh * cell.maximum_soc_fraction + 1e-6
            )
            assert flow.charge_kwh <= cell.charge_kw * dt + 1e-6
            assert flow.discharge_kwh <= cell.discharge_kw * dt + 1e-6
            if not cell.allow_grid_charge:
                assert flow.charge_kwh <= max(slot.pv_kwh - slot.load_kwh, 0) + 1e-6
            if not cell.allow_battery_export:
                assert flow.discharge_kwh <= max(slot.load_kwh - slot.pv_kwh, 0) + 1e-6


def test_negative_prices_cannot_create_simultaneous_flows(negative_price_problem):
    result = solve(negative_price_problem)
    assert_physical(result, negative_price_problem)
    for flow in result.flows:
        assert min(flow.charge_kwh, flow.discharge_kwh) <= 1e-6
        assert min(flow.grid_import_kwh, flow.grid_export_kwh) <= 1e-6


def test_loss_making_round_trip_is_rejected():
    source = problem(((0.3, 0.2, 0, 0), (0.3, 0.2, 0, 0)), battery=battery())
    result = solve(source)
    assert_physical(result, source)
    assert sum(
        flow.charge_kwh + flow.discharge_kwh for flow in result.flows
    ) == pytest.approx(0)
    assert result.objective == pytest.approx(0)


def test_price_spike_pays_for_a_single_round_trip():
    source = problem(((0.1, 0, 0, 1), (1.0, 0, 0, 1)), battery=battery())
    result = solve(source)
    assert_physical(result, source)
    assert result.flows[0].charge_kwh == pytest.approx(1)
    assert result.flows[1].discharge_kwh == pytest.approx(1)
    assert result.flows[0].grid_import_kwh == pytest.approx(2)
    assert result.flows[1].grid_import_kwh == pytest.approx(0)
    assert result.grid_cost == pytest.approx(0.2)
    assert result.wear_cost == pytest.approx(0.1)
    assert result.objective == pytest.approx(0.3)


def test_minimum_soc_limits_discharge_each_interval():
    source = problem(
        ((1, 0, 0, 3),),
        battery=battery(initial_kwh=4, minimum_soc_fraction=0.3),
        mode="value",
    )
    result = solve(source)
    assert_physical(result, source)
    assert result.flows[0].discharge_kwh == pytest.approx(1)
    assert result.flows[0].end_soc_kwh == pytest.approx(3)


def test_shared_inverter_and_export_curtailment():
    source = problem(((0.2, 0.3, 5, 0),), site=SiteLimits(2, 8, 10, True))
    result = solve(source)
    assert_physical(result, source)
    assert result.flows[0].grid_export_kwh == pytest.approx(2)
    assert result.flows[0].curtail_kwh == pytest.approx(3)


def test_curtailment_unsupported_can_make_pv_infeasible():
    source = problem(((0.2, 0.3, 5, 0),), site=SiteLimits(2, 8, 10, False))
    with pytest.raises(SolveError) as error:
        solve(source)
    assert error.value.reason == "infeasible"


def test_grid_limit_can_make_load_infeasible_without_battery():
    source = problem(((0.2, 0, 0, 3),), site=SiteLimits(5, 2, 2, False))
    with pytest.raises(SolveError) as error:
        solve(source)
    assert error.value.reason == "infeasible"


def test_zero_grid_limits_are_valid_when_solar_covers_load():
    source = problem(((0.2, 0, 1, 1),), site=SiteLimits(2, 0, 0, False))
    result = solve(source)
    assert_physical(result, source)
    assert result.flows[0].grid_import_kwh == pytest.approx(0)


def test_zero_inverter_limit_supports_grid_only_site():
    source = problem(((0.2, 0, 0, 1),), site=SiteLimits(0, 2, 0, False))
    result = solve(source)
    assert_physical(result, source)
    assert result.flows[0].grid_import_kwh == pytest.approx(1)


def test_zero_battery_charge_limit_is_a_valid_capability():
    source = problem(((0.2, 0, 0, 1),), battery=battery(charge_kw=0), mode="value")
    result = solve(source)
    assert_physical(result, source)
    assert result.flows[0].charge_kwh == pytest.approx(0)


def test_disabled_grid_charging_cannot_be_relabelled_as_solar():
    source = problem(
        ((-0.5, 0, 1, 2),),
        battery=battery(allow_grid_charge=False),
        mode="value",
        terminal_value=1,
    )
    result = solve(source)
    assert_physical(result, source)
    assert result.flows[0].charge_kwh == pytest.approx(0)


def test_disabled_battery_export_cannot_be_relabelled_as_load():
    source = problem(
        ((0.1, 1, 1, 0),), battery=battery(allow_battery_export=False), mode="value"
    )
    result = solve(source)
    assert_physical(result, source)
    assert result.flows[0].discharge_kwh == pytest.approx(0)


def test_terminal_modes_preserve_or_credit_residual_energy():
    source = problem(((0.1, 0, 0, 0),), battery=battery(wear_per_kwh=0))
    preserved = solve(source)
    valued = solve(replace(source, terminal_mode="value", terminal_value_per_kwh=1))
    assert_physical(preserved, source)
    assert_physical(valued, source)
    assert preserved.flows[0].end_soc_kwh == pytest.approx(5)
    assert preserved.terminal_credit == pytest.approx(0)
    assert valued.flows[0].charge_kwh == pytest.approx(3)
    assert valued.terminal_credit == pytest.approx(8)
    assert valued.objective == pytest.approx(-7.7)


@pytest.mark.parametrize(
    "start,first_day,next_day",
    [
        (datetime(2026, 9, 17, 21, 30, tzinfo=UTC), "2026-09-17", "2026-09-18"),
        (datetime(2026, 10, 25, 22, 30, tzinfo=UTC), "2026-10-25", "2026-10-26"),
    ],
)
def test_daily_raw_throughput_splits_slots_at_local_midnight(
    start, first_day, next_day
):
    source = problem(
        ((0, 0, 0, 0),),
        battery=battery(wear_per_kwh=0),
        mode="value",
        terminal_value=1,
        start=start,
        budgets=((first_day, 0.25), (next_day, 2)),
        timezone_name="Europe/Warsaw",
    )
    result = solve(source)
    assert_physical(result, source)
    assert result.flows[0].charge_kwh == pytest.approx(0.5)


@pytest.mark.parametrize("day", ["2026-09-17T00:00:00", "20260917"])
def test_noncanonical_daily_budget_key_is_rejected(day):
    source = problem(
        ((0, 0, 0, 0),),
        battery=battery(wear_per_kwh=0),
        mode="value",
        terminal_value=1,
        budgets=((day, 0),),
    )
    with pytest.raises(InputError, match="throughput date"):
        solve(source)


@pytest.mark.parametrize("with_battery", [False, True])
def test_near_binary_solver_result_cannot_return_simultaneous_flows(
    monkeypatch, with_battery
):
    tiny = 4e-6
    if with_battery:
        source = problem(((0, 0, tiny, tiny),), battery=battery())
        candidate = [0, 0, 0, 0, 0, 0, 0, tiny, tiny, 5, tiny, 0, tiny, 0, 1e-6]
    else:
        source = problem(((0, 0, tiny, tiny),))
        candidate = [tiny, tiny, 0, 0, tiny, tiny, 1e-6]
    monkeypatch.setattr(
        "custom_components.energy_compass.engine.optimize.milp",
        lambda **kwargs: SimpleNamespace(
            status=0, x=np.array(candidate), message="optimal"
        ),
    )
    with pytest.raises(SolveError, match="invalid solver result"):
        solve(source)


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf")])
def test_invalid_solver_time_budget_is_rejected(budget):
    source = problem(((0, 0, 0, 0),))
    with pytest.raises(InputError):
        solve(source, time_limit_s=budget)


def test_flexible_load_uses_cheapest_slots_with_power_limit():
    from custom_components.energy_compass.engine.models import FlexibleLoadRequest
    from custom_components.energy_compass.engine.optimize import solve_flexible_load

    source = problem(((0.5, 0, 0, 0), (0.1, 0, 0, 0), (0.2, 0, 0, 0)))

    result = solve_flexible_load(
        source,
        FlexibleLoadRequest(energy_kwh=4, max_power_kw=3),
    )

    assert result.schedule_kwh == pytest.approx((0, 3, 1))
    assert sum(result.schedule_kwh) == pytest.approx(4)
    assert tuple(flow.grid_import_kwh for flow in result.plan.flows) == pytest.approx(
        result.schedule_kwh
    )
    assert result.plan.objective == pytest.approx(0.5)


def test_flexible_load_rejects_target_above_time_and_power_capacity():
    from custom_components.energy_compass.engine.models import FlexibleLoadRequest
    from custom_components.energy_compass.engine.optimize import solve_flexible_load

    source = problem(((0.2, 0, 0, 0),))

    with pytest.raises(SolveError) as error:
        solve_flexible_load(source, FlexibleLoadRequest(3.1, 3))

    assert error.value.reason == "infeasible"


@pytest.mark.parametrize(
    "energy,power",
    [(0, 3), (-1, 3), (float("nan"), 3), (3, 0), (3, float("inf"))],
)
def test_flexible_load_request_must_be_finite_and_positive(energy, power):
    from custom_components.energy_compass.engine.models import FlexibleLoadRequest
    from custom_components.energy_compass.engine.optimize import solve_flexible_load

    with pytest.raises(InputError, match="finite and positive"):
        solve_flexible_load(
            problem(((0.2, 0, 0, 0),)),
            FlexibleLoadRequest(energy, power),
        )


def test_flexible_load_can_use_battery_between_cheap_and_expensive_slots():
    from custom_components.energy_compass.engine.models import FlexibleLoadRequest
    from custom_components.energy_compass.engine.optimize import solve_flexible_load

    source = problem(
        ((0.1, 0, 0, 0), (1.0, 0, 0, 0)),
        battery=battery(),
    )

    result = solve_flexible_load(source, FlexibleLoadRequest(5, 3))

    assert result.schedule_kwh == pytest.approx((3, 2))
    assert result.plan.flows[0].charge_kwh == pytest.approx(2)
    assert result.plan.flows[1].discharge_kwh == pytest.approx(2)
    assert result.plan.objective == pytest.approx(0.7)


def test_flexible_load_can_use_battery_for_load_when_export_is_disabled():
    from custom_components.energy_compass.engine.models import FlexibleLoadRequest
    from custom_components.energy_compass.engine.optimize import solve_flexible_load

    source = problem(
        ((0.1, 0, 0, 0), (1.0, 0, 0, 0)),
        battery=battery(allow_battery_export=False),
    )

    result = solve_flexible_load(source, FlexibleLoadRequest(5, 3))

    assert result.plan.flows[1].discharge_kwh == pytest.approx(2)
    assert result.plan.flows[1].grid_export_kwh == pytest.approx(0)


def _time_limited(monkeypatch, *, feasible=True, gap=0.02):
    """Run the real solver, then report its answer as a time-limited incumbent."""
    from custom_components.energy_compass.engine import optimize

    original = optimize.milp

    def limited(**kwargs):
        result = original(**kwargs)
        return SimpleNamespace(
            status=1,
            x=result.x if feasible else None,
            message="Time limit reached.",
            mip_gap=gap,
        )

    monkeypatch.setattr(optimize, "milp", limited)


def test_time_limited_incumbent_raises_timeout_by_default(monkeypatch):
    _time_limited(monkeypatch)
    with pytest.raises(SolveError) as caught:
        solve(problem(((0.5, 0, 1, 0), (0.1, 0, 1, 0)), battery=battery()))
    assert caught.value.reason == "timeout"


def test_time_limited_incumbent_is_published_when_accepted(monkeypatch):
    source = problem(((0.5, 0, 1, 0), (0.1, 0, 1, 0)), battery=battery())
    proven = solve(source)
    assert proven.time_limited is False
    assert proven.mip_gap == 0.0
    _time_limited(monkeypatch)
    plan = solve(source, accept_incumbent=True)
    assert plan.time_limited is True
    assert plan.mip_gap == pytest.approx(0.02)
    assert plan.objective == pytest.approx(proven.objective)


def test_time_limit_without_incumbent_still_raises_timeout(monkeypatch):
    _time_limited(monkeypatch, feasible=False)
    with pytest.raises(SolveError) as caught:
        solve(problem(((0.5, 0, 1, 0),), battery=battery()), accept_incumbent=True)
    assert caught.value.reason == "timeout"


def test_accepted_incumbent_is_still_validated(monkeypatch):
    tiny = 4e-6
    source = problem(((0, 0, tiny, tiny),))
    monkeypatch.setattr(
        "custom_components.energy_compass.engine.optimize.milp",
        lambda **kwargs: SimpleNamespace(
            status=1,
            x=np.array([tiny, tiny, 0, 0, tiny, tiny, 1e-6]),
            message="Time limit reached.",
            mip_gap=0.01,
        ),
    )
    with pytest.raises(SolveError, match="invalid solver result"):
        solve(source, accept_incumbent=True)
