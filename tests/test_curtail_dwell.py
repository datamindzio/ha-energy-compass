"""A CURTAIL run goes on through intervals with too little PV to curtail (T-037).

On an export-capped site a pack that is full (or nearly so) has to curtail what the
cap cannot pass, and CURTAIL lasts the minimum duration. When PV drops inside it
there is nothing left to curtail: the HOLD of such an interval is the run going on,
it counts for the CURTAIL clock and starts no HOLD clock, and it lasts only until
the run's own duration is up. A carried PV-follow commitment whose pack cannot take
what the cap forces ends in the same release as a full pack.
"""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_dispatch_policy import dispatch

from benchmarks.curtail_sweep import follow, make_depleted
from benchmarks.curtail_sweep import roll as sweep
from custom_components.energy_compass.coordinator import EnergyCompassCoordinator
from custom_components.energy_compass.engine.dispatch_policy import (
    reserve_fallback,
    safety_exception,
    uncommitted_fallback,
    validate_modes,
)
from custom_components.energy_compass.engine.models import (
    Battery,
    FlexibleLoadRequest,
    Flow,
    InputError,
    SiteLimits,
    SolveError,
)
from custom_components.energy_compass.engine.normalize import validate_problem
from custom_components.energy_compass.engine.optimize import (
    solve,
    solve_flexible_load,
    solve_released,
)
from custom_components.energy_compass.runtime import restore_commitment

CURTAIL = Flow(0, 0.5, 0, 0, 0.3, 10, None, "CURTAIL")
HOLD = Flow(0.1, 0, 0, 0, 0, 10, None, "HOLD")
SELF = Flow(0, 0, 0, 0.1, 0, 9.9, "discharge", "SELF_CONSUME")


def capped(pv, *, initial=10.0, export_cap=2, sell=0.9, **changes):
    problem = dispatch(
        tuple((1.0, sell, value, 0.1) for value in pv), minimum_mode_minutes=60
    )
    return replace(
        problem,
        site=replace(problem.site, grid_export_kw=export_cap),
        battery=replace(problem.battery, initial_kwh=initial),
        **changes,
    )


def carried(problem, *, since=timedelta(minutes=15), mode="CHARGE_PV"):
    return replace(
        problem,
        initial_dispatch_mode=mode,
        initial_dispatch_mode_since=problem.slots[0].start - since,
    )


@pytest.mark.parametrize("initial", [9.9, 10.0])
def test_curtail_goes_on_when_pv_drops_inside_its_minimum_duration(initial):
    problem = capped((0.9, 0.9, *(0.0,) * 6), initial=initial)
    plan = solve(problem)
    modes = [flow.dispatch_mode for flow in plan.flows]
    assert modes[:4] == ["CURTAIL", "CURTAIL", "HOLD", "HOLD"]
    assert all(flow.curtail_kwh > 0.2 for flow in plan.flows[:2])
    # The two HOLD rows are the run going on; the commitment keeps its clock there.
    assert plan.carried_bridges == (2, 3)


def test_curtail_bridge_lasts_only_until_the_runs_own_duration_is_up():
    plan = solve(capped((0.9, 0.9, *(0.0,) * 10)))
    assert plan.carried_bridges == (2, 3)
    assert [flow.dispatch_mode for flow in plan.flows[:2]] == ["CURTAIL"] * 2


def test_curtail_with_pv_all_along_needs_no_bridge():
    plan = solve(capped((0.9,) * 8))
    assert [flow.dispatch_mode for flow in plan.flows] == ["CURTAIL"] * 8
    assert plan.carried_bridges == ()


def test_pv_returning_inside_the_run_curtails_again():
    plan = solve(capped((0.9, 0.9, 0.0, 0.9, 0.9, 0.9, 0.9, 0.9)))
    assert [flow.dispatch_mode for flow in plan.flows] == [
        *("CURTAIL",) * 2,
        "HOLD",
        *("CURTAIL",) * 5,
    ]
    assert plan.carried_bridges == (2,)


def test_curtail_run_started_before_the_horizon_goes_on_through_a_pv_gap():
    problem = capped((0.0, 0.0, 0.9, 0.9, 0.9, 0.9), initial=10.0)
    problem = carried(problem, since=timedelta(minutes=15), mode="CURTAIL")
    plan = solve(problem)
    modes = [flow.dispatch_mode for flow in plan.flows]
    # The run began 15 min before the horizon: 45 min of its duration are left.
    assert modes[:3] == ["HOLD", "HOLD", "CURTAIL"]
    assert plan.carried_bridges[:2] == (0, 1)


def test_hold_after_the_curtail_run_has_its_own_clock():
    # The bridge rows are CURTAIL's: the HOLD run starts when the duration is up, so
    # a mode that needs HOLD to be over cannot follow right after the bridge.
    plan = solve(capped((0.9, 0.9, *(0.0,) * 6)))
    assert plan.carried_bridges == (2, 3)
    hold_run = [i for i, flow in enumerate(plan.flows) if flow.dispatch_mode == "HOLD"]
    assert hold_run[:2] == [2, 3]


def test_validator_reads_hold_in_a_dry_interval_as_the_curtail_run_going_on():
    problem = capped((0.9, 0.9, *(0.0,) * 6))
    validate_modes(problem, (CURTAIL, CURTAIL, *(HOLD,) * 6))


def test_validator_gives_the_hold_after_the_run_its_own_clock():
    problem = capped((0.9, 0.9, *(0.0,) * 6))
    # HOLD starts at slot 4 (after the bridge) and lasts to slot 7.
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (CURTAIL, CURTAIL, *(HOLD,) * 4, HOLD, SELF))
    validate_modes(problem, (CURTAIL, CURTAIL, *(HOLD,) * 4, HOLD, HOLD))


def test_validator_rejects_a_mode_change_inside_the_run_that_is_not_a_bridge():
    problem = capped((0.9, 0.9, *(0.0,) * 6))
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (CURTAIL, CURTAIL, SELF, *(HOLD,) * 5))
    # An interval with PV to curtail is not a bridge: HOLD there ends the run early.
    problem = capped((0.9, 0.9, 0.9, 0.0, 0.0, 0.0, 0.0, 0.0))
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (CURTAIL, HOLD, *(HOLD,) * 6))


def test_carried_commitment_curtails_when_the_pack_cannot_take_what_the_cap_forces():
    # Slot 1 has 0.1 kWh of room and 0.8 kWh of surplus beside a 0.5 kWh cap:
    # CHARGE_PV cannot fill the pack and curtail in one interval, so the pack that
    # cannot take what the cap forces ends the commitment the way a full one does.
    problem = carried(
        capped((0.5, *(0.9,) * 7), initial=9.5), since=timedelta(minutes=15)
    )
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == [
        "CHARGE_PV",
        *("CURTAIL",) * 7,
    ]
    assert plan.flows[1].curtail_kwh >= 0.3 - 1e-6


def test_carried_commitment_keeps_charging_when_the_pack_can_take_the_surplus():
    # Room for the whole 0.3 kWh above the cap: the commitment goes on.
    problem = carried(
        capped((0.5, *(0.9,) * 7), initial=7.0), since=timedelta(minutes=15)
    )
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows[:3]] == ["CHARGE_PV"] * 3


def test_hold_with_no_curtail_run_before_it_is_not_a_bridge():
    # Little PV in the first three intervals, but no CURTAIL run to go on: the HOLD
    # that goes on from the carried one is a plain HOLD, not a CURTAIL bridge.
    rows = (
        (1.05, 0.55, 0.0, 0.4),
        (1.43, 0.47, 0.02, 0.02),
        (0.82, 0.29, 0.01, 0.1),
        (1.45, 0.02, 0.63, 0.02),
        (0.83, 0.58, 0.56, 0.1),
        (0.38, 0.06, 0.64, 0.2),
        (0.91, 0.59, 0.73, 0.05),
    )
    problem = dispatch(rows, minimum_mode_minutes=60)
    problem = replace(
        problem,
        site=replace(problem.site, inverter_kw=3, grid_export_kw=0.5),
        battery=replace(
            problem.battery,
            initial_kwh=9.9,
            charge_kw=1,
            discharge_kw=3,
            allow_grid_charge=False,
            allow_battery_export=False,
            wear_per_kwh=0.0,
            idle_drain_kw=0.05,
        ),
        terminal_mode="value",
        terminal_value_per_kwh=0.3,
        initial_dispatch_mode="HOLD",
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=29),
    )
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows[:4]] == [
        "HOLD",
        "HOLD",
        "HOLD",
        "CURTAIL",
    ]
    assert plan.carried_bridges == ()


