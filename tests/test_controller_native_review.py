"""Failures the native executor must show, listeners it must hold, writers it must refuse."""

import asyncio

import pytest
import test_deye_controller_blueprint as blueprint
from controller_support import controller_of, publication, publish
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState
from homeassistant.helpers import issue_registry as ir
from native_site import (
    OPERATION,
    SOC,
    VOLTAGE,
    native_site,
    refresh_telemetry,
)

from custom_components.energy_compass.settings import DOMAIN

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
FAILED = "deye_run_failed_{}"
BLUEPRINT = "deye_blueprint_active_{}"
PAUSE = "switch.synthetic_pause_inverter_writes"


@pytest.fixture
def expected_lingering_timers() -> bool:
    return True


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


def issue(hass, template, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, template.format(entry.entry_id))


def break_runs(host, monkeypatch):
    async def run(trigger=""):
        raise RuntimeError("boom")

    monkeypatch.setattr(host.executor, "run", run)


async def test_a_failing_run_is_visible_and_raises_a_repair_after_three(
    hass, site, monkeypatch
):
    entry, _ = site
    controller = controller_of(entry)
    host = controller.host
    await run_once(entry)
    assert controller.state.runtime["code"] != "error"
    original = host.executor.run
    break_runs(host, monkeypatch)
    for number in range(1, 4):
        await run_once(entry, f"fail-{number}")
        runtime = controller.state.runtime
        assert runtime["code"] == "error"
        assert runtime["reason_code"] == "run_failed"
        assert "log" in runtime["reason"]
        assert hass.states.get("sensor.synthetic_deye_controller_runtime").state == (
            "error"
        )
        assert (issue(hass, FAILED, entry) is not None) == (number == 3)
    monkeypatch.setattr(host.executor, "run", original)
    await run_once(entry, "recovered")
    assert controller.state.runtime["code"] != "error"
    assert issue(hass, FAILED, entry) is None


async def test_an_unavailable_controller_counts_as_a_failed_run(
    hass, site, monkeypatch
):
    entry, _ = site
    controller = controller_of(entry)
    host = controller.host

    async def run(trigger=""):
        return "controller_unavailable"

    monkeypatch.setattr(host.executor, "run", run)
    for _ in range(3):
        await run_once(entry)
    assert controller.state.runtime["code"] == "error"
    assert controller.state.runtime["reason_code"] == "controller_unavailable"
    assert issue(hass, FAILED, entry) is not None


async def test_a_failure_that_cannot_be_published_is_still_survived(
    hass, site, monkeypatch
):
    entry, _ = site
    controller = controller_of(entry)
    break_runs(controller.host, monkeypatch)

    async def refuse(*args, **kwargs):
        raise RuntimeError("store gone")

    monkeypatch.setattr(controller, "async_persist", refuse)
    await run_once(entry)
    assert controller.host._task.done()
    assert controller.host.runs >= 1


async def test_an_exception_after_a_run_keeps_the_task_and_the_rerun(
    hass, site, monkeypatch
):
    entry, _ = site
    host = controller_of(entry).host
    await host.async_wait_idle()
    gate = asyncio.Event()
    calls = []

    async def run(trigger=""):
        calls.append(trigger)
        await gate.wait()
        return "run_finished"

    def explode():
        raise RuntimeError("post-run")

    monkeypatch.setattr(host.executor, "run", run)
    for name in ("_sync_watch", "_log_change"):
        monkeypatch.setattr(host, name, explode)
    monkeypatch.setattr(controller_of(entry), "async_runtime_changed", explode)
    host.request("a")
    await asyncio.sleep(0)
    host.request("b")
    gate.set()
    await host.async_wait_idle()
    assert calls == ["a", "queued"]


async def test_the_listeners_exist_before_home_assistant_has_started(hass, freezer):
    hass.set_state(CoreState.starting)
    entry, _ = await native_site(hass, freezer)
    host = controller_of(entry).host
    assert host.runs == 0
    assert OPERATION in host._watched and SOC in host._watched
    hass.states.async_set(OPERATION, "Voltage")
    await hass.async_block_till_done()
    assert host.runs == 0
    hass.set_state(CoreState.running)
    hass.bus.async_fire("homeassistant_started")
    await hass.async_block_till_done()
    await host.async_wait_idle()
    started = host.runs
    assert started >= 1
    hass.states.async_set(OPERATION, "Capacity")
    await hass.async_block_till_done()
    await host.async_wait_idle()
    assert host.runs > started
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_watch_follows_the_setup_and_is_dropped_when_it_is_lost(
    hass, site, monkeypatch
):
    entry, _ = site
    host = controller_of(entry).host
    await run_once(entry)
    assert host._watched
    monkeypatch.setattr(host.port, "resolve_setup", lambda: None)
    host.port.setup = None
    host._sync_watch()
    assert host._watched == ()


