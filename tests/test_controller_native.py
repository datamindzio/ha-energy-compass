"""The native Deye controller inside Home Assistant: wiring, restart and unload."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from controller_support import START, controller_of, publication, publish
from homeassistant.config_entries import ConfigEntryState
from native_site import (
    CHARGE,
    DISCHARGE,
    GRID,
    Inverter,
    native_site,
    refresh_telemetry,
    register_inverter,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass import controller as core
from custom_components.energy_compass.coordinator import EnergyCompassCoordinator
from custom_components.energy_compass.settings import default_configuration

PAUSE = "switch.synthetic_pause_inverter_writes"
PROGRAM_4 = "select.inverter_deye_program_4_charging"
pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")


@pytest.fixture
async def site(hass, freezer):
    entry, inverter = await native_site(hass, freezer)
    yield entry, inverter
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


async def run_once(entry, trigger="test"):
    host = controller_of(entry).host
    host.request(trigger)
    await host.async_wait_idle()


async def test_only_the_native_engine_gets_an_executor(hass, freezer, site):
    entry, _ = site
    assert controller_of(entry).host is not None
    assert controller_of(entry).engine == "native"


async def test_the_blueprint_engine_has_no_executor(hass, controller_site):
    assert controller_of(controller_site).engine == "blueprint"
    assert controller_of(controller_site).host is None


async def test_an_accepted_plan_is_written_and_confirmed_through_the_services(
    hass, site
):
    entry, inverter = site
    await run_once(entry)
    assert inverter.writes == []
    controller = controller_of(entry)
    controller.async_set_mode("Auto")
    publish(entry, publication())
    refresh_telemetry(hass)
    await run_once(entry)
    runtime = controller.state.runtime
    assert runtime["code"] == "ok"
    assert runtime["confirmed_mode"] == "CHARGE_PV"
    assert controller.state.restore_pending is True
    written = dict(inverter.writes)
    assert written[CHARGE] > 0 and written[DISCHARGE] > 0 and written[GRID] == 0
    assert inverter.reads >= len(inverter.writes)
    # A run that finds everything confirmed writes nothing.
    count = len(inverter.writes)
    await run_once(entry)
    assert len(inverter.writes) == count


async def test_simulation_computes_but_never_writes(hass, site):
    entry, inverter = site
    controller_of(entry).async_set_mode("Simulation")
    publish(entry, publication())
    refresh_telemetry(hass)
    await run_once(entry)
    assert inverter.writes == []
    assert controller_of(entry).state.runtime["state"] == "CHARGE_PV"


async def test_a_new_session_restores_the_base_before_anything_is_forced(
    hass, hass_storage, freezer
):
    freezer.move_to(START)
    await hass.config.async_set_time_zone("Europe/Warsaw")
    device = register_inverter(hass)
    # What the previous process left: a forced profile on the inverter, Auto and the
    # restore obligation in the store.
    hass.states.async_set(CHARGE, "5", hass.states.get(CHARGE).attributes)
    hass.states.async_set(PROGRAM_4, "Grid", hass.states.get(PROGRAM_4).attributes)
    inverter = Inverter(hass)
    entry = MockConfigEntry(
        domain="energy_compass",
        data=default_configuration("EUR", "UTC"),
        options={
            "controller": {"enabled": True, "device_id": device.id, "engine": "native"}
        },
        title="Synthetic",
        version=3,
    )
    key = f"energy_compass.{entry.entry_id}.controller"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": core.ControllerState(
            mode="Auto",
            restore_pending=True,
            runtime={"owned_session": 1.0, "code": "ok", "confirmed_mode": "CHARGE_PV"},
        ).to_dict(),
    }
    entry.add_to_hass(hass)
    with patch.object(EnergyCompassCoordinator, "async_recalculate", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    controller = controller_of(entry)
    await controller.host.async_wait_idle()
    # No plan was accepted in this session, so the only profile is the base.
    assert controller.state.restore_pending is False
    assert controller.state.runtime["code"] == "restored"
    assert dict(inverter.writes)[PROGRAM_4] == "Disabled"
    assert dict(inverter.writes)[CHARGE] > 5
    base_writes = len(inverter.writes)
    freezer.move_to("2026-10-08T17:01:00+00:00")
    publish(entry, publication())
    refresh_telemetry(hass)
    await run_once(entry)
    forced = dict(inverter.writes[base_writes:])
    assert forced[CHARGE] > 0
    assert controller.state.runtime["confirmed_mode"] == "CHARGE_PV"
    await hass.config_entries.async_unload(entry.entry_id)


async def start_blocked_run(hass, entry, inverter):
    """Auto with a valid plan while the first inverter write hangs."""
    controller = controller_of(entry)
    publish(entry, publication())
    refresh_telemetry(hass)
    inverter.hold()
    controller.async_set_mode("Auto")
    for _ in range(50):
        await asyncio.sleep(0)
        if controller.state.restore_pending:
            break
    assert controller.state.restore_pending is True
    return controller


async def test_unloading_mid_transaction_keeps_the_restore_obligation(
    hass, hass_storage, site
):
    entry, inverter = site
    controller = await start_blocked_run(hass, entry, inverter)
    await hass.config_entries.async_unload(entry.entry_id)
    assert inverter.writes == []
    saved = hass_storage[f"energy_compass.{entry.entry_id}.controller"]["data"]
    assert saved["restore_pending"] is True
    assert saved["runtime"]["uncertain"]
    assert saved["runtime"]["owned_session"] == controller.session


async def test_cancellation_is_not_counted_as_a_failed_write(hass, site):
    entry, inverter = site
    controller = await start_blocked_run(hass, entry, inverter)
    host = controller.host
    reads = inverter.reads
    await host.async_stop()
    assert host._task is None
    assert inverter.reads == reads
    assert inverter.writes == []
    assert controller.state.runtime.get("code") != "write_failed"


async def test_triggers_during_a_run_coalesce_into_one_more_run(hass, site):
    entry, _ = site
    host = controller_of(entry).host
    await host.async_wait_idle()
    gate = asyncio.Event()
    calls = []

    async def run():
        calls.append(1)
        await gate.wait()
        return "run_finished"

    host.executor.run = run
    host.request("a")
    await asyncio.sleep(0)
    for trigger in "bcdef":
        host.request(trigger)
    gate.set()
    await host.async_wait_idle()
    assert len(calls) == 2


async def test_a_durable_persist_is_saved_at_once(hass, hass_storage, site):
    entry, _ = site
    controller = controller_of(entry)
    await controller.host.async_stop()
    key = f"energy_compass.{entry.entry_id}.controller"
    await controller.async_persist({"code": "ok"}, True, durable=True)
    assert hass_storage[key]["data"]["restore_pending"] is True
    assert hass_storage[key]["data"]["runtime"] == {"code": "ok"}


async def test_the_pause_switch_exists_only_for_the_native_engine(
    hass, controller_site
):
    assert hass.states.get(PAUSE) is None


async def test_the_pause_switch_is_stored_and_holds_the_writes(
    hass, hass_storage, site
):
    entry, inverter = site
    controller = controller_of(entry)
    assert hass.states.get(PAUSE).state == "off"
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": PAUSE}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(PAUSE).state == "on"
    assert controller.state.paused is True
    publish(entry, publication())
    refresh_telemetry(hass)
    controller.async_set_mode("Auto")
    await controller.host.async_wait_idle()
    assert inverter.writes == []
    assert controller.state.runtime["code"] == "paused"
    assert controller.state.restore_pending is False
    # Turning it off requests a run, which then writes.
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": PAUSE},
        blocking=True,
    )
    await controller.host.async_wait_idle()
    assert inverter.writes
    assert controller.state.runtime["code"] == "ok"
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert controller_of(entry).state.paused is False


async def test_the_pause_survives_a_reload(hass, hass_storage, site):
    entry, _ = site
    controller_of(entry).async_set_paused(True)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert controller_of(entry).state.paused is True
    assert hass.states.get(PAUSE).state == "on"
