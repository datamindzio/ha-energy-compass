"""Random problems on which HiGHS stops inside its 1e-6 tolerances; real HiGHS, no fakes.

Each case is a benchmark site over 24 or 48 hourly slots with buy and sell drawn per slot
from 0.3-1.2, battery export allowed and a PV scale picked by a seeded scan. They failed
as `solver_failure: invalid solver result` before the validators and the plan read the
noise the way the solver leaves it:

- seed 16: a discharge/export pair of 7.2e-7 kWh beside an export indicator at 0 sat
  above the old 1e-7 validator tolerance (T-031: `activity_tolerance`);
- seeds 7 and 24: CHARGE_PV with 1.6e-6 / 7.5e-6 kWh of curtailment, the binary of
  `curt <= M * mode_CURTAIL` at 1e-6 times M > 1, which the classifier reads as CURTAIL.
  Seed 7 passes on a changed vertex alone since T-030; seed 24 failed on `main`;
- seed 133 (48 slots): HOLD with 2.8e-6 kWh of discharge, the same binary noise on
  `bd <= M * (DISCHARGE_GRID + SELF_CONSUME)`, which the classifier reads as SELF_CONSUME.

The solver-independent cases below put that noise on every flow every mode leaves at zero.
"""

import random
from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from benchmarks.optimizer import make_problem
from custom_components.energy_compass.engine.dispatch_policy import (
    IDLE_FLOWS,
    MODES,
    validate_modes,
)
from custom_components.energy_compass.engine.machine_state import machine_state
from custom_components.energy_compass.engine.mode_noise import settle_mode_noise
from custom_components.energy_compass.engine.models import Flow, SolveError
from custom_components.energy_compass.engine.optimize import solve

CASES = {
    "seed 7 curtailment noise": (7, 3.2068840553749727, 24),
    "seed 16 export indicator noise": (16, 0.5105639826334869, 24),
    "seed 24 curtailment noise": (24, 15.666677533806073, 24),
    "seed 133 discharge noise on HOLD": (133, 0.32475534783774435, 48),
}


def corpus_problem(seed: int, pv_scale: float, count: int = 24):
    """The T-036 random corpus (high prices, battery export) at one PV scale."""
    r = random.Random(seed * 1000 + count)
    p = make_problem(96, allow_battery_export=r.random() < 0.3)
    p = replace(
        p,
        battery=replace(p.battery, allow_battery_export=True),
        limit_export_to_pv=False,
    )
    pv_k, load_k = r.uniform(0.3, 2.0), r.uniform(0.6, 2.0)
    start = p.slots[0].start
    slots = []
    for i in range(count):
        quarter = p.slots[4 * (i % 24)]
        slots.append(
            replace(
                quarter,
                start=start + timedelta(hours=i),
                end=start + timedelta(hours=i + 1),
                pv_kwh=4 * quarter.pv_kwh * pv_k * r.uniform(0.6, 1.2) * pv_scale,
                load_kwh=4 * quarter.load_kwh * load_k * r.uniform(0.7, 1.4),
                buy_per_kwh=r.uniform(0.3, 1.2),
                sell_per_kwh=r.uniform(0.3, 1.2),
            )
        )
    battery = replace(p.battery, initial_kwh=r.uniform(1.35, 12))
    return replace(p, slots=tuple(slots), battery=battery)


@pytest.mark.parametrize("case", CASES)
def test_noisy_solution_publishes_a_plan_whose_rows_match_their_modes(case):
    seed, pv_scale, count = CASES[case]
    problem = corpus_problem(seed, pv_scale, count)

    plan = solve(problem, time_limit_s=20.0, accept_incumbent=True)

    assert len(plan.flows) == count
    for slot, flow in zip(problem.slots, plan.flows, strict=True):
        assert flow.dispatch_mode == machine_state(slot, flow)


