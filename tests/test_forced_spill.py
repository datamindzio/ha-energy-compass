"""A site that cannot curtail still spills what it cannot pass once the pack is full (T-041).

An export cap (or an inverter rated below the PV) with `allow_curtailment` off leaves PV
no place to go when the pack is full: the inverter throttles it by itself. The plan reads
that as CURTAIL (the Deye controller runs its HOLD profile and the inverter does the rest),
but only for that PV and only into a full pack: curtailment stays off everywhere else.
"""

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine import dispatch_policy, optimize
from custom_components.energy_compass.engine.dispatch_policy import dwell_group
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


def problem(
    rows,
    *,
    initial=5.0,
    cap_kw=1.0,
    inverter_kw=8.0,
    minutes=0,
    curtail=False,
    drain_kw=0.0,
    charge_kw=4,
    **kw,
) -> Problem:
    """Quarter hours of `(buy, sell, pv, load)` on a 5 kWh pack that is full at 5 kWh."""
    return Problem(
        tuple(
            Slot(START + i * QUARTER, START + (i + 1) * QUARTER, *row)
            for i, row in enumerate(rows)
        ),
        SiteLimits(inverter_kw, 8, cap_kw, curtail),
        Battery(5, 0, 1, initial, charge_kw, 4, 1, 1, 0, False, False, drain_kw),
        "value",
        0,
        (),
        "UTC",
        minimum_mode_minutes=minutes,
        **kw,
    )


def test_full_pack_spills_what_the_export_cap_cannot_pass_and_no_more():
    # 1.2 kWh of PV, the cap passes 0.25 kWh a quarter hour: 0.95 kWh is spilled. A sell
    # price below zero makes spilling more attractive; the spill is the need, not more.
    plan = solve(problem(((1.0, -1.0, 1.2, 0),)))
    flow = plan.flows[0]
    assert machine_state(problem(((1.0, -1.0, 1.2, 0),)).slots[0], flow) == "CURTAIL"
    assert flow.curtail_kwh == pytest.approx(0.95, abs=1e-5)
    assert flow.grid_export_kwh == pytest.approx(0.25, abs=1e-5)
    assert flow.charge_kwh == pytest.approx(0, abs=1e-6)


def test_full_pack_spills_what_the_inverter_cannot_pass():
    # 1.2 kWh of PV against a 0.75 kWh inverter: 0.45 kWh goes nowhere.
    rows = ((1.0, 0.5, 1.2, 0),)
    plan = solve(problem(rows, cap_kw=8.0, inverter_kw=3.0))
    assert plan.flows[0].curtail_kwh == pytest.approx(0.45, abs=1e-5)


def test_pack_with_room_never_spills_even_at_a_negative_sell_price():
    # Spilling would save the negative export, but curtailment is off and the pack has
    # room: it charges at its full rate (1 kWh a quarter hour) instead.
    rows = ((1.0, -1.0, 1.2, 0), (1.0, -1.0, 1.2, 0))
    plan = solve(problem(rows, initial=1.0))
    assert [flow.curtail_kwh for flow in plan.flows] == [0, 0]
    assert plan.flows[0].charge_kwh == pytest.approx(1.0, abs=1e-5)


def test_pack_filled_in_the_interval_spills_from_the_next_one():
    # The first quarter fills the last 1 kWh of room (the pack takes 1 kWh a quarter hour,
    # exports 0.2 kWh): no spill yet, the pack had room at its start. The second spills.
    rows = ((1.0, 0.1, 1.2, 0), (1.0, 0.1, 1.2, 0))
    plan = solve(problem(rows, initial=4.0))
    assert plan.flows[0].curtail_kwh == pytest.approx(0, abs=1e-6)
    assert plan.flows[0].end_soc_kwh == pytest.approx(5.0, abs=1e-5)
    assert plan.flows[1].curtail_kwh == pytest.approx(0.95, abs=1e-5)


def test_spill_is_only_for_a_site_with_a_pack():
    rows = ((1.0, 0.5, 1.2, 0),)
    flat = problem(rows)
    with pytest.raises(SolveError) as error:
        solve(replace(flat, battery=None))
    assert error.value.reason == "infeasible"


def test_a_site_that_can_curtail_is_unchanged():
    # Curtailment on: the PV may go with a pack that has room too.
    rows = ((1.0, -1.0, 1.2, 0),)
    plan = solve(problem(rows, initial=1.0, curtail=True))
    assert plan.flows[0].curtail_kwh == pytest.approx(1.2, abs=1e-5)


def test_mode_model_plans_the_spill_as_a_curtail_run():
    rows = ((1.0, 0.5, 1.2, 0),) * 4
    plan = solve(problem(rows, minutes=60))
    assert [flow.dispatch_mode for flow in plan.flows] == ["CURTAIL"] * 4
    assert all(flow.curtail_kwh == pytest.approx(0.95, abs=1e-5) for flow in plan.flows)


def test_flexible_load_takes_from_what_is_spilled():
    # A probe load of 0.3 kWh is served by the PV that would be spilled.
    rows = ((1.0, 0.5, 1.2, 0),)
    result = solve_flexible_load(problem(rows), FlexibleLoadRequest(0.3, 4.0))
    assert result.schedule_kwh == pytest.approx((0.3,), abs=1e-5)
    assert result.plan.flows[0].curtail_kwh == pytest.approx(0.65, abs=1e-5)


