import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.energy.data import async_get_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.source_management import (
    SourceRef,
    source_inventory,
)

PLATFORMS = "homeassistant.components.energy.websocket_api.async_get_energy_platforms"
NOW = "2026-09-18T10:00:00+00:00"
COMPONENT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "energy_compass"
)
WH = {
    "2026-09-18T12:00:00+02:00": 1000,
    "2026-09-18T13:00:00+02:00": 2000,
    "2026-09-18T14:00:00+02:00": 500,
}
SOLCAST = [
    {"period_start": "2026-09-18T12:00:00+02:00", "pv_estimate": 1.5},
    {"period_start": "2026-09-18T12:30:00+02:00", "pv_estimate": 1.0},
]


@pytest.fixture
def forecast_entry(hass):
    entry = MockConfigEntry(
        domain="fx_forecast", title="Fx Forecast", state=ConfigEntryState.LOADED
    )
    entry.add_to_hass(hass)
    platform = AsyncMock(return_value={"wh_hours": WH})
    with patch(PLATFORMS, AsyncMock(return_value={"fx_forecast": platform})):
        yield entry


def pv_config(entry_id=None):
    config = default_configuration("PLN", "Europe/Warsaw")
    config["sources"]["pv"]["enabled"] = True
    if entry_id:
        config["sources"]["pv"]["solar_forecasts"] = [
            {"config_entry_id": entry_id, "domain": "fx_forecast"}
        ]
    return config


async def start(hass, config):
    entry = MockConfigEntry(domain="energy_compass", data=config, version=3)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return entry, result["flow_id"]


async def configure(hass, fid, data):
    return await hass.config_entries.options.async_configure(fid, data)


def draft(hass, fid):
    return hass.config_entries.options._progress[fid]._draft


async def to_solar_forecast_step(hass, fid):
    await configure(hass, fid, {"next_step_id": "sources"})
    await configure(hass, fid, {"next_step_id": "source_add"})
    await configure(hass, fid, {"target": "pv"})
    return await configure(hass, fid, {"mode": "solar_forecast", "group": 1})


def default_of(result, name):
    return result["data_schema"]({})[name]


async def test_add_solar_forecast_defaults_to_the_energy_prefs_entry(
    recorder_mock, hass, enable_custom_integrations, freezer, forecast_entry
):
    freezer.move_to(NOW)
    manager = await async_get_manager(hass)
    await manager.async_update(
        {
            "energy_sources": [
                {
                    "type": "solar",
                    "stat_energy_from": "sensor.fx_pv",
                    "config_entry_solar_forecast": [forecast_entry.entry_id],
                }
            ]
        }
    )
    _, fid = await start(hass, pv_config())
    result = await to_solar_forecast_step(hass, fid)
    assert result["step_id"] == "pv_solar_forecast"
    assert default_of(result, "config_entries") == [forecast_entry.entry_id]
    result = await configure(hass, fid, {"config_entries": [forecast_entry.entry_id]})
    assert result["step_id"] == "sources"
    pv = draft(hass, fid)["sources"]["pv"]
    assert pv["arrays"] == []
    assert pv["solar_forecasts"] == [
        {"config_entry_id": forecast_entry.entry_id, "domain": "fx_forecast"}
    ]
    await hass.async_stop()


async def test_default_is_empty_without_energy_prefs(
    recorder_mock, hass, enable_custom_integrations, freezer, forecast_entry
):
    freezer.move_to(NOW)
    _, fid = await start(hass, pv_config())
    result = await to_solar_forecast_step(hass, fid)
    assert default_of(result, "config_entries") == []
    result = await configure(hass, fid, {"config_entries": []})
    assert result["step_id"] == "pv_solar_forecast"
    assert result["errors"] == {"base": "invalid_source"}
    with pytest.raises(InvalidData):
        await configure(hass, fid, {"config_entries": ["fx9999"]})


