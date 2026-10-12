"""Documented limit: load-following probes do not fit the default probe time limit.

`self_sufficiency` and `backup_ready` keep SELF_CONSUME on through the night, and the
load-following rule makes a full-horizon probe take several seconds already on the
default horizon (24 h of quarter-hours = 96 intervals). The probe stays the exact full
solve (docs/adr/0006-consumption-probes-stay-exact.md), so at the default 2 s per
probe the reference costs stay unknown and the levels use the `short_coverage` policy.
docs/runtime-validation.md § Consumption probe limits holds the measured table
(`python -m benchmarks.probes`), docs/model.md § Incremental consumption and both
guides § Consumption levels state the limit; update them with this test when the
probes become fast enough.
"""

from dataclasses import replace

import pytest

from benchmarks.optimizer import make_problem
from benchmarks.probes import replay
from custom_components.energy_compass.engine.consumption import (
    analyze_consumption,
    analyze_flexible_loads,
)
from custom_components.energy_compass.engine.models import CompassSettings
from custom_components.energy_compass.engine.optimize import solve

# Two probe limits of budget: enough to show the first probe times out, short enough
# for a loaded CI host. The shipped budget only repeats it.
_BUDGET_S = 4


def _analyze(strategy, slots, settings, minimum_mode_minutes=60):
    problem = replace(
        make_problem(slots, strategy=strategy),
        minimum_mode_minutes=minimum_mode_minutes,
    )
    # The shipped base solve: 10 s, the validated incumbent at the limit.
    plan = solve(problem, time_limit_s=10, accept_incumbent=True)
    return analyze_consumption(problem, plan, settings=settings, budget_s=_BUDGET_S)


def _assert_defaults(settings):
    assert settings.display_horizon_hours == 24
    assert settings.reference_horizon_hours == 24
    assert settings.display_interval_minutes == 60
    assert settings.probe_time_limit_s == 2
    assert settings.short_coverage == "absolute_fallback"


@pytest.mark.timeout(180)
def test_default_horizon_self_sufficiency_probe_stays_unknown():
    settings = CompassSettings()
    _assert_defaults(settings)

    result = _analyze("self_sufficiency", 96, settings)

    assert len(result.opportunities) == 24
    assert result.opportunities[0].cost_per_kwh is None
    assert result.opportunities[0].level is None
    assert result.classification_mode == "absolute_fallback"
    assert result.coverage_reason == "reference_probe_failed"
    assert result.reference_complete is False


@pytest.mark.timeout(180)
def test_default_horizon_self_sufficiency_flexible_depth_stays_unknown():
    """The flexible depth solves share the probe limit and time out the same way."""
    settings = CompassSettings()
    _assert_defaults(settings)
    assert settings.flexible_load_enabled is True
    problem = make_problem(96, strategy="self_sufficiency")
    plan = solve(problem, time_limit_s=10, accept_incumbent=True)

    result = analyze_flexible_loads(
        problem, plan, settings=settings, budget_s=_BUDGET_S
    )

    assert result.depth_kwh is None
    assert result.profiles[0].price_status == "UNKNOWN"
    assert result.profiles[0].reason == "timeout"


@pytest.mark.timeout(180)
def test_default_horizon_cost_min_probe_is_known():
    """The same horizon and limits leave the default strategy its probe costs."""
    settings = CompassSettings()
    _assert_defaults(settings)

    result = _analyze("cost_min", 96, settings)

    assert len(result.opportunities) == 24
    assert result.opportunities[0].cost_per_kwh is not None


@pytest.mark.timeout(180)
@pytest.mark.parametrize("strategy", ["self_sufficiency", "backup_ready"])
@pytest.mark.parametrize("minimum_mode_minutes", [60, 0])
def test_192_interval_probes_stay_unknown_at_default_probe_limit(
    strategy, minimum_mode_minutes
):
    settings = CompassSettings(
        display_horizon_hours=24,
        reference_horizon_hours=24,
        display_interval_minutes=15,
    )
    assert settings.probe_time_limit_s == 2

    result = _analyze(strategy, 192, settings, minimum_mode_minutes)

    assert len(result.opportunities) == 96
    assert result.opportunities[0].cost_per_kwh is None
    assert result.opportunities[0].level is None
    assert result.classification_mode == "absolute_fallback"
    assert result.coverage_reason == "reference_probe_failed"
    assert result.reference_complete is False


def test_replay_applies_the_probe_limit_and_the_shared_budget():
    # 60 s total, 1 s finishing reserve: 49 s for probes after a 10 s base plan.
    assert replay(10.0, [1.0, 2.5, 1.0], 2.0) == 2
    assert replay(10.0, [2.0] * 30, 2.0) == 24
    assert replay(10.0, [3.0] * 30, 2.0) == 0
    assert replay(59.0, [0.1], 2.0) == 0
