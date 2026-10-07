from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.config_models import (
    LoadSource,
    NumericSetting,
    PriceSource,
)
from custom_components.energy_compass.engine.models import ForecastSettings, InputError
from custom_components.energy_compass.sources import history as load_history
from custom_components.energy_compass.sources.battery import (
    SocSettings,
    SocTracker,
    validate_soc,
)
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)
from custom_components.energy_compass.sources.history import (
    counter_statistics_to_hours,
    load_for_slots,
    power_samples_to_hours,
    recorder_statistics_to_hours,
)
from custom_components.energy_compass.sources.prices import (
    price_for_slots,
    price_series,
    raw_rce_prices,
)
from custom_components.energy_compass.sources.pv import pv_series, sum_pv_arrays

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def test_explicit_daily_estimate_and_supplied_forecast_have_distinct_coverage():
    slots = ((NOW, NOW + timedelta(minutes=30)),)
    daily_source = LoadSource("daily_estimate", daily_estimate=NumericSetting(fixed=24))
    daily = load_history.load_for_slots_with_quality(
        daily_source,
        {},
        NOW,
        slots,
        "UTC",
        ForecastSettings(),
    )
    assert (
        daily.values
        == load_for_slots(daily_source, {}, NOW, slots, "UTC", ForecastSettings())
        == (0.5,)
    )
    assert [
        (row.method, row.samples_available, row.samples_required)
        for row in daily.coverage
    ] == [("daily_estimate", None, None)]
    binding = IntervalBinding(
        EntityBinding("sensor.source", attribute="forecast"),
        start_path="start",
        end_path="end",
        value_path="load",
    )
    source = LoadSource("forecast", forecast=binding)
    states = snapshot(
        [
            {
                "start": NOW.isoformat(),
                "end": (NOW + timedelta(hours=1)).isoformat(),
                "load": 2,
            }
        ]
    )
    result = load_history.load_for_slots_with_quality(
        source,
        states,
        NOW,
        slots,
        "UTC",
        ForecastSettings(),
    )
    assert (
        result.values
        == load_for_slots(source, states, NOW, slots, "UTC", ForecastSettings())
        == (1,)
    )
    assert [
        (row.method, row.samples_available, row.samples_required)
        for row in result.coverage
    ] == [("forecast", None, None)]


def snapshot(value, attribute="forecast"):
    return {
        "sensor.source": {
            "state": "10",
            "last_updated": NOW.isoformat(),
            "attributes": {attribute: value},
        }
    }


def test_generic_price_array_and_timestamp_map():
    records = [
        {"start": "2026-09-17T12:00:00+00:00", "value": -0.2},
        {"start": "2026-09-17T12:30:00+00:00", "value": 0.3},
    ]
    binding = IntervalBinding(
        EntityBinding("sensor.source", attribute="forecast"),
        start_path="start",
        value_path="value",
        interval_minutes=30,
        unit="PLN/kWh",
    )
    assert [i.value for i in price_series(snapshot(records), binding, NOW)] == [
        -0.2,
        0.3,
    ]
    mapped = {r["start"]: r["value"] for r in records}
    assert [i.value for i in price_series(snapshot(mapped), binding, NOW)] == [
        -0.2,
        0.3,
    ]


def test_price_transform_applies_once_and_continuations_dedupe():
    records = [{"start": "2026-09-17T12:00:00+00:00", "value": 100}]
    binding = IntervalBinding(
        EntityBinding("sensor.source", attribute="forecast"),
        start_path="start",
        value_path="value",
        interval_minutes=60,
        unit="PLN/MWh",
    )
    result = price_series(
        snapshot(records),
        binding,
        NOW,
        multiplier=1.23,
        addition_per_kwh=0.02,
        continuations=(snapshot(records),),
    )
    assert len(result) == 1
    assert result[0].value == pytest.approx(0.143)
    assert raw_rce_prices(snapshot(records), binding, NOW)[0].value == 0.1