def test_curtail_bridge_ends_with_the_run_even_when_the_pack_alone_decides():
    # PV 0.73, 0.63, 0.1 and then none: the run began in the first interval, so the
    # HOLD at 45 minutes is inside its hour and goes on, the one at 60 minutes is not.
    rows = (
        (1.32, 0.1, 0.73, 0.05),
        (0.55, 0.58, 0.63, 0.4),
        (1.06, 0.63, 0.1, 0.1),
        (1.24, 0.42, 0.0, 0.02),
        (0.97, 0.18, 0.0, 0.1),
        (1.36, 0.14, 0.0, 0.05),
    )
    problem = dispatch(rows, minimum_mode_minutes=60)
    problem = replace(
        problem,
        site=replace(problem.site, grid_export_kw=2),
        battery=replace(
            problem.battery,
            initial_kwh=10.0,
            charge_kw=3,
            discharge_kw=1,
            allow_grid_charge=False,
            allow_battery_export=True,
            wear_per_kwh=0.01,
            idle_drain_kw=0.0,
        ),
        terminal_mode="value",
        terminal_value_per_kwh=0.0,
    )
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == [
        "CURTAIL",
        "CURTAIL",
        "CURTAIL",
        "HOLD",
        "HOLD",
        "HOLD",
    ]
    assert plan.carried_bridges == (3,)


def test_curtail_bridge_follows_the_run_not_any_forced_interval_before_it():
    # The forced interval at 0.62 kWh lies inside the hour of the later ones, but the
    # HOLD at the end follows a CURTAIL run only because the interval before it is one.
    problem = capped((0.0, 0.62, 0.0, 0.9, 0.0, 0.0, 0.9, 2.2, 0.0), initial=0.5)
    problem = replace(
        problem, battery=replace(problem.battery, minimum_soc_fraction=0.05)
    )
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows[6:]] == [
        "CURTAIL",
        "CURTAIL",
        "HOLD",
    ]
    # Since T-047 the CURTAIL of the 0.62 kWh interval may take a HOLD over, so the plan
    # no longer grid-charges to keep clear of it. Its dry intervals are bridges as well,
    # and a dry interval is one only after a CURTAIL row or another bridge row.
    modes = [flow.dispatch_mode for flow in plan.flows]
    assert modes == [
        "HOLD",
        "CURTAIL",
        "HOLD",
        "CURTAIL",
        "HOLD",
        "HOLD",
        "CURTAIL",
        "CURTAIL",
        "HOLD",
    ]
    assert 8 in plan.carried_bridges
    for index in plan.carried_bridges:
        assert modes[index] == "HOLD"
        assert modes[index - 1] == "CURTAIL" or index - 1 in plan.carried_bridges


def test_hold_after_a_long_curtail_run_is_not_a_bridge():
    # Nine CURTAIL intervals in a row, then dry ones: every CURTAIL began and ended
    # its own hour long ago, so the HOLD that follows is a plain HOLD with its clock.
    rows = (
        (0.79, 0.2, 1.9, 0.1),
        (0.24, 0.02, 2.14, 0.1),
        (0.95, 0.03, 1.61, 0.05),
        (1.31, 0.54, 1.63, 0.05),
        (1.09, 0.71, 0.1, 0.05),
        (0.25, 0.01, 1.97, 0.4),
        (0.86, 0.67, 1.64, 0.02),
        (0.22, 0.02, 1.51, 0.1),
        (0.44, 0.26, 1.49, 0.1),
        (0.66, 0.1, 0.02, 0.4),
        (0.47, 0.25, 0.02, 0.05),
    )
    problem = dispatch(rows, minimum_mode_minutes=60)
    problem = replace(
        problem,
        site=replace(problem.site, inverter_kw=3, grid_export_kw=0.5),
        battery=replace(
            problem.battery,
            initial_kwh=9.95,
            charge_kw=1,
            discharge_kw=1,
            allow_grid_charge=False,
            allow_battery_export=False,
            wear_per_kwh=0.0,
            idle_drain_kw=0.02,
        ),
        terminal_mode="value",
        terminal_value_per_kwh=0.3,
    )
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows[8:]] == [
        "CURTAIL",
        "HOLD",
        "HOLD",
    ]
    assert plan.carried_bridges == ()


def roll(problem, steps):
    """Plan, carry out the first interval, plan again: every step has a plan."""
    modes = []
    for _ in range(steps):
        plan = solve(problem)
        modes.append(plan.flows[0].dispatch_mode)
        problem = follow(problem, plan)
    return modes


@pytest.mark.parametrize("mode", ["CHARGE_PV", "SELF_CONSUME"])
def test_a_carried_fill_and_curtail_plan_can_be_followed_to_the_next_boundary(mode):
    # The plan fills the pack in the first interval and curtails from the second. One
    # interval later the pack is full and the commitment is carried: the same release
    # that planned the curtail must let the first interval of that plan go to CURTAIL.
    problem = carried(capped((0.5, *(0.9,) * 11), initial=9.5), mode=mode)
    modes = roll(problem, 5)
    assert modes[0] != "CURTAIL"
    assert modes[1:] == ["CURTAIL"] * 4


@pytest.mark.parametrize("mode", ["CHARGE_PV", "SELF_CONSUME"])
@pytest.mark.parametrize("initial", [9.9, 9.95])
def test_carried_commitment_curtails_in_the_first_interval_when_the_pack_is_full(
    mode, initial
):
    # 0.3 kWh above the cap and 0.1 kWh of room, the carried commitment cannot take
    # it: the first interval is the release, as it is for every later one.
    problem = carried(capped((0.9,) * 8, initial=initial), mode=mode)
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == ["CURTAIL"] * 8
    assert plan.flows[0].curtail_kwh >= 0.3 - 1e-6


@pytest.mark.parametrize("mode", ["CHARGE_PV", "SELF_CONSUME"])
def test_carried_commitment_holds_in_the_first_interval_when_the_pack_is_full(mode):
    # Nothing to curtail, nothing to take: a pack a hair below the ceiling is full.
    problem = carried(capped((0.9,) * 8, initial=9.99, export_cap=8), mode=mode)
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == ["HOLD"] * 8
    assert plan.carried_bridges == ()


def test_carried_commitment_with_room_charges_in_the_first_interval():
    problem = carried(capped((0.9,) * 8, initial=5.0, export_cap=8))
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows[:3]] == ["CHARGE_PV"] * 3


@pytest.mark.parametrize("mode", ["CHARGE_PV", "SELF_CONSUME"])
@pytest.mark.parametrize(
    ("pv", "since", "bridges"),
    [
        # The carried hour is up at the third interval: CURTAIL runs from the second.
        ((0.5, 0.9, *(0.0,) * 6), 15, (2, 3, 4)),
        ((0.5, 0.9, 0.9, *(0.0,) * 5), 0, (3, 4)),
        # The commitment's hour is over at once, the run is the one that counts.
        ((0.5, 0.9, *(0.0,) * 6), 45, (2, 3, 4)),
    ],
)
def test_a_curtail_run_and_the_carried_commitment_each_keep_their_own_clock(
    mode, pv, since, bridges
):
    # The commitment ends in a release (CURTAIL), and the dry intervals that follow
    # are the run going on: the commitment's HOLD-release rule does not apply to them.
    problem = carried(
        capped(pv, initial=9.5), since=timedelta(minutes=since), mode=mode
    )
    plan = solve(problem)
    modes = [flow.dispatch_mode for flow in plan.flows]
    assert modes[1] == "CURTAIL"
    assert modes[len(pv) - 6 :] == ["HOLD"] * 6 or modes[-1] == "HOLD"
    assert plan.carried_bridges == bridges
    # And it can be followed: the next plan, from the boundary after, exists.
    roll(problem, 3)


