"""Net-billing settlement inputs and factors at the runtime level."""

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
        "deposit_model": "manual",
        "deposit_refund_percent": 30,
        "export_value_factor": 0.2,
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
    config = dynamic_config(deposit_model="manual", export_value_factor=0.2)
    problem, _, _ = build_problem(config, states, NOW)
    by_hour = {slot.start.hour: slot for slot in problem.slots}
    midday, evening, night = by_hour[10], by_hour[17], by_hour[23]
    assert [slot.buy_per_kwh for slot in (midday, evening, night)] == pytest.approx(
        [0.79, 1.49, 0.593]
    )
    assert [slot.buy_energy_per_kwh for slot in (midday, evening, night)] == (
        pytest.approx([0.25, 0.95, 0.45])
    )
    # pi = 1 (factor 0.2 is below the 0.244 floor of rho 30 %): only the distribution
    # part is left to pay.
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
    config = dynamic_config(deposit_model="manual", export_value_factor=0.2)
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
    share = 0.30 / 1.23
    assert problem.export_value_factor == pytest.approx(0.65)
    assert problem.import_energy_factor == pytest.approx(1 - 0.35 / (1 - share))


def test_the_manual_floor_is_the_refund_percent_over_1_23():
    for percent, floor in ((30, 0.2439), (20, 0.1626)):
        problem, _, _ = build_problem(
            fixed_config(
                deposit_model="manual",
                deposit_refund_percent=percent,
                export_value_factor=floor,
            ),
            {},
            NOW,
        )
        assert problem.import_energy_factor == pytest.approx(0, abs=1e-3)
    # With 100 % the floor is 1/1.23 = 0.813, not 1: 0.9 is above it, 0.8 is below it.
    problem, _, _ = build_problem(
        fixed_config(
            deposit_model="manual",
            deposit_refund_percent=100,
            export_value_factor=0.9,
        ),
        {},
        NOW,
    )
    assert problem.import_energy_factor == pytest.approx(1 - 0.1 / (1 - 1 / 1.23))
    problem, _, _ = build_problem(
        fixed_config(
            deposit_model="manual",
            deposit_refund_percent=100,
            export_value_factor=0.8,
        ),
        {},
        NOW,
    )
    assert problem.import_energy_factor == 0


def values_for(**changes):
    config = fixed_config(**settings(**changes))
    return build_problem(config, {}, NOW)[1]


def test_off_model_reports_off_and_is_the_default():
    result = resolve_settlement(values_for(deposit_model="off"))
    assert result["reason"] == "off"
    assert result["model"] == "off"
    assert result["cap_pressure"] == 0
    assert result["export_value_factor"] == result["import_energy_factor"] == 1


def test_a_stored_auto_model_is_read_as_off():
    values = values_for(deposit_model="auto")
    assert values["deposit_model"] == "off"
    problem, _, _ = build_problem(
        fixed_config(deposit_model="auto", deposit_yearly_value=2800), {}, NOW
    )
    assert problem.export_value_factor == problem.import_energy_factor == 1.0
    result = compute(fixed_config(deposit_model="auto"), {}, NOW)
    assert result["dispatch_policy"]["settlement"]["model"] == "off"
    assert result["dispatch_policy"]["settlement"]["reason"] == "off"
    assert result["status"] != "invalid_input"


def test_an_unknown_deposit_model_is_still_rejected():
    with pytest.raises(InputError, match="invalid deposit_model"):
        build_problem(fixed_config(deposit_model="other"), {}, NOW)


def test_the_withdrawn_settings_are_not_settings():
    from custom_components.energy_compass.settings import NUMBERS

    for key in ("deposit_yearly_value", "deposit_yearly_energy_cost"):
        assert key not in NUMBERS


def test_diagnostics_keys_and_reasons_after_the_withdrawal():
    diagnostics = compute(fixed_config(**settings()), {}, NOW)["dispatch_policy"][
        "settlement"
    ]
    assert set(diagnostics) == {
        "model",
        "export_value_factor",
        "import_energy_factor",
        "cap_pressure",
        "refund_percent",
        "distribution_mode",
        "reason",
    }
    assert diagnostics["model"] == "manual"
    assert diagnostics["reason"] == "ok"
    assert diagnostics["cap_pressure"] == 1.0


def test_compute_publishes_no_settlement_state():
    for config in (fixed_config(), fixed_config(**settings())):
        assert "settlement_state" not in compute(config, {}, NOW)


def test_compute_with_the_model_off_reports_invoice_factors():
    diagnostics = compute(fixed_config(), {}, NOW)["dispatch_policy"]["settlement"]
    assert diagnostics["reason"] == "off"
    assert (
        diagnostics["export_value_factor"] == diagnostics["import_energy_factor"] == 1
    )


def test_reported_cost_stays_at_invoice_prices_when_the_cap_binds():
    config = fixed_config(buy_distribution_rate=0.4, **settings())
    result = compute(config, {}, NOW)
    off = compute(fixed_config(buy_distribution_rate=0.4), {}, NOW)
    assert result["intervals"][0]["buy_per_kwh"] == 1.0
    assert off["intervals"][0]["buy_per_kwh"] == 1.0
    # The planner may move energy around, but every interval is priced at the invoice.
    assert {row["buy_per_kwh"] for row in result["intervals"]} == {1.0}


def test_a_binding_cap_without_a_distribution_part_is_reported_as_split_missing():
    config = fixed_config(buy_distribution_rate=0, **settings())
    problem, _, _ = build_problem(config, {}, NOW)
    assert problem.import_energy_factor == 1.0
    assert problem.export_value_factor == pytest.approx(0.2)
    diagnostics = compute(config, {}, NOW)["dispatch_policy"]["settlement"]
    assert diagnostics["reason"] == "split_missing"
    assert diagnostics["import_energy_factor"] == 1.0
    assert diagnostics["cap_pressure"] == 1.0
    # With the factor at 1 there is nothing to force, so the reason stays ok.
    short = fixed_config(buy_distribution_rate=0, **settings(export_value_factor=1.0))
    assert compute(short, {}, NOW)["dispatch_policy"]["settlement"]["reason"] == "ok"


def test_manual_export_factor_is_not_clamped_by_the_refund_floor():
    # Setting 0.2 is below the floor 0.30 / 1.23 = 0.244: pi clamps to 1, phi_x stays 0.2.
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