def hourly_problem(pv_kwh, load_kwh, slots=1):
    """One or more identical hourly slots on the benchmark site, battery at 6 kWh."""
    base = make_problem(1)
    start = base.slots[0].start
    slot = replace(
        base.slots[0],
        end=start + timedelta(hours=1),
        pv_kwh=pv_kwh,
        load_kwh=load_kwh,
    )
    battery = replace(base.battery, initial_kwh=6.0, allow_battery_export=True)
    return replace(
        base,
        slots=tuple(
            replace(
                slot,
                start=start + timedelta(hours=i),
                end=start + timedelta(hours=i + 1),
            )
            for i in range(slots)
        ),
        battery=battery,
        limit_export_to_pv=False,
    )


def solver_rows(problem, rows):
    """The variable vectors, values and bounds a solver result of `rows` would leave.

    A row names its mode, the flows it carries and the one flow the site balance
    `pv - curt + gin + bd = load + gout + bc` fixes (`derive`).
    """
    battery = problem.battery
    names = ("gin", "gout", "curt", "bc", "bd", "energy", *(f"mode_{m}" for m in MODES))
    vectors, values, upper = [], [], []
    energy = battery.initial_kwh
    for slot, (mode, flow, derive) in zip(problem.slots, rows, strict=True):
        flow = {"gin": 0.0, "gout": 0.0, "curt": 0.0, "bc": 0.0, "bd": 0.0, **flow}
        net = (
            slot.load_kwh + flow["bc"] + flow["gout"] + flow["curt"] - slot.pv_kwh
            - flow["bd"] - flow["gin"]
        )  # fmt: skip
        if derive == "bd":
            flow["bd"] += net
        elif derive == "bc":
            flow["bc"] -= net
        elif net >= 0:
            flow["gin"] += net
        else:
            flow["gout"] -= net
        energy += battery.eta_charge * flow["bc"] - flow["bd"] / battery.eta_discharge
        flow["energy"] = energy
        vectors.append({name: len(values) + i for i, name in enumerate(names)})
        values += [flow.get(name, float(name == f"mode_{mode}")) for name in names]
        upper += [
            5.0,
            8.0,
            slot.pv_kwh,
            battery.charge_kw,
            battery.discharge_kw,
            battery.capacity_kwh,
            *[1.0] * len(MODES),
        ]
    return vectors, np.array(values), upper


def flows_of(vectors, values, mode_of):
    return tuple(
        Flow(
            *(float(values[v[name]]) for name in ("gin", "gout", "bc", "bd", "curt")),
            float(values[v["energy"]]),
            None,
            mode,
        )
        for v, mode in zip(vectors, mode_of, strict=True)
    )


def settled_flows(settled, modes):
    return tuple(
        Flow(f["gin"], f["gout"], f["bc"], f["bd"], f["curt"], f["energy"], None, mode)
        for f, mode in zip(settled, modes, strict=True)
    )


# Noise 1e-6 < n <= 1e-6 * (2 * M + 1) on flows the mode leaves at zero, M being the
# variable's bound: 3.3 kWh for the battery, 8 for export, the slot's PV for curtailment.
NOISE = {
    "HOLD discharge": ("HOLD", 0.5, 1.0, {"bd": 2.8e-6}, "grid"),
    "HOLD curtail and charge": ("HOLD", 2.0, 0.5, {"curt": 3e-6, "bc": 2e-6}, "grid"),
    "CHARGE_PV curtail and discharge": (
        "CHARGE_PV",
        3.0,
        0.5,
        {"curt": 6e-6, "bd": 2e-6},
        "bc",
    ),
    "CHARGE_GRID curtail and discharge": (
        "CHARGE_GRID",
        1.0,
        1.5,
        {"bc": 2.0, "curt": 2e-6, "bd": 3e-6},
        "grid",
    ),
    "DISCHARGE_GRID curtail and charge": (
        "DISCHARGE_GRID",
        1.0,
        0.5,
        {"bd": 2.0, "curt": 2e-6, "bc": 3e-6},
        "grid",
    ),
    "SELF_CONSUME without import": (
        "SELF_CONSUME",
        1.0,
        2.0,
        {"gout": 3e-6, "curt": 2e-6, "bc": 2e-6},
        "bd",
    ),
    "SELF_CONSUME with import": (
        "SELF_CONSUME",
        1.0,
        5.0,
        {"bd": 3.3, "gout": 4e-6, "bc": 3e-6},
        "grid",
    ),
    "CURTAIL charge and discharge": (
        "CURTAIL",
        2.0,
        0.5,
        {"curt": 1.4, "bc": 2e-6, "bd": 3e-6},
        "grid",
    ),
}


