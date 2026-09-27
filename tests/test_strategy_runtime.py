"""Runtime wiring for Step 6: effective_settings, strategy/weights into Problem,
the 48 h autonomy tail, commitment release and the result contract.

Mirrors the fixture style of tests/test_task5_runtime.py.
"""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.config_models import (
    LoadSource,
    PvSource,
)
from custom_components.energy_compass.engine.autonomy import autonomy_targets
from custom_components.energy_compass.engine.models import STRATEGIES
from custom_components.energy_compass.engine.strategy import STRATEGY_OVERRIDES
from custom_components.energy_compass.runtime import (
    build_problem,
    compute,
    effective_settings,
    measurement_diagnostics,
)
from custom_components.energy_compass.settings import (
    default_configuration,
    validate_configuration,
)
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)


def battery_configuration():
    config = default_configuration("EUR", "UTC")
    config["sources"].update(
        battery_enabled=True, soc=EntityBinding("sensor.soc").to_dict()
    )
    config["settings"].update(
        capacity_kwh=20,
        operating_floor=10,
        hardware_floor=0,
        soc_ceiling=100,
        inverter_kw=10,
        grid_import_kw=10,
        grid_export_kw=10,
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
        eta_charge=0.9,
        eta_discharge=0.8,
        wear_per_kwh=0.04,
        allow_grid_charge=True,
        allow_battery_export=True,
        limit_export_to_pv=False,
    )
    return config


def _soc_state(now):
    return {"sensor.soc": {"state": "50", "attributes": {}, "last_updated": now}}


def _pv_binding():
    return IntervalBinding(
        EntityBinding("sensor.pv", attribute="rows"),
        start_path="start",
        end_path="end",
        value_path="value",
        unit="kWh",
    )


def _pv_state(now, rows):
    return {
        "sensor.pv": {"state": "ok", "attributes": {"rows": rows}, "last_updated": now}
    }


def _with_pv(config, now, rows):
    config["sources"]["pv"] = PvSource(True, ((_pv_binding(),),)).to_dict()
    return _pv_state(now, rows)


def _load_forecast_binding():
    return IntervalBinding(
        EntityBinding("sensor.load", attribute="rows"),
        start_path="start",
        end_path="end",
        value_path="value",
        unit="kWh",
    )


def _with_forecast_load(config, now, rows):
    config["sources"]["load"] = LoadSource(
        "forecast", forecast=_load_forecast_binding()
    ).to_dict()
    return {
        "sensor.load": {
            "state": "ok",
            "attributes": {"rows": rows},
            "last_updated": now,
        }
    }


_NOW = datetime(2026, 9, 17, tzinfo=UTC)


def _flat_row(start, hours, value):
    return {"start": start, "end": start + timedelta(hours=hours), "value": value}


# --- Effective settings: the single resolution point --------------------


def test_cost_min_effective_settings_equal_validated_settings():
    now = _NOW
    config = default_configuration("EUR", "UTC")
    validated = validate_configuration(config, {}, now)
    effective = effective_settings(config, {}, now)
    assert effective == validated


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_effective_settings_applies_bundle_flags(strategy):
    now = _NOW
    config = default_configuration("EUR", "UTC")
    config["settings"]["strategy"] = strategy
    effective = effective_settings(config, {}, now)
    for key, value in STRATEGY_OVERRIDES[strategy].items():
        assert effective[key] == value


def test_effective_settings_respects_explicit_fields():
    now = _NOW
    config = default_configuration("EUR", "UTC")
    config["settings"]["strategy"] = "pv_swap"
    config["settings"]["limit_export_to_pv"] = False
    config["explicit_strategy_fields"] = ["limit_export_to_pv"]
    effective = effective_settings(config, {}, now)
    assert effective["limit_export_to_pv"] is False


def test_max_export_does_not_require_daily_export_counters():
    now = _NOW
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        strategy="max_export",
        limit_export_to_pv=True,
        grid_export_kw=5,
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
    )
    problem, values, _ = build_problem(config, {}, now)
    assert values["limit_export_to_pv"] is False
    assert problem.limit_export_to_pv is False


