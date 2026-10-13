"""Which rung of `solve_released` plans each boundary of followed days under `preserve_initial` (T-081).

    python benchmarks/day_replay_rungs.py 3 11 22 33               # days per stretch, then seeds
    python benchmarks/day_replay_rungs.py 3 11 22 33 --jobs 4 --json out.json --seasons gloom,winter
    python benchmarks/day_replay_rungs.py 7 11 22 33 --coverage day    # the horizon ends at midnight
    python benchmarks/day_replay_rungs.py compare main.json other.json

The aimed sweep of T-070 (`curtail_sweep.py aimrun`) draws short horizons with the PV within 0.3 kWh
of the load, which is where the class lives. This tool asks how often a house meets it: seeded days
of 96 quarter-hour intervals (a bell of PV scaled by season and by the day's cloud, a household load
with a morning and an evening peak, a two-zone buy price and a sell price that dips at noon), planned
on a forecast that is not the realized series (the PV forecast misses the day's cloud by up to 30 %
and has no interval noise, the load forecast is the profile without the appliances), followed one
interval at a time by `tools/replay.py::run_plan`, the loop the replay tool ships. The days are
synthetic: no household data is in the repository.

`--coverage rolling` (the default) plans over a rolling 24 hour horizon, what a site with fixed or
tariff prices has. `--coverage day` ends every horizon at midnight, the shortest a site with
day-ahead prices sees (tomorrow's prices not published yet, here never): each day is replayed on its
own, the state of charge carried over midnight and the mode not. The horizon is then 96 intervals at
midnight and one at 23:45, so the terminal rule binds inside the day.

A stretch is `days` followed days plus one day of look-ahead that is not followed (and not planned
on with `--coverage day`). The axes are the season (gloom, winter, shoulder, summer; `gloom` is an
overcast winter week, the bell at noon about the load of the house, 0.12 to 0.6 kW, so the surplus
is marginal on every day: the days the class needs), `minimum_mode_minutes` (60, 120) and
`idle_drain_kw` (0, 0.13). The site is a 10 kWh pack, floor 10 %, 3 kW, 0.95 each way, a 6 kW
inverter and an export cap of 5 kW, everything else the defaults of `settings.py` (curtailment, grid
charging and battery export off), `terminal_mode` `preserve_initial` (the default of the generic
profile). The export cap never forces a spill here: that class is ADR-0007's, not this tool's.

Per stretch it counts the boundaries by rung (`first`, `reserve_hold`, `uncommitted`: rung 3, `none`),
the boundaries that carry a PV-follow commitment inside its minimum duration (where the class of
ADR-0004 follow-up T-070 can arise), the first mode of the rung 3 plans, and the rung 3 plans that
start `HOLD` on a chargeable surplus: the forecast PV of the first interval above its load by at
least the minimum activity, with room in the pack. The cost is the realized cost of the followed
days, the change of stored energy priced at the mean buy price.

`run_plan` carries a mode like the replay tool does (also after a safety exception, where the
coordinator stores nothing: ADR-0005 point 25) and its daily counters run over the whole stretch;
with battery export off they bind nothing here. The engine is the checkout the file runs in, so the
same command on two checkouts compares them (`compare` reads the two `--json` files).
"""

import json
import math
import random
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import replay as R

ZONE = "UTC"
START = datetime(2026, 3, 2, tzinfo=UTC)
PER_DAY = 96
HOURS = 0.25
# season: the share of the 6 kWp the bell reaches at noon, sunrise and sunset (hours)
SEASONS = {
    "gloom": (0.1, 8.0, 16.0),
    "winter": (0.25, 8.0, 16.0),
    "shoulder": (0.55, 6.0, 18.0),
    "summer": (0.8, 4.5, 20.5),
}
MINIMUMS = (60, 120)
DRAINS = (0, 0.13)
SITE = {
    "capacity_kwh": 10,
    "operating_floor": 10,
    "charge_kw": 3,
    "discharge_kw": 3,
    "eta_charge": 0.95,
    "eta_discharge": 0.95,
    "inverter_kw": 6,
    "grid_import_kw": 10,
    "grid_export_kw": 5,
    "terminal_mode": "preserve_initial",
    "horizon_hours": 24,
    "refresh_minutes": 15,
}
PV_FOLLOW = ("CHARGE_PV", "SELF_CONSUME")


