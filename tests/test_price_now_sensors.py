"""The buy and sell price sensors show the plan interval in force, and move with it."""

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.energy_compass.config_models import NumericSetting, PriceSource
from custom_components.energy_compass.entity import price_interval
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)
from custom_components.energy_compass.sources.tariffs import TariffSchedule

START = "2026-09-18T10:00:00+00:00"


def base_config(zone="UTC", **settings):
    config = default_configuration("PLN", zone)
    config["settings"].update(
        refresh_minutes=60,
        horizon_hours=4,
        display_horizon_hours=4,
        reference_horizon_hours=4,
        display_interval_minutes=15,
        debounce_seconds=0,
        total_time_limit_s=300,
        **settings,
    )
    return config


def forecast(entity, unit, **extra):
    return PriceSource(
        "forecast",
        (
            IntervalBinding(
                EntityBinding(entity, attribute="rows"),
                start_path="start",
                interval_minutes=15,
                value_path="price",
                unit=unit,
                value_kind="price",
            ),
        ),
        **extra,
    ).to_dict()


async def setup_entry(hass, config, title="Prices"):
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title=title, version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def plan_rows(hass, slug="prices"):
    return hass.states.get(f"sensor.{slug}_plan").attributes["intervals"]


def containing(rows, now):
    return next(
        r
        for r in rows
        if datetime.fromisoformat(r["start"]) <= now < datetime.fromisoformat(r["end"])
    )


def test_price_interval_is_half_open_and_not_the_first_row():
    data = {
        "intervals": [
            {"start": "2026-09-18T10:00:00+00:00", "end": "2026-09-18T10:15:00+00:00"},
            {"start": "2026-09-18T10:15:00+00:00", "end": "2026-09-18T10:30:00+00:00"},
        ]
    }
    inside = datetime(2026, 9, 18, 10, 20, tzinfo=UTC)
    assert price_interval(data, inside) is data["intervals"][1]
    assert (
        price_interval(data, datetime(2026, 9, 18, 10, 15, tzinfo=UTC))
        is (data["intervals"][1])
    )
    assert price_interval(data, datetime(2026, 9, 18, 10, 30, tzinfo=UTC)) is None
    assert price_interval({}, inside) is None


