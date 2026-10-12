"""Run the native Deye controller against the same sanitized sample as the oracle.

`deye_oracle_v0137.EnergyCompassSim` plays Energy Compass. `Harness` exposes the API
of the blueprint harnesses (`data`, `ctx`, `accept`, `decision`, `restart`, ...) over
`controller_profile.decide`, so the scenario tests can run against either engine.
"""

import asyncio
import copy
from types import MappingProxyType

from deye_oracle import timestamp
from deye_oracle_v0137 import (
    CONTROLLER,
    INPUTS,
    MODE,
    PREFIX,
    RT,
    EnergyCompassSim,
)

from custom_components.energy_compass import controller as core
from custom_components.energy_compass import controller_profile as profile
from custom_components.energy_compass import controller_run as run_module
from custom_components.energy_compass import controller_texts as texts

LANGUAGE = "pl"
DEFAULTS = {
    "commissioned_battery_modes": ["Capacity"],
    "discharge_energy_entity": "",
    "voltage_grid_charge_ceiling": 55.2,
    "hold_grid_current": 1,
    "reached_discharge_current": 1,
    "balance_grid_current": 2,
    "max_power_w": 8000,
    "max_current": 18,
    "max_grid_current": 16,
    "relinquish_current": 18,
    "old_writers": [],
}


def snapshot_of(sim, ctx, runtime, paused=False) -> profile.Snapshot:
    """The profile's view of the sample at this instant; later edits do not reach it."""
    sim.sync()
    states = {
        entity: profile.Fact(
            x.get("state", "unknown"),
            MappingProxyType(dict(x.get("attributes", {}))),
            timestamp(x.get("last_reported"), 0),
        )
        for entity, x in sim.data.items()
    }
    view = states.get(CONTROLLER)
    controller = (
        None
        if view is None or view.state == "unavailable"
        else dict(sim.data[CONTROLLER]["attributes"])
    )
    return profile.Snapshot(
        now=sim.now,
        mode=sim.data[MODE]["state"],
        entities=entities_of(ctx, sim.prefix),
        states=states,
        controller=controller,
        runtime=dict(runtime),
        paused=paused,
    )


def entities_of(ctx, prefix) -> profile.Entities:
    return profile.Entities(
        charge=ctx["charge_entity"],
        discharge=ctx["discharge_entity"],
        grid=ctx["grid_entity"],
        operation=ctx["operation_entity"],
        soc=ctx["soc_entity"],
        voltage=ctx["voltage_entity"],
        prefix=prefix,
    )


def limits_of(ctx) -> profile.Limits:
    return profile.Limits(
        max_power_w=ctx["max_power_w"],
        max_current=ctx["max_current"],
        max_grid_current=ctx["max_grid_current"],
        relinquish_current=ctx["relinquish_current"],
        hold_grid_current=ctx["hold_grid_current"],
        reached_discharge_current=ctx["reached_discharge_current"],
        balance_grid_current=ctx["balance_grid_current"],
        voltage_grid_charge_ceiling=ctx["voltage_grid_charge_ceiling"],
        commissioned_battery_modes=tuple(ctx["commissioned_battery_modes"]),
        old_writers=tuple(ctx["old_writers"]),
        telemetry_entities=tuple(ctx["telemetry_entities"]),
        discharge_energy_entity=ctx["discharge_energy_entity"],
    )


def decision_dict(d: profile.Decision, language=LANGUAGE) -> dict:
    """The blueprint's decision dict (`build.py:474-481`) with the reason as text."""
    return {
        "valid": d.valid,
        "reason": texts.text(d.reason_code, language, d.reason_args),
        "state": d.state,
        "desired": d.desired,
        "telemetry": d.telemetry,
        "writers_safe": d.writers_safe,
        "settings_ok": d.settings_ok,
        "active_tou": d.active_tou,
        "times": d.times,
        "retained": d.retained,
        "generation": d.generation,
        "deadline": d.deadline,
        "row_start": d.row_start,
        "row_end": d.row_end,
        "operation": d.operation,
        "requested_mode": d.requested_mode,
        "commissioned": d.commissioned,
        "target_reached": d.target_reached,
        "reached_key": d.reached_key,
        "voltage": d.voltage,
        "soc": d.soc,
        "target_voltage": d.target_voltage,
        "target_soc": d.target_soc,
        "energy_mode": d.energy_mode,
        "discharged_kwh": d.discharged_kwh,
        "slot_energy": d.slot_energy,
        "warning": texts.text(d.warning_code, language),
    }


