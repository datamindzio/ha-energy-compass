"""The native Deye controller against the frozen v0.1.37 blueprint.

Decision parity runs a seeded corpus of plan, telemetry, runtime and settings
variations through the blueprint's DECISION and BASE_DECISION templates (the oracle)
and through `controller_profile.decide`, and requires every field of the decision
dict to be equal. Reasons are compared as the Polish text of the native reason code.
"""

import random

import pytest
from deye_native_harness import (
    decision_dict,
    limits_of,
    snapshot_of,
)
from deye_oracle import States
from deye_oracle_v0137 import (
    CACHE,
    INPUTS,
    MODE,
    RT,
    A,
    EnergyCompassSim,
    Harness,
    O,
    P,
)

from custom_components.energy_compass import controller as core
from custom_components.energy_compass import controller_profile as profile

COUNTER = "sensor.inverter_deye_total_battery_discharge"
ROW_STATES = [
    "CHARGE_GRID",
    "CHARGE_PV",
    "DISCHARGE_GRID",
    "SELF_CONSUME",
    "HOLD",
    "CURTAIL",
]
KWH = [0, 0.0001, 0.05, 0.3, 0.5, 2, 100]
END_SOC = [0, 1.3, 2.5, 5, 12, 12.5, 20, 21.512, 24.75, 25, 30]
SOC = [
    "10",
    "27",
    "48",
    "80",
    "98",
    "99",
    "99.01",
    "99.9",
    "100",
    "100",
    "20",
    "5",
    "unknown",
]
VOLTAGE = [
    "400",
    "496",
    "512.2",
    "520",
    "531.3",
    "533.4",
    "537.1",
    "537.1",
    "552",
    "610",
    "611",
    "unknown",
]
HELD = ["0", "1", "14", "15", "18", "unknown"]
TELEMETRY = [
    "sensor.inverter_deye_battery",
    "sensor.inverter_deye_battery_voltage",
    "sensor.inverter_deye_update_interval",
]
_ORACLE = []


def oracle():
    """One shared oracle: compiled templates are the slow part, the sample is reset."""
    if not _ORACLE:
        _ORACLE.append(Harness(INPUTS | {"discharge_energy_entity": COUNTER}))
    h = _ORACLE[0]
    EnergyCompassSim.__init__(h, h.ctx["old_writers"])
    h.env.globals["states"] = States(h.data)
    h.set(COUNTER, "5600.0")
    return h


def render(h, name, **extra):
    h.ctx["runtime"] = h.data[RT]["attributes"].get("runtime", {})
    h.ctx["restore_pending"] = h.restore_pending
    return h.render(h.expression(name), **extra)


def scenario(h, rng: random.Random):
    """Vary one sample in place."""
    row = h.data[P]["attributes"]["intervals"][0]
    row.update(
        state=rng.choice(ROW_STATES),
        charge_kwh=rng.choice(KWH),
        discharge_kwh=rng.choice(KWH),
        pv_kwh=rng.choice(KWH),
        load_kwh=rng.choice(KWH),
        curtail_kwh=rng.choice([0, 0, 0.05, 0.3]),
        end_soc_kwh=rng.choice(END_SOC),
        balance_hold=rng.random() < 0.15,
    )
    h.data["sensor.inverter_deye_battery"]["state"] = rng.choice(SOC)
    h.data["sensor.inverter_deye_battery_voltage"]["state"] = rng.choice(VOLTAGE)
    h.data[h.ctx["operation_entity"]]["state"] = rng.choice(
        ["Capacity", "Capacity", "Voltage", "Voltage", "Voltage", "No Battery"]
    )
    h.data[MODE]["state"] = rng.choice(["Auto", "Auto", "Auto", "Off", "Simulation"])
    for key, options in {
        "commissioned_battery_modes": [
            ["Capacity", "Voltage"],
            ["Capacity", "Voltage"],
            ["Capacity"],
        ],
        "max_current": [18, 5, 350],
        "max_power_w": [8000, 1000, 30000],
        "hold_grid_current": [1, 3],
        "reached_discharge_current": [0, 1, 50],
        "balance_grid_current": [0, 2, 5],
        "voltage_grid_charge_ceiling": [55.2, 57, 49, 54.1],
        "relinquish_current": [18, 0, 100],
        "max_grid_current": [16, 3],
        "discharge_energy_entity": [COUNTER, COUNTER, ""],
    }.items():
        h.ctx[key] = rng.choice(options)
    h.set(COUNTER, rng.choice(["5600.0", "5601.9", "5601.96", "unavailable", "5599"]))
    for entity in (h.ctx["charge_entity"], h.ctx["discharge_entity"]):
        h.data[entity]["state"] = rng.choice(HELD)
    if rng.random() < 0.08:
        h.data[rng.choice(TELEMETRY)]["last_reported"] = (
            h.now.replace(second=0).isoformat()
            if rng.random() < 0.5
            else "2026-09-19T09:00:00+00:00"
        )
    if rng.random() < 0.1:
        h.data[h.ctx["old_writers"][0]]["state"] = "on"
    if rng.random() < 0.1:
        h.data[h.ctx["old_writers"][1]]["attributes"]["current"] = 1
    if rng.random() < 0.08:
        h.data[A]["state"] = "on"
    if rng.random() < 0.08:
        h.data[O]["state"] = "error"
    if rng.random() < 0.08:
        h.battery[rng.choice(["capacity_kwh", "eta_charge", "eta_discharge"])] = (
            rng.choice([0, 1.2, -1])
        )
    if rng.random() < 0.08:
        e = f"time.inverter_deye_program_{rng.randint(1, 6)}_time"
        h.data[e]["state"] = rng.choice(["unknown", "25:00:00", "10:00", "03:00:00"])
    if rng.random() < 0.1:
        e = rng.choice(
            [
                "number.inverter_deye_program_3_soc",
                "number.inverter_deye_program_3_voltage",
                "number.inverter_deye_program_3_power",
                h.ctx["grid_entity"],
            ]
        )
        attributes = h.data[e]["attributes"]
        change = rng.choice(["step", "min", "max", "max_low"])
        if change == "max_low":
            attributes["max"] = 5
        else:
            attributes.pop(change, None)
    if rng.random() < 0.1:
        h.data["select.inverter_deye_program_3_charging"]["attributes"]["options"] = [
            "Disabled"
        ]
    shift = rng.choice([0, 0, 0, 3, 14, 31, 70])
    if shift:
        h.now = h.now.replace(minute=(h.now.minute + shift) % 60)
        for x in h.data.values():
            x["last_reported"] = h.now.isoformat()
    if rng.random() < 0.05:
        h.now = h.now.replace(hour=rng.randint(0, 23))
        for x in h.data.values():
            x["last_reported"] = h.now.isoformat()
    h.accept()
    if rng.random() < 0.1 and h.data[CACHE]["attributes"].get("snapshot"):
        h.revoked = core.Revocation(
            h.data[CACHE]["attributes"]["snapshot"]["generated_at"],
            h.now.isoformat(),
            "error: x",
        )
    if rng.random() < 0.08:
        h.session += 60


