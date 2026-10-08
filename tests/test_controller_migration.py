"""Rehearse the package-to-integration swap: the same plan, zero register writes.

A running v0.1.35 session is produced by the frozen blueprint itself. Its package
helpers are copied into Home Assistant, `controller_import_package` adopts them, and
the controller sensor that results drives the new blueprint on the same inverter
state. The new blueprint must decide what the old one decided and have nothing to
write.
"""

import copy
import json
from unittest.mock import AsyncMock, patch

import pytest
from controller_support import package_states, register_solarman
from deye_harness import MODE as NEW_MODE
from deye_harness import RT as NEW_RT
from deye_harness import Harness as NewHarness
from deye_harness import Runner as NewRunner
from deye_oracle import (
    CACHE,
    MODE,
    PENDING,
    RT,
    SESSION,
    SESSION_START,
    Harness,
    P,
    Runner,
)
from deye_oracle import timestamp as oracle_timestamp
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.coordinator import EnergyCompassCoordinator
from custom_components.energy_compass.settings import default_configuration

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

SERVICE_CONTROLLER = "sensor.synthetic_deye_controller"
SITE_ETA = 0.9746794344808963
STALE = {"conservative_grid_charge": True, "grid_current_limit_commissioned": 10}
CURRENTS = ("charge_entity", "discharge_entity", "grid_entity")
CURRENTS_OF_INTEREST = CURRENTS


def running_session(state="CHARGE_PV", **row):
    """A v0.1.35 controller that has been writing in Auto for a few minutes."""
    h = Harness()
    plan_row = h.data[P]["attributes"]["intervals"][0]
    plan_row.update(state=state, **row)
    runner = Runner(h)
    runner.run()
    assert runner.writes
    runtime = h.data[RT]["attributes"]["runtime"]
    assert runtime["confirmed_mode"] == state
    h.set(RT, "ok", runtime=runtime | STALE)
    return h


def old_verdict(h):
    decision = h.decision()
    h.ctx["decision"] = decision
    cleanup = h.render(h.expression("cleanup"))
    return decision, cleanup


def package_of(h):
    return {
        "mode": h.data[MODE]["state"],
        "session": h.data[SESSION]["state"],
        "session_start": h.data[SESSION_START]["attributes"]["timestamp"],
        "pending": h.data[PENDING]["state"],
        "snapshot": copy.deepcopy(h.data[CACHE]["attributes"]["snapshot"]),
        "runtime": copy.deepcopy(h.data[RT]["attributes"]["runtime"]),
    }


def ready_publication(old):
    """What the coordinator publishes for the plan the old blueprint was running."""
    attrs = old.data[P]["attributes"]
    return {
        "status": "ready",
        "valid": True,
        "alert": None,
        "refreshing": False,
        "plan_retained": False,
        **{
            key: attrs[key]
            for key in ("generated_at", "valid_until", "intervals", "dispatch_policy")
        },
    }


