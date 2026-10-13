"""Seeded sweep of carried PV-follow commitments (T-019, T-025).

Narrowing a rule that lets the solver out of a constraint exposes the next gap
(LESSONS.md, tightened-escape-exposes-neighbour-gap). The corpus is the place
where the pieces meet: sites that cannot export, packs a hair above the reserve
or below the ceiling, a standby loss, a legacy direction guard, and flexible-load
probes. The solver certifies every plan it returns with `validate_modes`, so a
plan that comes back at all agrees with the model.
"""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine.dispatch_policy import safety_exception
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

START = datetime(2026, 12, 18, 19, tzinfo=UTC)
QUARTER = timedelta(minutes=15)
# Around the minimum activity (0.025 kWh per quarter hour) and the classifier resolution.
ENERGIES = (0, 0, 0.01, 0.02, 0.024, 0.025, 0.03, 0.05, 0.1, 0.2, 0.5)
CAPACITY = 5

# Each sweep takes ~8 s alone; a busy host (load average ~10 on 16 cpus) took it past the
# suite's 45 s thread timeout, which kills the xdist worker without a traceback (T-051).
pytestmark = pytest.mark.timeout(180)


def corpus_problem(rng: random.Random) -> Problem:
    """One committed problem; every rule the commitment meets can be on or off."""
    slots = tuple(
        Slot(
            START + i * QUARTER,
            START + (i + 1) * QUARTER,
            round(rng.uniform(0.2, 1.5), 2),
            round(rng.uniform(0, 0.8), 2),
            rng.choice(ENERGIES),
            rng.choice(ENERGIES),
        )
        for i in range(rng.randint(2, 16))
    )
    reserve_fraction = rng.choice((0, 0.1, 0.2))
    reserve = CAPACITY * reserve_fraction
    initial = rng.choice(
        (
            reserve + rng.choice((0.005, 0.02, 0.03, 0.05, 0.1, 0.3)),
            round(rng.uniform(reserve, CAPACITY), 3),
            CAPACITY - rng.choice((0, 0.01, 0.05)),
        )
    )
    battery = Battery(
        CAPACITY,
        reserve_fraction,
        1,
        initial,
        rng.choice((0.5, 1, 3)),
        rng.choice((0.5, 1, 3)),
        0.95,
        0.95,
        rng.choice((0, 0.01)),
        rng.random() < 0.5,
        rng.random() < 0.5,
        rng.choice((0, 0, 0.02, 0.05)),
    )
    problem = Problem(
        slots,
        SiteLimits(8, 8, rng.choice((0, 8, 8)), rng.random() < 0.7),
        battery,
        rng.choice(("value", "value", "value", "preserve_initial")),
        rng.choice((0, 0.3)),
        (),
        "UTC",
        minimum_mode_minutes=rng.choice((30, 60, 120)),
        initial_dispatch_mode=rng.choice(("SELF_CONSUME", "CHARGE_PV")),
        initial_dispatch_mode_since=START
        - timedelta(minutes=rng.choice((0, 1, 15, 16, 29))),
    )
    if rng.random() < 0.3:
        problem = replace(
            problem,
            initial_battery_mode=rng.choice(("charge", "discharge")),
            initial_battery_mode_since=START
            - timedelta(minutes=rng.choice((0, 15, 45))),
        )
    return problem


def corpus(seed: int, count: int):
    rng = random.Random(seed)
    return [corpus_problem(rng) for _ in range(count)]


def activity(problem: Problem) -> float:
    return problem.minimum_mode_power_kw * 0.25


def starts_hold_on_a_surplus(problem: Problem, plan) -> list[int]:
    """List the committed intervals where HOLD starts although CHARGE_PV is open.

    Written from the rows alone: a row that continues the carried commitment (a
    bridge) counts as PV-follow, a full pack has nothing left to take (the observed
    pack included, T-037), and a direction the legacy guard blocks is not open.
    """
    if safety_exception(problem):
        return []
    held_until = problem.initial_dispatch_mode_since + timedelta(
        minutes=problem.minimum_mode_minutes
    )
    battery = problem.battery
    ceiling = battery.capacity_kwh * battery.maximum_soc_fraction
    guard_until = (
        problem.initial_battery_mode_since
        + timedelta(minutes=problem.minimum_mode_minutes)
        if problem.initial_battery_mode
        else None
    )
    hits = []
    holding = False
    left = battery.initial_kwh
    for i, (slot, flow) in enumerate(zip(problem.slots, plan.flows, strict=True)):
        if slot.start >= held_until:
            break
        bridge = i in plan.carried_bridges
        if flow.dispatch_mode == "HOLD" and not bridge:
            surplus = slot.pv_kwh - slot.load_kwh
            full = left >= ceiling - activity(problem) - 1e-3
            open_surplus = min(battery.charge_kw * 0.25, surplus) >= activity(problem)
            # A direction the legacy guard blocks is not open.
            if problem.initial_battery_mode == "discharge" and slot.start < guard_until:
                open_surplus = False
            if not holding and open_surplus and not full:
                hits.append(i)
            holding = True
        elif not bridge:
            holding = False
        left = flow.end_soc_kwh
    return hits


def run(problem: Problem):
    try:
        return solve(problem)
    except SolveError as error:
        return error


def test_every_committed_problem_is_planned_or_infeasible_never_rejected():
    planned = 0
    for index, problem in enumerate(corpus(11, 300)):
        result = run(problem)
        if isinstance(result, SolveError):
            # `solver_failure` is the validator disagreeing with the model.
            assert result.reason == "infeasible", (index, str(result))
            continue
        planned += 1
        assert starts_hold_on_a_surplus(problem, result) == [], index
    assert planned > 100


def test_flexible_load_probes_are_planned_or_infeasible_never_rejected():
    rng = random.Random(23)
    planned = 0
    for index, problem in enumerate(corpus(11, 120)):
        request = FlexibleLoadRequest(
            rng.choice((0.1, 0.3, 1.0)), rng.choice((0.5, 2.0))
        )
        try:
            result = solve_flexible_load(problem, request)
        except SolveError as error:
            assert error.reason == "infeasible", (index, str(error))
            continue
        planned += 1
        # The plan is certified against the load as realised, probe included.
        realised = replace(
            problem,
            slots=tuple(
                replace(slot, load_kwh=slot.load_kwh + extra)
                for slot, extra in zip(problem.slots, result.schedule_kwh, strict=True)
            ),
        )
        assert starts_hold_on_a_surplus(realised, result.plan) == [], index
    assert planned > 30


@pytest.mark.parametrize("seed", (3, 5))
def test_a_commitment_always_has_a_plan_when_nothing_else_stands_in_the_way(seed):
    # No terminal rule to meet, no standby loss, the site takes any export and the
    # pack leaves room to charge and energy to give: what is left of the commitment
    # is the committed intervals that offer neither SELF_CONSUME nor CHARGE_PV.
    for index, problem in enumerate(corpus(seed, 300)):
        battery = problem.battery
        free = (
            problem.terminal_mode == "value"
            and battery.idle_drain_kw == 0
            and problem.site.grid_export_kw > 0
            and problem.initial_battery_mode is None
            and CAPACITY * battery.minimum_soc_fraction + 0.3
            < battery.initial_kwh
            <= CAPACITY - 0.3
        )
        if free:
            result = run(problem)
            assert not isinstance(result, SolveError), (seed, index, str(result))