def test_validator_rejects_a_spill_into_a_pack_with_room(monkeypatch):
    # Without the gate the solver spills at a negative sell price; the certificate
    # (written from the rows, not from the model) refuses that plan.
    monkeypatch.setattr(optimize, "_gate_forced_spill", lambda *a, **k: None)
    rows = ((1.0, -1.0, 1.2, 0), (1.0, -1.0, 1.2, 0))
    with pytest.raises(SolveError) as error:
        solve(problem(rows, initial=1.0))
    assert error.value.reason == "solver_failure"
    assert "forced spill" in str(error.value)


@pytest.mark.parametrize("minutes", [0, 15, 60])
def test_idle_drain_does_not_close_the_gate_on_a_long_spill(minutes):
    # The pack loses 0.005 kWh a quarter hour: after two intervals it is below the level
    # at which one interval's need no longer fits. The spill goes on anyway, because the
    # previous interval spilled (nothing else could have left the surplus).
    rows = ((1.0, 0.5, 1.2, 0),) * 8
    plan = solve(problem(rows, minutes=minutes, drain_kw=0.02))
    assert all(flow.curtail_kwh > 0.9 for flow in plan.flows)
    assert all(flow.charge_kwh == pytest.approx(0, abs=1e-6) for flow in plan.flows)


@pytest.mark.parametrize("minutes", [0, 15, 60])
@pytest.mark.parametrize("initial", [4.999, 4.95, 4.5, 4.0495, 1.2])
def test_pack_with_less_room_than_the_need_has_a_plan(initial, minutes):
    # The surplus is 0.95 kWh a quarter hour. A pack with 0.001, 0.05 or 0.5 kWh of room
    # cannot take it, and no mode but CURTAIL can leave it: the plan is not infeasible.
    rows = ((1.0, 0.1, 1.2, 0),) * 6
    plan = solve(problem(rows, initial=initial, minutes=minutes))
    assert all(0 <= flow.end_soc_kwh <= 5 + 1e-6 for flow in plan.flows)
    if not minutes:
        for flow in plan.flows:
            assert flow.charge_kwh + flow.curtail_kwh + flow.grid_export_kwh == (
                pytest.approx(1.2, abs=1e-5)
            )


def test_mode_model_spill_charges_nothing_below_the_full_level():
    # Pinned limit (docs/model.md, forced spill): a CURTAIL interval whose need the pack
    # could take by its charge power (4 kW here) charges nothing, so a pack 0.5 kWh below
    # full stays there while the surplus lasts. CHARGE_PV cannot take
    # 0.95 kWh into 0.5 kWh of room and the unmoded rule forbids it a partial charge.
    rows = ((1.0, 0.1, 1.2, 0),) * 4
    plan = solve(problem(rows, initial=4.5, minutes=60))
    assert [flow.dispatch_mode for flow in plan.flows] == ["CURTAIL"] * 4
    assert [flow.end_soc_kwh for flow in plan.flows] == pytest.approx([4.5] * 4)


@pytest.mark.parametrize("minutes", [15, 60])
def test_forced_spill_beyond_the_charge_power_charges_beside_the_spill(minutes):
    # Curtailment off (T-064): the need is 0.95 kWh a quarter hour, the pack has 0.5 kWh
    # of room (less than the need: the gate is open) and takes 0.25 kWh a quarter hour.
    # The forced CURTAIL row charges what the pack takes and spills the rest, from PV
    # only; the evening asks for more than the pack holds, so what it took is worth 3.
    rows = ((1.0, 0.1, 1.2, 0),) * 4 + ((3.0, 0.1, 0, 1.0),) * 6
    site = problem(rows, initial=4.5, minutes=minutes, charge_kw=1)
    assert not site.site.allow_curtailment
    plan = solve(site)
    dispatch_policy.validate_modes(site, plan.flows, ())
    sun = plan.flows[:4]
    assert [flow.dispatch_mode for flow in sun] == ["CURTAIL"] * 4
    assert sum(flow.charge_kwh for flow in sun) == pytest.approx(0.5, abs=1e-5)
    assert max(flow.charge_kwh for flow in sun) == pytest.approx(0.25, abs=1e-5)
    for flow in sun:
        assert flow.grid_import_kwh == pytest.approx(0, abs=1e-6)
        assert flow.discharge_kwh == pytest.approx(0, abs=1e-6)
        assert flow.charge_kwh + flow.curtail_kwh == pytest.approx(0.95, abs=1e-5)
    assert sun[-1].end_soc_kwh == pytest.approx(5.0, abs=1e-5)


@pytest.mark.parametrize("minutes", [0, 15, 60])
def test_forced_spill_beyond_the_charge_power_needs_no_gate(minutes):
    # The same site with 1 kWh of room (more than the need, so the gate is shut): the pack
    # still cannot take the need by its charge power, and the inverter spills what is
    # beyond it whatever the room (T-066). The pack charges at its power beside the spill.
    rows = ((1.0, 0.1, 1.2, 0),) * 4 + ((3.0, 0.1, 0, 1.0),) * 6
    site = problem(rows, initial=4.0, minutes=minutes, charge_kw=1)
    plan = solve(site)
    dispatch_policy.validate_modes(site, plan.flows, ())
    for flow in plan.flows[:4]:
        assert flow.charge_kwh == pytest.approx(0.25, abs=1e-5)
        assert flow.curtail_kwh == pytest.approx(0.7, abs=1e-5)
        assert flow.dispatch_mode in (None, "CURTAIL")