async def imported_controller(hass, freezer, old, capacity=25.0, eta=SITE_ETA):
    """Load an entry whose settings equal v0.1.35's defaults and import `old`."""
    freezer.move_to(old.now)
    device = register_solarman(hass)
    config = default_configuration("EUR", "UTC")
    config["settings"] |= {
        "capacity_kwh": capacity,
        "eta_charge": eta,
        "eta_discharge": eta,
        "charge_kw": 8.0,
        "discharge_kw": 8.0,
    }
    entry = MockConfigEntry(
        domain="energy_compass",
        data=config,
        options={"controller": {"enabled": True, "device_id": device.id}},
        title="Synthetic",
        version=3,
    )
    entry.add_to_hass(hass)
    with patch.object(EnergyCompassCoordinator, "async_recalculate", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entry.runtime_data.async_set_updated_data(ready_publication(old))
    ids = package_states(hass, **package_of(old))
    before = {entity_id: hass.states.get(entity_id) for entity_id in ids.values()}
    answer = await hass.services.async_call(
        "energy_compass",
        "controller_import_package",
        {"controller": SERVICE_CONTROLLER},
        blocking=True,
        return_response=True,
    )
    return answer, before


def new_harness_from(hass, old):
    """The new blueprint on the old inverter state, fed by the real controller sensor."""
    state = hass.states.get(SERVICE_CONTROLLER)
    attrs = json.loads(json.dumps(dict(state.attributes)))
    attrs["mode_entity"] = NEW_MODE
    attrs["device_id"] = "test_solarman_device"
    h = NewHarness()
    h.now = old.now
    for entity, value in old.data.items():
        if entity.startswith(("number.", "select.", "time.", "sensor.inverter")):
            h.data[entity] = copy.deepcopy(value)
    for value in h.data.values():
        value["last_reported"] = h.now.isoformat()
    h.fixed_controller = (state.state, attrs)
    h.session = attrs["session"]
    h.set(NEW_MODE, hass.states.get("select.synthetic_deye_mode").state)
    runtime = hass.states.get("sensor.synthetic_deye_controller_runtime")
    h.set(
        NEW_RT,
        runtime.state,
        runtime=json.loads(json.dumps(runtime.attributes["runtime"])),
    )
    h.restore_pending = attrs["restore_pending"]
    return h


def new_verdict(h):
    decision = h.decision()
    h.ctx["decision"] = decision
    cleanup = h.render(h.expression("cleanup"))
    return decision, cleanup


def differences(old, new):
    return {key for key in old.keys() & new.keys() if old[key] != new[key]}


async def test_the_swap_changes_nothing_on_the_inverter(hass, freezer):
    old = running_session()
    old_decision, old_cleanup = old_verdict(old)
    assert old_decision["valid"] and not old_cleanup
    answer, _ = await imported_controller(hass, freezer, old)

    assert answer["session"] == oracle_timestamp(
        old.data[SESSION_START]["attributes"]["timestamp"]
    )
    assert answer["accepted_generation"] == old.data[CACHE]["state"]
    assert answer["restore_pending"] is True and answer["mode"] == "Auto"
    assert answer["snapshot_kept"] is True
    assert set(answer["dropped_keys"]) == set(STALE)

    new = new_harness_from(hass, old)
    new_decision, new_cleanup = new_verdict(new)
    assert new_decision["valid"], new_decision["reason"]
    assert not new_cleanup
    assert differences(old_decision, new_decision) == set()
    assert new_decision["desired"] == old_decision["desired"]
    assert len(new_decision["desired"]) == 27

    new.ctx["target"] = new_decision
    new.ctx["cleanup"] = False
    commands = new.render(new.expression("commands"))
    assert commands == []

    runner = NewRunner(new)
    runner.run()
    assert runner.writes == [] and runner.reads == []
    runtime = new.data[NEW_RT]["attributes"]["runtime"]
    assert runtime["code"] == "ok" and runtime["confirmed"] == old_decision["desired"]
    assert runtime["owned_session"] == new.session
    assert not set(STALE) & set(runtime)
    assert new.restore_pending is True


@pytest.mark.parametrize("state", ["CHARGE_PV", "SELF_CONSUME", "HOLD"])
async def test_every_planned_state_swaps_without_writes(hass, freezer, state):
    row = {"end_soc_kwh": 12, "charge_kwh": 0, "discharge_kwh": 0, "pv_kwh": 0}
    old = running_session(state, **(row if state == "HOLD" else {}))
    await imported_controller(hass, freezer, old)
    new = new_harness_from(hass, old)
    runner = NewRunner(new)
    runner.run()
    assert runner.writes == [] and runner.reads == []
    assert new.decision()["desired"] == old.decision()["desired"]


async def test_capacity_24_changes_only_the_active_threshold_in_a_hold_row(
    hass, freezer
):
    hold = {"end_soc_kwh": 12, "charge_kwh": 0, "discharge_kwh": 0, "pv_kwh": 0}
    old = running_session("HOLD", **hold)
    old_decision, _ = old_verdict(old)
    await imported_controller(hass, freezer, old, capacity=24.0)
    new = new_harness_from(hass, old)
    new_decision, new_cleanup = new_verdict(new)
    assert new_decision["valid"] and not new_cleanup
    changed = {
        entity
        for entity, value in new_decision["desired"].items()
        if value != old_decision["desired"][entity]
    }
    active = old_decision["active_tou"]
    assert changed == {
        f"number.inverter_deye_program_{active}_soc",
        f"number.inverter_deye_program_{active}_voltage",
    }
    assert old_decision["target_soc"] == 48 and new_decision["target_soc"] == 50


@pytest.mark.parametrize("state", ["CHARGE_PV", "SELF_CONSUME"])
async def test_capacity_24_rewrites_nothing_outside_a_soc_target(hass, freezer, state):
    old = running_session(state)
    await imported_controller(hass, freezer, old, capacity=24.0)
    new = new_harness_from(hass, old)
    runner = NewRunner(new)
    runner.run()
    assert runner.writes == []


async def test_rollback_before_the_package_is_removed(hass, freezer):
    old = running_session()
    _, package_before = await imported_controller(hass, freezer, old)
    assert package_before
    for entity_id, state in package_before.items():
        assert hass.states.get(entity_id) == state
    decision, cleanup = old_verdict(old)
    assert decision["valid"] and not cleanup
    runner = Runner(old)
    runner.run()
    assert runner.writes == [] and runner.reads == []


ETA_ROWS = {
    "DISCHARGE_GRID": {
        "charge_kwh": 0,
        "discharge_kwh": 0.5,
        "pv_kwh": 0,
        "end_soc_kwh": 5,
    },
    "CHARGE_GRID": {"charge_kwh": 0.5, "pv_kwh": 0, "end_soc_kwh": 20},
}


@pytest.mark.parametrize("state", list(ETA_ROWS))
@pytest.mark.parametrize("eta", [SITE_ETA, 1.0, 0.95])
async def test_the_efficiency_now_comes_from_energy_compass_settings(
    hass, freezer, state, eta
):
    """The v0.1.35 blueprint took eta from its own input; EC settings must match it."""
    old = running_session(state, **ETA_ROWS[state])
    old_decision, _ = old_verdict(old)
    await imported_controller(hass, freezer, old, eta=eta)
    new = new_harness_from(hass, old)
    new_decision, new_cleanup = new_verdict(new)
    assert new_decision["valid"] and not new_cleanup
    currents = {new.ctx[key] for key in CURRENTS_OF_INTEREST}
    changed = {
        entity
        for entity, value in new_decision["desired"].items()
        if value != old_decision["desired"][entity]
    }
    new.ctx["target"] = new_decision
    new.ctx["cleanup"] = False
    commands = {command["entity"] for command in new.render(new.expression("commands"))}
    runner = NewRunner(new)
    runner.run()
    if eta == SITE_ETA:
        assert changed == set() and commands == set()
        assert runner.writes == [] and runner.reads == []
    else:
        assert changed <= currents
        assert commands == changed
        assert {entity for entity, _ in runner.writes} <= currents
        if state == "DISCHARGE_GRID" and eta == 1.0:
            assert changed
