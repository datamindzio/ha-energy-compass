"""Seeded sweep of export-capped problems with PV gaps and carried modes (T-037).

A rule that lets the solver out of a constraint is judged by what it does to the
problems around it (LESSONS.md, tightened-escape-exposes-neighbour-gap). Run the same
seeds on two checkouts and compare:

    python benchmarks/curtail_sweep.py run 11 300 /tmp/new_11.json    # this branch
    python benchmarks/curtail_sweep.py run 11 300 /tmp/main_11.json   # a checkout of main
    python benchmarks/curtail_sweep.py compare /tmp/main_11.json /tmp/new_11.json

`roll` follows each plan the way the coordinator does (one interval, carry the
commitment and the pack, plan again) and `rollcompare` prints the problems that were
planned all the way before and are infeasible at some boundary now: a plan that is
there at the first boundary and gone at the next is no plan to the user.

`compare` prints the problems that were planned before and are infeasible now
(`NEWLY INFEASIBLE`), the ones that got worse (objective up by more than 1e-6), the
ones where the solver result is rejected that were not before (`NEW SOLVER FAILURE`)
and a count of each outcome. T-037 ran seeds 11, 22 and 33 with 300 problems each.

T-047 adds the same problems with a sell price that may fall below zero (`negrun`,
`negroll`) and with a flexible-load probe on top (`probe`, which plans through
`solve_flexible_load`: its plan is certified against the probe's load). The base seeds
draw sell prices from 0 to 0.8 and never plan a probe, so they cannot show a rule that
reads a slot's load.

T-050 adds a sunrise on a depleted pack (`deprun`, `deproll`, `deprobe` through a flexible-load probe): a carried CHARGE_PV or
SELF_CONSUME whose pack is at or just above the reserve, PV rising from nothing, and an
export cap of 0, 0.5 or 2 kW. The base draws start from a full or nearly full pack.

T-057 adds a HOLD released from a full pack (`fullrun`, `fullroll`): a carried CHARGE_PV
or SELF_CONSUME on a pack just below its ceiling, a standby loss, a few intervals the
export cap can pass and then PV above it, with and without curtailment. Where a rule
lets that HOLD go back to the commitment in an interval the site cannot pass, the
coordinator has to carry the return across the boundary too, or the plan promised in
one interval is another in the next. `roll` records, per row, the boundaries where the
first interval of the new plan is not the second of the one before (`changed`), and
`rollcompare` reports them beside the infeasible ones: a plan that exists now and is
gone at the next boundary, or whose next boundary changes its mode, is invisible to a
count of newly infeasible rows alone. A changed boundary is common on main too (about one
roll in five), so only the rows that change on one side and not the other count.

T-058 adds `resrun`/`resroll`: an ordinary draw with SELF_CONSUME carried and the pack exactly at
the reserve (`make_reserve`; almost every draw reaches the safety exception, where `deprun` reaches
it in about one draw in ten), the generator aimed at the reserve rule. `planned` goes through
`solve_released`, so a tree with the fallback plans the commitment's HOLD when the release has none.

T-070 adds a carried PV-follow commitment under `preserve_initial` with PV near the load
(`aimrun`, `aimroll`): the terminal rule asks the pack to bank surplus that an idle interval,
the last minimum duration of the horizon or a full pack cannot take, which the other kinds
(random terminal mode, PV well away from the load) do not draw. Before T-058's fallback 126 of 1600
first-boundary problems (seeds 101, 202, 303, 404 x 400) were infeasible with a plan once the commitment or the
terminal rule was dropped; on main da2c2e7 it is 1 of 63 infeasible (docs/model.md, ADR-0004).
Invoked by path, like the other benchmarks: the repository root must be on PYTHONPATH.
"""