@pytest.mark.parametrize("load", [0.01, 0.0])
def test_a_dry_interval_with_no_load_goes_on_with_the_curtail_run_too(load):
    # Below the minimum activity the row is an idle one, whose HOLD would otherwise
    # be a plain HOLD run of its own: it has to be the CURTAIL run going on as well.
    problem = carried(capped((0.5, 0.9, *(0.0,) * 6), initial=9.5))
    problem = replace(
        problem, slots=tuple(replace(slot, load_kwh=load) for slot in problem.slots)
    )
    plan = solve(problem)
    modes = [flow.dispatch_mode for flow in plan.flows]
    assert modes[1] == "CURTAIL"
    assert modes[2:5] == ["HOLD"] * 3
    assert plan.carried_bridges == (2, 3, 4)


def test_validator_accepts_hold_in_the_first_interval_of_a_full_pack():
    problem = carried(capped((0.9,) * 8, initial=9.99, export_cap=8))
    plan = solve(problem)
    assert plan.flows[0].dispatch_mode == "HOLD"
    validate_modes(problem, plan.flows)


def test_validator_rejects_hold_in_the_first_interval_when_the_pack_has_room():
    problem = carried(capped((0.9,) * 8, initial=5.0, export_cap=8))
    flows = solve(problem).flows
    # What the plan does, with the first interval left idle: the pack could charge.
    idle = replace(
        flows[0],
        grid_export_kwh=0.8,
        charge_kwh=0,
        end_soc_kwh=5.0,
        battery_mode=None,
        dispatch_mode="HOLD",
    )
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (idle, *flows[1:]))


# A HOLD takes no curtailment (T-047): in an interval whose PV the site cannot pass,
# a HOLD that has to last has no legal mode, and the CURTAIL the inverter does by itself
# takes it over. The run is a CURTAIL run from there on, with its own clock.


@pytest.mark.parametrize(
    ("problem", "modes"),
    [
        # A carried HOLD, then PV above the cap inside its minimum duration.
        (
            carried(capped((0.1, *(0.9,) * 5)), mode="HOLD"),
            ["HOLD", *("CURTAIL",) * 5],
        ),
        # The HOLD that the safety exception puts in place of a CHARGE_PV at the maximum.
        (carried(capped((0.9,) * 8), mode="CHARGE_PV"), ["CURTAIL"] * 8),
        # A CURTAIL run whose duration ended in a PV gap: the HOLD that starts there.
        (
            carried(
                capped((0, 0, *(0.9,) * 5)), since=timedelta(minutes=60), mode="CURTAIL"
            ),
            ["HOLD", "HOLD", *("CURTAIL",) * 5],
        ),
        # The same inside a free plan: a run, its gap, and PV back inside the HOLD's hour.
        (
            capped((0.9, 0.9, 0, 0, 0, 0.9, 0.9, 0.9, 0.9, 0.9)),
            ["CURTAIL"] * 2 + ["HOLD"] * 3 + ["CURTAIL"] * 5,
        ),
    ],
)
def test_curtail_takes_over_a_hold_that_cannot_pass_the_pv(problem, modes):
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == modes
    validate_modes(problem, plan.flows)
    # And it goes on when each plan is followed one interval and planned again.
    roll(problem, len(modes) - 2)


def test_validator_reads_curtail_as_a_takeover_only_where_the_site_cannot_pass_the_pv():
    # PV below the cap leaves HOLD a legal mode: CURTAIL there would be a label to leave
    # the minimum duration with, so the validator refuses it. The same rows on a PV the
    # cap cannot pass are the takeover: the need is what decides, not the run's length.
    curtail = Flow(0, 0.1, 0, 0, 0.3, 10, None, "CURTAIL")
    hold = Flow(0, 0, 0, 0, 0, 10, None, "HOLD")
    rows = (hold, *(curtail,) * 4, hold)
    below = carried(capped((0.1, *(0.5,) * 4, 0.1)), mode="HOLD")
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(below, rows)
    above = carried(capped((0.1, *(0.9,) * 4, 0.1)), mode="HOLD")
    validate_modes(above, rows)


def test_a_hold_is_taken_over_by_curtail_only_where_the_model_has_a_need_for_it():
    # PV below the cap leaves HOLD a legal mode. With a sell price below zero a CURTAIL
    # pays there, but the takeover is not offered: the HOLD lasts its hour, then CURTAIL.
    # The PV above the cap at the end makes the takeover an option of the model at all.
    pv = (0.1, *(0.5,) * 5, 0.9, 0.9)
    for sell in (0.9, -0.5):
        problem = carried(capped(pv, sell=sell), mode="HOLD")
        plan = solve(problem)
        modes = [flow.dispatch_mode for flow in plan.flows]
        assert modes[:3] == ["HOLD"] * 3
        validate_modes(problem, plan.flows)
    assert modes[3:] == ["CURTAIL"] * 5


def test_validator_reads_curtail_in_a_forced_interval_as_the_hold_taken_over():
    problem = carried(capped((0.1, 0.9, 0.9, 0.9, 0.9, 0.9)), mode="HOLD")
    rest = (CURTAIL,) * 5
    validate_modes(problem, (HOLD, *rest))
    # The run lasts its own minimum duration: nothing else may follow inside it.
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (HOLD, CURTAIL, CURTAIL, SELF, *(HOLD,) * 2))


def test_hold_after_the_run_that_took_a_hold_over_has_its_own_clock():
    problem = carried(capped((0.1, 0.9, 0.9, 0.9, 0.9, 0.0, 0.0, 0.0)), mode="HOLD")
    plan = solve(problem)
    modes = [flow.dispatch_mode for flow in plan.flows]
    assert modes[:5] == ["HOLD", *("CURTAIL",) * 4]
    # The HOLD that follows lasts its own hour: no other mode ends inside it.
    assert modes[5:] == ["HOLD"] * 3
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (HOLD, *(CURTAIL,) * 4, HOLD, HOLD, SELF))


def test_validator_does_not_read_curtail_as_taking_over_a_released_hold():
    # A HOLD released from a PV-follow commitment (the pack depleted, not full) has no
    # clock of its own to take over; a CURTAIL after it is not the commitment going on
    # (the way back is CHARGE_PV or SELF_CONSUME, ADR-0005 amendment T-050).
    problem = capped((0.0, *(0.9,) * 5), initial=0.01)
    problem = carried(problem, mode="SELF_CONSUME")
    idle = Flow(0, 0, 0, 0, 0, 0.01, None, "HOLD")
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (idle, *(CURTAIL,) * 5))


def probed(problem, energy, power):
    """Plan the problem with a flexible load; the plan is certified against its probe."""
    return solve_flexible_load(problem, FlexibleLoadRequest(energy, power))


@pytest.mark.parametrize("mode", ["HOLD", "CHARGE_PV"])
def test_probe_that_makes_hold_legal_is_planned_under_a_negative_sell_price(mode):
    # The sell price below zero makes a takeover CURTAIL worth more than a HOLD that the
    # probe made legal. The takeover is offered only for the probe loads that leave the
    # PV a need as realised (the validator reads it so), and `solve_flexible_load`
    # raised solver_failure "minimum mode duration" where the model offered it for all.
    problem = carried(capped((0.9,) * 8, sell=-0.5), mode=mode)
    plan = probed(problem, 4.0, 2).plan
    assert [flow.dispatch_mode for flow in plan.flows] == [
        *("HOLD",) * 3,
        *("CURTAIL",) * 5,
    ]


@pytest.mark.parametrize(
    ("pv", "modes"),
    [
        ((0.9, *(0.0,) * 5), ["HOLD"] * 6),
        ((0.1, *(0.9,) * 5), ["HOLD", *("CURTAIL",) * 5]),
    ],
)
def test_probe_in_a_hold_that_must_last_is_planned_under_a_negative_sell_price(
    pv, modes
):
    problem = carried(capped(pv, sell=-0.5), mode="HOLD")
    plan = probed(problem, 0.5, 2).plan
    assert [flow.dispatch_mode for flow in plan.flows] == modes