def test_price_conflicting_duplicate_and_missing_coverage_fail():
    binding = IntervalBinding(
        EntityBinding("sensor.source", attribute="forecast"),
        start_path="start",
        value_path="value",
        interval_minutes=60,
        unit="PLN/kWh",
    )
    first = snapshot([{"start": "2026-09-17T12:00:00+00:00", "value": 0.1}])
    second = snapshot([{"start": "2026-09-17T12:00:00+00:00", "value": 0.2}])
    with pytest.raises(InputError, match="conflict"):
        price_series(first, binding, NOW, continuations=(second,))
    with pytest.raises(InputError, match="coverage"):
        price_series(first, binding, NOW, required_slots=((NOW, NOW.replace(hour=14)),))


def test_selected_fixed_and_forecast_prices_have_explicit_currency():
    slots = ((NOW, NOW.replace(hour=13)),)
    fixed = PriceSource("fixed", fixed=NumericSetting(fixed=-0.1, unit="PLN/kWh"))
    assert price_for_slots(fixed, {}, NOW, slots, "PLN") == (-0.1,)
    with pytest.raises(InputError, match="currency"):
        price_for_slots(fixed, {}, NOW, slots, "EUR")
    record = [{"start": NOW.isoformat(), "value": 100}]
    binding = IntervalBinding(
        EntityBinding("sensor.source", attribute="forecast"),
        start_path="start",
        value_path="value",
        interval_minutes=60,
        unit="PLN/MWh",
    )
    selected = PriceSource(
        "forecast",
        forecast=(binding,),
        multiplier=NumericSetting(fixed=1.23),
        addition_per_kwh=NumericSetting(fixed=0.02),
    )
    assert price_for_slots(
        selected, snapshot(record), NOW, slots, "PLN"
    ) == pytest.approx((0.143,))


def test_record_unit_cannot_override_selected_price_currency():
    records = [{"start": NOW.isoformat(), "value": 100, "unit": "EUR/MWh"}]
    binding = IntervalBinding(
        EntityBinding("sensor.source", attribute="forecast"),
        start_path="start",
        value_path="value",
        unit_path="unit",
        interval_minutes=60,
        unit="PLN/MWh",
    )
    source = PriceSource("forecast", forecast=(binding,))
    with pytest.raises(InputError, match="currency"):
        price_for_slots(
            source, snapshot(records), NOW, ((NOW, NOW.replace(hour=13)),), "PLN"
        )
    with pytest.raises(InputError, match="currency"):
        price_series(snapshot(records), binding, NOW)


def test_stale_price_multiplier_helper_blocks_selected_price():
    records = [{"start": NOW.isoformat(), "value": 100}]
    binding = IntervalBinding(
        EntityBinding("sensor.source", attribute="forecast"),
        start_path="start",
        value_path="value",
        interval_minutes=60,
        unit="PLN/MWh",
    )
    states = snapshot(records)
    states["input_number.multiplier"] = {
        "state": "1.23",
        "last_updated": "2026-09-17T11:00:00+00:00",
        "attributes": {},
    }
    source = PriceSource(
        "forecast",
        forecast=(binding,),
        multiplier=NumericSetting(
            entity=EntityBinding("input_number.multiplier"), max_age_seconds=300
        ),
    )
    with pytest.raises(InputError, match="stale"):
        price_for_slots(source, states, NOW, ((NOW, NOW.replace(hour=13)),), "PLN")


def test_solcast_average_power_and_additive_pv_arrays():
    records = [{"period_start": "2026-09-17T12:00:00+00:00", "pv_estimate": 2.0}]
    binding = IntervalBinding(
        EntityBinding("sensor.source", attribute="forecast"),
        start_path="period_start",
        value_path="pv_estimate",
        interval_minutes=30,
        unit="kW",
        value_kind="power",
    )
    series = pv_series(snapshot(records), binding, NOW)
    assert series[0].value == 1.0
    assert sum_pv_arrays((series, series))[0].value == 2.0


