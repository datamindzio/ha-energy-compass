"""The SOC source choice and the estimator group in the options flow."""

from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntryState
from native_site import OPERATION, native_site
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.settings import (
    default_configuration,
    merged_configuration,
)
from custom_components.energy_compass.sources.bindings import EntityBinding

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

SOURCES = {
    "charge_counter": "sensor.charged",
    "discharge_counter": "sensor.discharged",
    "voltage": "sensor.pack_v",
    "current": "sensor.pack_a",
}
TEMPLATE = {
    "anchor_kwh": 21.3,
    "anchor_charge": 6288.3,
    "anchor_discharge": 5662.4,
    "anchor_kind": "corrected",
    "anchor_at": "2026-10-12T19:06:30+00:00",
    "pending_kwh": 0.1,
    "unit_of_measurement": "%",
}


def _sources(hass):
    for entity, state, unit in (
        ("sensor.charged", "6300.0", "kWh"),
        ("sensor.discharged", "5670.0", "kWh"),
        ("sensor.pack_v", "520", "V"),
        ("sensor.pack_a", "0", "A"),
    ):
        hass.states.async_set(entity, state, {"unit_of_measurement": unit})


def _entry(hass, **options):
    _sources(hass)
    config = default_configuration("EUR", "UTC")
    config["sources"].update(
        battery_enabled=True, soc=EntityBinding("sensor.template_soc").to_dict()
    )
    hass.states.async_set("sensor.template_soc", "60", TEMPLATE)
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Home", version=3, options=options
    )
    entry.add_to_hass(hass)
    return entry


async def _unload(hass, entry):
    if entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def _open(hass, entry, *, expert=False):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow = result["flow_id"]
    if expert:
        result = await hass.config_entries.options.async_configure(
            flow, {"next_step_id": "show_expert"}
        )
    assert "soc_estimator" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        flow, {"next_step_id": "soc_estimator"}
    )
    assert result["step_id"] == "soc_estimator"
    return flow, result


def _fields(form):
    return {str(key) for key in form["data_schema"].schema}


async def test_the_step_is_only_in_the_menu_with_a_battery(hass):
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(domain="energy_compass", data=config, version=3)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "soc_estimator" not in result["menu_options"]


async def test_choosing_the_estimator_replaces_the_soc_binding_and_seeds_it(hass):
    entry = _entry(hass)
    flow, form = await _open(hass, entry)
    assert "seed_from_template" in _fields(form)
    assert "sensor.template_soc" in form["description_placeholders"]["seed"]
    assert "capacity_kwh" not in _fields(form)
    result = await hass.config_entries.options.async_configure(
        flow,
        {
            "soc_source": "counter_estimator",
            **SOURCES,
            "empty_voltage": 480,
            "anchor_on_candidates": False,
            "shadow": False,
            "seed_from_template": True,
        },
    )
    assert result["step_id"] == "menu"
    result = await hass.config_entries.options.async_configure(
        flow, {"next_step_id": "preview"}
    )
    result = await hass.config_entries.options.async_configure(flow, {"confirm": True})
    assert result["type"] == "create_entry", result
    saved = merged_configuration(entry)
    assert saved["sources"]["soc_mode"] == "counter_estimator"
    assert saved["sources"]["soc"] is None
    assert saved["soc_estimator"]["empty_voltage"] == 480
    assert saved["soc_estimator"]["seed"]["anchor_kwh"] == 21.3
    assert saved["soc_estimator"]["bindings"]["voltage"]["entity_id"] == "sensor.pack_v"
    await hass.async_block_till_done(wait_background_tasks=True)
    await _unload(hass, entry)


async def test_the_pack_voltage_that_is_zero_is_required(hass):
    entry = _entry(hass)
    flow, _ = await _open(hass, entry)
    result = await hass.config_entries.options.async_configure(
        flow,
        {
            "soc_source": "counter_estimator",
            **SOURCES,
            "anchor_on_candidates": False,
            "shadow": False,
        },
    )
    assert result["step_id"] == "soc_estimator"
    assert result["errors"] == {"base": "invalid_input"}
    assert "0 %" in result["description_placeholders"]["detail"]
    assert "soc_mode" not in merged_configuration(entry)["sources"]


