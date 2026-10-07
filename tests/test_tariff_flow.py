import json
from pathlib import Path

import pytest
from homeassistant.data_entry_flow import InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.presets import PRESETS
from custom_components.energy_compass.settings import (
    default_configuration,
    merged_configuration,
)
from custom_components.energy_compass.source_management import source_inventory
from custom_components.energy_compass.sources.tariffs import CATALOG

NOW = "2026-09-18T10:00:00+00:00"
COMPONENT_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "energy_compass"
)
RCE_RECORDS = [
    {"dtime": "2026-09-18 11:00:00", "rce_pln": 400.0, "period": "10:45 - 11:00"},
    {"dtime": "2026-09-18 11:15:00", "rce_pln": 420.0, "period": "11:00 - 11:15"},
]
SCHEDULE_PGE = {"tariff": "pge_g12", "meter_winter_clock": False, "params": {}}


def pln_config():
    config = default_configuration("PLN", "Europe/Warsaw")
    config["settings"].update(
        horizon_hours=2, display_horizon_hours=2, reference_horizon_hours=2
    )
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


async def to_tariff(hass, fid, role):
    await configure(hass, fid, {"next_step_id": "tariffs"})
    return await configure(hass, fid, {"next_step_id": f"tariff_{role}"})


def mode_values(result):
    for marker, field in result["data_schema"].schema.items():
        if str(marker) == "mode":
            return [option["value"] for option in field.config["options"]]
    raise AssertionError("no mode field")


def default_of(result, name):
    return result["data_schema"]({})[name]


async def save_schedule(hass, fid, tariff="pge_g12", winter=False):
    await to_tariff(hass, fid, "buy")
    result = await configure(hass, fid, {"mode": "schedule"})
    assert result["step_id"] == "tariff_schedule"
    return await configure(hass, fid, {"tariff": tariff, "meter_winter_clock": winter})


async def save_rce(hass, fid, entity="sensor.rce"):
    await to_tariff(hass, fid, "sell")
    result = await configure(hass, fid, {"mode": "rce"})
    assert result["step_id"] == "tariff_rce"
    return await configure(hass, fid, {"entity": entity})


@pytest.fixture
def rce_state(hass):
    hass.states.async_set("sensor.rce", "400", {"prices": RCE_RECORDS})