def runtime_variant(h, rng, first):
    """Optionally seed the stored runtime with a latch or the previous decision."""
    choice = rng.choice(["none", "none", "latch", "hold", "slot", "slot_old"])
    runtime = {}
    if choice == "latch":
        runtime = {"reached_key": first["reached_key"]}
    elif choice == "hold":
        runtime = {
            "state": rng.choice(["HOLD", "CHARGE_PV"]),
            "desired": first["desired"],
        }
    elif choice == "slot":
        runtime = {"slot_energy": {"key": first["reached_key"], "discharge": 5590.0}}
    elif choice == "slot_old":
        runtime = {"slot_energy": {"key": ["old"], "discharge": 5590.0}}
    h.data[RT]["attributes"]["runtime"] = runtime


def native(h, *, base=False, ceiling=None):
    runtime = h.data[RT]["attributes"].get("runtime", {})
    return decision_dict(
        profile.decide(
            snapshot_of(h, h.ctx, runtime), limits_of(h.ctx), ceiling, base=base
        )
    )


def ceilings(h, rng):
    if rng.random() < 0.5:
        return None
    return {
        h.ctx[k]: rng.choice([0, 1, 13, 14, 15, 99])
        for k in ("charge_entity", "discharge_entity", "grid_entity")
    }


@pytest.mark.parametrize("seed", range(60))
def test_decision_parity_on_the_scenario_corpus(seed):
    rng = random.Random(seed)
    for case in range(25):
        h = oracle()
        scenario(h, rng)
        runtime_variant(h, rng, native(h))
        ceiling = ceilings(h, rng)
        extra = {} if ceiling is None else {"current_ceiling": ceiling}
        want = render(h, "decision", **extra)
        got = native(h, ceiling=ceiling)
        assert got == want, (seed, case)
        want_base = render(h, "baseline", **extra)
        assert native(h, base=True, ceiling=ceiling) == want_base, (seed, case)


def test_corpus_covers_every_state_reason_and_branch():
    rng = random.Random(7)
    seen = {"valid": 0, "reached": 0, "energy": 0, "full": 0, "balance": 0}
    reasons, valid_states, warnings = set(), set(), set()
    for _ in range(1500):
        h = oracle()
        scenario(h, rng)
        d = native(h)
        reasons.add(d["reason"].split(":")[0])
        warnings.add(d["warning"])
        seen["valid"] += d["valid"]
        seen["reached"] += d["target_reached"]
        seen["energy"] += d["energy_mode"]
        if d["valid"]:
            valid_states.add(d["state"])
            row = h.data[P]["attributes"]["intervals"][0]
            seen["balance"] += bool(row.get("balance_hold")) and d["state"].startswith(
                "CHARGE"
            )
            seen["full"] += d["soc"] >= 100
    assert valid_states >= set(ROW_STATES)
    assert {"ok", "telemetria", "plan", "nastawy", "bateria", "napięcie"} <= reasons
    assert all(count >= 20 for count in seen.values()), seen
    assert warnings == {"", "CURTAIL nieobsługiwany: zastosowano HOLD"}