def test_forced_spill_beyond_the_charge_power_spills_no_more_than_that_with_room():
    # The spill with room is the PV beyond the pack and not a kWh more: where the evening
    # pays for energy, the pack takes all its power allows. 0.95 kWh against 0.25 kWh a
    # quarter hour leaves 0.7 to spill; a pack that held more would be worth more.
    rows = ((1.0, 0.1, 1.2, 0),) * 4 + ((3.0, 0.1, 0, 1.0),) * 6
    plan = solve(problem(rows, initial=1.0, charge_kw=1))
    for flow in plan.flows[:4]:
        assert flow.charge_kwh == pytest.approx(0.25, abs=1e-5)
        assert flow.curtail_kwh == pytest.approx(0.7, abs=1e-5)


@pytest.mark.parametrize("minutes", [15, 60])
def test_a_need_below_the_minimum_activity_is_planned(minutes):
    # 0.26 kWh of PV against a 0.25 kWh cap: 0.01 kWh to spill, below the 0.025 kWh a
    # curtailing quarter hour has to move at the default 0.1 kW minimum power. The
    # interval may spill the activity, not only the need.
    rows = ((1.0, 0.1, 0.26, 0),) * 4
    plan = solve(problem(rows, minutes=minutes))
    assert [flow.dispatch_mode for flow in plan.flows] == ["CURTAIL"] * 4
    assert all(
        flow.curtail_kwh == pytest.approx(0.025, abs=1e-5) for flow in plan.flows
    )


@pytest.mark.parametrize("minutes", [30, 60])
def test_a_fresh_charge_pv_run_that_fills_the_pack_hands_over_to_curtail(minutes):
    # The pack (1 kWh of room) fills inside the first CHARGE_PV interval's dwell. The
    # dwell may end in a forced CURTAIL (ADR-0005: a PV_FOLLOW group ends in CURTAIL
    # when the PV cannot be placed), so the plan is not stuck between the two.
    rows = ((1.0, 0.1, 1.2, 0),) * 8
    plan = solve(problem(rows, initial=4.0, minutes=minutes))
    modes = [flow.dispatch_mode for flow in plan.flows]
    assert modes[0] == "CHARGE_PV"
    assert modes[-1] == "CURTAIL"


@pytest.mark.parametrize("minutes", [0, 15, 30, 60])
def test_bell_shaped_day_is_planned_in_every_mode_model(minutes):
    # PV rises from 0 to 1.4 kWh a quarter hour and falls again: the need tapers through
    # every size, from far above the minimum activity to a few watts.
    rows = tuple(
        (1.0, 0.1, round(1.4 * math.sin(math.pi * (i + 0.5) / 24) ** 2, 3), 0.05)
        for i in range(24)
    )
    plan = solve(problem(rows, initial=3.0, minutes=minutes))
    assert plan.flows[-1].end_soc_kwh <= 5 + 1e-6
    assert sum(flow.curtail_kwh for flow in plan.flows) > 0


def test_validator_requires_the_pack_to_be_full_at_the_start_of_a_spill(monkeypatch):
    # With the model's gate removed, a negative sell price makes spilling at the very
    # first interval of a pack that has room the cheapest plan. The certificate looks at
    # the same plan the way a person would: the pack had room, the spill was not forced.
    monkeypatch.setattr(optimize, "_gate_forced_spill", lambda *a, **k: None)
    rows = ((1.0, -1.0, 1.2, 0),)
    with pytest.raises(SolveError) as error:
        solve(problem(rows, initial=4.0))
    assert "forced spill with room in the pack" in str(error.value)


@pytest.mark.parametrize("minutes", [0, 15, 60])
def test_pack_with_room_that_charges_too_slowly_is_planned(minutes):
    # T-066 (was a pinned limit of T-041): the gate reads the room of the pack, not its
    # charge power. At 0.4 kW the pack takes 0.1 kWh a quarter hour of a 0.95 kWh surplus
    # and the inverter throttles the rest: that is a forced spill whatever the room.
    rows = ((1.0, 0.1, 1.2, 0),) * 4
    for initial in (1.0, 5.0):
        site = problem(rows, initial=initial, minutes=minutes, charge_kw=0.4)
        plan = solve(site)
        dispatch_policy.validate_modes(site, plan.flows, ())
        for flow in plan.flows:
            assert flow.charge_kwh + flow.curtail_kwh + flow.grid_export_kwh == (
                pytest.approx(1.2, abs=1e-5)
            )
            assert flow.curtail_kwh >= 0.85 - 1e-5
            assert flow.charge_kwh <= 0.1 + 1e-5


@pytest.mark.parametrize("minutes", [15, 30, 60])
def test_a_forced_curtail_at_the_first_interval_keeps_its_run_open_with_room(minutes):
    # T-066: the first interval's PV is beyond the pack's charge power (spilled whatever the
    # room), the second is not, and the dwell forbids CHARGE_PV there. The run goes on as
    # CURTAIL, which the first interval has to relay (it has no gate row of its own).
    rows = ((1.0, 0.1, 1.41, 0.1), (1.0, 0.1, 0.98, 0.05), (1.0, 0.1, 0, 0.5))
    site = problem(rows, initial=3.0, minutes=minutes, charge_kw=3, cap_kw=1)
    plan = solve(site)
    assert plan.flows[0].dispatch_mode == "CURTAIL"
    assert plan.flows[0].curtail_kwh == pytest.approx(0.31, abs=1e-5)


