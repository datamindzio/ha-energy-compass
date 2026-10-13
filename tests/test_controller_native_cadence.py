"""How often the native engine runs and stores: only when its inputs change."""

from unittest.mock import patch

import pytest
from controller_support import controller_of, iso, publication, publish, tick
from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util
from native_site import native_site, refresh_telemetry

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")


@pytest.fixture
async def site(hass, freezer):
    entry, inverter = await native_site(hass, freezer)
    yield entry, inverter
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


async def settle(entry):
    host = controller_of(entry).host
    await host.async_wait_idle()
    return host


async def run_once(entry, trigger="test"):
    host = controller_of(entry).host
    host.request(trigger)
    await host.async_wait_idle()


def triggered(host, kind="controller"):
    return [run for run in host.history if run["trigger"] == kind]


async def accepted_plan(hass, entry):
    controller_of(entry).async_set_mode("Auto")
    publish(entry, publication())
    refresh_telemetry(hass)
    host = await settle(entry)
    await run_once(entry)
    await run_once(entry)
    return host


async def test_identical_publications_request_no_run(hass, site):
    entry, _ = site
    host = await accepted_plan(hass, entry)
    controller = controller_of(entry)
    before = len(triggered(host))
    runs = host.runs
    for _ in range(20):
        publish(entry, publication())
        controller.async_refresh_view()
        controller._publish()
    await hass.async_block_till_done()
    await settle(entry)
    assert host.runs == runs
    assert len(triggered(host)) == before


async def test_a_new_plan_generation_requests_exactly_one_run(hass, site):
    entry, _ = site
    host = await accepted_plan(hass, entry)
    runs = host.runs
    publish(entry, publication(generated_at=iso(17, 0, 50)))
    await hass.async_block_till_done()
    await settle(entry)
    assert host.runs == runs + 1
    assert host.history[-1]["trigger"] == "controller"
    for _ in range(5):
        publish(entry, publication(generated_at=iso(17, 0, 50)))
    await hass.async_block_till_done()
    await settle(entry)
    assert host.runs == runs + 1


async def test_the_next_event_timer_requests_one_run(hass, site):
    entry, _ = site
    host = await accepted_plan(hass, entry)
    controller = controller_of(entry)
    assert controller.view()[0] is not None
    assert controller._timer is not None
    runs = host.runs
    controller._timer()
    controller._advance(dt_util.utcnow())
    await hass.async_block_till_done()
    await settle(entry)
    assert host.runs == runs + 1
    assert host.history[-1]["trigger"] == "controller"


async def test_an_unchanged_decision_does_not_touch_the_store(hass, site):
    entry, _ = site
    host = await accepted_plan(hass, entry)
    controller = controller_of(entry)
    stamp = controller.state.runtime_written_at
    with (
        patch.object(controller._store, "async_delay_save") as delayed,
        patch.object(controller._store, "async_save") as saved,
    ):
        await run_once(entry)
        await run_once(entry)
    assert delayed.call_count == 0
    assert saved.call_count == 0
    assert controller.state.runtime_written_at == stamp
    assert host.history[-1]["outcome"] == "run_finished"


async def test_a_changed_decision_is_stored(hass, freezer, site):
    entry, _ = site
    await accepted_plan(hass, entry)
    controller = controller_of(entry)
    stamp = controller.state.runtime_written_at
    freezer.tick(1)
    publish(entry, publication(generated_at=iso(17, 0, 50)))
    with patch.object(controller._store, "async_delay_save") as delayed:
        await hass.async_block_till_done()
        await settle(entry)
    assert delayed.call_count >= 1
    assert controller.state.runtime_written_at != stamp


async def test_pausing_requests_one_run(hass, site):
    entry, _ = site
    host = await accepted_plan(hass, entry)
    runs = host.runs
    controller_of(entry).async_set_paused(True)
    await hass.async_block_till_done()
    await settle(entry)
    assert host.runs == runs + 1
    assert host.history[-1]["trigger"] == "controller"


RUNTIME = "sensor.synthetic_deye_controller_runtime"


async def test_last_run_reaches_the_sensor_within_seconds_without_a_write_storm(
    hass, freezer, site
):
    from homeassistant.const import EVENT_STATE_CHANGED

    entry, _ = site
    host = await accepted_plan(hass, entry)
    await tick(hass, freezer, 70)
    refresh_telemetry(hass)
    await run_once(entry)
    await tick(hass, freezer, 70)
    refresh_telemetry(hass)
    await run_once(entry)
    await tick(hass, freezer, 70)
    refresh_telemetry(hass)
    await run_once(entry)
    await tick(hass, freezer, 4)
    writes = []
    runs = host.runs
    hass.bus.async_listen(
        EVENT_STATE_CHANGED,
        lambda event: (
            writes.append(event) if event.data["entity_id"] == RUNTIME else None
        ),
    )
    refresh_telemetry(hass)
    await run_once(entry, "first")
    await tick(hass, freezer, 4)
    assert hass.states.get(RUNTIME).attributes["last_run"] is host.history[-1]
    refresh_telemetry(hass)
    await run_once(entry, "second")
    await tick(hass, freezer, 4)
    assert hass.states.get(RUNTIME).attributes["last_run"] is host.history[-1]
    assert host.history[-1]["trigger"] in ("second", "minute")
    assert len(writes) <= host.runs - runs


async def test_the_sensor_exposes_the_stored_reason_code(hass, site):
    entry, _ = site
    await accepted_plan(hass, entry)
    controller = controller_of(entry)
    code = controller.state.runtime["reason_code"]
    assert code
    assert hass.states.get(RUNTIME).attributes["reason_code"] == code


async def test_last_run_carries_the_probe_result_when_one_ran(hass, site):
    entry, _ = site
    controller = controller_of(entry)
    host = controller.host
    controller.async_set_mode("Simulation")
    await settle(entry)
    assert host.history[-1]["probe"] == "passed"
    await run_once(entry)
    assert "probe" not in host.history[-1]


async def test_a_failed_probe_names_the_entities(hass, site):
    entry, inverter = site
    controller = controller_of(entry)
    inverter.raw = {address + 1: raw for address, raw in inverter.raw.items()}
    controller.async_set_mode("Simulation")
    await settle(entry)
    probe = controller.host.history[-1]["probe"]
    assert probe.startswith("failed: ") and len(probe) > len("failed: ")


async def test_notable_runs_survive_a_flood_of_routine_ones(hass, site):
    from custom_components.energy_compass.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry, _ = site
    host = await accepted_plan(hass, entry)
    publish(entry, publication(generated_at=iso(17, 0, 50)))
    refresh_telemetry(hass)
    await settle(entry)
    wrote = [run for run in host.history if any("attempt" in w for w in run["writes"])]
    assert wrote
    for number in range(60):
        await run_once(entry, f"routine-{number}")
    assert not any(run in wrote for run in host.history)
    diagnostics = (await async_get_config_entry_diagnostics(hass, entry))[
        "deye_controller"
    ]
    assert len(diagnostics["run_history"]) == 50
    notable = diagnostics["run_history_notable"]
    assert len(notable) <= 20
    assert all(run in notable for run in wrote)
    assert all(
        run["outcome"] != "run_finished" or run["code"] != "ok" or run["writes"]
        for run in notable
    )
