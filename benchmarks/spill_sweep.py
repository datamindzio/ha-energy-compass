"""Seeded sweep of sites that cannot curtail, with PV the grid or the inverter cannot pass (T-041).

A site with an export cap (or an inverter rated below the PV) and `allow_curtailment`
off, whose pack fills before the last surplus, has no plan unless the inverter's own
throttling is planned as a forced spill. A change to that rule is judged by the
problems around it (LESSONS.md, tightened-escape-exposes-neighbour-gap): run the same
seeds on two checkouts and compare, as `curtail_sweep.py` does:

    python benchmarks/spill_sweep.py run 11 300 /tmp/new_11.json
    python benchmarks/spill_sweep.py run 11 300 /tmp/main_11.json   # a checkout of main
    python benchmarks/spill_sweep.py compare /tmp/main_11.json /tmp/new_11.json

`roll` follows each plan one interval at a time, as the coordinator does, and
`rollcompare` prints the problems that were followed to the end before and are
infeasible at some boundary now (a plan that is gone at the next boundary is no plan).
`summary` prints, per minimum mode duration, how many of a run's problems are planned
(of a roll: followed, `bellroll` to the end of the day, with the stops by reason, see
`followed`): "0 newly infeasible" says nothing about what stays infeasible.

The draw is `curtail_sweep.make` with curtailment forced off and the minimum mode
duration 0, 15, 30 or 60 minutes (PV 0.6-1 times a peak in every interval: it never draws a
tapering need, a need below the minimum activity or a pack that fills inside a minimum
duration); `bell` and `bellroll` draw a bell-shaped day on the same sites, with cloud
dips, that crosses every need level; `negrun` draws sell prices below zero and `probe`
plans a flexible load on top. The export episode hurdle is zero: with it the model trades real money
against the hurdle (a plan with fewer export episodes may cost more), and `compare` reads
money. Invoked by path, like the other benchmarks: the repository root
must be on PYTHONPATH.
"""

import json
import math
import random
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import curtail_sweep as sweep


def make(rng, negative=False, moded=False):
    problem = sweep.make_negative(rng) if negative else sweep.make(rng)
    return make_off(problem, rng, moded)


def make_bell(rng, moded=False):
    """A bell-shaped day on a random site: PV tapers through every need level, with cloud dips."""
    base = sweep.make(rng)
    count = rng.randint(12, 40)
    peak = rng.choice((0.9, 1.5, 2.2))
    shape = rng.choice((1, 2))
    pv = [
        round(
            peak * math.sin(math.pi * (i + rng.uniform(0.2, 0.8)) / count) ** shape, 3
        )
        for i in range(count)
    ]
    for _ in range(rng.choice((0, 1, 2))):
        start = rng.randrange(count)
        factor = rng.choice((0, 0.2, 0.5))
        for k in range(start, min(count, start + rng.randint(1, 4))):
            pv[k] = round(pv[k] * factor, 3)
    slots = tuple(
        replace(
            base.slots[0],
            start=base.slots[0].start + i * sweep.Q,
            end=base.slots[0].start + (i + 1) * sweep.Q,
            pv_kwh=pv[i],
            buy_per_kwh=round(rng.uniform(0.2, 1.5), 2),
            sell_per_kwh=round(rng.uniform(0, 0.8), 2),
            load_kwh=rng.choice((0.02, 0.05, 0.1, 0.2, 0.4)),
        )
        for i in range(count)
    )
    return make_off(replace(base, slots=slots), rng, moded)


def tagged(problem, request=None):
    """Run a problem; the last element of its row is the minimum mode duration drawn."""
    return [*sweep.run(problem, request), problem.minimum_mode_minutes]


def draws(kind, seed, count):
    rng = random.Random(seed)
    if kind == "probe":
        out = []
        for _ in range(count):
            problem, request = sweep.make_probe(rng)
            out.append(tagged(make_off(problem, rng), request))
        return out
    if kind in ("roll", "negroll"):
        return [
            followed(make(rng, kind == "negroll", moded=True), 8) for _ in range(count)
        ]
    if kind == "bellroll":
        # To the end of the day: the taper after a long CURTAIL run is where a plan that
        # cannot be followed stops.
        return [followed(make_bell(rng, moded=True)) for _ in range(count)]
    if kind == "bell":
        return [tagged(make_bell(rng)) for _ in range(count)]
    return [tagged(make(rng, kind == "negrun")) for _ in range(count)]


def followed(problem, steps=None):
    """`curtail_sweep.roll`, with the reason of a stop and the minimum mode duration drawn.

    The first five items are the row of `curtail_sweep.roll` (`roll_compare` reads them
    by position: item 4 is the list of boundaries that contradict the plan before);
    the reason and the minutes come after them.

    A day followed to its end runs into the end of its own forecast, where a carried
    commitment may have no plan on any site: a stop is `off only` when the problem it
    stops at is planned with curtailment on (the forced spill's own gap), `any site`
    when it is not, and `start` when the first plan is already missing.
    """
    trace = []
    changed = []
    promised = None
    for step in range(steps or len(problem.slots)):
        if len(problem.slots) < 2:
            break
        try:
            plan = sweep.solve(problem)
        except sweep.SolveError as error:
            able = replace(problem, site=replace(problem.site, allow_curtailment=True))
            why = "start" if not step else "off only"
            if sweep.run(able)[0] != "ok":
                why = "any site" if step else "start, any site"
            return [
                "err",
                step,
                str(error)[:40],
                trace,
                changed,
                why,
                problem.minimum_mode_minutes,
            ]
        modes = [flow.dispatch_mode for flow in plan.flows]
        if promised is not None and promised != modes[0]:
            changed.append(step)
        promised = modes[1]
        trace.append(modes[0] + ("*" if 0 in plan.carried_bridges else ""))
        problem = sweep.follow(problem, plan)
    return ["ok", len(trace), "", trace, changed, "", problem.minimum_mode_minutes]


def summary(path):
    """Print the planned (a roll: followed) share of a run per minimum mode duration."""
    rows = json.loads(Path(path).read_text())
    planned = Counter(row[-1] for row in rows if row[0] == "ok")
    total = Counter(row[-1] for row in rows)
    for minutes in sorted(total):
        stops = Counter(
            row[5]
            for row in rows
            if len(row) == 7 and row[0] == "err" and row[-1] == minutes
        )
        detail = f" stops {dict(sorted(stops.items()))}" if stops else ""
        print(
            f"minimum_mode_minutes {minutes}: {planned[minutes]}/{total[minutes]}{detail}"
        )


def make_off(problem, rng, moded=False):
    """Curtailment off, and a minimum mode duration of 15, 30 or 60 minutes (also 0 unless `moded`)."""
    minutes = rng.choice((15, 30, 60) if moded else (0, 15, 30, 60))
    kw = (
        {"initial_dispatch_mode": None, "initial_dispatch_mode_since": None}
        if not minutes
        else {}
    )
    return replace(
        problem,
        site=replace(problem.site, allow_curtailment=False),
        minimum_mode_minutes=minutes,
        minimum_export_episode_benefit=0.0,
        **kw,
    )


if __name__ == "__main__":
    if sys.argv[1] == "compare":
        sweep.compare(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "rollcompare":
        sweep.roll_compare(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "summary":
        summary(sys.argv[2])
    else:
        out = draws(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
        Path(sys.argv[4]).write_text(json.dumps(out))
        print(sum(row[0] == "ok" for row in out), "ok of", len(out))
