"""Hourly solves keep native interval advice and freshness checks current."""

from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.energy_compass.config_models import PriceSource
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)


@pytest.fixture
async def hourly_entry(
    recorder_mock, hass, enable_custom_integrations, freezer, request
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    config = default_configuration("PLN", "UTC")
    config["settings"].update(
        refresh_minutes=60,
        horizon_hours=4,
        display_horizon_hours=4,
        reference_horizon_hours=4,
        display_interval_minutes=15,
        debounce_seconds=0,
        total_time_limit_s=300,
    )
    if getattr(request, "param", None) == "helper":
        config["settings"]["refresh_minutes"] = 15
        config["helpers"]["refresh_minutes"] = {"fixed": 60, "unit": "min"}
    config["sources"]["buy"] = PriceSource(
        "forecast",
        (
            IntervalBinding(
                EntityBinding("sensor.market", attribute="rows"),
                start_path="start",
                interval_minutes=15,
                value_path="price",
                unit="PLN/kWh",
                value_kind="price",
                max_age_seconds=60
                if getattr(request, "param", None) == "short_forecast"
                else None,
            ),
        ),
    ).to_dict()
    config["helpers"]["grid_import_kw"] = {
        "entity": {"entity_id": "sensor.limit"},
        "unit": "kW",
        "max_age_seconds": 60
        if getattr(request, "param", None) == "short_helper"
        else 600,
    }
    hass.states.async_set("sensor.limit", "5", {"unit_of_measurement": "kW"})
    start = dt_util.utcnow()
    hass.states.async_set(
        "sensor.market",
        "ok",
        {
            "rows": [
                {"start": start + timedelta(minutes=15 * i), "price": i / 10}
                for i in range(20)
            ]
        },
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Hourly", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)


async def advance(hass, freezer, stamp, *, refresh_helper=True):
    freezer.move_to(stamp)
    if refresh_helper:
        hass.states.async_set(
            "sensor.limit",
            "5",
            {
                "unit_of_measurement": "kW",
                "reported": stamp,
            },
        )
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


@pytest.mark.parametrize("hourly_entry", ["fixed", "helper"], indirect=True)
async def test_hourly_plan_advances_quarters_without_new_solve(
    hourly_entry, hass, freezer
):
    first = hourly_entry.runtime_data.data["generated_at"]
    assert hourly_entry.runtime_data.data["valid_until"] == "2026-09-18T12:00:00+00:00"
    for minute, price in ((5, 0), (15, 0.1), (30, 0.2), (45, 0.3)):
        await advance(hass, freezer, f"2026-09-18T10:{minute:02d}:00+00:00")
        data = hourly_entry.runtime_data.data
        assert data["generated_at"] == first
        assert data["valid_until"] == "2026-09-18T12:00:00+00:00"
        assert (
            data["inputs_valid_until"]
            == (dt_util.utcnow() + timedelta(minutes=10)).isoformat()
        )
        assert hass.states.get("binary_sensor.hourly_forecast_valid").state == "on"
        assert hass.states.get("sensor.hourly_energy_compass").attributes[
            "buy_per_kwh"
        ] == pytest.approx(price)
        assert float(
            hass.states.get("sensor.hourly_consumption_cost").state
        ) == pytest.approx(price)
    await advance(hass, freezer, "2026-09-18T11:00:00+00:00")
    assert hourly_entry.runtime_data.data["generated_at"] != first


@pytest.mark.parametrize("expired_at", ["10:10:00", "10:10:01"])
async def test_stale_helper_alerts_and_retains_without_running_optimizer(
    hourly_entry, hass, freezer, expired_at
):
    coordinator = hourly_entry.runtime_data
    first = coordinator.data["generated_at"]
    await advance(hass, freezer, f"2026-09-18T{expired_at}+00:00", refresh_helper=False)
    assert coordinator.data["status"] == "invalid_input"
    assert coordinator._runner is None and coordinator._worker is None
    assert coordinator.data["generated_at"] == first
    assert hass.states.get("binary_sensor.hourly_forecast_valid").state == "on"
    assert hass.states.get("binary_sensor.hourly_alert").state == "on"
    await advance(hass, freezer, "2026-09-18T10:11:00+00:00")
    assert coordinator.data["valid"]
    assert coordinator.data["generated_at"] != first


@pytest.mark.parametrize(
    "hourly_entry", ["short_helper", "short_forecast"], indirect=True
)
async def test_input_deadline_advances_and_expires_during_pending_solve(
    hourly_entry, hass, freezer
):
    import asyncio
    import threading
    from unittest.mock import patch

    from custom_components.energy_compass import coordinator as module

    coordinator = hourly_entry.runtime_data
    started, release = threading.Event(), threading.Event()
    updated, invalidated = asyncio.Event(), asyncio.Event()
    original_compute = module.compute
    original_generation = coordinator.data["generated_at"]
    new_deadline = "2026-09-18T10:01:20+00:00"
    source = (
        "sensor.limit"
        if coordinator.configuration["helpers"]["grid_import_kw"]["max_age_seconds"]
        == 60
        else "sensor.market"
    )

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original_compute(*args, **kwargs)

    def published():
        if coordinator.data.get("inputs_valid_until") == new_deadline:
            updated.set()
        if coordinator.data.get("status") == "invalid_input":
            invalidated.set()

    unsubscribe = coordinator.async_add_listener(published)
    freezer.move_to("2026-09-18T10:00:10+00:00")
    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(coordinator.async_recalculate())
        try:
            assert await hass.async_add_executor_job(started.wait, 2)
            freezer.move_to("2026-09-18T10:00:20+00:00")
            state = hass.states.get(source)
            hass.states.async_set(
                source, state.state, {**state.attributes, "report": 2}
            )
            await asyncio.wait_for(updated.wait(), 2)
            assert coordinator.data["generated_at"] == original_generation
            assert hass.states.get("binary_sensor.hourly_alert").state == "off"
            freezer.move_to(new_deadline)
            async_fire_time_changed(hass, dt_util.utcnow())
            await asyncio.wait_for(invalidated.wait(), 2)
            assert hass.states.get("binary_sensor.hourly_alert").state == "on"
            assert coordinator._boundary.when() - hass.loop.time() >= 1
        finally:
            release.set()
            await job
            await hass.async_block_till_done()
            unsubscribe()


async def test_material_price_change_recalculates_before_hour(
    hourly_entry, hass, freezer
):
    first = hourly_entry.runtime_data.data["generated_at"]
    freezer.move_to("2026-09-18T10:02:00+00:00")
    rows = [dict(row) for row in hass.states.get("sensor.market").attributes["rows"]]
    rows[0]["price"] = 3
    hass.states.async_set("sensor.market", "ok", {"rows": rows})
    await hass.async_block_till_done()
    assert hourly_entry.runtime_data.data["generated_at"] != first
    assert float(
        hass.states.get("sensor.hourly_consumption_cost").state
    ) == pytest.approx(2.6133333333333333)


@pytest.fixture
async def switching_entry(recorder_mock, hass, enable_custom_integrations, freezer):
    from test_mode_dwell import native_config

    freezer.move_to("2026-09-18T10:00:00+00:00")
    config = native_config()
    config["settings"].update(
        refresh_minutes=60,
        total_time_limit_s=300,
        minimum_mode_minutes=15,
        minimum_export_episode_benefit=0,
        allow_battery_export=True,
        grid_export_kw=5,
        limit_export_to_pv=False,
        buy_rate=0,
        wear_per_kwh=0.05,
    )
    config["sources"]["sell"] = PriceSource(
        "forecast",
        (
            IntervalBinding(
                EntityBinding("sensor.sale", attribute="rows"),
                start_path="start",
                interval_minutes=15,
                value_path="price",
                unit="PLN/kWh",
                value_kind="price",
            ),
        ),
    ).to_dict()
    start = dt_util.utcnow()
    hass.states.async_set("sensor.soc", "50")
    hass.states.async_set(
        "sensor.sale",
        "ok",
        {
            "rows": [
                {
                    "start": start + timedelta(minutes=15 * i),
                    "price": 5 if i == 1 else -1,
                }
                for i in range(12)
            ]
        },
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Switching", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)


async def test_cached_mode_transition_updates_commitment_and_export_clock(
    switching_entry, hass, freezer
):
    coordinator = switching_entry.runtime_data
    first = coordinator.data["generated_at"]
    assert coordinator._battery_commitment["mode"] == "HOLD"
    for stamp in ("10:15:00", "10:20:00"):
        await advance(hass, freezer, f"2026-09-18T{stamp}+00:00", refresh_helper=False)
        assert coordinator.data["generated_at"] == first
        assert (
            hass.states.get("sensor.switching_energy_compass").state == "DISCHARGE_GRID"
        )
        assert coordinator._battery_commitment == {
            "mode": "DISCHARGE_GRID",
            "since": "2026-09-18T10:15:00+00:00",
        }
        assert coordinator._export_commitment["until"] == "2026-09-18T10:30:00+00:00"
    await advance(hass, freezer, "2026-09-18T10:30:00+00:00", refresh_helper=False)
    assert coordinator._battery_commitment["mode"] == "HOLD"
    assert coordinator._export_commitment is None
    assert coordinator.data["generated_at"] == first


async def test_late_result_publishes_current_mode_not_finished_first_interval(
    switching_entry, hass, freezer
):
    import asyncio
    import threading
    from unittest.mock import patch

    from custom_components.energy_compass import coordinator as module

    coordinator = switching_entry.runtime_data
    started, release = threading.Event(), threading.Event()
    original = module.compute

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        started.set()
        assert release.wait(10)
        return result

    freezer.move_to("2026-09-18T10:14:00+00:00")
    with patch.object(module, "compute", new=delayed):
        task = hass.async_create_task(coordinator.async_recalculate())
        try:
            assert await hass.async_add_executor_job(started.wait, 3)
            freezer.move_to("2026-09-18T10:16:00+00:00")
        finally:
            release.set()
            await asyncio.wait_for(task, 5)
    assert coordinator.data["valid"], coordinator.data
    assert coordinator.data["intervals"][0]["start"] == "2026-09-18T10:15:00+00:00"
    assert hass.states.get("sensor.switching_energy_compass").state == "DISCHARGE_GRID"
    assert coordinator._battery_commitment["since"] == "2026-09-18T10:15:00+00:00"


async def test_identical_fresh_soc_report_recovers_on_health_tick(
    switching_entry, hass, freezer
):
    from copy import deepcopy

    coordinator = switching_entry.runtime_data
    config = deepcopy(dict(switching_entry.data))
    config["settings"]["soc_max_age_seconds"] = 600
    hass.config_entries.async_update_entry(switching_entry, data=config)
    await coordinator.async_recalculate()
    first = coordinator.data["generated_at"]
    await advance(hass, freezer, "2026-09-18T10:10:01+00:00", refresh_helper=False)
    assert coordinator.data["status"] == "invalid_input"
    freezer.move_to("2026-09-18T10:11:00+00:00")
    hass.states.async_set("sensor.soc", "50")
    await hass.async_block_till_done()
    assert coordinator.data["status"] == "invalid_input"
    await advance(hass, freezer, "2026-09-18T10:16:00+00:00", refresh_helper=False)
    assert coordinator.data["valid"], coordinator.data
    assert coordinator.data["generated_at"] != first


async def test_overdue_input_deadline_is_consumed_once(hourly_entry, hass, freezer):
    coordinator = hourly_entry.runtime_data
    freezer.move_to("2026-09-18T10:10:00+00:00")
    coordinator._next_boundary(coordinator.configuration["settings"])
    assert 0 < coordinator._boundary.when() - hass.loop.time() <= 0.11
    freezer.tick(timedelta(seconds=0.2))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.hourly_alert").state == "on"
    assert coordinator._inputs_valid_until is None
    assert coordinator._boundary.when() - hass.loop.time() >= 1
