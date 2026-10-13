"""Seeded sweep of export-capped problems with PV gaps and carried modes (T-037).

A rule that lets the solver out of a constraint is judged by what it does to the
problems around it (LESSONS.md, tightened-escape-exposes-neighbour-gap). Run the same
seeds on two checkouts and compare:

    python benchmarks/curtail_sweep.py run 11 300 /tmp/new_11.json    # this branch
    python benchmarks/curtail_sweep.py run 11 300 /tmp/main_11.json   # a checkout of main
    python benchmarks/curtail_sweep.py compare /tmp/main_11.json /tmp/new_11.json

`compare` prints the problems that were planned before and are infeasible now
(`NEWLY INFEASIBLE`), the ones that got worse (objective up by more than 1e-6) and a
count of each outcome. T-037 ran seeds 11, 22 and 33 with 300 problems each.
Invoked by path, like the other benchmarks: the repository root must be on PYTHONPATH.
"""

import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from custom_components.energy_compass.engine.models import (
    Battery,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import solve

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


def run(problem):
    try:
        plan = solve(problem)
    except SolveError as error:
        return ["err", str(error)[:40]]
    return [
        "ok",
        round(plan.objective, 6),
        [flow.dispatch_mode for flow in plan.flows],
        list(plan.carried_bridges),
    ]


def compare(before_path, after_path):
    before = json.loads(Path(before_path).read_text())
    after = json.loads(Path(after_path).read_text())
    counts = {
        "newly_infeasible": 0,
        "newly_planned": 0,
        "worse": 0,
        "better": 0,
        "same": 0,
    }
    for index, (a, b) in enumerate(zip(before, after, strict=True)):
        if a[0] == "ok" and b[0] != "ok":
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


if __name__ == "__main__":
    if sys.argv[1] == "compare":
        compare(sys.argv[2], sys.argv[3])
    else:
        rng = random.Random(int(sys.argv[2]))
        out = [run(make(rng)) for _ in range(int(sys.argv[3]))]
        Path(sys.argv[4]).write_text(json.dumps(out))
        print(sum(row[0] == "ok" for row in out), "ok of", len(out))
