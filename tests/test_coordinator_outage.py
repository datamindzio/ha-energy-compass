"""No plan is calculated while the grid is out; the end of the outage starts a solve."""

from datetime import timedelta
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from test_recalculation import published_entry as published_entry  # noqa: PLC0414

from custom_components.energy_compass import coordinator as module


@pytest.fixture
def outage():
    """The grid watch as the coordinator reads it; the test flips `return_value`."""
    with patch.object(
        module.EnergyCompassCoordinator,
        "grid_outage",
        new_callable=PropertyMock,
        return_value=True,
    ) as flag:
        yield flag


async def test_the_outage_holds_the_plan_and_names_the_reason(
    published_entry, hass, freezer, outage
):
    coordinator = published_entry.runtime_data
    original = hass.states.get("sensor.refresh_plan")
    freezer.move_to("2026-09-17T10:01:00+00:00")
    coordinator.async_grid_changed(True)
    await hass.async_block_till_done()
    assert coordinator.data["status"] == "calculating"
    assert coordinator.data["reason"] == "grid_outage"
    assert coordinator.data["plan_retained"] is True
    plan = hass.states.get("sensor.refresh_plan")
    assert plan.state == original.state
    assert plan.attributes["intervals"] == original.attributes["intervals"]
    assert hass.states.get("binary_sensor.refresh_alert").state == "off"


async def test_no_solve_runs_while_the_grid_is_out(
    published_entry, hass, freezer, outage
):
    coordinator = published_entry.runtime_data
    coordinator.async_grid_changed(True)
    freezer.move_to("2026-09-17T10:20:00+00:00")
    with patch.object(module, "compute") as compute:
        await coordinator.async_recalculate()
        hass.states.async_set("sensor.soc", "50")
        await hass.async_block_till_done()
        coordinator._source_changed(None)
        await hass.async_block_till_done()
    compute.assert_not_called()
    assert coordinator.data["reason"] == "grid_outage"
    assert coordinator._runner is None


@pytest.mark.parametrize("held", [True, False])
async def test_the_boundary_tick_solves_only_without_an_outage(
    published_entry, hass, freezer, outage, held
):
    coordinator = published_entry.runtime_data
    outage.return_value = held
    generation = coordinator._generation
    coordinator._refresh_due = dt_util.utcnow()
    freezer.move_to("2026-09-17T10:20:00+00:00")
    coordinator._next_boundary(coordinator.configuration["settings"])
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await hass.async_block_till_done()
    if held:
        assert coordinator._generation == generation
        assert coordinator.data["reason"] == "grid_outage"
    else:
        assert coordinator._generation > generation


@pytest.mark.parametrize("rearm", ["boundary", "invalidate"])
@pytest.mark.parametrize("held", [True, False])
async def test_the_boundary_is_not_rearmed_in_a_busy_loop_during_an_outage(
    published_entry, hass, freezer, outage, held, rearm
):
    """Missed refresh and input deadlines must not turn the tick into 10 per second."""
    coordinator = published_entry.runtime_data
    outage.return_value = held
    freezer.move_to("2026-09-17T10:20:00+00:00")
    coordinator._refresh_due = dt_util.utcnow() - timedelta(minutes=5)
    coordinator._inputs_valid_until = dt_util.utcnow() - timedelta(minutes=1)
    if rearm == "boundary":
        coordinator._next_boundary(coordinator.configuration["settings"])
    else:
        coordinator._invalidate("calculating", "grid_outage")
    delay = coordinator._boundary.when() - hass.loop.time()
    if held:
        assert delay >= 1
    else:
        assert delay < 1


async def test_a_boundary_tick_during_an_outage_rearms_once_and_idles(
    published_entry, hass, freezer, outage
):
    coordinator = published_entry.runtime_data
    freezer.move_to("2026-09-17T10:20:00+00:00")
    coordinator._refresh_due = dt_util.utcnow() - timedelta(minutes=5)
    coordinator._next_boundary(coordinator.configuration["settings"])
    with patch.object(
        coordinator, "_next_boundary", wraps=coordinator._next_boundary
    ) as rearm:
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
        await hass.async_block_till_done()
    assert rearm.call_count <= 2
    assert coordinator._boundary.when() - hass.loop.time() >= 1


@pytest.mark.parametrize("calls", [1, 2])
async def test_the_end_of_the_outage_solves_at_once(
    published_entry, hass, freezer, outage, calls
):
    coordinator = published_entry.runtime_data
    before = hass.states.get("sensor.refresh_plan")
    coordinator.async_grid_changed(True)
    generation = coordinator._generation
    freezer.move_to("2026-09-17T10:20:00+00:00")
    outage.return_value = False
    for _ in range(calls):
        coordinator.async_grid_changed(False)
    await hass.async_block_till_done()
    assert coordinator._generation > generation
    assert coordinator.data["status"] == "ready"
    assert coordinator.data.get("reason") != "grid_outage"
    after = hass.states.get("sensor.refresh_plan")
    assert after.attributes["generated_at"] > before.attributes["generated_at"]
    assert after.attributes["plan_retained"] is False


async def test_a_restart_during_an_outage_does_not_solve(
    published_entry, hass, freezer, outage
):
    coordinator = published_entry.runtime_data
    with patch.object(module, "compute") as compute:
        await coordinator.async_recalculate()
    compute.assert_not_called()
    assert coordinator.data["reason"] == "grid_outage"


async def test_diagnostics_say_the_planner_is_held(published_entry, outage):
    coordinator = published_entry.runtime_data
    assert coordinator.calculation_counts()["grid_outage_hold"] is True
    outage.return_value = False
    assert coordinator.calculation_counts()["grid_outage_hold"] is False
