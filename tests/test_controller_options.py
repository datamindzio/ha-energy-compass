import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from controller_support import PREFIX, START, register_solarman
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass import controller as core
from custom_components.energy_compass.coordinator import EnergyCompassCoordinator
from custom_components.energy_compass.settings import (
    default_configuration,
    merged_configuration,
)

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

COMPONENT = Path(__file__).parents[1] / "custom_components/energy_compass"


@pytest.fixture(autouse=True)
def quiet_solves(freezer):
    freezer.move_to(START)
    with patch.object(EnergyCompassCoordinator, "async_recalculate", AsyncMock()):
        yield


async def make_entry(hass, options=None, title="Synthetic", load=True):
    entry = MockConfigEntry(
        domain="energy_compass",
        data=default_configuration("EUR", "UTC"),
        options=options or {},
        title=title,
        version=3,
    )
    entry.add_to_hass(hass)
    if load:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def open_step(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "deye_controller"}
    )


async def submit(hass, form, data):
    return await hass.config_entries.options.async_configure(form["flow_id"], data)


def suggested(form, key):
    (field,) = [item for item in form["data_schema"].schema if item == key]
    return field.description["suggested_value"]


async def test_enabling_saves_reloads_once_and_creates_the_entities(hass):
    device = register_solarman(hass)
    entry = await make_entry(hass)
    before = entry.runtime_data
    assert before.controller is None
    form = await open_step(hass, entry)
    assert form["step_id"] == "deye_controller"
    result = await submit(hass, form, {"enabled": True, "device_id": device.id})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["controller"] == {"enabled": True, "device_id": device.id}
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not before
    assert entry.runtime_data.controller.resolution.prefix == PREFIX
    assert hass.states.get("sensor.synthetic_deye_controller") is not None
    assert hass.states.get("select.synthetic_deye_mode") is not None
    assert hass.states.get("sensor.synthetic_deye_controller_runtime") is not None


async def test_changing_only_the_device_applies_live(hass):
    first = register_solarman(hass)
    second = register_solarman(hass, prefix="second_program_", tag="second")
    entry = await make_entry(
        hass, {"controller": {"enabled": True, "device_id": first.id}}
    )
    coordinator = entry.runtime_data
    form = await open_step(hass, entry)
    result = await submit(hass, form, {"enabled": True, "device_id": second.id})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator
    assert entry.options["controller"]["device_id"] == second.id
    attrs = hass.states.get("sensor.synthetic_deye_controller").attributes
    assert attrs["program_prefix"] == "second_program_"
    assert attrs["device_id"] == second.id
    assert coordinator.controller.state.history[-1].kind == "device"


async def test_disabling_reloads_and_removes_the_controller(hass):
    device = register_solarman(hass)
    entry = await make_entry(
        hass, {"controller": {"enabled": True, "device_id": device.id}}
    )
    form = await open_step(hass, entry)
    await submit(hass, form, {"enabled": False, "device_id": device.id})
    await hass.async_block_till_done()
    assert entry.options["controller"]["enabled"] is False
    assert entry.runtime_data.controller is None
    state = hass.states.get("sensor.synthetic_deye_controller")
    assert state is None or state.state == "unavailable"


@pytest.mark.parametrize("hold", ["mode", "restore_pending"])
@pytest.mark.parametrize("change", ["disable", "device"])
async def test_the_controller_is_not_released_while_it_still_owns_the_inverter(
    hass, hold, change
):
    first = register_solarman(hass)
    second = register_solarman(hass, prefix="second_program_", tag="second")
    entry = await make_entry(
        hass, {"controller": {"enabled": True, "device_id": first.id}}
    )
    controller = entry.runtime_data.controller
    if hold == "mode":
        controller.async_set_mode("Auto")
    else:
        controller.async_update_runtime(None, True)
    form = await open_step(hass, entry)
    data = (
        {"enabled": False, "device_id": first.id}
        if change == "disable"
        else {"enabled": True, "device_id": second.id}
    )
    result = await submit(hass, form, data)
    assert result["type"] == "form"
    assert result["errors"] == {"base": "controller_not_released"}
    assert entry.options["controller"] == {"enabled": True, "device_id": first.id}
    controller.async_set_mode("Off")
    controller.async_update_runtime(None, False)
    result = await submit(hass, result, data)
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["controller"]["enabled"] is (change == "device")


