"""Net-billing settlement inputs, factors and hysteresis at the runtime level."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.config_models import NumericSetting, PriceSource
from custom_components.energy_compass.runtime import build_problem, compute
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.settlement import resolve_settlement
from custom_components.energy_compass.sources.tariffs import TariffSchedule

NOW = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
ZONE = "Europe/Warsaw"


def g12_config(**changes):
    config = default_configuration("PLN", ZONE)
    config["sources"]["buy"] = PriceSource(
        "schedule", schedule=TariffSchedule.default("pge_g12")
    ).to_dict()
    config["settings"].update(
        {
            "buy_rate": 0.8,
            "buy_off_peak_rate": 0.4,
            "buy_energy_rate": 0.5,
            "buy_off_peak_energy_rate": 0.3,
            "horizon_hours": 24,
            "display_horizon_hours": 24,
            "reference_horizon_hours": 24,
            **changes,
        }
    )
    return config


def fixed_config(**settings):
    config = default_configuration("PLN", ZONE)
    config["sources"]["buy"] = PriceSource(
        "fixed", fixed=NumericSetting(fixed=0, unit="PLN/kWh")
    ).to_dict()
    config["settings"].update(
        horizon_hours=6,
        display_horizon_hours=6,
        reference_horizon_hours=6,
        **settings,
    )
    return config


def settings(**changes):
    base = {
        "deposit_model": "auto",
        "deposit_refund_percent": 30,
        "export_value_factor": 1.0,
        "deposit_yearly_value": 2800,
        "deposit_yearly_energy_cost": 2150,
    }
    return {**base, **changes}


def test_defaults_leave_the_problem_on_invoice_prices():
    problem, values, _ = build_problem(
        g12_config(buy_energy_rate=0, buy_off_peak_energy_rate=0), {}, NOW
    )
    assert values["deposit_model"] == "off"
    assert problem.export_value_factor == problem.import_energy_factor == 1.0
    assert all(slot.buy_energy_per_kwh == 0 for slot in problem.slots)


def test_energy_part_follows_the_band_multiplier_and_vat_but_not_the_addition():
    config = g12_config(
        buy_multiplier=1.1, buy_apply_vat=True, vat_percent=23, buy_addition=0.05
    )
    problem, _, _ = build_problem(config, {}, NOW)
    factor = 1.1 * 1.23
    by_hour = {slot.start.hour: slot for slot in problem.slots}
    day, night = by_hour[11], by_hour[1]
    assert day.buy_energy_per_kwh == pytest.approx(0.5 * factor)
    assert night.buy_energy_per_kwh == pytest.approx(0.3 * factor)
    assert day.buy_per_kwh == pytest.approx(0.8 * factor + 0.05)
    assert night.buy_per_kwh == pytest.approx(0.4 * factor + 0.05)


def test_g11_and_fixed_use_one_energy_rate_for_every_slot():
    config = g12_config()
    config["sources"]["buy"] = PriceSource(
        "schedule", schedule=TariffSchedule.default("g11")
    ).to_dict()
    problem, _, _ = build_problem(config, {}, NOW)
    assert {round(slot.buy_energy_per_kwh, 6) for slot in problem.slots} == {0.5}
    problem, _, _ = build_problem(
        fixed_config(buy_rate=1.0, buy_energy_rate=0.6, buy_multiplier=2), {}, NOW
    )
    assert {round(slot.buy_energy_per_kwh, 6) for slot in problem.slots} == {1.2}


def test_the_energy_part_is_capped_by_the_buy_price_of_the_slot():
    problem, _, _ = build_problem(
        fixed_config(buy_rate=0.2, buy_energy_rate=0.9), {}, NOW
    )
    assert {slot.buy_energy_per_kwh for slot in problem.slots} == {0.2}


def test_manual_model_sets_both_factors_from_the_stated_value():
    problem, _, _ = build_problem(
        fixed_config(deposit_model="manual", export_value_factor=0.65), {}, NOW
    )
    assert problem.export_value_factor == pytest.approx(0.65)
    assert problem.import_energy_factor == pytest.approx(0.5)


def test_auto_model_derives_the_factors_from_the_yearly_inputs():
    problem, _, _ = build_problem(fixed_config(**settings()), {}, NOW)
    assert problem.export_value_factor == pytest.approx(0.62, abs=0.01)
    assert problem.import_energy_factor == pytest.approx(0.45, abs=0.01)


def values_for(**changes):
    config = fixed_config(**settings(**changes))
    return build_problem(config, {}, NOW)[1]


def test_missing_inputs_leave_the_model_off_without_state():
    for changes in ({"deposit_yearly_value": 0}, {"deposit_yearly_energy_cost": 0}):
        result, state = resolve_settlement(values_for(**changes), None, NOW, ZONE)
        assert result["reason"] == "inputs_missing"
        assert result["cap_pressure"] == 0
        assert result["export_value_factor"] == result["import_energy_factor"] == 1
        assert state is None


def test_off_model_reports_off_and_clears_the_state():
    result, state = resolve_settlement(
        values_for(deposit_model="off"), {"anything": 1}, NOW, ZONE
    )
    assert result["reason"] == "off"
    assert result["model"] == "off"
    assert state is None


def test_pressure_is_evaluated_once_per_local_day_and_held():
    values = values_for()
    first, state = resolve_settlement(values, None, NOW, ZONE)
    assert first["reason"] == "ok"
    assert first["evaluated_on"] == "2026-07-01"
    later = NOW + timedelta(hours=5)
    held, held_state = resolve_settlement(values, state, later, ZONE)
    assert held["reason"] == "held"
    assert held["cap_pressure"] == first["cap_pressure"]
    assert held_state == state


def test_local_midnight_not_utc_midnight_starts_a_new_day():
    values = values_for()
    # 22:30 UTC is already 00:30 on 2 July in Warsaw (summer time, UTC+2).
    evening = datetime(2026, 7, 1, 21, 30, tzinfo=UTC)
    _, state = resolve_settlement(values, None, evening - timedelta(hours=2), ZONE)
    result, _ = resolve_settlement(values, state, evening, ZONE)
    assert result["reason"] == "held"
    result, _ = resolve_settlement(values, state, evening + timedelta(hours=1), ZONE)
    assert result["reason"] == "ok"
    assert result["evaluated_on"] == "2026-07-02"


def test_dead_band_keeps_a_small_daily_move_and_takes_a_large_one():
    _, state = resolve_settlement(values_for(), None, NOW, ZONE)
    held_pressure = state["cap_pressure"]
    tomorrow = NOW + timedelta(days=1)
    # A 3 % rise in D moves π by well under 0.10: held.
    small = values_for(deposit_yearly_value=2800)
    state["inputs"] = {**state["inputs"]}
    result, new_state = resolve_settlement(small, state, tomorrow, ZONE)
    assert result["cap_pressure"] == held_pressure
    assert new_state["evaluated_on"] == "2026-07-02"
    # π jumps to 1: replaces the held value.
    big = values_for(deposit_yearly_value=3300)
    result, new_state = resolve_settlement(big, state, tomorrow, ZONE)
    assert result["cap_pressure"] == 1.0
    assert new_state["cap_pressure"] == 1.0


def test_an_inside_the_band_candidate_is_held_on_the_next_day():
    values = values_for()
    _, state = resolve_settlement(values, None, NOW, ZONE)
    # Same inputs, yesterday's state with a slightly different held π.
    state = {**state, "cap_pressure": state["cap_pressure"] + 0.04}
    result, _ = resolve_settlement(values, state, NOW + timedelta(days=1), ZONE)
    assert result["cap_pressure"] == pytest.approx(state["cap_pressure"])


def test_changing_an_input_re_evaluates_at_once_without_the_dead_band():
    _, state = resolve_settlement(values_for(), None, NOW, ZONE)
    changed = values_for(deposit_yearly_value=2900)
    result, new_state = resolve_settlement(
        changed, state, NOW + timedelta(hours=1), ZONE
    )
    assert result["reason"] == "ok"
    assert result["cap_pressure"] != state["cap_pressure"]
    assert new_state["inputs"]["yearly_deposit"] == 2900


def test_a_malformed_held_state_is_ignored():
    for held in ({}, {"inputs": {}}, {"cap_pressure": "x", "evaluated_on": "y"}):
        result, _ = resolve_settlement(values_for(), held, NOW, ZONE)
        assert result["reason"] == "ok"


def test_compute_publishes_the_settlement_diagnostics_and_state():
    config = fixed_config(**settings(deposit_yearly_value=3300))
    result = compute(config, {}, NOW)
    diagnostics = result["dispatch_policy"]["settlement"]
    assert diagnostics["model"] == "auto"
    assert diagnostics["cap_pressure"] == 1.0
    assert diagnostics["export_value_factor"] == pytest.approx(0.3)
    assert diagnostics["import_energy_factor"] == 0.0
    assert diagnostics["reason"] == "ok"
    assert result["settlement_state"]["cap_pressure"] == 1.0
    # A second run with the persisted state holds it.
    again = compute(
        config,
        {},
        NOW + timedelta(hours=1),
        settlement_state=result["settlement_state"],
    )
    assert again["dispatch_policy"]["settlement"]["reason"] == "held"


def test_compute_with_the_model_off_adds_no_state():
    result = compute(fixed_config(), {}, NOW)
    assert "settlement_state" not in result
    diagnostics = result["dispatch_policy"]["settlement"]
    assert diagnostics["reason"] == "off"
    assert (
        diagnostics["export_value_factor"] == diagnostics["import_energy_factor"] == 1
    )


def test_reported_cost_stays_at_invoice_prices_when_the_cap_binds():
    config = fixed_config(buy_rate=1.0, **settings(deposit_yearly_value=3300))
    config["settings"]["buy_energy_rate"] = 0.6
    result = compute(config, {}, NOW)
    off = compute(fixed_config(buy_rate=1.0, buy_energy_rate=0.6), {}, NOW)
    assert result["expected_net_cost"] == pytest.approx(off["expected_net_cost"])
    assert result["intervals"][0]["buy_per_kwh"] == 1.0
