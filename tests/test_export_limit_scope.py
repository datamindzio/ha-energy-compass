"""`export_limit_scope`: `local_day` (whole-day budget) vs `produced` (running cap).

Covers the live bug this setting fixes: `local_day` lets the plan sell energy that
was grid-charged overnight before any PV exists, financed against PV the day will
only produce later. `produced` tightens the Sell only PV budget to a running
check so export can never get ahead of PV generated so far.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine.models import (
    Battery,
    InputError,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.normalize import validate_problem
from custom_components.energy_compass.engine.optimize import solve
from custom_components.energy_compass.runtime import build_problem, compute
from custom_components.energy_compass.settings import (
    default_configuration,
    validate_configuration,
)

_TOL = 1e-4


def _night_and_day_problem(scope):
    """Night grid-charge, high morning sell price before PV, PV at midday, high evening sell."""
    start = datetime(2026, 9, 18, tzinfo=UTC)
    boundaries = (0, 6, 9, 18, 24)
    rows = (
        (0.1, 0.1, 0, 0),  # 00:00-06:00 night: cheap grid charge
        (5.0, 5.0, 0, 0),  # 06:00-09:00 morning: high sell price, no PV yet
        (0.3, 0.2, 30, 2),  # 09:00-18:00 midday: PV production
        (0.3, 1.0, 0, 0),  # 18:00-24:00 evening: high sell price again
    )
    slots = tuple(
        Slot(
            start + timedelta(hours=boundaries[i]),
            start + timedelta(hours=boundaries[i + 1]),
            *row,
        )
        for i, row in enumerate(rows)
    )
    return Problem(
        slots,
        SiteLimits(50, 50, 50, False),
        Battery(10, 0, 1, 0, 5, 5, 1, 1, 0, True, True),
        "value",
        0,
        (),
        "UTC",
        minimum_mode_minutes=0,
        limit_export_to_pv=True,
        export_limit_scope=scope,
        minimum_export_episode_benefit=0,
        minimum_grid_charge_episode_benefit=0,
    )


def test_local_day_sells_before_pv_but_produced_waits_for_it():
    local_day_plan = solve(_night_and_day_problem("local_day"))
    # Documents today's live behaviour: the daily total lets the plan sell
    # grid-charged energy at 06:00-09:00 before any PV exists that day.
    assert local_day_plan.flows[1].grid_export_kwh > _TOL

    produced_plan = solve(_night_and_day_problem("produced"))
    # No PV has been produced yet at 06:00-09:00, so the running cap forbids it.
    assert produced_plan.flows[1].grid_export_kwh <= _TOL
    # Once PV has flowed (09:00-15:00), stored energy can still be sold that
    # evening against the PV produced so far.
    assert produced_plan.flows[3].grid_export_kwh > _TOL


def _observed_counter_problem(slot0_sell=5.0, slot1_sell=5.0):
    start = datetime(2026, 9, 18, tzinfo=UTC)
    slots = (
        Slot(start, start + timedelta(hours=1), 5.0, slot0_sell, 0, 0),
        Slot(
            start + timedelta(hours=1),
            start + timedelta(hours=2),
            5.0,
            slot1_sell,
            3,
            0,
        ),
    )
    return Problem(
        slots,
        SiteLimits(50, 50, 50, False),
        Battery(10, 0, 1, 5, 0, 5, 1, 1, 0, False, True),
        "value",
        0,
        (),
        "UTC",
        minimum_mode_minutes=0,
        limit_export_to_pv=True,
        export_limit_scope="produced",
        minimum_export_episode_benefit=0,
        minimum_grid_charge_episode_benefit=0,
        pv_generated_today_kwh=5,
        grid_exported_today_kwh=5,
    )


def test_observed_export_at_pv_forbids_more_until_pv_grows_and_stays_feasible():
    plan = solve(_observed_counter_problem())
    # Already-observed export matches already-observed PV: no headroom yet.
    assert plan.flows[0].grid_export_kwh <= _TOL
    # 3 kWh of fresh PV in the second slot buys exactly 3 kWh of fresh export,
    # drawn from the battery's pre-existing charge; the plan stays feasible.
    assert plan.flows[1].grid_export_kwh == pytest.approx(3, abs=_TOL)


def test_validator_rejects_a_plan_that_exports_ahead_of_pv(monkeypatch):
    from custom_components.energy_compass.engine import optimize

    original = optimize._validate_solution
    delta = 2.0

    def corrupt(problem, vectors, values, fractions, budgets):
        # Slot 0 has a zero export ceiling (observed export already covers
        # observed PV); push its export above zero, financed by extra battery
        # discharge so every other physical check (site balance, SOC) still
        # holds and only the produced-scope prefix check trips.
        values[vectors[0]["gout"]] += delta
        values[vectors[0]["bd"]] += delta
        values[vectors[0]["battery_grid"]] += delta
        for v in vectors:
            values[v["energy"]] -= delta
        return original(problem, vectors, values, fractions, budgets)

    monkeypatch.setattr(optimize, "_validate_solution", corrupt)
    with pytest.raises(SolveError, match="export exceeds PV generated so far"):
        solve(_observed_counter_problem())


def test_invalid_export_limit_scope_rejected():
    problem = replace(_observed_counter_problem(), export_limit_scope="whenever")
    with pytest.raises(InputError, match="export_limit_scope"):
        validate_problem(problem)


def test_export_limit_scope_defaults_to_local_day():
    config = default_configuration("PLN", "UTC")
    assert config["settings"]["export_limit_scope"] == "local_day"
    del config["settings"]["export_limit_scope"]
    values = validate_configuration(config, {}, datetime.now(UTC))
    assert values["export_limit_scope"] == "local_day"


def test_invalid_export_limit_scope_setting_rejected():
    config = default_configuration("PLN", "UTC")
    config["settings"]["export_limit_scope"] = "whenever"
    with pytest.raises(InputError, match="export_limit_scope"):
        validate_configuration(config, {}, datetime.now(UTC))


def test_dispatch_policy_reflects_export_limit_scope():
    from custom_components.energy_compass.config_models import NumericSetting
    from custom_components.energy_compass.sources.bindings import EntityBinding

    now = datetime(2026, 9, 18, 12, tzinfo=UTC)
    config = default_configuration("PLN", "Europe/Warsaw")
    config["settings"].update(
        grid_export_kw=8,
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
        export_limit_scope="produced",
    )
    states = {}
    for key, value in (("pv_energy_today", 8), ("grid_export_energy_today", 6)):
        entity_id = f"sensor.{key}"
        config["measurements"][key] = NumericSetting(
            entity=EntityBinding(entity_id), unit="kWh", max_age_seconds=86400
        ).to_dict()
        states[entity_id] = {
            "state": str(value),
            "attributes": {"unit_of_measurement": "kWh"},
            "last_updated": now,
            "last_reported": now,
        }
    source, _, _ = build_problem(config, states, now)
    assert source.export_limit_scope == "produced"
    result = compute(config, states, now)
    assert result["dispatch_policy"]["export_limit_scope"] == "produced"