def test_every_mode_is_covered_for_every_flow_it_leaves_at_zero():
    flows = {name: set() for name in ("bc", "bd", "curt", "gout")}
    for mode, *_ in NOISE.values():
        for name in IDLE_FLOWS[mode]:
            flows[name].add(mode)
    assert {m for m, *_ in NOISE.values()} == set(MODES)
    assert all(flows.values())


@pytest.mark.parametrize("case", NOISE)
def test_noise_on_flows_a_mode_leaves_at_zero_is_settled(case):
    mode, pv, load, noise, derive = NOISE[case]
    problem = hourly_problem(pv, load)
    vectors, values, upper = solver_rows(problem, [(mode, noise, derive)])
    raw = flows_of(vectors, values, [mode])
    battery = problem.battery

    # The solver result alone is what failed on real HiGHS: the label does not hold.
    with pytest.raises(SolveError, match="physical dispatch mode"):
        validate_modes(problem, raw)

    (flow,) = settled = settle_mode_noise(problem, vectors, values, upper)
    plan = settled_flows(settled, [mode])
    validate_modes(problem, plan)
    assert machine_state(problem.slots[0], plan[0]) == mode
    for name in IDLE_FLOWS[mode]:
        assert flow[name] == 0.0
    # One consistent row: the site balance closes and the energy follows the battery.
    slot = problem.slots[0]
    assert slot.pv_kwh - flow["curt"] + flow["gin"] + flow["bd"] == pytest.approx(
        slot.load_kwh + flow["gout"] + flow["bc"], abs=1e-12
    )
    assert flow["energy"] == pytest.approx(
        battery.initial_kwh
        + battery.eta_charge * flow["bc"]
        - flow["bd"] / battery.eta_discharge,
        abs=1e-12,
    )
    assert min(flow["gin"], flow["gout"]) == pytest.approx(0, abs=1e-5)


# Zeros as real HiGHS returns them: a hair below zero on a few percent of the slots.
DUST = {"bd": -6.161471333143709e-16, "gout": -4.6e-14, "bc": -7.7e-16, "curt": -2e-14}
DUSTY = {
    "HOLD curtail": ("HOLD", 2.0, 0.5, {"curt": 3e-6}, "grid"),
    "HOLD discharge": ("HOLD", 0.5, 1.0, {"bd": 2.8e-6}, "grid"),
    "CHARGE_PV curtail": ("CHARGE_PV", 3.0, 0.5, {"curt": 6e-6}, "bc"),
    "CHARGE_PV discharge": ("CHARGE_PV", 3.0, 0.5, {"bc": 2.5, "bd": 2e-6}, "grid"),
    "CHARGE_GRID curtail": ("CHARGE_GRID", 1.0, 1.5, {"bc": 2.0, "curt": 2e-6}, "grid"),
    "CHARGE_GRID discharge": ("CHARGE_GRID", 1.0, 1.5, {"bc": 2.0, "bd": 3e-6}, "grid"),
}


