"""The strategy select writes the user's choice and supersedes any run in flight."""

import threading
from unittest.mock import patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from test_recalculation import published_entry as published_entry  # noqa: PLC0414

from custom_components.energy_compass import coordinator as module
from custom_components.energy_compass.select import EnergyCompassStrategySelect
from custom_components.energy_compass.settings import STRATEGIES, merged_configuration


async def _select_option(hass, entity_id, option):
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": option},
        blocking=True,
    )


async def test_strategy_select_exposes_six_options(published_entry, hass):
    state = hass.states.get("select.refresh_strategy")
    assert state is not None
    assert set(state.attributes["options"]) == set(STRATEGIES)


async def test_strategy_select_reports_current_strategy(published_entry, hass):
    assert hass.states.get("select.refresh_strategy").state == "cost_min"


async def test_select_option_writes_entry_options_configuration(published_entry, hass):
    await _select_option(hass, "select.refresh_strategy", "max_export")
    assert merged_configuration(published_entry)["settings"]["strategy"] == "max_export"


async def test_select_option_stamps_strategy_changed_at(published_entry, hass):
    started, release = threading.Event(), threading.Event()
    original = module.compute

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(
            _select_option(hass, "select.refresh_strategy", "max_export")
        )
        try:
            assert await hass.async_add_executor_job(started.wait, 2)
            assert (
                merged_configuration(published_entry)["strategy_changed_at"] is not None
            )
        finally:
            release.set()
            await job
            await hass.async_block_till_done()


async def test_select_option_updates_cached_configuration_immediately(
    published_entry, hass
):
    coordinator = published_entry.runtime_data
    started, release = threading.Event(), threading.Event()
    original = module.compute

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(
            _select_option(hass, "select.refresh_strategy", "max_export")
        )
        try:
            assert await hass.async_add_executor_job(started.wait, 2)
            assert coordinator.configuration["settings"]["strategy"] == "max_export"
            assert coordinator.data["strategy"] != "max_export"
        finally:
            release.set()
            await job
            await hass.async_block_till_done()

    assert coordinator.data["strategy"] == "max_export"


async def test_select_option_bumps_the_generation(published_entry, hass):
    coordinator = published_entry.runtime_data
    before = coordinator._generation
    await _select_option(hass, "select.refresh_strategy", "max_export")
    assert coordinator._generation == before + 1


async def test_select_option_triggers_recalculation(published_entry, hass):
    coordinator = published_entry.runtime_data
    await _select_option(hass, "select.refresh_strategy", "max_export")
    assert coordinator.data["strategy"] == "max_export"
    assert hass.states.get("select.refresh_strategy").state == "max_export"


async def test_select_same_option_is_a_noop(published_entry, hass):
    coordinator = published_entry.runtime_data
    before_generation = coordinator._generation
    await _select_option(hass, "select.refresh_strategy", "cost_min")
    assert coordinator._generation == before_generation
    assert published_entry.options == {}


async def test_select_rejects_unknown_option(published_entry, hass):
    entity = EnergyCompassStrategySelect(published_entry.runtime_data, "strategy")
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("not_a_strategy")


async def test_select_is_available_with_an_invalid_plan(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from test_mode_dwell import native_config

    freezer.move_to("2026-09-18T10:00:00+00:00")
    entry = MockConfigEntry(
        domain="energy_compass", data=native_config(), title="Empty", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.empty_plan").state == "unavailable"
    strategy = hass.states.get("select.empty_strategy")
    assert strategy is not None
    assert strategy.state == "cost_min"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_select_during_solve_discards_the_running_generation(
    published_entry, hass
):
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
            await _select_option(hass, "select.refresh_strategy", "max_export")
        finally:
            unsubscribe()
            release.set()
            await job
            await hass.async_block_till_done()

    assert "cost_min" not in seen
    assert coordinator.data["strategy"] == "max_export"