async def test_fixed_buy_price_and_forecast_rce_sell_price(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    config = base_config()
    config["sources"]["buy"] = PriceSource(
        "fixed", fixed=NumericSetting(fixed=0, unit="PLN/kWh")
    ).to_dict()
    config["settings"]["buy_rate"] = 0.75
    # RCE style: PLN/MWh rows, converted to PLN/kWh by the binding unit.
    config["sources"]["sell"] = forecast("sensor.rce", "PLN/MWh")
    start = dt_util.utcnow()
    hass.states.async_set(
        "sensor.rce",
        "ok",
        {
            "rows": [
                {"start": start + timedelta(minutes=15 * i), "price": 100 * (i + 1)}
                for i in range(20)
            ]
        },
    )
    await setup_entry(hass, config)
    buy = hass.states.get("sensor.prices_buy_price_now")
    sell = hass.states.get("sensor.prices_sell_price_now")
    assert float(buy.state) == pytest.approx(0.75)
    assert float(sell.state) == pytest.approx(0.1)
    assert buy.attributes["unit_of_measurement"] == "PLN/kWh"
    assert buy.attributes["state_class"] == "measurement"
    assert "device_class" not in buy.attributes
    assert sell.attributes["unit_of_measurement"] == "PLN/kWh"
    entry = hass.config_entries.async_entries("energy_compass")[0]

    # Inside the 2nd interval the sensors follow that interval, not intervals[0].
    freezer.move_to("2026-09-18T10:20:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    rows = plan_rows(hass)
    second = containing(rows, dt_util.utcnow())
    assert second["start"] == "2026-09-18T10:15:00+00:00"
    assert float(hass.states.get("sensor.prices_sell_price_now").state) == (
        pytest.approx(second["sell_per_kwh"])
    )
    assert float(hass.states.get("sensor.prices_sell_price_now").state) == (
        pytest.approx(0.2)
    )
    assert float(hass.states.get("sensor.prices_buy_price_now").state) == (
        pytest.approx(second["buy_per_kwh"])
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_state_moves_at_the_interval_boundary_without_a_new_plan(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    config = base_config()
    config["sources"]["sell"] = forecast("sensor.rce", "PLN/MWh")
    start = dt_util.utcnow()
    hass.states.async_set(
        "sensor.rce",
        "ok",
        {
            "rows": [
                {"start": start + timedelta(minutes=15 * i), "price": 100 * (i + 1)}
                for i in range(20)
            ]
        },
    )
    entry = await setup_entry(hass, config)
    plan_before = hass.states.get("sensor.prices_plan").attributes["generated_at"]
    assert float(hass.states.get("sensor.prices_sell_price_now").state) == (
        pytest.approx(0.1)
    )
    freezer.move_to("2026-09-18T10:15:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.prices_plan").attributes["generated_at"] == (
        plan_before
    )
    assert float(hass.states.get("sensor.prices_sell_price_now").state) == (
        pytest.approx(0.2)
    )
    freezer.move_to("2026-09-18T10:30:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.prices_sell_price_now").state) == (
        pytest.approx(0.3)
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_tariff_schedule_buy_price_follows_the_zone(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    # G12 in Warsaw: 12:45 local is peak, 13:00-15:00 local is the midday off-peak.
    freezer.move_to("2026-07-01T10:45:00+00:00")
    config = base_config("Europe/Warsaw")
    config["sources"]["buy"] = PriceSource(
        "schedule", schedule=TariffSchedule.default("pge_g12")
    ).to_dict()
    config["settings"].update(
        buy_rate=0.8,
        buy_off_peak_rate=0.4,
        buy_distribution_rate=0.3,
        buy_distribution_off_peak_rate=0.1,
    )
    entry = await setup_entry(hass, config)
    rows = plan_rows(hass)
    now_row = containing(rows, dt_util.utcnow())
    state = float(hass.states.get("sensor.prices_buy_price_now").state)
    assert state == pytest.approx(now_row["buy_per_kwh"])
    assert state == pytest.approx(0.8)
    seen = set()
    for stamp in ("2026-07-01T11:00:00+00:00", "2026-07-01T13:00:00+00:00"):
        freezer.move_to(stamp)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        value = float(hass.states.get("sensor.prices_buy_price_now").state)
        assert value == pytest.approx(containing(rows, dt_util.utcnow())["buy_per_kwh"])
        seen.add(round(value, 6))
    assert seen == {0.4, 0.8}
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_invalid_plan_has_no_price(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    config = base_config()
    config["sources"]["buy"] = PriceSource(
        "fixed", fixed=NumericSetting(fixed=0, unit="PLN/kWh")
    ).to_dict()
    config["settings"]["buy_rate"] = 0.75
    entry = await setup_entry(hass, config)
    assert float(hass.states.get("sensor.prices_buy_price_now").state) == (
        pytest.approx(0.75)
    )
    # Past valid_until with no new plan: the sensors stop quoting a stale price.
    freezer.move_to("2026-09-18T20:00:00+00:00")
    coordinator = entry.runtime_data
    coordinator.data = {**coordinator.data, "valid": False, "refreshing": False}
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    for key in ("buy", "sell"):
        assert hass.states.get(f"sensor.prices_{key}_price_now").state in (
            "unknown",
            "unavailable",
        )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_price_sensors_follow_expose_costs(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    registry = er.async_get(hass)
    off = await setup_entry(hass, base_config(expose_costs=False), "Off")
    for key in ("buy", "sell"):
        entity = registry.async_get(f"sensor.off_{key}_price_now")
        assert entity.disabled_by == er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(f"sensor.off_{key}_price_now") is None
    assert await hass.config_entries.async_unload(off.entry_id)

    on = await setup_entry(hass, base_config(expose_costs=True), "On")
    for key in ("buy", "sell"):
        assert registry.async_get(f"sensor.on_{key}_price_now").disabled_by is None
    assert await hass.config_entries.async_unload(on.entry_id)
