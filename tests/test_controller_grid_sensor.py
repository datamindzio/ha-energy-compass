"""The grid outage binary sensor of the native controller."""

import json
from pathlib import Path

import pytest
from controller_support import tick
from homeassistant.helpers import entity_registry as er
from test_controller_grid_ha import GRID, add_grid, attach

pytestmark = pytest.mark.usefixtures("recorder_mock")

COMPONENT = Path(__file__).parents[1] / "custom_components/energy_compass"
OUTAGE = "binary_sensor.synthetic_grid_outage"


async def test_the_sensor_is_registered_by_key(hass, controller_site):
    entry = er.async_get(hass).async_get(OUTAGE)
    assert entry.unique_id == f"{controller_site.entry_id}_grid_outage"
    assert entry.translation_key == "grid_outage"
    assert entry.platform == "energy_compass"


async def test_without_a_grid_source_it_is_unavailable(hass, controller_site):
    assert hass.states.get(OUTAGE).state == "unavailable"


async def test_it_follows_the_outage_with_its_attributes(
    hass, controller_site, freezer
):
    add_grid(hass, controller_site)
    hass.states.async_set(GRID, "on")
    await attach(hass, controller_site)
    state = hass.states.get(OUTAGE)
    assert state.state == "off"
    assert state.attributes["device_class"] == "problem"
    assert state.attributes["since"] is None
    assert state.attributes["source"] == "entity"
    hass.states.async_set(GRID, "off")
    await hass.async_block_till_done()
    await tick(hass, freezer, 16)
    state = hass.states.get(OUTAGE)
    assert state.state == "on"
    assert state.attributes["since"]
    assert state.attributes["lost_at"]
    assert state.attributes["restored_pending_since"] is None
    assert state.attributes["source_state"] == "off"
    hass.states.async_set(GRID, "on")
    await hass.async_block_till_done()
    await tick(hass, freezer, 60)
    state = hass.states.get(OUTAGE)
    assert state.state == "on"
    assert state.attributes["restored_pending_since"]
    await tick(hass, freezer, 5 * 60)
    state = hass.states.get(OUTAGE)
    assert state.state == "off"
    assert state.attributes["since"] is None


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("strings.json", "Grid outage"),
        ("translations/en.json", "Grid outage"),
        ("translations/pl.json", "Awaria sieci"),
    ],
)
def test_the_name_exists_in_every_translation_file(path, name):
    entity = json.loads((COMPONENT / path).read_text())["entity"]
    assert entity["binary_sensor"]["grid_outage"]["name"] == name
    attributes = entity["binary_sensor"]["grid_outage"]["state_attributes"]
    assert {
        "since",
        "lost_at",
        "restored_pending_since",
        "source",
        "source_state",
    } <= set(attributes)
