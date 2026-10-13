"""The settlement model in the coordinator: stateless, `auto` read as `off`."""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.settings import default_configuration

START = "2026-09-24T10:00:00+00:00"


def _config(model):
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        deposit_model=model,
        export_value_factor=0.2,
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


async def test_manual_model_is_published_and_writes_no_store(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    entry, key = _entry(hass, "manual")
    coordinator = await _start(hass, entry)

    settlement = coordinator.data["dispatch_policy"]["settlement"]
    assert settlement["model"] == "manual"
    assert settlement["cap_pressure"] == 1.0
    assert settlement["reason"] == "ok"
    assert not hasattr(coordinator, "_settlement")
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert key not in hass_storage


async def test_a_stored_auto_model_plans_as_off_without_an_alert(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    entry, key = _entry(hass, "auto")
    coordinator = await _start(hass, entry)

    settlement = coordinator.data["dispatch_policy"]["settlement"]
    assert settlement["model"] == "off"
    assert settlement["reason"] == "off"
    assert settlement["export_value_factor"] == 1.0
    assert settlement["import_energy_factor"] == 1.0
    assert coordinator.data["status"] != "invalid_input"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert key not in hass_storage


async def test_a_leftover_settlement_store_is_ignored(
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

    settlement = coordinator.data["dispatch_policy"]["settlement"]
    assert settlement["reason"] == "off"
    assert settlement["cap_pressure"] == 0
    assert await hass.config_entries.async_unload(entry.entry_id)