@pytest.mark.parametrize("energy", [0.25, 0.5, 1.0, 1.5])
@pytest.mark.parametrize("sell", [-0.5, 0.0, 0.9])
@pytest.mark.parametrize(
    "pv", [(0.9,) * 6, (0.1, *(0.9,) * 5), (0.9, 0.9, 0.0, 0.0, 0.0, 0.9)]
)
def test_probes_around_the_need_are_planned_in_a_hold_that_must_last(pv, sell, energy):
    # The need is 0.3 kWh in a 0.9 kWh interval; the probes run from none to more than
    # the need in an interval, at a power that leaves a need and at one that does not.
    for power in (1.2, 2.0):
        problem = carried(capped(pv, sell=sell), mode="HOLD")
        plan = probed(problem, energy, power).plan
        assert len(plan.flows) == len(pv)


@pytest.mark.parametrize("mode", ["SELF_CONSUME", "CHARGE_PV"])
@pytest.mark.parametrize(
    ("pv", "export_cap", "initial", "committed", "released"),
    [
        (
            (0, 0.9, 0.9, 0.9, 0.9, 0.9),
            2,
            0.01,
            ("HOLD", "CHARGE_PV", "CHARGE_PV"),
            (0,),
        ),
        (
            (0.05, 0.3, 0.5, 0.7, 0.9, 0.9),
            0,
            0.01,
            ("HOLD", "CHARGE_PV", "CHARGE_PV"),
            (0,),
        ),
        # The first interval has a surplus the pack takes: the commitment goes on.
        ((0.9,) * 6, 2, 0.02, ("CHARGE_PV",) * 3, ()),
    ],
)
def test_a_depleted_carried_commitment_charges_the_pv_the_cap_cannot_pass(
    mode, pv, export_cap, initial, committed, released
):
    # The pack is too empty to go on, so the commitment is released into HOLD; where
    # the PV above the house cannot be exported the HOLD has no legal flow, and the
    # inverter charges (T-050). Every sunrise on a zero-export site. The intervals
    # inside the commitment's hour are pinned: the HOLD, then CHARGE_PV (CURTAIL is
    # offered only after a full pack).
    problem = carried(capped(pv, initial=initial, export_cap=export_cap), mode=mode)
    plan = solve(problem)
    validate_modes(problem, plan.flows, plan.carried_bridges)
    modes = [flow.dispatch_mode for flow in plan.flows]
    assert tuple(modes[:3]) == committed
    assert plan.released_holds == released


def test_validator_accepts_a_depleted_hold_that_goes_back_to_charge_pv():
    problem = carried(
        capped((0.0, *(0.9,) * 5), initial=0.01, export_cap=2), mode="SELF_CONSUME"
    )
    idle = Flow(0, 0, 0, 0, 0, 0.01, None, "HOLD")
    charges = tuple(
        Flow(0, 0, 0.8, 0, 0, 0.01 + 0.8 * n, "charge", "CHARGE_PV")
        for n in range(1, 6)
    )
    validate_modes(problem, (idle, *charges))


def test_validator_rejects_a_hold_released_from_a_full_pack_that_goes_back():
    # Only the depleted release returns (ADR-0005 point 2): the pack that was full
    # leaves the commitment for good unless it is depleted by the time it goes back, and
    # a member mode after its HOLD is a second change.
    problem = carried(capped((0.2,) * 6, initial=10.0), mode="CHARGE_PV")
    hold = Flow(0, 0.1, 0, 0, 0, 10, None, "HOLD")
    charge = Flow(0, 0, 0.1, 0, 0, 10, "charge", "CHARGE_PV")
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (hold, *(charge,) * 5))


def test_the_return_to_the_commitment_is_not_a_run_of_its_own():
    # The commitment's deadline is the only clock: a return that ends the horizon
    # (less than the minimum duration left) is the commitment going on, not a new
    # PV-follow run that has to last an hour.
    problem = carried(capped((0.0, 0.18), initial=0.01, export_cap=0), mode="CHARGE_PV")
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == ["HOLD", "CHARGE_PV"]


@pytest.mark.parametrize("export_cap", [0, 2])
def test_a_depleted_pack_leaves_and_re_enters_the_commitment_more_than_once(export_cap):
    # ADR-0005 point 17: the commitment's deadline is the only clock, so each time the
    # pack is empty again it may be released into HOLD, and each return is the
    # commitment going on. Two hours, the pack empties twice (T-050).
    pv = (0, 0.25, 0, 0, 0, 0.25, 0, 0)
    problem = dispatch(
        tuple((1.0, 0.9, value, 0.1) for value in pv), minimum_mode_minutes=120
    )
    problem = carried(
        replace(
            problem,
            site=replace(problem.site, grid_export_kw=export_cap),
            battery=replace(problem.battery, initial_kwh=0.0),
        ),
        since=timedelta(0),
    )
    plan = solve(problem)
    validate_modes(problem, plan.flows, plan.carried_bridges)
    assert [flow.dispatch_mode for flow in plan.flows] == [
        "HOLD",
        "CHARGE_PV",
        "SELF_CONSUME",
        "SELF_CONSUME",
        "HOLD",
        "CHARGE_PV",
        "SELF_CONSUME",
        "SELF_CONSUME",
    ]
    assert plan.released_holds == (0, 4)


def depleted(pv, *, mode="CHARGE_PV", export_cap=2, allow_curtailment=True, **changes):
    """A sunrise on an empty pack, a PV-follow commitment carried in (T-050)."""
    problem = carried(capped(pv, initial=0.01, export_cap=export_cap), mode=mode)
    return replace(
        problem,
        site=replace(problem.site, allow_curtailment=allow_curtailment),
        **changes,
    )


SUNRISES = [
    ((0, *(0.9,) * 7), 2),
    ((0.05, 0.3, 0.5, 0.7, *(0.9,) * 4), 0),
]


@pytest.mark.parametrize("allow_curtailment", [True, False])
@pytest.mark.parametrize("mode", ["SELF_CONSUME", "CHARGE_PV"])
@pytest.mark.parametrize(("pv", "export_cap"), SUNRISES)
def test_a_release_from_a_depleted_pack_is_carried_across_the_boundary(
    pv, export_cap, mode, allow_curtailment
):
    # The first plan releases into HOLD and charges from the next interval. Once the
    # HOLD is carried out the coordinator stores the commitment, its clock and the
    # release (not a HOLD with a clock of its own), so the plan from the boundary is the
    # charge the first one published: it was a HOLD to the end or no plan before.
    problem = depleted(
        pv, mode=mode, export_cap=export_cap, allow_curtailment=allow_curtailment
    )
    first = solve(problem)
    assert [flow.dispatch_mode for flow in first.flows[:3]] == [
        "HOLD",
        "CHARGE_PV",
        "CHARGE_PV",
    ]
    after = follow(problem, first)
    assert (after.initial_dispatch_mode, after.initial_dispatch_mode_since) == (
        mode,
        problem.initial_dispatch_mode_since,
    )
    assert after.initial_dispatch_released
    second = solve(after)
    assert [flow.dispatch_mode for flow in second.flows[:2]] == ["CHARGE_PV"] * 2
    # And on to the end of the commitment, step after step.
    assert roll(problem, 5)[:3] == ["HOLD", "CHARGE_PV", "CHARGE_PV"]


@pytest.mark.parametrize("mode", ["SELF_CONSUME", "CHARGE_PV"])
def test_a_released_hold_that_goes_on_is_carried_too(mode):
    # The pack is empty and the PV below the house for two intervals: the HOLD is
    # carried out twice, and the plan from the second boundary still goes back.
    problem = depleted((0, 0, *(0.9,) * 6), mode=mode)
    first = solve(problem)
    assert [flow.dispatch_mode for flow in first.flows[:3]] == [
        "HOLD",
        "HOLD",
        "CHARGE_PV",
    ]
    after = follow(follow(problem, first), solve(follow(problem, first)))
    assert after.initial_dispatch_released
    assert solve(after).flows[0].dispatch_mode == "CHARGE_PV"


