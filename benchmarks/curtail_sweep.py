"""Seeded sweep of export-capped problems with PV gaps and carried modes (T-037).

A rule that lets the solver out of a constraint is judged by what it does to the
problems around it (LESSONS.md, tightened-escape-exposes-neighbour-gap). Run the same
seeds on two checkouts and compare:

    python benchmarks/curtail_sweep.py run 11 300 /tmp/new_11.json    # this branch
    python benchmarks/curtail_sweep.py run 11 300 /tmp/main_11.json   # a checkout of main
    python benchmarks/curtail_sweep.py compare /tmp/main_11.json /tmp/new_11.json

`roll` follows each plan the way the coordinator does (one interval, carry the
commitment and the pack, plan again) and `rollcompare` prints the problems that were
planned all the way before and are infeasible at some boundary now: a plan that is
there at the first boundary and gone at the next is no plan to the user.

`compare` prints the problems that were planned before and are infeasible now
(`NEWLY INFEASIBLE`), the ones that got worse (objective up by more than 1e-6), the
ones where the solver result is rejected that were not before (`NEW SOLVER FAILURE`)
and a count of each outcome. T-037 ran seeds 11, 22 and 33 with 300 problems each.

T-047 adds the same problems with a sell price that may fall below zero (`negrun`,
`negroll`) and with a flexible-load probe on top (`probe`, which plans through
`solve_flexible_load`: its plan is certified against the probe's load). The base seeds
draw sell prices from 0 to 0.8 and never plan a probe, so they cannot show a rule that
reads a slot's load.
Invoked by path, like the other benchmarks: the repository root must be on PYTHONPATH.
"""

