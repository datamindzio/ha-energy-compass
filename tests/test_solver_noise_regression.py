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

One more case needs a battery above 4.5 kWh per slot (T-049):

- seed 108 (48 slots) on a 30 kWh battery that takes 25 kWh per slot: a CHARGE_PV slot with
  98.8 kWh of PV and 16.0 kWh of load took 24.999975 kWh, 2.5e-5 short of the limit (the
  binary of `bc >= M * (x_CHARGE_PV - full)` at 1 - 1e-6 with M = 25), and exported the rest,
  which the fixed 1e-5 tolerance of the follow check rejected as a partial charge.

The solver-independent cases below put that noise on every flow every mode leaves at zero.
"""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from benchmarks.optimizer import make_problem
from custom_components.energy_compass.engine.dispatch_policy import (
    IDLE_FLOWS,
    MODES,
    validate_modes,
)
from custom_components.energy_compass.engine.export_benefit import (
    validate_export_benefit,
)
from custom_components.energy_compass.engine.machine_state import machine_state
from custom_components.energy_compass.engine.mode_noise import (
    _settle,
    settle_mode_noise,
)
from custom_components.energy_compass.engine.models import (
    Battery,
    Flow,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import _validate_settled, solve

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


def big_battery_problem(seed, pv_scale, count, limit_kwh=25.0, capacity_kwh=30.0):
    """A corpus problem on a site whose battery takes `limit_kwh` per slot, load scaled to match.

    The benchmark battery moves 3.3 kWh per slot, where the noise of a follow row stays
    under the 1e-5 kWh tolerance; at `limit_kwh` it does not.
    """
    problem = corpus_problem(seed, pv_scale, count)
    battery = problem.battery
    return replace(
        problem,
        slots=tuple(
            replace(s, load_kwh=s.load_kwh * limit_kwh / 3.3) for s in problem.slots
        ),
        battery=replace(
            battery,
            capacity_kwh=capacity_kwh,
            charge_kw=limit_kwh,
            discharge_kw=limit_kwh,
            initial_kwh=battery.initial_kwh * capacity_kwh / 13.5,
        ),
        site=SiteLimits(3 * limit_kwh, 3 * limit_kwh, 3 * limit_kwh, True),
    )


def test_charge_from_pv_on_a_battery_above_4_5_kwh_per_slot_publishes_a_plan():
    problem = big_battery_problem(108, 21.794695277085804, 48)

    plan = solve(problem, time_limit_s=20.0, accept_incumbent=True)

    assert len(plan.flows) == 48
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


def with_export_indicator(vectors, values, active, start=0.0):
    """The export indicator rows `constrain_export_benefit` adds, at the given values."""
    (v,) = vectors
    v.update(export_active=len(values), export_start=len(values) + 1)
    return np.append(values, [active, start])


def test_export_pair_noise_beside_an_indicator_a_hair_off_zero_is_not_an_export():
    # T-052: HiGHS leaves `export_active` (the DISCHARGE_GRID binary) at 4.9e-7 and
    # `bd <= M * (e + z)`, `gout <= M * (1 + e - z)` carry 1e-6 * M beside it: 3.5e-6
    # kWh of both, above the fixed 1e-6 the old validator read as an export.
    problem = hourly_problem(0.5, 1.0)
    vectors, values, upper = solver_rows(
        problem, [("HOLD", {"bd": 4.9e-6, "gout": 3.5e-6}, "grid")]
    )
    values = with_export_indicator(vectors, values, 4.9e-7)

    assert validate_export_benefit(problem, vectors, values) == (0, 0.0)

    settled = settle_mode_noise(problem, vectors, values, upper)
    (flow,) = settled
    assert validate_export_benefit(problem, vectors, values, settled) == (0, 0.0)
    plan = settled_flows([flow], ["HOLD"])
    validate_modes(problem, plan)
    assert flow["bd"] == flow["gout"] == 0.0
    assert machine_state(problem.slots[0], plan[0]) == "HOLD"


def test_export_pair_beyond_the_noise_bound_beside_an_inactive_indicator_is_refused():
    problem = hourly_problem(0.5, 1.0)
    vectors, values, _ = solver_rows(
        problem, [("HOLD", {"bd": 1e-3, "gout": 1e-3}, "grid")]
    )
    values = with_export_indicator(vectors, values, 0.0)

    with pytest.raises(SolveError, match="physical export indicator"):
        validate_export_benefit(problem, vectors, values)


def unequal_limits_case(row, *, discharge_kw, export_kw, pv, load):
    """A moded slot on a site whose battery and export limits differ.

    The export indicator is the DISCHARGE_GRID binary, as `constrain_export_benefit`
    binds it when the mode model exists.
    """
    base = hourly_problem(pv, load)
    problem = replace(
        base,
        site=SiteLimits(100.0, 100.0, export_kw, True),
        battery=replace(base.battery, discharge_kw=discharge_kw),
    )
    vectors, values, upper = solver_rows(problem, [row])
    (v,) = vectors
    v.update(export_active=v["mode_DISCHARGE_GRID"], export_start=len(values))
    return problem, vectors, np.append(values, [0.0]), upper


# Each row gates its own flow by that flow's bound (SELF_CONSUME's `gout <= G * (1 - mode)`
# by the export limit, CHARGE_PV's `bd <= D * (1 - mode)` by the discharge limit) and the
# other side of the pair is free. T-052 review: the bound was the smaller of the limits,
# which refused 1e-5 kWh where the row allows 1e-6 * (2 * 45 + 1) = 9.1e-5.
UNEQUAL_LIMITS = {
    "export limit above the discharge limit": (
        ("SELF_CONSUME", {"gout": 1e-5}, "bd"),
        {"discharge_kw": 3.3, "export_kw": 45.0, "pv": 0.5, "load": 1.0},
        "gout",
    ),
    "discharge limit above the export limit": (
        ("CHARGE_PV", {"bd": 1e-5, "bc": 3.3}, "grid"),
        {"discharge_kw": 45.0, "export_kw": 3.3, "pv": 4.0, "load": 0.5},
        "bd",
    ),
}


@pytest.mark.parametrize("case", UNEQUAL_LIMITS)
def test_pair_noise_up_to_the_gated_flows_own_bound_is_not_an_export(case):
    row, limits, noisy = UNEQUAL_LIMITS[case]
    problem, vectors, values, upper = unequal_limits_case(row, **limits)

    assert validate_export_benefit(problem, vectors, values) == (0, 0.0)

    settled = settle_mode_noise(problem, vectors, values, upper)
    assert validate_export_benefit(problem, vectors, values, settled) == (0, 0.0)
    assert settled[0][noisy] == 0.0
    plan = settled_flows(settled, [row[0]])
    validate_modes(problem, plan)
    assert machine_state(problem.slots[0], plan[0]) == row[0]


@pytest.mark.parametrize("case", UNEQUAL_LIMITS)
def test_pair_noise_beyond_the_gated_flows_own_bound_is_refused(case):
    (mode, noise, derive), limits, noisy = UNEQUAL_LIMITS[case]
    # Beyond 9.1e-5, with the other side of the pair a real flow.
    row = (mode, {**noise, noisy: 2e-4}, derive)
    problem, vectors, values, _ = unequal_limits_case(row, **limits)

    with pytest.raises(SolveError, match="physical export indicator"):
        validate_export_benefit(problem, vectors, values)


def test_import_noise_beside_an_export_when_the_grid_indicator_is_off_one_is_netted():
    # T-052: `gin <= M * grid_mode` with grid_mode 7.8e-8 and M 45 left 1.8e-6 kWh of
    # import beside 13.6 kWh of export.
    problem = replace(hourly_problem(0.5, 1.0), site=SiteLimits(45.0, 45.0, 45.0, True))
    vectors, values, upper = solver_rows(
        problem,
        [("DISCHARGE_GRID", {"bd": 3.3, "gin": 1.8e-6, "gout": 2.0}, "bd")],
    )
    (v,) = vectors
    raw = values[v["gin"]] - values[v["gout"]]

    (flow,) = settle_mode_noise(problem, vectors, values, upper)

    assert flow["gin"] == 0.0
    assert flow["gout"] == pytest.approx(-raw, abs=1e-15)
    plan = settled_flows([flow], ["DISCHARGE_GRID"])
    validate_modes(problem, plan)
    slot = problem.slots[0]
    assert slot.pv_kwh + flow["bd"] == pytest.approx(
        slot.load_kwh + flow["gout"] + flow["bc"], abs=1e-12
    )


# Without minimum duration (0) there are no mode variables: `battery_mode` gates `bc` and
# `bd`, and with the export hurdle `export_active` and `export_side` gate `bd` and `gout`.
# The clean solution is solved on real HiGHS and the noise those rows allow is added to
# it before validation, as a solver stopping inside its tolerances leaves it.
HOUR = datetime(2026, 9, 17, tzinfo=UTC)


def unmoded_problem(buy, sell, pv, load, battery, *, hurdle, terminal=0.0):
    slot = Slot(HOUR, HOUR + timedelta(hours=1), buy, sell, pv, load)
    return Problem(
        (slot,),
        SiteLimits(60, 60, 60, True),
        battery,
        "value",
        terminal,
        (),
        "UTC",
        minimum_mode_minutes=0,
        limit_export_to_pv=False,
        minimum_export_episode_benefit=1.0 if hurdle else 0.0,
    )


def solve_with_noise(monkeypatch, problem, noise):
    """Solve, adding `noise(vector, values)` to the solver result before it is validated."""
    from custom_components.energy_compass.engine import optimize

    real = optimize._validate_solution

    def noisy(problem_, vectors, values, *rest):
        noise(vectors[0], values)
        real(problem_, vectors, values, *rest)

    monkeypatch.setattr(optimize, "_validate_solution", noisy)
    return solve(problem)


def move(values, v, d, flows, closing):
    """Add `d` kWh to `flows`, the site balance closed by `closing` (-d)."""
    for name in flows:
        values[v[name]] += d
    for name in closing:
        values[v[name]] -= d


BIG_BATTERY_20 = Battery(40, 0.1, 0.9, 20, 20, 20, 1, 1, 0.01, True, True)


def test_unmoded_battery_direction_noise_does_not_publish_a_charge(monkeypatch):
    # No mode variables: 1.5e-5 kWh of charge beside a 16 kWh discharge, `battery_mode`
    # at 1e-6 (`bc <= 20 * battery_mode + 1e-6` allows 2.1e-5). The plan used to read
    # CHARGE_GRID, charge winning the machine state's precedence over discharge.
    problem = unmoded_problem(1.0, 0.1, 0.0, 30.0, BIG_BATTERY_20, hurdle=False)
    clean = solve(problem).flows[0]

    def noise(v, values):
        values[v["battery_mode"]] = 1e-6
        move(
            values,
            v,
            1.5e-5,
            ("bc", "grid_battery", "bd", "battery_load"),
            ("grid_load",),
        )

    plan = solve_with_noise(monkeypatch, problem, noise)

    (flow,) = plan.flows
    assert flow.charge_kwh == 0.0
    assert machine_state(problem.slots[0], flow) == machine_state(
        problem.slots[0], clean
    )
    assert machine_state(problem.slots[0], flow) != "CHARGE_GRID"
    slot = problem.slots[0]
    assert slot.pv_kwh + flow.grid_import_kwh + flow.discharge_kwh == pytest.approx(
        slot.load_kwh + flow.grid_export_kwh + flow.charge_kwh, abs=1e-12
    )
    assert flow.end_soc_kwh == pytest.approx(
        BIG_BATTERY_20.initial_kwh - flow.discharge_kwh, abs=1e-12
    )


def battery_cycle(battery_mode):
    """1e-5 kWh more of PV into the battery while the battery feeds the load as much."""

    def noise(v, values):
        values[v["battery_mode"]] = battery_mode
        if "discharging" in v:
            values[v["discharging"]] = 1 - battery_mode
        move(values, v, 1e-5, ("bd", "battery_load", "pv_battery", "bc"), ("pv_load",))

    return noise


# pv, load, terminal value, `battery_mode`: the grid is idle in both slots.
GRID_IDLE = {
    "charging the whole PV surplus": (10.0, 2.0, 1.0, 1 - 9e-7),
    "covering the whole net load": (2.0, 10.0, 0.5, 9e-7),
}


@pytest.mark.parametrize("hurdle", [False, True])
@pytest.mark.parametrize("case", GRID_IDLE)
def test_unmoded_battery_direction_noise_does_not_open_the_grid(
    monkeypatch, case, hurdle
):
    # No mode variables, grid idle, 1e-5 kWh on the idle battery direction (`bd <= 20 *
    # (1 - battery_mode) + 1e-6` allows 1.9e-5). Zeroing it used to close the balance
    # through the grid: 1e-5 kWh of import beside the charge read CHARGE_GRID, of export
    # beside the discharge DISCHARGE_GRID. The other direction gives it back instead.
    pv, load, terminal, battery_mode = GRID_IDLE[case]
    problem = unmoded_problem(
        1.0, 0.1, pv, load, BIG_BATTERY_20, hurdle=hurdle, terminal=terminal
    )
    slot = problem.slots[0]
    clean = solve(problem).flows[0]
    assert (clean.grid_import_kwh, clean.grid_export_kwh) == pytest.approx((0.0, 0.0))
    assert machine_state(slot, clean) in ("CHARGE_PV", "SELF_CONSUME")

    plan = solve_with_noise(monkeypatch, problem, battery_cycle(battery_mode))

    (flow,) = plan.flows
    assert machine_state(slot, flow) == machine_state(slot, clean)
    assert (flow.grid_import_kwh, flow.grid_export_kwh) == pytest.approx(
        (0.0, 0.0), abs=1e-12
    )
    assert min(flow.charge_kwh, flow.discharge_kwh) == 0.0
    assert slot.pv_kwh + flow.grid_import_kwh + flow.discharge_kwh == pytest.approx(
        slot.load_kwh + flow.grid_export_kwh + flow.charge_kwh, abs=1e-12
    )
    assert flow.end_soc_kwh == pytest.approx(
        BIG_BATTERY_20.initial_kwh + flow.charge_kwh - flow.discharge_kwh, abs=1e-12
    )
    assert flow.end_soc_kwh == pytest.approx(clean.end_soc_kwh, abs=1e-12)


def export_hurdle_problem(load):
    # Full battery, 5 kWh of PV: the surplus is exported, the battery holds.
    return unmoded_problem(
        0.5,
        0.3,
        5.0,
        load,
        replace(BIG_BATTERY_20, initial_kwh=36.0),
        hurdle=True,
        terminal=1.0,
    )


def test_unmoded_export_pair_noise_beside_an_inactive_indicator_is_no_export(
    monkeypatch,
):
    # The own indicators `export_active` (e) and `export_side` (z) at 9e-7: `bd <= 20 * (e
    # + z)` leaves 1.5e-5 kWh of discharge beside a 4 kWh PV export, which the plan read as
    # DISCHARGE_GRID with no new export episode.
    problem = export_hurdle_problem(1.0)
    clean = solve(problem).flows[0]
    assert clean.discharge_kwh == 0.0

    def noise(v, values):
        values[v["export_active"]] = values[v["export_side"]] = 9e-7
        move(values, v, 1.5e-5, ("bd", "battery_grid", "gout"), ())
        values[v["energy"]] -= 1.5e-5

    plan = solve_with_noise(monkeypatch, problem, noise)

    (flow,) = plan.flows
    assert flow.discharge_kwh == 0.0
    assert flow.grid_export_kwh == pytest.approx(clean.grid_export_kwh, abs=1e-12)
    assert machine_state(problem.slots[0], flow) == machine_state(
        problem.slots[0], clean
    )
    assert plan.new_export_episodes == 0
    assert flow.end_soc_kwh == pytest.approx(clean.end_soc_kwh, abs=1e-12)


def test_unmoded_export_noise_on_the_other_side_of_the_indicator_is_settled(
    monkeypatch,
):
    # z at 1 - 9e-7 gates `gout <= 60 * (1 + e - z)`: 1.5e-5 kWh of export, fed by the
    # battery, beside a 12 kWh discharge to the load. The plan used to read
    # DISCHARGE_GRID; the battery now delivers 1.5e-5 less.
    problem = unmoded_problem(1.0, 0.1, 0.0, 12.0, BIG_BATTERY_20, hurdle=True)
    clean = solve(problem).flows[0]
    assert (clean.discharge_kwh, clean.grid_export_kwh) == pytest.approx((12.0, 0.0))

    def noise(v, values):
        values[v["export_active"]] = 9e-7
        values[v["export_side"]] = 1 - 9e-7
        move(values, v, 1.5e-5, ("bd", "battery_grid", "gout"), ())
        values[v["energy"]] -= 1.5e-5

    plan = solve_with_noise(monkeypatch, problem, noise)

    (flow,) = plan.flows
    assert flow.grid_export_kwh == 0.0
    assert flow.discharge_kwh == pytest.approx(clean.discharge_kwh, abs=1e-12)
    assert flow.end_soc_kwh == pytest.approx(clean.end_soc_kwh, abs=1e-12)
    assert machine_state(problem.slots[0], flow) == machine_state(
        problem.slots[0], clean
    )
    assert plan.new_export_episodes == 0


HOLD_PASS_THROUGH = {
    # (battery_mode, export_side, extra kWh on gout beyond the 1e-5 that bd passes through)
    "gout equal to bd": (9e-7, 1 - 9e-7, 0.0),
    "gout 1e-9 above bd": (9e-7, 1 - 9e-7, 1e-9),
    "gout 1e-9 below bd": (9e-7, 1 - 9e-7, -1e-9),
    "export side 0 control": (9e-7, 9e-7, 0.0),
    "export side 0 control, gout 1e-9 above bd": (9e-7, 9e-7, 1e-9),
    "export side 0 control, gout 1e-9 below bd": (9e-7, 9e-7, -1e-9),
}


@pytest.mark.parametrize("case", HOLD_PASS_THROUGH)
def test_unmoded_hold_with_battery_to_grid_pass_through_is_published_as_hold(
    monkeypatch, case
):
    # PV 2, load 2: the solver holds. Battery direction (discharge), export indicator at
    # 9e-7 and 1e-5 kWh of `bd` passing through to `gout` (free `bd`; `gout` gated by the
    # export side). A residual of 1e-9 inside the balance tolerance used to decide whether
    # the slot published (`physical export indicator`) with `export_side` at 1.
    battery_mode, side, extra = HOLD_PASS_THROUGH[case]
    problem = unmoded_problem(
        1.0, 0.1, 2.0, 2.0, BIG_BATTERY_20, hurdle=True, terminal=0.5
    )
    clean = solve(problem).flows[0]
    assert machine_state(problem.slots[0], clean) == "HOLD"

    def noise(v, values):
        values[v["battery_mode"]] = battery_mode
        values[v["export_active"]] = 9e-7
        values[v["export_side"]] = side
        move(values, v, 1e-5, ("bd", "battery_grid", "gout"), ())
        values[v["energy"]] -= 1e-5
        values[v["gout"]] += extra

    plan = solve_with_noise(monkeypatch, problem, noise)

    (flow,) = plan.flows
    assert machine_state(problem.slots[0], flow) == "HOLD"
    assert flow.discharge_kwh <= 1e-8
    assert flow.grid_export_kwh <= 1e-8
    assert flow.charge_kwh == 0.0
    assert flow.grid_import_kwh == pytest.approx(clean.grid_import_kwh, abs=1e-8)
    assert flow.end_soc_kwh == pytest.approx(clean.end_soc_kwh, abs=1e-8)
    assert plan.new_export_episodes == 0


CYCLING_GAP = {
    "bc equal to bd": 0.0,
    "bc 1e-9 above bd": 1e-9,
    "bc 1e-9 below bd": -1e-9,
    "bc 5e-7 above bd": 5e-7,
}


@pytest.mark.parametrize("hurdle", (False, True), ids=("no hurdle", "hurdle, z=1"))
@pytest.mark.parametrize("case", CYCLING_GAP)
def test_unmoded_hold_with_battery_cycling_noise_is_published_as_hold(
    monkeypatch, case, hurdle
):
    # PV 2, load 2: the solver holds. Discharge direction (`battery_mode` at 9e-7): the
    # PV charges the battery 5e-6 kWh (plus the gap on `bc`, the idle flow) while the
    # battery feeds the load 5e-6. The zeroed `bc` exceeds the working `bd` by the gap
    # and `gout` is not idle (hurdle off, or the export indicator active). The gap's
    # sign used to decide whether the slot publishes (`simultaneous battery directions`).
    gap = CYCLING_GAP[case]
    problem = unmoded_problem(
        1.0, 0.1, 2.0, 2.0, BIG_BATTERY_20, hurdle=hurdle, terminal=0.5
    )
    clean = solve(problem).flows[0]
    assert machine_state(problem.slots[0], clean) == "HOLD"

    def noise(v, values):
        values[v["battery_mode"]] = 9e-7
        if hurdle:
            values[v["export_active"]] = 9e-7
            values[v["export_side"]] = 1 - 9e-7
        move(values, v, 5e-6, ("bc", "bd", "pv_battery", "battery_load"), ("pv_load",))
        move(values, v, gap, ("bc", "pv_battery"), ())
        values[v["energy"]] += gap

    plan = solve_with_noise(monkeypatch, problem, noise)

    (flow,) = plan.flows
    assert machine_state(problem.slots[0], flow) == "HOLD"
    assert flow.charge_kwh == 0.0
    assert flow.discharge_kwh <= 1e-8
    assert flow.grid_export_kwh <= 1e-6
    assert flow.grid_import_kwh == pytest.approx(clean.grid_import_kwh, abs=1e-8)
    assert flow.end_soc_kwh == pytest.approx(clean.end_soc_kwh, abs=1e-8)
    assert plan.new_export_episodes == 0


def test_unmoded_settle_leaves_the_balance_residual_the_discharge_cannot_cover_on_gout():
    # `gout` idle, `bd` 1e-9 short of what is taken off `gout`: `bd` to 0, the residual on
    # `gout`.
    flow = unmoded_slot_flow(gout=1e-5 + 1e-9, bd=1e-5)

    settled = _settle(("bc", "gout"), flow, {"bc": 20.0, "gout": 60.0}, moded=False)

    assert settled["bd"] == 0.0
    assert settled["gout"] == pytest.approx(1e-9, abs=1e-15)


BC_ABOVE_WORKING_BD = {
    "1e-9 above": 1e-9,
    "5e-7 above": 5e-7,
    "9e-7 above": 9e-7,
    "1e-6 above": 1e-6,
}


@pytest.mark.parametrize("case", BC_ABOVE_WORKING_BD)
def test_unmoded_settle_leaves_the_balance_residual_a_zeroed_bc_leaves_on_gout(case):
    # `gout` is not idle: the zeroed `bc` (battery cycling noise) exceeds the working `bd`
    # by the balance tolerance. `bd` to 0, the residual on `gout`.
    gap = BC_ABOVE_WORKING_BD[case]
    flow = unmoded_slot_flow(bc=5e-6 + gap, bd=5e-6)

    settled = _settle(("bc",), flow, {"bc": 20.0}, moded=False)

    assert settled["bc"] == 0.0
    assert settled["bd"] == 0.0
    assert settled["gout"] == pytest.approx(gap, abs=1e-15)
    assert settled["gin"] == 0.0


def test_unmoded_settle_adds_the_residual_to_the_gout_already_present():
    flow = unmoded_slot_flow(bc=3e-6, gout=9e-7, bd=2.5e-6)

    settled = _settle(("bc", "gout"), flow, {"bc": 20.0, "gout": 60.0}, moded=False)

    assert settled["bd"] == 0.0
    assert settled["gout"] == pytest.approx(1.4e-6, abs=1e-15)


def test_unmoded_settle_refuses_a_shortfall_beyond_the_balance_tolerance():
    flow = unmoded_slot_flow(gout=1e-5 + 1e-5, bd=1e-5)

    assert (
        _settle(("bc", "gout"), flow, {"bc": 20.0, "gout": 60.0}, moded=False) is None
    )


def test_moded_settle_still_refuses_what_the_discharge_cannot_absorb():
    flow = unmoded_slot_flow(gout=1e-5 + 1e-9, bd=1e-5)

    assert _settle(("gout",), flow, {"gout": 60.0}, moded=True) is None


def test_unmoded_export_pair_the_settle_cannot_clear_is_refused(monkeypatch):
    # The discharge noise passes the validator ahead of the settle (it is within the
    # bound of `bd`), but z at 1 says `gout` is the gated flow, and a real 4 kWh export
    # is not noise: the settle clears neither side and the pair is no plan.
    problem = export_hurdle_problem(1.0)

    def noise(v, values):
        values[v["battery_mode"]] = 0.0
        values[v["export_active"]] = 9e-7
        values[v["export_side"]] = 1 - 9e-7
        move(values, v, 1.5e-5, ("bd", "battery_grid", "gout"), ())
        values[v["energy"]] -= 1.5e-5

    with pytest.raises(SolveError, match="physical export indicator"):
        solve_with_noise(monkeypatch, problem, noise)


def pv_charge_problem():
    # PV 10, load 2, battery 20 kWh per slot: the solver charges 8 kWh from PV.
    return unmoded_problem(
        1.0, 0.1, 10.0, 2.0, BIG_BATTERY_20, hurdle=True, terminal=1.0
    )


def charging_indicators(v, values, side):
    # Charge direction (`battery_mode` a hair off 1), export indicator a hair off 0.
    values[v["battery_mode"]] = 1 - 9e-7
    values[v["export_active"]] = 9e-7
    values[v["export_side"]] = side


def battery_to_grid(v, values, extra=0.0):
    # 1e-5 kWh of discharge exported, the battery energy down by it.
    move(values, v, 1e-5, ("bd", "battery_grid", "gout"), ())
    values[v["energy"]] -= 1e-5
    values[v["gout"]] += extra


def pv_leak_to_grid(v, values):
    # 5e-6 kWh of the PV surplus exported instead of charged.
    move(values, v, 5e-6, ("pv_grid", "gout"), ("pv_battery", "bc"))
    values[v["energy"]] -= 5e-6


# `export_side` z, the `gout` residual beyond the discharge noise, and the PV leak: the
# idle set names `bd` (charge direction) and, for z above 0.5, `gout` (the export pair).
# The pair is satisfied by `bd = 0`, so `gout` is not zeroed and the sign of a 1e-9
# residual does not decide whether the slot publishes (T-063; probe T-052/probe2.py).
CHARGING_NOISE = {
    "z=1, bd = gout": (1 - 9e-7, 0.0, False),
    "z=1, gout 1e-9 above bd": (1 - 9e-7, 1e-9, False),
    "z=1, gout 1e-9 below bd": (1 - 9e-7, -1e-9, False),
    "z=1, gout 5e-6 PV leak above bd": (1 - 9e-7, 0.0, True),
    "z=0, gout 5e-6 PV leak above bd": (9e-7, 0.0, True),
    "z=0, gout 1e-9 above bd": (9e-7, 1e-9, False),
}


@pytest.mark.parametrize("case", CHARGING_NOISE)
def test_unmoded_charging_slot_with_discharge_and_export_noise_is_published(
    monkeypatch, case
):
    side, extra, leak = CHARGING_NOISE[case]
    problem = pv_charge_problem()
    slot = problem.slots[0]
    clean = solve(problem).flows[0]
    assert machine_state(slot, clean) == "CHARGE_PV"

    def noise(v, values):
        charging_indicators(v, values, side)
        battery_to_grid(v, values, extra)
        if leak:
            pv_leak_to_grid(v, values)

    plan = solve_with_noise(monkeypatch, problem, noise)

    (flow,) = plan.flows
    assert machine_state(slot, flow) == "CHARGE_PV"
    assert flow.discharge_kwh == 0.0
    assert flow.grid_import_kwh == 0.0
    assert flow.grid_export_kwh <= 5e-6 + 1e-12
    assert flow.charge_kwh == pytest.approx(clean.charge_kwh, abs=1e-5)
    # The solver's balance row is itself out by the injected 1e-9 residual.
    assert slot.pv_kwh + flow.grid_import_kwh + flow.discharge_kwh == pytest.approx(
        slot.load_kwh + flow.grid_export_kwh + flow.charge_kwh, abs=2e-9
    )
    assert flow.end_soc_kwh == pytest.approx(
        BIG_BATTERY_20.initial_kwh + flow.charge_kwh, abs=1e-12
    )
    assert plan.new_export_episodes == 0


@pytest.mark.parametrize("power_kw", [0.001, 0.1])
def test_unmoded_discharge_noise_the_classifier_reads_as_zero_is_not_a_pair(
    monkeypatch, power_kw
):
    # 5e-7 kWh of discharge, below the 1e-6 the classifier calls zero, beside an 8 kWh PV
    # export on a full battery and an inactive indicator. With a power floor of 1 W the
    # activity tolerance is 1e-7 and `settle` leaves the flow (it is not above 1e-6), so
    # the post-settle pair check read a pair and refused a slot the classifier reads HOLD.
    slot = Slot(HOUR, HOUR + timedelta(hours=1), 0.5, 0.3, 10.0, 2.0)
    problem = replace(
        unmoded_problem(
            0.5,
            0.3,
            10.0,
            2.0,
            replace(BIG_BATTERY_20, initial_kwh=36.0),
            hurdle=True,
            terminal=1.0,
        ),
        minimum_mode_power_kw=power_kw,
    )
    clean = solve(problem).flows[0]
    assert machine_state(slot, clean) == "HOLD"

    def noise(v, values):
        values[v["battery_mode"]] = 9e-7
        values[v["export_active"]] = values[v["export_side"]] = 0.0
        move(values, v, 5e-7, ("bd", "battery_grid", "gout"), ())
        values[v["energy"]] -= 5e-7

    plan = solve_with_noise(monkeypatch, problem, noise)

    (flow,) = plan.flows
    assert machine_state(slot, flow) == "HOLD"
    assert flow.grid_export_kwh == pytest.approx(clean.grid_export_kwh, abs=1e-6)
    assert plan.new_export_episodes == 0


def unmoded_slot_flow(**flow):
    return {"gin": 0.0, "gout": 0.0, "curt": 0.0, "bc": 0.0, "bd": 0.0, **flow}


# A direction the idle row zeroes that the working direction cannot give back: the working
# flow is smaller than the noise (1e-6 < flow, noise <= 1e-6 * (2M + 1) = 4.1e-5 at M = 20).
SMALLER_THAN_NOISE = {
    "charge below the discharge noise": (
        ("bd",),
        unmoded_slot_flow(bc=5e-6, bd=1e-5),
    ),
    "discharge below the charge noise": (
        ("bc",),
        unmoded_slot_flow(bc=1e-5, bd=5e-6),
    ),
}


@pytest.mark.parametrize("case", SMALLER_THAN_NOISE)
def test_unmoded_settle_leaves_a_slot_whose_working_direction_cannot_give_back(case):
    idle, flow = SMALLER_THAN_NOISE[case]
    upper = {name: 20.0 for name in idle}

    assert _settle(idle, flow, upper, moded=False) is None


@pytest.mark.parametrize("case", SMALLER_THAN_NOISE)
def test_unmoded_slot_the_settle_cannot_close_is_refused_not_published(case):
    idle, flow = SMALLER_THAN_NOISE[case]
    problem = unmoded_problem(1.0, 0.1, 0.0, 0.0, BIG_BATTERY_20, hurdle=False)
    names = ("gin", "gout", "curt", "bc", "bd", "energy", "battery_mode")
    vectors = [{name: i for i, name in enumerate(names)}]
    values = np.array(
        [
            *(flow[name] for name in names[:5]),
            BIG_BATTERY_20.initial_kwh,
            1.0 if idle == ("bd",) else 0.0,
        ]
    )
    upper = [20.0] * len(names)

    settled = settle_mode_noise(problem, vectors, values, upper)

    # Nothing was settled, so both directions stay above the 1e-6 the classifier reads.
    assert min(settled[0]["bc"], settled[0]["bd"]) > 1e-6
    with pytest.raises(SolveError, match="simultaneous battery directions"):
        _validate_settled(problem, settled)


def test_validate_settled_holds_the_published_rows_to_one_direction():
    problem = unmoded_problem(1.0, 0.1, 0.0, 0.0, BIG_BATTERY_20, hurdle=False)
    clean = {"gin": 1.0, "gout": 0.0, "bc": 2.0, "bd": 1e-6, "curt": 0.0, "energy": 0.0}

    _validate_settled(problem, [clean])
    with pytest.raises(SolveError, match="simultaneous battery directions"):
        _validate_settled(problem, [{**clean, "bd": 2e-6}])
    with pytest.raises(SolveError, match="simultaneous grid directions"):
        _validate_settled(problem, [{**clean, "gout": 2e-6}])


# T-052: battery limit L kWh per hourly slot, site limits 3L, capacity 30 kWh, load and PV
# scaled by L / 3.3 (the T-049 sweep): limit, seed, slots and PV scale (which already
# carries L / 3.3) of the failing problems.
BIG_BATTERY = {
    "L10 seed 170 export indicator": (10, 170, 48, 11.108974005045065),
    "L15 seed 116 export indicator": (15, 116, 48, 16.663461007567598),
    "L15 seed 125 simultaneous directions": (15, 125, 48, 6.319934510705097),
    "L15 seed 133 export indicator": (15, 133, 48, 2.396955362482144),
    "L15 seed 198 export indicator": (15, 198, 24, 1.1584318051846672),
    "L20 seed 111 export indicator": (20, 111, 48, 1.9682142293196627),
    "L20 seed 135 export indicator": (20, 135, 48, 3.195940483309525),
}


@pytest.mark.parametrize("case", BIG_BATTERY)
def test_a_battery_of_10_to_20_kwh_per_slot_publishes_a_plan_on_real_highs(case):
    limit, seed, count, pv_scale = BIG_BATTERY[case]
    base = corpus_problem(seed, pv_scale, count)
    capacity = 30.0
    problem = replace(
        base,
        battery=replace(
            base.battery,
            capacity_kwh=capacity,
            charge_kw=limit,
            discharge_kw=limit,
            initial_kwh=base.battery.initial_kwh * capacity / 13.5,
        ),
        site=SiteLimits(3 * limit, 3 * limit, 3 * limit, True),
        slots=tuple(
            replace(slot, load_kwh=slot.load_kwh * limit / 3.3) for slot in base.slots
        ),
    )

    plan = solve(problem, time_limit_s=20.0, accept_incumbent=True)

    for slot, flow in zip(problem.slots, plan.flows, strict=True):
        assert flow.dispatch_mode == machine_state(slot, flow)
        assert min(flow.grid_import_kwh, flow.grid_export_kwh) <= 1e-6


# T-080: the charge direction of the battery cycling noise (the mirror of T-075).
BD_GAP_WITHIN_TOLERANCE = {
    "bd equal to bc": 0.0,
    "bd 1e-9 above bc": 1e-9,
    "bd 1e-9 below bc": -1e-9,
    "bd 5e-7 above bc": 5e-7,
    "bd 1e-6 above bc": 1e-6,
}
# The end-to-end slot keeps the site balance closed inside the validator's own tolerance.
BD_GAP_IN_THE_BALANCE = {k: g for k, g in BD_GAP_WITHIN_TOLERANCE.items() if g < 1e-6}


@pytest.mark.parametrize("gap", BD_GAP_WITHIN_TOLERANCE.values(), ids=list(BD_GAP_WITHIN_TOLERANCE))
def test_unmoded_settle_gives_the_charge_back_and_imports_the_residual_a_zeroed_bd_leaves(
    gap,
):
    # The zeroed `bd` (battery cycling noise) exceeds the working `bc` by the balance
    # tolerance: `bc` to 0, the residual on `gin`.
    flow = unmoded_slot_flow(bc=5e-6, bd=5e-6 + gap)

    settled = _settle(("bd",), flow, {"bd": 20.0}, moded=False)

    # A `bd` short of `bc` gives only that much back: `bc` keeps the difference.
    assert settled["bd"] == 0.0
    assert settled["bc"] == pytest.approx(max(-gap, 0.0), abs=1e-15)
    assert settled["gin"] == pytest.approx(max(gap, 0.0), abs=1e-15)
    assert settled["gout"] == 0.0


def test_unmoded_settle_adds_the_charge_residual_to_the_gin_already_present():
    flow = unmoded_slot_flow(bc=2.5e-6, bd=3e-6, gin=9e-7)

    settled = _settle(("bd",), flow, {"bd": 20.0}, moded=False)

    assert settled["bc"] == 0.0
    # The import already present is not cut first: only `gout` is netted off a shortfall.
    assert settled["gin"] == pytest.approx(9e-7 + 5e-7, abs=1e-15)


def test_unmoded_settle_refuses_a_charge_shortfall_beyond_the_balance_tolerance():
    flow = unmoded_slot_flow(bc=5e-6, bd=5e-6 + 2e-6)

    assert _settle(("bd",), flow, {"bd": 20.0}, moded=False) is None


@pytest.mark.parametrize("hurdle", (False, True), ids=("no hurdle", "hurdle, z=1"))
@pytest.mark.parametrize(
    "gap", BD_GAP_IN_THE_BALANCE.values(), ids=list(BD_GAP_IN_THE_BALANCE)
)
def test_unmoded_hold_with_charge_direction_cycling_noise_is_published_as_hold(
    monkeypatch, gap, hurdle
):
    # PV 2, load 2: the solver holds. Charge direction (`battery_mode` at 1 - 9e-7): the
    # PV charges the battery 5e-6 kWh while the battery feeds the load 5e-6 plus the gap
    # on `bd`, the idle flow. The gap's sign used to decide whether the slot published
    # (`simultaneous battery directions`).
    problem = unmoded_problem(
        1.0, 0.1, 2.0, 2.0, BIG_BATTERY_20, hurdle=hurdle, terminal=0.5
    )
    clean = solve(problem).flows[0]
    assert machine_state(problem.slots[0], clean) == "HOLD"

    def noise(v, values):
        values[v["battery_mode"]] = 1 - 9e-7
        if hurdle:
            values[v["export_active"]] = 9e-7
            values[v["export_side"]] = 1 - 9e-7
        move(values, v, 5e-6, ("bc", "bd", "pv_battery", "battery_load"), ("pv_load",))
        move(values, v, gap, ("bd", "battery_load"), ())
        values[v["energy"]] -= gap

    plan = solve_with_noise(monkeypatch, problem, noise)

    (flow,) = plan.flows
    assert machine_state(problem.slots[0], flow) == "HOLD"
    assert flow.charge_kwh <= 1e-8
    assert flow.discharge_kwh == 0.0
    assert flow.grid_export_kwh == pytest.approx(clean.grid_export_kwh, abs=1e-8)
    assert flow.grid_import_kwh == pytest.approx(clean.grid_import_kwh, abs=1e-6)
    assert flow.end_soc_kwh == pytest.approx(clean.end_soc_kwh, abs=1e-6)
    assert plan.new_export_episodes == 0


@pytest.mark.parametrize("hurdle", (False, True), ids=("no hurdle", "hurdle, z=1"))
def test_unmoded_charge_direction_cycling_noise_beyond_the_tolerance_is_refused(
    monkeypatch, hurdle
):
    # `bd` 5e-6 above the working `bc`, the surplus load balanced by curtailed PV so the
    # site balance holds: a real simultaneous direction, not settled.
    problem = unmoded_problem(
        1.0, 0.1, 2.0, 2.0, BIG_BATTERY_20, hurdle=hurdle, terminal=0.5
    )

    def noise(v, values):
        values[v["battery_mode"]] = 1 - 9e-7
        if hurdle:
            values[v["export_active"]] = 9e-7
            values[v["export_side"]] = 1 - 9e-7
        move(values, v, 5e-6, ("bc", "bd", "pv_battery", "battery_load"), ("pv_load",))
        move(values, v, 5e-6, ("bd", "battery_load", "curt"), ("pv_load",))
        values[v["energy"]] -= 5e-6

    with pytest.raises(SolveError, match="simultaneous battery directions"):
        solve_with_noise(monkeypatch, problem, noise)