def stored(commitment, mode, start, released=(), minutes=60):
    """What the coordinator stores for a plan whose current interval is `mode`."""
    coordinator = SimpleNamespace(
        _battery_commitment=dict(commitment) if commitment else None,
        _dispatch_store=SimpleNamespace(async_delay_save=lambda *_: None),
    )
    EnergyCompassCoordinator._commit_battery_direction(
        coordinator,
        {
            "generated_at": start,
            "intervals": [{"dispatch_mode": mode, "start": start}],
            "dispatch_policy": {
                "enabled": True,
                "minimum_mode_minutes": minutes,
                "released_hold_starts": list(released),
            },
        },
    )
    return coordinator._battery_commitment


def test_the_coordinator_keeps_the_commitment_and_its_clock_for_a_released_hold():
    since, start = "2026-12-18T10:45:00+00:00", "2026-12-18T11:00:00+00:00"
    held = {"mode": "SELF_CONSUME", "since": since}
    kept = {**held, "released": True}
    assert stored(held, "HOLD", start, [start]) == kept
    # The HOLD that goes on, and the first interval after a plan that went back.
    assert stored(kept, "HOLD", start, [start]) == kept
    assert stored(kept, "CHARGE_PV", start) == {"mode": "CHARGE_PV", "since": since}
    assert stored(kept, "SELF_CONSUME", start) == held
    # Any other row leaves the commitment, and a HOLD the plan does not list as released
    # (a full pack, a HOLD of its own) starts its own clock as before.
    assert stored(held, "HOLD", start) == {"mode": "HOLD", "since": start}
    assert stored(kept, "HOLD", start) == {"mode": "HOLD", "since": start}
    assert stored(kept, "CURTAIL", start) == {"mode": "CURTAIL", "since": start}
    # The HOLD that outlasted the commitment's hour goes back to a run of its own.
    later = "2026-12-18T11:50:00+00:00"
    assert stored(kept, "CHARGE_PV", later) == {"mode": "CHARGE_PV", "since": later}
    assert stored(kept, "CHARGE_PV", later, minutes=0) == {
        "mode": "CHARGE_PV",
        "since": since,
    }
    assert stored(kept, "HOLD", later, [later]) == kept
    # No commitment to keep: a HOLD is a HOLD.
    assert stored(None, "HOLD", start, [start]) == {"mode": "HOLD", "since": start}
    assert stored({"mode": "HOLD", "since": since}, "HOLD", start, [start]) == {
        "mode": "HOLD",
        "since": since,
    }


def test_a_stored_release_is_restored_only_for_a_pv_follow_commitment():
    now = datetime.fromisoformat("2026-12-18T11:00:00+00:00")
    since = "2026-12-18T10:45:00+00:00"
    for mode in ("SELF_CONSUME", "CHARGE_PV"):
        raw = {"mode": mode, "since": since, "released": True}
        assert restore_commitment(raw, now) == raw
    for raw in (
        {"mode": "HOLD", "since": since, "released": True},
        {"mode": "CURTAIL", "since": since, "released": True},
        {"mode": "CHARGE_PV", "since": since, "released": "yes"},
        {"mode": "CHARGE_PV", "since": since},
    ):
        assert "released" not in restore_commitment(raw, now)


def test_a_release_needs_a_pv_follow_commitment_to_come_from():
    problem = depleted((0, *(0.9,) * 7))
    validate_problem(replace(problem, initial_dispatch_released=True))
    for mode in ("HOLD", "CURTAIL"):
        with pytest.raises(InputError, match="released HOLD"):
            validate_problem(
                replace(
                    problem, initial_dispatch_mode=mode, initial_dispatch_released=True
                )
            )
    with pytest.raises(InputError, match="released HOLD"):
        validate_problem(
            replace(
                problem,
                initial_dispatch_mode=None,
                initial_dispatch_mode_since=None,
                initial_dispatch_released=True,
            )
        )


def test_a_released_hold_that_outlasts_the_commitment_goes_on_without_a_clock():
    # The commitment's hour is up in the middle of the sunrise: the HOLD that was
    # released from it is not a new HOLD run, which could not pass the PV above the cap.
    problem = depleted(
        (0.05, *(0.9,) * 7), mode="SELF_CONSUME", allow_curtailment=False
    )
    problem = replace(
        problem,
        battery=replace(problem.battery, allow_grid_charge=False),
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=75),
    )
    with pytest.raises(SolveError):
        solve(problem)
    flagged = replace(problem, initial_dispatch_released=True)
    plan = solve(flagged)
    assert [flow.dispatch_mode for flow in plan.flows[:2]] == ["HOLD", "CHARGE_PV"]
    assert plan.released_holds == (0,)


def test_a_recovered_pack_leaves_the_carried_release_to_the_plan():
    # The pack the release was for is not empty any more: the HOLD may go on or go back
    # to the commitment, as the plan finds best, and the commitment is not left.
    problem = depleted((0, *(0.9,) * 7), mode="SELF_CONSUME")
    problem = replace(
        problem,
        battery=replace(problem.battery, initial_kwh=5.0),
        initial_dispatch_released=True,
    )
    plan = solve(problem)
    assert plan.flows[0].dispatch_mode in {"HOLD", "SELF_CONSUME", "CHARGE_PV"}
    validate_modes(problem, plan.flows, plan.carried_bridges)


def test_validator_reads_the_carried_release_as_a_hold_that_may_go_back():
    problem = replace(
        depleted((0, *(0.9,) * 7), mode="SELF_CONSUME"), initial_dispatch_released=True
    )
    idle = Flow(0, 0, 0, 0, 0, 0.01, None, "HOLD")
    # The PV the house does not use leaves as export: the HOLD of a sunny interval.
    sunny = Flow(0, 0.8, 0, 0, 0, 0.01, None, "HOLD")
    charges = tuple(
        Flow(0, 0, 0.8, 0, 0, 0.01 + 0.8 * n, "charge", "CHARGE_PV")
        for n in range(1, 8)
    )
    # The release goes on through the commitment (ends 45 minutes in) and goes back.
    assert validate_modes(problem, (idle, *charges)) == {0}
    assert validate_modes(problem, (idle, sunny, sunny, *charges[2:])) == {0, 1, 2}
    # A CURTAIL is not on offer on a pack that is not full (ADR-0005 point 13).
    curtail = Flow(0, 0, 0, 0, 0.8, 0.01, None, "CURTAIL")
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (idle, curtail, *charges[1:]))


def test_a_depleted_release_is_planned_through_a_flexible_load_probe():
    # With a probe the idle intervals that follow the HOLD are rows the probe may open:
    # the model reads the release and the way back there too (`_resume` in the idle
    # branch), and the plan is certified against the probe's load.
    problem = carried(
        capped((0, 0.1, 0.1, *(0.9,) * 5), initial=0.01, export_cap=2),
        since=timedelta(0),
        mode="SELF_CONSUME",
    )
    plan = probed(problem, 0.25, 0.5).plan
    assert [flow.dispatch_mode for flow in plan.flows[:4]] == [
        "HOLD",
        "HOLD",
        "HOLD",
        "CHARGE_PV",
    ]
    assert plan.released_holds == (0, 1, 2)
    validate_modes(problem, plan.flows, plan.carried_bridges)


@pytest.mark.parametrize("allow_curtailment", [True, False])
@pytest.mark.parametrize("mode", ["SELF_CONSUME", "CHARGE_PV"])
@pytest.mark.parametrize(("pv", "export_cap"), SUNRISES)
def test_a_pack_exactly_at_the_reserve_is_the_depleted_release_not_a_hold_with_a_clock(
    pv, export_cap, mode, allow_curtailment
):
    # The usual empty state: the inverter stops at the minimum SOC, so the observed
    # pack is the reserve to the last bit (T-058). A carried PV-follow commitment is
    # released like it is 0.01 kWh above (T-050), not a safety exception whose HOLD
    # has the commitment's clock and curtails beside the empty pack or has no plan.
    problem = depleted(
        pv, mode=mode, export_cap=export_cap, allow_curtailment=allow_curtailment
    )
    problem = replace(problem, battery=replace(problem.battery, initial_kwh=0.0))
    first = solve(problem)
    modes = [flow.dispatch_mode for flow in first.flows]
    assert modes[:3] == ["HOLD", "CHARGE_PV", "CHARGE_PV"]
    assert "CURTAIL" not in modes[:3]
    assert first.released_holds[:1] == (0,)
    validate_modes(problem, first.flows, first.carried_bridges)
    # Rolled: the HOLD is carried out, the pack is at the reserve again (standby loss,
    # a SOC step) and the release is carried with the commitment's clock.
    after = follow(problem, first)
    assert after.initial_dispatch_released
    assert after.initial_dispatch_mode == mode
    after = replace(after, battery=replace(after.battery, initial_kwh=0.0))
    second = solve(after)
    assert [flow.dispatch_mode for flow in second.flows[:2]] == ["CHARGE_PV"] * 2
    assert roll(problem, 5)[:3] == ["HOLD", "CHARGE_PV", "CHARGE_PV"]


