"""A running full candidate holds the charge path open, end to end (native engine).

The estimator reads real states, the controller reads the estimator and the fake
inverter's register bank shows what was written.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from controller_support import controller_of, publication, publish
from homeassistant.config_entries import ConfigEntryState
from native_site import (
    CHARGE,
    GRID,
    OPERATION,
    VOLTAGE,
    native_site,
    refresh_telemetry,
)

from custom_components.energy_compass import soc_estimator_config as cfg
from custom_components.energy_compass.coordinator import EnergyCompassCoordinator
from custom_components.energy_compass.settings import default_configuration

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
PROGRAM_SELECTS = [f"select.inverter_deye_program_{i}_charging" for i in range(1, 7)]
SOURCES = {
    "charge_counter": ("sensor.est_charged", "kWh"),
    "discharge_counter": ("sensor.est_discharged", "kWh"),
    "current": ("sensor.est_current", "A"),
    "pv_power": ("sensor.est_pv", "W"),
    "grid_power": ("sensor.est_grid", "W"),
}


@pytest.fixture
async def site(hass, freezer):
    entry, inverter = await native_site(
        hass,
        freezer,
        controller={"limits": {"commissioned_battery_modes": ["Capacity", "Voltage"]}},
    )
    # The plan is published by hand: the planner's own reaction to the sources
    # (which this site does not fully provide) must not replace it.
    with (
        patch.object(EnergyCompassCoordinator, "async_recalculate", AsyncMock()),
        patch.object(EnergyCompassCoordinator, "_source_changed", lambda *_: None),
        patch.object(EnergyCompassCoordinator, "_next_boundary", lambda *_: None),
    ):
        yield entry, inverter
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


def _put(hass, entity_id, state, unit=None):
    attributes = (
        dict(hass.states.get(entity_id).attributes)
        if hass.states.get(entity_id)
        else {}
    )
    if unit:
        attributes["unit_of_measurement"] = unit
    hass.states.async_set(entity_id, str(state), attributes, force_update=True)


def _sources(hass, *, voltage, export=2500.0):
    hass.states.async_set(OPERATION, "Voltage", hass.states.get(OPERATION).attributes)
    values = {
        "charge_counter": 100.0,
        "discharge_counter": 50.0,
        "current": -0.3,
        "pv_power": 4000.0,
        "grid_power": -export,
    }
    for role, (entity, unit) in SOURCES.items():
        _put(hass, entity, values[role], unit)
    _put(hass, VOLTAGE, voltage, "V")


def _configuration():
    config = default_configuration("EUR", "UTC")
    config["sources"].update(
        battery_enabled=True, soc_mode=cfg.COUNTER_ESTIMATOR, soc=None, bms_soc=None
    )
    config["settings"].update(grid_export_kw=0)
    config["soc_estimator"] = {
        "bindings": {
            **{role: {"entity_id": entity} for role, (entity, _) in SOURCES.items()},
            "voltage": {"entity_id": VOLTAGE},
        },
        "empty_voltage": 480,
        "full_min_voltage": 552,
        "eta_charge": 0.9,
        "initial_soc_percent": 50,
    }
    return config


async def _run(entry):
    host = controller_of(entry).host
    host.request("test")
    await host.async_wait_idle()


async def _start(hass, freezer, site, voltage):
    entry, inverter = site
    coordinator = entry.runtime_data
    coordinator.configuration = _configuration()
    estimator = coordinator.soc_estimator
    estimator.configure(coordinator.configuration)
    assert estimator.active, estimator.error
    _sources(hass, voltage=voltage)
    estimator.tick()
    freezer.tick(timedelta(seconds=30))
    _sources(hass, voltage=voltage)
    controller_of(entry).async_set_mode("Auto")
    publish(entry, publication(windows={}))
    estimator.tick()
    refresh_telemetry(hass)
    await _run(entry)
    return entry, inverter, estimator


async def _advance(hass, freezer, minutes, voltage):
    """Sources report every 30 s, as a live site does: an anchor reads fresh values."""
    for _ in range(int(2 * minutes)):
        freezer.tick(timedelta(seconds=30))
        _sources(hass, voltage=voltage)
        refresh_telemetry(hass)
        await hass.async_block_till_done()


def _timed(inverter, freezer):
    """Every register write takes a second, as on the wire."""
    apply = inverter._apply

    def timed(entity, value):
        freezer.tick(timedelta(seconds=1))
        apply(entity, value)

    inverter._apply = timed


async def _into_hold(hass, freezer, entry, estimator, voltage):
    """The HOLD row starts at 17:15: the sources keep reporting, the controller runs."""
    await _advance(hass, freezer, 15, voltage)
    freezer.tick(timedelta(seconds=1))
    await _run(entry)
    estimator.tick()


async def test_a_candidate_keeps_the_written_charge_current_at_the_cap_into_hold(
    hass, freezer, site
):
    entry, inverter, estimator = await _start(hass, freezer, site, voltage=555.0)
    controller = controller_of(entry)
    assert controller.state.runtime["state"] == "CHARGE_PV"
    assert estimator.state.full_candidate_since is not None
    assert estimator.full_candidate
    cap = float(hass.states.get(CHARGE).state)
    assert cap > 1
    _timed(inverter, freezer)
    inverter.writes.clear()

    await _into_hold(hass, freezer, entry, estimator, 555.0)
    assert controller.state.runtime["state"] == "HOLD"
    assert float(hass.states.get(CHARGE).state) == cap
    # The switch to HOLD's Grid program stops the writers for the length of one
    # transaction (the shared write order), so time passes between 0 A and the
    # cap; the hold current is never written.
    charge_writes = [value for entity, value in inverter.writes if entity == CHARGE]
    assert charge_writes[-1] == cap
    assert 1 not in charge_writes
    # HOLD keeps its own grid current and Grid program: nothing extra was opened.
    assert float(hass.states.get(GRID).state) == 1
    assert [hass.states.get(e).state for e in PROGRAM_SELECTS].count("Grid") == 1
    # The candidate survived the plan row that the estimator would exclude.
    assert estimator.full_candidate
    assert estimator.state.full_candidate_since is not None


async def test_a_charge_limit_that_stays_low_ends_the_candidate(hass, freezer, site):
    entry, _, estimator = await _start(hass, freezer, site, voltage=555.0)
    await _into_hold(hass, freezer, entry, estimator, 555.0)
    assert estimator.full_candidate

    # The limit entity is kept at 0 A (the controller would write it back).
    for _ in range(3):
        hass.states.async_set(CHARGE, "0", hass.states.get(CHARGE).attributes)
        await _advance(hass, freezer, 0.5, 555.0)
    assert estimator.full_candidate
    for _ in range(4):
        hass.states.async_set(CHARGE, "0", hass.states.get(CHARGE).attributes)
        await _advance(hass, freezer, 0.5, 555.0)
    assert not estimator.full_candidate


async def test_without_a_candidate_hold_lowers_the_charge_current(hass, freezer, site):
    entry, _, estimator = await _start(hass, freezer, site, voltage=540.0)
    assert not estimator.full_candidate
    assert float(hass.states.get(CHARGE).state) > 1

    await _into_hold(hass, freezer, entry, estimator, 540.0)
    assert controller_of(entry).state.runtime["state"] == "HOLD"
    assert float(hass.states.get(CHARGE).state) == 1


async def test_the_hold_breaks_and_the_charge_closes_when_the_voltage_falls(
    hass, freezer, site
):
    entry, _, estimator = await _start(hass, freezer, site, voltage=555.0)
    await _into_hold(hass, freezer, entry, estimator, 555.0)
    assert float(hass.states.get(CHARGE).state) > 1

    await _advance(hass, freezer, 1, 530.0)
    assert not estimator.full_candidate
    await _run(entry)
    assert float(hass.states.get(CHARGE).state) == 1
