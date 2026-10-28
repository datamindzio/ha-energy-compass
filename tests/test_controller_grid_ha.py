"""The grid presence watch inside Home Assistant: sources, debounce, edges, persistence."""

import asyncio
from unittest.mock import Mock

from controller_support import controller_of, tick
from homeassistant.helpers import entity_registry as er

from custom_components.energy_compass import controller as core
from custom_components.energy_compass.diagnostics import (
    async_get_config_entry_diagnostics,
)

GRID = "binary_sensor.inverter_deye_grid"
VOLTAGE = "sensor.inverter_deye_grid_l1_voltage"


def add_grid(hass, entry, translation_key="grid"):
    controller = controller_of(entry)
    return er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "solarman",
        "inverter_grid",
        device_id=controller.device_id,
        translation_key=translation_key,
        suggested_object_id="inverter_deye_grid",
    )


def add_voltage(hass, entry):
    controller = controller_of(entry)
    return er.async_get(hass).async_get_or_create(
        "sensor",
        "solarman",
        "inverter_grid_l1_voltage",
        device_id=controller.device_id,
        translation_key="grid_l1_voltage",
        suggested_object_id="inverter_deye_grid_l1_voltage",
    )


async def attach(hass, entry):
    """Let the controller notice a newly registered entity (the registry event)."""
    await hass.async_block_till_done()
    return controller_of(entry)


def watch(entry):
    return controller_of(entry).state.grid


async def test_without_a_source_nothing_is_watched(controller_site, hass, freezer):
    controller = controller_of(controller_site)
    assert controller.view()[1]["grid"] is None
    await tick(hass, freezer, 3600)
    assert controller.state.grid == core.GridWatch()
    block = (await async_get_config_entry_diagnostics(hass, controller_site))[
        "deye_controller"
    ]["grid"]
    assert block["presence_entity"] is None
    assert block["voltage_entities"] == []
    assert block["edges"] == []


async def test_the_presence_entity_is_found_on_the_device(
    controller_site, hass, freezer
):
    add_grid(hass, controller_site)
    hass.states.async_set(GRID, "on")
    controller = await attach(hass, controller_site)
    attrs = controller.view()[1]["grid"]
    assert attrs["source"] == "entity"
    assert attrs["present"] is True
    assert attrs["outage"] is False


async def test_the_presence_entity_is_found_by_object_id_without_a_translation_key(
    controller_site, hass, freezer
):
    add_grid(hass, controller_site, translation_key=None)
    hass.states.async_set(GRID, "on")
    controller = await attach(hass, controller_site)
    assert controller.view()[1]["grid"]["source"] == "entity"


async def test_a_short_dropout_is_not_an_outage(controller_site, hass, freezer):
    add_grid(hass, controller_site)
    hass.states.async_set(GRID, "on")
    controller = await attach(hass, controller_site)
    changed = Mock()
    controller_site.runtime_data.async_grid_changed = changed
    hass.states.async_set(GRID, "off")
    await hass.async_block_till_done()
    await tick(hass, freezer, 10)
    hass.states.async_set(GRID, "on")
    await tick(hass, freezer, 30)
    assert controller.state.grid.active is False
    changed.assert_not_called()


