"""Run the native Deye controller against the same sanitized sample as the oracle.

`deye_oracle_v0137.EnergyCompassSim` plays Energy Compass. `Harness` exposes the API
of the blueprint harnesses (`data`, `ctx`, `accept`, `decision`, `restart`, ...) over
`controller_profile.decide`, so the scenario tests can run against either engine.
"""

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

from custom_components.energy_compass import controller_profile as profile
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


def snapshot_of(sim, ctx, runtime) -> profile.Snapshot:
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
        "reason": texts.reason_text(d.reason_code, language, d.reason_args),
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
        "warning": texts.warning_text(d.warning_code, language),
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
        return snapshot_of(self, self.ctx, self.stored_runtime())

    def limits(self):
        return limits_of(self.ctx)

    def decision(self, base=False, ceiling=None):
        return decision_dict(
            profile.decide(self.snapshot(), self.limits(), ceiling, base=base)
        )
