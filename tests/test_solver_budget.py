"""Tight dispatch bounds and optional-work budgets preserve usable advice."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass import runtime
from custom_components.energy_compass.engine import consumption, optimize
from custom_components.energy_compass.engine.models import (
    Battery,
    CompassSettings,
    InputError,
    Problem,
    SiteLimits,
    Slot,
)
from custom_components.energy_compass.flow_schema import settings_schema
from custom_components.energy_compass.settings import (
    default_configuration,
    validate_configuration,
)


def solar_problem():
    start = datetime(2026, 9, 18, tzinfo=UTC)
    return Problem(
        (Slot(start, start + timedelta(hours=1), 1, 5, 10, 1),),
        SiteLimits(10, 10, 10, False),
        Battery(10, 0, 1, 0, 1, 1, 1, 1, 0, True, False),
        "value",
        10,
        (),
        "UTC",
        limit_export_to_pv=False,
    )


def test_mode_relaxation_excludes_fictitious_grid_arbitrage(monkeypatch):
    source = solar_problem()
    assert optimize.solve(source).objective == pytest.approx(-50)
    captured = []
    original = optimize._Model.solve

    class Captured(Exception):
        pass

    def capture(model, limit):
        captured.append(model)
        raise Captured

    monkeypatch.setattr(optimize._Model, "solve", capture)
    with pytest.raises(Captured):
        optimize.solve(source)
    model = captured[0]
    model.integrality = [0] * len(model.integrality)
    values = original(model, 5)
    # PV10 minus load1 and charge1 leaves export8; terminal energy is worth10.
    # Fractional directions must not buy1 to export an additional1 at the higher rate.
    assert sum(c * x for c, x in zip(model.cost, values)) == pytest.approx(-50)


def test_exhausted_optional_probes_do_not_discard_valid_base_plan(monkeypatch):
    now = datetime(2026, 9, 18, tzinfo=UTC)
    config = default_configuration("PLN", "UTC")
    config["settings"].update(
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
        solve_time_limit_s=1,
        total_time_limit_s=3,
    )
    clock = [0.0]
    original = consumption.solve

    def late_probe(problem, *, time_limit_s):
        result = original(problem, time_limit_s=time_limit_s)
        clock[0] += time_limit_s + 0.02
        return result

    monkeypatch.setattr(runtime, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(consumption, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(consumption, "solve", late_probe)
    result = runtime.compute(config, {}, now)
    assert result["valid"]
    assert result["status"] == "ready"
    assert result["coverage_reason"] == "reference_probe_failed"
    assert result["intervals"]
    assert all(row["cost_per_kwh"] is None for row in result["outlook"])
    assert clock[0] < 3


def test_zero_optional_budget_leaves_costs_unknown():
    source = solar_problem()
    baseline = optimize.solve(source)
    result = consumption.analyze_consumption(
        source,
        baseline,
        settings=CompassSettings(display_horizon_hours=1, reference_horizon_hours=1),
        budget_s=0,
    )
    assert result.coverage_reason == "reference_probe_failed"
    assert all(row.cost_per_kwh is None for row in result.opportunities)


@pytest.mark.parametrize("seconds", [20, 30])
def test_long_base_budget_keeps_hard_overall_limit(seconds):
    now = datetime(2026, 9, 18, tzinfo=UTC)
    config = default_configuration("PLN", "UTC")
    assert config["settings"]["solve_time_limit_s"] == 10
    config["settings"]["solve_time_limit_s"] = seconds
    assert (
        settings_schema("performance", config["settings"])(
            {"solve_time_limit_s": seconds}
        )["solve_time_limit_s"]
        == seconds
    )
    values = validate_configuration(config, {}, now)
    assert values["solve_time_limit_s"] == seconds
    assert values["total_time_limit_s"] == 60
    config["settings"]["total_time_limit_s"] = seconds
    with pytest.raises(InputError, match="total compute budget"):
        validate_configuration(config, {}, now)


def test_base_budget_above_supported_limit_is_rejected():
    config = default_configuration("PLN", "UTC")
    config["settings"]["solve_time_limit_s"] = 31
    with pytest.raises(InputError, match="solve_time_limit_s outside"):
        validate_configuration(config, {}, datetime(2026, 9, 18, tzinfo=UTC))


def test_five_minute_budget_supports_longer_consumption_probes(monkeypatch):
    now = datetime(2026, 9, 18, tzinfo=UTC)
    config = default_configuration("PLN", "UTC")
    config["settings"].update(
        total_time_limit_s=300,
        solve_time_limit_s=30,
        probe_time_limit_s=30,
        refresh_minutes=60,
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
    )
    clock = [0.0]
    original = consumption.solve

    def slow_probe(problem, *, time_limit_s):
        assert time_limit_s > 20
        result = original(problem, time_limit_s=time_limit_s)
        clock[0] += 20
        return result

    monkeypatch.setattr(runtime, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(consumption, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(consumption, "solve", slow_probe)
    result = runtime.compute(config, {}, now)
    assert result["valid"]
    assert result["coverage_reason"] == "complete"
    assert all(row["cost_per_kwh"] == pytest.approx(0) for row in result["outlook"])
