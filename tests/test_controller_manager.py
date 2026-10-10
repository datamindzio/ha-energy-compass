import json
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from controller_support import (
    PREFIX,
    START,
    controller_of,
    iso,
    publication,
    publish,
    register_solarman,
    tick,
)
from homeassistant.config_entries import ConfigEntryState  # noqa: F401
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass import controller as core
from custom_components.energy_compass.controller_ha import controller_session
from custom_components.energy_compass.coordinator import EnergyCompassCoordinator
from custom_components.energy_compass.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.energy_compass.settings import default_configuration

pytestmark = pytest.mark.usefixtures("recorder_mock")


async def test_ready_publication_is_accepted_and_saved(
    hass, hass_storage, controller_site, freezer
):
    controller = controller_of(controller_site)
    assert controller.view()[1]["plan_reason"] == "session"
    publish(controller_site, publication())
    state, attrs = controller.state, controller.view()[1]
    assert state.accepted["generated_at"] == iso(17, 0, 45)
    assert state.accepted["battery"]["capacity_kwh"] == 24.0
    assert attrs["plan_reason"] == "ok"
    assert attrs["generation"] == iso(17, 0, 45)
    assert attrs["capacity_kwh"] == 24.0
    assert [event.kind for event in state.history] == ["accepted"]
    await tick(hass, freezer, 2)
    key = f"energy_compass.{controller_site.entry_id}.controller"
    saved = hass_storage[key]["data"]
    assert saved["accepted"]["generated_at"] == iso(17, 0, 45)
    assert saved["history"][0]["kind"] == "accepted"


async def test_error_publication_revokes(controller_site):
    controller = controller_of(controller_site)
    publish(controller_site, publication())
    publish(
        controller_site,
        {
            "status": "invalid_input",
            "valid": False,
            "alert": {"code": "x"},
            "reason": "r",
        },
    )
    assert controller.state.revoked.generation == iso(17, 0, 45)
    assert controller.view()[1]["plan_reason"] == "revoked"
    assert controller.view()[1]["revoked_generation"] == iso(17, 0, 45)
    assert [event.kind for event in controller.state.history] == ["accepted", "revoked"]


async def test_entry_reload_keeps_the_plan_and_revokes_nothing(
    hass, controller_site, freezer
):
    publish(controller_site, publication())
    assert await hass.config_entries.async_reload(controller_site.entry_id)
    await hass.async_block_till_done()
    controller = controller_of(controller_site)
    assert controller_site.runtime_data.data == {
        "status": "calculating",
        "valid": False,
    }
    assert controller.state.accepted["generated_at"] == iso(17, 0, 45)
    assert controller.state.revoked is None
    assert controller.view()[1]["plan_reason"] == "ok"
    assert [event.kind for event in controller.state.history] == ["accepted"]


async def test_core_restart_drops_the_plan_of_the_old_session(
    hass, controller_site, freezer
):
    publish(controller_site, publication())
    first = controller_of(controller_site).session
    hass.data.pop("energy_compass")
    freezer.move_to("2026-10-08T17:05:00+00:00")
    assert await hass.config_entries.async_reload(controller_site.entry_id)
    await hass.async_block_till_done()
    controller = controller_of(controller_site)
    assert controller.session > first
    assert controller.state.accepted is None
    assert controller.view()[1]["plan_reason"] == "session"
    assert [event.kind for event in controller.state.history] == [
        "accepted",
        "dropped",
    ]


async def test_reload_reuses_the_session(hass, controller_site):
    first = controller_of(controller_site).session
    assert controller_session(hass, controller_site.entry_id) == first
    assert await hass.config_entries.async_reload(controller_site.entry_id)
    await hass.async_block_till_done()
    assert controller_of(controller_site).session == first


async def test_state_is_the_next_event_and_advances_one_second_later(
    hass, controller_site, freezer
):
    controller = controller_of(controller_site)
    publish(controller_site, publication())
    assert controller.view()[0] == datetime(2026, 10, 8, 17, 15, tzinfo=UTC)
    await tick(hass, freezer, 14 * 60)
    assert freezer().replace(tzinfo=UTC) == datetime(2026, 10, 8, 17, 15, tzinfo=UTC)
    assert controller.view()[0] == datetime(2026, 10, 8, 17, 15, tzinfo=UTC)
    await tick(hass, freezer, 1)
    assert controller.view()[0] == datetime(2026, 10, 8, 18, 0, tzinfo=UTC)


