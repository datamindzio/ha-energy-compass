from datetime import UTC, datetime

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.runtime import compute
from custom_components.energy_compass.settings import default_configuration

KEYS = {"capacity_kwh", "eta_charge", "eta_discharge", "charge_kw", "discharge_kw"}


def _config():
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
        capacity_kwh=24.0,
        charge_kw=6.5,
        discharge_kw=7.5,
    )
    return config


def test_compute_carries_the_battery_parameters_it_solved_with():
    result = compute(_config(), {}, datetime(2026, 9, 17, tzinfo=UTC))
    parameters = result["controller_parameters"]
    assert set(parameters) == KEYS
    assert parameters["capacity_kwh"] == 24.0
    assert parameters["charge_kw"] == 6.5
    assert parameters["discharge_kw"] == 7.5
    assert 0 < parameters["eta_charge"] <= 1
    assert 0 < parameters["eta_discharge"] <= 1


@pytest.fixture
async def entry(recorder_mock, hass, enable_custom_integrations, freezer):
    freezer.move_to("2026-09-17T10:00:00+00:00")
    entry = MockConfigEntry(
        domain="energy_compass", data=_config(), title="Synthetic", version=3
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)


async def test_publication_and_retention_keep_the_parameters(entry):
    coordinator = entry.runtime_data
    assert coordinator.data["status"] == "ready"
    published = coordinator.data["controller_parameters"]
    assert set(published) == KEYS
    coordinator._invalidate("calculating", "test")
    assert coordinator.data["plan_retained"] is True
    assert coordinator.data["controller_parameters"] == published


async def test_plan_entity_does_not_expose_the_parameters(entry, hass):
    state = hass.states.get("sensor.synthetic_plan")
    assert state is not None
    assert "controller_parameters" not in state.attributes
    assert not KEYS & set(state.attributes)