def test_published_policy_reports_effective_flags():
    now = _NOW
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        strategy="max_export",
        limit_export_to_pv=True,
        grid_export_kw=5,
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
    )
    result = compute(config, {}, now)
    assert result["dispatch_policy"]["limit_export_to_pv"] is False


def test_measurement_diagnostics_uses_effective_flags():
    now = _NOW
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        strategy="max_export", limit_export_to_pv=True, grid_export_kw=5
    )
    config["measurements"]["pv_energy_today"] = {
        "entity": EntityBinding("sensor.pv_today").to_dict(),
        "unit": "kWh",
    }
    diagnostics = measurement_diagnostics(config, {}, now)
    assert diagnostics["pv_energy_today"]["usage"] == "diagnostic_only"


# --- Strategy, weights and flags into Problem ----------------------------


def test_cost_min_problem_matches_previous_defaults():
    now = _NOW
    config = default_configuration("EUR", "UTC")
    problem, _, _ = build_problem(config, {}, now)
    assert problem.strategy == "cost_min"
    assert problem.import_weight == 1.0
    assert problem.export_weight == 1.0
    assert problem.import_kwh_weight == 0.0
    assert problem.battery_export_penalty_per_kwh == 0.0
    assert problem.pv_export_margin == 0.0
    assert problem.peak_import_weight == 0.0
    assert problem.cap_violation_weight == 0.0
    assert problem.soft_import_cap_kw is None
    assert problem.soft_export_cap_kw is None
    assert problem.soc_target_weight == 0.0
    assert problem.soc_target_kwh == ()
    assert problem.soc_target_window == ()
    assert problem.strategy_changed is False


def test_import_penalty_reaches_the_cost_min_problem():
    config = default_configuration("EUR", "UTC")
    assert config["settings"]["import_penalty_per_kwh"] == 0
    config["settings"]["import_penalty_per_kwh"] = 0.12
    problem, _, _ = build_problem(config, {}, _NOW)
    assert problem.strategy == "cost_min"
    assert problem.import_kwh_weight == 0.12


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_strategy_overrides_reach_the_problem(strategy):
    now = _NOW
    config = default_configuration("EUR", "UTC")
    config["settings"]["strategy"] = strategy
    problem, _, _ = build_problem(config, {}, now)
    assert problem.strategy == strategy
    if strategy == "self_sufficiency":
        assert problem.import_kwh_weight > 0
    elif strategy == "pv_swap":
        assert problem.pv_export_margin == config["settings"]["pv_swap_margin_per_kwh"]
        assert problem.limit_export_to_pv is True
    elif strategy == "max_export":
        assert problem.limit_export_to_pv is False
        assert problem.minimum_export_episode_benefit == 0.0
    elif strategy == "grid_friendly":
        assert problem.peak_import_weight > 0
        assert problem.cap_violation_weight > 0


def test_explicit_field_is_not_overridden_by_bundle():
    now = _NOW
    config = default_configuration("EUR", "UTC")
    config["settings"]["strategy"] = "pv_swap"
    config["settings"]["limit_export_to_pv"] = False
    config["explicit_strategy_fields"] = ["limit_export_to_pv"]
    problem, _, _ = build_problem(config, {}, now)
    assert problem.limit_export_to_pv is False


# --- Autonomy tail --------------------------------------------------------


def test_autonomy_floor_uses_forty_eight_hour_tail():
    now = _NOW
    config = battery_configuration()
    config["settings"].update(
        strategy="cost_min",
        autonomy_reserve=True,
        horizon_hours=24,
        display_horizon_hours=24,
        reference_horizon_hours=24,
        daily_load_kwh=24,
    )
    rows = [_flat_row(now, 30, 0.0), _flat_row(now + timedelta(hours=30), 18, 5.0)]
    states = {**_soc_state(now), **_with_pv(config, now, rows)}
    problem, _, quality = build_problem(config, states, now)
    assert "autonomy_tail_unavailable" not in quality["warnings"]
    assert "autonomy_floor_requires_pv" not in quality["warnings"]

    priced_only = autonomy_targets(
        tuple(slot.start for slot in problem.slots),
        tuple((slot.end - slot.start).total_seconds() / 3600 for slot in problem.slots),
        tuple(slot.load_kwh for slot in problem.slots),
        tuple(slot.pv_kwh for slot in problem.slots),
        reserve_kwh=problem.battery.capacity_kwh * problem.battery.minimum_soc_fraction,
        usable_capacity_kwh=problem.battery.capacity_kwh
        * problem.battery.maximum_soc_fraction,
        eta_discharge=problem.battery.eta_discharge,
        timezone="UTC",
    )
    assert problem.soc_target_kwh[-1] > priced_only.targets_kwh[-1]