class _Stop(Exception):
    """The followed days are over; the look-ahead day is not followed."""


def _profile_load(hour):
    return 0.25 + (0.8 if 6 <= hour < 9 else 0) + (1.2 if 17 <= hour < 22 else 0)


def make_days(rng, season, days):
    """`days` + 1 days of intervals: the forecast the plan sees and the series the house realizes."""
    peak, rise, fall = SEASONS[season]
    intervals = []
    for day in range(days + 1):
        cloud = rng.uniform(0.2, 1.0)
        seen = min(1.0, max(0.1, cloud * rng.uniform(0.7, 1.3)))
        sell_scale = rng.uniform(0.6, 1.2)
        appliance = rng.sample(range(PER_DAY), 3)
        for index in range(PER_DAY):
            hour = index * HOURS
            sun = (hour - rise) / (fall - rise)
            bell = 6 * peak * math.sin(math.pi * sun) ** 1.5 if 0 < sun < 1 else 0.0
            usual = _profile_load(hour)
            load = usual * rng.uniform(0.5, 1.6)
            if any(0 <= index - begin < 3 for begin in appliance):
                load += 2.0
            cheap = hour < 6 or hour >= 22 or 13 <= hour < 15
            intervals.append(
                R.Interval(
                    START + timedelta(days=day, minutes=15 * index),
                    0.65 if cheap else 1.10,
                    round(sell_scale * (0.15 if 10 <= hour < 15 else 0.45), 4),
                    round(bell * seen * HOURS, 3),
                    round(usual * HOURS, 3),
                    round(bell * cloud * rng.uniform(0.6, 1.2) * HOURS, 3),
                    round(load * HOURS, 3),
                )
            )
    return tuple(intervals)


def rung_of(problem, solved):
    """As `curtail_sweep.rung_and_mode` names the rung `solve_released` planned on."""
    if solved.initial_dispatch_reserve_hold:
        return "reserve_hold"
    if problem.initial_dispatch_mode and not solved.initial_dispatch_mode:
        return "uncommitted"
    return "first"


def stretch(job):
    seed, season, minimum, drain, days, coverage = job
    rng = random.Random(f"{seed}/{season}/{minimum}/{drain}")
    values, _ = R.resolve_settings(
        {**SITE, "minimum_mode_minutes": minimum, "idle_drain_kw": drain}, {}
    )
    series = make_days(rng, season, days)
    followed = days * PER_DAY
    rungs, firsts = Counter(), Counter()
    inside = chargeable = 0
    lost = 0.0
    examples = []
    flows = []
    solve, realize = R.solve_released, R.realize

    def counted(problem, **options):
        nonlocal inside, chargeable, lost
        if len(flows) >= followed:
            raise _Stop
        carried = problem.initial_dispatch_mode in PV_FOLLOW and (
            problem.slots[0].start
            < problem.initial_dispatch_mode_since + timedelta(minutes=minimum)
        )
        inside += carried
        try:
            plan, solved = solve(problem, **options)
        except R.SolveError:
            rungs["none"] += 1
            raise
        rung = rung_of(problem, solved)
        rungs[rung] += 1
        if rung == "uncommitted":
            mode = plan.flows[0].dispatch_mode
            firsts[mode] += 1
            slot, battery = problem.slots[0], problem.battery
            surplus = slot.pv_kwh - slot.load_kwh
            room = (
                battery.capacity_kwh * battery.maximum_soc_fraction
                - battery.initial_kwh
            )
            if (
                mode == "HOLD"
                and surplus >= problem.minimum_mode_power_kw * HOURS - 1e-9
                and room > 1e-6
            ):
                chargeable += 1
                lost += min(
                    surplus, room / battery.eta_charge, battery.charge_kw * HOURS
                )
            if len(examples) < 5:
                examples.append(
                    [
                        len(flows),
                        problem.initial_dispatch_mode,
                        mode,
                        round(surplus, 3),
                        round(battery.initial_kwh, 3),
                    ]
                )
        return plan, solved

    def kept(*args, **options):
        flow, soc = realize(*args, **options)
        flows.append(flow)
        return flow, soc

    R.solve_released, R.realize = counted, kept
    try:
        if coverage == "day":
            for begin in range(0, followed, PER_DAY):
                soc = flows[-1].end_soc_kwh if flows else values["capacity_kwh"] * 0.5
                R.run_plan(
                    R.Day(
                        f"day {begin // PER_DAY}",
                        100 * soc / values["capacity_kwh"],
                        series[begin : begin + PER_DAY],
                    ),
                    values,
                    ZONE,
                )
        else:
            R.run_plan(R.Day("stretch", 50.0, series), values, ZONE)
    except _Stop:
        pass
    finally:
        R.solve_released, R.realize = solve, realize
    used = series[:followed]
    mean_buy = sum(interval.buy for interval in used) / followed
    cost = (
        sum(
            R.flow_cost(interval, flow, values["wear_per_kwh"])
            for interval, flow in zip(used, flows, strict=True)
        )
        + (values["capacity_kwh"] * 0.5 - flows[-1].end_soc_kwh) * mean_buy
    )
    return {
        "stretch": [seed, season, minimum, drain, coverage],
        "boundaries": followed,
        "rungs": dict(rungs),
        "inside_pv_follow_dwell": inside,
        "rung3_first": dict(firsts),
        "rung3_hold_on_chargeable_surplus": chargeable,
        "rung3_hold_surplus_kwh": round(lost, 3),
        "rung3_examples": examples,
        "cost": round(cost, 4),
    }


