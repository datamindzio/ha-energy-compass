"""The settlement Store: persisted hysteresis state, restart, and model off."""

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.settings import default_configuration

START = "2026-09-24T10:00:00+00:00"


def _config(model):
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        deposit_model=model,
        deposit_yearly_value=3300,
        deposit_yearly_energy_cost=2150,
        # Without a distribution part the cap would report split_missing, not held.
        buy_distribution_rate=0.1,
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
    )
    return config


def _entry(hass, model):
    entry = MockConfigEntry(
        domain="energy_compass", data=_config(model), title="Home", version=3
    )
    entry.add_to_hass(hass)
    return entry, f"energy_compass.{entry.entry_id}.settlement"


async def _start(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry.runtime_data


async def test_state_is_persisted_and_survives_restart(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    entry, key = _entry(hass, "auto")
    coordinator = await _start(hass, entry)

    state = coordinator._settlement
    assert state["cap_pressure"] == 1.0
    assert state["evaluated_on"] == dt_util.utcnow().date().isoformat()
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass_storage[key]["data"] == state

    coordinator = await _start(hass, entry)
    assert coordinator._settlement == state
    assert coordinator.data["dispatch_policy"]["settlement"]["reason"] == "held"


async def test_model_off_keeps_no_state_and_drops_a_stale_one(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    entry, key = _entry(hass, "off")
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"cap_pressure": 0.7, "evaluated_on": "2026-09-23", "inputs": {}},
    }
    coordinator = await _start(hass, entry)

    assert coordinator._settlement is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass_storage[key]["data"] is None


async def test_a_corrupt_store_is_ignored(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    entry, key = _entry(hass, "auto")
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"cap_pressure": "high", "evaluated_on": 5, "inputs": 3},
    }
    coordinator = await _start(hass, entry)

    assert coordinator._settlement["cap_pressure"] == 1.0


async def test_model_off_without_a_stored_state_writes_no_file(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    entry, key = _entry(hass, "off")
    coordinator = await _start(hass, entry)

    assert coordinator._settlement is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert key not in hass_storage
