"""Seeded sweep: can a balance window the solver chose carry more than 1e-6 kWh of discharge?

`optimize._constrain_balance` gates discharge with `bd + cap * chosen <= cap`, and HiGHS
accepts a binary within 1e-6 of 1, so a chosen window could in theory carry
`1e-6 * cap` of `bd` while `_validate_balance` requires `bd <= 1e-6`. This sweep solves
random problems on real HiGHS (hold and fixed windows, hourly and 15-minute slots,
battery discharge limit 8-60 kW, up to 60 kWh per hourly slot) and reports every solver
failure by message plus the largest `bd` inside a chosen window.

Result of the T-048 run (1600 seeds, 3 s limit, 4 sets of 400): 0 `balance discharge`
failures in about 2 800 chosen windows; the largest `bd` in a chosen window was 7.4e-11
kWh. The only failures were `timeout` on 15-minute problems. Not reproduced.

    python -m benchmarks.balance_discharge --seeds 400 --start 0
"""

import argparse
import random
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from custom_components.energy_compass.engine import optimize
from custom_components.energy_compass.engine.balance import (
    candidate_windows,
    lift_slots,
    pin_balance,
)
from custom_components.energy_compass.engine.models import (
    Battery,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)

_T0 = datetime(2026, 9, 24, 0, tzinfo=UTC)


def make_problem(seed: int) -> Problem:
    rng = random.Random(seed)
    step = rng.choice([60, 15])
    count = rng.choice([8, 12, 24]) if step == 60 else rng.choice([24, 48])
    hours = step / 60
    capacity = rng.choice([10.0, 25.0, 40.0, 100.0])
    discharge_kw = rng.choice([8, 15, 30, 60])
    slots = tuple(
        Slot(
            _T0 + timedelta(minutes=step * i),
            _T0 + timedelta(minutes=step * (i + 1)),
            rng.choice([0.1, 0.3, 0.6, 1.2, 2.0]) * rng.uniform(0.7, 1.3),
            rng.uniform(0.0, 1.5),
            rng.choice([0, 0, rng.uniform(0, 20) * hours]),
            rng.uniform(0, 6) * hours,
        )
        for i in range(count)
    )
    initial = capacity * rng.choice([0.5, 0.9, 0.99, 1.0, rng.uniform(0.2, 1.0)])
    battery = Battery(
        capacity,
        rng.choice([0.05, 0.1, 0.2]),
        rng.choice([1.0, 0.95, 0.9]),
        min(initial, capacity),
        discharge_kw,
        discharge_kw,
        rng.choice([1.0, 0.95]),
        rng.choice([1.0, 0.95]),
        rng.choice([0.0, 0.02]),
        True,
        rng.choice([False, True]),
    )
    base = Problem(
        slots,
        SiteLimits(rng.choice([25, 60]), 60, 60, rng.choice([True, False])),
        battery,
        "value",
        0.0,
        (),
        "UTC",
        minimum_mode_minutes=rng.choice([0, 15, 30, 60]),
    )
    hold = rng.choice([60, 120])
    windows = candidate_windows(
        slots,
        phase=rng.choice(["eligible", "due", "holding"]),
        now=_T0,
        hold_minutes=hold,
        remaining_minutes=hold,
        grid_charge_ok=(True,) * count,
    )
    return replace(
        base,
        balance_windows=windows,
        balance_threshold_kwh=rng.choice([0.9, 0.99]) * capacity,
        balance_miss_cost=rng.choice([0.5, 5, 50, 500]),
        balance_lift_slots=lift_slots(windows, battery, count),
    )


def sweep(seeds: range, time_limit_s: float = 3.0) -> tuple[Counter, float, int]:
    """Solve each seed free and pinned; return failure counts, max bd, windows seen."""
    outcomes: Counter = Counter()
    seen = {"max_bd": 0.0, "windows": 0}
    original = optimize._validate_balance

    def spy(problem, vectors, values, choices, miss):
        for position, index in enumerate(choices):
            if values[index] > 0.5:
                seen["windows"] += 1
                for t in problem.balance_windows[position].slots:
                    seen["max_bd"] = max(
                        seen["max_bd"], float(values[vectors[t]["bd"]])
                    )
        return original(problem, vectors, values, choices, miss)

    optimize._validate_balance = spy
    try:
        for seed in seeds:
            problem = make_problem(seed)
            try:
                plan = optimize.solve(problem, time_limit_s=time_limit_s)
                outcomes["ok"] += 1
                if plan.balance_start is not None:
                    optimize.solve(
                        pin_balance(problem, plan), time_limit_s=time_limit_s
                    )
                    outcomes["pinned ok"] += 1
            except SolveError as err:
                outcomes[str(err).split(" (")[0]] += 1
    finally:
        optimize._validate_balance = original
    return outcomes, seen["max_bd"], seen["windows"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=400)
    parser.add_argument("--start", type=int, default=0)
    args = parser.parse_args()
    outcomes, max_bd, windows = sweep(range(args.start, args.start + args.seeds))
    print(dict(outcomes))
    print(
        f"chosen windows validated: {windows}; max bd in a chosen window: {max_bd:.3g}"
    )


if __name__ == "__main__":
    main()
