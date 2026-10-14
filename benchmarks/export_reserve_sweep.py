"""Cost of keeping `export_reserve_margin_kwh` after a battery-export slot (T-093).

Seeded draws of a G12-like day (cheap night and midday, a dear evening run that a
cheap slot follows) with battery export on. Each draw is planned without the floor
(margin 0, today's plan) and with it. Two shapes of the evening:

- `--shape run`: the sell price is high through the whole dear run, so the plan
  exports to the cheap slot and the pack ends at the reserve where the floor is
  void (the last slot before a cheap slot carries none). The floor rarely binds.
- `--shape live`: the sell price is high for a short window inside the dear run
  (the live site of 2026-10-14 exported 18:30-19:45 and bought to 22:00), so a plan
  that empties the pack in the window leaves it at `reserve + deficit` to the cheap
  slot: the floor binds there.

The table reports, per margin, for all draws and for the **binding** draws (the plan
without the floor ends an export slot below the floor, `floor_gap > 0`):

- `premium`: modelled plan cost with the floor minus without (what the insurance costs
  when the forecast is right), mean and worst;
- `exported`: battery export in kWh, with and without the floor;
- `a miss buys` (binding draws only; elsewhere the two plans differ only by ties):
  kWh a load `--miss` times above the forecast would have bought after the last export
  slot, without the floor and with it (it saturates once the margin covers the miss),
  and `miss_cost` the same at the run's median buy price;
- `time_ratio`: solve time with / without the floor (median and max), the
  absolute solve times and the draws that end at the 10 s limit (the runtime's
  `accept_incumbent`).

Run by path inside the test image like `benchmarks/optimizer.py`:
`python benchmarks/export_reserve_sweep.py --draws 40 --slots 96 --shape live`.
"""

import argparse
import random
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import pi, sin
from pathlib import Path
from statistics import mean, median
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.energy_compass.engine.models import (
    Battery,
    Problem,
    SiteLimits,
    Slot,
)
from custom_components.energy_compass.engine.optimize import solve
from custom_components.energy_compass.engine.peak_reserve import (
    export_reserve_targets,
    export_reserve_weight,
)

_CHEAP = 0.61
# Export below this is the solver's activity-floor noise, not an export episode.
_TOKEN_KWH = 0.05


def draw(rng: random.Random, count: int, shape: str = "run") -> Problem:
    start = datetime(2026, 10, 14, tzinfo=UTC)
    peak = rng.uniform(1.1, 1.3)
    sell_ratio = rng.uniform(0.8, 1.0)
    pv_scale = rng.uniform(0.0, 2.5)
    load_scale = rng.uniform(0.6, 1.5)
    # live shape: the sell price is high only for a few slots of the dear run (drawn
    # only for that shape, so `run` keeps the draws it had before the option existed)
    window_start = window_hours = 0.0
    if shape == "live":
        window_start = rng.uniform(17.5, 19.5)
        window_hours = rng.uniform(1.0, 2.0)
    slots = []
    for index in range(count):
        hour = (index % 96) / 4
        evening = 17 <= hour < 22
        cheap = hour < 6 or 13 <= hour < 15 or hour >= 22
        buy = _CHEAP if cheap else peak
        sell = buy * sell_ratio if evening else 0.3
        if shape == "live" and evening:
            in_window = window_start <= hour < window_start + window_hours
            sell = buy * sell_ratio if in_window else 0.3
        pv = pv_scale * max(0.0, sin(pi * (hour - 6) / 12)) / 4
        load = load_scale * (0.12 + (0.1 if 6 <= hour < 9 or 17 <= hour < 22 else 0))
        slots.append(
            Slot(
                start + timedelta(minutes=15 * index),
                start + timedelta(minutes=15 * (index + 1)),
                buy,
                sell,
                pv,
                load,
            )
        )
    capacity = rng.choice((10.0, 13.5, 20.0))
    battery = Battery(
        capacity,
        0.1,
        0.9,
        rng.uniform(0.3, 0.9) * capacity,
        3.3,
        3.3,
        0.94,
        0.94,
        0.08,
        True,
        True,
    )
    return Problem(
        tuple(slots),
        SiteLimits(5, 8, 8, True),
        battery,
        "value",
        _CHEAP,
        (),
        "UTC",
        limit_export_to_pv=False,
        maximum_grid_charge_price=_CHEAP,
    )


def with_floor(problem: Problem, margin: float) -> Problem:
    battery = problem.battery
    buy = tuple(s.buy_per_kwh for s in problem.slots)
    reserve = export_reserve_targets(
        buy,
        tuple(s.sell_per_kwh for s in problem.slots),
        tuple(s.load_kwh for s in problem.slots),
        tuple(s.pv_kwh for s in problem.slots),
        cheap_price=_CHEAP + 0.01,
        reserve_kwh=battery.capacity_kwh * battery.minimum_soc_fraction,
        usable_capacity_kwh=battery.capacity_kwh * battery.maximum_soc_fraction,
        eta_discharge=battery.eta_discharge,
        margin_kwh=margin,
    )
    return replace(
        problem,
        export_reserve_kwh=reserve.targets_kwh,
        export_reserve_window=reserve.window_ids,
        export_reserve_weight=export_reserve_weight(buy, reserve.window_ids),
    )