def test_interval_sign_mapping_for_negative_load_template():
    records = [{"start": "2026-09-17T12:00:00+00:00", "value": -500}]
    binding = IntervalBinding(
        EntityBinding("sensor.source", attribute="forecast"),
        start_path="start",
        value_path="value",
        interval_minutes=60,
        unit="Wh",
        value_sign=-1,
    )
    assert pv_series(snapshot(records), binding, NOW)[0].value == 0.5


def test_scalar_pv_and_ambiguous_local_dst_are_rejected():
    binding = IntervalBinding(
        EntityBinding("sensor.source", attribute="forecast"),
        start_path="start",
        value_path="value",
        interval_minutes=30,
        unit="kWh",
        source_timezone="Europe/Warsaw",
    )
    with pytest.raises(InputError):
        pv_series(snapshot(4.0), binding, NOW)
    with pytest.raises(InputError, match="ambiguous"):
        pv_series(
            snapshot([{"start": "2026-10-25 02:15:00", "value": 1}]), binding, NOW
        )


def test_pse_hour24_end_and_negative_rce():
    binding = IntervalBinding(
        EntityBinding("sensor.source", attribute="forecast"),
        end_path="dtime",
        value_path="rce_pln",
        interval_minutes=15,
        unit="PLN/MWh",
        source_timezone="Europe/Warsaw",
    )
    records = [{"dtime": "2026-09-17 24:00:00", "rce_pln": -200}]
    result = raw_rce_prices(snapshot(records), binding, NOW)
    assert result[0].start.isoformat() == "2026-09-17T21:45:00+00:00"
    assert result[0].value == -0.2


def test_recorder_counter_reset_and_incomplete_hour():
    samples = [
        ("2026-09-17T10:00:00+00:00", 1042.4),
        ("2026-09-17T11:00:00+00:00", 1043.1),
        ("2026-09-17T12:00:00+00:00", 0.2),
        ("2026-09-17T13:00:00+00:00", 0.8),
        ("2026-09-17T13:30:00+00:00", 1.0),
    ]
    assert counter_statistics_to_hours(samples) == (
        (datetime(2026, 9, 17, 10, tzinfo=UTC), pytest.approx(0.7)),
        (datetime(2026, 9, 17, 12, tzinfo=UTC), pytest.approx(0.6)),
    )


def test_recorder_sum_semantics_and_selected_load_modes():
    rows = (
        {"start": "2026-09-15T09:00:00+00:00", "sum": 0},
        {"start": "2026-09-15T10:00:00+00:00", "sum": 1},
        {"start": "2026-09-16T09:00:00+00:00", "sum": 8},
        {"start": "2026-09-16T10:00:00+00:00", "sum": 11},
    )
    assert [value for _, value in recorder_statistics_to_hours(rows)] == [1.0, 3.0]
    slot = (
        (datetime(2026, 9, 17, 10, tzinfo=UTC), datetime(2026, 9, 17, 11, tzinfo=UTC)),
    )
    assert load_for_slots(
        LoadSource("recorder", statistic_id="sensor.home_energy"),
        {},
        NOW,
        slot,
        "UTC",
        ForecastSettings(minimum_samples=2),
        statistics=rows,
    ) == (2.0,)
    assert load_for_slots(
        LoadSource("daily_estimate", daily_estimate=NumericSetting(fixed=24)),
        {},
        NOW,
        slot,
        "UTC",
        ForecastSettings(),
    ) == (1.0,)


def test_power_history_integrates_quarter_hour_samples_only_when_complete():
    samples = tuple(
        (f"2026-09-16T10:{minute:02d}:00+00:00", 1000) for minute in (0, 15, 30, 45)
    ) + (("2026-09-16T11:00:00+00:00", 1000),)
    assert power_samples_to_hours(samples, unit="W") == (
        (datetime(2026, 9, 16, 10, tzinfo=UTC), 1.0),
    )
    assert power_samples_to_hours(samples[:-1], unit="W") == ()


