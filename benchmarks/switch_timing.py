"""Time of the default run and of the run without presolve on a `spill_sweep.py` draw (T-085).

    python benchmarks/switch_timing.py <kind> <seed> <count> <out.json>

Run with the check on (the head). Records every model that takes the check: the time, status and
cost of the first and the second run and whether the second satisfies every row, and dumps the
matrices where the second plan is taken (cheaper by the margin and satisfying every row) in the
format of `tests/fixtures/solver`. `kind` and `seed` are those of `spill_sweep.py`. Invoked by
path with the repository root on PYTHONPATH.

The `draw` of a record is the 0-based index of the draw in the list `spill_sweep.py` writes (the
one the ADR, `docs/model.md` and the fixtures name: seed 7, draw 277), not a count from 1.
"""

import json
import sys
from time import perf_counter

import curtail_sweep as sweep
import numpy as np
import spill_sweep

from custom_components.energy_compass.engine import optimize


def dump(model):
    """The model as the solver saw it (cost, bounds, integrality, rows)."""
    return {
        "exact_mip_gap": model.exact_mip_gap,
        "cost": model.cost,
        "lower": model.lower,
        "upper": model.upper,
        "integrality": model.integrality,
        "rows": [
            [
                [[column, value] for column, value in terms.items()],
                None if lower == -np.inf else lower,
                None if upper == np.inf else upper,
            ]
            for terms, lower, upper in model.rows
        ],
    }


def measure(kind, seed, count):
    """Return the records of the checked matrices, the dumps of the taken ones, the wall time."""
    records = []
    dumps = []
    current = {}
    draw = [-1]
    run = optimize._Model._run
    plain = sweep.run

    def timed(self, matrix, time_limit_s, **options):
        started = perf_counter()
        result = run(self, matrix, time_limit_s, **options)
        elapsed = perf_counter() - started
        cost = None if result.fun is None else float(result.fun)
        if not options:
            current[id(self)] = {
                "draw": draw[0],
                "first_s": elapsed,
                "first_status": result.status,
                "first_cost": cost,
                "compare": self.compare_switches,
                "limit": time_limit_s,
            }
            return result
        row = current.get(id(self))
        if row is None or not row["compare"] or row["first_status"] != 0:
            return result
        row.update(
            second_s=elapsed,
            second_status=result.status,
            second_cost=cost,
            second_ok=bool(
                result.status == 0
                and result.x is not None
                and self._satisfies(matrix, result.x)
            ),
        )
        margin = optimize._SWITCH_ABSOLUTE + optimize._SWITCH_RELATIVE * abs(
            row["first_cost"]
        )
        row["taken"] = bool(
            row["second_ok"] and row["second_cost"] < row["first_cost"] - margin
        )
        if row["taken"]:
            dumps.append({"draw": draw[0], "row": dict(row), "model": dump(self)})
        records.append(dict(row))
        return result

    def counted(problem, request=None):
        draw[0] += 1
        return plain(problem, request)

    optimize._Model._run = timed
    sweep.run = counted
    started = perf_counter()
    try:
        spill_sweep.draws(kind, seed, count)
    finally:
        optimize._Model._run = run
        sweep.run = plain
    return records, dumps, perf_counter() - started


def main(kind, seed, count, out):
    records, dumps, wall_s = measure(kind, seed, count)
    with open(out, "w") as handle:
        json.dump({"records": records, "dumps": dumps, "wall_s": wall_s}, handle)
    firsts = [row["first_s"] for row in records]
    seconds = [row["second_s"] for row in records]
    print(
        f"{kind} {seed} {count}: checked {len(records)}, "
        f"default runs {sum(firsts):.1f} s, second runs {sum(seconds):.1f} s "
        f"(longest {max(firsts, default=0):.2f} s, {max(seconds, default=0):.2f} s), "
        f"taken {sum(row['taken'] for row in records)}, "
        f"second run not optimal {sum(row['second_status'] != 0 for row in records)}"
    )


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
