"""Refresh failures report a problem without erasing a covered advisory plan."""

import threading
from copy import deepcopy
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from test_hourly_schedule import switching_entry as switching_entry  # noqa: PLC0414
from test_recalculation import published_entry as published_entry  # noqa: PLC0414

from custom_components.energy_compass import coordinator as module
from custom_components.energy_compass.engine.models import SolveError


@pytest.mark.parametrize("failure", ["timeout", "infeasible", "error"])
async def test_failed_solve_keeps_plan_and_alert_until_success(
    published_entry, hass, freezer, failure
):
    coordinator = published_entry.runtime_data
    original = hass.states.get("sensor.refresh_plan")
    alert = hass.states.get("binary_sensor.refresh_alert")
    assert alert is not None and alert.state == "off"
    error = RuntimeError("worker failed") if failure == "error" else SolveError(failure)
    freezer.move_to("2026-09-17T10:01:00+00:00")
    with patch.object(module, "compute", side_effect=error):
        await coordinator.async_recalculate()
    plan = hass.states.get("sensor.refresh_plan")
    assert plan.state == original.state
    assert plan.attributes["intervals"] == original.attributes["intervals"]
    assert plan.attributes["plan_retained"] is True
    assert hass.states.get("binary_sensor.refresh_forecast_valid").state == "on"
    alert = hass.states.get("binary_sensor.refresh_alert")
    assert alert.state == "on"
    assert alert.attributes["code"] == failure
    assert alert.attributes["since"] == "2026-09-17T10:01:00+00:00"
    assert (
        alert.attributes["last_successful_plan_at"]
        == original.attributes["generated_at"]
    )
    freezer.move_to("2026-09-17T10:02:00+00:00")
    await coordinator.async_recalculate()
    assert hass.states.get("binary_sensor.refresh_alert").state == "off"
    assert hass.states.get("sensor.refresh_plan").state != original.state
    assert hass.states.get("sensor.refresh_plan").attributes["plan_retained"] is False


async def test_missing_input_advances_retained_plan_and_stops_at_coverage_end(
    published_entry, hass, freezer
):
    original = hass.states.get("sensor.refresh_plan").state
    hass.states.async_set("input_number.limit", "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.refresh_plan").state == original
    alert = hass.states.get("binary_sensor.refresh_alert")
    assert alert.state == "on"
    assert "unavailable" in alert.attributes["reason"]
    since = alert.attributes["since"]
    for stamp, price in (("11:00:00", 0.1), ("12:00:00", 0.5), ("13:00:00", 1.5)):
        freezer.move_to(f"2026-09-17T{stamp}+00:00")
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert hass.states.get("sensor.refresh_plan").state == original
        assert (
            hass.states.get("sensor.refresh_energy_compass").attributes["buy_per_kwh"]
            == price
        )
        assert (
            hass.states.get("binary_sensor.refresh_alert").attributes["since"] == since
        )
    freezer.move_to("2026-09-17T14:00:00+00:00")
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get("sensor.refresh_plan").state == "unavailable"
    assert hass.states.get("binary_sensor.refresh_forecast_valid").state == "off"
    assert hass.states.get("binary_sensor.refresh_alert").state == "on"


async def test_soc_jump_keeps_plan_then_rebases_after_fresh_stable_reports(
    switching_entry, hass, freezer
):
    coordinator = switching_entry.runtime_data
    original = hass.states.get("sensor.switching_plan").state
    freezer.move_to("2026-09-18T10:01:00+00:00")
    hass.states.async_set("sensor.soc", "100")
    await hass.async_block_till_done()
    # An upward jump (BMS recalibration near full) keeps the plan without an
    # alert, so a controller does not drop it while the new level is confirmed.
    assert hass.states.get("sensor.switching_plan").state == original
    assert hass.states.get("binary_sensor.switching_alert").state == "off"
    assert coordinator.data["status"] == "calculating"
    assert coordinator.data["reason"] == "soc_rebase_pending"
    assert coordinator.data["plan_retained"] is True
    # Re-reading the same timestamp is not evidence that the new level is stable.
    freezer.move_to("2026-09-18T10:02:01+00:00")
    await coordinator.async_recalculate()
    assert hass.states.get("sensor.switching_plan").state == original
    assert hass.states.get("binary_sensor.switching_alert").state == "off"
    assert coordinator.data["reason"] == "soc_rebase_pending"
    assert coordinator._previous_soc[1] == 5
    # Identical state reports do not emit state_changed; a health tick recovers.
    hass.states.async_set("sensor.soc", "100")
    async_fire_time_changed(hass, dt_util.utcnow())
    await coordinator.async_recalculate()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.switching_plan").state != original
    assert hass.states.get("binary_sensor.switching_alert").state == "off"
    assert coordinator._previous_soc[1] == 10


@pytest.mark.parametrize("value", ["unavailable", "101", "-1"])
async def test_invalid_soc_never_becomes_recovery_baseline(
    switching_entry, hass, freezer, value
):
    coordinator = switching_entry.runtime_data
    previous = coordinator._previous_soc
    original = hass.states.get("sensor.switching_plan").state
    for stamp in ("10:01:00", "10:03:00"):
        freezer.move_to(f"2026-09-18T{stamp}+00:00")
        hass.states.async_set("sensor.soc", value)
        await coordinator.async_recalculate()
        await hass.async_block_till_done()
    assert coordinator._previous_soc == previous
    assert hass.states.get("sensor.switching_plan").state == original
    assert hass.states.get("binary_sensor.switching_alert").state == "on"