def followed(start, steps):
    """Plan, take the first interval as planned, carry its mode and the pack, plan again."""
    current, commitment, trace = start, None, []
    for _ in range(steps):
        plan = solve(current)
        flow = plan.flows[0]
        trace.append(flow.dispatch_mode)
        if 0 in plan.carried_bridges:
            pass  # the run and its clock go on
        elif commitment and dwell_group(commitment[0]) == dwell_group(trace[-1]):
            commitment = (trace[-1], commitment[1])
        else:
            commitment = (trace[-1], current.slots[0].start)
        current = replace(
            current,
            slots=current.slots[1:],
            battery=replace(
                current.battery,
                initial_kwh=min(flow.end_soc_kwh, current.battery.capacity_kwh),
            ),
            initial_dispatch_mode=commitment[0],
            initial_dispatch_mode_since=commitment[1],
        )
    return trace


@pytest.mark.parametrize("minutes", [15, 30, 60])
@pytest.mark.parametrize(("first", "rest"), [(1.2, 0.7), (1.2, 0.5), (1.0, 0.6)])
def test_followed_spill_keeps_its_plan_when_the_need_drops_below_the_room(
    minutes, first, rest
):
    # Review round 2: 0.5 kWh of room, a need of 0.95 kWh opens the spill, and from the
    # next interval the need (0.45 kWh) fits the room. The plan made one interval later
    # starts inside the CURTAIL run it planned: the run keeps its first interval open.
    rows = ((1.0, 0.1, first, 0),) + ((1.0, 0.1, rest, 0),) * 7
    trace = followed(problem(rows, initial=4.5, minutes=minutes), 7)
    assert trace[0] == "CURTAIL"
    assert len(trace) == 7


def test_carried_curtail_run_keeps_the_first_interval_open():
    # A CURTAIL commitment in its dwell has no other legal mode: on a pack with room it
    # goes on as the spill it is (the inverter throttles), as a run inside a plan does.
    rows = ((1.0, 0.1, 1.2, 0),) * 4
    carried = {"initial_dispatch_mode": "CURTAIL", "initial_dispatch_mode_since": START}
    plan = solve(problem(rows, initial=3.0, minutes=60, **carried))
    assert [flow.dispatch_mode for flow in plan.flows] == ["CURTAIL"] * 4
    # Without the run the same pack has room: no spill, it charges.
    plan = solve(problem(rows, initial=3.0, minutes=60))
    assert plan.flows[0].dispatch_mode == "CHARGE_PV"
    assert plan.flows[0].curtail_kwh == 0


def taper(lasting, minutes, **kw):
    """A 10 kWh pack 0.125 kWh below full, a need of 0.101 kWh, `lasting` more of 0.1, then none."""
    rows = (
        ((1.0, 0.1, 0.701, 0.1),)
        + ((1.0, 0.1, 0.7, 0.1),) * lasting
        + ((1.0, 0.1, 0.5, 0.1),) * (10 - lasting)
    )
    base = problem(rows, cap_kw=2.0, minutes=minutes, **kw)
    return replace(
        base, battery=replace(base.battery, capacity_kwh=10, initial_kwh=9.875)
    )


@pytest.mark.parametrize("minutes", [30, 60])
@pytest.mark.parametrize("lasting", [0, 1, 2, 3, 4])
def test_need_that_ends_inside_the_pv_follow_dwell_is_planned(minutes, lasting):
    # Review round 2: the pack has room for the first need, so CHARGE_PV is the only mode
    # and it fills the pack. The spill takes over while the need lasts and goes on as a
    # HOLD bridge after it; with no need left at all the run ends in a HOLD of its own.
    plan = solve(taper(lasting, minutes))
    modes = [flow.dispatch_mode for flow in plan.flows]
    assert modes[0] == "CHARGE_PV"
    assert modes[1 : 1 + lasting] == ["CURTAIL"] * lasting
    assert set(modes[1 + lasting :]) == {"HOLD"}
    assert plan.flows[0].end_soc_kwh == pytest.approx(10, abs=1e-5)


def test_pv_follow_run_ends_in_hold_only_once_the_pack_is_full(monkeypatch):
    # The model's release without its full-pack test: HOLD exports the later surplus at a
    # price, so leaving CHARGE_PV early pays. The certificate reads the pack: it had room.
    def release_anywhere(model, problem, vectors, *rest):
        filled = {}
        for i in range(1, len(vectors)):
            filled[i] = model.variable(upper=1.0)
            model.constrain({filled[i]: 1, vectors[i]["mode_HOLD"]: -1}, -math.inf, 0)
        return filled

    monkeypatch.setattr(dispatch_policy, "_fill_releases", release_anywhere)
    rows = ((1.0, 0.1, 1.2, 0),) + ((1.0, 0.5, 0.2, 0),) * 5
    with pytest.raises(SolveError) as error:
        solve(problem(rows, initial=3.0, minutes=60))
    assert "minimum mode duration" in str(error.value)