class Harness(EnergyCompassSim):
    def __init__(self, inputs=INPUTS):
        values = DEFAULTS | inputs
        super().__init__(values["old_writers"])
        self.ctx = {
            **values,
            "mode_entity": MODE,
            "device_id": "test_solarman_device",
        }
        self.paused = False
        self.sync()
        self.ctx["program_prefix"] = self.prefix
        self.ctx["registers"] = self.register_dicts()

    def register_dicts(self):
        registers = profile.register_map(entities_of(self.ctx, PREFIX))
        return {
            entity: {"address": r.address, "scale": r.scale}
            for entity, r in registers.items()
        }

    def stored_runtime(self):
        return self.data[RT]["attributes"].get("runtime", {})

    def snapshot(self):
        return snapshot_of(self, self.ctx, self.stored_runtime(), self.paused)

    def limits(self):
        return limits_of(self.ctx)

    def decision(self, base=False, ceiling=None):
        return decision_dict(
            profile.decide(self.snapshot(), self.limits(), ceiling, base=base)
        )


ControllerGone = run_module.ControllerUnavailable
# The blueprint's own action shapes, so hooks written for it fire at the same points.
STAGE_ACTIONS = {
    "target": {"variables": {"target": "", "transaction_attempt": "", "replan": False}},
    "target_base": {"variables": {"target": ""}},
    "write_fresh": {"variables": {"fresh": "", "baseline": "", "allowed": ""}},
    "write_recheck": {"variables": {"fresh": "", "baseline": "", "allowed": ""}},
    "fresh": {"variables": {"fresh": "", "baseline": "", "profile_confirmed": ""}},
}
REPEAT_TRANSACTIONS = {"repeat": {"count": 3, "sequence": []}}


class SimPort:
    """The executor's port over the sample: services act on `Harness` state."""

    def __init__(self, runner):
        self.runner = runner
        self.h = runner.h
        self.targets = 0

    def now(self):
        return self.h.now

    def snapshot(self, stage):
        runner = self.runner
        if runner.before_action:
            if stage == "start":
                self.targets = 0
            if stage == "target":
                self.targets += 1
                if self.targets == 1:
                    runner.before_action(runner, REPEAT_TRANSACTIONS)
            if stage in STAGE_ACTIONS:
                runner.before_action(runner, STAGE_ACTIONS[stage])
        return self.h.snapshot()

    async def persist(self, runtime=None, restore_pending=None, *, durable=False):
        h = self.h
        if h.controller_missing:
            raise ControllerGone("persist")
        self.runner.runtime_calls.append(
            {"runtime": runtime, "restore_pending": restore_pending}
        )
        store = h.data[RT]["attributes"]
        if runtime is not None:
            store["runtime"] = core.validate_runtime(runtime)
            h.data[RT]["state"] = store["runtime"].get("code", "waiting")
        if restore_pending is not None:
            assert isinstance(restore_pending, bool)
            h.restore_pending = restore_pending
        return copy.deepcopy(store.get("runtime", {})), h.restore_pending

    async def select_mode(self, mode):
        self.h.data[MODE]["state"] = mode

    async def write(self, command):
        runner, h = self.runner, self.h
        entity, value = command.entity, command.value
        runner.writes.append((entity, value))
        h.data[entity]["state"] = str(value)
        register = h.ctx["registers"][entity]
        if entity not in runner.drop:
            runner.raw[register["address"]] = (
                {"Disabled": 0, "Grid": 1, "Sell": 32}[value]
                if entity.startswith("select.")
                else int(float(value) / register["scale"])
            )
        if runner.hook:
            runner.hook(runner, entity, value)

    async def read_registers(self, address, count):
        self.runner.reads.append(address)
        return {
            a: raw
            for a, raw in self.runner.raw.items()
            if address <= a < address + count
        }


class Runner:
    """Same API as the blueprint harness' runner, over `DeyeExecutor`."""

    def __init__(self, h, probe_layout=False):
        self.h = h
        self.ctx = {"trigger": {"id": "change"}}
        self.writes = []
        self.reads = []
        self.raw = {}
        self.drop = set()
        self.hook = None
        self.before_action = None
        self.delays = []
        self.runtime_calls = []
        self.outcomes = []
        for entity, reg in h.ctx["registers"].items():
            value = h.data[entity]["state"]
            self.raw[reg["address"]] = (
                {"Disabled": 0, "Grid": 1, "Sell": 32}.get(value, 0)
                if entity.startswith("select.")
                else round(float(value) / reg["scale"])
            )
        self.executor = run_module.DeyeExecutor(
            SimPort(self), h.limits, language=LANGUAGE, probe_layout=probe_layout
        )

    def run(self):
        # Energy Compass processes every publication before the controller runs.
        self.h.accept()
        self.outcomes.append(asyncio.run(self.executor.run()))
