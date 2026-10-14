"""What the estimator reads of the native controller through real Home Assistant states."""

import dataclasses
import datetime as dt

import pytest
from homeassistant.config_entries import ConfigEntryState
from native_site import native_site

from custom_components.energy_compass.soc_estimator import (
    Anchor,
    EstimatorSettings,
    EstimatorState,
    Inputs,
    step,
)

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
PREFIX = "inverter_deye_program_"
T0 = dt.datetime(2026, 10, 14, 12, tzinfo=dt.UTC)
SETTINGS = EstimatorSettings(
    capacity_kwh=24.0,
    empty_voltage_v=496.0,
    eta_charge=0.9,
    max_charge_current_a=14.0,
    anchor_on_candidates=True,
    export_limit_w=0.0,
)
ROLES = ("charge_limit", "discharge_limit", "grid_charge_current")


@pytest.fixture
async def site(hass, freezer):
    entry, _ = await native_site(hass, freezer)
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


def _program(hass, number, direction, volts):
    hass.states.async_set(
        f"select.{PREFIX}{number}_charging",
        direction,
        {"options": ["Disabled", "Grid", "Generator", "Grid & Generator", "Sell"]},
    )
    hass.states.async_set(f"number.{PREFIX}{number}_voltage", volts, {})


def _runtime(entry, **values):
    controller = entry.runtime_data.controller
    controller.state = dataclasses.replace(controller.state, runtime=values)


def _facts(entry):
    return entry.runtime_data.soc_estimator._facts(dict.fromkeys(ROLES))


async def test_the_native_controller_state_and_the_program_are_read(hass, site):
    _program(hass, 3, "Grid", "55.2")
    _runtime(site, code="ok", state="CHARGE_GRID", active_tou=3)
    facts = _facts(site)
    assert facts.state == "CHARGE_GRID"
    assert facts.program_direction == "Grid"
    # The program voltage is in units of the 48 V scale; the pack reads x10.
    assert facts.program_voltage_v == pytest.approx(552.0)
    assert facts.charge_limit_a == 18.0
    assert facts.grid_charge_a == 16.0


async def test_only_the_active_program_counts(hass, site):
    _program(hass, 3, "Grid", "55.2")
    _program(hass, 4, "Disabled", "49.6")
    _runtime(site, code="ok", state="CHARGE_PV", active_tou=4)
    facts = _facts(site)
    assert facts.program_direction == "Disabled"
    assert facts.program_voltage_v == pytest.approx(496.0)


async def test_a_blocked_controller_gives_no_state(hass, site):
    _program(hass, 3, "Grid", "55.2")
    _runtime(site, code="blocked", state="CHARGE_GRID", active_tou=3)
    assert _facts(site).state is None


async def test_the_full_rule_follows_the_program_read_from_the_states(hass, site):
    _program(hass, 3, "Grid", "55.2")
    _runtime(site, code="ok", state="CHARGE_GRID", active_tou=3)
    facts = _facts(site)

    def run(voltage):
        state = EstimatorState(Anchor("initial", T0, 20.0, 1000.0, 500.0), T0)
        for second in range(0, 660, 60):
            step(
                state,
                SETTINGS,
                Inputs(
                    T0 + dt.timedelta(seconds=second),
                    1000.0,
                    500.0,
                    -100.0,
                    voltage,
                    -0.2,
                    0.0,
                    800.0,
                    -1500.0,
                    facts,
                ),
            )
        return state.candidates

    assert len(run(540.0)) == 1
    assert run(548.0) == []


async def test_without_the_native_engine_the_targets_are_invisible(hass, freezer):
    entry, _ = await native_site(hass, freezer, controller={"engine": "blueprint"})
    try:
        _program(hass, 3, "Grid", "55.2")
        facts = _facts(entry)
        assert facts.state is None
        assert facts.program_direction is None
        assert facts.program_voltage_v is None
        assert facts.charge_limit_a is None
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