def bell_day(minutes, initial, drain_kw):
    """48 quarter hours of a 6 kW bell on a 10 kWh pack behind a 2 kW export cap."""
    rows = tuple(
        (1.0, 0.1, round(1.5 * math.sin(math.pi * (i + 0.5) / 48) ** 2, 3), 0.1)
        for i in range(48)
    )
    base = problem(rows, cap_kw=2.0, minutes=minutes, drain_kw=drain_kw)
    return replace(
        base, battery=replace(base.battery, capacity_kwh=10, initial_kwh=initial)
    )


@pytest.mark.parametrize("minutes", [30, 60])
@pytest.mark.parametrize("initial", [8.0, 9.7])
def test_bell_day_with_standby_loss_is_followed_to_its_end(minutes, initial):
    # Review round 2: the afternoon taper after a long spill. The run leaves the room of
    # its standby loss, the need drops below it, CHARGE_PV fills the pack and the need is
    # gone inside the hour. Every plan on the way is the next one's start.
    trace = followed(bell_day(minutes, initial, 0.02), 47)
    assert len(trace) == 47
    assert "CURTAIL" in trace


def test_binary_left_fractional_by_the_solver_is_solved_again(monkeypatch):
    # Review round 2: the bell day from its second interval, pack at 9.7 of 10. HiGHS
    # returned this as optimal with CURTAIL at 0.16 in the interval whose need (0.004 kWh)
    # is below the minimum activity; without presolve the same model has its plan.
    day = bell_day(60, 9.7, 0.0)
    day = replace(day, slots=day.slots[1:])
    runs = []
    run = optimize._Model._run

    def counted(self, matrix, time_limit_s, **options):
        runs.append(options)
        return run(self, matrix, time_limit_s, **options)

    monkeypatch.setattr(optimize._Model, "_run", counted)
    plan = solve(day)
    assert plan.flows[36].dispatch_mode == "CURTAIL"
    assert plan.flows[36].curtail_kwh == pytest.approx(0.025, abs=1e-6)
    # Solved once where the solver is sound; twice, the second without presolve, where not.
    assert runs in ([{}], [{}, {"presolve": False}])


def carried_hold(rows, battery, minutes, value=0.0):
    """Quarter hours on an inverter of 3 kW behind a 1 kW cap, a HOLD carried for one."""
    return Problem(
        tuple(
            Slot(START + i * QUARTER, START + (i + 1) * QUARTER, *row)
            for i, row in enumerate(rows)
        ),
        SiteLimits(3, 8, 1, False),
        battery,
        "preserve_initial",
        value,
        (),
        "UTC",
        minimum_mode_minutes=minutes,
        initial_dispatch_mode="HOLD",
        initial_dispatch_mode_since=START - QUARTER,
    )


def test_problem_the_presolve_calls_infeasible_is_solved_again(monkeypatch):
    # Rolled sweep (negroll, seed 11, problem 17, second plan): HiGHS presolve reported
    # this problem as infeasible; without presolve it has the plan main has.
    rows = (
        (0.78, 0.29, 0.36, 0.2),
        (0.62, -0.47, 0.32, 0.4),
        (1.07, -0.0, 0.33, 0.4),
        (0.51, -0.05, 0.34, 0.05),
        (0.87, -0.52, 0.27, 0.05),
        (0.61, -0.06, 0.26, 0.4),
        (1.1, -0.14, 0.25, 0.4),
        (0.54, 0.1, 0.36, 0.2),
        (0.39, -0.21, 0.36, 0.1),
        (0.9, -0.38, 0.26, 0.05),
        (0.7, -0.04, 0.28, 0.05),
        (0.33, 0.07, 0.37, 0.4),
    )
    battery = Battery(5, 0, 1, 4.995, 4, 4, 0.95, 0.95, 0.01, False, False, 0.02)
    runs = []
    run = optimize._Model._run

    def counted(self, matrix, time_limit_s, **options):
        runs.append(options)
        return run(self, matrix, time_limit_s, **options)

    monkeypatch.setattr(optimize._Model, "_run", counted)
    plan = solve(carried_hold(rows, battery, 15))
    assert plan.objective == pytest.approx(0.1156, abs=1e-4)
    assert all(flow.curtail_kwh == pytest.approx(0, abs=1e-6) for flow in plan.flows)
    assert runs in ([{}], [{}, {"presolve": False}])


def test_infeasible_site_is_not_solved_twice_without_a_forced_spill(monkeypatch):
    # The second run is for the forced-spill rows only: a site with nothing to spill (here
    # a load above what the grid and the pack deliver) reports its problem after one run.
    runs = []
    run = optimize._Model._run

    def counted(self, matrix, time_limit_s, **options):
        runs.append(options)
        return run(self, matrix, time_limit_s, **options)

    monkeypatch.setattr(optimize._Model, "_run", counted)
    with pytest.raises(SolveError) as error:
        solve(problem(((1.0, 0.1, 0, 5.0),) * 2))
    assert error.value.reason == "infeasible"
    assert runs == [{}]