@pytest.mark.parametrize("mode", ["SELF_CONSUME", "CHARGE_PV"])
def test_a_carried_release_whose_pack_drops_to_the_reserve_is_not_locked_again(mode):
    # T-050 carries the release across the boundary; the pack that drops to the reserve
    # during the HOLD (repro 4) must not turn it back into a safety-exception HOLD.
    problem = depleted((0, *(0.9,) * 7), mode=mode, export_cap=2)
    first = solve(problem)
    after = follow(problem, first)
    after = replace(after, battery=replace(after.battery, initial_kwh=0.0))
    assert after.initial_dispatch_released
    second = solve(after)
    assert second.flows[0].dispatch_mode == "CHARGE_PV"


def test_a_discharge_grid_commitment_at_the_reserve_is_still_a_safety_exception():
    problem = carried(capped((0.9,) * 4, initial=0.0), mode="DISCHARGE_GRID")
    assert safety_exception(problem)["reason"] == "observed_soc_minimum"


def _unreleasable():
    """A depleted release the pack's charge power cannot take: it charges and curtails (T-064)."""
    problem = depleted((0, *(0.9,) * 7), mode="SELF_CONSUME", export_cap=0)
    battery = replace(problem.battery, initial_kwh=0.0, charge_kw=1.0)
    return replace(problem, battery=battery)


def test_a_release_the_charge_power_cannot_take_is_planned_by_charging_and_curtailing():
    # The charge power of the pack cannot take the PV above the export cap (T-058 left the
    # house the commitment's HOLD, T-064 plans the release): the HOLD is the safety
    # release, then every interval curtails what the cap cannot pass.
    problem = _unreleasable()
    plan = solve(problem)
    modes = [flow.dispatch_mode for flow in plan.flows]
    assert modes[0] == "HOLD"
    assert set(modes[1:]) == {"CURTAIL"}
    assert safety_exception(problem) is None
    validate_modes(problem, plan.flows, plan.carried_bridges)
    # Without curtailment there is no plan: the surplus has nowhere to go.
    stuck = replace(problem, site=replace(problem.site, allow_curtailment=False))
    with pytest.raises(SolveError) as error:
        solve(stuck)
    assert error.value.reason == "infeasible"


def test_the_commitments_own_hold_is_a_plan_for_the_release():
    # The rung `solve_released` takes when the release has none: the commitment's HOLD
    # with its clock, which curtails, reported as the safety exception it was.
    problem = _unreleasable()
    fallback = reserve_fallback(problem)
    assert fallback.initial_dispatch_reserve_hold
    assert safety_exception(fallback)["reason"] == "observed_soc_minimum"
    plan = solve(fallback)
    modes = [flow.dispatch_mode for flow in plan.flows]
    assert modes[0] == "HOLD"
    assert "CURTAIL" in modes
    validate_modes(fallback, plan.flows, plan.carried_bridges)


def test_the_fallback_is_for_a_commitment_at_the_reserve_only():
    problem = _unreleasable()
    assert reserve_fallback(replace(problem, initial_dispatch_mode="CHARGE_PV")) is None
    above = replace(problem, battery=replace(problem.battery, initial_kwh=0.5))
    assert reserve_fallback(above) is None
    assert reserve_fallback(reserve_fallback(problem)) is None


def test_the_runtime_plans_the_commitments_hold_when_the_release_has_no_plan(
    monkeypatch,
):
    from test_export_benefit import native_config

    from custom_components.energy_compass import runtime

    config = native_config()
    config["settings"].update(minimum_mode_minutes=60)
    now = datetime(2026, 9, 18, 1, tzinfo=UTC)
    states = {"sensor.soc": {"state": "0", "attributes": {}, "last_updated": now}}
    commitment = {
        "mode": "SELF_CONSUME",
        "since": (now - timedelta(minutes=15)).isoformat(),
    }
    real = runtime.solve
    problems = []

    def release_has_no_plan(problem, **kwargs):
        problems.append(problem)
        if not problem.initial_dispatch_reserve_hold:
            raise SolveError("infeasible")
        return real(problem, **kwargs)

    monkeypatch.setattr(runtime, "solve", release_has_no_plan)
    result = runtime.compute(config, states, now, battery_commitment=commitment)
    assert [p.initial_dispatch_reserve_hold for p in problems] == [False, True]
    assert result["intervals"][0]["dispatch_mode"] == "HOLD"
    assert result["dispatch_policy"]["safety_exception"]["reason"] == (
        "observed_soc_minimum"
    )
    # Nothing but a solver failure reaches the fallback.
    problems.clear()

    def timeout(problem, **kwargs):
        problems.append(problem)
        raise SolveError("timeout")

    monkeypatch.setattr(runtime, "solve", timeout)
    with pytest.raises(SolveError, match="timeout"):
        runtime.compute(config, states, now, battery_commitment=commitment)
    assert len(problems) == 1


def _stranded():
    """A carried CHARGE_PV, the pack just above the reserve, that no plan can keep (T-058).

    Draw 12 of seed 11 of `benchmarks/curtail_sweep.py deprun`: the commitment's minimum
    duration (120 minutes) meets a pack of 0.05 kWh and a charge power of 1 kW. The same
    state has no plan on `main`, and neither has the release nor the commitment's own HOLD;
    only the last rung of `solve_released` plans it.
    """
    rng = random.Random(11)
    for _ in range(13):
        problem = make_depleted(rng)
    return problem


def test_a_stranded_commitment_has_no_plan_but_without_it():
    problem = _stranded()
    with pytest.raises(SolveError) as error:
        solve(problem)
    assert error.value.reason == "infeasible"
    assert reserve_fallback(problem) is None
    free = uncommitted_fallback(problem)
    assert free.initial_dispatch_mode is None
    assert free.initial_dispatch_mode_since is None
    plan = solve(free)
    validate_modes(free, plan.flows, plan.carried_bridges)


@pytest.mark.parametrize(
    "mode", [None, "HOLD", "CURTAIL", "CHARGE_GRID", "DISCHARGE_GRID"]
)
def test_only_a_pv_follow_commitment_is_left_out_as_the_last_resort(mode):
    problem = carried(capped((0.9,) * 4), mode=mode) if mode else capped((0.9,) * 4)
    assert uncommitted_fallback(problem) is None


class _Solver:
    """A solver that fails with `reason` for the problems `fails` names, and records calls."""

    def __init__(self, fails, reason="infeasible", seconds=0.0, clock=None):
        self.fails, self.reason, self.seconds, self.clock = (
            fails,
            reason,
            seconds,
            clock,
        )
        self.calls = []

    def __call__(self, problem, **kwargs):
        self.calls.append((problem, kwargs))
        if self.clock is not None:
            self.clock[0] += self.seconds
        if self.fails(problem):
            raise SolveError(self.reason)
        return solve(problem, **kwargs)


def test_solve_released_solves_a_problem_with_a_plan_once_and_returns_it():
    problem = depleted((0, *(0.9,) * 7), mode="SELF_CONSUME", export_cap=2)
    solver = _Solver(lambda _: False)
    plan, solved = solve_released(problem, accept_incumbent=True, solver=solver)
    assert solved is problem
    assert [call[0] for call in solver.calls] == [problem]
    assert solver.calls[0][1] == {"time_limit_s": 10.0, "accept_incumbent": True}
    assert plan.flows[0].dispatch_mode == "HOLD"


