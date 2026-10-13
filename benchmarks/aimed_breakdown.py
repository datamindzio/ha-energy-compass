"""What the aimed PV-follow draws (`curtail_sweep.py aimrun`) do on the checkout it runs in (T-070).

    python benchmarks/aimed_breakdown.py 400 101 202 303 404      # count, then seeds

Three measures, named the way docs/model.md and ADR-0004 quote them (all on first-boundary problems):

- `rungs`: which rung of `solve_released` plans the draw (`first`: the commitment and the terminal rule
  hold; `reserve_hold`; `uncommitted`: rung 3, the commitment is dropped; `none`: no plan on any rung),
  and the first mode of the rung 3 plans (a PV-follow mode there would be the commitment kept);
- `class on rung 1`: the plain solve has no plan with the commitment, has one without it and has one
  with the value terminal instead of `preserve_initial`. This is what T-065 counted (213 on main
  0778580, 126 on its head). A draw of the class is planned by rung 3 now, but its first interval
  leaves the commitment;
- `no plan on any rung`: the rest of the draws, with the facts that separate them (a plan once the
  minimum duration, the terminal rule or curtailment changes, where the pack starts, and the forced
  surplus profile: curtailment off, an export cap of 0.5 kW or less, an interval whose PV is above the
  load plus the cap, which only a charge or a curtailment can take).

The engine is whatever is on PYTHONPATH, so the same command on two checkouts compares them. Invoked by
path, like the other benchmarks: the repository root must be on PYTHONPATH.
"""

import random
import sys
from collections import Counter
from dataclasses import replace

import curtail_sweep as sweep

from custom_components.energy_compass.engine.models import SolveError
from custom_components.energy_compass.engine.optimize import solve


def has_plan(problem):
    try:
        solve(problem)
    except SolveError as error:
        if error.reason != "infeasible":
            raise
        return False
    return True


def uncommitted(problem):
    return replace(
        problem, initial_dispatch_mode=None, initial_dispatch_mode_since=None
    )


def forced_surplus(problem):
    """Curtailment off, an export cap of 0.5 kW or less and an interval with PV above load + cap."""
    site = problem.site
    return (
        not site.allow_curtailment
        and site.grid_export_kw <= 0.5
        and any(
            slot.pv_kwh - slot.load_kwh - site.grid_export_kw * 0.25 > 1e-9
            for slot in problem.slots
        )
    )


def draw_rows(count, seeds):
    rows = []
    for seed in seeds:
        rng = random.Random(seed)
        for draw in range(count):
            problem = sweep.make_aimed(rng)
            battery = problem.battery
            rung, first_mode = sweep.rung_and_mode(problem)
            row = {
                "seed": seed,
                "draw": draw,
                "rung": rung,
                "first_mode": first_mode,
                "forced": forced_surplus(problem),
                "plain": has_plan(problem),
                "curtail": problem.site.allow_curtailment,
                "below_ceiling": round(
                    battery.capacity_kwh * battery.maximum_soc_fraction
                    - battery.initial_kwh,
                    3,
                ),
                "above_reserve": round(
                    battery.initial_kwh
                    - battery.capacity_kwh * battery.minimum_soc_fraction,
                    3,
                ),
            }
            if not row["plain"]:
                free = uncommitted(problem)
                row["free"] = has_plan(free)
                row["value"] = has_plan(replace(problem, terminal_mode="value"))
            if row["rung"] == "none":
                row["neither"] = has_plan(
                    replace(uncommitted(problem), terminal_mode="value")
                )
                row["no_dwell"] = has_plan(replace(problem, minimum_mode_minutes=0))
                row["curtail_on"] = has_plan(
                    replace(problem, site=replace(problem.site, allow_curtailment=True))
                )
            rows.append(row)
    return rows


def report(rows):
    n = len(rows)
    print("draws", n, "| rungs", dict(sorted(Counter(r["rung"] for r in rows).items())))
    print(
        "first mode of the rung 3 plans:",
        dict(
            sorted(
                Counter(
                    r["first_mode"] for r in rows if r["rung"] == "uncommitted"
                ).items()
            )
        ),
    )
    bad = [r for r in rows if not r["plain"]]
    free = [r for r in bad if r["free"]]
    klass = [r for r in free if r["value"]]
    print(
        "class on rung 1: no plan with the commitment",
        len(bad),
        "| a plan without it",
        len(free),
        "| a plan with the value terminal",
        sum(r["value"] for r in bad),
        "| both (the class)",
        len(klass),
        "| e.g.",
        [(r["seed"], r["draw"]) for r in klass[:6]],
    )
    none = [r for r in rows if r["rung"] == "none"]
    print(
        "no plan on any rung:",
        len(none),
        "| per seed",
        dict(sorted(Counter(r["seed"] for r in none).items())),
    )
    for key, label in (
        ("value", "a plan with the value terminal"),
        ("neither", "a plan with neither commitment nor terminal"),
        ("no_dwell", "a plan with minimum_mode_minutes=0"),
        ("curtail_on", "a plan with allow_curtailment true"),
    ):
        print("   ", label + ":", sum(r[key] for r in none))
    print(
        "    allow_curtailment false:",
        sum(not r["curtail"] for r in none),
        "of the",
        len(none),
        "|",
        sum(not r["curtail"] for r in rows),
        "of all",
        n,
    )
    print(
        "    forced surplus profile:",
        sum(r["forced"] for r in none),
        "of the",
        len(none),
        "|",
        sum(r["forced"] for r in rows),
        "of all",
        n,
        "| no plan at all, e.g.",
        [(r["seed"], r["draw"]) for r in none[:8]],
    )
    print(
        "    pack within 0.3 kWh of its ceiling:",
        sum(r["below_ceiling"] <= 0.3 + 1e-9 for r in none),
        "| within 0.3 kWh of the reserve:",
        sum(r["above_reserve"] <= 0.3 + 1e-9 for r in none),
        "| neither:",
        sum(
            r["below_ceiling"] > 0.3 + 1e-9 and r["above_reserve"] > 0.3 + 1e-9
            for r in none
        ),
    )


if __name__ == "__main__":
    report(draw_rows(int(sys.argv[1]), [int(seed) for seed in sys.argv[2:]]))
