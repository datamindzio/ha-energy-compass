import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from controller_support import (
    START,
    controller_of,
    iso,
    publication,
    publish,
    tick,
)
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass import controller as core
from custom_components.energy_compass.controller_entity import (
    DeyeControllerSensor,
    DeyeRuntimeSensor,
)
from custom_components.energy_compass.coordinator import EnergyCompassCoordinator
from custom_components.energy_compass.settings import default_configuration

pytestmark = pytest.mark.usefixtures("recorder_mock")

COMPONENT = Path(__file__).parents[1] / "custom_components/energy_compass"
CONTROLLER = "sensor.synthetic_deye_controller"
MODE = "select.synthetic_deye_mode"
RUNTIME = "sensor.synthetic_deye_controller_runtime"


def test_the_three_entities_are_registered_by_key(hass, controller_site):
    registry = er.async_get(hass)
    expected = {
        CONTROLLER: ("deye_controller", "deye_controller"),
        MODE: ("deye_mode", "deye_mode"),
        RUNTIME: ("deye_runtime", "deye_runtime"),
    }
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("energy_compass", controller_site.entry_id), controller_site.entry_id
    )
    for entity_id, (key, translation) in expected.items():
        entry = registry.async_get(entity_id)
        assert entry.unique_id == f"{controller_site.entry_id}_{key}"
        assert entry.translation_key == translation
        assert entry.platform == "energy_compass"
        assert entry.device_id == device.id


def test_entity_classes_hold_the_documented_unrecorded_sets():
    assert DeyeControllerSensor._unrecorded_attributes == {"accepted", "tou"}
    assert DeyeRuntimeSensor._unrecorded_attributes == {"runtime", "updated_at"}
    assert core.MODES == ("Off", "Simulation", "Auto")


async def test_controller_sensor_attributes_follow_the_contract(hass, controller_site):
    publish(controller_site, publication())
    await hass.async_block_till_done()
    state = hass.states.get(CONTROLLER)
    assert state.state == "2026-10-08T17:15:00+00:00"
    attrs = [key for key in state.attributes if key in core.CONTROLLER_ATTRIBUTES]
    assert tuple(attrs) == core.CONTROLLER_ATTRIBUTES
    assert state.attributes["mode_entity"] == MODE
    assert state.attributes["plan_reason"] == "ok"
    assert state.attributes["accepted"]["generated_at"] == iso(17, 0, 45)
    assert state.attributes["device_class"] == "timestamp"


async def test_selecting_a_mode_updates_store_select_and_controller_together(
    hass, controller_site
):
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": MODE, "option": "Simulation"},
        blocking=True,
    )
    assert controller_of(controller_site).state.mode == "Simulation"
    assert hass.states.get(MODE).state == "Simulation"
    assert hass.states.get(CONTROLLER).attributes["mode"] == "Simulation"


async def test_mode_select_starts_off_with_the_three_options(hass, controller_site):
    state = hass.states.get(MODE)
    assert state.state == "Off"
    assert state.attributes["options"] == ["Off", "Simulation", "Auto"]


async def test_runtime_sensor_state_is_the_code(hass, controller_site):
    assert hass.states.get(RUNTIME).state == "waiting"
    controller_of(controller_site).async_update_runtime({"code": "blocked"}, None)
    await hass.async_block_till_done()
    state = hass.states.get(RUNTIME)
    assert state.state == "blocked"
    assert state.attributes["runtime"] == {"code": "blocked"}


async def test_runtime_detail_changes_are_delayed_and_summary_changes_are_not(
    hass, controller_site, freezer
):
    controller = controller_of(controller_site)
    writes = []
    hass.bus.async_listen(
        EVENT_STATE_CHANGED,
        lambda event: (
            writes.append(event) if event.data["entity_id"] == RUNTIME else None
        ),
    )
    controller.async_update_runtime({"code": "ok", "reason": "a"}, None)
    await hass.async_block_till_done()
    assert len(writes) == 1
    controller.async_update_runtime(
        {"code": "ok", "reason": "a", "last_confirmation": "2026-10-08T17:01:00"},
        None,
    )
    await hass.async_block_till_done()
    assert len(writes) == 1
    assert "last_confirmation" not in hass.states.get(RUNTIME).attributes["runtime"]
    await tick(hass, freezer, 59)
    assert len(writes) == 1
    await tick(hass, freezer, 2)
    assert len(writes) == 2
    assert "last_confirmation" in hass.states.get(RUNTIME).attributes["runtime"]
    controller.async_update_runtime({"code": "restored", "reason": "a"}, None)
    await hass.async_block_till_done()
    assert len(writes) == 3
    assert hass.states.get(RUNTIME).state == "restored"


async def test_restore_pending_change_is_visible_on_both_entities_at_once(
    hass, controller_site
):
    controller_of(controller_site).async_update_runtime(None, True)
    await hass.async_block_till_done()
    assert hass.states.get(CONTROLLER).attributes["restore_pending"] is True
    assert hass.states.get(RUNTIME).attributes["restore_pending"] is True


async def test_nothing_is_created_when_the_controller_is_disabled(
    hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    entry = MockConfigEntry(
        domain="energy_compass",
        data=default_configuration("EUR", "UTC"),
        title="Other",
        version=3,
    )
    entry.add_to_hass(hass)
    with patch.object(EnergyCompassCoordinator, "async_recalculate", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    found = [
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if "deye" in item.unique_id
    ]
    assert found == []
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "path, names",
    [
        (
            "strings.json",
            ("Deye controller", "Deye mode", "Deye controller runtime"),
        ),
        (
            "translations/en.json",
            ("Deye controller", "Deye mode", "Deye controller runtime"),
        ),
        (
            "translations/pl.json",
            ("Sterownik Deye", "Tryb sterownika Deye", "Stan pracy sterownika Deye"),
        ),
    ],
)
def test_entity_names_exist_in_every_translation_file(path, names):
    entity = json.loads((COMPONENT / path).read_text())["entity"]
    assert entity["sensor"]["deye_controller"]["name"] == names[0]
    assert entity["select"]["deye_mode"]["name"] == names[1]
    assert entity["sensor"]["deye_runtime"]["name"] == names[2]