async def test_saving_without_a_change_does_not_reload(hass):
    device = register_solarman(hass)
    entry = await make_entry(
        hass, {"controller": {"enabled": True, "device_id": device.id}}
    )
    coordinator = entry.runtime_data
    form = await open_step(hass, entry)
    await submit(hass, form, {"enabled": True, "device_id": device.id})
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator


async def test_enabling_needs_a_device(hass):
    entry = await make_entry(hass)
    form = await open_step(hass, entry)
    result = await submit(hass, form, {"enabled": True})
    assert result["type"] == "form"
    assert result["errors"] == {"device_id": "controller_device_required"}
    assert "controller" not in entry.options


async def test_a_device_without_the_six_programs_is_refused(hass):
    device = register_solarman(hass)
    registry = er.async_get(hass)
    registry.async_remove(f"number.{PREFIX}4_soc")
    entry = await make_entry(hass)
    form = await open_step(hass, entry)
    result = await submit(hass, form, {"enabled": True, "device_id": device.id})
    assert result["errors"] == {"device_id": "controller_tou_incomplete"}


async def test_renamed_program_entities_are_refused(hass):
    device = register_solarman(hass)
    er.async_get(hass).async_update_entity(
        f"number.{PREFIX}2_power", new_entity_id="number.custom_name"
    )
    entry = await make_entry(hass)
    form = await open_step(hass, entry)
    result = await submit(hass, form, {"enabled": True, "device_id": device.id})
    assert result["errors"] == {"device_id": "controller_tou_prefix"}


async def test_a_device_cannot_be_controlled_by_two_entries(hass):
    device = register_solarman(hass)
    await make_entry(
        hass, {"controller": {"enabled": True, "device_id": device.id}}, title="First"
    )
    other = await make_entry(hass, title="Second")
    form = await open_step(hass, other)
    result = await submit(hass, form, {"enabled": True, "device_id": device.id})
    assert result["errors"] == {"device_id": "controller_device_in_use"}


async def test_a_lone_solarman_device_is_preselected(hass):
    device = register_solarman(hass)
    entry = await make_entry(hass)
    form = await open_step(hass, entry)
    assert suggested(form, "device_id") == device.id


async def test_two_solarman_devices_are_not_guessed(hass):
    register_solarman(hass)
    register_solarman(hass, prefix="second_program_", tag="second")
    entry = await make_entry(hass)
    form = await open_step(hass, entry)
    assert suggested(form, "device_id") is None


async def test_status_texts(hass):
    device = register_solarman(hass)
    plain = await make_entry(hass, title="Plain")
    form = await open_step(hass, plain)
    assert form["description_placeholders"] == {"status": "Not configured."}
    enabled = await make_entry(
        hass, {"controller": {"enabled": True, "device_id": device.id}}
    )
    form = await open_step(hass, enabled)
    assert form["description_placeholders"] == {
        "status": "TOU programs: prefix inverter_deye_program_, 30 entities."
    }
    hass.config.language = "pl"
    form = await open_step(hass, enabled)
    assert form["description_placeholders"] == {
        "status": "Programy TOU: prefiks inverter_deye_program_, 30 encji."
    }
    form = await open_step(hass, plain)
    assert form["description_placeholders"] == {"status": "Nie skonfigurowano."}


async def test_the_menu_offers_the_step_before_the_atlas_items(hass):
    entry = await make_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    options = result["menu_options"]
    assert options.index("deye_controller") + 1 == options.index("energy_atlas")