def test_power_history_rejects_unobserved_gap_and_forwards_selected_limit():
    samples = (("2026-09-16T10:00:00+00:00", 1000), ("2026-09-16T11:00:00+00:00", 1000))
    with pytest.raises(InputError, match="gap"):
        power_samples_to_hours(samples, max_gap_minutes=30)
    source = LoadSource(
        "recorder",
        power=EntityBinding("sensor.home_power"),
        history_unit="W",
        power_max_gap_minutes=30,
    )
    with pytest.raises(InputError, match="gap"):
        load_for_slots(
            source,
            {},
            NOW,
            ((NOW, NOW.replace(hour=13)),),
            "UTC",
            ForecastSettings(),
            power_samples=samples,
        )


def test_load_forecast_mode_resamples_selected_intervals():
    records = [{"start": "2026-09-17T12:00:00+00:00", "value": 2}]
    binding = IntervalBinding(
        EntityBinding("sensor.source", attribute="forecast"),
        start_path="start",
        value_path="value",
        interval_minutes=60,
        unit="kWh",
    )
    slots = ((NOW, NOW.replace(minute=30)),)
    assert load_for_slots(
        LoadSource("forecast", forecast=binding),
        snapshot(records),
        NOW,
        slots,
        "UTC",
        ForecastSettings(),
    ) == (1.0,)


def test_soc_staleness_jump_and_bms_disagreement():
    setting = SocSettings(
        capacity_kwh=10,
        maximum_power_kw=5,
        max_age_seconds=300,
        max_disagreement_fraction=0.05,
    )
    assert validate_soc(50, "%", NOW, NOW, setting) == 5.0
    with pytest.raises(InputError, match="stale"):
        validate_soc(50, "%", NOW, NOW.replace(minute=10), setting)
    with pytest.raises(InputError, match="jump"):
        validate_soc(
            90,
            "%",
            NOW.replace(minute=1),
            NOW.replace(minute=1),
            setting,
            previous=(NOW, 5.0),
        )
    with pytest.raises(InputError, match="disagreement"):
        validate_soc(50, "%", NOW, NOW, setting, bms_kwh=6.0)


def test_soc_guard_needs_new_plausible_reading_to_recover():
    setting = SocSettings(10, 5, 300)
    guard = SocTracker(setting)
    assert guard.accept(50, "%", NOW, NOW) == 5.0
    jump_time = NOW.replace(minute=1)
    with pytest.raises(InputError, match="jump"):
        guard.accept(90, "%", jump_time, jump_time)
    with pytest.raises(InputError, match="new"):
        guard.accept(50, "%", jump_time, jump_time)
    recovery = NOW.replace(minute=2)
    assert guard.accept(50, "%", recovery, recovery) == 5.0


def _price_dicts(**overrides):
    from custom_components.energy_compass.settings import default_configuration

    config = default_configuration("PLN", "Europe/Warsaw")
    config["sources"]["buy"].update(overrides.get("buy", {}))
    config["sources"]["sell"].update(overrides.get("sell", {}))
    return config


def _validate(config):
    from custom_components.energy_compass.config_models import (
        SourceConfig,
        validate_sources,
    )

    validate_sources(SourceConfig.from_dict(config["sources"]), set())


SCHEDULE = {
    "tariff": "pge_g12",
    "meter_winter_clock": False,
    "params": {},
}
RCE_BINDING = {
    "entity": {"entity_id": "sensor.rce"},
    "unit": "PLN/MWh",
    "value_kind": "price",
}


def test_legacy_price_dicts_round_trip_byte_identically():
    from custom_components.energy_compass.settings import default_configuration

    sources = default_configuration("PLN", "Europe/Warsaw")["sources"]
    for role in ("buy", "sell"):
        assert PriceSource.from_dict(sources[role]).to_dict() == sources[role]
        assert "schedule" not in sources[role]
        assert "floor_per_kwh" not in sources[role]
    forecast = PriceSource(
        "forecast",
        (IntervalBinding(EntityBinding("sensor.rce"), unit="PLN/MWh"),),
    ).to_dict()
    assert PriceSource.from_dict(forecast).to_dict() == forecast
    assert "floor_per_kwh" not in forecast