async def test_failed_forecast_is_reported_and_not_saved(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    entry = MockConfigEntry(domain="fx_forecast", state=ConfigEntryState.LOADED)
    entry.add_to_hass(hass)
    platform = AsyncMock(return_value=None)
    with patch(PLATFORMS, AsyncMock(return_value={"fx_forecast": platform})):
        _, fid = await start(hass, pv_config())
        await to_solar_forecast_step(hass, fid)
        result = await configure(hass, fid, {"config_entries": [entry.entry_id]})
    assert result["errors"] == {"base": "invalid_source"}
    assert result["description_placeholders"]["detail"] == (
        "Energy solar forecast unavailable: fx_forecast"
    )
    assert "solar_forecasts" not in draft(hass, fid)["sources"]["pv"]


async def test_pv_mode_requires_enabled_pv(
    recorder_mock, hass, enable_custom_integrations, freezer, forecast_entry
):
    freezer.move_to(NOW)
    config = pv_config()
    config["sources"]["pv"]["enabled"] = False
    _, fid = await start(hass, config)
    await configure(hass, fid, {"next_step_id": "sources"})
    await configure(hass, fid, {"next_step_id": "source_add"})
    await configure(hass, fid, {"target": "pv"})
    result = await configure(hass, fid, {"mode": "solar_forecast", "group": 1})
    assert result["step_id"] == "source_mode"
    assert result["errors"] == {"base": "component_disabled"}


async def test_inventory_edit_and_remove(
    recorder_mock, hass, enable_custom_integrations, freezer, forecast_entry
):
    freezer.move_to(NOW)
    entry_id = forecast_entry.entry_id
    _, fid = await start(hass, pv_config(entry_id))
    rows = source_inventory(
        draft_config := pv_config(entry_id), er.async_get(hass), "en"
    )
    ref = SourceRef("pv", "solar_forecast", binding_index=0)
    assert (
        ref,
        f"PV · Energy dashboard solar forecast · fx_forecast {entry_id}",
    ) in rows
    assert (
        source_inventory(draft_config, er.async_get(hass), "pl")[
            [row[0] for row in rows].index(ref)
        ][1]
        == f"PV · Prognoza PV z panelu Energia · fx_forecast {entry_id}"
    )
    await configure(hass, fid, {"next_step_id": "sources"})
    inventory = await configure(hass, fid, {"next_step_id": "source_inventory"})
    choices = next(
        field.config["options"]
        for key, field in inventory["data_schema"].schema.items()
        if str(key) == "source"
    )
    value = next(item["value"] for item in choices if entry_id in item["label"])
    actions = await configure(hass, fid, {"source": value})
    assert actions["step_id"] == "source_actions"
    assert actions["menu_options"] == [
        "source_edit",
        "source_remove",
        "source_inventory",
        "sources",
    ]
    result = await configure(hass, fid, {"next_step_id": "source_edit"})
    assert result["step_id"] == "pv_solar_forecast"
    assert default_of(result, "config_entries") == [entry_id]
    result = await configure(hass, fid, {"config_entries": [entry_id]})
    assert result["step_id"] == "sources"
    inventory = await configure(hass, fid, {"next_step_id": "source_inventory"})
    await configure(hass, fid, {"source": value})
    result = await configure(hass, fid, {"next_step_id": "source_remove"})
    assert result["step_id"] == "source_remove"
    result = await configure(hass, fid, {"confirm": True})
    assert result["step_id"] == "source_remove"
    assert result["errors"] == {"base": "invalid_source"}
    assert result["description_placeholders"]["detail"] == (
        "replace or disable PV before removing its final group"
    )
    assert len(draft(hass, fid)["sources"]["pv"]["solar_forecasts"]) == 1


async def test_entity_group_replaces_solar_forecasts(
    recorder_mock, hass, enable_custom_integrations, freezer, forecast_entry
):
    freezer.move_to(NOW)
    config = pv_config(forecast_entry.entry_id)
    config["preset"] = "solcast"
    hass.states.async_set("sensor.fx_today", "5", {"detailedForecast": SOLCAST})
    _, fid = await start(hass, config)
    await configure(hass, fid, {"next_step_id": "sources"})
    await configure(hass, fid, {"next_step_id": "source_add"})
    await configure(hass, fid, {"target": "pv"})
    await configure(hass, fid, {"mode": "forecast", "group": 1})
    await configure(hass, fid, {"entity_id": "sensor.fx_today"})
    result = await configure(hass, fid, {"attribute": "detailedForecast"})
    result = await configure(hass, fid, result["data_schema"]({}))
    assert result["step_id"] == "sources", result.get("errors")
    pv = draft(hass, fid)["sources"]["pv"]
    assert "solar_forecasts" not in pv
    assert len(pv["arrays"]) == 1


async def test_installation_pv_off_clears_both(
    recorder_mock, hass, enable_custom_integrations, freezer, forecast_entry
):
    freezer.move_to(NOW)
    _, fid = await start(hass, pv_config(forecast_entry.entry_id))
    result = await configure(hass, fid, {"next_step_id": "installation"})
    data = result["data_schema"]({})
    data["pv_enabled"] = False
    result = await configure(hass, fid, data)
    assert result["step_id"] == "menu"
    pv = draft(hass, fid)["sources"]["pv"]
    assert pv == {"enabled": False, "arrays": []}


async def test_preview_line_is_exact_and_legacy_preview_is_unchanged(
    recorder_mock, hass, enable_custom_integrations, freezer, forecast_entry
):
    freezer.move_to(NOW)
    entry_id = forecast_entry.entry_id
    _, fid = await start(hass, pv_config(entry_id))
    result = await configure(hass, fid, {"next_step_id": "preview"})
    preview = result["description_placeholders"]["preview"]
    line = (
        f"PV source: Energy dashboard solar forecast fx_forecast {entry_id}; Wh per "
        "local hour as in the Energy dashboard, hours without values count as 0; "
        "no age check."
    )
    assert f".\n{line}\n" in preview
    assert "Source inputs: validated" in preview

    legacy = pv_config()
    hass.states.async_set("sensor.fx_today", "5", {"detailedForecast": SOLCAST})
    legacy["sources"]["pv"]["arrays"] = [
        [
            {
                "entity": {
                    "entity_id": "sensor.fx_today",
                    "attribute": "detailedForecast",
                },
                "value_path": "pv_estimate",
                "start_path": "period_start",
                "interval_minutes": 30,
                "unit": "kW",
                "value_kind": "power",
                "source_timezone": "Europe/Warsaw",
            }
        ]
    ]
    _, fid = await start(hass, legacy)
    result = await configure(hass, fid, {"next_step_id": "preview"})
    assert "PV source:" not in result["description_placeholders"]["preview"]


def test_pv_solar_forecast_strings_exist_in_all_json_files():
    expected = {
        "strings.json": {
            "title": "Energy dashboard solar forecast",
            "data": {"config_entries": "Forecast integrations (added together)"},
            "description": "{detail}",
        },
        "translations/en.json": {
            "title": "Energy dashboard solar forecast",
            "data": {"config_entries": "Forecast integrations (added together)"},
            "description": "{detail}",
        },
        "translations/pl.json": {
            "title": "Prognoza PV z panelu Energia",
            "data": {"config_entries": "Integracje prognozy (sumowane)"},
            "description": "{detail}",
        },
    }
    for name, step in expected.items():
        data = json.loads((COMPONENT / name).read_text())
        for flow in ("config", "options"):
            assert data[flow]["step"]["pv_solar_forecast"] == step
