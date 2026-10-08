import ast
from copy import deepcopy
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass import source_builders
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.source_builders import (
    measurement_setting,
    rce_sell_binding,
    rce_unit_error,
    set_forecast_sell,
    set_load_statistic,
    set_pv_group,
    set_rce_sell,
    set_soc,
    solcast_pv_binding,
    template_sell_binding,
)
from custom_components.energy_compass.sources.bindings import EntityBinding

NOW = "2026-09-18T10:00:00+00:00"
ZONE = "Europe/Warsaw"
RCE_RECORDS = [
    {"dtime": "2026-09-18 11:00:00", "rce_pln": 400.0, "period": "10:45 - 11:00"},
    {"dtime": "2026-09-18 11:15:00", "rce_pln": 420.0, "period": "11:00 - 11:15"},
]
TEMPLATE_RECORDS = [
    {
        "start": "2026-09-18T10:00:00+00:00",
        "end": "2026-09-18T10:15:00+00:00",
        "price": 0.4,
    }
]
SOLCAST_RECORDS = [
    {
        "period_start": "2026-09-18T12:00:00+02:00",
        "pv_estimate": 1.5,
        "pv_estimate10": 1.0,
        "pv_estimate90": 2.0,
    }
]


def config(**kwargs):
    result = default_configuration("PLN", ZONE)
    result["sources"]["pv"]["enabled"] = True
    result["sources"]["battery_enabled"] = True
    result.update(kwargs)
    return result


async def start(hass, configuration):
    entry = MockConfigEntry(domain="energy_compass", data=configuration, version=3)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return result["flow_id"]


async def configure(hass, fid, data):
    return await hass.config_entries.options.async_configure(fid, data)


def draft(hass, fid):
    return hass.config_entries.options._progress[fid]._draft


async def add_source(hass, fid, target, mode, *, in_sources=False, **extra):
    if not in_sources:
        await configure(hass, fid, {"next_step_id": "sources"})
    await configure(hass, fid, {"next_step_id": "source_add"})
    await configure(hass, fid, {"target": target})
    return await configure(hass, fid, {"mode": mode, **extra})


def test_module_is_pure():
    tree = ast.parse(Path(source_builders.__file__).read_text())
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        assert not any(name.split(".")[0] == "homeassistant" for name in names)


def test_solcast_binding_literal():
    binding = solcast_pv_binding(EntityBinding("sensor.fx_a", "rid"), ZONE)
    assert binding.to_dict() == {
        "entity": {
            "entity_id": "sensor.fx_a",
            "registry_id": "rid",
            "attribute": "detailedForecast",
            "record_value_path": None,
        },
        "value_path": "pv_estimate",
        "start_path": "period_start",
        "end_path": None,
        "duration_path": None,
        "interval_minutes": 30,
        "unit": "kW",
        "unit_path": None,
        "value_kind": "power",
        "value_sign": 1.0,
        "source_timezone": ZONE,
        "published_path": None,
        "max_age_seconds": 86400,
    }


def test_template_sell_binding_literal():
    binding = template_sell_binding(EntityBinding("sensor.fx_t", None))
    data = binding.to_dict()
    assert data["entity"]["attribute"] == "prices"
    assert (data["value_path"], data["start_path"], data["end_path"]) == (
        "price",
        "start",
        "end",
    )
    assert data["interval_minutes"] == 15
    assert data["unit"] == "PLN/kWh"
    assert data["source_timezone"] == "UTC"
    assert data["published_path"] == "attributes.published_at"
    assert data["max_age_seconds"] == 4500


def test_set_pv_group_bounds_and_hygiene():
    draft_config = config()
    draft_config["sources"]["pv"]["solar_forecasts"] = [
        {"config_entry_id": "fx", "domain": "x"}
    ]
    binding = solcast_pv_binding(EntityBinding("sensor.fx_a"), ZONE)
    set_pv_group(draft_config, 0, [binding])
    pv = draft_config["sources"]["pv"]
    assert pv["arrays"] == [[binding.to_dict()]]
    assert "solar_forecasts" not in pv
    set_pv_group(draft_config, 1, [binding, binding])
    assert len(pv["arrays"]) == 2
    set_pv_group(draft_config, 0, [binding, binding])
    assert len(pv["arrays"][0]) == 2
    with pytest.raises(Exception, match="consecutive"):
        set_pv_group(draft_config, 3, [binding])
    with pytest.raises(Exception, match="consecutive"):
        set_pv_group(draft_config, -1, [binding])