def test_schedule_and_floor_round_trip():
    schedule = {
        "mode": "schedule",
        "forecast": [],
        "fixed": None,
        "multiplier": {"fixed": 1.0},
        "addition_per_kwh": {"fixed": 0.0},
        "schedule": SCHEDULE,
    }
    source = PriceSource.from_dict(_normalized(schedule))
    assert source.schedule.tariff == "pge_g12"
    assert source.to_dict()["schedule"] == SCHEDULE
    floored = PriceSource(
        "forecast",
        (IntervalBinding(EntityBinding("sensor.rce"), unit="PLN/MWh"),),
        floor_per_kwh=0.0,
    )
    assert floored.to_dict()["floor_per_kwh"] == 0.0
    assert PriceSource.from_dict(floored.to_dict()) == floored


def _normalized(data):
    from custom_components.energy_compass.config_models import NumericSetting

    return {
        **data,
        "multiplier": NumericSetting(fixed=1.0).to_dict(),
        "addition_per_kwh": NumericSetting(fixed=0.0).to_dict(),
    }


def _schedule_dict(**extra):
    return _normalized(
        {
            "mode": "schedule",
            "forecast": [],
            "fixed": None,
            "schedule": SCHEDULE,
            **extra,
        }
    )


def test_schedule_buy_validates_and_adds_no_dependencies():
    _validate(_price_dicts(buy=_schedule_dict()))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"sell": _schedule_dict()}, "tariff schedule is a buy source only"),
        (
            {"buy": _schedule_dict(schedule=None)},
            "schedule price needs a tariff only",
        ),
        (
            {"buy": _schedule_dict(forecast=[RCE_BINDING])},
            "schedule price needs a tariff only",
        ),
        (
            {"buy": _schedule_dict(fixed={"fixed": 1, "unit": "PLN/kWh"})},
            "schedule price needs a tariff only",
        ),
        (
            {"buy": {"schedule": SCHEDULE}},
            "tariff schedule key under a non-schedule mode",
        ),
        ({"buy": {"floor_per_kwh": 0.0}}, "price floor needs a forecast source"),
        (
            {"buy": _schedule_dict(floor_per_kwh=0.0)},
            "price floor needs a forecast source",
        ),
        (
            {"buy": _schedule_dict(schedule={**SCHEDULE, "tariff": "gone"})},
            "unknown tariff; reconfigure the buy source",
        ),
        (
            {
                "sell": {
                    "mode": "forecast",
                    "forecast": [RCE_BINDING],
                    "fixed": None,
                    "floor_per_kwh": float("nan"),
                }
            },
            "price floor",
        ),
    ],
)
def test_price_validation_errors(overrides, message):
    with pytest.raises(InputError, match=message):
        _validate(_price_dicts(**overrides))


def test_schedule_requires_pln():
    from custom_components.energy_compass.settings import default_configuration

    config = default_configuration("EUR", "Europe/Warsaw")
    config["sources"]["buy"] = _schedule_dict()
    with pytest.raises(InputError, match="tariff schedule requires PLN"):
        _validate(config)


def test_off_peak_rate_setting():
    from custom_components.energy_compass import setup_profiles as sp
    from custom_components.energy_compass.settings import (
        default_configuration,
        validate_configuration,
    )

    config = default_configuration("PLN", "Europe/Warsaw")
    assert config["settings"]["buy_off_peak_rate"] == 0
    assert "buy_off_peak_rate" in sp.TARIFF_PRICE_KEYS
    del config["settings"]["buy_off_peak_rate"]
    assert validate_configuration(config, {}, NOW)["buy_off_peak_rate"] == 0