def test_autonomy_tail_uses_pv_and_load_coverage_only():
    now = _NOW
    config = battery_configuration()
    config["settings"].update(
        strategy="cost_min",
        autonomy_reserve=True,
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
        daily_load_kwh=24,
    )
    rows = [_flat_row(now, 30, 0.0), _flat_row(now + timedelta(hours=30), 18, 5.0)]
    states = {**_soc_state(now), **_with_pv(config, now, rows)}
    problem, _, quality = build_problem(config, states, now)
    assert "autonomy_tail_unavailable" not in quality["warnings"]

    priced_only = autonomy_targets(
        tuple(slot.start for slot in problem.slots),
        tuple((slot.end - slot.start).total_seconds() / 3600 for slot in problem.slots),
        tuple(slot.load_kwh for slot in problem.slots),
        tuple(slot.pv_kwh for slot in problem.slots),
        reserve_kwh=problem.battery.capacity_kwh * problem.battery.minimum_soc_fraction,
        usable_capacity_kwh=problem.battery.capacity_kwh
        * problem.battery.maximum_soc_fraction,
        eta_discharge=problem.battery.eta_discharge,
        timezone="UTC",
    )
    assert problem.soc_target_kwh[-1] > priced_only.targets_kwh[-1]


def test_autonomy_floor_absent_when_pv_disabled():
    now = _NOW
    config = battery_configuration()
    config["settings"].update(strategy="cost_min", autonomy_reserve=True)
    states = _soc_state(now)
    problem, _, quality = build_problem(config, states, now)
    assert "autonomy_floor_requires_pv" in quality["warnings"]
    assert problem.soc_target_kwh == ()
    assert problem.soc_target_window == ()
    assert problem.soc_target_weight == 0.0


def test_autonomy_tail_issues_no_additional_recorder_query():
    from unittest.mock import patch

    from custom_components.energy_compass import runtime as runtime_module

    now = _NOW
    config = battery_configuration()
    config["sources"]["load"] = LoadSource(
        "recorder", statistic_id="sensor.household", history_unit="kWh"
    ).to_dict()
    config["settings"].update(
        strategy="cost_min",
        autonomy_reserve=True,
        allow_fallback=True,
        fallback_daily_kwh=24,
    )
    rows = [_flat_row(now, 48, 0.0)]
    states = {**_soc_state(now), **_with_pv(config, now, rows)}
    statistics = ({"start": "2026-09-16T23:00:00+00:00", "sum": 0},)
    power_samples = ()

    real = runtime_module.load_for_slots_with_quality
    with patch(
        "custom_components.energy_compass.runtime.load_for_slots_with_quality",
        wraps=real,
    ) as spy:
        build_problem(
            config, states, now, statistics=statistics, power_samples=power_samples
        )

    assert spy.call_count == 2
    for call in spy.call_args_list:
        assert call.kwargs["statistics"] is statistics
        assert call.kwargs["power_samples"] is power_samples


def test_autonomy_tail_for_daily_estimate_load():
    now = _NOW
    config = battery_configuration()
    config["settings"].update(
        strategy="cost_min", autonomy_reserve=True, daily_load_kwh=24
    )
    rows = [_flat_row(now, 48, 0.0)]
    states = {**_soc_state(now), **_with_pv(config, now, rows)}
    problem, _, quality = build_problem(config, states, now)
    assert "autonomy_tail_unavailable" not in quality["warnings"]
    assert len(problem.soc_target_kwh) == len(problem.slots)
    assert problem.soc_target_kwh[-1] > 0


