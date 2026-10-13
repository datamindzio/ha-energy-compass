"""What the `charge_kwh` of a published CURTAIL row looks like (T-076).

    python benchmarks/curtail_charge_rows.py 150 11 22      # draws per kind, then seeds

The Deye controller (native and blueprint) carries out a CURTAIL row that charges. Two numbers decide
how, and both come from here, on the plans `solve_released` returns for the `curtail_sweep.py` draws
(`run`, `negrun`, `deprun`, `resrun`, `fullrun`), rows read the way the coordinator publishes them
(`machine_snapshot`, no rounding):

- `noise`: rows that do not charge but carry a `charge_kwh` above 0 (HiGHS leaves up to 1e-6 kWh on a
  flow the chosen mode does not use; the display classifier calls that zero). The controller's threshold
  `CURTAIL_CHARGE_MIN_KWH` has to sit above the largest of them (`largest`), and `between` counts rows
  between 1e-6 and 1e-3 kWh, which no threshold can tell from a small charge;
- `partial`: charging rows that plan less than `charge_kw x h` while the pack ends below its ceiling, so
  the row's charge was free to be more (it is free between 0 and the PV left after the curtailment and the
  load): the controller must charge those at the planned current, not at the cap.

The engine is whatever is on PYTHONPATH. Invoked by path, like the other benchmarks: the repository root
must be on PYTHONPATH.
"""

import random
import sys

import curtail_sweep as sweep

from custom_components.energy_compass.engine.consumption import machine_snapshot
from custom_components.energy_compass.engine.models import SolveError

MAKERS = {
    "run": sweep.make,
    "negrun": sweep.make_negative,
    "deprun": sweep.make_depleted,
    "resrun": sweep.make_reserve,
    "fullrun": sweep.make_full,
}
TOLERANCE = 1e-6  # engine.machine_state._TOL
MARGIN = 1e-4  # a pack within this of its ceiling counts as full


def rows(count, seeds):
    """(kind, plans, rows) for every maker: each row is the published CURTAIL row and its context."""
    for kind, maker in MAKERS.items():
        found = []
        plans = 0
        for seed in seeds:
            rng = random.Random(seed)
            for _ in range(count):
                try:
                    plan, solved = sweep.planned(maker(rng))
                except SolveError:
                    continue
                plans += 1
                battery = solved.battery
                for slot, flow in zip(solved.slots, plan.flows, strict=True):
                    row = machine_snapshot(slot, flow)
                    if row["state"] != "CURTAIL":
                        continue
                    hours = (slot.end - slot.start).total_seconds() / 3600
                    ceiling = battery.capacity_kwh * battery.maximum_soc_fraction
                    found.append((row, battery.charge_kw * hours, ceiling))
        yield kind, plans, found


def summary(count, seeds):
    """One line per kind, and the totals."""
    lines = []
    total = {
        "curtail": 0,
        "zero": 0,
        "negative": 0,
        "noise": 0,
        "between": 0,
        "charging": 0,
        "partial": 0,
    }
    largest = 0.0
    for kind, plans, found in rows(count, seeds):
        counts = dict.fromkeys(total, 0)
        for row, power_kwh, ceiling in found:
            charge = row["charge_kwh"]
            counts["curtail"] += 1
            if charge < 0:
                counts["negative"] += 1
            elif charge == 0:
                counts["zero"] += 1
            elif charge <= TOLERANCE:
                counts["noise"] += 1
                largest = max(largest, charge)
            elif charge <= 1e-3:
                counts["between"] += 1
            else:
                counts["charging"] += 1
                full = row["end_soc_kwh"] >= ceiling - MARGIN
                if charge < power_kwh - MARGIN and not full:
                    counts["partial"] += 1
        lines.append(
            f"{kind}: {plans} plans, "
            + ", ".join(f"{k} {v}" for k, v in counts.items())
        )
        for name, value in counts.items():
            total[name] += value
    lines.append("all: " + ", ".join(f"{k} {v}" for k, v in total.items()))
    lines.append(f"largest noise charge_kwh: {largest:.3g}")
    return lines


if __name__ == "__main__":
    draws = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    seeds = [int(seed) for seed in sys.argv[2:]] or [11, 22]
    print("\n".join(summary(draws, seeds)))
