"""Keep published values visible while replacing an advisory generation."""

import asyncio
import threading
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from test_hourly_schedule import switching_entry as switching_entry  # noqa: PLC0414

from custom_components.energy_compass import coordinator as module
from custom_components.energy_compass.config_models import PriceSource
from custom_components.energy_compass.engine.models import SolveError
from custom_components.energy_compass.sensor import SENSOR_KEYS
from custom_components.energy_compass.settings import (
    default_configuration,
    merged_configuration,
)
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)


@pytest.fixture
async def published_entry(recorder_mock, hass, enable_custom_integrations, freezer):
    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=4,
        display_horizon_hours=4,
        reference_horizon_hours=4,
        cheap_percentile=50,
    )
    config["helpers"]["grid_import_kw"] = {
        "entity": {"entity_id": "input_number.limit"},
        "unit": "kW",
        "max_age_seconds": None,
    }
    config["sources"]["buy"] = PriceSource(
        "forecast",
        (
            IntervalBinding(
                EntityBinding("sensor.market", attribute="rows"),
                start_path="start",
                interval_minutes=60,
                value_path="price",
                unit="EUR/kWh",
                value_kind="price",
            ),
        ),
    ).to_dict()
    hass.states.async_set("input_number.limit", "5", {"unit_of_measurement": "kW"})
    now = dt_util.utcnow()
    hass.states.async_set(
        "sensor.market",
        "ok",
        {
            "rows": [
                {"start": now + timedelta(hours=index), "price": price}
                for index, price in enumerate((0, 0.1, 0.5, 1.5))
            ]
        },
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Refresh", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)


def recommendation_states(hass):
    return {
        key: hass.states.get(f"sensor.refresh_{key}")
        for key in SENSOR_KEYS
        if key not in ("optimizer_status", "battery_balance")
    }


@pytest.mark.parametrize("copy_data", [False, True])
async def test_unchanged_entities_are_not_reported_again(
    published_entry, hass, freezer, copy_data
):
    """Duplicate coordinator publications must not write unchanged entity states."""
    coordinator = published_entry.runtime_data
    entity_ids = [
        f"sensor.refresh_{key}"
        for key in SENSOR_KEYS
        if not key.startswith("next_") and key != "battery_balance"
    ] + ["binary_sensor.refresh_forecast_valid"]
    reported = {
        entity_id: hass.states.get(entity_id).last_reported for entity_id in entity_ids
    }
    freezer.tick(timedelta(seconds=1))
    data = deepcopy(coordinator.data) if copy_data else coordinator.data
    coordinator.async_set_updated_data(data)
    for entity_id, timestamp in reported.items():
        assert hass.states.get(entity_id).last_reported == timestamp, entity_id


async def test_only_entities_with_changed_values_or_attributes_are_reported(
    published_entry, hass, freezer
):
    """Nested plan edits must publish without rewriting unrelated sensors."""
    coordinator = published_entry.runtime_data
    unchanged_id = "sensor.refresh_expected_wear_cost"
    original_reported = hass.states.get(unchanged_id).last_reported
    level_id = "sensor.refresh_consumption_compass"
    old_level = hass.states.get(level_id).state
    new_level = "LIMIT" if old_level != "LIMIT" else "BOOST"
    plan_id = "sensor.refresh_plan"
    original_plan_state = hass.states.get(plan_id).state
    freezer.tick(timedelta(seconds=1))
    # In-place edits exercise isolation from the coordinator's mutable arrays.
    coordinator.data["outlook"][0]["level"] = new_level
    coordinator.async_set_updated_data(coordinator.data)
    assert hass.states.get(level_id).state == new_level
    assert hass.states.get(plan_id).state == original_plan_state
    assert hass.states.get(plan_id).attributes["outlook"][0]["level"] == new_level
    assert hass.states.get(plan_id).last_reported == dt_util.utcnow()
    assert hass.states.get(unchanged_id).last_reported == original_reported


async def test_freshness_attributes_publish_with_unchanged_value(
    published_entry, hass, freezer
):
    """Deduplication must not freeze timestamps that consumers use for validity."""
    coordinator = published_entry.runtime_data
    entity_id = "sensor.refresh_consumption_compass"
    original_state = hass.states.get(entity_id).state
    freezer.tick(timedelta(seconds=1))
    new_deadline = "2026-09-17T10:35:00+00:00"
    coordinator.async_set_updated_data(
        {**coordinator.data, "valid_until": new_deadline}
    )
    state = hass.states.get(entity_id)
    assert state.state == original_state
    assert state.attributes["valid_until"] == new_deadline
    assert state.last_reported == dt_util.utcnow()


@pytest.mark.parametrize("trigger", ["manual", "source", "boundary"])
async def test_recalculation_keeps_published_values_until_replacement(
    published_entry, hass, freezer, trigger
):
    """Starting or queueing a solve must not erase a displayed generation."""
    before = recommendation_states(hass)
    assert all(
        state.state not in ("unavailable", "unknown") for state in before.values()
    )
    assert hass.states.get("binary_sensor.refresh_forecast_valid").state == "on"
    started, release = threading.Event(), threading.Event()
    original = module.compute
    job = None

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    def assert_previous_visible():
        assert hass.states.get("sensor.refresh_optimizer_status").state == "calculating"
        assert hass.states.get("binary_sensor.refresh_forecast_valid").state == "on"
        assert (
            hass.states.get("binary_sensor.refresh_forecast_valid").attributes[
                "missing_sources"
            ]
            == []
        )
        for key, previous in before.items():
            current = hass.states.get(previous.entity_id)
            assert current.state == previous.state, key
            assert (
                current.attributes["generated_at"]
                == previous.attributes["generated_at"]
            )
            assert current.attributes["valid_until"] == "2026-09-17T14:00:00+00:00"
            assert current.attributes["refreshing"] is True
        assert (
            hass.states.get("sensor.refresh_plan").attributes["intervals"]
            == before["plan"].attributes["intervals"]
        )

    with patch.object(module, "compute", new=delayed):
        try:
            if trigger == "boundary":
                freezer.move_to(before["plan"].attributes["valid_until"])
                async_fire_time_changed(hass, dt_util.utcnow())
            else:
                freezer.tick(timedelta(seconds=1))
                if trigger == "manual":
                    job = hass.async_create_task(
                        published_entry.runtime_data.async_recalculate()
                    )
                else:
                    hass.states.async_set(
                        "input_number.limit", "6", {"unit_of_measurement": "kW"}
                    )
                    await hass.async_block_till_done()
                    assert not started.is_set()
                    assert_previous_visible()
                    freezer.tick(timedelta(seconds=5))
                    async_fire_time_changed(hass, dt_util.utcnow())
            # Yield to timer callbacks before waiting on the executor thread.
            await asyncio.sleep(0)
            assert await hass.async_add_executor_job(started.wait, 2)
            assert_previous_visible()
        finally:
            release.set()
            if job is not None:
                await job
            await hass.async_block_till_done()

    assert hass.states.get("sensor.refresh_optimizer_status").state == "ready"
    assert hass.states.get("binary_sensor.refresh_forecast_valid").state == "on"
    plan = hass.states.get("sensor.refresh_plan")
    assert plan.state != before["plan"].state
    assert plan.attributes["refreshing"] is False


@pytest.mark.parametrize("failure", ["timeout", "infeasible", "error"])
async def test_failed_refresh_and_pending_retry_keep_values(
    published_entry, hass, failure
):
    """A failed replacement and its retry keep the last covered plan available."""
    error = RuntimeError("worker failed") if failure == "error" else SolveError(failure)
    with patch.object(module, "compute", side_effect=error):
        await published_entry.runtime_data.async_recalculate()
    assert hass.states.get("sensor.refresh_optimizer_status").state == failure
    assert hass.states.get("binary_sensor.refresh_forecast_valid").state == "on"
    assert all(
        state.state != "unavailable" for state in recommendation_states(hass).values()
    )
    # Retrying keeps both the plan and the alert until replacement succeeds.
    started, release = threading.Event(), threading.Event()
    original = module.compute

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(published_entry.runtime_data.async_recalculate())
        try:
            assert await hass.async_add_executor_job(started.wait, 2)
            assert (
                hass.states.get("sensor.refresh_optimizer_status").state
                == "calculating"
            )
            assert hass.states.get("binary_sensor.refresh_forecast_valid").state == "on"
            assert hass.states.get("binary_sensor.refresh_alert").state == "on"
            assert all(
                state.state != "unavailable"
                for state in recommendation_states(hass).values()
            )
        finally:
            release.set()
            await job
    assert hass.states.get("binary_sensor.refresh_forecast_valid").state == "on"


async def test_source_loss_during_refresh_keeps_retained_values(published_entry, hass):
    """A source problem cannot erase the plan during or after a pending solve."""
    coordinator = published_entry.runtime_data
    started, release = threading.Event(), threading.Event()
    invalidated = asyncio.Event()
    original = module.compute

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    def updated():
        if coordinator.data["status"] == "invalid_input":
            invalidated.set()

    unsubscribe = coordinator.async_add_listener(updated)
    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(coordinator.async_recalculate())
        try:
            assert await hass.async_add_executor_job(started.wait, 2)
            hass.states.async_set("input_number.limit", "unavailable")
            await asyncio.wait_for(invalidated.wait(), 2)
            assert hass.states.get("binary_sensor.refresh_forecast_valid").state == "on"
            assert all(
                state.state != "unavailable"
                for state in recommendation_states(hass).values()
            )
        finally:
            release.set()
            await job
            await hass.async_block_till_done()
            unsubscribe()
    assert hass.states.get("sensor.refresh_optimizer_status").state == "invalid_input"
    assert all(
        state.state != "unavailable" for state in recommendation_states(hass).values()
    )


async def test_expired_snapshot_without_refresh_remains_unavailable(
    published_entry, hass, freezer
):
    """Retaining a pending display must not relax normal freshness checks."""
    coordinator = published_entry.runtime_data
    freezer.move_to(coordinator.data["valid_until"])
    coordinator.async_set_updated_data(coordinator.data)
    assert hass.states.get("binary_sensor.refresh_forecast_valid").state == "off"
    assert all(
        state.state == "unavailable" for state in recommendation_states(hass).values()
    )


async def test_ended_window_is_not_upcoming_in_retained_plan(
    published_entry, hass, freezer
):
    """The retained plan must agree with the timestamp entity after a window ends."""
    started, release = threading.Event(), threading.Event()
    original = module.compute

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    freezer.move_to("2026-09-17T11:00:00+00:00")
    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(published_entry.runtime_data.async_recalculate())
        try:
            assert await hass.async_add_executor_job(started.wait, 2)
            assert (
                hass.states.get("sensor.refresh_next_boost_start").state
                == "unavailable"
            )
            status = hass.states.get("sensor.refresh_plan").attributes["window_status"]
            assert status["boost"] == "none_in_coverage"
            assert status["cheap"] == "active"
            assert status["limit"] == "upcoming"
        finally:
            release.set()
            await job


async def test_apply_configuration_supersedes_in_flight_generation(
    published_entry, hass, freezer
):
    """Applying a new configuration mid-solve must discard the stale generation."""
    coordinator = published_entry.runtime_data
    started, release = threading.Event(), threading.Event()
    original = module.compute

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    seen = []
    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(coordinator.async_recalculate())
        unsubscribe = coordinator.async_add_listener(
            lambda: seen.append(coordinator.data.get("strategy"))
        )
        try:
            assert await hass.async_add_executor_job(started.wait, 2)
            new_config = merged_configuration(published_entry)
            new_config["settings"]["strategy"] = "max_export"
            await coordinator.async_apply_configuration(new_config)
        finally:
            unsubscribe()
            release.set()
            await job
            await hass.async_block_till_done()

    assert "cost_min" not in seen
    assert coordinator.data["strategy"] == "max_export"


async def test_rapid_switches_publish_only_the_last(published_entry, hass, freezer):
    """Several switches while a solve is in flight collapse into one recompute."""
    coordinator = published_entry.runtime_data
    started, release = threading.Event(), threading.Event()
    original = module.compute
    calls = []

    def delayed(*args, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(coordinator.async_recalculate())
        try:
            assert await hass.async_add_executor_job(started.wait, 2)
            for strategy in ("grid_friendly", "backup_ready", "max_export"):
                config = merged_configuration(published_entry)
                config["settings"]["strategy"] = strategy
                await coordinator.async_apply_configuration(config)
            release.set()
            await job
            await hass.async_block_till_done()
        finally:
            release.set()

    assert coordinator.data["strategy"] == "max_export"
    # the stale in-flight solve plus exactly one retry for the last switch
    assert len(calls) == 2


async def test_apply_configuration_updates_the_cached_snapshot_before_refresh(
    published_entry, hass
):
    """The snapshot swap must be visible before the new solve even starts."""
    coordinator = published_entry.runtime_data
    started, release = threading.Event(), threading.Event()
    original = module.compute

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    new_config = merged_configuration(published_entry)
    new_config["settings"]["strategy"] = "max_export"
    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(coordinator.async_apply_configuration(new_config))
        try:
            assert await hass.async_add_executor_job(started.wait, 2)
            assert coordinator.configuration["settings"]["strategy"] == "max_export"
            assert coordinator.data["strategy"] != "max_export"
        finally:
            release.set()
            await job
            await hass.async_block_till_done()

    assert coordinator.data["strategy"] == "max_export"


async def test_published_strategy_comes_from_the_result_not_the_configuration(
    published_entry, hass
):
    """Mutating the cached configuration must never move the published label."""
    coordinator = published_entry.runtime_data
    assert coordinator.data["strategy"] == "cost_min"
    coordinator.configuration["settings"]["strategy"] = "max_export"
    assert coordinator.data["strategy"] == "cost_min"


@pytest.mark.replan_cooldown
async def test_input_change_waits_for_minimum_replan_pause(
    published_entry, hass, freezer
):
    """Live failure: every solve used its full budget and restarted on the next
    input change, so 'ready' lasted ~80 ms and a controller gated on it never
    accepted a new plan."""
    coordinator = published_entry.runtime_data
    pause = coordinator.configuration["settings"].get("minimum_replan_seconds", 900)
    assert pause == 900
    original = hass.states.get("sensor.refresh_plan").state
    calls = []
    real = module.compute

    def counted(*args, **kwargs):
        calls.append(dt_util.utcnow())
        return real(*args, **kwargs)

    with patch.object(module, "compute", new=counted):
        freezer.move_to("2026-09-17T10:00:30+00:00")
        hass.states.async_set("input_number.limit", "6", {"unit_of_measurement": "kW"})
        await hass.async_block_till_done()
        assert not calls
        assert coordinator.data["status"] == "ready"
        assert hass.states.get("sensor.refresh_plan").state == original
        freezer.move_to("2026-09-17T10:15:01+00:00")
        async_fire_time_changed(hass, dt_util.utcnow(), fire_all=True)
        await hass.async_block_till_done()
    assert len(calls) == 1
    assert coordinator.data["status"] == "ready"
    assert hass.states.get("sensor.refresh_plan").state != original


@pytest.mark.replan_cooldown
async def test_invalid_input_is_not_delayed_by_replan_pause(
    published_entry, hass, freezer
):
    coordinator = published_entry.runtime_data
    freezer.move_to("2026-09-17T10:00:30+00:00")
    hass.states.async_set("input_number.limit", "6", {"unit_of_measurement": "kW"})
    await hass.async_block_till_done()
    assert coordinator.data["status"] == "ready"
    hass.states.async_set("input_number.limit", "unavailable")
    await hass.async_block_till_done()
    assert coordinator.data["status"] == "invalid_input"
    assert hass.states.get("binary_sensor.refresh_alert").state == "on"


@pytest.mark.replan_cooldown
async def test_changes_during_a_solve_rerun_after_the_pause(
    published_entry, hass, freezer
):
    import asyncio

    coordinator = published_entry.runtime_data
    real = module.compute
    calls = []

    async def change():
        hass.states.async_set("input_number.limit", "7", {"unit_of_measurement": "kW"})
        await asyncio.sleep(0)

    def busy(*args, **kwargs):
        calls.append(1)
        result = real(*args, **kwargs)
        if len(calls) == 1:
            asyncio.run_coroutine_threadsafe(change(), hass.loop).result(5)
        return result

    freezer.move_to("2026-09-17T10:05:00+00:00")
    with patch.object(module, "compute", new=busy):
        await coordinator.async_recalculate()
        await hass.async_block_till_done()
        # The superseded result is published and stays ready during the pause.
        assert len(calls) == 1
        assert coordinator.data["status"] == "ready"
        freezer.move_to("2026-09-17T10:20:01+00:00")
        async_fire_time_changed(hass, dt_util.utcnow(), fire_all=True)
        await hass.async_block_till_done()
    assert len(calls) == 2
    assert coordinator.data["status"] == "ready"


@pytest.mark.replan_cooldown
async def test_rate_limit_survives_a_failed_solve(published_entry, hass, freezer):
    """Gating on 'ready' left the busiest state ungated: after a solve ended in
    error the retained plan was still published, yet every input change started
    another calculation immediately."""
    coordinator = published_entry.runtime_data
    calls = []

    def failing(*args, **kwargs):
        calls.append(dt_util.utcnow())
        raise SolveError("infeasible")

    freezer.move_to("2026-09-17T10:05:00+00:00")
    with patch.object(module, "compute", new=failing):
        await coordinator.async_recalculate()
        await hass.async_block_till_done()
        assert len(calls) == 1
        assert coordinator.data["valid"]
        assert coordinator.data["plan_retained"]
        freezer.move_to("2026-09-17T10:06:00+00:00")
        hass.states.async_set("input_number.limit", "6", {"unit_of_measurement": "kW"})
        await hass.async_block_till_done()
        assert len(calls) == 1
        freezer.move_to("2026-09-17T10:20:01+00:00")
        async_fire_time_changed(hass, dt_util.utcnow(), fire_all=True)
        await hass.async_block_till_done()
    assert len(calls) == 2


@pytest.mark.replan_cooldown
async def test_rate_limit_lifts_without_a_retained_plan(published_entry, hass, freezer):
    """A limit that outlived the plan would leave the integration with nothing
    to publish until it expired."""
    coordinator = published_entry.runtime_data
    calls = []
    real = module.compute

    def counted(*args, **kwargs):
        calls.append(dt_util.utcnow())
        return real(*args, **kwargs)

    freezer.move_to("2026-09-17T10:00:30+00:00")
    coordinator.data = {**coordinator.data, "valid": False}
    with patch.object(module, "compute", new=counted):
        hass.states.async_set("input_number.limit", "6", {"unit_of_measurement": "kW"})
        await hass.async_block_till_done()
        freezer.tick(timedelta(seconds=6))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    assert len(calls) == 1


@pytest.mark.replan_cooldown
async def test_calculation_counts_report_the_cadence(published_entry, hass, freezer):
    coordinator = published_entry.runtime_data
    counts = coordinator.calculation_counts()
    assert counts["calculations_since_load"] >= 1
    assert counts["calculations_last_hour"] == counts["calculations_since_load"]
    assert counts["last_calculation_started_at"] == "2026-09-17T10:00:00+00:00"
    attrs = hass.states.get("sensor.refresh_optimizer_status").attributes
    assert attrs["last_calculation_started_at"] == "2026-09-17T10:00:00+00:00"
    assert attrs["calculations_since_load"] == counts["calculations_since_load"]
    freezer.move_to("2026-09-17T11:30:00+00:00")
    later = coordinator.calculation_counts()
    assert later["calculations_last_hour"] == 0
    assert later["calculations_last_24h"] == counts["calculations_last_24h"]


async def test_soc_dead_band_holds_until_the_trigger_percent(
    switching_entry, hass, freezer
):
    """A battery under load crosses small SOC steps constantly; only a move of
    soc_trigger_percent from the last calculated value starts a new one."""
    coordinator = switching_entry.runtime_data
    assert coordinator.configuration["settings"]["soc_trigger_percent"] == 5
    calls = []
    real = module.compute

    def counted(*args, **kwargs):
        calls.append(dt_util.utcnow())
        return real(*args, **kwargs)

    with patch.object(module, "compute", new=counted):
        freezer.move_to("2026-09-18T10:10:00+00:00")
        hass.states.async_set("sensor.soc", "54")
        await hass.async_block_till_done()
        freezer.tick(timedelta(seconds=6))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert not calls
        freezer.move_to("2026-09-18T10:20:00+00:00")
        hass.states.async_set("sensor.soc", "56")
        await hass.async_block_till_done()
        freezer.tick(timedelta(seconds=6))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    assert len(calls) == 1