def test_autonomy_tail_for_forecast_load_stops_at_forecast_coverage():
    now = _NOW
    config = battery_configuration()
    config["settings"].update(
        strategy="cost_min",
        autonomy_reserve=True,
        horizon_hours=24,
        display_horizon_hours=24,
        reference_horizon_hours=24,
    )
    pv_rows = [_flat_row(now, 48, 0.0)]
    load_rows = [_flat_row(now, 30, 30.0)]
    states = {
        **_soc_state(now),
        **_with_pv(config, now, pv_rows),
        **_with_forecast_load(config, now, load_rows),
    }
    problem, _, quality = build_problem(config, states, now)
    assert "autonomy_tail_unavailable" not in quality["warnings"]

    combined_starts = tuple(slot.start for slot in problem.slots) + tuple(
        now + timedelta(hours=h) for h in range(24, 30)
    )
    combined_durations = (1.0,) * 30
    combined_loads = (1.0,) * 30
    combined_pv = (0.0,) * 30
    reference = autonomy_targets(
        combined_starts,
        combined_durations,
        combined_loads,
        combined_pv,
        reserve_kwh=problem.battery.capacity_kwh * problem.battery.minimum_soc_fraction,
        usable_capacity_kwh=problem.battery.capacity_kwh
        * problem.battery.maximum_soc_fraction,
        eta_discharge=problem.battery.eta_discharge,
        timezone="UTC",
    )
    assert problem.soc_target_kwh == reference.targets_kwh[:24]
    assert problem.soc_target_window == reference.window_ids[:24]


def test_autonomy_tail_preserves_native_edges():
    now = _NOW
    config = battery_configuration()
    config["settings"].update(
        strategy="cost_min",
        autonomy_reserve=True,
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
        daily_load_kwh=24,
    )
    rows = [
        _flat_row(now, 1, 0.0),
        _flat_row(now + timedelta(hours=1), 4, 0.0),
        {
            "start": now + timedelta(hours=5),
            "end": now + timedelta(hours=5, minutes=15),
            "value": 0.5,
        },
        {
            "start": now + timedelta(hours=5, minutes=15),
            "end": now + timedelta(hours=6),
            "value": 0.0,
        },
        _flat_row(now + timedelta(hours=6), 4, 0.0),
    ]
    states = {**_soc_state(now), **_with_pv(config, now, rows)}
    problem, _, quality = build_problem(config, states, now)
    assert "autonomy_tail_unavailable" not in quality["warnings"]
    assert problem.soc_target_kwh[0] == pytest.approx(12.9375)


def test_autonomy_tail_coarsens_above_192_intervals():
    now = _NOW
    config = battery_configuration()
    config["settings"].update(
        strategy="cost_min",
        autonomy_reserve=True,
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
        daily_load_kwh=24,
    )
    rows = [_flat_row(now, 1, 0.0)]
    dense_start = now + timedelta(hours=1)
    for i in range(220):
        rows.append(
            {
                "start": dense_start + timedelta(minutes=i),
                "end": dense_start + timedelta(minutes=i + 1),
                "value": 0.0,
            }
        )
    states = {**_soc_state(now), **_with_pv(config, now, rows)}
    problem, _, quality = build_problem(config, states, now)
    assert "autonomy_tail_coarsened" in quality["warnings"]
    assert "autonomy_tail_unavailable" not in quality["warnings"]
    assert len(problem.soc_target_kwh) == len(problem.slots)


def test_autonomy_tail_failure_degrades_to_priced_horizon():
    now = _NOW
    config = battery_configuration()
    config["sources"]["load"] = LoadSource(
        "recorder", statistic_id="sensor.household", history_unit="kWh"
    ).to_dict()
    config["settings"].update(
        strategy="cost_min",
        autonomy_reserve=True,
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
        allow_fallback=False,
    )
    rows = [_flat_row(now, 48, 0.0)]
    states = {**_soc_state(now), **_with_pv(config, now, rows)}
    statistics = (
        {"start": (now - timedelta(hours=49)).isoformat(), "sum": 0},
        {"start": (now - timedelta(hours=48)).isoformat(), "sum": 1},
        {"start": (now - timedelta(hours=25)).isoformat(), "sum": 0},
        {"start": (now - timedelta(hours=24)).isoformat(), "sum": 1},
    )
    problem, _, quality = build_problem(config, states, now, statistics=statistics)
    assert "autonomy_tail_unavailable" in quality["warnings"]
    assert len(problem.soc_target_kwh) == len(problem.slots)