def test_set_forecast_sell_keeps_floor_only_for_raw_rce():
    draft_config = config()
    draft_config["helpers"]["sell_rate"] = {"x": 1}
    set_rce_sell(draft_config, [rce_sell_binding(EntityBinding("sensor.fx_r"), ZONE)])
    assert draft_config["sources"]["sell"]["floor_per_kwh"] == 0.0
    assert "sell_rate" not in draft_config["helpers"]
    set_forecast_sell(
        draft_config, [rce_sell_binding(EntityBinding("sensor.fx_r"), ZONE)]
    )
    assert draft_config["sources"]["sell"]["floor_per_kwh"] == 0.0
    set_forecast_sell(
        draft_config, [template_sell_binding(EntityBinding("sensor.fx_t"))]
    )
    sell = draft_config["sources"]["sell"]
    assert sell["mode"] == "forecast"
    assert sell["fixed"] is None
    assert "floor_per_kwh" not in sell
    assert "schedule" not in sell


def test_rce_unit_error_cases():
    assert rce_unit_error({"unit_of_measurement": "PLN/MWh"}) is None
    assert rce_unit_error({}) is None
    assert (
        rce_unit_error({"unit_of_measurement": "PLN/kWh"})
        == "RCE sensor must report PLN/MWh"
    )


@pytest.mark.parametrize("unit", ["PLN/MWh", None])
async def test_tariff_rce_rejects_kwh_unit(
    recorder_mock, hass, enable_custom_integrations, freezer, unit
):
    freezer.move_to(NOW)
    fid = await start(hass, config())
    hass.states.async_set(
        "sensor.rce", "0.4", {"prices": RCE_RECORDS, "unit_of_measurement": "PLN/kWh"}
    )
    result = await add_source(hass, fid, "sell", "rce")
    assert result["step_id"] == "tariff_rce"
    result = await configure(hass, fid, {"entity": "sensor.rce"})
    assert result["step_id"] == "tariff_rce"
    assert result["errors"] == {"base": "invalid_source"}
    assert result["description_placeholders"]["detail"] == (
        "RCE sensor must report PLN/MWh"
    )
    attributes = {"prices": RCE_RECORDS}
    if unit:
        attributes["unit_of_measurement"] = unit
    hass.states.async_set("sensor.rce", "400", attributes)
    result = await configure(hass, fid, {"entity": "sensor.rce"})
    assert result["step_id"] == "sources"
    assert draft(hass, fid)["sources"]["sell"]["floor_per_kwh"] == 0.0