def test_pinned_limit_hold_that_lost_the_full_level_has_no_plan_for_a_need():
    # Pinned limit (docs/model.md, forced spill), bell roll seed 12, problem 72: a HOLD
    # with a clock of its own (60 minutes) loses 0.0125 kWh a quarter hour to standby and
    # is below the full level when 0.021 kWh the site cannot pass arrives. The pack has
    # room, so that is no spill, and the HOLD cannot charge. A site that can curtail
    # takes the HOLD over with a CURTAIL; so does this one while the pack stays full.
    rows = ((0.9, 0.0, 0.054, 0.1), (1.14, 0.49, 0.192, 0.4), (0.78, 0.41, 0.371, 0.1))
    battery = Battery(5, 0, 1, 4.9875, 4, 1, 0.95, 0.95, 0.01, True, False, 0.05)
    day = carried_hold(rows, battery, 60, 0.3)
    with pytest.raises(SolveError) as error:
        solve(day)
    assert error.value.reason == "infeasible"
    able = solve(replace(day, site=replace(day.site, allow_curtailment=True)))
    assert able.flows[2].dispatch_mode == "CURTAIL"
    lossless = solve(replace(day, battery=replace(battery, idle_drain_kw=0.0)))
    assert lossless.flows[2].dispatch_mode == "CURTAIL"


def test_pinned_limit_curtail_run_with_a_gap_in_the_need_has_no_plan_at_60_minutes():
    # Pinned limit (docs/model.md, forced spill), rolled sweep negroll seed 12, problem
    # 65, fourth plan: PV around the inverter rating (0.75 kWh a quarter hour at 3 kW).
    # The first need opens a CURTAIL run of 60 minutes, the second interval has none (a
    # bridge) and the third has 0.02 kWh, less than the room two standby losses have made
    # (0.025 kWh): the relay rides the interval before, which is no CURTAIL, the gate reads
    # room, and the run's clock forbids the charge. The roll reaches it through the safety
    # exception: a CHARGE_PV commitment on a full pack keeps its clock over the forced
    # CURTAIL (`_commit_battery_direction`), so the next plan starts the run afresh.
    gap = (
        (1.0, 0.1, 0.81, 0.1),
        (1.0, 0.1, 0.75, 0.0),
        (1.0, 0.1, 0.77, 0.0),
        (1.0, 0.1, 0.75, 0.0),
    )
    site = {"cap_kw": 8, "inverter_kw": 3, "drain_kw": 0.05}
    with pytest.raises(SolveError) as error:
        solve(problem(gap, minutes=60, **site))
    assert error.value.reason == "infeasible"
    steady = (gap[0], gap[2], gap[2], gap[2])
    plan = solve(problem(steady, minutes=60, **site))
    assert [flow.dispatch_mode for flow in plan.flows] == ["CURTAIL"] * 4
    lossless = solve(problem(gap, minutes=60, **{**site, "drain_kw": 0.0}))
    assert [flow.dispatch_mode for flow in lossless.flows][::2] == ["CURTAIL"] * 2
    shorter = solve(problem(gap, minutes=30, **site))
    assert shorter.flows[2].dispatch_mode == "CHARGE_PV"
    able = solve(problem(gap, minutes=60, curtail=True, **site))
    assert able.flows[2].dispatch_mode == "CURTAIL"


def test_pack_refilled_after_a_carried_curtail_run_ends_the_pv_follow_run_in_hold():
    # Rolled sweep (negroll, seed 12, problem 79, fourth plan): the spill that began the
    # CURTAIL run is behind the horizon. The pack lost 0.0375 kWh to standby in the run,
    # so it has to charge that back (one CHARGE_PV interval) before the 0.03 kWh the 8 kW
    # inverter cannot pass in the fourth interval can be a spill; the plan before counted
    # on that CHARGE_PV ending in a HOLD, and so does this one.
    rows = (
        (0.71, 0.2, 1.73, 0.4),
        (1.18, -0.4, 1.75, 0.05),
        (1.45, -0.23, 1.36, 0.4),
        (0.49, 0.16, 2.03, 0.4),
        (1.31, 0.6, 1.63, 0.05),
        (1.34, -0.14, 1.37, 0.1),
    )
    day = Problem(
        tuple(
            Slot(START + i * QUARTER, START + (i + 1) * QUARTER, *row)
            for i, row in enumerate(rows)
        ),
        SiteLimits(8, 8, 8, False),
        Battery(5, 0, 1, 4.9625, 1, 1, 0.95, 0.95, 0.01, True, False, 0.05),
        "value",
        0,
        (),
        "UTC",
        minimum_mode_minutes=60,
        initial_dispatch_mode="CURTAIL",
        initial_dispatch_mode_since=START - 3 * QUARTER,
    )
    plan = solve(day)
    assert [flow.dispatch_mode for flow in plan.flows[:4]] == [
        "HOLD",
        "CHARGE_PV",
        "HOLD",
        "CURTAIL",
    ]
    assert 0 in plan.carried_bridges
    assert plan.flows[3].curtail_kwh == pytest.approx(0.03, abs=1e-6)