def missed_kwh(problem: Problem, plan, miss: float) -> tuple[float, float]:
    """kWh bought after the last export slot if the load is `miss` above the forecast."""
    flows = plan.flows
    last = max(
        (i for i, f in enumerate(flows) if f.grid_export_kwh > _TOKEN_KWH), default=None
    )
    if last is None:
        return 0.0, 0.0
    battery = problem.battery
    prices = [s.buy_per_kwh for s in problem.slots]
    end = next(
        (i for i in range(last + 1, len(prices)) if prices[i] <= _CHEAP + 0.01),
        len(prices),
    )
    deficit = sum(
        max(0.0, problem.slots[i].load_kwh - problem.slots[i].pv_kwh)
        for i in range(last + 1, end)
    )
    energy = flows[last].end_soc_kwh
    floor = battery.capacity_kwh * battery.minimum_soc_fraction
    usable = max(0.0, energy - floor)
    need = deficit * (1 + miss) / battery.eta_discharge
    bought = max(0.0, need - usable)
    return bought, bought * median(prices[last + 1 : end] or [0.0])


def floor_gap(problem: Problem, plan) -> float:
    """kWh the plan ends an export slot below the floor, summed over the runs."""
    gaps: dict[int, float] = {}
    for index, flow in enumerate(plan.flows):
        window = problem.export_reserve_window[index]
        if window == -1 or flow.grid_export_kwh <= _TOKEN_KWH:
            continue
        gap = problem.export_reserve_kwh[index] - flow.end_soc_kwh
        gaps[window] = max(gaps.get(window, 0.0), gap)
    return sum(max(0.0, gap) for gap in gaps.values())


def report(label: str, rows: list[dict], miss: bool) -> str:
    if not rows:
        return f"  {label}: none"
    text = (
        f"  {label} ({len(rows)}): premium mean={mean(r['premium'] for r in rows):+.4f} "
        f"worst={max(r['premium'] for r in rows):+.4f} | exported "
        f"{mean(r['export0'] for r in rows):.2f} -> {mean(r['export1'] for r in rows):.2f} "
        "kWh | "
    )
    if miss:
        text += (
            f"a miss buys {mean(r['bought0'] for r in rows):.3f} -> "
            f"{mean(r['bought1'] for r in rows):.3f} kWh, "
            f"miss_cost {mean(r['cost0'] for r in rows):.4f} -> "
            f"{mean(r['cost1'] for r in rows):.4f}, "
            f"net(premium-miss_cost avoided)="
            f"{mean(r['premium'] - r['cost0'] + r['cost1'] for r in rows):+.4f} | "
        )
    ratios = [r["time1"] / max(r["time0"], 1e-9) for r in rows]
    return (
        text
        + f"time_ratio median={median(ratios):.2f} max={max(ratios):.2f} | solve s "
        f"median {median(r['time0'] for r in rows):.2f} -> "
        f"{median(r['time1'] for r in rows):.2f}, max "
        f"{max(r['time0'] for r in rows):.2f} -> {max(r['time1'] for r in rows):.2f} | "
        f"time-limited {sum(1 for r in rows if r['limited0'])} -> "
        f"{sum(1 for r in rows if r['limited1'])} | shortfall>0 in "
        f"{sum(1 for r in rows if r['shortfall'] > 1e-6)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--draws", type=int, default=40)
    parser.add_argument("--slots", type=int, choices=(48, 96, 192), default=96)
    parser.add_argument("--seed", type=int, default=93)
    parser.add_argument("--miss", type=float, default=0.3)
    parser.add_argument("--shape", choices=("run", "live"), default="run")
    parser.add_argument("--margins", type=float, nargs="+", default=[0.5, 1.0])
    args = parser.parse_args()
    rng = random.Random(args.seed)
    problems = [draw(rng, args.slots, args.shape) for _ in range(args.draws)]
    base = []
    for problem in problems:
        started = perf_counter()
        plan = solve(problem, accept_incumbent=True)
        base.append((plan, perf_counter() - started))
    print(
        f"draws={args.draws} slots={args.slots} seed={args.seed} shape={args.shape} "
        f"miss={args.miss} exporting_draws="
        f"{sum(1 for p, _ in base if any(f.grid_export_kwh > 1e-6 for f in p.flows))}"
    )
    for margin in args.margins:
        rows = []
        for problem, (plan0, time0) in zip(problems, base, strict=True):
            floored = with_floor(problem, margin)
            started = perf_counter()
            plan1 = solve(floored, accept_incumbent=True)
            time1 = perf_counter() - started
            bought0, cost0 = missed_kwh(problem, plan0, args.miss)
            bought1, cost1 = missed_kwh(problem, plan1, args.miss)
            rows.append(
                {
                    "binding": floor_gap(floored, plan0) > 1e-3,
                    "premium": plan1.objective - plan0.objective,
                    "export0": sum(f.grid_export_kwh for f in plan0.flows),
                    "export1": sum(f.grid_export_kwh for f in plan1.flows),
                    "bought0": bought0,
                    "bought1": bought1,
                    "cost0": cost0,
                    "cost1": cost1,
                    "shortfall": plan1.peak_reserve_shortfall_kwh,
                    "time0": time0,
                    "time1": time1,
                    "limited0": plan0.time_limited,
                    "limited1": plan1.time_limited,
                }
            )
        binding = [r for r in rows if r["binding"]]
        print(f"margin={margin}: binding draws {len(binding)}/{len(rows)}")
        print(report("all", rows, miss=False))
        print(report("binding", binding, miss=True))


if __name__ == "__main__":
    main()