async def test_tou_time_change_recomputes_the_next_event_at_once(hass, controller_site):
    controller = controller_of(controller_site)
    publish(controller_site, publication())
    assert controller.view()[1]["next_tou"].startswith("2026-10-08T22:00:00+02:00")
    hass.states.async_set(f"time.{PREFIX}3_time", "19:10:00")
    await hass.async_block_till_done()
    assert controller.view()[0] == datetime(2026, 10, 8, 17, 10, tzinfo=UTC)
    assert controller.view()[1]["tou"][f"time.{PREFIX}3_time"] == "19:10:00"
    assert controller.view()[1]["program_prefix"] == PREFIX


async def test_view_carries_the_resolved_device(controller_site):
    attrs = controller_of(controller_site).view()[1]
    assert attrs["device_id"] == controller_site.options["controller"]["device_id"]
    assert attrs["tou_problem"] is None
    assert len(attrs["tou"]) == 30
    assert tuple(attrs) == core.CONTROLLER_ATTRIBUTES


async def test_mode_change_is_recorded_and_stored(hass, controller_site):
    controller = controller_of(controller_site)
    assert controller.state.mode == "Off"
    controller.async_set_mode("Auto")
    assert controller.view()[1]["mode"] == "Auto"
    assert controller.state.history[-1].kind == "mode"
    with pytest.raises(ValueError):
        controller.async_set_mode("Dance")


async def test_runtime_replace_and_read(controller_site):
    controller = controller_of(controller_site)
    answer = controller.async_update_runtime({"code": "ok", "state": "HOLD"}, True)
    assert answer["runtime"] == {"code": "ok", "state": "HOLD"}
    assert answer["restore_pending"] is True
    assert answer["session"] == controller.session
    assert controller.view()[1]["restore_pending"] is True
    assert controller.state.runtime_written_at is not None
    assert controller.async_update_runtime(None, None)["runtime"]["state"] == "HOLD"
    with pytest.raises(core.RuntimeInvalid):
        controller.async_update_runtime({"bogus": 1}, None)


async def test_invalid_store_warns_once_and_defaults_safely(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer, caplog
):
    freezer.move_to(START)
    device = register_solarman(hass)
    entry = MockConfigEntry(
        domain="energy_compass",
        data=default_configuration("EUR", "UTC"),
        options={"controller": {"enabled": True, "device_id": device.id}},
        version=3,
    )
    entry.add_to_hass(hass)
    key = f"energy_compass.{entry.entry_id}.controller"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"mode": "garbage", "secret": "do-not-log"},
    }
    with (
        caplog.at_level(logging.DEBUG),
        patch.object(EnergyCompassCoordinator, "async_recalculate", AsyncMock()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    controller = entry.runtime_data.controller
    assert controller.state.mode == "Off" and controller.state.restore_pending
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "Deye controller" in record.message
    ]
    assert len(warnings) == 1
    ours = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.energy_compass")
    )
    assert "do-not-log" not in ours
    await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_report_the_controller_without_plan_rows(
    hass, controller_site
):
    publish(controller_site, publication())
    controller_of(controller_site).async_update_runtime({"code": "ok"}, False)
    report = await async_get_config_entry_diagnostics(hass, controller_site)
    text = json.dumps(report["deye_controller"])
    section = report["deye_controller"]
    assert section["enabled"] is True
    assert section["session"] == controller_of(controller_site).session
    assert section["mode"] == "Off"
    assert section["plan_reason"] == "ok"
    assert section["program_prefix"] == PREFIX
    assert section["accepted"]["interval_count"] == 3
    assert section["accepted"]["generated_at"] == iso(17, 0, 45)
    assert section["runtime"] == {"code": "ok", "keys": ["code"]}
    assert section["history"][0]["kind"] == "accepted"
    assert '"intervals"' not in text and "dispatch_policy" not in text


async def test_disabled_controller_has_no_object_and_no_store(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    entry = MockConfigEntry(
        domain="energy_compass",
        data=default_configuration("EUR", "UTC"),
        title="Synthetic",
        version=3,
    )
    entry.add_to_hass(hass)
    with patch.object(EnergyCompassCoordinator, "async_recalculate", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data.controller is None
        report = await async_get_config_entry_diagnostics(hass, entry)
        assert report["deye_controller"] == {"enabled": False}
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert f"energy_compass.{entry.entry_id}.controller" not in hass_storage


async def test_removing_the_entry_removes_the_store(
    hass, hass_storage, controller_site, freezer
):
    publish(controller_site, publication())
    key = f"energy_compass.{controller_site.entry_id}.controller"
    assert await hass.config_entries.async_unload(controller_site.entry_id)
    assert key in hass_storage
    assert await hass.config_entries.async_remove(controller_site.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