# T-072: 96 quarter hours of `(buy, sell, pv, load)` from 2026-12-18 11:00 UTC, a bell-shaped
# day on a 3 kW inverter behind a 2 kW export cap (random draw of the T-041 review, #61).
WINTER_DAY = (
    (0.91, 0.57, 0.0, 0.4),
    (1.07, 0.27, 0.0, 0.02),
    (1.31, 0.69, 0.0, 0.4),
    (1.37, 0.36, 0.0, 0.4),
    (1.19, 0.19, 0.0, 0.1),
    (1.32, 0.24, 0.0, 0.02),
    (1.37, 0.07, 0.0, 0.4),
    (1.0, 0.43, 0.0, 0.4),
    (0.27, 0.41, 0.0, 0.2),
    (1.48, 0.7, 0.0, 0.05),
    (1.34, 0.12, 0.0, 0.02),
    (0.82, 0.16, 0.0, 0.05),
    (1.34, 0.57, 0.0, 0.1),
    (0.47, 0.39, 0.0, 0.1),
    (0.99, 0.71, 0.0, 0.4),
    (0.72, 0.72, 0.0, 0.05),
    (0.54, 0.22, 0.0, 0.4),
    (0.99, 0.44, 0.0, 0.1),
    (1.05, 0.74, 0.0, 0.05),
    (0.78, 0.48, 0.0, 0.4),
    (0.76, 0.69, 0.0, 0.2),
    (1.07, 0.64, 0.0, 0.05),
    (0.34, 0.14, 0.0, 0.2),
    (1.08, 0.35, 0.0, 0.2),
    (0.91, 0.63, 0.002, 0.05),
    (1.37, 0.02, 0.017, 0.05),
    (0.87, 0.5, 0.045, 0.4),
    (0.69, 0.45, 0.016, 0.1),
    (0.6, 0.54, 0.031, 0.4),
    (1.26, 0.1, 0.054, 0.1),
    (1.08, 0.3, 0.305, 0.1),
    (1.22, 0.42, 0.417, 0.2),
    (0.42, 0.64, 0.601, 0.4),
    (1.03, 0.5, 0.68, 0.05),
    (1.27, 0.6, 0.654, 0.02),
    (0.93, 0.29, 0.783, 0.2),
    (0.21, 0.29, 0.915, 0.02),
    (1.48, 0.02, 0.929, 0.2),
    (0.78, 0.3, 1.155, 0.4),
    (0.55, 0.65, 1.12, 0.4),
    (0.9, 0.41, 1.355, 0.05),
    (0.27, 0.36, 1.343, 0.1),
    (0.52, 0.42, 1.574, 0.4),
    (0.99, 0.19, 1.461, 0.02),
    (1.06, 0.67, 1.642, 0.4),
    (0.93, 0.15, 1.646, 0.2),
    (1.39, 0.43, 1.592, 0.05),
    (0.23, 0.2, 1.696, 0.02),
    (0.39, 0.33, 1.623, 0.02),
    (0.4, 0.56, 1.668, 0.2),
    (0.43, 0.63, 1.561, 0.4),
    (1.39, 0.06, 1.461, 0.4),
    (1.24, 0.28, 1.814, 0.2),
    (1.33, 0.32, 1.392, 0.1),
    (0.37, 0.25, 1.746, 0.1),
    (0.84, 0.76, 1.207, 0.02),
    (1.11, 0.37, 1.268, 0.1),
    (0.37, 0.09, 1.134, 0.4),
    (0.7, 0.63, 1.018, 0.02),
    (0.24, 0.55, 1.105, 0.05),
    (0.24, 0.75, 0.854, 0.2),
    (1.12, 0.72, 0.826, 0.1),
    (0.68, 0.41, 0.675, 0.4),
    (0.82, 0.55, 0.591, 0.05),
    (0.58, 0.2, 0.357, 0.4),
    (0.89, 0.07, 0.355, 0.05),
    (0.61, 0.16, 0.234, 0.4),
    (1.06, 0.22, 0.135, 0.4),
    (0.76, 0.71, 0.096, 0.2),
    (1.5, 0.35, 0.055, 0.4),
    (0.81, 0.61, 0.021, 0.02),
    (0.87, 0.52, 0.002, 0.02),
    (0.54, 0.35, 0.0, 0.2),
    (0.46, 0.7, 0.0, 0.1),
    (0.63, 0.7, 0.0, 0.02),
    (0.41, 0.21, 0.0, 0.4),
    (0.94, 0.28, 0.0, 0.05),
    (0.24, 0.61, 0.0, 0.02),
    (0.85, 0.12, 0.0, 0.1),
    (0.37, 0.58, 0.0, 0.02),
    (1.22, 0.38, 0.0, 0.05),
    (0.86, 0.19, 0.0, 0.1),
    (0.8, 0.33, 0.0, 0.02),
    (0.36, 0.08, 0.0, 0.1),
    (0.87, 0.42, 0.0, 0.05),
    (1.24, 0.39, 0.0, 0.2),
    (0.8, 0.49, 0.0, 0.1),
    (0.61, 0.66, 0.0, 0.4),
    (1.38, 0.76, 0.0, 0.2),
    (0.3, 0.66, 0.0, 0.05),
    (1.15, 0.14, 0.0, 0.1),
    (1.18, 0.1, 0.0, 0.1),
    (0.42, 0.7, 0.0, 0.1),
    (0.29, 0.46, 0.0, 0.02),
    (0.22, 0.72, 0.0, 0.05),
    (0.85, 0.68, 0.0, 0.05),
)


def winter_day() -> Problem:
    start = datetime(2026, 12, 18, 11, tzinfo=UTC)
    return Problem(
        tuple(
            Slot(start + i * QUARTER, start + (i + 1) * QUARTER, *row)
            for i, row in enumerate(WINTER_DAY)
        ),
        SiteLimits(3, 8, 2, False),
        Battery(10, 0, 1, 7.06, 4, 1, 0.95, 0.95, 0, False, True, 0.02),
        "preserve_initial",
        0,
        (),
        "UTC",
        minimum_mode_minutes=0,
        minimum_export_episode_benefit=0.0,
    )


