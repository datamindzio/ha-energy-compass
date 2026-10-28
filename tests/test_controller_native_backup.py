"""BACKUP: while the grid is out the battery powers the house at the inverter's full caps.

The state ignores the plan row: both currents sit at their caps, grid charging is off, every
TOU program is Disabled at the base targets. It acts only in mode Auto; Simulation computes
it without writing and Off hands the inverter back with a single BASE restore. The frozen
blueprint knows nothing of it; only the native engine is held to this.
"""

from dataclasses import replace

import pytest
from deye_native_harness import Harness, Runner
from deye_oracle import P
from deye_oracle_v0137 import INPUTS

from custom_components.energy_compass import controller_profile as profile

CAPS = INPUTS | {"commissioned_battery_modes": ["Capacity", "Voltage"]}


def _site(state="CHARGE_GRID", *, outage=True, operation="Capacity", **row):
    h = Harness(CAPS)
    h.data[h.ctx["operation_entity"]]["state"] = operation
    h.data[P]["attributes"]["intervals"][0].update(
        {
            "state": state,
            "charge_kwh": 0.4,
            "discharge_kwh": 0,
            "end_soc_kwh": 20,
            "pv_kwh": 0,
            "load_kwh": 0.05,
            **row,
        }
    )
    h.accept()
    h.grid_outage = outage
    return h


def _selects(h, decision):
    return {
        entity: value
        for entity, value in decision.desired.items()
        if entity.startswith("select.")
    }


def _caps(h):
    """The caps the profile uses for a forcing state, outage or not."""
    plain = _site("SELF_CONSUME", outage=False)
    decision = profile.decide(plain.snapshot(), plain.limits())
    return (
        decision.desired[plain.ctx["charge_entity"]],
        decision.desired[plain.ctx["discharge_entity"]],
    )


@pytest.mark.parametrize(
    "state", ["CHARGE_PV", "SELF_CONSUME", "CHARGE_GRID", "DISCHARGE_GRID", "HOLD"]
)
@pytest.mark.parametrize("operation", ["Capacity", "Voltage"])
def test_an_outage_decides_backup_whatever_the_plan_says(state, operation):
    h = _site(state, operation=operation)
    decision = profile.decide(h.snapshot(), h.limits())
    assert decision.valid, decision.reason_code
    assert decision.state == "BACKUP"
    assert decision.reason_code == "grid_outage"
    assert decision.grid_outage


def test_backup_values_are_the_caps_with_everything_else_off():
    h = _site()
    decision = profile.decide(h.snapshot(), h.limits())
    charge_cap, discharge_cap = _caps(h)
    assert charge_cap > 1 and discharge_cap > 1
    desired = decision.desired
    assert desired[h.ctx["charge_entity"]] == charge_cap
    assert desired[h.ctx["discharge_entity"]] == discharge_cap
    assert desired[h.ctx["grid_entity"]] == 0
    assert set(_selects(h, decision).values()) == {"Disabled"}
    assert len(_selects(h, decision)) == 6
    for entity, value in desired.items():
        if entity.endswith("_soc"):
            assert value == profile.BASE_SOC
        if entity.endswith("_voltage") and entity.startswith("number."):
            assert value == profile.BASE_VOLTAGE
        if entity.endswith("_power"):
            assert value == h.limits().max_power_w
    assert not decision.target_reached


def test_backup_needs_no_plan():
    h = Harness(CAPS)
    h.grid_outage = True
    decision = profile.decide(h.snapshot(), h.limits())
    assert decision.state == "BACKUP" and decision.valid


@pytest.mark.parametrize("mode", ["Auto", "Simulation"])
def test_backup_is_computed_in_auto_and_simulation(mode):
    h = _site()
    h.data[h.ctx["mode_entity"]]["state"] = mode
    decision = profile.decide(h.snapshot(), h.limits())
    assert decision.state == "BACKUP" and decision.valid


def test_off_is_hands_off():
    h = _site()
    h.data[h.ctx["mode_entity"]]["state"] = "Off"
    decision = profile.decide(h.snapshot(), h.limits())
    assert decision.state == "BASE"
    assert decision.desired[h.ctx["charge_entity"]] == h.limits().relinquish_current