def test_solve_released_plans_the_commitments_hold_before_leaving_it():
    problem = _unreleasable()
    solver = _Solver(lambda p: not p.initial_dispatch_reserve_hold)
    plan, solved = solve_released(problem, solver=solver)
    assert solved.initial_dispatch_reserve_hold
    assert solved.initial_dispatch_mode == "SELF_CONSUME"
    assert plan.flows[0].dispatch_mode == "HOLD"
    validate_modes(solved, plan.flows, plan.carried_bridges)


def test_solve_released_plans_the_release_when_the_site_can_charge_and_curtail():
    problem = _unreleasable()
    solver = _Solver(lambda _: False)
    plan, solved = solve_released(problem, solver=solver)
    assert solved is problem
    assert len(solver.calls) == 1
    assert plan.flows[0].dispatch_mode == "HOLD"


def test_solve_released_leaves_the_commitment_last():
    problem = _unreleasable()
    # Neither the release nor the commitment's own HOLD has a plan: the commitment goes.
    solver = _Solver(lambda p: p.initial_dispatch_mode is not None)
    plan, solved = solve_released(problem, solver=solver)
    assert [
        (p.initial_dispatch_reserve_hold, p.initial_dispatch_mode)
        for p, _ in solver.calls
    ] == [(False, "SELF_CONSUME"), (True, "SELF_CONSUME"), (False, None)]
    assert solved is solver.calls[-1][0]
    assert plan.flows


def test_solve_released_plans_a_stranded_commitment_without_it():
    problem = _stranded()
    plan, solved = solve_released(problem)
    assert solved.initial_dispatch_mode is None
    assert len(plan.flows) == len(problem.slots)


def test_solve_released_raises_the_first_failure_when_every_rung_has_none():
    problem = _unreleasable()
    solver = _Solver(lambda _: True)
    with pytest.raises(SolveError) as error:
        solve_released(problem, solver=solver)
    assert error.value.reason == "infeasible"
    assert len(solver.calls) == 3
    # A problem with no PV-follow commitment has no rung at all.
    solver = _Solver(lambda _: True)
    with pytest.raises(SolveError):
        solve_released(capped((0.9,) * 4), solver=solver)
    assert len(solver.calls) == 1


@pytest.mark.parametrize("reason", ["timeout", "invalid solver result"])
def test_only_infeasible_takes_a_rung(reason):
    solver = _Solver(lambda _: True, reason=reason)
    with pytest.raises(SolveError) as error:
        solve_released(_unreleasable(), solver=solver)
    assert error.value.reason == reason
    assert len(solver.calls) == 1


def test_the_rungs_share_the_budget(monkeypatch):
    from custom_components.energy_compass.engine import optimize

    clock = [100.0]
    monkeypatch.setattr(optimize, "perf_counter", lambda: clock[0])
    # The release spends 7 of the 10 seconds: the next rung gets what is left, not 10.
    solver = _Solver(
        lambda p: not p.initial_dispatch_reserve_hold, seconds=7, clock=clock
    )
    solve_released(_unreleasable(), time_limit_s=10, budget_s=10, solver=solver)
    assert [call[1]["time_limit_s"] for call in solver.calls] == [10, 3]
    # A budget the release spent leaves no second solve: a timeout, not a second full limit.
    clock[0] = 100.0
    solver = _Solver(lambda _: True, seconds=10, clock=clock)
    with pytest.raises(SolveError) as error:
        solve_released(_unreleasable(), time_limit_s=10, budget_s=10, solver=solver)
    assert error.value.reason == "timeout"
    assert len(solver.calls) == 1


def test_a_followed_stranded_commitment_goes_on_without_it():
    # Rolled: the state is reached by following a release at the reserve; the plan that
    # leaves the commitment is carried like any other and the next boundary has a plan.
    problem = _stranded()
    plan, solved = solve_released(problem)
    after = follow(solved, plan, problem)
    assert after.initial_dispatch_mode == plan.flows[0].dispatch_mode
    next_plan, _ = solve_released(after)
    assert next_plan.flows


def _real_compute_inputs():
    from test_export_benefit import native_config
    from test_strategy_runtime import _pv_binding, _pv_state

    from custom_components.energy_compass.config_models import PvSource

    config = native_config()
    config["settings"].update(
        minimum_mode_minutes=60, grid_export_kw=0, allow_curtailment=True
    )
    now = datetime(2026, 9, 18, 1, tzinfo=UTC)
    config["sources"]["pv"] = PvSource(True, ((_pv_binding(),),)).to_dict()
    rows = [
        {
            "start": now + timedelta(minutes=15 * i),
            "end": now + timedelta(minutes=15 * (i + 1)),
            "value": 0.0 if i == 0 else 0.9,
        }
        for i in range(8)
    ]
    states = {
        "sensor.soc": {"state": "0", "attributes": {}, "last_updated": now},
        **_pv_state(now, rows),
    }
    commitment = {
        "mode": "SELF_CONSUME",
        "since": (now - timedelta(minutes=15)).isoformat(),
    }
    return config, states, now, commitment


def test_the_runtime_plans_the_release_that_charges_and_curtails():
    # No monkeypatched solver: an export cap of 0, a charge power of 1 kW beside 3.6 kW
    # of PV and a pack at the reserve. The release is a plan of its own (T-064): the
    # HOLD, then CURTAIL; no safety exception is reported.
    from custom_components.energy_compass import runtime

    config, states, now, commitment = _real_compute_inputs()
    result = runtime.compute(config, states, now, battery_commitment=commitment)
    modes = [row["dispatch_mode"] for row in result["intervals"]]
    assert result["status"] == "ready"
    assert modes[0] == "HOLD"
    assert "CURTAIL" in modes
    assert result["dispatch_policy"]["safety_exception"] is None


# Draw 278 of seed 22 of `benchmarks/curtail_sweep.py deproll` (T-057): a carried
# SELF_CONSUME on a pack just above the reserve, the PV rising from nothing, an export cap
# of 0.5 kW (0.125 kWh an interval) and a standby loss. (buy, sell, PV, load) per interval.
SUNRISE_278 = (
    (0.27, 0.47, 0.1, 0.02),
    (0.36, 0.75, 0.2, 0.4),
    (1.43, 0.01, 0.18, 0.05),
    (0.26, 0.15, 0.3, 0.4),
    (1.15, 0.29, 0.3, 0.4),
    (0.75, 0.37, 0.3, 0.1),
    (0.22, 0.31, 0.3, 0.02),
    (0.55, 0.74, 0.3, 0.05),
)


def sunrise_278(**changes):
    problem = dispatch(
        SUNRISE_278,
        minimum_mode_minutes=120,
        minimum_mode_power_kw=0.1,
        terminal_mode="preserve_initial",
    )
    problem = carried(problem, mode="SELF_CONSUME")
    problem = replace(
        problem,
        site=SiteLimits(3, 8, 0.5, True),
        battery=replace(
            problem.battery,
            capacity_kwh=5,
            initial_kwh=0.02,
            charge_kw=3,
            discharge_kw=1,
            eta_charge=0.95,
            eta_discharge=0.95,
            wear_per_kwh=0,
            allow_grid_charge=False,
            allow_battery_export=False,
            idle_drain_kw=0.05,
        ),
    )
    return replace(problem, **changes)


def test_a_plan_followed_one_interval_leaves_the_commitment_a_plan_at_the_next_boundary():
    # Followed one interval, the plan's suffix is the next problem. Its pack is the
    # planned 0.083 kWh and pays more standby loss than the plan assumed (the loss is
    # capped by the trajectory from the observed pack), so a deficit it could cover
    # before it empties or releases a HOLD, and that HOLD meets a surplus the cap
    # cannot pass with the pack above its lock floor (T-057: infeasible at the first
    # boundary on main). Followed to the end of the horizon, the released HOLD rows and
    # the return included.
    problem = sunrise_278()
    row = sweep(problem)
    assert row[:2] == ["ok", len(problem.slots) - 1]


