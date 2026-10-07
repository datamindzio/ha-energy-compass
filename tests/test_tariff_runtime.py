from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.config_models import NumericSetting, PriceSource
from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.runtime import (
    build_problem,
    effective_settings,
    freshness_deadline,
)
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)
from custom_components.energy_compass.sources.prices import price_for_slots
from custom_components.energy_compass.sources.tariffs import TariffSchedule

PEAK_MESSAGE = (
    "tariff schedule needs positive buy rates: "
    "set Buy rate and Off-peak buy rate in Tariffs → Values"
)


def schedule_config(
    tariff="pge_g12", *, timezone="Europe/Warsaw", winter=False, hours=12
):
    config = default_configuration("PLN", timezone)
    schedule = TariffSchedule.default(tariff)
    schedule = TariffSchedule(tariff, winter, schedule.params)
    config["sources"]["buy"] = PriceSource("schedule", schedule=schedule).to_dict()
    config["settings"].update(
        buy_rate=0.8,
        buy_off_peak_rate=0.4,
        horizon_hours=hours,
        display_horizon_hours=hours,
        reference_horizon_hours=hours,
    )
    return config


def buy_by_start(problem):
    return {slot.start: slot.buy_per_kwh for slot in problem.slots}


def price_at(problem, instant):
    for slot in problem.slots:
        if slot.start <= instant < slot.end:
            return slot.buy_per_kwh
    raise AssertionError(instant)


NOW = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)


def test_schedule_buy_needs_no_entity_and_does_not_limit_horizon():
    config = schedule_config(hours=24)
    problem, values, _ = build_problem(config, {}, NOW)
    assert problem.slots[0].start == NOW
    assert problem.slots[-1].end == NOW + timedelta(hours=24)
    assert freshness_deadline(config, {}, values, NOW) is None


def test_schedule_transform_and_g12w_saturday():
    config = schedule_config(hours=24)
    config["settings"].update(
        buy_multiplier=1.1, buy_apply_vat=True, vat_percent=23, buy_addition=0.05
    )
    problem, _, _ = build_problem(config, {}, NOW)
    factor = 1.1 * 1.23
    peak = 0.8 * factor + 0.05
    off_peak = 0.4 * factor + 0.05
    assert price_at(problem, datetime(2026, 7, 1, 11, tzinfo=UTC)) == pytest.approx(
        peak
    )
    assert price_at(problem, datetime(2026, 7, 1, 14, tzinfo=UTC)) == pytest.approx(
        off_peak
    )
    assert price_at(problem, datetime(2026, 7, 1, 1, tzinfo=UTC)) == pytest.approx(
        off_peak
    )

    saturday = datetime(2026, 7, 4, 0, 0, tzinfo=UTC)
    weekend, _, _ = build_problem(schedule_config("pge_g12w"), {}, saturday)
    assert {slot.buy_per_kwh for slot in weekend.slots} == {0.4}


def test_g11_ignores_off_peak_rate():
    config = schedule_config("g11")
    config["settings"]["buy_off_peak_rate"] = 0
    problem, _, _ = build_problem(config, {}, NOW)
    assert {slot.buy_per_kwh for slot in problem.slots} == {0.8}


@pytest.mark.parametrize("key", ["buy_rate", "buy_off_peak_rate"])
def test_non_positive_rates_fail_closed(key):
    config = schedule_config()
    config["settings"][key] = 0
    with pytest.raises(InputError) as raised:
        build_problem(config, {}, NOW)
    assert str(raised.value) == PEAK_MESSAGE
    config["settings"][key] = -0.1
    with pytest.raises(InputError, match="positive buy rates"):
        build_problem(config, {}, NOW)


def test_g11_rejects_non_positive_peak_rate():
    config = schedule_config("g11")
    config["settings"]["buy_rate"] = 0
    with pytest.raises(InputError, match="positive buy rates"):
        build_problem(config, {}, NOW)


def test_non_whole_hour_zone_gets_edges_without_crossing_error():
    config = schedule_config("tauron_g12", timezone="Asia/Kolkata")
    problem, _, _ = build_problem(config, {}, NOW)
    starts = {slot.start for slot in problem.slots}
    assert datetime(2026, 7, 1, 0, 30, tzinfo=UTC) in starts
    assert datetime(2026, 7, 1, 7, 30, tzinfo=UTC) in starts
    assert price_at(problem, datetime(2026, 7, 1, 0, 10, tzinfo=UTC)) == 0.4
    assert price_at(problem, datetime(2026, 7, 1, 0, 40, tzinfo=UTC)) == 0.8
    assert price_at(problem, datetime(2026, 7, 1, 8, 0, tzinfo=UTC)) == 0.4


