"""The native Deye controller against the frozen v0.1.37 blueprint.

Decision parity runs a seeded corpus of plan, telemetry, runtime and settings
variations through the blueprint's DECISION and BASE_DECISION templates (the oracle)
and through `controller_profile.decide`, and requires every field of the decision
dict to be equal. Reasons are compared as the Polish text of the native reason code.

One deliberate divergence: a plan row with missing or non-numeric values where the
blueprint's templates raise (`|float` or `as_timestamp` without a default) is an
invalid decision (`plan_malformed`) natively. The corpus never produces such a row,
so nothing is excluded from the comparison; `test_controller_run.py` pins the rule.
"""

import itertools
import random

import deye_engines
import pytest
import test_deye_controller as scenarios
from deye_native_harness import (
    decision_dict,
    entities_of,
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
    h.focus_runtime = None
    return h


def render(h, name, **extra):
    h.ctx["runtime"] = h.data[RT]["attributes"].get("runtime", {})
    h.ctx["restore_pending"] = h.restore_pending
    return h.render(h.expression(name), **extra)


LIMIT_OPTIONS = {
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
}


def limit_choices(rng):
    return {key: rng.choice(options) for key, options in LIMIT_OPTIONS.items()}


FOCI = [
    "valid",
    "valid",
    "charge_grid_voltage_reached",
    "charge_grid_voltage_open",
    "discharge_energy_reached",
    "discharge_energy_open",
    "latch",
    "full_held",
    "balance",
]


def focus_sample(h, rng, focus, apply_limits):
    """Steer a sample onto a branch that uniform draws almost never reach.

    Everything the branch needs is set, so the sample is a valid plan under an
    approved mode; the remaining random draws of `scenario` are skipped.
    """
    row = h.data[P]["attributes"]["intervals"][0]
    h.data[MODE]["state"] = "Auto"
    h.data["sensor.inverter_deye_battery"]["state"] = rng.choice(
        ["27", "48", "80", "100"]
    )
    h.data["sensor.inverter_deye_battery_voltage"]["state"] = rng.choice(
        ["512.2", "520", "531.3"]
    )
    h.data[h.ctx["operation_entity"]]["state"] = rng.choice(["Capacity", "Voltage"])
    row.update(balance_hold=False, pv_kwh=0, load_kwh=0, curtail_kwh=0, end_soc_kwh=20)
    if apply_limits:
        h.ctx.update(
            commissioned_battery_modes=["Capacity", "Voltage"],
            voltage_grid_charge_ceiling=rng.choice([55.2, 54.1]),
            discharge_energy_entity=COUNTER,
            max_current=18,
            max_power_w=8000,
            relinquish_current=18,
            max_grid_current=rng.choice([16, 3]),
        )
    h.focus_runtime = None
    if focus.startswith("charge_grid_voltage"):
        ceiling = h.ctx["voltage_grid_charge_ceiling"]
        reached = focus.endswith("reached")
        row.update(state="CHARGE_GRID", charge_kwh=rng.choice([0.5, 2, 100]))
        h.data[h.ctx["operation_entity"]]["state"] = "Voltage"
        h.data["sensor.inverter_deye_battery_voltage"]["state"] = (
            str(ceiling * 10 + rng.choice([0, 3, 40])) if reached else "520"
        )
    elif focus.startswith("discharge_energy"):
        reached = focus.endswith("reached")
        row.update(state="DISCHARGE_GRID", discharge_kwh=0.5 if reached else 2)
        h.data[h.ctx["operation_entity"]]["state"] = "Voltage"
        h.set(COUNTER, "5601.0" if reached else "5600.5")
        h.focus_runtime = "slot"
    elif focus == "latch":
        row.update(
            state=rng.choice(["CHARGE_GRID", "DISCHARGE_GRID"]),
            charge_kwh=2,
            discharge_kwh=2,
        )
        h.focus_runtime = "latch"
    elif focus == "full_held":
        row.update(state="HOLD")
        h.data[h.ctx["operation_entity"]]["state"] = "Voltage"
        h.data["sensor.inverter_deye_battery"]["state"] = rng.choice(["99.01", "99.9"])
        h.focus_runtime = "full_held"
    elif focus == "balance":
        row.update(
            state=rng.choice(["CHARGE_PV", "CHARGE_GRID"]),
            balance_hold=True,
            charge_kwh=rng.choice([0, 0.5]),
        )
    else:
        row.update(
            state=rng.choice(ROW_STATES),
            charge_kwh=rng.choice(KWH[1:]),
            discharge_kwh=rng.choice(KWH[1:]),
        )


def scenario(h, rng: random.Random, apply_limits=True):
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
    if apply_limits:
        h.ctx.update(limit_choices(rng))
    h.set(
        COUNTER,
        rng.choice(
            [
                "5600.0",
                "5601.9",
                "5601.945",
                "5601.95",
                "5601.96",
                "unavailable",
                "5599",
            ]
        ),
    )
    for entity in (h.ctx["charge_entity"], h.ctx["discharge_entity"]):
        h.data[entity]["state"] = rng.choice(HELD)
    focus = rng.choice(FOCI) if rng.random() < 0.5 else None
    h.focus_runtime = None
    if focus:
        focus_sample(h, rng, focus, apply_limits)

    def chance(p):
        return rng.random() < p and focus is None

    if chance(0.08):
        h.data[rng.choice(TELEMETRY)]["last_reported"] = (
            h.now.replace(second=0).isoformat()
            if rng.random() < 0.5
            else "2026-09-19T09:00:00+00:00"
        )
    if chance(0.1):
        h.data[h.ctx["old_writers"][0]]["state"] = "on"
    if chance(0.1):
        h.data[h.ctx["old_writers"][1]]["attributes"]["current"] = 1
    if chance(0.08):
        h.data[A]["state"] = "on"
    if chance(0.08):
        h.data[O]["state"] = "error"
    if chance(0.08):
        h.battery[rng.choice(["capacity_kwh", "eta_charge", "eta_discharge"])] = (
            rng.choice([0, 1.2, -1])
        )
    if chance(0.08):
        e = f"time.inverter_deye_program_{rng.randint(1, 6)}_time"
        h.data[e]["state"] = rng.choice(["unknown", "25:00:00", "10:00", "03:00:00"])
    if chance(0.1):
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
    if chance(0.1):
        h.data["select.inverter_deye_program_3_charging"]["attributes"]["options"] = [
            "Disabled"
        ]
    shift = rng.choice([0, 0, 0, 3, 14, 31, 70]) if focus is None else 0
    if shift:
        h.now = h.now.replace(minute=(h.now.minute + shift) % 60)
        for x in h.data.values():
            x["last_reported"] = h.now.isoformat()
    if chance(0.05):
        h.now = h.now.replace(hour=rng.randint(0, 23))
        for x in h.data.values():
            x["last_reported"] = h.now.isoformat()
    # The frozen 0.1.37 blueprint holds every CURTAIL row; a CURTAIL row that charges
    # (T-064) is a deliberate divergence of the native engine, pinned in
    # tests/test_controller_native_curtail.py. No rng draw: the corpus is unchanged.
    if row["state"] == "CURTAIL":
        row["charge_kwh"] = 0
    h.accept()
    if rng.random() < 0.1 and h.data[CACHE]["attributes"].get("snapshot"):
        h.revoked = core.Revocation(
            h.data[CACHE]["attributes"]["snapshot"]["generated_at"],
            h.now.isoformat(),
            "error: x",
        )
    if chance(0.08):
        h.session += 60


def runtime_variant(h, rng, first):
    """Optionally seed the stored runtime with a latch or the previous decision."""
    choice = rng.choice(["none", "none", "latch", "hold", "slot", "slot", "slot_old"])
    choice = getattr(h, "focus_runtime", None) or choice
    runtime = {}
    if choice == "latch":
        runtime = {"reached_key": first["reached_key"]}
    elif choice == "hold":
        runtime = {
            "state": rng.choice(["HOLD", "CHARGE_PV"]),
            "desired": first["desired"],
        }
    elif choice == "full_held":
        soc = h.data["sensor.inverter_deye_battery"]
        shown, soc["state"] = soc["state"], "100"
        runtime = {"state": "HOLD", "desired": native(h)["desired"]}
        soc["state"] = shown
    elif choice == "slot":
        runtime = {
            "slot_energy": {
                "key": first["reached_key"],
                "discharge": 5600.0
                if h.focus_runtime
                else rng.choice([5590.0, 5600.0, 5600.0]),
            }
        }
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


def branch_counts(cases, seed=11):
    """How often the decision corpus reaches each branch the parity claim rests on."""
    rng = random.Random(seed)
    counts = dict.fromkeys(BRANCHES, 0)
    for _ in range(cases):
        h = oracle()
        scenario(h, rng)
        runtime_variant(h, rng, native(h))
        d = native(h)
        base = native(h, base=True)
        rt = h.data[RT]["attributes"].get("runtime", {})
        row = h.data[P]["attributes"]["intervals"][0]
        if not d["valid"] or d["row_start"] != row["start"]:
            continue
        state, grid = d["state"], d["desired"][h.ctx["grid_entity"]]
        balance = bool(row.get("balance_hold")) and state in (
            "CHARGE_PV",
            "CHARGE_GRID",
        )
        latched = rt.get("reached_key") == d["reached_key"]
        grid_row = state in ("CHARGE_GRID", "DISCHARGE_GRID")
        found = {
            "charge_grid_voltage_reached": state == "CHARGE_GRID"
            and d["operation"] == "Voltage"
            and not balance
            and d["target_reached"]
            and not latched,
            "charge_grid_voltage_open": state == "CHARGE_GRID"
            and d["operation"] == "Voltage"
            and not balance
            and not d["target_reached"]
            and grid > 0,
            "discharge_energy_reached": state == "DISCHARGE_GRID"
            and d["energy_mode"]
            and d["target_reached"]
            and not latched,
            "discharge_energy_open": state == "DISCHARGE_GRID"
            and d["energy_mode"]
            and not d["target_reached"],
            "latch_hit": grid_row and not balance and latched,
            "full_held": state == "HOLD"
            and d["operation"] == "Voltage"
            and rt.get("state") == "HOLD"
            and 99 < d["soc"] < 100
            and bool(rt.get("desired"))
            and "Grid" not in rt["desired"].values()
            and d["target_voltage"] == 49.6,
            "balance_row": balance,
            "cleanup": state != "BASE" and base["state"] == "BASE" and base["valid"],
        }
        for name, hit in found.items():
            counts[name] += bool(hit)
    return counts


BRANCHES = (
    "charge_grid_voltage_reached",
    "charge_grid_voltage_open",
    "discharge_energy_reached",
    "discharge_energy_open",
    "latch_hit",
    "full_held",
    "balance_row",
    "cleanup",
)


def test_the_decision_corpus_reaches_every_branch_often():
    counts = branch_counts(1200)
    assert all(count >= 40 for count in counts.values()), counts


def decide_now(h, *, base=False, ceiling=None):
    runtime = h.data[RT]["attributes"].get("runtime", {})
    return profile.decide(
        snapshot_of(h, h.ctx, runtime), limits_of(h.ctx), ceiling, base=base
    )


def perturb(h, rng, desired):
    """Move some live entity states away from (or onto) the desired profile."""
    for entity, value in desired.items():
        roll = rng.random()
        if roll < 0.55:
            h.data[entity]["state"] = str(value)
        elif roll < 0.7:
            h.data[entity]["state"] = rng.choice(["0", "1", "unknown", "Grid", "Sell"])
        elif roll < 0.8:
            h.data[entity]["state"] = str(value)[:-1] or "0"


def command_tuples(commands):
    return [
        (c.entity, c.value, c.phase)
        if isinstance(c, profile.Command)
        else (c["entity"], c["value"], c["phase"])
        for c in commands
    ]


def random_runtime(rng, desired):
    entities = list(desired)
    runtime = {}
    if rng.random() < 0.2:
        runtime["uncertain"] = rng.sample(entities, rng.randint(1, 3))
    if rng.random() < 0.5:
        runtime["confirmed"] = {
            e: desired[e] if rng.random() < 0.8 else 0
            for e in entities
            if rng.random() < 0.9
        }
    return runtime


@pytest.mark.parametrize("seed", range(40))
def test_command_list_parity_on_the_scenario_corpus(seed):
    rng = random.Random(1000 + seed)
    produced = 0
    for case in range(25):
        h = oracle()
        scenario(h, rng)
        target = decide_now(h, base=rng.random() < 0.3)
        perturb(h, rng, target.desired)
        runtime = random_runtime(rng, target.desired)
        h.data[RT]["attributes"]["runtime"] = runtime
        want = render(h, "commands", target=decision_dict(target))
        got = profile.diff(snapshot_of(h, h.ctx, runtime), target)
        assert command_tuples(got) == command_tuples(want), (seed, case)
        produced += bool(want)
    assert produced


def mutate_after_target(h, rng):
    """A change between the target and the re-check, as the mid-run tests make."""
    kind = rng.choice(
        [
            "none",
            "none",
            "cap",
            "cap",
            "cap",
            "mode",
            "writer",
            "stale",
            "voltage",
            "soc",
            "row",
            "expiry",
            "generation",
            "operation",
            "time",
        ]
    )
    if kind == "mode":
        h.data[MODE]["state"] = rng.choice(["Off", "Simulation", "Auto"])
    elif kind == "writer":
        h.data[h.ctx["old_writers"][0]]["state"] = "on"
    elif kind == "stale":
        h.data[rng.choice(TELEMETRY)]["last_reported"] = "2026-09-19T09:00:00+00:00"
    elif kind == "voltage":
        h.data[h.ctx["voltage_entity"]]["state"] = rng.choice(["516.6", "521.1", "560"])
    elif kind == "cap":
        # A voltage step that crosses a whole ampere moves only the current ceilings: the cap-only replan case.
        entity = h.data[h.ctx["voltage_entity"]]
        if profile._is_number(entity["state"]):
            entity["state"] = str(
                float(entity["state"]) + rng.choice([-45, -30, 30, 45])
            )
    elif kind == "soc":
        h.data[h.ctx["soc_entity"]]["state"] = rng.choice(["20", "100", "50"])
    elif kind == "row":
        snapshot = h.data[CACHE]["attributes"].get("snapshot") or {}
        for row in snapshot.get("intervals", [])[:1]:
            row["state"] = rng.choice(ROW_STATES)
    elif kind == "expiry":
        snapshot = h.data[CACHE]["attributes"].get("snapshot") or {}
        if snapshot:
            snapshot["valid_until"] = h.now.isoformat()
    elif kind == "generation":
        snapshot = h.data[CACHE]["attributes"].get("snapshot") or {}
        if snapshot:
            snapshot["generated_at"] = "2026-09-19T09:41:59+00:00"
    elif kind == "operation":
        h.data[h.ctx["operation_entity"]]["state"] = rng.choice(["Voltage", "Capacity"])
    elif kind == "time":
        h.data["time.inverter_deye_program_2_time"]["state"] = "07:00:00"


@pytest.mark.parametrize("seed", range(40))
def test_guard_and_cap_only_replan_parity_on_the_scenario_corpus(seed):
    rng = random.Random(2000 + seed)
    for case in range(25):
        h = oracle()
        scenario(h, rng)
        ceiling = ceilings(h, rng)
        target = decide_now(h, ceiling=ceiling)
        runtime = random_runtime(rng, target.desired)
        h.data[RT]["attributes"]["runtime"] = runtime
        mutate_after_target(h, rng)
        fresh = decide_now(h, ceiling=ceiling)
        baseline = decide_now(h, base=True, ceiling=ceiling)
        snapshot = snapshot_of(h, h.ctx, runtime)
        command = profile.Command(
            *rng.choice(
                [
                    (e, v, rng.choice(["disable", "limits", "enable"]))
                    for e, v in target.desired.items()
                ]
            )
        )
        cleanup, failed = rng.random() < 0.3, rng.random() < 0.2
        attempt = rng.choice([1, 1, 2])
        kwargs = {
            "runtime": runtime,
            "target": decision_dict(target),
            "fresh": decision_dict(fresh),
            "baseline": decision_dict(baseline),
        }
        want = render(
            h,
            "allowed",
            command=command._asdict(),
            cleanup=cleanup,
            failed=failed,
            **kwargs,
        )
        got = profile.guard(
            snapshot,
            command,
            target=target,
            fresh=fresh,
            baseline=baseline,
            cleanup=cleanup,
            failed=failed,
        )
        assert got == bool(want), (seed, case)
        want = render(
            h, "replan", cleanup=cleanup, transaction_attempt=attempt, **kwargs
        )
        got = profile.cap_only_replan(
            snapshot, target, fresh, cleanup=cleanup, transaction_attempt=attempt
        )
        assert got == bool(want), (seed, case)


def test_guard_and_cap_only_replan_corpus_reaches_both_outcomes():
    rng = random.Random(77)
    outcomes = {"guard": [0, 0], "replan": [0, 0]}
    for _ in range(1200):
        h = oracle()
        scenario(h, rng)
        ceiling = ceilings(h, rng)
        target = decide_now(h, ceiling=ceiling)
        runtime = random_runtime(rng, target.desired)
        h.data[RT]["attributes"]["runtime"] = runtime
        mutate_after_target(h, rng)
        fresh = decide_now(h, ceiling=ceiling)
        baseline = decide_now(h, base=True, ceiling=ceiling)
        snapshot = snapshot_of(h, h.ctx, runtime)
        command = profile.Command(
            *rng.choice(
                [
                    (e, v, rng.choice(["disable", "limits", "enable"]))
                    for e, v in target.desired.items()
                ]
            )
        )
        cleanup = rng.random() < 0.3
        allowed = profile.guard(
            snapshot,
            command,
            target=target,
            fresh=fresh,
            baseline=baseline,
            cleanup=cleanup,
            failed=False,
        )
        replan = profile.cap_only_replan(
            snapshot, target, fresh, cleanup=cleanup, transaction_attempt=1
        )
        outcomes["guard"][allowed] += 1
        outcomes["replan"][replan] += 1
    assert min(outcomes["guard"]) >= 100 and min(outcomes["replan"]) >= 25, outcomes


def raw_bank(h, rng):
    """A holding-register answer around the sample's own values, with faults."""
    registers = profile.register_map(entities_of(h.ctx, h.prefix))
    raw = {}
    for entity, register in registers.items():
        state = h.data[entity]["state"]
        if entity.startswith("select."):
            value = {"Disabled": 0, "Grid": 1, "Sell": 32}.get(state, 0)
        else:
            value = round(profile._float(state) / register.scale)
        roll = rng.random()
        if roll < 0.6:
            raw[register.address] = value
        elif roll < 0.7:
            raw[register.address] = value + rng.choice([-1, 1, 2, 45])
        elif roll < 0.78:
            raw[str(register.address)] = value
        elif roll < 0.84:
            raw[register.address] = rng.choice(["x", None, float("nan"), 7.9, "12"])
        elif roll < 0.9:
            raw[register.address] = rng.choice([2, 3, 33, 31])
    return raw


def test_register_map_parity_with_the_blueprint():
    h = oracle()
    got = profile.register_map(entities_of(h.ctx, h.prefix))
    assert [(e, r.address, r.scale) for e, r in got.items()] == [
        (e, r["address"], r["scale"]) for e, r in h.ctx["registers"].items()
    ]
    assert len(got) == 27


@pytest.mark.parametrize("seed", range(20))
def test_observation_and_confirmation_parity(seed):
    rng = random.Random(3000 + seed)
    for case in range(25):
        h = oracle()
        scenario(h, rng)
        registers = profile.register_map(entities_of(h.ctx, h.prefix))
        raw = rng.choice([raw_bank(h, rng), raw_bank(h, rng), None, "bad", {}])
        want = render(h, "observed", raw_response=raw if raw is not None else {})
        got = profile.observe(raw, registers)
        if raw is None:
            want = render(h, "observed", raw_response=None)
        assert got == want, (seed, case)
        for entity, register in registers.items():
            for value in rng.sample(
                [0, 1, 14, 14.0, 49.6, "Grid", "Sell", "Disabled", "Other"], 3
            ):
                if entity.startswith("select.") != isinstance(value, str):
                    continue
                command = {"entity": entity, "value": value, "phase": "enable"}
                response = raw if isinstance(raw, dict) else {}
                raw_value = render(
                    h,
                    "raw_value",
                    raw_response=response,
                    command=command,
                    registers=h.ctx["registers"],
                )
                confirmed = render(
                    h,
                    "confirmed_write",
                    raw_response=response,
                    raw_value=raw_value,
                    command=command,
                    registers=h.ctx["registers"],
                )
                assert profile.confirms(
                    response, profile.Command(**command), register
                ) == bool(confirmed), (seed, case, entity, value)


def scenario_cases():
    """Every parametrized case of the scenario tests that does not pin the blueprint."""
    cases = []
    for name, function in vars(scenarios).items():
        if not name.startswith("test_") or not callable(function):
            continue
        marks = getattr(function, "pytestmark", [])
        if any(mark.name == "blueprint_only" for mark in marks):
            continue
        axes = []
        for mark in marks:
            if mark.name != "parametrize":
                continue
            names = mark.args[0]
            names = (
                [n.strip() for n in names.split(",")]
                if isinstance(names, str)
                else names
            )
            values = [[v] if len(names) == 1 else list(v) for v in mark.args[1]]
            axes.append([dict(zip(names, value, strict=True)) for value in values])
        for combination in itertools.product(*axes):
            kwargs = {k: v for part in combination for k, v in part.items()}
            label = "-".join(str(v) for v in kwargs.values())
            cases.append(pytest.param(function, kwargs, id=f"{name}[{label}]"))
    return cases


def record(engine, function, kwargs):
    arguments = dict(kwargs)
    deye_engines.use(engine)
    deye_engines.TRACE = []
    try:
        names = function.__code__.co_varnames[: function.__code__.co_argcount]
        if "h" in names:
            arguments["h"] = deye_engines.Harness()
        if "engine" in names:
            arguments["engine"] = "native"
        function(**arguments)
        return deye_engines.TRACE
    finally:
        deye_engines.TRACE = None
        deye_engines.use("blueprint")


SCENARIOS = scenario_cases()


def test_every_scenario_is_compared():
    assert len(SCENARIOS) >= 255


@pytest.mark.parametrize("function,kwargs", SCENARIOS)
def test_scenario_parity_with_the_frozen_blueprint(function, kwargs):
    """Same decisions, same ordered writes, same reads, same final runtime."""
    if kwargs.get("missing") in ("schema", "no_mode_entity"):
        pytest.skip("the native controller has no schema or mode entity to lose")
    oracle_trace = record("oracle", function, kwargs)
    native_trace = record("native", function, kwargs)
    assert native_trace == oracle_trace


def fuzzed_run(engine, seed):
    """Several runs over a random sample with lost writes and mid-run changes."""
    rng = random.Random(seed)
    deye_engines.use(engine)
    deye_engines.TRACE = []
    try:
        h = deye_engines.Harness(INPUTS | limit_choices(rng))
        h.set(COUNTER, "5600.0")
        runner = deye_engines.Runner(h)
        scenario(h, rng, apply_limits=False)
        for _ in range(rng.randint(1, 4)):
            entities = list(runner.raw)
            runner.drop.clear()
            for entity in h.ctx["registers"]:
                if rng.random() < 0.04:
                    runner.drop.add(entity)
            if rng.random() < 0.15:
                address = rng.choice(entities)
                runner.raw[address] = rng.choice([0, 1, 45, 32, 4960])
            target_write = rng.randint(0, 6)
            seen = []

            def hook(r, entity, value, seen=seen, target_write=target_write):
                seen.append(entity)
                if len(seen) == target_write:
                    mutate_after_target(h, rng)

            runner.hook = hook if rng.random() < 0.7 else None
            runner.run()
            if rng.random() < 0.4:
                mutate_after_target(h, rng)
            if rng.random() < 0.3:
                h.data[RT]["attributes"]["runtime"] = {}
        return deye_engines.TRACE
    finally:
        deye_engines.TRACE = None
        deye_engines.use("blueprint")


@pytest.mark.parametrize("seed", range(60))
def test_fuzzed_run_parity_with_the_frozen_blueprint(seed):
    oracle_trace = fuzzed_run("oracle", 5000 + seed)
    native_trace = fuzzed_run("native", 5000 + seed)
    assert native_trace == oracle_trace


def test_fuzzed_runs_write_abort_and_restore():
    found = dict.fromkeys(("writes", "restored", "lost_write"), 0)
    for seed in range(60):
        trace = fuzzed_run("native", 5000 + seed)
        for entry in trace:
            if entry[0] == "run":
                found["writes"] += len(entry[1])
                runtime = entry[4]
                found["restored"] += runtime.get("code") == "restored"
                found["lost_write"] += runtime.get("code") in (
                    "write_failed",
                    "verification_required",
                )
    assert found["writes"] > 1000, found
    assert found["restored"] >= 20 and found["lost_write"] >= 20, found


@pytest.mark.parametrize("planned", [2.0, 0.025, 0.3])
def test_discharge_energy_boundary_parity(planned):
    """The reached test of an energy-counted sell row, around its 0.05 kWh margin."""
    deye_engines.use("oracle")
    try:
        h = scenarios.voltage_sell()
    finally:
        deye_engines.use("blueprint")
    h.data[P]["attributes"]["intervals"][0].update(discharge_kwh=planned)
    h.accept()
    runtime = h.data[RT]["attributes"]
    runtime["runtime"] = {
        "slot_energy": {
            "key": render(h, "decision")["reached_key"],
            "discharge": 5600.0,
        }
    }
    seen = set()
    for margin in (-0.2, -0.06, -0.0501, -0.05, -0.0499, -0.04, 0, 0.1):
        h.set(COUNTER, str(5600.0 + planned + margin))
        want = render(h, "decision")
        got = native(h)
        assert got == want, margin
        seen.add(got["target_reached"])
    assert seen == ({True, False} if planned > 0.1 else {True})