def test_the_base_decision_stays_base_during_an_outage():
    h = _site()
    decision = profile.decide(h.snapshot(), h.limits(), base=True)
    assert decision.state == "BASE"
    relinquish = h.limits().relinquish_current
    assert decision.desired[h.ctx["charge_entity"]] == relinquish


def test_stale_telemetry_islanded_falls_back_to_base():
    h = _site()
    h.data[h.ctx["soc_entity"]]["state"] = "unavailable"
    decision = profile.decide(h.snapshot(), h.limits())
    assert not decision.valid
    assert decision.state == "BASE"
    assert decision.reason_code == "telemetry_stale"


def test_an_unusable_operation_mode_falls_back_to_base():
    h = _site()
    h.data[h.ctx["operation_entity"]]["state"] = "Backup"
    decision = profile.decide(h.snapshot(), h.limits())
    assert not decision.valid and decision.state == "BASE"


def test_unsound_tou_times_fall_back_to_base():
    h = _site()
    for number in range(1, 7):
        h.data[f"time.{h.prefix}{number}_time"]["state"] = "00:00:00"
    decision = profile.decide(h.snapshot(), h.limits())
    assert not decision.valid and decision.state == "BASE"
    assert decision.reason_code == "tou_times"


def test_an_uncommissioned_battery_mode_falls_back_to_base():
    h = _site(operation="Voltage")
    limits = replace(h.limits(), commissioned_battery_modes=("Capacity",))
    decision = profile.decide(h.snapshot(), limits)
    assert not decision.valid and decision.state == "BASE"
    assert decision.reason_code == "mode_not_commissioned"


def test_without_an_outage_nothing_changes():
    h = _site("SELF_CONSUME", outage=False)
    decision = profile.decide(h.snapshot(), h.limits())
    assert decision.state == "SELF_CONSUME"
    assert not decision.grid_outage


# --- the write plan ------------------------------------------------------------------


def _running():
    """A site that ran a CHARGE_GRID row on the grid: the inverter holds that profile."""
    h = _site("CHARGE_GRID", outage=False, charge_kwh=0.5)
    Runner(h).run()
    h.grid_outage = True
    return h


def _plan(h):
    snapshot = h.snapshot()
    target = profile.decide(snapshot, h.limits())
    return snapshot, target, profile.diff(snapshot, target)


def test_the_plan_uses_the_backup_phase_and_never_stops_a_current():
    h = _running()
    _, target, commands = _plan(h)
    assert target.state == "BACKUP"
    assert commands
    assert {c.phase for c in commands} == {"backup"}
    for command in commands:
        if command.entity in (h.ctx["charge_entity"], h.ctx["discharge_entity"]):
            assert command.value > 0


def test_the_order_is_discharge_charge_grid_selects_then_tou():
    h = _running()
    _, _, commands = _plan(h)
    entities = [c.entity for c in commands]
    discharge, charge, grid = (
        h.ctx["discharge_entity"],
        h.ctx["charge_entity"],
        h.ctx["grid_entity"],
    )
    rank = {discharge: 0, charge: 1, grid: 2}

    def key(entity):
        if entity in rank:
            return rank[entity]
        return 3 if entity.startswith("select.") else 4

    assert [key(e) for e in entities] == sorted(key(e) for e in entities)
    # The running CHARGE_GRID profile has a grid current and an enabled program.
    assert grid in entities
    assert any(e.startswith("select.") for e in entities)


def test_only_what_differs_is_written():
    h = _running()
    _, target, _ = _plan(h)
    for entity, value in target.desired.items():
        h.data[entity]["state"] = str(value)
    assert profile.diff(replace(h.snapshot(), grid_outage=True), target) == []


def test_a_dirty_entity_is_rewritten():
    h = _running()
    _, target, _ = _plan(h)
    for entity, value in target.desired.items():
        h.data[entity]["state"] = str(value)
    dirty = h.ctx["charge_entity"]
    runtime = {**h.stored_runtime(), "uncertain": [dirty]}
    snapshot = replace(h.snapshot(), runtime=runtime, grid_outage=True)
    commands = profile.diff(snapshot, target)
    assert [c.entity for c in commands] == [dirty]
    assert commands[0].phase == "backup"


