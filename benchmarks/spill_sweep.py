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

The draw is `curtail_sweep.make` with curtailment forced off and the minimum mode
duration 0, 15 or 60 minutes; `negrun` draws sell prices below zero and `probe` plans a
flexible load on top. The export episode hurdle is zero: with it the model trades real money
against the hurdle (a plan with fewer export episodes may cost more), and `compare` reads
money. Invoked by path, like the other benchmarks: the repository root
must be on PYTHONPATH.
"""

import json
import random
import sys
from dataclasses import replace
from pathlib import Path

import curtail_sweep as sweep


def make(rng, negative=False, moded=False):
    problem = sweep.make_negative(rng) if negative else sweep.make(rng)
    return make_off(problem, rng, moded)


def draws(kind, seed, count):
    rng = random.Random(seed)
    if kind == "probe":
        out = []
        for _ in range(count):
            problem, request = sweep.make_probe(rng)
            out.append(sweep.run(make_off(problem, rng), request))
        return out
    if kind in ("roll", "negroll"):
        return [
            sweep.roll(make(rng, kind == "negroll", moded=True)) for _ in range(count)
        ]
    return [sweep.run(make(rng, kind == "negrun")) for _ in range(count)]


def make_off(problem, rng, moded=False):
    """Curtailment off, and a minimum mode duration of 15 or 60 minutes (also 0 unless `moded`)."""
    minutes = rng.choice((15, 60) if moded else (0, 15, 60))
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
    else:
        out = draws(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
        Path(sys.argv[4]).write_text(json.dumps(out))
        print(sum(row[0] == "ok" for row in out), "ok of", len(out))