def test_winter_clock_shifts_summer_edges():
    local, _, _ = build_problem(schedule_config(hours=24), {}, NOW)
    old_meter, _, _ = build_problem(schedule_config(winter=True, hours=24), {}, NOW)
    # July afternoon window: 13-15 UTC on the local clock, 14-16 UTC on fixed CET
    for hour, minute, local_rate, winter_rate in (
        (13, 30, 0.4, 0.8),
        (15, 30, 0.8, 0.4),
        (4, 30, 0.8, 0.4),
        (20, 30, 0.4, 0.8),
    ):
        at = datetime(2026, 7, 1, hour, minute, tzinfo=UTC)
        assert price_at(local, at) == local_rate, at
        assert price_at(old_meter, at) == winter_rate, at


def forecast_states(values):
    start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    records = [
        {
            "dtime": (start + timedelta(minutes=15 * (index + 1))).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "rce_pln": value,
        }
        for index, value in enumerate(values)
    ]
    return {
        "sensor.rce": {
            "state": "1",
            "last_updated": start.isoformat(),
            "attributes": {"prices": records},
        }
    }


def rce_binding():
    return IntervalBinding(
        EntityBinding("sensor.rce", attribute="prices"),
        value_path="rce_pln",
        end_path="dtime",
        interval_minutes=15,
        unit="PLN/MWh",
        source_timezone="UTC",
    )


SLOTS = (
    (NOW, NOW + timedelta(minutes=15)),
    (NOW + timedelta(minutes=15), NOW + timedelta(minutes=30)),
)


def test_floor_applies_before_multiplier_and_addition():
    states = forecast_states([-50, 120])
    source = PriceSource(
        "forecast",
        (rce_binding(),),
        multiplier=NumericSetting(fixed=1.23),
        floor_per_kwh=0.0,
    )
    assert price_for_slots(source, states, NOW, SLOTS, "PLN") == pytest.approx(
        (0.0, 0.1476)
    )
    with_addition = PriceSource(
        "forecast",
        (rce_binding(),),
        multiplier=NumericSetting(fixed=1.23),
        addition_per_kwh=NumericSetting(fixed=0.01),
        floor_per_kwh=0.0,
    )
    assert price_for_slots(with_addition, states, NOW, SLOTS, "PLN") == pytest.approx(
        (0.01, 0.1576)
    )
    unfloored = PriceSource(
        "forecast", (rce_binding(),), multiplier=NumericSetting(fixed=1.23)
    )
    assert price_for_slots(unfloored, states, NOW, SLOTS, "PLN") == pytest.approx(
        (-0.0615, 0.1476)
    )


def test_floor_works_for_the_buy_role_too():
    states = forecast_states([-50, 120])
    buy = PriceSource("forecast", (rce_binding(),), floor_per_kwh=0.0)
    assert price_for_slots(buy, states, NOW, SLOTS, "PLN") == pytest.approx((0.0, 0.12))


def test_legacy_fixed_and_forecast_prices_are_unchanged():
    states = forecast_states([100, 100])
    forecast = PriceSource(
        "forecast",
        (rce_binding(),),
        multiplier=NumericSetting(fixed=1.23),
        addition_per_kwh=NumericSetting(fixed=0.02),
    )
    assert price_for_slots(forecast, states, NOW, SLOTS, "PLN") == pytest.approx(
        (0.143, 0.143)
    )
    fixed = PriceSource("fixed", fixed=NumericSetting(fixed=0.5, unit="PLN/kWh"))
    assert price_for_slots(fixed, {}, NOW, SLOTS, "PLN") == (0.5, 0.5)


def test_schedule_price_for_slots_requires_complete_input():
    schedule = PriceSource("schedule", schedule=TariffSchedule.default("pge_g12"))
    rates = {"peak": 1.0, "off_peak": 0.5}
    for kwargs in (
        {},
        {"timezone": "Europe/Warsaw"},
        {"schedule_rates": rates},
    ):
        with pytest.raises(InputError, match="schedule price selection"):
            price_for_slots(schedule, {}, NOW, SLOTS, "PLN", **kwargs)
    with pytest.raises(InputError, match="schedule price selection"):
        price_for_slots(
            schedule,
            {},
            NOW,
            SLOTS,
            "EUR",
            timezone="Europe/Warsaw",
            schedule_rates=rates,
        )
    assert price_for_slots(
        schedule, {}, NOW, SLOTS, "PLN", timezone="Europe/Warsaw", schedule_rates=rates
    ) == (0.5, 0.5)


def test_effective_settings_include_off_peak_rate():
    config = schedule_config()
    assert effective_settings(config, {}, NOW)["buy_off_peak_rate"] == 0.4
