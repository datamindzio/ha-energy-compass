"""The native controller reads the counter SOC estimator instead of the BMS SOC."""

from datetime import UTC, datetime

import pytest
from homeassistant.config_entries import ConfigEntryState
from native_site import OPERATION, SOC, native_site

from custom_components.energy_compass import controller_profile as profile
from custom_components.energy_compass.controller_ha import estimator_capacity_conflict
from custom_components.energy_compass.controller_native import native_setup

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
REPORTED = datetime(2026, 10, 8, 17, 0, 50, tzinfo=UTC)


@pytest.fixture
async def site(hass, freezer):
    entry, _ = await native_site(hass, freezer)
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


def _estimator_on(entry, fraction=0.37):
    coordinator = entry.runtime_data
    coordinator.configuration["sources"].update(
        battery_enabled=True, soc_mode="counter_estimator"
    )
    coordinator.soc_estimator.view = lambda: (
        None
        if fraction is None
        else {"fraction": fraction, "reported_at": REPORTED, "top_confirmed": False}
    )
    return coordinator


async def test_the_controller_reads_the_estimator_not_the_bms_entity(hass, site):
    port = site.runtime_data.controller.host.port
    assert port.snapshot("start").state(SOC) == "51"
    _estimator_on(site)
    snapshot = port.snapshot("start")
    assert float(snapshot.state(SOC)) == pytest.approx(37.0)
    assert snapshot.last_reported(SOC) == REPORTED.timestamp()


async def test_an_unavailable_estimator_fails_the_telemetry_gate(hass, site):
    _estimator_on(site, fraction=None)
    snapshot = site.runtime_data.controller.host.port.snapshot("start")
    assert snapshot.state(SOC) == "unavailable"
    decision = profile.decide(
        snapshot, site.runtime_data.controller.host.port.setup.limits
    )
    assert decision.telemetry is False


async def test_capacity_is_not_commissioned_while_the_estimator_is_the_source(
    hass, site
):
    controller = site.runtime_data.controller
    before = native_setup(hass, controller)
    assert "Capacity" in before.limits.commissioned_battery_modes
    _estimator_on(site)
    after = native_setup(hass, controller)
    assert "Capacity" not in after.limits.commissioned_battery_modes
    assert after.limits.max_current == before.limits.max_current


async def test_the_options_refusal_bites_only_in_capacity_mode(hass, site):
    controller = site.options["controller"]
    assert hass.states.get(OPERATION).state == "Capacity"
    assert estimator_capacity_conflict(hass, controller) is True
    hass.states.async_set(OPERATION, "Voltage", hass.states.get(OPERATION).attributes)
    assert estimator_capacity_conflict(hass, controller) is False
    assert (
        estimator_capacity_conflict(hass, {**controller, "engine": "blueprint"})
        is False
    )
    assert estimator_capacity_conflict(hass, {**controller, "enabled": False}) is False
    assert estimator_capacity_conflict(hass, None) is False
