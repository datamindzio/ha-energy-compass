"""A carried PV-follow commitment is released where the terminal rule cannot be met (T-065).

`preserve_initial` asks for the pack to end where it began. A carried CHARGE_PV
or SELF_CONSUME forbids HOLD while the pack can follow, and SELF_CONSUME draws the
house's net load from it: with no PV surplus later to put it back, the problem had
no plan, though the boundary before had planned the same slot from a longer horizon
(`benchmarks/curtail_sweep.py` deproll seed 11 draw 293, T-058). The lock floor now
counts the terminal requirement among what a slot owes (`dispatch_policy._owed`), so
the interval may be a HOLD released from the pack, as for a standby loss (ADR-0004).

That floor counts a pack the terminal rule has spent as depleted, which a HOLD released
from a *full* pack does not go back on: the coordinator carries it as a plain HOLD with a
clock of its own, so a return planned for it is a plan the next boundary cannot continue
(draw 241 of seed 33 of `benchmarks/curtail_sweep.py fullroll`, T-065 review).
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_carried_commitment_guard_probes import TIE_761, committed, modes

from benchmarks.curtail_sweep import follow, roll
from custom_components.energy_compass.engine import dispatch_policy as policy
from custom_components.energy_compass.engine.models import (
    Battery,
    Flow,
    SiteLimits,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import solve

START = datetime(2026, 12, 18, 12, 30, tzinfo=UTC)
# The T-065 pack: 10 kWh, reserve 1 kWh, charge 1 kW, no grid charge, 0.323 kWh above reserve.
PACK = Battery(10, 0.1, 1, 1.323, 1, 4, 0.95, 0.95, 0, False, False, 0)
# 0.3 kWh of PV against a 0.4 kWh load, then 0.1 kWh of surplus: the surplus puts back
# 0.095 kWh, the house draws 0.105 kWh from the pack.
ROWS = [(0.3, 0.4, 0.8, 0.2), (0.3, 0.2, 0.8, 0.2)]


def problem(rows=ROWS, battery=PACK, mode="CHARGE_PV", **changes):
    return committed(
        rows,
        battery,
        mode,
        120,
        90,
        start=START,
        terminal_mode=changes.pop("terminal_mode", "preserve_initial"),
        **changes,
    )


@pytest.mark.parametrize("mode", ["CHARGE_PV", "SELF_CONSUME"])
@pytest.mark.parametrize("slots", [1, 2])
def test_a_terminal_rule_the_house_would_break_releases_the_commitment(mode, slots):
    plan = solve(problem(ROWS[:slots], mode=mode))
    assert modes(plan)[0] == "HOLD"
    assert plan.flows[0].discharge_kwh == pytest.approx(0)
    assert plan.flows[-1].end_soc_kwh >= 1.323 - 1e-6


def test_the_release_is_a_valid_plan_for_the_validator():
    source = problem()
    plan = solve(source)
    policy.validate_modes(source, plan.flows, plan.carried_bridges)


def test_a_later_surplus_that_refills_the_pack_keeps_the_commitment():
    rows = [ROWS[0], (1.0, 0.2, 0.8, 0.2), (1.0, 0.2, 0.8, 0.2)]
    plan = solve(problem(rows))
    assert modes(plan)[0] == "SELF_CONSUME"
    assert plan.flows[-1].end_soc_kwh >= 1.323 - 1e-6


def test_a_terminal_value_asks_nothing_of_the_pack():
    plan = solve(problem(terminal_mode="value", terminal_value_per_kwh=0.0))
    assert modes(plan)[0] == "SELF_CONSUME"


# Draw 241 of seed 33 of `benchmarks/curtail_sweep.py fullroll`: a carried SELF_CONSUME since
# the first interval on a pack 0.01 kWh below its 10 kWh ceiling, a standby loss of 0.05 kW,
# an export cap of 0.5 kW (0.125 kWh an interval) and `preserve_initial`. The first interval
# is a HOLD released from the full pack; the PV of the second (0.16 kWh over the house)
# is above the cap. (PV, load, buy, sell) per interval.
FULL_241 = [
    (0.218, 0.1, 1.08, 0.07),
    (0.26, 0.1, 0.24, 0.12),
    (0.1, 0.1, 0.41, 0.57),
    (0.106, 0.1, 0.43, 0.04),
    (0.1, 0.1, 0.39, 0.79),
    (0.1, 0.1, 1.35, 0.43),
    (0.1, 0.1, 1.38, 0.1),
    (0.112, 0.1, 0.99, 0.27),
]


def full_241():
    source = committed(
        FULL_241,
        Battery(10, 0.1, 1, 9.99, 4, 4, 0.95, 0.95, 0.01, False, False, 0.05),
        "SELF_CONSUME",
        60,
        0,
        start=datetime(2026, 12, 18, 11, 0, tzinfo=UTC),
        terminal_mode="preserve_initial",
    )
    return replace(source, site=SiteLimits(3, 8, 0.5, False))


def test_a_pack_spent_only_by_the_terminal_rule_does_not_take_a_full_pack_hold_back():
    # The terminal requirement (about 9.99 kWh) lifts the lock floor of the second
    # interval to 9.987 kWh, above the 9.9775 kWh the HOLD leaves: the pack counts as
    # depleted for the release, not for the return of a run that began on a full pack.
    source = full_241()
    drains = policy.drain_schedule(source)
    held_until = source.initial_dispatch_mode_since + timedelta(minutes=60)
    floor = policy._lock_floor(
        source, drains, policy._owed(source, drains, held_until), 1
    )
    assert floor > 9.98
    row = roll(source)
    # Planned at the first boundary and followed to the end, or no plan at the first
    # boundary (as on `main`): never a plan the second boundary contradicts.
    assert row[:2] in (["err", 0], ["ok", len(FULL_241) - 1])


def flows_241():
    """The plan the lock floor alone accepted: HOLD, back to CHARGE_PV, HOLD again."""
    rows = [
        (0.0, 0.118, 0.0, 0.0, 9.9775, "discharge", "HOLD"),
        (
            0.0,
            0.12315789473684383,
            0.03684210526315618,
            0.0,
            10.0,
            "charge",
            "CHARGE_PV",
        ),
        (0.0, 0.0, 0.0, 0.0, 9.9875, "discharge", "HOLD"),
        (0.0, 0.006, 0.0, 0.0, 9.975, "discharge", "HOLD"),
        (0.0, 0.0, 0.0, 0.0, 9.9625, "discharge", "HOLD"),
        (0.0, 0.0, 0.0, 0.0, 9.95, "discharge", "HOLD"),
        (0.0, 0.0, 0.0, 0.0, 9.9375, "discharge", "HOLD"),
        (0.0, 0.012, 0.0, 0.0, 9.925, "discharge", "HOLD"),
    ]
    return [
        Flow(imported, exported, charged, discharged, 0.0, soc, battery, dispatch)
        for imported, exported, charged, discharged, soc, battery, dispatch in rows
    ]


def test_the_validator_refuses_the_return_of_a_full_pack_hold_on_the_terminal_floor():
    source = full_241()
    with pytest.raises(SolveError, match="minimum mode duration"):
        policy.validate_modes(source, flows_241(), (2, 3))


def test_both_readings_of_a_bridge_or_released_hold_tie_roll_on_from_the_same_state():
    # TIE_761 (`test_carried_commitment_guard_probes.py`): the last interval is a bridge
    # or, since T-065, a HOLD run released from a pack that cannot refill for the terminal
    # rule. Same flows and cost, so the solver may return either. The coordinator stores
    # the first interval only, and that one is a bridge in both readings: the next
    # boundary gets the same problem, and the last interval is the first of a problem
    # of its own, which reads its own tie.
    plan = solve(TIE_761)
    nexts = []
    for reading in ((0, 1, 2), (0, 1)):
        released = policy.validate_modes(TIE_761, plan.flows, reading)
        assert (2 in released) == (reading == (0, 1))
        read = replace(
            plan, carried_bridges=reading, released_holds=tuple(sorted(released))
        )
        nexts.append(follow(TIE_761, read))
    assert nexts[0] == nexts[1]
    source = nexts[0]
    while source.slots:
        source = follow(source, solve(source))