def test_autonomy_floor_absent_for_cost_min_by_default():
    now = _NOW
    config = battery_configuration()
    rows = [_flat_row(now, 48, 0.0)]
    states = {**_soc_state(now), **_with_pv(config, now, rows)}
    problem, _, quality = build_problem(config, states, now)
    assert problem.soc_target_kwh == ()
    assert problem.soc_target_window == ()
    assert problem.soc_target_weight == 0.0
    assert "autonomy_floor_requires_pv" not in quality["warnings"]


def test_backup_ready_raises_floor_without_merging_windows():
    now = _NOW
    base = battery_configuration()
    base["settings"].update(
        horizon_hours=24, display_horizon_hours=24, reference_horizon_hours=24
    )
    rows = [_flat_row(now, 30, 0.0), _flat_row(now + timedelta(hours=30), 18, 5.0)]

    self_sufficiency_config = base
    self_sufficiency_config["settings"]["strategy"] = "self_sufficiency"
    states = {**_soc_state(now), **_with_pv(self_sufficiency_config, now, rows)}
    baseline, _, _ = build_problem(self_sufficiency_config, states, now)

    import copy

    backup_config = copy.deepcopy(base)
    backup_config["settings"]["strategy"] = "backup_ready"
    backup_config["settings"]["backup_target_soc_percent"] = 90
    states2 = {**_soc_state(now), **_with_pv(backup_config, now, rows)}
    raised, _, _ = build_problem(backup_config, states2, now)

    assert raised.soc_target_window == baseline.soc_target_window
    assert all(r >= b for r, b in zip(raised.soc_target_kwh, baseline.soc_target_kwh))
    assert any(r > b for r, b in zip(raised.soc_target_kwh, baseline.soc_target_kwh))


def test_grid_charge_ceiling_below_autonomy_weight_warns():
    now = _NOW
    config = battery_configuration()
    config["settings"].update(
        strategy="self_sufficiency",
        limit_grid_charge_price=True,
        maximum_grid_charge_price=0.0,
        buy_rate=5.0,
        autonomy_margin_per_kwh=0.10,
    )
    rows = [_flat_row(now, 48, 0.0)]
    states = {**_soc_state(now), **_with_pv(config, now, rows)}
    _, _, quality = build_problem(config, states, now)
    assert "grid_charge_ceiling_below_autonomy_weight" in quality["warnings"]


def test_load_quality_unchanged_when_autonomy_enabled():
    now = _NOW
    base = battery_configuration()
    rows = [_flat_row(now, 48, 0.0)]

    off_config = base
    states_off = {**_soc_state(now), **_with_pv(off_config, now, rows)}
    _, _, quality_off = build_problem(off_config, states_off, now)

    import copy

    on_config = copy.deepcopy(base)
    on_config["settings"]["autonomy_reserve"] = True
    states_on = {**_soc_state(now), **_with_pv(on_config, now, rows)}
    _, _, quality_on = build_problem(on_config, states_on, now)

    assert quality_on["load"] == quality_off["load"]


# --- Commitment release ---------------------------------------------------


def _committed_config(now, *, released):
    config = battery_configuration()
    config["settings"]["minimum_mode_minutes"] = 120
    if released:
        config["strategy_changed_at"] = now.isoformat()
    return config


def test_strategy_change_clears_the_mode_commitment():
    now = _NOW
    config = _committed_config(now, released=True)
    states = _soc_state(now)
    commitment = {
        "mode": "CHARGE_GRID",
        "since": (now - timedelta(minutes=10)).isoformat(),
    }
    problem, _, _ = build_problem(config, states, now, battery_commitment=commitment)
    assert problem.initial_dispatch_mode is None
    assert problem.initial_dispatch_mode_since is None
    assert problem.initial_battery_mode is None
    assert problem.initial_battery_mode_since is None
    assert problem.strategy_changed is True


