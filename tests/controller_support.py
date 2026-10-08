"""Shared fixtures for the controller tests: a loaded entry with a Solarman device."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.energy_compass import controller as core
from custom_components.energy_compass.coordinator import EnergyCompassCoordinator
from custom_components.energy_compass.settings import default_configuration

START = "2026-10-08T17:00:30+00:00"
TIMES = {
    1: "00:00:00",
    2: "06:00:00",
    3: "13:00:00",
    4: "15:00:00",
    5: "22:00:00",
    6: "23:00:00",
}
PREFIX = "inverter_deye_program_"


def iso(hour, minute=0, second=0):
    return datetime(2026, 10, 8, hour, minute, second, tzinfo=UTC).isoformat()


def rows():
    def row(start, end, state):
        values = {key: 0.5 for key in core.ROW_NUMBERS}
        values.update(start=start, end=end, state=state, balance_hold=False)
        return values

    return [
        row(iso(16, 45), iso(17, 0), "SELF_CONSUME"),
        row(iso(17, 0), iso(17, 15), "CHARGE_PV"),
        row(iso(17, 15), iso(18, 0), "HOLD"),
    ]


def publication(generated_at=None, **override):
    data = {
        "status": "ready",
        "valid": True,
        "generated_at": generated_at or iso(17, 0, 45),
        "valid_until": iso(19),
        "intervals": rows(),
        "dispatch_policy": {"limit_grid_charge_price": 0.6},
        "refreshing": False,
        "plan_retained": False,
        "alert": None,
        "controller_parameters": {
            "capacity_kwh": 24.0,
            "eta_charge": 0.97,
            "eta_discharge": 0.96,
            "charge_kw": 8.0,
            "discharge_kw": 7.0,
        },
    }
    data.update(override)
    return data


def register_solarman(hass):
    solarman = MockConfigEntry(domain="solarman", title="Inverter")
    solarman.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=solarman.entry_id, identifiers={("solarman", "inverter")}
    )
    registry = er.async_get(hass)
    for number in range(1, 7):
        for name, domain in core.TOU_FIELDS:
            entry = registry.async_get_or_create(
                domain,
                "solarman",
                f"inverter_{number}_{name}",
                config_entry=solarman,
                device_id=device.id,
                translation_key=f"program_{number}_{name}",
                suggested_object_id=f"{PREFIX}{number}_{name}",
            )
            hass.states.async_set(
                entry.entity_id, TIMES[number] if name == "time" else "0"
            )
    return device


@pytest.fixture
async def controller_site(recorder_mock, hass, enable_custom_integrations, freezer):
    freezer.move_to(START)
    await hass.config.async_set_time_zone("Europe/Warsaw")
    device = register_solarman(hass)
    entry = MockConfigEntry(
        domain="energy_compass",
        data=default_configuration("EUR", "UTC"),
        options={"controller": {"enabled": True, "device_id": device.id}},
        title="Synthetic",
        version=3,
    )
    entry.add_to_hass(hass)
    with patch.object(EnergyCompassCoordinator, "async_recalculate", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        freezer.move_to("2026-10-08T17:01:00+00:00")
        yield entry
        if hass.config_entries.async_get_entry(entry.entry_id) and (
            entry.state is ConfigEntryState.LOADED
        ):
            await hass.config_entries.async_unload(entry.entry_id)


def controller_of(entry):
    return entry.runtime_data.controller


def publish(entry, data):
    entry.runtime_data.async_set_updated_data(data)


async def tick(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