def total(rows):
    rungs, firsts = Counter(), Counter()
    for row in rows:
        rungs.update(row["rungs"])
        firsts.update(row["rung3_first"])
    return {
        "stretches": len(rows),
        "boundaries": sum(row["boundaries"] for row in rows),
        "rungs": dict(rungs),
        "inside_pv_follow_dwell": sum(row["inside_pv_follow_dwell"] for row in rows),
        "rung3_first": dict(firsts),
        "rung3_hold_on_chargeable_surplus": sum(
            row["rung3_hold_on_chargeable_surplus"] for row in rows
        ),
        "rung3_hold_surplus_kwh": round(
            sum(row["rung3_hold_surplus_kwh"] for row in rows), 3
        ),
        "cost": round(sum(row["cost"] for row in rows), 2),
    }


def compare(before, after):
    """Stretch by stretch: the rungs and the realized cost of two checkouts on the same days."""
    pairs = {tuple(row["stretch"]): [row] for row in before}
    for row in after:
        pairs[tuple(row["stretch"])].append(row)
    changed = [
        (key, old, new)
        for key, (old, new) in sorted(pairs.items())
        if (old["rungs"], old["cost"]) != (new["rungs"], new["cost"])
    ]
    for key, old, new in changed:
        print(key, old["rungs"], old["cost"], "->", new["rungs"], new["cost"])
    deltas = [new["cost"] - old["cost"] for _, old, new in changed]
    print(
        {
            "stretches": len(pairs),
            "different": len(changed),
            "dearer": sum(delta > 1e-9 for delta in deltas),
            "cheaper": sum(delta < -1e-9 for delta in deltas),
            "cost_change": round(sum(deltas), 4),
        }
    )
    print("before", total(before))
    print("after", total(after))


def main(argv):
    if argv and argv[0] == "compare":
        compare(*(json.loads(Path(path).read_text()) for path in argv[1:3]))
        return 0
    jobs, out, numbers, seasons, coverage = 1, None, [], list(SEASONS), "rolling"
    args = iter(argv)
    for arg in args:
        if arg == "--jobs":
            jobs = int(next(args))
        elif arg == "--json":
            out = Path(next(args))
        elif arg == "--seasons":
            seasons = next(args).split(",")
        elif arg == "--coverage":
            coverage = next(args)
        else:
            numbers.append(int(arg))
    days, seeds = numbers[0], numbers[1:]
    work = [
        (seed, season, minimum, drain, days, coverage)
        for seed in seeds
        for season in seasons
        for minimum in MINIMUMS
        for drain in DRAINS
    ]
    if jobs > 1:
        with Pool(jobs) as pool:
            rows = pool.map(stretch, work, chunksize=1)
    else:
        rows = [stretch(job) for job in work]
    for row in rows:
        if row["rungs"].keys() - {"first"}:
            print(row)
    for season in seasons:
        print(season, total([row for row in rows if row["stretch"][1] == season]))
    print("all", total(rows))
    if out:
        out.write_text(json.dumps(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
