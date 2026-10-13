"""A loaded entry running the native Deye controller against a fake Solarman inverter.

The inverter is a register bank behind the three services the controller uses
(`number.set_value`, `select.select_option`, `solarman.read_holding_registers`), so
tests exercise the real port, host and store, and read the effect from the bank.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from controller_support import PREFIX, START, register_solarman
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass import controller_profile as profile
from custom_components.energy_compass.coordinator import EnergyCompassCoordinator
from custom_components.energy_compass.settings import default_configuration

DEVICE_TYPE = "HV 3-Phase Hybrid Inverter"
DEVICE_ATTRIBUTES = {
    "Protocol Version": "1.4",
    "Control Board Firmware Version": "0000-0000-00000",
    "Communication Board Firmware Version": "0000-0000-0000",
    "Rated Power": 8000,
    "Serial Number": "SN-REDACTED",
    "Modbus Address": 1,
}
SENSORS = {
    "device": ("sensor", "inverter_deye_device", DEVICE_TYPE, DEVICE_ATTRIBUTES),
    "battery": ("sensor", "inverter_deye_battery", "51", {}),
    "battery_voltage": ("sensor", "inverter_deye_battery_voltage", "537.1", {}),
    "total_battery_discharge": (
        "sensor",
        "inverter_deye_total_battery_discharge",
        "5600.0",
        {},
    ),
    "battery_operation_mode": (
        "select",
        "inverter_deye_battery_operation_mode",
        "Capacity",
        {"options": ["Voltage", "Capacity", "No Battery"]},
    ),
    "battery_max_charging_current": (
        "number",
        "inverter_deye_battery_max_charging_current",
        "18",
        {"min": 0, "max": 350, "step": 1.0},
    ),
    "battery_max_discharging_current": (
        "number",
        "inverter_deye_battery_max_discharging_current",
        "18",
        {"min": 0, "max": 350, "step": 1.0},
    ),
    "battery_grid_charging_current": (
        "number",
        "inverter_deye_battery_grid_charging_current",
        "16",
        {"min": 0, "max": 350, "step": 1.0},
    ),
}
CHARGE = "number.inverter_deye_battery_max_charging_current"
DISCHARGE = "number.inverter_deye_battery_max_discharging_current"
GRID = "number.inverter_deye_battery_grid_charging_current"
OPERATION = "select.inverter_deye_battery_operation_mode"
SOC = "sensor.inverter_deye_battery"
VOLTAGE = "sensor.inverter_deye_battery_voltage"
OPTIONS = ["Disabled", "Grid", "Generator", "Grid & Generator", "Sell"]
PROGRAM_ATTRIBUTES = {
    "power": ("8000", {"min": 0, "max": 128000, "step": 1.0}),
    "voltage": ("50", {"min": 0, "max": 61, "step": 0.1}),
    "soc": ("20", {"min": 0, "max": 100, "step": 1.0}),
    "charging": ("Disabled", {"options": OPTIONS}),
}
ENTITIES = profile.Entities(CHARGE, DISCHARGE, GRID, OPERATION, SOC, VOLTAGE, PREFIX)


class Inverter:
    """Register bank and recording services."""

    def __init__(self, hass, select_handler=None):
        self.hass = hass
        self._select_handler = select_handler
        self.registers = profile.register_map(ENTITIES)
        self.writes = []
        self.reads = 0
        self.gate = None
        self.raw = {}
        self.dropped = set()
        for entity, register in self.registers.items():
            self.raw[register.address] = self._to_raw(
                entity, hass.states.get(entity).state
            )
        hass.services.async_register("number", "set_value", self._set_value)
        hass.services.async_register("select", "select_option", self._select)
        hass.services.async_register(
            "solarman",
            "read_holding_registers",
            self._read,
            supports_response=SupportsResponse.ONLY,
        )

    def _to_raw(self, entity, state):
        register = self.registers[entity]
        if entity.startswith("select."):
            return profile.SELECT_RAW.get(state, 0)
        return round(float(state) / register.scale)

    def _apply(self, entity, value):
        state = self.hass.states.get(entity)
        self.writes.append((entity, value))
        self.hass.states.async_set(entity, str(value), state.attributes)
        if entity not in self.dropped:
            self.raw[self.registers[entity].address] = self._to_raw(entity, str(value))

    async def _set_value(self, call: ServiceCall):
        if self.gate is not None:
            await self.gate.wait()
        self._apply(call.data["entity_id"], call.data["value"])

    async def _select(self, call: ServiceCall):
        if call.data["entity_id"] not in self.registers:
            # Energy Compass's own select entities share the service name.
            return await self._select_handler(call)
        if self.gate is not None:
            await self.gate.wait()
        self._apply(call.data["entity_id"], call.data["option"])

    async def _read(self, call: ServiceCall):
        self.reads += 1
        address, count = call.data["address"], call.data["count"]
        return {a: raw for a, raw in self.raw.items() if address <= a < address + count}

    def hold(self):
        self.gate = asyncio.Event()

    def release(self):
        self.gate.set()
        self.gate = None


async def fake_inverter(hass):
    """The register bank, with the real `select` component set up before its services.

    The integration forwards its `select` platform while a run may already be writing.
    That first load registers Home Assistant's own `select.select_option` over the
    fake one, so a write landing after it is a no-op and never confirms. Loading the
    component first makes the fake service the last registration, whatever the timing;
    it hands the entities it does not own to the real handler.
    """
    assert await async_setup_component(hass, "select", {})
    real = hass.services.async_services_internal()["select"]["select_option"]
    return Inverter(hass, real.job.target)


def register_inverter(hass, device_type=DEVICE_TYPE, protocol="1.4"):
    """The Solarman device with the 30 TOU entities and the controller's own."""
    device = register_solarman(hass)
    dr.async_get(hass).async_update_device(device.id, model="SG0*HP3")
    registry = er.async_get(hass)
    solarman = next(iter(hass.config_entries.async_entries("solarman")))
    for key, (domain, object_id, state, attributes) in SENSORS.items():
        entry = registry.async_get_or_create(
            domain,
            "solarman",
            f"inverter_{key}",
            config_entry=solarman,
            device_id=device.id,
            translation_key=key,
            suggested_object_id=object_id,
        )
        if key == "device":
            state = device_type
            attributes = {**attributes, "Protocol Version": protocol}
        hass.states.async_set(entry.entity_id, state, attributes)
    for number in range(1, 7):
        for name, (state, attributes) in PROGRAM_ATTRIBUTES.items():
            entity_id = f"{'select' if name == 'charging' else 'number'}.{PREFIX}{number}_{name}"
            hass.states.async_set(entity_id, state, attributes)
    return device


def refresh_telemetry(hass):
    """Report the telemetry again, so its last_reported is now."""
    for entity_id in (SOC, VOLTAGE):
        state = hass.states.get(entity_id)
        hass.states.async_set(
            entity_id, state.state, state.attributes, force_update=True
        )


async def native_site(
    hass, freezer, *, controller=None, extra_options=None, **hardware
):
    """A loaded native entry; returns (entry, inverter)."""
    freezer.move_to(START)
    await hass.config.async_set_time_zone("Europe/Warsaw")
    device = register_inverter(hass, **hardware)
    inverter = await fake_inverter(hass)
    entry = MockConfigEntry(
        domain="energy_compass",
        data=default_configuration("EUR", "UTC"),
        options={
            "controller": {
                "enabled": True,
                "device_id": device.id,
                "engine": "native",
                **(controller or {}),
            },
            **(extra_options or {}),
        },
        title="Synthetic",
        version=3,
    )
    entry.add_to_hass(hass)
    with patch.object(EnergyCompassCoordinator, "async_recalculate", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    freezer.move_to("2026-10-08T17:01:00+00:00")
    refresh_telemetry(hass)
    return entry, inverter
