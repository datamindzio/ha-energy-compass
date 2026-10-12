"""Net-billing settlement inputs, factors and hysteresis at the runtime level."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.config_models import (
    NumericSetting,
    PriceSource,
    SourceConfig,
    validate_sources,
)
from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.engine.settlement import planning_buy
from custom_components.energy_compass.runtime import build_problem, compute
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.settlement import resolve_settlement
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)
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
            "buy_distribution_rate": 0.3,
            "buy_distribution_off_peak_rate": 0.1,
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
        {
            "horizon_hours": 6,
            "display_horizon_hours": 6,
            "reference_horizon_hours": 6,
            "buy_rate": 1.0,
            "buy_distribution_rate": 0.4,
            **settings,
        }
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
        g12_config(buy_distribution_rate=0, buy_distribution_off_peak_rate=0), {}, NOW
    )
    assert values["deposit_model"] == "off"
    assert values["buy_distribution_mode"] == "included"
    assert problem.export_value_factor == problem.import_energy_factor == 1.0
    by_hour = {slot.start.hour: slot for slot in problem.slots}
    assert by_hour[11].buy_per_kwh == 0.8
    assert by_hour[1].buy_per_kwh == 0.4
    assert all(slot.buy_energy_per_kwh == slot.buy_per_kwh for slot in problem.slots)


def test_included_distribution_is_split_off_and_follows_vat_only():
    config = g12_config(
        buy_multiplier=1.1, buy_apply_vat=True, vat_percent=23, buy_addition=0.05
    )
    problem, _, _ = build_problem(config, {}, NOW)
    factor = 1.1 * 1.23
    by_hour = {slot.start.hour: slot for slot in problem.slots}
    day, night = by_hour[11], by_hour[1]
    # The buy source keeps today's meaning: the all-in rate with multiplier, VAT, addition.
    assert day.buy_per_kwh == pytest.approx(0.8 * factor + 0.05)
    assert night.buy_per_kwh == pytest.approx(0.4 * factor + 0.05)
    # Distribution takes VAT only; the multiplier and the addition never apply to it.
    assert day.buy_energy_per_kwh == pytest.approx(0.8 * factor + 0.05 - 0.3 * 1.23)
    assert night.buy_energy_per_kwh == pytest.approx(0.4 * factor + 0.05 - 0.1 * 1.23)


def test_added_distribution_raises_the_buy_price_without_the_deposit_model():
    config = g12_config(buy_distribution_mode="added")
    problem, values, _ = build_problem(config, {}, NOW)
    assert values["deposit_model"] == "off"
    by_hour = {slot.start.hour: slot for slot in problem.slots}
    assert by_hour[11].buy_per_kwh == pytest.approx(0.8 + 0.3)
    assert by_hour[1].buy_per_kwh == pytest.approx(0.4 + 0.1)
    assert by_hour[11].buy_energy_per_kwh == pytest.approx(0.8)
    assert by_hour[1].buy_energy_per_kwh == pytest.approx(0.4)
    # Planner and reported cost alike see the invoice price.
    result = compute(config, {}, NOW)
    assert result["intervals"][0]["buy_per_kwh"] == pytest.approx(0.4 + 0.1)
    plain = compute(g12_config(), {}, NOW)
    assert result["expected_net_cost"] > plain["expected_net_cost"]


def test_g11_and_fixed_use_one_distribution_rate_for_every_slot():
    config = g12_config()
    config["sources"]["buy"] = PriceSource(
        "schedule", schedule=TariffSchedule.default("g11")
    ).to_dict()
    problem, _, _ = build_problem(config, {}, NOW)
    assert {round(slot.buy_energy_per_kwh, 6) for slot in problem.slots} == {0.5}
    problem, _, _ = build_problem(
        fixed_config(buy_rate=1.0, buy_distribution_rate=0.6, buy_multiplier=2),
        {},
        NOW,
    )
    assert {round(slot.buy_energy_per_kwh, 6) for slot in problem.slots} == {1.4}
    assert {slot.buy_per_kwh for slot in problem.slots} == {2.0}


def test_the_energy_part_is_never_negative_and_capped_by_the_buy_price():
    problem, _, _ = build_problem(
        fixed_config(buy_rate=0.2, buy_distribution_rate=0.9), {}, NOW
    )
    assert {slot.buy_energy_per_kwh for slot in problem.slots} == {0.0}
    problem, _, _ = build_problem(
        fixed_config(buy_rate=-0.1, buy_distribution_rate=0.0), {}, NOW
    )
    assert {slot.buy_energy_per_kwh for slot in problem.slots} == {0.0}


def test_two_zones_need_both_distribution_rates_or_neither():
    for peak, off_peak in ((0.5, 0), (0, 0.2)):
        with pytest.raises(InputError, match="both rates or neither"):
            build_problem(
                g12_config(
                    buy_distribution_rate=peak, buy_distribution_off_peak_rate=off_peak
                ),
                {},
                NOW,
            )
    build_problem(
        g12_config(buy_distribution_rate=0, buy_distribution_off_peak_rate=0), {}, NOW
    )


def _energy_prices(hours):
    """RCE-style quarter-hour records starting at NOW, PLN/MWh by UTC hour."""
    return {
        "sensor.energy_price": {
            "state": "1",
            "last_updated": NOW.isoformat(),
            "attributes": {
                "prices": [
                    {
                        "dtime": (NOW + timedelta(minutes=15 * (index + 1))).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "price": hours.get(index // 4, 500) * 1000,
                    }
                    for index in range(96)
                ]
            },
        }
    }


def dynamic_config(**settings):
    """Dynamic energy price (forecast) plus a G12 distribution tariff, added on top."""
    config = default_configuration("PLN", ZONE)
    config["sources"]["buy"] = PriceSource(
        "forecast",
        (
            IntervalBinding(
                EntityBinding("sensor.energy_price", attribute="prices"),
                value_path="price",
                end_path="dtime",
                interval_minutes=15,
                unit="PLN/MWh",
                value_kind="price",
                source_timezone="UTC",
            ),
        ),
        distribution_schedule=TariffSchedule.default("pge_g12"),
    ).to_dict()
    config["settings"].update(
        {
            "horizon_hours": 24,
            "display_horizon_hours": 24,
            "reference_horizon_hours": 24,
            "buy_distribution_mode": "added",
            "buy_distribution_rate": 0.540,
            "buy_distribution_off_peak_rate": 0.143,
            **settings,
        }
    )
    return config


def test_dynamic_price_with_a_g12_distribution_tariff_follows_the_adr_example():
    # UTC hours: 10 = 12:00 local midday, 17 = 19:00 evening peak, 23 = 01:00 night.
    states = _energy_prices({10: 0.25, 17: 0.95, 23: 0.45})
    config = dynamic_config(deposit_model="manual", export_value_factor=0.3)
    problem, _, _ = build_problem(config, states, NOW)
    by_hour = {slot.start.hour: slot for slot in problem.slots}
    midday, evening, night = by_hour[10], by_hour[17], by_hour[23]
    assert [slot.buy_per_kwh for slot in (midday, evening, night)] == pytest.approx(
        [0.79, 1.49, 0.593]
    )
    assert [slot.buy_energy_per_kwh for slot in (midday, evening, night)] == (
        pytest.approx([0.25, 0.95, 0.45])
    )
    # pi = 1 (factor 0.3 at rho 30 %): only the distribution part is left to pay.
    assert problem.import_energy_factor == 0
    assert [
        planning_buy(problem, slot) for slot in (midday, evening, night)
    ] == pytest.approx([0.540, 0.540, 0.143])


def test_distribution_band_edges_become_slot_boundaries():
    # PGE G12 in summer: 15:00-17:00 local (13:00-15:00 UTC) is off-peak.
    states = _energy_prices({})
    problem, _, _ = build_problem(dynamic_config(), states, NOW)
    starts = {slot.start for slot in problem.slots}
    assert datetime(2026, 7, 1, 13, tzinfo=UTC) in starts
    assert datetime(2026, 7, 1, 15, tzinfo=UTC) in starts


def test_a_negative_energy_price_keeps_its_real_price_when_the_cap_binds():
    states = _energy_prices({10: -0.10})
    config = dynamic_config(deposit_model="manual", export_value_factor=0.3)
    problem, _, _ = build_problem(config, states, NOW)
    slot = {slot.start.hour: slot for slot in problem.slots}[10]
    assert slot.buy_per_kwh == pytest.approx(0.44)
    assert slot.buy_energy_per_kwh == 0
    assert planning_buy(problem, slot) == pytest.approx(0.44)


def test_a_schedule_source_rejects_a_distribution_tariff_of_its_own():
    config = g12_config()
    config["sources"]["buy"]["distribution_schedule"] = TariffSchedule.default(
        "pge_g12"
    ).to_dict()
    with pytest.raises(InputError, match="own zones"):
        validate_sources(SourceConfig.from_dict(config["sources"]), set())


def test_the_distribution_tariff_round_trips_and_is_only_stored_when_set():
    plain = PriceSource("fixed", fixed=NumericSetting(fixed=1, unit="PLN/kWh"))
    assert "distribution_schedule" not in plain.to_dict()
    config = dynamic_config()["sources"]["buy"]
    restored = PriceSource.from_dict(config)
    assert restored.distribution_schedule == TariffSchedule.default("pge_g12")
    assert restored.to_dict() == config


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
    config = fixed_config(
        buy_distribution_rate=0.4, **settings(deposit_yearly_value=3300)
    )
    result = compute(config, {}, NOW)
    off = compute(fixed_config(buy_distribution_rate=0.4), {}, NOW)
    assert result["intervals"][0]["buy_per_kwh"] == 1.0
    assert off["intervals"][0]["buy_per_kwh"] == 1.0
    # The planner may move energy around, but every interval is priced at the invoice.
    assert {row["buy_per_kwh"] for row in result["intervals"]} == {1.0}


def test_a_binding_cap_without_a_distribution_part_is_reported_as_split_missing():
    config = fixed_config(
        buy_distribution_rate=0, **settings(deposit_yearly_value=3300)
    )
    problem, _, _ = build_problem(config, {}, NOW)
    assert problem.import_energy_factor == 1.0
    assert problem.export_value_factor == pytest.approx(0.3)
    diagnostics = compute(config, {}, NOW)["dispatch_policy"]["settlement"]
    assert diagnostics["reason"] == "split_missing"
    assert diagnostics["import_energy_factor"] == 1.0
    assert diagnostics["cap_pressure"] == 1.0
    # With the model short there is nothing to force, so the reason stays ok.
    short = fixed_config(buy_distribution_rate=0, **settings(deposit_yearly_value=2000))
    assert compute(short, {}, NOW)["dispatch_policy"]["settlement"]["reason"] == "ok"


def test_manual_export_factor_is_not_clamped_by_the_refund_cap():
    # Setting 0.2 is below the cap's floor of 0.3: pi clamps to 1, phi_x stays 0.2.
    problem, _, _ = build_problem(
        fixed_config(deposit_model="manual", export_value_factor=0.2), {}, NOW
    )
    assert problem.export_value_factor == pytest.approx(0.2)
    assert problem.import_energy_factor == 0.0


def test_the_distribution_mode_is_published_with_the_settlement():
    diagnostics = compute(fixed_config(buy_distribution_mode="added"), {}, NOW)[
        "dispatch_policy"
    ]["settlement"]
    assert diagnostics["distribution_mode"] == "added"