async def test_reconfigure_keeps_the_controller_settings(hass):
    device = register_solarman(hass)
    entry = await make_entry(
        hass, {"controller": {"enabled": True, "device_id": device.id}}
    )
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "preview"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"confirm": True}
    )
    assert result["type"] == "abort"
    await hass.async_block_till_done()
    assert entry.options["controller"] == {"enabled": True, "device_id": device.id}


def add_package_mode(hass):
    platform, unique_id = core.PACKAGE_IDENTITIES["mode"]
    er.async_get(hass).async_get_or_create("input_select", platform, unique_id)


async def test_package_users_get_the_controller_enabled_before_the_platforms(hass):
    device = register_solarman(hass)
    add_package_mode(hass)
    with (
        patch.object(type(hass.config_entries), "async_schedule_reload") as scheduled,
        patch.object(type(hass.config_entries), "async_reload") as reloaded,
    ):
        entry = await make_entry(hass)
    assert entry.options["controller"] == {"enabled": True, "device_id": device.id}
    assert entry.runtime_data.controller is not None
    scheduled.assert_not_called()
    reloaded.assert_not_called()
    assert hass.states.get("select.synthetic_deye_mode").state == "Off"
    kinds = [event.kind for event in entry.runtime_data.controller.state.history]
    assert kinds == ["auto_enabled"]


async def test_auto_enable_needs_exactly_one_entry(hass):
    register_solarman(hass)
    add_package_mode(hass)
    first = await make_entry(hass, title="First", load=False)
    second = await make_entry(hass, title="Second", load=False)
    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()
    for entry in (first, second):
        assert entry.state is ConfigEntryState.LOADED
        assert "controller" not in entry.options
        assert entry.runtime_data.controller is None


async def test_auto_enable_respects_an_explicit_choice(hass):
    device = register_solarman(hass)
    add_package_mode(hass)
    entry = await make_entry(
        hass, {"controller": {"enabled": False, "device_id": device.id}}
    )
    assert entry.options["controller"]["enabled"] is False
    assert entry.runtime_data.controller is None


async def test_auto_enable_needs_the_package(hass):
    register_solarman(hass)
    entry = await make_entry(hass)
    assert "controller" not in entry.options


async def test_auto_enable_needs_one_resolvable_device(hass):
    register_solarman(hass)
    register_solarman(hass, prefix="second_program_", tag="second")
    add_package_mode(hass)
    entry = await make_entry(hass)
    assert "controller" not in entry.options


async def test_the_controller_key_is_not_part_of_the_configuration(hass):
    device = register_solarman(hass)
    entry = await make_entry(
        hass, {"controller": {"enabled": True, "device_id": device.id}}, load=False
    )
    assert merged_configuration(entry) == json.loads(json.dumps(dict(entry.data)))
    with_configuration = MockConfigEntry(
        domain="energy_compass",
        data=default_configuration("EUR", "UTC"),
        options={
            "controller": {"enabled": True, "device_id": "x"},
            "configuration": default_configuration("PLN", "UTC"),
        },
        version=3,
    )
    assert merged_configuration(with_configuration)["currency"] == "PLN"
    assert "controller" not in merged_configuration(with_configuration)


@pytest.mark.parametrize(
    "path", ["strings.json", "translations/en.json", "translations/pl.json"]
)
def test_option_texts_exist_in_every_translation_file(path):
    options = json.loads((COMPONENT / path).read_text())["options"]
    assert options["step"]["menu"]["menu_options"]["deye_controller"]
    step = options["step"]["deye_controller"]
    assert "{status}" in step["description"]
    assert set(step["data"]) == {"enabled", "device_id"}
    assert set(options["error"]) >= {
        "controller_device_required",
        "controller_tou_incomplete",
        "controller_tou_prefix",
        "controller_device_in_use",
        "controller_not_released",
    }
