from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.energy.data import async_get_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.detect import (
    DetectionContext,
    EnergyPrefsFacts,
    detect,
)
from custom_components.energy_compass.detect_ha import async_detection_snapshot

PLATFORMS = "homeassistant.components.energy.websocket_api.async_get_energy_platforms"
NOW = "2026-10-08T10:20:00+00:00"
FRESH = "2026-10-08T10:15:00+00:00"
WH = {
    "2026-10-08T12:00:00+02:00": 1000,
    "2026-10-08T13:00:00+02:00": 2000,
}


def context():
    return DetectionContext(
        "PLN",
        "Europe/Warsaw",
        True,
        True,
        datetime(2026, 10, 8, 10, 20, tzinfo=UTC),
    )


def prices():
    start = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    return [
        {
            "start": (start + timedelta(minutes=15 * i)).isoformat(),
            "end": (start + timedelta(minutes=15 * (i + 1))).isoformat(),
            "price": 0.5,
        }
        for i in range(8)
    ]


@pytest.fixture
def site(hass, freezer):
    freezer.move_to(NOW)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    solarman = MockConfigEntry(domain="solarman")
    solarman.add_to_hass(hass)
    inverter = devices.async_get_or_create(
        config_entry_id=solarman.entry_id,
        identifiers={("solarman", "fx-inverter")},
        manufacturer="Deye",
        model="FX-HYBRID",
    )
    bms = devices.async_get_or_create(
        config_entry_id=solarman.entry_id,
        identifiers={("solarman", "fx-bms")},
        manufacturer="Deye",
    )
    devices.async_update_device(bms.id, disabled_by=dr.DeviceEntryDisabler.USER)

    def add(platform, uid, object_id, **kwargs):
        entry = entities.async_get_or_create(
            "number" if kwargs.pop("domain", None) == "number" else "sensor",
            platform,
            uid,
            suggested_object_id=object_id,
            **kwargs,
        )
        return entry

    result = {
        "load": add(
            "solarman",
            "fx-load",
            "fx_load",
            config_entry=solarman,
            device_id=inverter.id,
            translation_key="total_load_consumption",
        ),
        "battery_power": add(
            "solarman",
            "fx-bpower",
            "fx_battery_power",
            config_entry=solarman,
            device_id=inverter.id,
            translation_key="battery_power",
        ),
        "import": add(
            "solarman",
            "fx-import",
            "fx_import_total",
            config_entry=solarman,
            device_id=inverter.id,
            translation_key="total_energy_import",
        ),
        "number": add(
            "solarman",
            "fx-number",
            "fx_pv_power",
            config_entry=solarman,
            device_id=inverter.id,
            translation_key="pv_power",
            domain="number",
        ),
        "pack": add(
            "solarman",
            "fx-pack",
            "fx_pack_1",
            config_entry=solarman,
            device_id=bms.id,
            translation_key="battery_1",
            disabled_by=er.RegistryEntryDisabler.DEVICE,
        ),
        "sell": add("template", "energy_compass_sell_fx", "fx_ec_sell"),
        "soc": add(
            "template",
            "fx-soc",
            "fx_soc",
            original_device_class="battery",
        ),
        "other": add("hue", "fx-hue", "fx_hue"),
    }
    hass.states.async_set(
        "sensor.fx_load",
        "100",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    hass.states.async_set(
        "sensor.fx_battery_power", "-500", {"unit_of_measurement": "W"}
    )
    hass.states.async_set(
        "sensor.fx_import_total",
        "10",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    hass.states.async_set("number.fx_pv_power", "8600", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.fx_soc", "40", {"unit_of_measurement": "%"})
    hass.states.async_set(
        "sensor.fx_ec_sell",
        "0.5",
        {
            "unit_of_measurement": "PLN/kWh",
            "prices": prices(),
            "published_at": FRESH,
            "settlement": "RCE, floor 0, multiplier 1.23",
        },
    )
    hass.states.async_set("sensor.fx_hue", "1", {})
    forecast = MockConfigEntry(
        domain="fx_forecast", title="Fx Forecast", state=ConfigEntryState.LOADED
    )
    forecast.add_to_hass(hass)
    result.update(solarman=solarman, forecast=forecast, inverter=inverter, bms=bms)
    return result


async def save_prefs(hass, sources):
    manager = await async_get_manager(hass)
    await manager.async_update({"energy_sources": sources})


def prefs_sources(forecast_id):
    return [
        {
            "type": "solar",
            "stat_energy_from": "sensor.fx_unknown_pv_total",
            "config_entry_solar_forecast": [forecast_id],
        },
        {
            "type": "battery",
            "stat_energy_from": "sensor.fx_unknown_discharge",
            "stat_energy_to": "sensor.fx_unknown_charge",
            "power_config": {"stat_rate": "sensor.fx_battery_power"},
            "stat_soc": "sensor.fx_soc",
        },
        {
            "type": "grid",
            "stat_energy_from": "sensor.fx_import_total",
            "stat_energy_to": None,
        },
    ]


def forecast_platform():
    return AsyncMock(return_value={"wh_hours": WH})


async def test_registry_device_and_state_facts(hass, site):
    with patch(PLATFORMS, AsyncMock(return_value={})):
        snapshot = await async_detection_snapshot(hass)
    assert set(snapshot.entities) == {
        "sensor.fx_load",
        "sensor.fx_battery_power",
        "sensor.fx_import_total",
        "number.fx_pv_power",
        "sensor.fx_pack_1",
        "sensor.fx_ec_sell",
        "sensor.fx_soc",
    }
    load = snapshot.entities["sensor.fx_load"]
    assert (load.platform, load.translation_key, load.unique_id) == (
        "solarman",
        "total_load_consumption",
        "fx-load",
    )
    assert load.registry_id == site["load"].id
    assert load.config_entry_id == site["solarman"].entry_id
    assert load.device_id == site["inverter"].id
    assert load.disabled is False
    assert (load.state, load.attributes["unit_of_measurement"]) == ("100", "kWh")
    assert load.last_updated == NOW
    pack = snapshot.entities["sensor.fx_pack_1"]
    assert pack.disabled is True
    assert pack.state is None
    assert pack.attributes == {}
    assert snapshot.entities["sensor.fx_soc"].device_class == "battery"
    assert snapshot.entities["number.fx_pv_power"].translation_key == "pv_power"
    assert snapshot.entities["sensor.fx_ec_sell"].unique_id == "energy_compass_sell_fx"
    assert set(snapshot.devices) == {site["inverter"].id, site["bms"].id}
    device = snapshot.devices[site["inverter"].id]
    assert (device.manufacturer, device.model) == ("Deye", "FX-HYBRID")
    assert device.config_entry_ids == frozenset({site["solarman"].entry_id})


async def test_energy_preferences_are_normalized_and_referenced_entities_added(
    hass, site
):
    await save_prefs(hass, prefs_sources(site["forecast"].entry_id))
    with patch(PLATFORMS, AsyncMock(return_value={"fx_forecast": forecast_platform()})):
        snapshot = await async_detection_snapshot(hass)
    assert snapshot.energy == EnergyPrefsFacts(
        solar_energy=("sensor.fx_unknown_pv_total",),
        solar_power=(),
        solar_forecast_entries=(site["forecast"].entry_id,),
        grid_import=("sensor.fx_import_total",),
        grid_export=(),
        grid_power=(),
        battery_power=("sensor.fx_battery_power",),
        battery_soc=("sensor.fx_soc",),
    )
    missing = snapshot.entities["sensor.fx_unknown_pv_total"]
    assert missing.registry_id is None
    assert missing.state is None
    assert "sensor.fx_unknown_discharge" not in snapshot.entities
    await hass.async_stop()


async def test_unconfigured_energy_gives_no_prefs(hass, site):
    with patch(PLATFORMS, AsyncMock(return_value={})):
        snapshot = await async_detection_snapshot(hass)
    assert snapshot.energy is None
    assert snapshot.solar_forecasts == {}


async def test_raising_energy_preferences_give_no_prefs(hass, site):
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        snapshot = await async_detection_snapshot(hass)
    assert snapshot.energy is None


async def test_solar_forecast_facts_for_loaded_and_not_loaded_entries(hass, site):
    other = MockConfigEntry(domain="fx_forecast", title="Fx Idle")
    other.add_to_hass(hass)
    sources = prefs_sources(site["forecast"].entry_id)
    sources[0]["config_entry_solar_forecast"].append(other.entry_id)
    await save_prefs(hass, sources)
    platform = forecast_platform()
    with patch(PLATFORMS, AsyncMock(return_value={"fx_forecast": platform})):
        snapshot = await async_detection_snapshot(hass)
    loaded = snapshot.solar_forecasts[site["forecast"].entry_id]
    assert (loaded.domain, loaded.title, loaded.wh_hours, loaded.error) == (
        "fx_forecast",
        "Fx Forecast",
        WH,
        None,
    )
    idle = snapshot.solar_forecasts[other.entry_id]
    assert idle.wh_hours is None
    assert idle.error == "Energy solar forecast not loaded: fx_forecast"
    platform.assert_awaited_once()
    await hass.async_stop()


async def test_end_to_end_detection_on_a_home_assistant_site(hass, site):
    await save_prefs(hass, prefs_sources(site["forecast"].entry_id))
    with patch(PLATFORMS, AsyncMock(return_value={"fx_forecast": forecast_platform()})):
        snapshot = await async_detection_snapshot(hass)
    detection = detect(snapshot, context())
    offers = {offer.row: offer for offer in detection.offers}
    pv = offers["pv"]
    assert pv.default == 0
    assert (pv.options[0].kind, pv.options[0].titles) == (
        "solar_forecast",
        ("Fx Forecast",),
    )
    sell = offers["sell"].options[0]
    assert (sell.kind, sell.targets[0].entity_id) == (
        "template_sell",
        "sensor.fx_ec_sell",
    )
    assert sell.targets[0].registry_id == site["sell"].id
    battery = offers["battery_power"].options[0]
    assert (battery.provider, battery.targets[0].entity_id) == (
        "energy_prefs",
        "sensor.fx_battery_power",
    )
    imported = offers["grid_import_energy"].options[0]
    assert imported.targets[0].entity_id == "sensor.fx_import_total"
    assert offers["load"].options[0].targets[0].entity_id == "sensor.fx_load"
    assert offers["soc"].options[0].targets[0].entity_id == "sensor.fx_soc"
    assert "number.fx_pv_power" not in {
        target.entity_id
        for offer in detection.offers
        for option in offer.options
        for target in option.targets
    }
    assert "bms_soc" not in offers
    await hass.async_stop()