async def test_rce_flow_matches_builder(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    configuration = config()
    fid = await start(hass, configuration)
    hass.states.async_set("sensor.rce", "400", {"prices": RCE_RECORDS})
    await add_source(hass, fid, "sell", "rce")
    await configure(hass, fid, {"entity": "sensor.rce"})
    expected = deepcopy(configuration)
    set_rce_sell(expected, [rce_sell_binding(EntityBinding("sensor.rce"), ZONE)])
    assert draft(hass, fid)["sources"]["sell"] == expected["sources"]["sell"]
    assert draft(hass, fid)["helpers"] == expected["helpers"]


async def test_sell_template_flow_matches_builder(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    configuration = config()
    fid = await start(hass, configuration)
    hass.states.async_set(
        "sensor.fx_template",
        "0.4",
        {"prices": TEMPLATE_RECORDS, "published_at": NOW},
    )
    result = await add_source(hass, fid, "sell", "forecast")
    result = await configure(hass, fid, {"entity_id": "sensor.fx_template"})
    result = await configure(hass, fid, {"attribute": "prices"})
    assert result["step_id"] == "source_mapping"
    result = await configure(
        hass,
        fid,
        {
            "value_path": "price",
            "start_path": "start",
            "end_path": "end",
            "duration_path": "",
            "unit_path": "",
            "published_path": "attributes.published_at",
            "interval_minutes": 15,
            "unit": "PLN/kWh",
            "value_kind": "price",
            "value_sign": 1,
            "source_timezone": "UTC",
            "check_age": True,
            "max_age_hours": 1.25,
        },
    )
    assert result["step_id"] == "sources", result.get("errors")
    expected = deepcopy(configuration)
    set_forecast_sell(
        expected, [template_sell_binding(EntityBinding("sensor.fx_template"))]
    )
    assert draft(hass, fid)["sources"]["sell"] == expected["sources"]["sell"]


async def test_pv_solcast_flow_matches_builder(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(NOW)
    configuration = config(preset="solcast")
    fid = await start(hass, configuration)
    for name in ("today", "tomorrow"):
        hass.states.async_set(
            f"sensor.fx_{name}", "5", {"detailedForecast": SOLCAST_RECORDS}
        )
    result = await add_source(hass, fid, "pv", "forecast", group=1)
    for name in ("today", "tomorrow"):
        if name == "tomorrow":
            result = await add_source(
                hass, fid, "pv", "forecast", group=1, in_sources=True
            )
        await configure(hass, fid, {"entity_id": f"sensor.fx_{name}"})
        result = await configure(hass, fid, {"attribute": "detailedForecast"})
        assert result["step_id"] == "source_mapping"
        result = await configure(hass, fid, result["data_schema"]({}))
        assert result["step_id"] == "sources", result.get("errors")
    expected = deepcopy(configuration)
    set_pv_group(
        expected,
        0,
        [
            solcast_pv_binding(EntityBinding("sensor.fx_today"), ZONE),
            solcast_pv_binding(EntityBinding("sensor.fx_tomorrow"), ZONE),
        ],
    )
    assert draft(hass, fid)["sources"]["pv"] == expected["sources"]["pv"]


async def test_load_statistic_flow_matches_builder(
    recorder_mock, hass, enable_custom_integrations
):
    configuration = config()
    fid = await start(hass, configuration)
    await add_source(hass, fid, "load", "statistic")
    result = await configure(
        hass,
        fid,
        {"statistic_id": "sensor.fx_load_total", "unit": "Wh", "sign": 1},
    )
    assert result["step_id"] == "sources", result.get("errors")
    expected = deepcopy(configuration)
    set_load_statistic(expected, "sensor.fx_load_total", "Wh", 1)
    assert draft(hass, fid)["sources"]["load"] == expected["sources"]["load"]


@pytest.mark.parametrize(
    ("target", "attribute"), [("soc", None), ("bms_soc", "BMS SOC")]
)
async def test_soc_flow_matches_builder(
    recorder_mock, hass, enable_custom_integrations, freezer, target, attribute
):
    freezer.move_to(NOW)
    configuration = config()
    fid = await start(hass, configuration)
    hass.states.async_set(
        "sensor.fx_soc", "55", {"unit_of_measurement": "%", "BMS SOC": 54}
    )
    await add_source(hass, fid, target, "measurement")
    await configure(hass, fid, {"entity_id": "sensor.fx_soc"})
    result = await configure(hass, fid, {"attribute": attribute} if attribute else {})
    assert result["step_id"] == "source_measurement"
    result = await configure(hass, fid, result["data_schema"]({}))
    assert result["step_id"] == "sources", result.get("errors")
    expected = deepcopy(configuration)
    set_soc(
        expected,
        EntityBinding("sensor.fx_soc", None, attribute),
        bms=target == "bms_soc",
    )
    assert draft(hass, fid)["sources"][target] == expected["sources"][target]
    assert draft(hass, fid)["soc_options"] == expected["soc_options"]


@pytest.mark.parametrize(
    ("row", "unit", "sign", "age"),
    [
        ("battery_power", "W", 1, 3600),
        ("grid_export_power", "W", -1, 3600),
        ("pv_energy_today", "Wh", 1, 86400),
        ("pv_energy", "kWh", 1, 86400),
    ],
)
async def test_measurement_flow_matches_builder(
    recorder_mock, hass, enable_custom_integrations, freezer, row, unit, sign, age
):
    freezer.move_to(NOW)
    fid = await start(hass, config())
    hass.states.async_set("sensor.fx_meter", "5", {"unit_of_measurement": unit})
    await add_source(hass, fid, row, "measurement")
    await configure(hass, fid, {"entity_id": "sensor.fx_meter"})
    result = await configure(hass, fid, {})
    assert result["step_id"] == "source_measurement"
    result = await configure(
        hass,
        fid,
        {
            "unit": unit,
            "sign": sign,
            "timestamp_path": "last_updated",
            "max_age_seconds": age,
        },
    )
    assert result["step_id"] == "sources", result.get("errors")
    expected = measurement_setting(
        row,
        EntityBinding("sensor.fx_meter"),
        source_unit=unit,
        sign=sign,
        max_age_seconds=age,
    )
    assert draft(hass, fid)["measurements"][row] == expected
