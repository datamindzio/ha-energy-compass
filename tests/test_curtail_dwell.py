"""A CURTAIL run goes on through intervals with too little PV to curtail (T-037).

On an export-capped site a pack that is full (or nearly so) has to curtail what the
cap cannot pass, and CURTAIL lasts the minimum duration. When PV drops inside it
there is nothing left to curtail: the HOLD of such an interval is the run going on,
it counts for the CURTAIL clock and starts no HOLD clock, and it lasts only until
the run's own duration is up. A carried PV-follow commitment whose pack cannot take
what the cap forces ends in the same release as a full pack.
"""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_dispatch_policy import dispatch

from custom_components.energy_compass.engine.dispatch_policy import validate_modes
from custom_components.energy_compass.engine.models import Flow, SolveError
from custom_components.energy_compass.engine.optimize import solve

CURTAIL = Flow(0, 0.5, 0, 0, 0.3, 10, None, "CURTAIL")
HOLD = Flow(0.1, 0, 0, 0, 0, 10, None, "HOLD")
SELF = Flow(0, 0, 0, 0.1, 0, 9.9, "discharge", "SELF_CONSUME")


def capped(pv, *, initial=10.0, export_cap=2, **changes):
    problem = dispatch(
        tuple((1.0, 0.9, value, 0.1) for value in pv), minimum_mode_minutes=60
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
    assert plan.carried_bridges == (8,)


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