async def test_a_state_change_reaches_the_run_request(hass, site):
    entry, _ = site
    host = controller_of(entry).host
    await host.async_wait_idle()
    before = host.runs
    hass.states.async_set(SOC, "52")
    await hass.async_block_till_done()
    await host.async_wait_idle()
    assert host.runs == before + 1


@pytest.mark.parametrize(
    ("old", "new", "fires"),
    [
        ("537.1", "unavailable", True),
        ("unavailable", "537.1", True),
        ("unknown", "unavailable", True),
        ("unavailable", "unknown", True),
        ("537.1", "538.0", False),
    ],
)
async def test_telemetry_availability_edges_fire_in_every_direction(
    hass, site, old, new, fires
):
    entry, _ = site
    host = controller_of(entry).host
    await host.async_wait_idle()
    hass.states.async_set(VOLTAGE, old)
    await hass.async_block_till_done()
    await host.async_wait_idle()
    before = host.runs
    hass.states.async_set(VOLTAGE, new)
    await hass.async_block_till_done()
    await host.async_wait_idle()
    assert (host.runs > before) is fires


async def test_a_running_blueprint_automation_blocks_a_stored_auto(hass, site):
    entry, inverter = site
    controller = controller_of(entry)
    publish(entry, publication())
    refresh_telemetry(hass)
    controller.async_set_mode("Auto")
    await controller.host.async_wait_idle()
    assert inverter.writes
    written = len(inverter.writes)
    await blueprint.setup_controller(hass)
    assert hass.states.get("automation.test_deye").state == "on"
    refresh_telemetry(hass)
    await run_once(entry)
    runtime = controller.state.runtime
    assert runtime["reason_code"] == "blueprint_active"
    assert runtime["takeover_blocked"] is True
    assert "automation.test_deye" in runtime["reason"]
    assert len(inverter.writes) == written
    repair = issue(hass, BLUEPRINT, entry)
    assert repair is not None
    assert repair.translation_placeholders == {"automation": "automation.test_deye"}
    await hass.services.async_call(
        "automation", "turn_off", {"entity_id": "automation.test_deye"}, blocking=True
    )
    refresh_telemetry(hass)
    await run_once(entry)
    assert controller.state.runtime["reason_code"] != "blueprint_active"
    assert issue(hass, BLUEPRINT, entry) is None


async def test_the_pause_is_saved_at_once(hass, hass_storage, site):
    entry, _ = site
    key = f"energy_compass.{entry.entry_id}.controller"
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": PAUSE}, blocking=True
    )
    assert hass_storage[key]["data"]["paused"] is True
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": PAUSE}, blocking=True
    )
    assert hass_storage[key]["data"]["paused"] is False


async def test_switching_the_engine_to_blueprint_clears_the_pause(hass, site):
    entry, _ = site
    controller_of(entry).async_set_paused(True)
    await hass.config_entries.async_unload(entry.entry_id)
    options = dict(entry.options["controller"], engine="blueprint")
    hass.config_entries.async_update_entry(entry, options={"controller": options})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert controller_of(entry).engine == "blueprint"
    assert controller_of(entry).state.paused is False


async def test_a_request_from_inside_a_run_never_starts_a_nested_run(
    hass, site, monkeypatch
):
    """Home Assistant starts tasks eagerly; the run's own persists fire the listener."""
    entry, _ = site
    host = controller_of(entry).host
    await host.async_wait_idle()
    active = 0
    deepest = 0
    calls = []

    async def run(trigger=""):
        nonlocal active, deepest
        active += 1
        deepest = max(deepest, active)
        calls.append(trigger)
        if len(calls) < 5:
            # Before any suspension, as the controller listener does on a persist.
            host.request("nested")
        active -= 1
        return "run_finished"

    monkeypatch.setattr(host.executor, "run", run)
    host.request("outer")
    host.request("second")
    await host.async_wait_idle()
    assert deepest == 1
    assert calls == ["outer", "queued", "queued", "queued", "queued"]
    assert host._running is False
