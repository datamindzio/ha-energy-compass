"""Solve every matrix of a `spill_sweep.py` draw with the solver switches both ways (T-066).

HiGHS is not a proof (LESSONS.md, solver-defect-pinned-on-one-switch): a plan it calls
optimal may be dearer than a point that satisfies every row of the same matrix. A model
that publishes the default run's plan is judged by solving each matrix four ways, the
default run, `presolve=False`, `mip_rel_gap=0` and both, and counting the matrices where
the default run is dearer than another by more than the gap it asked for:

    python benchmarks/switch_audit.py probe 77 300
    python benchmarks/switch_audit.py bell 5 300

`kind` and `seed` are those of `spill_sweep.py` (`run`, `negrun`, `probe`, `bell`; the
rolling kinds solve a matrix per boundary and are not audited). Run it on two checkouts to
compare formulations; a count of zero says nothing about a rate it could not have seen
(at 2 in 7000 matrices, 7500 clean ones happen 12 % of the time). Invoked by path: the
repository root must be on PYTHONPATH.
"""

import sys

import curtail_sweep as sweep
import spill_sweep

from custom_components.energy_compass.engine import optimize

# A default run is dearer than another by more than this on a matrix of cost `best`.
ABSOLUTE = 1e-6
RELATIVE = 1e-4

SWITCHES = (
    ("presolve=False", {"presolve": False}),
    ("mip_rel_gap=0", {"mip_rel_gap": 0}),
    ("both", {"presolve": False, "mip_rel_gap": 0}),
)


def audit(kind, seed, count):
    """Return the costs of every matrix solved per draw: `[[{run: cost}, ...], ...]`."""
    matrices = []
    solve = optimize._Model._run
    plain = sweep.run

    def run(self, matrix, time_limit_s, **options):
        result = solve(self, matrix, time_limit_s, **options)
        if not options and result.status == 0 and result.fun is not None:
            costs = {"default": float(result.fun)}
            for name, switch in SWITCHES:
                other = solve(self, matrix, time_limit_s, **switch)
                costs[name] = float(other.fun) if other.status == 0 else None
            matrices.append(costs)
        return result

    def measured(problem, request=None):
        matrices.clear()
        row = plain(problem, request)
        draws.append([dict(costs) for costs in matrices])
        return row

    draws = []
    optimize._Model._run = run
    sweep.run = measured
    try:
        spill_sweep.draws(kind, seed, count)
    finally:
        optimize._Model._run = solve
        sweep.run = plain
    return draws


def dearer(costs):
    """Tell whether the default run of a matrix costs more than another run of it."""
    best = min(cost for cost in costs.values() if cost is not None)
    return costs["default"] - best > ABSOLUTE + RELATIVE * abs(best)


def main(kind, seed, count):
    draws = audit(kind, seed, count)
    found = [
        (index, run, costs)
        for index, matrices in enumerate(draws)
        for run, costs in enumerate(matrices)
        if dearer(costs)
    ]
    total = sum(len(matrices) for matrices in draws)
    print(
        f"{kind} {seed}: {len(draws)} draws, {total} matrices, "
        f"default dearer than another run on {len(found)}"
    )
    for index, run, costs in found:
        shown = {
            name: None if cost is None else round(cost, 6)
            for name, cost in costs.items()
        }
        print(f"  draw {index} run {run} {shown}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