async def test_failure_without_previous_plan_only_exposes_alert(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from test_mode_dwell import native_config

    freezer.move_to("2026-09-18T10:00:00+00:00")
    entry = MockConfigEntry(
        domain="energy_compass",
        data=deepcopy(native_config()),
        title="Empty",
        version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.empty_plan").state == "unavailable"
    alert = hass.states.get("binary_sensor.empty_alert")
    assert alert is not None and alert.state == "on"
    assert alert.attributes["plan_retained"] is False
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_pending_worker_does_not_freeze_current_interval(
    switching_entry, hass, freezer
):
    coordinator = switching_entry.runtime_data
    original_plan = hass.states.get("sensor.switching_plan").state
    started, release = threading.Event(), threading.Event()
    advanced = threading.Event()
    original_compute = module.compute

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original_compute(*args, **kwargs)

    def updated():
        rows = coordinator.data.get("intervals", [])
        if rows and rows[0]["start"] == "2026-09-18T10:15:00+00:00":
            advanced.set()

    unsubscribe = coordinator.async_add_listener(updated)
    freezer.move_to("2026-09-18T10:14:00+00:00")
    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(coordinator.async_recalculate())
        try:
            assert await hass.async_add_executor_job(started.wait, 2)
            freezer.move_to("2026-09-18T10:16:00+00:00")
            async_fire_time_changed(hass, dt_util.utcnow())
            assert await hass.async_add_executor_job(advanced.wait, 2)
            assert hass.states.get("sensor.switching_plan").state == original_plan
            assert (
                hass.states.get("sensor.switching_energy_compass").state
                == "DISCHARGE_GRID"
            )
            assert (
                hass.states.get("binary_sensor.switching_forecast_valid").state == "on"
            )
        finally:
            release.set()
            await job
            unsubscribe()


async def test_retained_plan_keeps_its_generation_strategy(
    published_entry, hass, freezer
):
    """A retained plan must keep the strategy label of the generation that solved it."""
    coordinator = published_entry.runtime_data
    original_strategy = coordinator.data["strategy"]
    coordinator.configuration["settings"]["strategy"] = "max_export"
    freezer.move_to("2026-09-17T10:01:00+00:00")
    with patch.object(module, "compute", side_effect=SolveError("timeout")):
        await coordinator.async_recalculate()
    assert hass.states.get("sensor.refresh_plan").attributes["plan_retained"] is True
    assert coordinator.data["strategy"] == original_strategy


@pytest.mark.parametrize("intermediate", ["unavailable", "78"])
async def test_interrupted_or_oscillating_soc_restarts_stabilization(
    switching_entry, hass, freezer, intermediate
):
    original = hass.states.get("sensor.switching_plan").state
    for stamp, value in (
        ("10:01:00", "80"),
        ("10:01:59", intermediate),
        ("10:02:01", "82"),
    ):
        freezer.move_to(f"2026-09-18T{stamp}+00:00")
        hass.states.async_set("sensor.soc", value)
        await hass.async_block_till_done()
        await switching_entry.runtime_data.async_recalculate()
    assert hass.states.get("sensor.switching_plan").state == original
    # A missing source is an error on its own; a plausible oscillation only
    # restarts the pending upward rebase.
    expected = "on" if intermediate == "unavailable" else "off"
    assert hass.states.get("binary_sensor.switching_alert").state == expected
    assert switching_entry.runtime_data._previous_soc[1] == 5


async def test_unconfirmed_upward_soc_jump_fails_after_grace(
    switching_entry, hass, freezer
):
    coordinator = switching_entry.runtime_data
    original = hass.states.get("sensor.switching_plan").state
    freezer.move_to("2026-09-18T10:01:00+00:00")
    hass.states.async_set("sensor.soc", "100")
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.switching_alert").state == "off"
    # No fresh report ever confirms the new level.
    freezer.move_to(
        f"2026-09-18T10:0{1 + (module.SOC_REBASE_GRACE_SECONDS + 1) // 60}:"
        f"{(module.SOC_REBASE_GRACE_SECONDS + 1) % 60:02d}+00:00"
    )
    await coordinator.async_recalculate()
    alert = hass.states.get("binary_sensor.switching_alert")
    assert alert.state == "on"
    assert alert.attributes["code"] == "soc_measurement_jump"
    assert hass.states.get("sensor.switching_plan").state == original
    assert coordinator._previous_soc[1] == 5


async def test_downward_soc_jump_still_alerts_immediately(
    switching_entry, hass, freezer
):
    freezer.move_to("2026-09-18T10:01:00+00:00")
    hass.states.async_set("sensor.soc", "0")
    await hass.async_block_till_done()
    alert = hass.states.get("binary_sensor.switching_alert")
    assert alert.state == "on"
    assert alert.attributes["code"] == "soc_measurement_jump"
    assert switching_entry.runtime_data._soc_rebase_since is None
