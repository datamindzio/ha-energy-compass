"""Seeded sweep: can a balance window the solver chose carry more than 1e-6 kWh of discharge?

`optimize._constrain_balance` gates discharge with `bd + cap * chosen <= cap`, and HiGHS
accepts a binary within 1e-6 of 1, so a chosen window could in theory carry
`1e-6 * cap` of `bd` while `_validate_balance` requires `bd <= 1e-6`. This sweep solves
random problems on real HiGHS (hold and fixed windows, hourly and 15-minute slots,
battery discharge limit 8-60 kW, up to 60 kWh per hourly slot) and reports every solver
failure by message plus the largest `bd` inside a chosen window, per slot length.

Result of the T-048 run (seeds 0-1599: 795 problems with 15-minute slots, 805 hourly; 8
chunks of 200 on 6 parallel containers, so wall-clock limits bind harder than on an idle
host and the counts below move from run to run):

- exact solves (`--time-limit 3`): 1 924 chosen windows validated (816 15-minute, 1 108
  hourly), 0 `balance discharge` failures, largest `bd` in a chosen window 1.27e-11 kWh.
  Solves that never reached the validator: 82 of 795 free 15-minute solves and 2 of 805
  hourly ones timed out, plus 2 pinned 15-minute solves.
- time-limited incumbents, the way `runtime.py` solves the base plan
  (`--time-limit 0.4 --accept-incumbent`): 639 incumbents (361 free + 64 pinned 15-minute,
  166 free + 48 pinned hourly) went through the same validator among 1 894 chosen
  windows; 0 failures, largest `bd` 1.13e-11 kWh; 2 free 15-minute solves found no
  incumbent and timed out.

Not reproduced.

    python -m benchmarks.balance_discharge --seeds 200 --start 0
    python -m benchmarks.balance_discharge --seeds 200 --start 0 --time-limit 0.4 --accept-incumbent
"""

import argparse
import random
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
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


@dataclass
class Sweep:
    """What a sweep saw; every count is keyed by the slot length in minutes."""

    outcomes: Counter = field(default_factory=Counter)  # (minutes, label) -> solves
    windows: Counter = field(default_factory=Counter)  # chosen windows validated
    max_bd: float = 0.0  # largest bd inside a chosen window, kWh

    def balance_failures(self) -> dict:
        return {k: n for k, n in self.outcomes.items() if "balance" in k[1]}


def sweep(
    seeds: Iterable[int],
    time_limit_s: float = 3.0,
    accept_incumbent: bool = False,
) -> Sweep:
    """Solve each seed free and pinned on real HiGHS and validate every chosen window.

    `time-limited` outcomes count the solves that returned an incumbent, a subset of `ok`.

    `accept_incumbent` solves the way the base plan does (`runtime.py`): a time-limited
    incumbent goes through the same `_validate_balance` instead of raising `timeout`.
    A `timeout` outcome never reaches the validator, so it is counted, not hidden.
    """
    result = Sweep()
    original = optimize._validate_balance

    def spy(problem, vectors, values, choices, miss):
        minutes = _minutes(problem)
        for position, index in enumerate(choices):
            if values[index] > 0.5:
                result.windows[minutes] += 1
                for t in problem.balance_windows[position].slots:
                    result.max_bd = max(result.max_bd, float(values[vectors[t]["bd"]]))
        return original(problem, vectors, values, choices, miss)

    optimize._validate_balance = spy
    try:
        for seed in seeds:
            problem = make_problem(seed)
            minutes = _minutes(problem)
            stage = ""
            try:
                plan = optimize.solve(
                    problem,
                    time_limit_s=time_limit_s,
                    accept_incumbent=accept_incumbent,
                )
                result.outcomes[minutes, "ok"] += 1
                result.outcomes[minutes, "time-limited"] += plan.time_limited
                if plan.balance_start is not None:
                    stage = "pinned "
                    plan = optimize.solve(
                        pin_balance(problem, plan),
                        time_limit_s=time_limit_s,
                        accept_incumbent=accept_incumbent,
                    )
                    result.outcomes[minutes, "pinned ok"] += 1
                    result.outcomes[minutes, "pinned time-limited"] += plan.time_limited
            except SolveError as err:
                label = str(err) if err.reason == "solver_failure" else err.reason
                result.outcomes[minutes, stage + label] += 1
    finally:
        optimize._validate_balance = original
    return result


def _minutes(problem: Problem) -> int:
    first = problem.slots[0]
    return int((first.end - first.start).total_seconds() // 60)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=400)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--time-limit", type=float, default=3.0)
    parser.add_argument("--accept-incumbent", action="store_true")
    args = parser.parse_args()
    result = sweep(
        range(args.start, args.start + args.seeds),
        args.time_limit,
        args.accept_incumbent,
    )
    for minutes in (15, 60):
        row = {k[1]: n for k, n in result.outcomes.items() if k[0] == minutes}
        print(f"{minutes} min: {row}; chosen windows: {result.windows[minutes]}")
    print(f"max bd in a chosen window: {result.max_bd:.3g}")


if __name__ == "__main__":
    main()