async def test_pln_offers_schedule_and_rce_everywhere(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    _, fid = await start(hass, pln_config())
    buy = await to_tariff(hass, fid, "buy")
    assert mode_values(buy) == ["fixed", "entity", "forecast", "schedule", "back"]
    await configure(hass, fid, {"mode": "back"})
    sell = await configure(hass, fid, {"next_step_id": "tariff_sell"})
    assert mode_values(sell) == ["fixed", "entity", "forecast", "rce", "back"]
    await configure(hass, fid, {"mode": "back"})
    await configure(hass, fid, {"next_step_id": "menu"})
    await configure(hass, fid, {"next_step_id": "sources"})
    await configure(hass, fid, {"next_step_id": "source_add"})
    result = await configure(hass, fid, {"target": "buy"})
    assert result["step_id"] == "source_mode"
    assert "schedule" in mode_values(result)
    assert "rce" not in mode_values(result)


async def test_eur_offers_neither(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    _, fid = await start(hass, default_configuration("EUR", "UTC"))
    buy = await to_tariff(hass, fid, "buy")
    assert mode_values(buy) == ["fixed", "entity", "forecast", "back"]
    await configure(hass, fid, {"mode": "back"})
    sell = await configure(hass, fid, {"next_step_id": "tariff_sell"})
    assert mode_values(sell) == ["fixed", "entity", "forecast", "back"]
    with pytest.raises(InvalidData):
        await configure(hass, fid, {"mode": "rce"})


async def test_schedule_save_follows_the_key_hygiene_row(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    config = pln_config()
    entry, fid = await start(hass, config)
    result = await save_schedule(hass, fid)
    assert result["step_id"] == "tariffs"
    buy = draft(hass, fid)["sources"]["buy"]
    assert buy["mode"] == "schedule"
    assert buy["forecast"] == []
    assert buy["fixed"] is None
    assert buy["schedule"] == SCHEDULE_PGE
    assert "floor_per_kwh" not in buy
    assert buy["multiplier"] == config["sources"]["buy"]["multiplier"]
    assert dict(entry.data) == config


async def test_enea_schedule_routes_through_hours_and_stores_ints(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    _, fid = await start(hass, pln_config())
    result = await save_schedule(hass, fid, "enea_g12", winter=True)
    assert result["step_id"] == "tariff_schedule_hours"
    assert default_of(result, "night_start") == "22"
    assert default_of(result, "afternoon_start") == "13"
    result = await configure(hass, fid, {"night_start": "23", "afternoon_start": "15"})
    assert result["step_id"] == "tariffs"
    assert draft(hass, fid)["sources"]["buy"]["schedule"] == {
        "tariff": "enea_g12",
        "meter_winter_clock": True,
        "params": {"night_start": 23, "afternoon_start": 15},
    }
    buy = await configure(hass, fid, {"next_step_id": "tariff_buy"})
    assert default_of(buy, "mode") == "schedule"
    form = await configure(hass, fid, {"mode": "schedule"})
    assert default_of(form, "tariff") == "enea_g12"
    assert default_of(form, "meter_winter_clock") is True
    hours = await configure(
        hass, fid, {"tariff": "enea_g12", "meter_winter_clock": True}
    )
    assert default_of(hours, "night_start") == "23"
    assert default_of(hours, "afternoon_start") == "15"


async def test_changing_tariff_resets_enea_hours_to_the_catalog_default(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    config = pln_config()
    config["sources"]["buy"].update(
        mode="schedule",
        schedule={
            "tariff": "pge_g12",
            "meter_winter_clock": False,
            "params": {},
        },
    )
    _, fid = await start(hass, config)
    result = await save_schedule(hass, fid, "enea_g12")
    assert default_of(result, "night_start") == "22"


async def test_rce_save_builds_the_pse_binding_with_a_zero_floor(
    recorder_mock, hass, enable_custom_integrations, freezer, rce_state
):
    freezer.move_to(NOW)
    _, fid = await start(hass, pln_config())
    result = await save_rce(hass, fid)
    assert result["step_id"] == "tariffs"
    sell = draft(hass, fid)["sources"]["sell"]
    preset = PRESETS["pse"]
    assert sell["mode"] == "forecast"
    assert sell["floor_per_kwh"] == 0.0
    assert sell["fixed"] is None
    assert "schedule" not in sell
    binding = sell["forecast"][0]
    assert binding["entity"]["entity_id"] == "sensor.rce"
    assert binding["entity"]["attribute"] == preset.price_attribute == "prices"
    assert binding["value_path"] == preset.price_value_field
    assert binding["end_path"] == preset.price_end_field
    assert binding["start_path"] is None
    assert binding["unit"] == "PLN/MWh"
    assert binding["interval_minutes"] == 15
    assert binding["value_kind"] == "price"
    assert binding["value_sign"] == 1
    assert binding["source_timezone"] == "Europe/Warsaw"
    assert binding["max_age_seconds"] == 24 * 3600


async def test_rce_entity_without_prices_re_shows_the_form(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    hass.states.async_set("sensor.plain", "1", {})
    _, fid = await start(hass, pln_config())
    result = await save_rce(hass, fid, "sensor.plain")
    assert result["step_id"] == "tariff_rce"
    assert result["errors"] == {"base": "invalid_source"}
    assert draft(hass, fid)["sources"]["sell"]["mode"] == "fixed"


async def test_schedule_to_fixed_drops_the_schedule(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    _, fid = await start(hass, pln_config())
    await save_schedule(hass, fid)
    await configure(hass, fid, {"next_step_id": "tariff_buy"})
    result = await configure(hass, fid, {"mode": "fixed"})
    assert result["step_id"] == "tariffs"
    buy = draft(hass, fid)["sources"]["buy"]
    assert buy["mode"] == "fixed"
    assert "schedule" not in buy


async def test_rce_to_fixed_drops_the_floor(
    recorder_mock, hass, enable_custom_integrations, freezer, rce_state
):
    freezer.move_to(NOW)
    _, fid = await start(hass, pln_config())
    await save_rce(hass, fid)
    await configure(hass, fid, {"next_step_id": "tariff_sell"})
    result = await configure(hass, fid, {"mode": "fixed"})
    assert result["step_id"] == "tariffs"
    sell = draft(hass, fid)["sources"]["sell"]
    assert sell["mode"] == "fixed"
    assert "floor_per_kwh" not in sell
    assert sell["forecast"] == []


async def test_rce_to_generic_forecast_edit_keeps_the_floor(
    recorder_mock, hass, enable_custom_integrations, freezer, rce_state
):
    freezer.move_to(NOW)
    _, fid = await start(hass, pln_config())
    await save_rce(hass, fid)
    await configure(hass, fid, {"next_step_id": "tariff_sell"})
    result = await configure(hass, fid, {"mode": "forecast"})
    assert result["step_id"] == "source_entity"
    result = await configure(hass, fid, {"entity_id": "sensor.rce"})
    assert result["step_id"] == "source_attribute"
    result = await configure(hass, fid, {"attribute": "prices"})
    assert result["step_id"] == "source_mapping"
    result = await configure(hass, fid, result["data_schema"]({}))
    assert result["step_id"] == "tariffs", result.get("errors")
    sell = draft(hass, fid)["sources"]["sell"]
    assert sell["mode"] == "forecast"
    assert sell["floor_per_kwh"] == 0.0


async def test_helper_bound_fixed_to_schedule_drops_only_the_rate_helper(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    config = pln_config()
    helper = {
        "entity": {"entity_id": "sensor.rate"},
        "unit": "PLN/kWh",
        "source_unit": "PLN/kWh",
        "multiplier": 1,
        "max_age_seconds": None,
    }
    config["helpers"] = {"buy_rate": helper, "buy_off_peak_rate": helper}
    _, fid = await start(hass, config)
    await save_schedule(hass, fid)
    helpers = draft(hass, fid)["helpers"]
    assert "buy_rate" not in helpers
    assert helpers["buy_off_peak_rate"] == helper


async def test_schedule_to_schedule_keeps_both_rate_helpers(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    config = pln_config()
    helper = {
        "entity": {"entity_id": "sensor.rate"},
        "unit": "PLN/kWh",
        "source_unit": "PLN/kWh",
        "multiplier": 1,
        "max_age_seconds": None,
    }
    config["sources"]["buy"].update(mode="schedule", fixed=None, schedule=SCHEDULE_PGE)
    config["helpers"] = {"buy_rate": helper, "buy_off_peak_rate": helper}
    _, fid = await start(hass, config)
    await save_schedule(hass, fid, "tauron_g12")
    helpers = draft(hass, fid)["helpers"]
    assert helpers == {"buy_rate": helper, "buy_off_peak_rate": helper}
    assert draft(hass, fid)["sources"]["buy"]["schedule"]["tariff"] == "tauron_g12"


async def test_reverse_mapping_defaults_and_inventory_label(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    config = pln_config()
    config["sources"]["buy"].update(mode="schedule", fixed=None, schedule=SCHEDULE_PGE)
    config["sources"]["sell"].update(
        mode="forecast",
        fixed=None,
        floor_per_kwh=0.0,
        forecast=[
            {
                "entity": {"entity_id": "sensor.rce", "attribute": "prices"},
                "value_path": "rce_pln",
                "end_path": "dtime",
                "interval_minutes": 15,
                "unit": "PLN/MWh",
                "value_kind": "price",
            }
        ],
    )
    _, fid = await start(hass, config)
    buy = await to_tariff(hass, fid, "buy")
    assert default_of(buy, "mode") == "schedule"
    await configure(hass, fid, {"mode": "back"})
    sell = await configure(hass, fid, {"next_step_id": "tariff_sell"})
    assert default_of(sell, "mode") == "rce"
    en = dict(source_inventory(config, None, "en"))
    labels = {ref.role: label for ref, label in en.items()}
    assert labels["buy"] == (
        "Buy · Polish tariff schedule (G11/G12/G12w) · PGE Dystrybucja G12"
    )
    pl = {ref.role: label for ref, label in source_inventory(config, None, "pl")}
    assert pl["buy"] == "Zakup · Taryfa OSD (G11/G12/G12w) · PGE Dystrybucja G12"


async def test_source_edit_on_schedule_buy_opens_the_schedule_step(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    config = pln_config()
    config["sources"]["buy"].update(mode="schedule", fixed=None, schedule=SCHEDULE_PGE)
    _, fid = await start(hass, config)
    await configure(hass, fid, {"next_step_id": "sources"})
    inventory = await configure(hass, fid, {"next_step_id": "source_inventory"})
    options = inventory["data_schema"].schema
    selector_field = next(v for k, v in options.items() if str(k) == "source")
    index = next(
        o["value"]
        for o in selector_field.config["options"]
        if o["label"].startswith("Buy")
    )
    await configure(hass, fid, {"source": index})
    result = await configure(hass, fid, {"next_step_id": "source_edit"})
    assert result["step_id"] == "tariff_schedule"
    assert default_of(result, "tariff") == "pge_g12"


async def test_untouched_legacy_entry_round_trips_sources_byte_identically(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    config = pln_config()
    entry = MockConfigEntry(domain="energy_compass", data=config, version=3)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await configure(hass, fid, {"next_step_id": "preview"})
    result = await configure(hass, fid, {"confirm": True})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done(wait_background_tasks=True)
    saved = result["data"]["configuration"]["sources"]
    assert json.dumps(saved, sort_keys=True) == json.dumps(
        config["sources"], sort_keys=True
    )
    assert merged_configuration(entry)["sources"] == config["sources"]
    assert await hass.config_entries.async_unload(entry.entry_id)


def _walk(doc, path):
    node = doc
    for part in path:
        node = node[part]
    return node


STEP_KEYS = {
    "tariff_schedule": ("title", ("data", "tariff"), ("data", "meter_winter_clock")),
    "tariff_schedule_hours": (
        "title",
        ("data", "night_start"),
        ("data", "afternoon_start"),
    ),
    "tariff_rce": ("title", ("data", "entity"), "description"),
}


@pytest.mark.parametrize(
    "file", ["strings.json", "translations/en.json", "translations/pl.json"]
)
def test_new_steps_and_rate_labels_exist_in_every_translation_file(file):
    doc = json.loads((COMPONENT_DIR / file).read_text())
    for section in ("config", "options"):
        for step, keys in STEP_KEYS.items():
            for key in keys:
                path = (
                    section,
                    "step",
                    step,
                    *(key if isinstance(key, tuple) else (key,)),
                )
                assert _walk(doc, path)
        data = doc[section]["step"]["tariff_values"]["data"]
        english = file != "translations/pl.json"
        assert data["buy_rate"] == (
            "Buy rate (fixed, or G11/peak rate of a tariff schedule)"
            if english
            else "Cena zakupu (stała albo G11/szczytowa w taryfie OSD)"
        )
        assert data["buy_off_peak_rate"] == (
            "Off-peak buy rate (G12/G12w tariff schedule)"
            if english
            else "Cena zakupu poza szczytem (taryfa OSD G12/G12w)"
        )


def test_catalog_has_a_label_in_both_languages():
    assert all(spec.labels["en"] and spec.labels["pl"] for spec in CATALOG.values())
