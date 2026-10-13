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
    # Pinned limit (docs/model.md, forced spill): a CURTAIL interval charges nothing, so a
    # pack 0.5 kWh below full stays there while the surplus lasts. CHARGE_PV cannot take
    # 0.95 kWh into 0.5 kWh of room and the unmoded rule forbids it a partial charge.
    rows = ((1.0, 0.1, 1.2, 0),) * 4
    plan = solve(problem(rows, initial=4.5, minutes=60))
    assert [flow.dispatch_mode for flow in plan.flows] == ["CURTAIL"] * 4
    assert [flow.end_soc_kwh for flow in plan.flows] == pytest.approx([4.5] * 4)


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
def test_pinned_limit_pack_with_room_that_charges_too_slowly_has_no_plan(minutes):
    # Pinned limit (docs/model.md, forced spill): the gate reads the room of the pack, not
    # its charge power. At 0.4 kW the pack takes 0.1 kWh a quarter hour of a 0.95 kWh
    # surplus: the inverter would throttle the rest, but the pack has room, so the plan
    # may not call that a spill. Once the pack is full the same site is planned.
    rows = ((1.0, 0.1, 1.2, 0),) * 4
    with pytest.raises(SolveError) as error:
        solve(problem(rows, initial=1.0, minutes=minutes, charge_kw=0.4))
    assert error.value.reason == "infeasible"
    plan = solve(problem(rows, initial=5.0, minutes=minutes, charge_kw=0.4))
    assert all(flow.curtail_kwh == pytest.approx(0.95, abs=1e-5) for flow in plan.flows)


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
    return replace(base, battery=replace(base.battery, capacity_kwh=10, initial_kwh=9.875))


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
    return replace(base, battery=replace(base.battery, capacity_kwh=10, initial_kwh=initial))


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