@pytest.mark.parametrize("case", DUSTY)
def test_noise_beside_zeros_the_solver_left_a_hair_negative_is_settled(case):
    mode, pv, load, noise, derive = DUSTY[case]
    problem = hourly_problem(pv, load)
    vectors, values, upper = solver_rows(problem, [(mode, noise, derive)])
    (v,) = vectors
    dusted = [name for name in DUST if values[v[name]] == 0.0]
    for name in dusted:
        values[v[name]] = DUST[name]
    assert set(dusted) & set(IDLE_FLOWS[mode])
    with pytest.raises(SolveError, match="physical dispatch mode"):
        validate_modes(problem, flows_of(vectors, values, [mode]))

    (flow,) = settled = settle_mode_noise(problem, vectors, values, upper)

    plan = settled_flows(settled, [mode])
    validate_modes(problem, plan)
    assert machine_state(problem.slots[0], plan[0]) == mode
    for name in noise:
        if name in IDLE_FLOWS[mode]:
            assert flow[name] == 0.0
    # The solver's own zeros are not noise to settle: they stay as it returned them.
    for name in dusted:
        if name in IDLE_FLOWS[mode]:
            assert flow[name] == DUST[name]
    slot = problem.slots[0]
    assert slot.pv_kwh - flow["curt"] + flow["gin"] + flow["bd"] == pytest.approx(
        slot.load_kwh + flow["gout"] + flow["bc"], abs=1e-12
    )


def test_self_consume_noise_the_discharge_cannot_absorb_is_left_as_solved():
    problem = hourly_problem(1.0, 1.0)
    noise = {"gout": -4.6e-14, "curt": 2e-6, "bd": 1e-6}
    vectors, values, upper = solver_rows(problem, [("SELF_CONSUME", noise, "grid")])

    (flow,) = settle_mode_noise(problem, vectors, values, upper)

    assert (flow["curt"], flow["bd"]) == (2e-6, 1e-6)


def test_noise_the_classifier_already_reads_as_zero_is_left_as_solved():
    problem = hourly_problem(1.0, 2.0)
    noise = {"gout": 0.9e-6, "curt": 0.9e-6, "bc": 0.9e-6}
    vectors, values, upper = solver_rows(problem, [("SELF_CONSUME", noise, "bd")])
    validate_modes(problem, flows_of(vectors, values, ["SELF_CONSUME"]))

    (flow,) = settle_mode_noise(problem, vectors, values, upper)

    assert {name: flow[name] for name in noise} == noise
    assert flow["bd"] == values[vectors[0]["bd"]]


def test_noise_above_the_threshold_next_to_noise_below_it_settles_to_a_valid_row():
    problem = hourly_problem(1.0, 2.0)
    noise = {"gout": 0.9e-6, "curt": 2e-6}
    vectors, values, upper = solver_rows(problem, [("SELF_CONSUME", noise, "bd")])

    settled = settle_mode_noise(problem, vectors, values, upper)

    validate_modes(problem, settled_flows(settled, ["SELF_CONSUME"]))
    assert (settled[0]["curt"], settled[0]["gout"]) == (0.0, 0.9e-6)


def test_settled_energy_carries_into_the_slots_after_it():
    problem = hourly_problem(0.5, 1.0, slots=2)
    rows = [("HOLD", {"bd": 2.8e-6}, "grid"), ("HOLD", {}, "grid")]
    vectors, values, upper = solver_rows(problem, rows)

    first, second = settle_mode_noise(problem, vectors, values, upper)

    assert first["bd"] == 0.0
    assert first["energy"] == pytest.approx(6.0, abs=1e-12)
    assert second["energy"] == pytest.approx(6.0, abs=1e-12)


def test_a_flow_beyond_the_noise_bound_is_not_settled():
    problem = hourly_problem(0.5, 1.0)
    vectors, values, upper = solver_rows(problem, [("HOLD", {"bd": 1e-3}, "grid")])

    (flow,) = settle_mode_noise(problem, vectors, values, upper)

    assert flow["bd"] == 1e-3
    with pytest.raises(SolveError, match="physical dispatch mode"):
        validate_modes(problem, settled_flows([flow], ["HOLD"]))


def test_a_clean_solution_is_published_as_the_solver_left_it():
    problem = hourly_problem(0.5, 1.0)
    vectors, values, upper = solver_rows(problem, [("SELF_CONSUME", {}, "bd")])

    (flow,) = settle_mode_noise(problem, vectors, values, upper)

    assert flow["bd"] == pytest.approx(0.5)
    assert flow["gin"] == flow["gout"] == flow["curt"] == flow["bc"] == 0.0
    assert flow["energy"] == values[vectors[0]["energy"]]