async def test_an_outage_starts_after_the_debounce_and_ends_when_stable(
    controller_site, hass, freezer
):
    add_grid(hass, controller_site)
    hass.states.async_set(GRID, "on")
    controller = await attach(hass, controller_site)
    changed = Mock()
    controller_site.runtime_data.async_grid_changed = changed
    hass.states.async_set(GRID, "off")
    await hass.async_block_till_done()
    await tick(hass, freezer, 16)
    assert controller.state.grid.active is True
    changed.assert_called_once_with(True)
    attrs = controller.view()[1]["grid"]
    assert attrs["outage"] is True
    assert attrs["lost_at"] and attrs["since"]
    assert [e.detail for e in controller.state.history if e.kind == "grid"] == [
        "started"
    ]

    hass.states.async_set(GRID, "on")
    await hass.async_block_till_done()
    await tick(hass, freezer, 60)
    assert controller.state.grid.active is True
    assert controller.view()[1]["grid"]["restored_pending_since"]
    await tick(hass, freezer, 5 * 60)
    assert controller.state.grid.active is False
    assert changed.call_args_list[-1].args == (False,)
    assert [e.detail for e in controller.state.history if e.kind == "grid"] == [
        "started",
        "ended",
    ]
    block = (await async_get_config_entry_diagnostics(hass, controller_site))[
        "deye_controller"
    ]["grid"]
    assert [edge["detail"] for edge in block["edges"]] == ["started", "ended"]
    assert block["presence_entity"] == GRID


async def test_the_phase_voltages_decide_without_a_presence_entity(
    controller_site, hass, freezer
):
    add_voltage(hass, controller_site)
    hass.states.async_set(VOLTAGE, "231")
    controller = await attach(hass, controller_site)
    assert controller.view()[1]["grid"]["source"] == "voltage"
    hass.states.async_set(VOLTAGE, "0")
    await hass.async_block_till_done()
    await tick(hass, freezer, 16)
    assert controller.state.grid.active is True


async def test_the_option_overrides_the_device_entity(controller_site, hass, freezer):
    add_grid(hass, controller_site)
    hass.states.async_set(GRID, "on")
    hass.states.async_set("binary_sensor.mains", "off")
    options = {
        **controller_site.options,
        "controller": {
            **controller_site.options["controller"],
            "limits": {
                "grid_presence_entity": "binary_sensor.mains",
                "outage_enter_seconds": 60,
            },
        },
    }
    hass.config_entries.async_update_entry(controller_site, options=options)
    await hass.async_block_till_done()
    controller = controller_of(controller_site)
    # The next coordinator update would do this; the form does not reload the entry.
    controller.async_refresh_view()
    await tick(hass, freezer, 30)
    assert controller.state.grid.active is False
    await tick(hass, freezer, 40)
    assert controller.state.grid.active is True


async def test_an_outage_survives_a_restart(controller_site, hass, freezer):
    add_grid(hass, controller_site)
    hass.states.async_set(GRID, "off")
    controller = await attach(hass, controller_site)
    await tick(hass, freezer, 16)
    assert controller.state.grid.active is True
    saved = controller.state.to_dict()
    restored, valid = core.load_state(saved, session=controller.session, now=None)
    assert valid
    assert restored.grid == controller.state.grid


async def saved(hass):
    """The write scheduled for now runs on the next turn of the loop, not a second later."""
    await asyncio.sleep(0)
    await hass.async_block_till_done()


async def test_an_edge_is_saved_before_the_delayed_save_runs(
    controller_site, hass, hass_storage, freezer
):
    """A crash within a second of the edge must not forget it."""
    key = f"energy_compass.{controller_site.entry_id}.controller"
    add_grid(hass, controller_site)
    hass.states.async_set(GRID, "off")
    controller = await attach(hass, controller_site)
    await tick(hass, freezer, 16)
    assert controller.state.grid.active is True
    await saved(hass)
    assert hass_storage[key]["data"]["grid"]["active"] is True
    hass.states.async_set(GRID, "on")
    await tick(hass, freezer, 301)
    assert controller.state.grid.active is False
    await saved(hass)
    assert hass_storage[key]["data"]["grid"]["active"] is False


async def test_an_unreadable_source_keeps_the_known_state(
    controller_site, hass, freezer
):
    add_grid(hass, controller_site)
    hass.states.async_set(GRID, "off")
    controller = await attach(hass, controller_site)
    await tick(hass, freezer, 16)
    hass.states.async_set(GRID, "unavailable")
    await tick(hass, freezer, 3600)
    assert controller.state.grid.active is True