def test_a_released_hold_goes_back_to_the_commitment_in_an_interval_the_site_cannot_pass():
    # The boundary problem of the test above, planned directly. The HOLD released for
    # a deficit the pack could not cover goes on until the surplus of 0.2 kWh against
    # an export cap of 0.125 kWh, where the pack (0.086 kWh, above its lock floor of
    # 0.039 kWh) is no longer depleted: only the PV the site cannot pass takes the HOLD
    # back to CHARGE_PV (CURTAIL is offered only after a full pack).
    first = sunrise_278()
    problem = replace(
        first,
        slots=first.slots[1:],
        battery=replace(first.battery, initial_kwh=0.0835),
        initial_dispatch_mode="CHARGE_PV",
    )
    plan = solve(problem)
    validate_modes(problem, plan.flows, plan.carried_bridges)
    modes = [flow.dispatch_mode for flow in plan.flows]
    assert modes[:5] == [
        "SELF_CONSUME",
        "CHARGE_PV",
        "HOLD",
        "HOLD",
        "CHARGE_PV",
    ]
    assert plan.released_holds == (2, 3)


def full_pack_hold(allow_curtailment):
    """A carried CHARGE_PV on a pack 0.01 kWh below its ceiling, a standby loss.

    The PV passes the 0.125 kWh export cap in the first interval (0.1 kWh over the
    house) and does not in the second (0.14 kWh): the commitment's first interval is a
    HOLD released from the full pack, the second a surplus a HOLD cannot pass.
    """
    pv = (0.2, 0.24, *(0.2,) * 8)
    problem = dispatch(
        tuple((1.0, 0.5, value, 0.1) for value in pv),
        minimum_mode_minutes=60,
        minimum_mode_power_kw=0.1,
    )
    return carried(
        replace(
            problem,
            site=SiteLimits(8, 8, 0.5, allow_curtailment),
            battery=replace(
                problem.battery,
                initial_kwh=9.99,
                idle_drain_kw=0.05,
                allow_grid_charge=False,
                allow_battery_export=False,
            ),
        ),
        since=timedelta(0),
    )


def test_a_hold_released_from_a_full_pack_has_no_plan_where_the_site_cannot_pass_the_pv():
    # Without curtailment the pack would have to take PV it has no room for, and the
    # HOLD released from it cannot go back to the commitment: the coordinator stores
    # that HOLD with a clock of its own, so a plan that returned in-plan was no plan one
    # interval later (T-057 review: 54 of 1296 aimed draws). No plan at the first
    # boundary, as on main.
    problem = full_pack_hold(False)
    with pytest.raises(SolveError, match="infeasible"):
        solve(problem)
    assert sweep(problem)[:2] == ["err", 0]


def test_a_hold_released_from_a_full_pack_is_curtailed_not_returned_in_the_plan():
    # With curtailment the plan is HOLD, then CURTAIL where the site cannot pass the
    # PV, and the next boundary plans the same: the published plan is not contradicted
    # one interval later (T-057 review: the first revision planned CHARGE_PV there).
    problem = full_pack_hold(True)
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows[:5]] == [
        "HOLD",
        "CURTAIL",
        "CURTAIL",
        "CURTAIL",
        "CURTAIL",
    ]
    assert plan.released_holds == ()
    # Followed to the end of the horizon: a plan at every boundary, each one's first
    # interval the second of the plan before.
    row = sweep(problem, steps=12)
    assert row[:2] == ["ok", len(problem.slots) - 1]
    assert row[4] == []


def return_flows(pack):
    """A HOLD, then CHARGE_PV where the site cannot pass the PV (0.4 kWh over the house)."""
    hold = Flow(0, 0, 0, 0, 0, pack, None, "HOLD")
    surplus = Flow(0, 0, 0.4, 0, 0, pack, "charge", "CHARGE_PV")
    after = Flow(0, 0, 0.1, 0, 0, pack, "charge", "CHARGE_PV")
    return (hold, surplus, *(after,) * 4)


def test_validator_accepts_a_depleted_hold_that_goes_back_where_the_site_cannot_pass():
    # The pack is above its lock floor again when the HOLD meets PV the cap cannot pass:
    # the HOLD released from a depleted pack goes back, the pack takes the surplus.
    problem = carried(
        capped((0.0, 0.5, *(0.2,) * 4), initial=0.01, export_cap=0.5),
        mode="SELF_CONSUME",
    )
    validate_modes(problem, return_flows(1.0))


def test_validator_rejects_a_hold_released_from_a_full_pack_that_goes_back_there_too():
    # The same return of a HOLD released from a full pack is refused: the coordinator
    # carries only the depleted release across a boundary, so the plan that returned in
    # one interval was no plan in the next (T-057 review).
    # (The pack is 0.01 kWh below its ceiling: at the ceiling the observed SOC is a
    # safety exception and the commitment is a HOLD already.)
    problem = carried(
        capped((0.2, 0.5, *(0.2,) * 4), initial=9.99, export_cap=0.5), mode="CHARGE_PV"
    )
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, return_flows(9.99))


def _surplus_beyond_the_pack(**changes):
    """PV the pack cannot take by its charge power, on a site that exports nothing (T-064).

    Four sunny intervals (0.9 kWh of PV, 0.1 kWh of load) on a pack that charges 0.25 kWh
    an interval, then four evenings that the stored energy is worth most in.
    """
    rows = ((0.3, 0.0, 0.9, 0.1),) * 4 + ((3.0, 0.0, 0.0, 0.2),) * 4
    problem = dispatch(rows, minimum_mode_minutes=60)
    return replace(
        problem,
        site=replace(problem.site, grid_export_kw=0, allow_curtailment=True),
        battery=replace(problem.battery, initial_kwh=0.0, charge_kw=1.0),
        **changes,
    )


def test_a_curtail_interval_charges_the_pack_with_what_it_can_take():
    # The model charges and curtails in one interval (T-064): the pack takes its charge
    # power, the inverter curtails the rest, the evening is served from the pack.
    problem = _surplus_beyond_the_pack()
    plan = solve(problem)
    validate_modes(problem, plan.flows, ())
    sun = plan.flows[:4]
    assert [flow.dispatch_mode for flow in sun] == ["CURTAIL"] * 4
    assert max(flow.charge_kwh for flow in sun) == pytest.approx(0.25)
    for flow in sun:
        assert flow.curtail_kwh > 0
        assert flow.discharge_kwh == 0
        assert flow.grid_export_kwh == 0
        assert flow.charge_kwh + flow.curtail_kwh == pytest.approx(0.8)
    assert [flow.dispatch_mode for flow in plan.flows[4:]] == ["SELF_CONSUME"] * 4
    assert sum(flow.discharge_kwh for flow in plan.flows[4:]) > 0.5


def test_a_curtail_row_validates_only_what_the_surplus_can_charge():
    problem = _surplus_beyond_the_pack()
    plan = solve(problem)
    flows = list(plan.flows)
    row = flows[1]
    # A charge from the grid, or a discharge, is not a CURTAIL.
    for bad in (
        replace(row, charge_kwh=0.9),
        replace(row, discharge_kwh=0.2, charge_kwh=0.0),
    ):
        with pytest.raises(SolveError, match="mode"):
            validate_modes(problem, (*flows[:1], bad, *flows[2:]), ())


def test_a_probe_is_planned_with_a_curtail_that_charges():
    problem = _surplus_beyond_the_pack()
    plan = probed(problem, 0.5, 2).plan
    sun = plan.flows[:4]
    assert [flow.dispatch_mode for flow in sun] == ["CURTAIL"] * 4
    assert max(flow.charge_kwh for flow in sun) > 0


def test_a_plan_that_charges_and_curtails_can_be_followed_to_every_boundary():
    problem = _surplus_beyond_the_pack()
    modes = roll(problem, 6)
    assert modes[:4] == ["CURTAIL"] * 4
    row = sweep(problem)
    assert row[:2] == ["ok", len(problem.slots) - 1]


def test_without_curtailment_the_surplus_beyond_the_pack_has_no_plan():
    problem = _surplus_beyond_the_pack()
    stuck = replace(problem, site=replace(problem.site, allow_curtailment=False))
    with pytest.raises(SolveError) as error:
        solve(stuck)
    assert error.value.reason == "infeasible"
