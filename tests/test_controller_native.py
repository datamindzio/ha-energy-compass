"""The native Deye controller inside Home Assistant: wiring, restart and unload."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from controller_support import START, controller_of, publication, publish
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from native_site import (
    CHARGE,
    DISCHARGE,
    GRID,
    Inverter,
    native_site,
    refresh_telemetry,
    register_inverter,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.energy_compass import controller as core
from custom_components.energy_compass.coordinator import EnergyCompassCoordinator
from custom_components.energy_compass.settings import DOMAIN, default_configuration

ISSUE = "deye_hardware_unverified_{}"
PAUSE = "switch.synthetic_pause_inverter_writes"
PROGRAM_4 = "select.inverter_deye_program_4_charging"
pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")


@pytest.fixture
async def site(hass, freezer):
    entry, inverter = await native_site(hass, freezer)
    yield entry, inverter
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


def describe_runs(host):
    """Every recorded run, short enough for an assertion message."""
    runs = []
    for run in host.history:
        runs.append(
            {
                "trigger": run.get("trigger"),
                "at": run.get("at"),
                "outcome": run.get("outcome"),
                "code": run.get("code"),
                "decision": (run.get("decision") or {}).get("reason_code"),
                "transactions": [
                    (t["attempt"], t["cleanup"], len(t["commands"]))
                    for t in run.get("transactions", [])
                ],
                "writes": [
                    (
                        w["entity"].rsplit(".", 1)[-1],
                        w["value"],
                        w.get("confirmed"),
                        w.get("blocked"),
                    )
                    for w in run.get("writes", [])
                ][:12],
            }
        )
    return json.dumps(runs)


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
    last_run = describe_runs(controller.host)
    assert controller.state.restore_pending is False, last_run
    assert controller.state.runtime["code"] == "restored", last_run
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

    async def run(trigger=""):
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


async def test_the_verified_unit_publishes_its_facts_and_raises_no_issue(hass, site):
    entry, _ = site
    hardware = hass.states.get("sensor.synthetic_deye_controller").attributes[
        "hardware"
    ]
    assert hardware["verified"] is True
    assert hardware["device_type"] == "HV 3-Phase Hybrid Inverter"
    assert hardware["protocol"] == "1.4"
    assert hardware["model"] == "SG0*HP3"
    assert "SN-REDACTED" not in str(hardware)
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, ISSUE.format(entry.entry_id)) is None
    )
    assert controller_of(entry).diagnostics()["hardware"]["verified"] is True


async def test_an_unlisted_unit_raises_a_dismissible_warning_and_still_runs(
    hass, freezer
):
    entry, inverter = await native_site(hass, freezer, protocol="2.0")
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE.format(entry.entry_id))
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False
    assert issue.translation_placeholders["protocol"] == "2.0"
    attributes = hass.states.get("sensor.synthetic_deye_controller").attributes
    assert attributes["hardware"]["verified"] is False
    controller = controller_of(entry)
    publish(entry, publication())
    refresh_telemetry(hass)
    controller.async_set_mode("Auto")
    await controller.host.async_wait_idle()
    assert inverter.writes
    await hass.config_entries.async_unload(entry.entry_id)
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, ISSUE.format(entry.entry_id)) is None
    )


async def test_a_shifted_register_bank_blocks_the_first_write(hass, site):
    entry, inverter = site
    controller = controller_of(entry)
    inverter.raw = {address + 1: raw for address, raw in inverter.raw.items()}
    publish(entry, publication())
    refresh_telemetry(hass)
    controller.async_set_mode("Auto")
    await controller.host.async_wait_idle()
    assert inverter.writes == []
    runtime = controller.state.runtime
    assert runtime["code"] == "blocked"
    assert runtime["reason_code"] == "layout_mismatch"
    assert controller.state.restore_pending is False


async def test_the_layout_probe_also_runs_in_simulation(hass, site):
    entry, inverter = site
    controller = controller_of(entry)
    inverter.raw = {address + 1: raw for address, raw in inverter.raw.items()}
    controller.async_set_mode("Simulation")
    await controller.host.async_wait_idle()
    assert controller.state.runtime["reason_code"] == "layout_mismatch"
    assert inverter.writes == []


async def test_the_runtime_sensor_carries_the_last_run_unrecorded(hass, site):
    entry, _ = site
    controller = controller_of(entry)
    await run_once(entry, "test-trigger")
    state = hass.states.get("sensor.synthetic_deye_controller_runtime")
    # Detail-only changes reach the state at most once a minute.
    assert "last_run" in state.attributes
    last = controller.runtime_view()[1]["last_run"]
    assert last["trigger"] == "test-trigger"
    assert last["outcome"] == "released"
    from custom_components.energy_compass.controller_entity import (
        RUNTIME_UNRECORDED,
        DeyeRuntimeSensor,
    )

    assert "last_run" in RUNTIME_UNRECORDED
    assert "last_run" in DeyeRuntimeSensor._unrecorded_attributes
    assert controller.host.history[-1] == last


async def test_a_logbook_entry_is_written_when_code_or_confirmed_mode_changes(
    hass, site
):
    entry, _ = site
    controller = controller_of(entry)
    events = async_capture_events(hass, "logbook_entry")
    await run_once(entry)
    assert events == []
    publish(entry, publication())
    refresh_telemetry(hass)
    await run_once(entry)
    events.clear()
    controller.async_set_mode("Auto")
    await controller.host.async_wait_idle()
    await hass.async_block_till_done()
    assert len(events) == 1
    data = events[0].data
    assert data["name"] == "Deye controller"
    assert data["entity_id"] == "sensor.synthetic_deye_controller_runtime"
    assert "CHARGE_PV" in data["message"] and "ok" in data["message"]
    await run_once(entry)
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_diagnostics_carry_the_engine_hardware_and_run_history(hass, site):
    from custom_components.energy_compass.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry, _ = site
    for number in range(52):
        await run_once(entry, f"run-{number}")
    diagnostics = (await async_get_config_entry_diagnostics(hass, entry))[
        "deye_controller"
    ]
    assert diagnostics["engine"] == "native"
    assert diagnostics["hardware"]["verified"] is True
    history = diagnostics["run_history"]
    assert len(history) == 50
    assert history[-1]["trigger"] == "run-51"
    assert "SN-REDACTED" not in json.dumps(diagnostics, default=str)


async def test_the_start_trigger_waits_for_home_assistant_and_unloads_cleanly(
    hass, freezer
):
    from homeassistant.core import CoreState

    hass.set_state(CoreState.starting)
    entry, _ = await native_site(hass, freezer)
    host = controller_of(entry).host
    assert host.runs == 0
    hass.set_state(CoreState.running)
    hass.bus.async_fire("homeassistant_started")
    await hass.async_block_till_done()
    await host.async_wait_idle()
    assert host.runs >= 1
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
