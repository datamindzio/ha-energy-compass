"""Documented limit: load-following probes do not fit the default probe time limit.

`self_sufficiency` and `backup_ready` keep SELF_CONSUME on through the night, and the
load-following rule makes every full-horizon probe of 192 intervals take several
seconds. The probe stays the exact full solve (a cheaper restricted solve published
costs far from it), so at the default 2 s per probe the reference costs stay unknown
and the levels use the `short_coverage` policy. docs/model.md § Incremental
consumption and both guides § Consumption levels state this; update them with this
test when the probes become fast enough.
"""

from dataclasses import replace

import pytest

from benchmarks.optimizer import make_problem
from custom_components.energy_compass.engine.consumption import analyze_consumption
from custom_components.energy_compass.engine.models import CompassSettings
from custom_components.energy_compass.engine.optimize import solve


@pytest.mark.parametrize("strategy", ["self_sufficiency", "backup_ready"])
@pytest.mark.parametrize("minimum_mode_minutes", [60, 0])
def test_192_interval_probes_stay_unknown_at_default_probe_limit(
    strategy, minimum_mode_minutes
):
    problem = replace(
        make_problem(192, strategy=strategy),
        minimum_mode_minutes=minimum_mode_minutes,
    )
    plan = solve(problem, time_limit_s=15, accept_incumbent=True)
    settings = CompassSettings(
        display_horizon_hours=24,
        reference_horizon_hours=24,
        display_interval_minutes=15,
    )
    assert settings.probe_time_limit_s == 2

    # Three probe limits of budget: enough to show the probes time out, short
    # enough for the per-test timeout. The shipped budget only repeats it.
    result = analyze_consumption(problem, plan, settings=settings, budget_s=6)

    assert len(result.opportunities) == 96
    assert result.opportunities[0].cost_per_kwh is None
    assert result.opportunities[0].level is None
    assert result.classification_mode == "absolute_fallback"
    assert result.coverage_reason == "reference_probe_failed"
    assert result.reference_complete is False