async def test_the_entity_source_needs_no_estimator_settings(hass):
    entry = _entry(hass)
    flow, _ = await _open(hass, entry)
    result = await hass.config_entries.options.async_configure(
        flow,
        {"soc_source": "entity", "anchor_on_candidates": False, "shadow": False},
    )
    assert result["step_id"] == "menu"


async def test_shadow_runs_beside_the_bound_entity(hass):
    entry = _entry(hass)
    flow, _ = await _open(hass, entry)
    result = await hass.config_entries.options.async_configure(
        flow,
        {
            "soc_source": "entity",
            **SOURCES,
            "empty_voltage": 480,
            "anchor_on_candidates": False,
            "shadow": True,
            "seed_from_template": True,
        },
    )
    assert result["step_id"] == "menu"
    await hass.config_entries.options.async_configure(flow, {"next_step_id": "preview"})
    result = await hass.config_entries.options.async_configure(flow, {"confirm": True})
    assert result["type"] == "create_entry", result
    saved = merged_configuration(entry)
    assert saved["sources"]["soc"]["entity_id"] == "sensor.template_soc"
    assert saved["soc_estimator"]["shadow"] is True
    assert saved["soc_estimator"]["seed"]["anchor_kind"] == "corrected"
    await hass.async_block_till_done(wait_background_tasks=True)
    await _unload(hass, entry)


async def test_expert_fields_appear_only_with_expert_settings(hass):
    entry = _entry(hass)
    _, hidden = await _open(hass, entry)
    entry2 = _entry(hass)
    _, shown = await _open(hass, entry2, expert=True)
    for key in ("capacity_kwh", "pv_power", "grid_sign", "bms_soc", "rated_pv_w"):
        assert key not in _fields(hidden)
        assert key in _fields(shown)


async def test_the_efficiency_is_suggested_from_the_round_trip_else_the_default(hass):
    entry = _entry(hass)
    _, form = await _open(hass, entry)
    schema = {str(key): key for key in form["data_schema"].schema}
    assert schema["eta_charge"].description["suggested_value"] == 0.95
    assert schema["full_hold_seconds"].description["suggested_value"] == 600
    assert schema["drift_warn_cycles"].description["suggested_value"] == 3
    assert "empty_voltage" in schema
    assert schema["empty_voltage"].description["suggested_value"] is None


async def test_the_estimator_is_refused_while_the_inverter_is_in_capacity_mode(
    hass, freezer
):
    entry, _ = await native_site(hass, freezer)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "configuration": {
                **default_configuration("EUR", "UTC"),
                "sources": {
                    **default_configuration("EUR", "UTC")["sources"],
                    "battery_enabled": True,
                    "soc": EntityBinding("sensor.template_soc").to_dict(),
                },
            },
        },
    )
    hass.states.async_set("sensor.template_soc", "60", {"unit_of_measurement": "%"})
    flow, _ = await _open(hass, entry)
    values = {
        "soc_source": "counter_estimator",
        **SOURCES,
        "empty_voltage": 480,
        "anchor_on_candidates": False,
        "shadow": False,
    }
    assert hass.states.get(OPERATION).state == "Capacity"
    refused = await hass.config_entries.options.async_configure(flow, values)
    assert refused["errors"] == {"base": "soc_estimator_capacity_mode"}
    hass.states.async_set(OPERATION, "Voltage", hass.states.get(OPERATION).attributes)
    accepted = await hass.config_entries.options.async_configure(flow, values)
    assert accepted["step_id"] == "menu"
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_the_measured_round_trip_becomes_the_suggested_efficiency(hass):
    entry = _entry(hass)
    entry.runtime_data = SimpleNamespace(
        calibration_report=lambda: {"battery_round_trip": {"measured": 0.937}}
    )
    _, form = await _open(hass, entry)
    schema = {str(key): key for key in form["data_schema"].schema}
    assert schema["eta_charge"].description["suggested_value"] == 0.937
    assert "0.937" in form["description_placeholders"]["round_trip"]
