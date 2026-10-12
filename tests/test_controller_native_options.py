"""The Deye options step: engine, entity resolution, limits and the blueprint prefill."""

import pytest
import test_deye_controller_blueprint as blueprint
from controller_support import controller_of, register_solarman
from homeassistant.config_entries import ConfigEntryState
from native_site import (
    SOC,
    VOLTAGE,
    Inverter,
    native_site,
    register_inverter,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.settings import default_configuration

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")


@pytest.fixture
def expected_lingering_timers() -> bool:
    return True


async def open_step(hass, entry, *, expert=False):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    if expert:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "show_expert"}
        )
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "deye_controller"}
    )


async def submit(hass, form, data):
    return await hass.config_entries.options.async_configure(form["flow_id"], data)


def fields(form):
    return {str(key) for key in form["data_schema"].schema}


def suggested(form, key):
    (field,) = [item for item in form["data_schema"].schema if item == key]
    return field.description["suggested_value"]


async def make_entry(hass, options=None):
    from unittest.mock import AsyncMock, patch

    from custom_components.energy_compass.coordinator import EnergyCompassCoordinator

    entry = MockConfigEntry(
        domain="energy_compass",
        data=default_configuration("EUR", "UTC"),
        options=options or {},
        title="Synthetic",
        version=3,
    )
    entry.add_to_hass(hass)
    with patch.object(EnergyCompassCoordinator, "async_recalculate", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_a_fresh_form_suggests_native_and_a_blueprint_entry_keeps_blueprint(
    hass,
):
    device = register_inverter(hass)
    entry = await make_entry(hass)
    assert suggested(await open_step(hass, entry), "engine") == "native"
    entry2 = await make_entry_with(hass, device.id)
    assert suggested(await open_step(hass, entry2), "engine") == "blueprint"


async def make_entry_with(hass, device_id, **controller):
    entry = MockConfigEntry(
        domain="energy_compass",
        data=default_configuration("EUR", "UTC"),
        options={"controller": {"enabled": True, "device_id": device_id, **controller}},
        title="Second",
        version=3,
    )
    entry.add_to_hass(hass)
    return entry


async def test_enabling_native_reloads_into_an_executor_and_the_pause_switch(hass):
    device = register_inverter(hass)
    Inverter(hass)
    entry = await make_entry(hass)
    form = await open_step(hass, entry)
    result = await submit(
        hass, form, {"enabled": True, "device_id": device.id, "engine": "native"}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["controller"]["engine"] == "native"
    assert controller_of(entry).host is not None
    assert hass.states.get("switch.synthetic_pause_inverter_writes") is not None


async def test_a_device_without_the_controller_entities_is_refused_by_name(hass):
    device = register_solarman(hass)
    entry = await make_entry(hass)
    form = await open_step(hass, entry)
    result = await submit(
        hass, form, {"enabled": True, "device_id": device.id, "engine": "native"}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"device_id": "controller_entity_missing"}
    assert "battery_operation_mode" not in result["description_placeholders"]["missing"]
    assert "operation" in result["description_placeholders"]["missing"]
    assert (
        "solarman.read_holding_registers"
        in (result["description_placeholders"]["missing"])
    )


async def test_a_disabled_entity_counts_as_missing(hass):
    from homeassistant.helpers import entity_registry as er

    device = register_inverter(hass)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", "solarman", "inverter_battery_voltage"
    )
    registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    entry = await make_entry(hass)
    Inverter(hass)
    form = await open_step(hass, entry)
    result = await submit(
        hass, form, {"enabled": True, "device_id": device.id, "engine": "native"}
    )
    assert result["errors"] == {"device_id": "controller_entity_missing"}
    assert result["description_placeholders"]["missing"] == "voltage"


async def test_a_low_voltage_pack_cannot_enable_the_native_engine(hass):
    device = register_inverter(hass)
    Inverter(hass)
    hass.states.async_set(VOLTAGE, "52.4")
    entry = await make_entry(hass)
    form = await open_step(hass, entry)
    result = await submit(
        hass, form, {"enabled": True, "device_id": device.id, "engine": "native"}
    )
    assert result["errors"] == {"device_id": "controller_low_voltage_pack"}
    assert result["description_placeholders"]["voltage"] == "52.4"
    # The blueprint engine is not subject to the check.
    result = await submit(
        hass, result, {"enabled": True, "device_id": device.id, "engine": "blueprint"}
    )
    assert result["type"] == "create_entry"


async def test_changing_the_engine_needs_a_released_controller_and_reloads(
    hass, freezer
):
    entry, _ = await native_site(hass, freezer)
    controller = controller_of(entry)
    controller.async_set_mode("Auto")
    device = entry.options["controller"]["device_id"]
    form = await open_step(hass, entry)
    data = {"enabled": True, "device_id": device, "engine": "blueprint"}
    result = await submit(hass, form, data)
    assert result["errors"] == {"base": "controller_not_released"}
    controller.async_set_mode("Off")
    await controller.host.async_wait_idle()
    result = await submit(hass, result, data)
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert controller_of(entry).engine == "blueprint"
    assert controller_of(entry).host is None
    state = hass.states.get("switch.synthetic_pause_inverter_writes")
    assert state is None or state.state == "unavailable"


async def test_the_expert_fields_appear_only_with_expert_settings(hass, freezer):
    entry, _ = await native_site(hass, freezer)
    plain = fields(await open_step(hass, entry))
    assert plain == {"enabled", "device_id", "engine"}
    expert = fields(await open_step(hass, entry, expert=True))
    assert {
        "max_power_w",
        "max_current",
        "relinquish_current",
        "max_grid_current",
        "hold_grid_current",
        "reached_discharge_current",
        "balance_grid_current",
        "voltage_grid_charge_ceiling",
        "commissioned_battery_modes",
        "old_writers",
        "telemetry_entities",
        "discharge_energy_entity",
        "charge_entity",
        "voltage_entity",
    } <= expert


async def test_limits_apply_live_without_a_reload(hass, freezer):
    entry, _ = await native_site(hass, freezer)
    coordinator = entry.runtime_data
    controller = controller_of(entry)
    device = entry.options["controller"]["device_id"]
    form = await open_step(hass, entry, expert=True)
    result = await submit(
        hass,
        form,
        {
            "enabled": True,
            "device_id": device,
            "engine": "native",
            "max_power_w": 2000,
            "commissioned_battery_modes": ["Capacity", "Voltage"],
            "old_writers": ["automation.some_old_one"],
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator
    assert entry.options["controller"]["limits"] == {
        "max_power_w": 2000,
        "commissioned_battery_modes": ["Capacity", "Voltage"],
        "old_writers": ["automation.some_old_one"],
    }
    controller.host.request("test")
    await controller.host.async_wait_idle()
    limits = controller.host.port.setup.limits
    assert limits.max_power_w == 2000
    assert limits.commissioned_battery_modes == ("Capacity", "Voltage")
    assert limits.old_writers == ("automation.some_old_one",)
    # Emptying the group returns every limit to its derived default.
    form = await open_step(hass, entry, expert=True)
    assert suggested(form, "max_power_w") == 2000
    await submit(hass, form, {"enabled": True, "device_id": device, "engine": "native"})
    await hass.async_block_till_done()
    assert "limits" not in entry.options["controller"]


async def test_an_entity_override_replaces_the_resolved_one(hass, freezer):
    entry, _ = await native_site(hass, freezer)
    controller = controller_of(entry)
    device = entry.options["controller"]["device_id"]
    hass.states.async_set("sensor.other_soc", "51")
    form = await open_step(hass, entry, expert=True)
    result = await submit(
        hass,
        form,
        {
            "enabled": True,
            "device_id": device,
            "engine": "native",
            "soc_entity": "sensor.other_soc",
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["controller"]["entities"] == {"soc": "sensor.other_soc"}
    controller.host.request("test")
    await controller.host.async_wait_idle()
    assert controller.host.port.setup.entities.soc == "sensor.other_soc"
    assert controller.host.port.setup.limits.telemetry_entities[0] == "sensor.other_soc"


async def test_the_single_blueprint_automation_prefills_the_expert_fields(hass):
    device = register_inverter(hass)
    Inverter(hass)
    await blueprint.setup_controller(
        hass,
        max_power_w=7000,
        max_current=17,
        old_writers=["automation.old_one"],
        telemetry_entities=[SOC, VOLTAGE],
        soc_entity="sensor.renamed_soc",
        voltage_entity=VOLTAGE,
    )
    entry = await make_entry_with(hass, device.id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    form = await open_step(hass, entry)
    # The group is shown because there is something to review.
    assert suggested(form, "max_power_w") == 7000
    assert suggested(form, "max_current") == 17
    assert suggested(form, "old_writers") == ["automation.old_one"]
    assert suggested(form, "soc_entity") == "sensor.renamed_soc"
    # An entity input equal to what the device resolves to is not an override.
    assert suggested(form, "voltage_entity") is None
    # Defaults the automation runs with are carried over too.
    assert suggested(form, "relinquish_current") is not None
    assert "limits" not in entry.options["controller"]
    assert "blueprint" in form["description_placeholders"]["status"]


async def test_two_blueprint_automations_prefill_nothing(hass):
    device = register_inverter(hass)
    Inverter(hass)
    await blueprint.setup_controller(hass, max_power_w=7000)
    entry = await make_entry_with(hass, device.id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    from custom_components.energy_compass import controller_ha

    real = controller_ha.blueprint_automations
    controller_ha.blueprint_automations = lambda hass: [*real(hass), *real(hass)]
    try:
        form = await open_step(hass, entry)
    finally:
        controller_ha.blueprint_automations = real
    assert fields(form) == {"enabled", "device_id", "engine"}
