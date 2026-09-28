"""A strategy switch clears its one-shot release token exactly once."""

import threading
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass import coordinator as module
from custom_components.energy_compass.engine.models import SolveError
from custom_components.energy_compass.settings import (
    default_configuration,
    merged_configuration,
)


def _config(**settings):
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
        debounce_seconds=0,
        total_time_limit_s=300,
        **settings,
    )
    return config


@pytest.fixture
async def plain_entry(recorder_mock, hass, enable_custom_integrations, freezer):
    """A working entry with no pending strategy release."""
    freezer.move_to("2026-09-20T10:00:00+00:00")
    entry = MockConfigEntry(
        domain="energy_compass", data=_config(), title="Plain", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
async def released_entry(recorder_mock, hass, enable_custom_integrations, freezer):
    """An entry stored with a pending release and no in-memory coordinator state."""
    freezer.move_to("2026-09-20T10:00:00+00:00")
    config = _config()
    config["strategy_changed_at"] = dt_util.utcnow().isoformat()
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Released", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)


def _arm(entry, coordinator):
    """Stamp a pending release directly into the stored entry, as a switch would."""
    config = merged_configuration(entry)
    config["strategy_changed_at"] = dt_util.utcnow().isoformat()
    coordinator.hass.config_entries.async_update_entry(
        entry, options={**entry.options, "configuration": config}
    )
    coordinator.configuration["strategy_changed_at"] = config["strategy_changed_at"]


async def test_release_survives_restart_and_is_consumed_once(released_entry, hass):
    coordinator = released_entry.runtime_data
    assert coordinator.data["valid"] is True
    assert coordinator.data.get("strategy_released") is True
    assert merged_configuration(released_entry).get("strategy_changed_at") is None
    await coordinator.async_recalculate()
    assert coordinator.data.get("strategy_released") is False


async def test_release_not_consumed_when_generation_is_superseded(plain_entry, hass):
    coordinator = plain_entry.runtime_data
    _arm(plain_entry, coordinator)
    original = module.compute
    started1, started2 = threading.Event(), threading.Event()
    release1, release2 = threading.Event(), threading.Event()
    calls = []

    def delayed(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            started1.set()
            assert release1.wait(10)
        else:
            started2.set()
            assert release2.wait(10)
        return original(*args, **kwargs)

    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(coordinator.async_recalculate())
        try:
            assert await hass.async_add_executor_job(started1.wait, 2)
            coordinator._generation += 1
            release1.set()
            assert await hass.async_add_executor_job(started2.wait, 2)
            # the superseded generation must not have consumed the token
            assert merged_configuration(plain_entry)["strategy_changed_at"] is not None
        finally:
            release2.set()
            await job
            await hass.async_block_till_done()

    assert merged_configuration(plain_entry)["strategy_changed_at"] is None


async def test_release_not_consumed_when_publication_invalidates(
    plain_entry, hass, freezer
):
    coordinator = plain_entry.runtime_data
    _arm(plain_entry, coordinator)
    original = module.compute
    started, release = threading.Event(), threading.Event()

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(coordinator.async_recalculate())
        try:
            assert await hass.async_add_executor_job(started.wait, 2)
            # jump past the (1 h) plan horizon so the fresh publish finds no
            # current interval, however the compute itself still succeeds
            freezer.tick(timedelta(hours=3))
            release.set()
            await job
            await hass.async_block_till_done()
        finally:
            release.set()

    assert coordinator.data["valid"] is False
    assert merged_configuration(plain_entry)["strategy_changed_at"] is not None


async def test_release_not_consumed_when_compute_fails(plain_entry, hass):
    coordinator = plain_entry.runtime_data
    _arm(plain_entry, coordinator)

    with patch.object(module, "compute", side_effect=SolveError("infeasible")):
        await coordinator.async_recalculate()

    assert coordinator.data["status"] == "infeasible"
    assert merged_configuration(plain_entry)["strategy_changed_at"] is not None


async def test_clearing_the_token_does_not_reload_or_recalculate(plain_entry, hass):
    coordinator = plain_entry.runtime_data
    _arm(plain_entry, coordinator)
    generation_before = coordinator._generation

    with patch.object(coordinator, "async_recalculate") as recalculate:
        coordinator._clear_strategy_change()

    recalculate.assert_not_called()
    assert coordinator._generation == generation_before
    assert plain_entry.state is ConfigEntryState.LOADED
    assert merged_configuration(plain_entry)["strategy_changed_at"] is None


async def test_clearing_the_token_creates_the_options_document_when_absent(
    plain_entry, hass
):
    coordinator = plain_entry.runtime_data
    assert plain_entry.options == {}
    data = deepcopy(dict(plain_entry.data))
    data["strategy_changed_at"] = dt_util.utcnow().isoformat()
    hass.config_entries.async_update_entry(plain_entry, data=data)
    coordinator.configuration["strategy_changed_at"] = data["strategy_changed_at"]

    coordinator._clear_strategy_change()

    assert "configuration" in plain_entry.options
    assert merged_configuration(plain_entry)["strategy_changed_at"] is None
    assert plain_entry.data["strategy_changed_at"] == data["strategy_changed_at"]
