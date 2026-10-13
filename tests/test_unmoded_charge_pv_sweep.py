"""Seeded sweep of unmoded problems (`minimum_mode_minutes = 0`) for the CHARGE_PV rule (T-034).

The rule that a CHARGE_PV charge takes the whole surplus narrows what the solver may
do, and narrowing a rule exposes the next gap (LESSONS.md,
tightened-escape-exposes-neighbour-gap). The corpus mixes export caps, inverter
ratings below the PV, curtailment on and off, packs a hair below the ceiling or
above the reserve and standby loss. The solver certifies every plan it returns
with `validate_modes`, so a plan that comes back at all agrees with the model; what
the sweep adds is that no plan shows a partial charge beside export and that no
problem is rejected for any other reason than being infeasible.
"""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine.machine_state import machine_state
from custom_components.energy_compass.engine.models import (
    Battery,
    FlexibleLoadRequest,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import (
    solve,
    solve_flexible_load,
)

START = datetime(2026, 6, 18, 9, tzinfo=UTC)
QUARTER = timedelta(minutes=15)
# Around the minimum activity (0.025 kWh per quarter hour) and the classifier resolution.
ENERGIES = (0, 0, 0.01, 0.02, 0.024, 0.025, 0.03, 0.05, 0.1, 0.2, 0.5, 0.8, 1.2, 2.0)


def corpus_problem(rng: random.Random) -> Problem:
    """One unmoded problem; every rule the CHARGE_PV surplus rule meets can be on or off."""
    step = timedelta(minutes=rng.choice((15, 15, 30, 60)))
    slots = tuple(
        Slot(
            START + i * step,
            START + (i + 1) * step,
            round(rng.uniform(0.2, 1.5), 2),
            round(rng.uniform(-0.2, 1.2), 2),
            rng.choice(ENERGIES),
            rng.choice(ENERGIES),
        )
        for i in range(rng.randint(2, 24))
    )
    capacity = rng.choice((5, 10))
    low = rng.choice((0, 0.1, 0.2))
    high = rng.choice((1, 1, 0.9, 0.8))
    initial = rng.choice(
        (
            capacity * low + rng.choice((0, 0.005, 0.03, 0.1, 0.3)),
            round(rng.uniform(capacity * low, capacity * high), 3),
            capacity * high - rng.choice((0, 0.01, 0.05, 0.2, 0.6)),
        )
    )
    battery = Battery(
        capacity,
        low,
        high,
        initial,
        rng.choice((0.5, 1, 3, 4)),
        rng.choice((0.5, 1, 3, 4)),
        rng.choice((0.9, 0.95, 1)),
        rng.choice((0.9, 0.95, 1)),
        rng.choice((0, 0.01, 0.05)),
        rng.random() < 0.5,
        rng.random() < 0.5,
        rng.choice((0, 0, 0.02, 0.05)),
    )
    return Problem(
        slots,
        SiteLimits(
            rng.choice((3, 8)),
            rng.choice((8, 8, 2)),
            rng.choice((0, 8, 8, 1)),
            rng.random() < 0.7,
        ),
        battery,
        rng.choice(("value", "value", "value", "preserve_initial")),
        rng.choice((0, 0.3, 0.9)),
        (),
        "UTC",
        minimum_mode_minutes=0,
    )


# Each sweep takes several seconds alone; a busy host takes it past the suite's 45 s
# thread timeout, which kills the xdist worker without a traceback (T-051).
pytestmark = pytest.mark.timeout(180)


def corpus(seed: int, count: int):
    rng = random.Random(seed)
    return [corpus_problem(rng) for _ in range(count)]


def partial_charges(problem: Problem, flows, extra=None) -> list[int]:
    """List the CHARGE_PV rows that export beside a charge below the limit and the ceiling.

    Written from the rows alone, not from the model: it is the state a controller
    holding the charge cap cannot produce. A flexible-load probe's own load is part
    of what the house draws.
    """
    battery = problem.battery
    ceiling = battery.capacity_kwh * battery.maximum_soc_fraction
    hits = []
    for i, (slot, flow) in enumerate(zip(problem.slots, flows, strict=True)):
        if extra:
            slot = replace(slot, load_kwh=slot.load_kwh + extra[i])
        hours = (slot.end - slot.start).total_seconds() / 3600
        if machine_state(slot, flow) != "CHARGE_PV" or flow.grid_export_kwh < 1e-5:
            continue
        if (
            flow.charge_kwh < battery.charge_kw * hours - 1e-5
            and flow.end_soc_kwh < ceiling - 1e-4
        ):
            hits.append(i)
    return hits


def test_every_unmoded_problem_is_planned_or_infeasible_never_rejected():
    planned = 0
    for index, problem in enumerate(corpus(7, 300)):
        try:
            plan = solve(problem)
        except SolveError as error:
            # `solver_failure` is the validator disagreeing with the model.
            assert error.reason == "infeasible", (index, str(error))
            continue
        planned += 1
        assert partial_charges(problem, plan.flows) == [], index
    # A quarter of the corpus has no plan whatever the rule (a terminal rule the pack
    # cannot meet, load above the grid limit): 228 of 300 had one before the rule,
    # and an export cap or an inverter below the PV with curtailment off costs the
    # few more that the pinned problems below stand for.
    assert planned >= 227


def test_unmoded_flexible_load_probes_are_planned_or_infeasible_never_rejected():
    rng = random.Random(8)
    planned = 0
    for index, problem in enumerate(corpus(7, 120)):
        request = FlexibleLoadRequest(
            rng.choice((0.1, 0.3, 1.0)), rng.choice((0.5, 2.0))
        )
        try:
            result = solve_flexible_load(problem, request)
        except SolveError as error:
            assert error.reason == "infeasible", (index, str(error))
            continue
        planned += 1
        assert partial_charges(problem, result.plan.flows, result.schedule_kwh) == [], (
            index
        )
    # 89 of 120 on the merged engine (origin/main fbcffe0); a lost plan is a regression.
    assert planned >= 89


def quarter_problem(rows, *, site, battery) -> Problem:
    """Four quarter hours of `(buy, sell, pv, load)`, no minimum mode duration."""
    return Problem(
        tuple(
            Slot(START + i * QUARTER, START + (i + 1) * QUARTER, *row)
            for i, row in enumerate(rows)
        ),
        site,
        battery,
        "value",
        0,
        (),
        "UTC",
        minimum_mode_minutes=0,
    )


# Both problems below have a plan with a partial charge beside export without the
# rule, and none with it. The mode model (`minimum_mode_minutes` 15 and 60) is
# infeasible on them too: the surplus has nowhere to go but the pack, the rule
# makes every charge the whole surplus, and the pack is full before the last
# surplus. docs/model.md (CHARGE_PV) states the limit.


def test_export_cap_without_curtailment_leaves_no_plan_when_the_pack_fills_early():
    # 1 kW of export takes 0.25 kWh a quarter hour, the rest of the PV has to charge
    # the pack: 0.95, 0.45 and 0.45 kWh beside that export without the rule (the pack
    # ends on 4.94 of 5 kWh). With it the first charge is the full 1 kWh of the power
    # limit and the second the whole 0.7 kWh surplus, so the pack is full before the
    # last quarter, which then has a quarter kWh nowhere to go.
    problem = quarter_problem(
        (
            (1.01, 0.75, 0.05, 0),
            (0.84, 0.01, 1.2, 0),
            (1.01, 0.2, 1.2, 0.5),
            (0.28, 0.61, 1.2, 0.5),
        ),
        site=SiteLimits(8, 8, 1, False),
        battery=Battery(5, 0.1, 1, 3.295, 4, 3, 0.9, 1, 0, True, False, 0.02),
    )
    with pytest.raises(SolveError) as error:
        solve(problem)
    assert error.value.reason == "infeasible"


def test_inverter_below_the_pv_without_curtailment_leaves_no_plan_when_the_pack_fills_early():
    # PV of 1.2 kWh a quarter hour against a 0.75 kWh inverter: at least 0.45 kWh
    # of each sunny quarter has to charge the pack. Without the rule 0.45 charged
    # beside 0.75 of export at both ends; with it the first quarter takes the 0.6 kWh
    # of room (the pack ends full) and the 0.375 kWh the house draws in between
    # leaves 0.375 of room for the last quarter's 0.45.
    problem = quarter_problem(
        (
            (1.0, 0.5, 1.2, 0),
            (1.0, 0.5, 0, 0.125),
            (1.0, 0.5, 0, 0.125),
            (1.0, 0.5, 0, 0.125),
            (1.0, 0.5, 1.2, 0),
        ),
        site=SiteLimits(3, 8, 8, False),
        battery=Battery(10, 0.1, 1, 9.4, 3, 0.5, 1, 1, 0, True, False, 0),
    )
    with pytest.raises(SolveError) as error:
        solve(problem)
    assert error.value.reason == "infeasible"