# --- the guard -----------------------------------------------------------------------


def _guard(h, command, *, snapshot=None, fresh=None, target=None):
    snapshot = snapshot or h.snapshot()
    target = target or profile.decide(snapshot, h.limits())
    fresh = fresh or profile.decide(snapshot, h.limits())
    baseline = profile.decide(snapshot, h.limits(), base=True)
    return profile.guard(
        snapshot,
        command,
        target=target,
        fresh=fresh,
        baseline=baseline,
        cleanup=False,
        failed=False,
    )


def _first(h):
    _, _, commands = _plan(h)
    return commands[0]


def test_the_guard_allows_a_backup_write_in_auto():
    h = _running()
    assert _guard(h, _first(h))


def test_the_guard_blocks_while_paused():
    h = _running()
    h.paused = True
    assert not _guard(h, _first(h))


def test_the_guard_blocks_in_simulation():
    h = _running()
    command = _first(h)
    h.data[h.ctx["mode_entity"]]["state"] = "Simulation"
    assert not _guard(h, command)


def test_the_guard_blocks_when_the_outage_has_ended():
    h = _running()
    command = _first(h)
    target = profile.decide(h.snapshot(), h.limits())
    h.grid_outage = False
    assert not _guard(h, command, target=target)


def test_the_guard_blocks_with_an_old_writer_on():
    h = _running()
    h.blueprint_active = ("automation.deye",)
    assert not _guard(h, _first(h))


def test_the_guard_blocks_once_the_mode_is_off():
    h = _running()
    command = _first(h)
    target = profile.decide(h.snapshot(), h.limits())
    h.data[h.ctx["mode_entity"]]["state"] = "Off"
    assert not _guard(h, command, target=target)


# --- the executor --------------------------------------------------------------------


def test_the_run_reaches_backup_without_ever_writing_zero_to_a_battery_limit():
    h = _running()
    runner = Runner(h)
    runner.writes.clear()
    runner.run()
    assert runner.outcomes[-1] == "run_finished"
    assert runner.writes
    battery = (h.ctx["charge_entity"], h.ctx["discharge_entity"])
    assert all(value != 0 for entity, value in runner.writes if entity in battery)
    runtime = h.stored_runtime()
    assert runtime["state"] == "BACKUP"
    assert runtime["reason_code"] == "grid_outage"
    assert runtime["code"] == "ok"
    assert runtime["confirmed_mode"] == "BACKUP"


def test_a_second_run_writes_nothing():
    h = _running()
    runner = Runner(h)
    runner.run()
    runner.writes.clear()
    runner.run()
    assert runner.writes == []


def test_simulation_shows_backup_and_writes_nothing():
    h = _running()
    h.data[h.ctx["mode_entity"]]["state"] = "Simulation"
    h.restore_pending = False
    runner = Runner(h)
    runner.writes.clear()
    runner.run()
    assert runner.outcomes[-1] == "simulation"
    assert runner.writes == []
    assert h.stored_runtime()["state"] == "BACKUP"


def test_off_during_an_outage_restores_the_base_once_without_a_current_dip():
    h = _running()
    runner = Runner(h)
    runner.run()
    h.data[h.ctx["mode_entity"]]["state"] = "Off"
    h.restore_pending = True
    runner.writes.clear()
    runner.run()
    battery = (h.ctx["charge_entity"], h.ctx["discharge_entity"])
    relinquish = h.limits().relinquish_current
    assert runner.writes
    assert all(
        value == relinquish for entity, value in runner.writes if entity in battery
    )


def test_the_outage_ending_restores_the_plan_state():
    h = _running()
    runner = Runner(h)
    runner.run()
    assert h.stored_runtime()["state"] == "BACKUP"
    h.grid_outage = False
    runner.run()
    assert h.stored_runtime()["state"] == "CHARGE_GRID"