def test_release_produces_no_safety_exception():
    now = _NOW
    states = {"sensor.soc": {"state": "90", "attributes": {}, "last_updated": now}}
    commitment = {
        "mode": "CHARGE_GRID",
        "since": (now - timedelta(minutes=10)).isoformat(),
    }

    not_released = _committed_config(now, released=False)
    not_released["settings"]["soc_ceiling"] = 90
    still_locked = compute(not_released, states, now, battery_commitment=commitment)
    assert still_locked["dispatch_policy"]["safety_exception"] is not None

    released = _committed_config(now, released=True)
    released["settings"]["soc_ceiling"] = 90
    freed = compute(released, states, now, battery_commitment=commitment)
    assert freed["dispatch_policy"]["safety_exception"] is None


def test_release_keeps_the_export_episode_commitment():
    now = _NOW
    config = _committed_config(now, released=True)
    states = _soc_state(now)
    export_commitment = {
        "generated_at": (now - timedelta(minutes=30)).isoformat(),
        "until": (now + timedelta(hours=1)).isoformat(),
    }
    problem, _, _ = build_problem(
        config, states, now, export_commitment=export_commitment
    )
    assert problem.initial_export_active is True


def test_without_a_stamp_the_commitment_is_carried():
    now = _NOW
    config = _committed_config(now, released=False)
    states = _soc_state(now)
    commitment = {
        "mode": "CHARGE_GRID",
        "since": (now - timedelta(minutes=10)).isoformat(),
    }
    problem, _, _ = build_problem(config, states, now, battery_commitment=commitment)
    assert problem.initial_dispatch_mode == "CHARGE_GRID"
    assert problem.initial_dispatch_mode_since == now - timedelta(minutes=10)
    assert problem.strategy_changed is False


def test_price_ceiling_conflict_after_release_does_not_pause():
    now = _NOW
    config = _committed_config(now, released=True)
    config["settings"].update(
        limit_grid_charge_price=True, maximum_grid_charge_price=-1000
    )
    states = _soc_state(now)
    commitment = {
        "mode": "CHARGE_GRID",
        "since": (now - timedelta(minutes=10)).isoformat(),
    }
    result = compute(config, states, now, battery_commitment=commitment)
    assert result["valid"] is True
    assert result["dispatch_policy"]["safety_exception"] is None


def test_result_carries_strategy_and_metrics():
    now = _NOW
    config = default_configuration("EUR", "UTC")
    config["settings"]["strategy"] = "self_sufficiency"
    result = compute(config, {}, now)
    assert result["strategy"] == "self_sufficiency"
    assert result["autonomy_shortfall_kwh"] == 0.0
    assert result["cap_violation_kwh"] == 0.0
    assert result["strategy_released"] is False


def test_peak_reserve_absent_by_default():
    config = battery_configuration()
    problem, _, _ = build_problem(config, _soc_state(_NOW), _NOW)
    assert problem.peak_reserve_kwh == ()
    assert problem.peak_reserve_window == ()
    assert problem.peak_reserve_weight == 0.0


def test_peak_reserve_flat_tariff_warns_and_adds_no_floor():
    config = battery_configuration()
    config["settings"].update(peak_reserve_margin_kwh=0.5)
    problem, _, quality = build_problem(config, _soc_state(_NOW), _NOW)
    assert problem.peak_reserve_kwh == ()
    assert problem.peak_reserve_weight == 0.0
    assert "peak_reserve_no_expensive_period" in quality["warnings"]


def test_peak_reserve_uses_grid_charge_ceiling_as_threshold():
    config = battery_configuration()
    config["settings"].update(
        peak_reserve_margin_kwh=0.5,
        limit_grid_charge_price=True,
        maximum_grid_charge_price=-1000,
    )
    problem, _, quality = build_problem(config, _soc_state(_NOW), _NOW)
    assert any(slot.load_kwh > slot.pv_kwh for slot in problem.slots)
    assert problem.peak_reserve_weight > 0
    assert set(problem.peak_reserve_window) == {0}
    reserve = problem.battery.capacity_kwh * problem.battery.minimum_soc_fraction
    assert problem.peak_reserve_kwh[-1] == pytest.approx(reserve + 0.5)
    assert "peak_reserve_no_expensive_period" not in quality["warnings"]