import json
import random
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from custom_components.energy_compass.coordinator import EnergyCompassCoordinator
from custom_components.energy_compass.engine import dispatch_policy, optimize
from custom_components.energy_compass.engine.models import (
    Battery,
    FlexibleLoadRequest,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import solve, solve_flexible_load

START = datetime(2026, 12, 18, 11, tzinfo=UTC)
Q = timedelta(minutes=15)
MODES = (None, "CHARGE_PV", "SELF_CONSUME", "HOLD", "CURTAIL", "CHARGE_PV", "CURTAIL")


def make(rng):
    n = rng.randint(4, 16)
    peak = rng.choice((0.4, 0.9, 1.5, 2.2))
    pv = []
    for i in range(n):
        v = round(max(0, peak * rng.uniform(0.6, 1)), 2)
        pv.append(v)
    for _ in range(rng.choice((0, 1, 2, 3))):  # gaps
        a = rng.randrange(n)
        for k in range(a, min(n, a + rng.randint(1, 6))):
            pv[k] = rng.choice((0, 0, 0.01, 0.02, 0.05, 0.1))
    slots = tuple(
        Slot(
            START + i * Q,
            START + (i + 1) * Q,
            round(rng.uniform(0.2, 1.5), 2),
            round(rng.uniform(0, 0.8), 2),
            pv[i],
            rng.choice((0.02, 0.05, 0.1, 0.2, 0.4)),
        )
        for i in range(n)
    )
    cap = rng.choice((5, 10))
    reserve = rng.choice((0, 0.1))
    initial = rng.choice(
        (
            cap,
            cap - 0.01,
            cap - 0.05,
            cap - 0.1,
            cap - 0.3,
            cap - 1,
            round(rng.uniform(reserve * cap, cap), 2),
        )
    )
    battery = Battery(
        cap,
        reserve,
        1,
        initial,
        rng.choice((1, 3, 4)),
        rng.choice((1, 3, 4)),
        0.95,
        0.95,
        rng.choice((0, 0.01)),
        rng.random() < 0.5,
        rng.random() < 0.5,
        rng.choice((0, 0, 0.02, 0.05)),
    )
    mode = rng.choice(MODES)
    kw = {}
    if mode:
        kw = {
            "initial_dispatch_mode": mode,
            "initial_dispatch_mode_since": START
            - timedelta(minutes=rng.choice((0, 1, 15, 16, 29, 45))),
        }
    return Problem(
        slots,
        SiteLimits(
            rng.choice((3, 8)),
            8,
            rng.choice((0.5, 1, 2, 8)),
            rng.random() < 0.9,
        ),
        battery,
        rng.choice(("value", "preserve_initial")),
        rng.choice((0, 0.3)),
        (),
        "UTC",
        minimum_mode_minutes=rng.choice((30, 60, 120)),
        **kw,
    )


def make_negative(rng):
    """The same problem with sell prices from below zero to 0.8 (the base draw never is)."""
    problem = make(rng)
    slots = tuple(
        replace(slot, sell_per_kwh=round(rng.uniform(-0.6, 0.8), 2))
        for slot in problem.slots
    )
    return replace(problem, slots=slots)


def make_probe(rng):
    """A problem with negative sell prices and a flexible load that fits the horizon."""
    problem = make_negative(rng)
    power = rng.choice((0.5, 1, 2, 4))
    room = power * 0.25 * len(problem.slots)
    energy = round(min(rng.choice((0.25, 0.5, 1, 2, 4)), 0.8 * room), 2)
    return problem, FlexibleLoadRequest(energy, power)


def make_depleted(rng):
    """A sunrise on an empty pack with a carried PV-follow commitment (T-050)."""
    problem = make(rng)
    peak = rng.choice((0.3, 0.9, 1.5, 2.2))
    first = rng.randrange(0, 3)
    slots = tuple(
        replace(
            slot,
            pv_kwh=0.0
            if i < first
            else round(peak * min(1, (i - first + 1) / rng.choice((1, 3, 5))), 2),
        )
        for i, slot in enumerate(problem.slots)
    )
    floor = problem.battery.capacity_kwh * problem.battery.minimum_soc_fraction
    battery = replace(
        problem.battery,
        initial_kwh=floor + rng.choice((0, 0.01, 0.02, 0.05, 0.2)),
    )
    return replace(
        problem,
        slots=slots,
        battery=battery,
        site=replace(problem.site, grid_export_kw=rng.choice((0, 0.5, 2))),
        initial_dispatch_mode=rng.choice(("CHARGE_PV", "SELF_CONSUME")),
        initial_dispatch_mode_since=START
        - timedelta(minutes=rng.choice((0, 1, 15, 29))),
    )


def make_reserve(rng):
    """An ordinary draw with a PV-follow commitment carried and the pack exactly at the reserve (T-058).

    Any export cap, PV with gaps, a carried SELF_CONSUME since 0 to 45 minutes: the draws
    where the safety exception of `dispatch_policy.safety_exception` applies (the pack at
    the reserve, inside the commitment's minimum duration). `make_depleted` draws the pack at
    the reserve in one of five draws and SELF_CONSUME in one of two, only on a sunrise.
    """
    problem = make(rng)
    battery = problem.battery
    return replace(
        problem,
        battery=replace(
            battery, initial_kwh=battery.capacity_kwh * battery.minimum_soc_fraction
        ),
        initial_dispatch_mode="SELF_CONSUME",
        initial_dispatch_mode_since=START
        - timedelta(minutes=rng.choice((0, 1, 15, 16, 29, 45))),
    )


def make_depleted_probe(rng):
    """The sunrise on an empty pack with a flexible load on top (`deprobe`)."""
    problem = make_depleted(rng)
    power = rng.choice((0.5, 1, 2, 4))
    room = power * 0.25 * len(problem.slots)
    energy = round(min(rng.choice((0.25, 0.5, 1, 2, 4)), 0.8 * room), 2)
    return problem, FlexibleLoadRequest(energy, power)


def make_aimed(rng):
    """A carried PV-follow commitment under `preserve_initial`, PV within 0.3 kWh of the load (T-070).

    Any pack from nearly empty to full, a standby loss of 0 to 0.2 kW, an export cap of 0,
    0.5, 2 or 8 kW and a commitment that has run 0 to 45 minutes of a 60 or 120 minute
    minimum duration. Where the pack cannot bank the surplus the terminal rule asks for
    (idle intervals, the last minimum duration of the horizon, a pack at its ceiling) the
    commitment and the terminal rule cannot both hold: the problem is infeasible with both
    and has a plan without either. The base draws almost never ask for that.
    """
    base = make(rng) if rng.random() < 0.6 else make_full(rng)
    slots = tuple(
        replace(
            s,
            pv_kwh=round(
                max(
                    0.0,
                    s.load_kwh
                    + rng.choice((-0.3, -0.1, -0.05, -0.02, 0, 0.02, 0.05, 0.1, 0.3)),
                ),
                3,
            ),
        )
        for s in base.slots
    )
    b = base.battery
    low = b.capacity_kwh * b.minimum_soc_fraction
    cap = b.capacity_kwh
    initial = rng.choice(
        (
            cap,
            cap - 0.01,
            cap - 0.02,
            cap - 0.3,
            low + 0.05,
            low + 0.3,
            round(rng.uniform(low, cap), 3),
        )
    )
    return replace(
        base,
        slots=slots,
        battery=replace(
            b, initial_kwh=initial, idle_drain_kw=rng.choice((0, 0.02, 0.05, 0.2))
        ),
        terminal_mode="preserve_initial",
        site=replace(base.site, grid_export_kw=rng.choice((0, 0.5, 2, 8))),
        minimum_mode_minutes=rng.choice((60, 120)),
        initial_dispatch_mode=rng.choice(("CHARGE_PV", "SELF_CONSUME")),
        initial_dispatch_mode_since=START
        - timedelta(minutes=rng.choice((0, 15, 29, 45))),
    )


def make_full(rng):
    """A HOLD released from a full pack, then PV the export cap cannot pass (T-057)."""
    n = rng.randint(6, 12)
    export_kw = rng.choice((0.5, 1))
    passable = export_kw * 0.25
    load = rng.choice((0.05, 0.1))
    slots = []
    holds = rng.choice((1, 1, 2, 3))
    for i in range(n):
        if i < holds:
            surplus = rng.uniform(0.5, 0.95) * passable
        elif i < holds + rng.randint(1, 3):
            surplus = passable * rng.uniform(1.05, 3)
        else:
            surplus = rng.choice((0, 0, rng.uniform(0, 0.9) * passable))
        slots.append(
            Slot(
                START + i * Q,
                START + (i + 1) * Q,
                round(rng.uniform(0.2, 1.5), 2),
                round(rng.uniform(0, 0.8), 2),
                round(load + surplus, 3),
                load,
            )
        )
    cap = rng.choice((5, 10))
    battery = Battery(
        cap,
        rng.choice((0, 0.1)),
        1,
        round(cap - rng.choice((0, 0.005, 0.01, 0.015, 0.025)), 3),
        rng.choice((1, 3, 4)),
        rng.choice((1, 3, 4)),
        0.95,
        0.95,
        rng.choice((0, 0.01)),
        False,
        False,
        rng.choice((0.05, 0.2, 0.4)),
    )
    return Problem(
        tuple(slots),
        SiteLimits(rng.choice((3, 8)), 8, export_kw, rng.random() < 0.5),
        battery,
        rng.choice(("value", "preserve_initial")),
        rng.choice((0, 0.3)),
        (),
        "UTC",
        minimum_mode_minutes=rng.choice((60, 120)),
        minimum_mode_power_kw=rng.choice((0.05, 0.1)),
        initial_dispatch_mode=rng.choice(("CHARGE_PV", "CHARGE_PV", "SELF_CONSUME")),
        initial_dispatch_mode_since=START - timedelta(minutes=rng.choice((0, 15))),
    )


def planned(problem, request=None):
    """Solve as the runtime does: `solve_released`, a depleted release without a plan falls back.

    Returns the plan and the problem it was solved for. A tree without `solve_released`
    (a checkout of main to compare with) solves once and never falls back. A flexible-load
    request is planned on the problem the base plan was solved for, like the runtime's probes.
    """
    released = getattr(optimize, "solve_released", None)
    if released:
        plan, problem = released(problem)
    else:
        plan = solve(problem)
    if request is None:
        return plan, problem
    return solve_flexible_load(problem, request).plan, problem


def run(problem, request=None):
    try:
        plan, _ = planned(problem, request)
    except SolveError as error:
        return ["err", str(error)[:40]]
    return [
        "ok",
        round(plan.objective, 6),
        [flow.dispatch_mode for flow in plan.flows],
        list(plan.carried_bridges),
    ]


def commit(problem, plan, built=None):
    """The commitment the coordinator stores once the first interval is followed.

    `_commit_battery_direction` itself, fed the way `_publish_current` feeds it for a
    plan whose current interval is the first, so a roll follows what ships. A tree
    without the release (`released_holds`) stores the older two-key commitment.
    `built` is the problem as built, before `solve_released` took a rung: the
    coordinator still holds its commitment when the plan was solved without it.
    """
    first = problem.slots[0].start.isoformat()
    stored = None
    built = built or problem
    if built.initial_dispatch_mode:
        stored = {
            "mode": built.initial_dispatch_mode,
            "since": built.initial_dispatch_mode_since.isoformat(),
        }
        if getattr(built, "initial_dispatch_released", False):
            stored["released"] = True
    coordinator = SimpleNamespace(
        _battery_commitment=stored,
        _dispatch_store=SimpleNamespace(async_delay_save=lambda *_: None),
    )
    EnergyCompassCoordinator._commit_battery_direction(
        coordinator,
        {
            "generated_at": first,
            "intervals": [
                {"dispatch_mode": plan.flows[0].dispatch_mode, "start": first}
            ],
            "dispatch_policy": {
                "enabled": True,
                "minimum_mode_minutes": problem.minimum_mode_minutes,
                "released_hold_starts": [first]
                if 0 in getattr(plan, "released_holds", ())
                else [],
                "safety_exception": dispatch_policy.safety_exception(problem),
            },
        },
        carried_idle=0 in plan.carried_bridges,
    )
    return coordinator._battery_commitment


def follow(problem, plan, built=None):
    """The next problem when the first interval is carried out as planned."""
    commitment = commit(problem, plan, built)
    battery = problem.battery
    low = battery.capacity_kwh * battery.minimum_soc_fraction
    high = battery.capacity_kwh * battery.maximum_soc_fraction
    released = (
        {"initial_dispatch_released": bool(commitment.get("released"))}
        if "initial_dispatch_released" in Problem.__dataclass_fields__
        else {}
    )
    if "initial_dispatch_reserve_hold" in Problem.__dataclass_fields__:
        # The fallback is decided afresh at every boundary.
        released["initial_dispatch_reserve_hold"] = False
    return replace(
        problem,
        slots=problem.slots[1:],
        battery=replace(
            battery,
            initial_kwh=min(max(plan.flows[0].end_soc_kwh, low), high),
        ),
        initial_dispatch_mode=commitment["mode"],
        initial_dispatch_mode_since=datetime.fromisoformat(commitment["since"]),
        **released,
    )


def roll(problem, steps=8):
    """Plan, follow the first interval, carry the commitment and the pack, plan again.

    The trace names the executed mode of each interval: `*` a carried idle bridge, `!` the
    plan of `reserve_fallback` (the commitment's own HOLD), `?` the plan without the commitment.
    The last item lists the boundaries whose first interval is not what the plan before
    promised for it (its second interval): a plan the next boundary contradicts.
    """
    trace = []
    changed = []
    promised = None
    for step in range(steps):
        if len(problem.slots) < 2:
            break
        built = problem
        try:
            plan, problem = planned(problem)
        except SolveError as error:
            return ["err", step, str(error)[:40], trace, changed]
        modes = [flow.dispatch_mode for flow in plan.flows]
        if promised is not None and promised != modes[0]:
            changed.append(step)
        promised = modes[1]
        trace.append(
            modes[0]
            + ("*" if 0 in plan.carried_bridges else "")
            + ("!" if getattr(problem, "initial_dispatch_reserve_hold", False) else "")
            + (
                "?"
                if built.initial_dispatch_mode and not problem.initial_dispatch_mode
                else ""
            )
        )
        problem = follow(problem, plan, built)
    return ["ok", len(trace), "", trace, changed]


def roll_compare(before_path, after_path):
    """Compare two rolls row by row: what is infeasible, how far each roll got, what changed."""
    before = json.loads(Path(before_path).read_text())
    after = json.loads(Path(after_path).read_text())
    counts = {
        "newly_infeasible_while_followed": 0,
        "fixed_while_followed": 0,
        "first_failure_later": 0,
        "first_failure_earlier": 0,
        "newly_changed_next_boundary": 0,
        "no_longer_changed_next_boundary": 0,
        "different_trace": 0,
    }
    for index, (a, b) in enumerate(zip(before, after, strict=True)):
        if a[0] == "ok" and b[0] != "ok":
            counts["newly_infeasible_while_followed"] += 1
            print("NEWLY INFEASIBLE WHILE FOLLOWED", index, b)
        elif a[0] != "ok" and b[0] == "ok":
            counts["fixed_while_followed"] += 1
        elif a[0] != "ok":
            # Both fail: where the roll gets stuck moved.
            if b[1] > a[1]:
                counts["first_failure_later"] += 1
                print("first failure later", index, a[1], "->", b[1])
            elif b[1] < a[1]:
                counts["first_failure_earlier"] += 1
                print("FIRST FAILURE EARLIER", index, a[1], "->", b[1])
        # The boundaries both rolls planned: does the plan promised there hold up?
        reached = min(len(a[3]), len(b[3]))
        was = {step for step in a[4] if step < reached}
        now = {step for step in b[4] if step < reached}
        if now - was:
            counts["newly_changed_next_boundary"] += 1
            print("PLAN CONTRADICTED AT THE NEXT BOUNDARY", index, sorted(now - was))
        if was - now:
            counts["no_longer_changed_next_boundary"] += 1
        if a[3][:reached] != b[3][:reached]:
            counts["different_trace"] += 1
    print(counts)


def rejected(row):
    return row[0] == "err" and "invalid solver result" in row[1]


def compare(before_path, after_path):
    before = json.loads(Path(before_path).read_text())
    after = json.loads(Path(after_path).read_text())
    counts = {
        "newly_infeasible": 0,
        "newly_planned": 0,
        "new_solver_failure": 0,
        "worse": 0,
        "better": 0,
        "same": 0,
    }
    for index, (a, b) in enumerate(zip(before, after, strict=True)):
        if rejected(b) and not rejected(a):
            # A plan the solver found and the certificate refused: not "infeasible".
            counts["new_solver_failure"] += 1
            print("NEW SOLVER FAILURE", index, a[:2], b)
        elif a[0] == "ok" and b[0] != "ok":
            counts["newly_infeasible"] += 1
            print("NEWLY INFEASIBLE", index, b)
        elif a[0] != "ok" and b[0] == "ok":
            counts["newly_planned"] += 1
        elif a[0] == "ok":
            delta = b[1] - a[1]
            if delta > 1e-6:
                counts["worse"] += 1
                print("worse", index, delta)
            elif delta < -1e-6:
                counts["better"] += 1
            else:
                counts["same"] += 1
    print(counts)


def draws(kind, seed, count):
    rng = random.Random(seed)
    if kind == "probe":
        return [run(*make_probe(rng)) for _ in range(count)]
    if kind == "deprobe":
        return [run(*make_depleted_probe(rng)) for _ in range(count)]
    makers = {
        "run": make,
        "negrun": make_negative,
        "deprun": make_depleted,
        "resrun": make_reserve,
        "fullrun": make_full,
        "aimrun": make_aimed,
    }
    if kind in makers:
        return [run(makers[kind](rng)) for _ in range(count)]
    makers = {
        "roll": make,
        "negroll": make_negative,
        "deproll": make_depleted,
        "resroll": make_reserve,
        "fullroll": make_full,
        "aimroll": make_aimed,
    }
    return [roll(makers[kind](rng)) for _ in range(count)]


if __name__ == "__main__":
    if sys.argv[1] == "compare":
        compare(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "rollcompare":
        roll_compare(sys.argv[2], sys.argv[3])
    else:
        out = draws(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
        Path(sys.argv[4]).write_text(json.dumps(out))
        if sys.argv[1] in (
            "roll",
            "negroll",
            "deproll",
            "resroll",
            "fullroll",
            "aimroll",
        ):
            print(
                sum(row[0] == "ok" for row in out),
                "followed to the end, of",
                len(out),
                "; infeasible at the first boundary:",
                sum(row[0] == "err" and row[1] == 0 for row in out),
            )
        else:
            print(sum(row[0] == "ok" for row in out), "ok of", len(out))
