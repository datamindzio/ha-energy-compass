from datetime import timedelta
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import EntityBinding

START = "2026-09-24T10:00:00+00:00"


def _config():
    config = default_configuration("EUR", "UTC")
    config["sources"].update(
        battery_enabled=True, soc=EntityBinding("sensor.soc").to_dict()
    )
    config["settings"].update(
        capacity_kwh=20,
        soc_ceiling=100,
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
        inverter_kw=5,
        grid_import_kw=7,
        allow_grid_charge=True,
        limit_export_to_pv=False,
        minimum_mode_minutes=15,
        lfp_balance=True,
    )
    return config


async def _setup(hass, hass_storage=None, stored=None, config=None):
    entry = MockConfigEntry(
        domain="energy_compass", data=config or _config(), title="Home", version=3
    )
    entry.add_to_hass(hass)
    if stored is not None:
        hass_storage[f"energy_compass.{entry.entry_id}.balance"] = {
            "version": 1,
            "minor_version": 1,
            "key": f"energy_compass.{entry.entry_id}.balance",
            "data": stored,
        }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def test_full_soc_completes_a_hold(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    hass.states.async_set("sensor.soc", "100", {"unit_of_measurement": "%"})
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    assert coordinator.balance_report()["phase"] == "holding"
    for _ in range(13):
        freezer.tick(timedelta(minutes=5))
        hass.states.async_set(
            "sensor.soc", "100", {"unit_of_measurement": "%"}, force_update=True
        )
        await hass.async_block_till_done()
    assert coordinator.balance_report()["phase"] == "ok"
    assert coordinator._balance["last_completed_at"].startswith("2026-09-24T11:00")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_hold_survives_restart(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    hass.states.async_set("sensor.soc", "100", {"unit_of_measurement": "%"})
    stored = {
        "last_completed_at": None,
        "hold_started_at": "2026-09-24T09:30:00+00:00",
        "last_full_at": "2026-09-24T09:55:00+00:00",
    }
    entry = await _setup(hass, hass_storage, stored)
    report = entry.runtime_data.balance_report()
    assert report["phase"] == "holding"
    assert report["hold_progress_minutes"] == 30
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unavailable_soc_does_not_reset_hold(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    hass.states.async_set("sensor.soc", "100", {"unit_of_measurement": "%"})
    stored = {
        "last_completed_at": None,
        "hold_started_at": "2026-09-24T09:50:00+00:00",
        "last_full_at": "2026-09-24T09:55:00+00:00",
    }
    entry = await _setup(hass, hass_storage, stored)
    hass.states.async_set("sensor.soc", "unavailable")
    await hass.async_block_till_done()
    assert entry.runtime_data._balance["hold_started_at"] == "2026-09-24T09:50:00+00:00"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_soc_outage_gives_no_hold_credit(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    hass.states.async_set("sensor.soc", "100", {"unit_of_measurement": "%"})
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    freezer.tick(timedelta(minutes=10))
    hass.states.async_set(
        "sensor.soc", "100", {"unit_of_measurement": "%"}, force_update=True
    )
    await hass.async_block_till_done()
    hass.states.async_set("sensor.soc", "unavailable")
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=80))
    hass.states.async_set("sensor.soc", "90", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert coordinator._balance["last_completed_at"] is None
    assert coordinator.balance_report()["phase"] == "due"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_invalid_store_is_reseeded(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer, caplog
):
    freezer.move_to(START)
    hass.states.async_set("sensor.soc", "60", {"unit_of_measurement": "%"})
    config = _config()
    config["settings"]["lfp_balance"] = False
    for stored in (
        "garbage",
        {"last_completed_at": "not a time", "hold_started_at": None},
        {"last_completed_at": "2026-09-20T10:00:00", "hold_started_at": None},
    ):
        entry = await _setup(hass, hass_storage, stored, config=config)
        coordinator = entry.runtime_data
        assert coordinator.data["valid"], coordinator.data.get("reason")
        assert coordinator._balance == {
            "last_completed_at": None,
            "hold_started_at": None,
            "last_full_at": None,
        }
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert "Invalid stored balance state" in caplog.text


async def test_failed_seed_is_not_persisted(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    hass.states.async_set("sensor.soc", "60", {"unit_of_measurement": "%"})
    with patch(
        "custom_components.energy_compass.coordinator.async_balance_history",
        side_effect=RuntimeError("recorder down"),
    ):
        entry = await _setup(hass)
    assert entry.runtime_data.balance_report()["phase"] == "due"
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert f"energy_compass.{entry.entry_id}.balance" not in hass_storage
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert f"energy_compass.{entry.entry_id}.balance" not in hass_storage


async def test_empty_store_seeds_as_due_without_history(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    hass.states.async_set("sensor.soc", "60", {"unit_of_measurement": "%"})
    entry = await _setup(hass)
    report = entry.runtime_data.balance_report()
    assert report["phase"] == "due"
    assert report["state"] in ("scheduled", "overdue")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_battery_balance_sensor(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    hass.states.async_set("sensor.soc", "100", {"unit_of_measurement": "%"})
    entry = await _setup(hass)
    state = hass.states.get("sensor.home_battery_balance")
    assert state.state == "holding"
    assert state.attributes["hold_required_minutes"] == 60
    assert state.attributes["threshold_percent"] == 99
    assert set(state.attributes["options"]) == {
        "ok",
        "eligible",
        "scheduled",
        "holding",
        "overdue",
    }
    for key in (
        "last_completed",
        "next_due",
        "days_overdue",
        "planned_start",
        "planned_end",
        "planned_mode",
        "hold_progress_minutes",
    ):
        assert key in state.attributes
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_battery_balance_sensor_disabled_when_feature_off(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from homeassistant.helpers import entity_registry as er

    freezer.move_to(START)
    hass.states.async_set("sensor.soc", "60", {"unit_of_measurement": "%"})
    config = _config()
    config["settings"]["lfp_balance"] = False
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Off", version=3
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", "energy_compass", f"{entry.entry_id}_battery_balance"
    )
    assert registry.async_get(entity_id).disabled_by is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