import json
import random
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from custom_components.energy_compass.engine.dispatch_policy import dwell_group
from custom_components.energy_compass.engine.models import (
    Battery,
    FlexibleLoadRequest,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import solve, solve_flexible_load

START = datetime(2026, 12, 18, 11, tzinfo=UTC)
Q = timedelta(minutes=15)
MODES = (None, "CHARGE_PV", "SELF_CONSUME", "HOLD", "CURTAIL", "CHARGE_PV", "CURTAIL")


def make(rng):
    n = rng.randint(4, 16)
    peak = rng.choice((0.4, 0.9, 1.5, 2.2))
    pv = []
    for i in range(n):
        v = round(max(0, peak * rng.uniform(0.6, 1)), 2)
        pv.append(v)
    for _ in range(rng.choice((0, 1, 2, 3))):  # gaps
        a = rng.randrange(n)
        for k in range(a, min(n, a + rng.randint(1, 6))):
            pv[k] = rng.choice((0, 0, 0.01, 0.02, 0.05, 0.1))
    slots = tuple(
        Slot(
            START + i * Q,
            START + (i + 1) * Q,
            round(rng.uniform(0.2, 1.5), 2),
            round(rng.uniform(0, 0.8), 2),
            pv[i],
            rng.choice((0.02, 0.05, 0.1, 0.2, 0.4)),
        )
        for i in range(n)
    )
    cap = rng.choice((5, 10))
    reserve = rng.choice((0, 0.1))
    initial = rng.choice(
        (
            cap,
            cap - 0.01,
            cap - 0.05,
            cap - 0.1,
            cap - 0.3,
            cap - 1,
            round(rng.uniform(reserve * cap, cap), 2),
        )
    )
    battery = Battery(
        cap,
        reserve,
        1,
        initial,
        rng.choice((1, 3, 4)),
        rng.choice((1, 3, 4)),
        0.95,
        0.95,
        rng.choice((0, 0.01)),
        rng.random() < 0.5,
        rng.random() < 0.5,
        rng.choice((0, 0, 0.02, 0.05)),
    )
    mode = rng.choice(MODES)
    kw = {}
    if mode:
        kw = {
            "initial_dispatch_mode": mode,
            "initial_dispatch_mode_since": START
            - timedelta(minutes=rng.choice((0, 1, 15, 16, 29, 45))),
        }
    return Problem(
        slots,
        SiteLimits(
            rng.choice((3, 8)),
            8,
            rng.choice((0.5, 1, 2, 8)),
            rng.random() < 0.9,
        ),
        battery,
        rng.choice(("value", "preserve_initial")),
        rng.choice((0, 0.3)),
        (),
        "UTC",
        minimum_mode_minutes=rng.choice((30, 60, 120)),
        **kw,
    )


def make_negative(rng):
    """The same problem with sell prices from below zero to 0.8 (the base draw never is)."""
    problem = make(rng)
    slots = tuple(
        replace(slot, sell_per_kwh=round(rng.uniform(-0.6, 0.8), 2))
        for slot in problem.slots
    )
    return replace(problem, slots=slots)


def make_probe(rng):
    """A problem with negative sell prices and a flexible load that fits the horizon."""
    problem = make_negative(rng)
    power = rng.choice((0.5, 1, 2, 4))
    room = power * 0.25 * len(problem.slots)
    energy = round(min(rng.choice((0.25, 0.5, 1, 2, 4)), 0.8 * room), 2)
    return problem, FlexibleLoadRequest(energy, power)


def run(problem, request=None):
    try:
        plan = (
            solve(problem)
            if request is None
            else solve_flexible_load(problem, request).plan
        )
    except SolveError as error:
        return ["err", str(error)[:40]]
    return [
        "ok",
        round(plan.objective, 6),
        [flow.dispatch_mode for flow in plan.flows],
        list(plan.carried_bridges),
    ]


def roll(problem, steps=8):
    """Plan, follow the first interval, carry the commitment and the pack, plan again."""
    commitment = None
    if problem.initial_dispatch_mode:
        commitment = (
            problem.initial_dispatch_mode,
            problem.initial_dispatch_mode_since,
        )
    trace = []
    battery = problem.battery
    for step in range(steps):
        if len(problem.slots) < 2:
            break
        try:
            plan = solve(problem)
        except SolveError as error:
            return ["err", step, str(error)[:40], trace]
        flow = plan.flows[0]
        mode = flow.dispatch_mode
        trace.append(mode + ("*" if 0 in plan.carried_bridges else ""))
        if 0 in plan.carried_bridges:
            pass  # a bridge row leaves the commitment and its clock as they are
        elif commitment and dwell_group(commitment[0]) == dwell_group(mode):
            commitment = (mode, commitment[1])
        else:
            commitment = (mode, problem.slots[0].start)
        low = battery.capacity_kwh * battery.minimum_soc_fraction
        high = battery.capacity_kwh * battery.maximum_soc_fraction
        problem = replace(
            problem,
            slots=problem.slots[1:],
            battery=replace(
                problem.battery, initial_kwh=min(max(flow.end_soc_kwh, low), high)
            ),
            initial_dispatch_mode=commitment[0],
            initial_dispatch_mode_since=commitment[1],
        )
    return ["ok", len(trace), "", trace]


def roll_compare(before_path, after_path):
    before = json.loads(Path(before_path).read_text())
    after = json.loads(Path(after_path).read_text())
    counts = {"newly_infeasible_while_followed": 0, "fixed_while_followed": 0}
    for index, (a, b) in enumerate(zip(before, after, strict=True)):
        if a[0] == "ok" and b[0] != "ok":
            counts["newly_infeasible_while_followed"] += 1
            print("NEWLY INFEASIBLE WHILE FOLLOWED", index, b)
        elif a[0] != "ok" and b[0] == "ok":
            counts["fixed_while_followed"] += 1
    print(counts)


def rejected(row):
    return row[0] == "err" and "invalid solver result" in row[1]


def compare(before_path, after_path):
    before = json.loads(Path(before_path).read_text())
    after = json.loads(Path(after_path).read_text())
    counts = {
        "newly_infeasible": 0,
        "newly_planned": 0,
        "new_solver_failure": 0,
        "worse": 0,
        "better": 0,
        "same": 0,
    }
    for index, (a, b) in enumerate(zip(before, after, strict=True)):
        if rejected(b) and not rejected(a):
            # A plan the solver found and the certificate refused: not "infeasible".
            counts["new_solver_failure"] += 1
            print("NEW SOLVER FAILURE", index, a[:2], b)
        elif a[0] == "ok" and b[0] != "ok":
            counts["newly_infeasible"] += 1
            print("NEWLY INFEASIBLE", index, b)
        elif a[0] != "ok" and b[0] == "ok":
            counts["newly_planned"] += 1
        elif a[0] == "ok":
            delta = b[1] - a[1]
            if delta > 1e-6:
                counts["worse"] += 1
                print("worse", index, delta)
            elif delta < -1e-6:
                counts["better"] += 1
            else:
                counts["same"] += 1
    print(counts)


def draws(kind, seed, count):
    rng = random.Random(seed)
    if kind == "probe":
        return [run(*make_probe(rng)) for _ in range(count)]
    makers = {"run": make, "negrun": make_negative}
    if kind in makers:
        return [run(makers[kind](rng)) for _ in range(count)]
    makers = {"roll": make, "negroll": make_negative}
    return [roll(makers[kind](rng)) for _ in range(count)]


if __name__ == "__main__":
    if sys.argv[1] == "compare":
        compare(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "rollcompare":
        roll_compare(sys.argv[2], sys.argv[3])
    else:
        out = draws(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
        Path(sys.argv[4]).write_text(json.dumps(out))
        if sys.argv[1] in ("roll", "negroll"):
            print(
                sum(row[0] == "ok" for row in out),
                "followed to the end, of",
                len(out),
                "; infeasible at the first boundary:",
                sum(row[0] == "err" and row[1] == 0 for row in out),
            )
        else:
            print(sum(row[0] == "ok" for row in out), "ok of", len(out))