def test_plan_of_the_day_highs_misjudges_without_presolve_is_the_better_one():
    # T-072: the pack is emptied into the evening prices, filled by the PV and the rest
    # spills. A point at -4.5041 satisfies every row; a plan above it is not the optimum.
    plan = solve(winter_day(), time_limit_s=30.0)
    assert plan.objective <= -4.5041 + 1e-4


def test_pinned_limit_highs_without_presolve_calls_a_worse_plan_optimal(monkeypatch):
    # T-072: the second attempt of `_Model.solve` is not a proof. On the same matrix HiGHS
    # without presolve reports `Optimal` 2.76 above the plan of the default run (-1.7465
    # against -4.5041; HiGHS 1.8.1 to 1.15.1, any seed): its root cuts cut the optimum off.
    # The default run is the one taken here, so the plan is the better one. Red after a
    # SciPy upgrade means HiGHS no longer does this: correct `docs/model.md` (forced spill)
    # and ADR-0007 (Amendment T-072) with what the new version does.
    # The matrix is the one before T-066: with the spill beyond the charge power ungated
    # the model is easier and HiGHS without presolve finds the optimum on it.
    monkeypatch.setattr(optimize, "forced_spill_limit", lambda *a, **k: 0)
    seen = {}
    run = optimize._Model._run

    def kept(self, matrix, time_limit_s, **options):
        result = run(self, matrix, time_limit_s, **options)
        seen.setdefault("first", (self, matrix, options, result))
        return result

    monkeypatch.setattr(optimize._Model, "_run", kept)
    plan = solve(winter_day(), time_limit_s=30.0)
    model, matrix, options, first = seen["first"]
    assert model.second_attempt
    assert options == {}
    assert first.status == 0
    assert first.fun == pytest.approx(plan.objective, abs=1e-6)
    second = run(model, matrix, 30.0, presolve=False)
    assert second.status == 0
    assert second.fun == pytest.approx(-1.7465, abs=1e-3)
    assert second.fun > first.fun + 2.5


# T-066 review: one draw of `benchmarks/spill_sweep.py probe` (seed 77, index 282): a pack
# at 9.9 of 10 kWh behind a 0.5 kW export cap, a flexible load of 2 kWh at 2 kW.
PROBE_DAY = (
    (1.01, 0.57, 1.94, 0.05),
    (0.8, -0.46, 1.54, 0.1),
    (0.85, -0.57, 1.72, 0.02),
    (1.45, 0.18, 2.11, 0.2),
    (1.13, 0.52, 1.94, 0.4),
    (1.4, 0.23, 1.61, 0.2),
    (0.55, 0.29, 1.88, 0.2),
    (1.13, -0.35, 1.32, 0.1),
    (1.45, 0.41, 1.61, 0.02),
    (1.3, 0.19, 1.7, 0.2),
    (1.44, 0.75, 1.82, 0.05),
    (0.37, -0.4, 1.6, 0.05),
    (0.44, 0.25, 1.56, 0.4),
    (0.79, 0.62, 2.13, 0.05),
)


def probe_day() -> Problem:
    start = datetime(2026, 12, 18, 11, tzinfo=UTC)
    return Problem(
        tuple(
            Slot(start + i * QUARTER, start + (i + 1) * QUARTER, *row)
            for i, row in enumerate(PROBE_DAY)
        ),
        SiteLimits(8, 8, 0.5, False),
        Battery(10, 0, 1, 9.9, 4, 4, 0.95, 0.95, 0.01, True, True, 0.05),
        "value",
        0,
        (),
        "UTC",
        minimum_mode_minutes=0,
        minimum_export_episode_benefit=0.0,
    )


def test_probe_with_a_spill_beyond_the_charge_power_is_planned_at_the_certified_cost():
    # A plan at -0.396053 satisfies every row of the model (also of the model before the
    # spill row was split into an ungated and a gated part, where HiGHS called -0.339803
    # optimal with status 0, gap 0 and no fractional binary: the default run was the
    # worse one and the second attempt never fired).
    plan = solve_flexible_load(probe_day(), FlexibleLoadRequest(2, 2)).plan
    assert plan.objective <= -0.396053 + 1e-4


def test_both_solver_switches_find_the_certified_cost_of_the_probe_day(monkeypatch):
    # T-066 review, LESSONS solver-defect-pinned-on-one-switch: the same matrix solved with
    # and without presolve, each against the certified point.
    seen = {}
    run = optimize._Model._run

    def kept(self, matrix, time_limit_s, **options):
        result = run(self, matrix, time_limit_s, **options)
        seen.setdefault("first", (self, matrix, options, result))
        return result

    monkeypatch.setattr(optimize._Model, "_run", kept)
    solve_flexible_load(probe_day(), FlexibleLoadRequest(2, 2))
    model, matrix, options, first = seen["first"]
    assert model.second_attempt
    assert options == {}
    assert first.status == 0
    assert first.fun <= -0.396053 + 1e-4
    unprocessed = run(model, matrix, 30.0, presolve=False)
    assert unprocessed.status == 0
    assert unprocessed.fun <= -0.396053 + 1e-4
