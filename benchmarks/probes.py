"""Measure the CPU time of every consumption probe and replay the shipped limits.

Each probe runs to its proven optimum with a long limit, so the CPU seconds do not
depend on the probe time limit. The replay then walks the serial loop of
`analyze_consumption` at the shipped limits and counts the probes that would be
known. docs/runtime-validation.md § Consumption probe limits is built from this.
"""

import argparse
import json
from dataclasses import replace
from statistics import median
from time import perf_counter, process_time

from benchmarks.optimizer import STRATEGIES, make_problem
from custom_components.energy_compass.engine import consumption
from custom_components.energy_compass.engine.consumption import analyze_consumption
from custom_components.energy_compass.engine.models import CompassSettings
from custom_components.energy_compass.engine.optimize import solve

_UNLIMITED_S = 900.0
_TOTAL_TIME_LIMIT_S = 60.0
_FINISHING_RESERVE_S = 1.0


def replay(base_s: float, probes_s: list[float], probe_limit_s: float) -> int:
    """Count the probes that finish inside the shipped per-probe and total limits."""
    remaining = _TOTAL_TIME_LIMIT_S - base_s - _FINISHING_RESERVE_S
    known = 0
    for seconds in probes_s:
        if remaining <= 0:
            break
        limit = min(probe_limit_s, remaining)
        known += seconds <= limit
        remaining -= min(seconds, limit)
    return known


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=STRATEGIES, default="cost_min")
    parser.add_argument("--slots", type=int, choices=(48, 96, 192), default=96)
    parser.add_argument("--minimum-mode-minutes", type=int, default=60)
    parser.add_argument(
        "--display-interval", type=int, choices=(15, 30, 60), default=60
    )
    parser.add_argument(
        "--every", type=int, default=1, help="measure every n-th probe only"
    )
    args = parser.parse_args()
    problem = replace(
        make_problem(args.slots, strategy=args.strategy),
        minimum_mode_minutes=args.minimum_mode_minutes,
    )
    hours = min(24, args.slots // 4)
    settings = CompassSettings(
        display_horizon_hours=hours,
        reference_horizon_hours=hours,
        display_interval_minutes=args.display_interval,
    )
    started = process_time()
    plan = solve(problem, time_limit_s=_UNLIMITED_S)
    base_s = process_time() - started
    # A spent budget returns the probe intervals without solving any of them.
    intervals = analyze_consumption(
        problem, plan, settings=settings, budget_s=0
    ).opportunities
    probes_s = []
    for item in intervals[:: args.every]:
        started = process_time()
        consumption._probe(
            problem,
            plan,
            item.start,
            item.end,
            settings.probe_kwh,
            perf_counter() + _UNLIMITED_S,
        )
        probes_s.append(process_time() - started)
    result = {
        "strategy": args.strategy,
        "slots": args.slots,
        "minimum_mode_minutes": args.minimum_mode_minutes,
        "display_interval_minutes": args.display_interval,
        "base_cpu_s": round(base_s, 2),
        "probes": len(intervals),
        "probes_measured": len(probes_s),
        "probe_cpu_s_min": round(min(probes_s), 2),
        "probe_cpu_s_median": round(median(probes_s), 2),
        "probe_cpu_s_max": round(max(probes_s), 2),
        "probe_cpu_s_sum": round(sum(probes_s), 1),
        "probes_over_limit": sum(s > settings.probe_time_limit_s for s in probes_s),
        "probe_time_limit_s": settings.probe_time_limit_s,
    }
    if args.every == 1:
        result["known_at_shipped_limits"] = replay(
            base_s, probes_s, settings.probe_time_limit_s
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
