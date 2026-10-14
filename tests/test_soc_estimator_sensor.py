"""The diagnostic sensor, the drift repair issue and the diagnostics block."""

import json
from datetime import timedelta

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity import EntityCategory
from test_soc_estimator_coordinator import START, _config, _set_sources, _setup

from custom_components.energy_compass.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.energy_compass.settings import options_require_reload

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")


def _entity_id(hass, entry):
    return er.async_get(hass).async_get_entity_id(
        "sensor", "energy_compass", f"{entry.entry_id}_soc_estimator"
    )


async def test_the_sensor_shows_the_estimate_and_its_detail(hass, freezer):
    freezer.move_to(START)
    _set_sources(hass)
    entry = await _setup(hass)
    entity_id = _entity_id(hass, entry)
    state = hass.states.get(entity_id)
    assert state.state == "50.0"
    assert state.attributes["unit_of_measurement"] == "%"
    assert state.attributes["anchor_kind"] == "initial"
    assert state.attributes["energy_kwh"] == pytest.approx(10.0)
    assert state.attributes["unanchored_top"] is False
    assert state.attributes["excess_kwh"] == 0
    assert state.attributes["bms_soc"] is None
    registered = er.async_get(hass).async_get(entity_id)
    assert registered.entity_category is EntityCategory.DIAGNOSTIC
    freezer.tick(timedelta(minutes=1))
    _set_sources(hass, charge=100.4)
    await hass.async_block_till_done()
    assert float(hass.states.get(entity_id).state) == pytest.approx(51.8)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_only_the_anchor_kind_is_recorded(hass, freezer):
    freezer.move_to(START)
    _set_sources(hass)
    entry = await _setup(hass)
    entity = hass.data["sensor"].get_entity(_entity_id(hass, entry))
    assert entity._unrecorded_attributes >= {"energy_kwh", "full_candidates", "anchors"}
    assert "anchor_kind" not in entity._unrecorded_attributes
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_sensor_goes_unavailable_with_the_counters(hass, freezer):
    freezer.move_to(START)
    _set_sources(hass)
    entry = await _setup(hass)
    entity_id = _entity_id(hass, entry)
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set("sensor.charged", "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_no_sensor_without_the_estimator_and_one_in_shadow(hass, freezer):
    freezer.move_to(START)
    _set_sources(hass)
    config = _config()
    config["sources"].update(soc_mode="entity")
    config["soc_estimator"]["shadow"] = False
    config["sources"]["soc"] = {"entity_id": "sensor.soc", "registry_id": None}
    hass.states.async_set("sensor.soc", "60", {"unit_of_measurement": "%"})
    entry = await _setup(hass, config)
    assert _entity_id(hass, entry) is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    config["soc_estimator"]["shadow"] = True
    shadow = await _setup(hass, config)
    assert _entity_id(hass, shadow) is not None
    assert hass.states.get(_entity_id(hass, shadow)).attributes["mode"] == "shadow"
    assert await hass.config_entries.async_unload(shadow.entry_id)


def test_turning_the_estimator_on_or_off_reloads_the_entry():
    off, on = _config(), _config()
    off["sources"]["soc_mode"] = "entity"
    off["soc_estimator"]["shadow"] = False
    assert options_require_reload(off, on)
    assert options_require_reload(on, off)
    assert not options_require_reload(on, _config())


async def test_a_long_run_without_an_anchor_raises_a_repair_issue(hass, freezer):
    freezer.move_to(START)
    _set_sources(hass)
    config = _config(drift_warn_cycles=0.5, eta_charge=1.0, capacity_kwh=8)
    entry = await _setup(hass, config)
    issues = ir.async_get(hass)
    issue_id = f"soc_estimator_unanchored_{entry.entry_id}"
    assert issues.async_get_issue("energy_compass", issue_id) is None
    charge, discharge = 100.0, 50.0
    for _ in range(12):
        freezer.tick(timedelta(minutes=2))
        charge += 0.5
        discharge += 0.5
        _set_sources(hass, charge=charge, discharge=discharge)
        await hass.async_block_till_done()
    issue = issues.async_get_issue("energy_compass", issue_id)
    assert issue is not None
    assert issue.translation_key == "soc_estimator_unanchored"
    assert issue.is_fixable is False
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_are_bounded_and_name_no_entity(hass, freezer):
    freezer.move_to(START)
    _set_sources(hass)
    entry = await _setup(hass)
    estimator = entry.runtime_data.soc_estimator
    for index in range(40):
        estimator.state.candidates.append({"started_at": str(index)})
        estimator._remember(estimator.state.anchor)
    block = (await async_get_config_entry_diagnostics(hass, entry))["soc_estimator"]
    assert block["enabled"] is True
    assert len(block["anchors"]) == 20
    assert block["anchor_kind"] == "initial"
    text = json.dumps(block)
    assert "sensor." not in text
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_say_when_the_estimator_is_off(hass, freezer):
    freezer.move_to(START)
    config = _config()
    config["sources"].update(soc_mode="entity")
    config["soc_estimator"]["shadow"] = False
    config["sources"]["soc"] = {"entity_id": "sensor.soc", "registry_id": None}
    hass.states.async_set("sensor.soc", "60", {"unit_of_measurement": "%"})
    entry = await _setup(hass, config)
    block = (await async_get_config_entry_diagnostics(hass, entry))["soc_estimator"]
    assert block == {"enabled": False}
    assert await hass.config_entries.async_unload(entry.entry_id)
