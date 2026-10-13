"""CHARGE_PV takes what PV leaves over: no grid export beside a spare charge power."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_dispatch_policy import dispatch

from custom_components.energy_compass.engine.dispatch_policy import validate_modes
from custom_components.energy_compass.engine.machine_state import machine_state
from custom_components.energy_compass.engine.models import (
    FlexibleLoadRequest,
    Flow,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import (
    solve,
    solve_flexible_load,
)


def sunny(slots=8, *, pv=0.6, load=0.1, sell=0.9, initial=2.0, lock=True, **changes):
    """Selling beats storing; CHARGE_PV carried in, so the lock forces charging."""
    changes = {"minimum_mode_minutes": 60, **changes}
    problem = dispatch(tuple((1.0, sell, pv, load) for _ in range(slots)), **changes)
    problem = replace(problem, battery=replace(problem.battery, initial_kwh=initial))
    if lock:
        problem = replace(
            problem,
            initial_dispatch_mode="CHARGE_PV",
            initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=15),
        )
    return problem


def assert_pv_following(problem, plan):
    battery = problem.battery
    ceiling = battery.capacity_kwh * battery.maximum_soc_fraction
    for slot, flow in zip(problem.slots, plan.flows, strict=True):
        hours = (slot.end - slot.start).total_seconds() / 3600
        if machine_state(slot, flow) != "CHARGE_PV" or flow.grid_export_kwh < 1e-5:
            continue
        assert (
            flow.charge_kwh >= battery.charge_kw * hours - 1e-5
            or flow.end_soc_kwh >= ceiling - 1e-4
        ), (slot.start, flow)


def test_carried_charge_pv_takes_the_whole_surplus_instead_of_trickling():
    # The previous model satisfied the carried lock with the minimum charge and
    # exported the rest, which the inverter cannot execute.
    problem = sunny()
    plan = solve(problem)
    rows = list(zip(problem.slots, plan.flows, strict=True))
    assert [machine_state(s, f) for s, f in rows[:3]] == ["CHARGE_PV"] * 3
    for _, flow in rows[:3]:
        assert flow.charge_kwh == pytest.approx(0.5)
        assert flow.grid_export_kwh == pytest.approx(0)
    assert_pv_following(problem, plan)


def test_export_is_allowed_once_charge_is_at_its_power_limit():
    # 1.9 kWh of surplus per quarter hour; the 4 kW battery takes at most 1 kWh.
    problem = sunny(slots=4, pv=2.0)
    plan = solve(problem)
    for flow in plan.flows[:3]:
        assert flow.dispatch_mode == "CHARGE_PV"
        assert flow.charge_kwh == pytest.approx(1)
        assert flow.grid_export_kwh == pytest.approx(0.9)
    assert_pv_following(problem, plan)


def test_export_is_allowed_when_the_battery_is_filled_to_its_ceiling():
    # 0.2 kWh of room cannot take a 0.5 kWh surplus: the inverter fills the pack
    # and exports the rest.
    problem = sunny(
        slots=4,
        initial=9.8,
        lock=False,
        terminal_value_per_kwh=2.0,
        minimum_mode_minutes=15,
    )
    plan = solve(problem)
    # Which slot tops the pack up is a tie; that it can is the point.
    filling = [f for f in plan.flows if f.dispatch_mode == "CHARGE_PV"]
    assert len(filling) == 1
    assert filling[0].end_soc_kwh == pytest.approx(10)
    assert filling[0].charge_kwh == pytest.approx(0.2)
    assert filling[0].grid_export_kwh == pytest.approx(0.3)
    assert_pv_following(problem, plan)


def test_free_plan_has_no_partial_charge_rows():
    problem = sunny(slots=24, lock=False, sell=0.5)
    plan = solve(problem)
    assert_pv_following(problem, plan)
    for slot, flow in zip(problem.slots, plan.flows, strict=True):
        if machine_state(slot, flow) == "HOLD":
            assert flow.charge_kwh == pytest.approx(0)


def test_validator_rejects_partial_charge_with_export():
    problem = dispatch(((1, 0, 5, 0),) * 2, minutes=60, minimum_mode_minutes=60)

    def rows(export_kwh, charge_kwh, soc):
        return tuple(
            Flow(0, export_kwh, charge_kwh, 0, 0, soc(i), "charge", "CHARGE_PV")
            for i in range(2)
        )

    # The 4 kW battery takes at most 4 kWh an hour: 1 kWh beside export is partial.
    with pytest.raises(SolveError, match="charge-from-PV exports"):
        validate_modes(problem, rows(4, 1, lambda i: (i + 1) * 1.0))
    with pytest.raises(SolveError, match="charge-from-PV exports"):
        validate_modes(problem, rows(2, 3, lambda i: (i + 1) * 3.0))
    # At the power limit, or filled to the ceiling, the export is what is left.
    validate_modes(problem, rows(1, 4, lambda i: (i + 1) * 4.0))
    validate_modes(
        replace(problem, battery=replace(problem.battery, initial_kwh=8)),
        (
            Flow(3, 0, 2, 0, 0, 10, "charge", "CHARGE_PV"),
            Flow(5, 5, 0, 0, 0, 10, None, "HOLD"),
        ),
    )
    validate_modes(
        replace(problem, battery=replace(problem.battery, initial_kwh=8)),
        (
            Flow(0, 3, 2, 0, 0, 10, "charge", "CHARGE_PV"),
            Flow(0, 5, 0, 0, 0, 10, None, "HOLD"),
        ),
    )


def big_battery(pv, load, limit, **battery):
    """One hourly slot, a battery taking `limit` kWh in it, a site that passes all of it."""
    problem = dispatch(((1, 0, pv, load),), minutes=60, minimum_mode_minutes=60)
    return replace(
        problem,
        site=replace(problem.site, inverter_kw=4 * limit, grid_export_kw=4 * limit),
        battery=replace(
            problem.battery,
            capacity_kwh=10 * limit,
            charge_kw=limit,
            discharge_kw=limit,
            **battery,
        ),
    )


@pytest.mark.parametrize("limit", [10, 25, 40])
def test_validator_accepts_the_charge_a_follow_row_leaves_short_by_its_noise(limit):
    """T-049: `bc >= M * (x_CHARGE_PV - full)` is met within 1e-6 * (2M + 1), M the limit.

    HiGHS took the CHARGE_PV binary at 1 - 1e-6 and charged 2.5e-5 kWh under the 25 kWh
    limit of a real 48-slot problem (T-036 corpus, seed 108, pinned in
    test_solver_noise_regression.py); the surplus beyond it is exported by the site
    balance, which is no partial charge. The shortfall here is the most that row allows,
    above the fixed 1e-5 kWh the check used before.
    """
    problem = big_battery(99, 16, limit)
    short = 0.99e-6 * (2 * limit + 1)
    assert short > 1e-5
    charge = limit - short
    flow = Flow(0, 83 - charge, charge, 0, 0, charge, "charge", "CHARGE_PV")

    validate_modes(problem, (flow,))


def test_validator_still_rejects_a_charge_short_by_more_than_the_noise_of_its_row():
    problem = big_battery(99, 16, 25)
    # 2 * 1e-6 * (2 * 99 + 1) is above the bound 1e-6 * (2M + 1) for M = 99.
    charge = 25 - 1e-6 * (2 * 99 + 1) * 2
    flow = Flow(0, 83 - charge, charge, 0, 0, charge, "charge", "CHARGE_PV")

    with pytest.raises(SolveError, match="charge-from-PV exports"):
        validate_modes(problem, (flow,))


def test_validator_widens_the_charge_noise_to_the_three_binaries_of_a_flexible_row():
    """T-054: with a flexible load the surplus row carries a third binary (the power one).

    Surplus 83 kWh (PV 99, load 16) makes the row's M 83: it leaves up to
    1e-6 * (3 * 83 + 1) = 2.5e-4 kWh short, above the 2.0e-4 kWh (2M + 1 with M = PV 99)
    the check allows without a flexible load.
    """
    problem = big_battery(99, 16, 25)
    short = 0.99e-6 * (3 * 83 + 1)
    assert short > 1e-6 * (2 * 99 + 1)
    charge = 25 - short
    flow = Flow(0, 83 - charge, charge, 0, 0, charge, "charge", "CHARGE_PV")

    validate_modes(problem, (flow,), flex_max=(30.0,))
    with pytest.raises(SolveError, match="charge-from-PV exports"):
        validate_modes(problem, (flow,))
    # Twice the noise of the three binaries is a real partial charge.
    charge = 25 - 2 * 1e-6 * (3 * 99 + 1)
    flow = Flow(0, 83 - charge, charge, 0, 0, charge, "charge", "CHARGE_PV")
    with pytest.raises(SolveError, match="charge-from-PV exports"):
        validate_modes(problem, (flow,), flex_max=(30.0,))


@pytest.mark.parametrize("left", [3, 2])
@pytest.mark.parametrize("room", [0.3, 0.4])
@pytest.mark.parametrize(
    ("ceiling", "idle_drain_kw"), [(1.0, 0.0), (0.9, 0.0), (1.0, 0.2), (0.9, 0.2)]
)
def test_carried_charge_pv_ends_on_a_full_pack(left, room, ceiling, idle_drain_kw):
    # The pack takes the whole surplus, so it can reach its ceiling before the
    # commitment ends; the inverter then holds, which must not make the plan
    # infeasible. With standby loss the pack keeps taking the draw and stays
    # CHARGE_PV.
    problem = sunny(initial=10 * ceiling - room)
    problem = replace(
        problem,
        battery=replace(
            problem.battery, maximum_soc_fraction=ceiling, idle_drain_kw=idle_drain_kw
        ),
        initial_dispatch_mode_since=problem.slots[0].start
        - timedelta(minutes=60 - 15 * left),
    )
    plan = solve(problem)
    assert_pv_following(problem, plan)
    # What the pack must end above for the next committed interval to be HOLD.
    release = 10 * ceiling + idle_drain_kw * 0.25 - problem.minimum_mode_power_kw * 0.25
    ended = problem.battery.initial_kwh
    modes = []
    for flow in plan.flows[:left]:
        modes.append(flow.dispatch_mode)
        if flow.dispatch_mode == "HOLD":
            assert ended >= release - 1e-4
        ended = flow.end_soc_kwh
    assert modes[0] == "CHARGE_PV"
    # A hold starts its own minimum duration: it is not left within the commitment.
    if "HOLD" in modes:
        assert set(modes[modes.index("HOLD") :]) == {"HOLD"}
    else:
        assert idle_drain_kw


def test_carried_charge_pv_keeps_charging_while_the_pack_has_room():
    # A pack with room to charge keeps the commitment: no hold below the ceiling.
    problem = sunny(initial=5.0, pv=0.6, load=0.1)
    plan = solve(problem)
    assert [f.dispatch_mode for f in plan.flows[:3]] == ["CHARGE_PV"] * 3


def test_validator_accepts_a_hold_after_a_full_pack_in_a_carried_commitment():
    problem = dispatch(((1, 0, 0.6, 0.1),) * 4, minimum_mode_minutes=60)
    start = problem.slots[0].start
    problem = replace(
        problem,
        initial_dispatch_mode="CHARGE_PV",
        initial_dispatch_mode_since=start - timedelta(minutes=15),
    )

    def flows(initial, soc):
        battery = replace(problem.battery, initial_kwh=initial)
        rows = (
            Flow(
                0,
                0.5 - (soc[0] - initial),
                soc[0] - initial,
                0,
                0,
                soc[0],
                "charge",
                "CHARGE_PV",
            ),
            Flow(0, 0.5, 0, 0, 0, soc[0], None, "HOLD"),
            Flow(0, 0.5, 0, 0, 0, soc[0], None, "HOLD"),
            Flow(0, 0.5, 0, 0, 0, soc[0], None, "HOLD"),
        )
        return replace(problem, battery=battery), rows

    # Filled to the ceiling in the first committed interval: hold is the rest.
    validate_modes(*flows(9.8, (10.0,)))
    # Room left for a minimum activity: the commitment goes on.
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(*flows(9.0, (9.5,)))
    # Room for one more minimum activity (0.025 kWh) keeps the commitment on.
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(*flows(9.45, (9.95,)))
    # Less room than that cannot take it: the pack is as good as full.
    validate_modes(*flows(9.49, (9.99,)))


def test_flexible_load_probe_keeps_charge_pv_following():
    problem = sunny(slots=12, pv=0.8, load=0.1)
    plan = solve_flexible_load(problem, FlexibleLoadRequest(1.0, 2.0))
    assert sum(plan.schedule_kwh) == pytest.approx(1.0)
    slots = tuple(
        replace(slot, load_kwh=slot.load_kwh + extra)
        for slot, extra in zip(problem.slots, plan.schedule_kwh, strict=True)
    )
    assert_pv_following(replace(problem, slots=slots), plan.plan)


def partial_need(slots=None, **changes):
    """One sunny quarter, then three that draw the pack down.

    The pack wants 0.6 of the 1.2 kWh of surplus. Without the rule it charged 0.6
    next to export of 0.6, which a controller holding the charge cap cannot do.
    """
    changes = {"minimum_mode_minutes": 0, **changes}
    rows = slots or (
        (3.0, 0.1, 1.5, 0.3),
        (2.0, 0.9, 0.0, 1.0),
        (1.0, 0.1, 0.0, 0.8),
        (0.5, 0.0, 0.0, 0.8),
    )
    problem = dispatch(rows, minutes=15, **changes)
    return replace(
        problem,
        battery=replace(problem.battery, initial_kwh=2.0, allow_grid_charge=False),
    )


def test_charge_pv_follows_the_surplus_without_a_minimum_mode_duration():
    problem = partial_need()
    plan = solve(problem)
    assert_pv_following(problem, plan)
    first = plan.flows[0]
    # The 4 kW battery takes 1 kWh a quarter hour: charge it all or not at all.
    assert first.charge_kwh == pytest.approx(0) or first.charge_kwh == pytest.approx(1)


def test_charge_pv_without_a_duration_exports_at_the_power_limit():
    # 1.9 kWh of surplus per quarter hour; the 4 kW battery takes at most 1 kWh.
    problem = partial_need(
        slots=((1.0, 0.1, 2.0, 0.1),) * 2 + ((3.0, 0.0, 0.0, 1.0),) * 2
    )
    problem = replace(problem, battery=replace(problem.battery, initial_kwh=0))
    plan = solve(problem)
    charging = [f for f in plan.flows if f.charge_kwh > 1e-6]
    assert charging
    for flow in charging:
        assert flow.charge_kwh == pytest.approx(1)
        assert flow.grid_export_kwh == pytest.approx(0.9)
    assert_pv_following(problem, plan)


def test_charge_pv_without_a_duration_exports_once_the_pack_is_full():
    problem = sunny(
        slots=4,
        initial=9.8,
        lock=False,
        terminal_value_per_kwh=2.0,
        minimum_mode_minutes=0,
    )
    plan = solve(problem)
    filling = [f for f in plan.flows if f.charge_kwh > 1e-6]
    assert len(filling) == 1
    assert filling[0].end_soc_kwh == pytest.approx(10)
    assert filling[0].charge_kwh == pytest.approx(0.2)
    assert filling[0].grid_export_kwh == pytest.approx(0.3)
    assert_pv_following(problem, plan)


@pytest.mark.parametrize(
    ("pv", "request_"),
    (
        # The surplus (1.7 and 1.2 kWh) is above the 1 kWh charge limit, so the rule is
        # "charge 1 kWh or none" less whatever the probe takes of the surplus.
        (2.0, FlexibleLoadRequest(0.3, 1.0)),
        (2.0, FlexibleLoadRequest(0.5, 4.0)),
        (1.5, FlexibleLoadRequest(0.3, 1.0)),
    ),
)
def test_flexible_load_probe_keeps_charge_pv_following_without_a_duration(pv, request_):
    # Without the rule the probe's plan charged 0.6-0.65 kWh of the surplus at the
    # first quarter and exported the rest, a state a controller holding the charge
    # cap cannot produce.
    problem = partial_need(
        slots=(
            (3.0, 0.1, pv, 0.3),
            (2.0, 0.9, 0.0, 1.0),
            (1.0, 0.1, 0.0, 0.8),
            (0.5, 0.0, 0.0, 0.8),
        )
    )
    result = solve_flexible_load(problem, request_)
    assert sum(result.schedule_kwh) == pytest.approx(request_.energy_kwh)
    slots = tuple(
        replace(slot, load_kwh=slot.load_kwh + extra)
        for slot, extra in zip(problem.slots, result.schedule_kwh, strict=True)
    )
    assert_pv_following(replace(problem, slots=slots), result.plan)
    first = result.plan.flows[0]
    # The 4 kW battery takes 1 kWh a quarter hour: charge it all or not at all, unless
    # the probe's own load leaves less surplus than that.
    assert (
        first.charge_kwh == pytest.approx(0)
        or first.charge_kwh >= min(1.0, pv - 0.3 - result.schedule_kwh[0]) - 1e-5
    )


def test_validator_rejects_partial_charge_with_export_without_a_duration():
    problem = dispatch(((1, 0, 5, 0),) * 2, minutes=60, minimum_mode_minutes=0)

    def rows(export_kwh, charge_kwh, soc):
        return tuple(
            Flow(0, export_kwh, charge_kwh, 0, 0, soc(i), "charge", "CHARGE_PV")
            for i in range(2)
        )

    with pytest.raises(SolveError, match="charge-from-PV exports"):
        validate_modes(problem, rows(4, 1, lambda i: (i + 1) * 1.0))
    validate_modes(problem, rows(1, 4, lambda i: (i + 1) * 4.0))


def full_pack_problem(pv, *, initial, export_cap=None, lead=15, drain=0.0, **site):
    problem = dispatch(
        tuple((1.0, 0.9, value, 0.1) for value in pv), minimum_mode_minutes=60
    )
    if export_cap is not None:
        site["grid_export_kw"] = export_cap
    return replace(
        problem,
        site=replace(problem.site, **site),
        battery=replace(problem.battery, initial_kwh=initial, idle_drain_kw=drain),
        initial_dispatch_mode="CHARGE_PV",
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=lead),
    )


@pytest.mark.parametrize("initial", [9.7, 10.0])
def test_full_pack_in_a_carried_commitment_curtails_above_the_export_cap(initial):
    # A 2 kW export cap passes 0.5 kWh per interval; the pack fills in the
    # commitment and the 0.6 kWh surplus after it is curtailed, which the released
    # HOLD's own minimum duration must not block.
    problem = full_pack_problem(
        (0.5, 0.2, 0.5, 0.7, 0.7, 0.7), initial=initial, export_cap=2
    )
    plan = solve(problem)
    modes = [flow.dispatch_mode for flow in plan.flows]
    assert modes[3:] == ["CURTAIL"] * 3
    assert "HOLD" in modes[:3]
    assert all(flow.curtail_kwh > 0.05 for flow in plan.flows[3:])


@pytest.mark.parametrize("initial", [9.7, 10.0])
def test_full_pack_in_a_carried_commitment_curtails_above_the_inverter(initial):
    problem = full_pack_problem((0.5, 0.6, 0.6, 2.2, 2.2, 2.2), initial=initial)
    plan = solve(problem)
    modes = [flow.dispatch_mode for flow in plan.flows]
    assert modes[3:] == ["CURTAIL"] * 3
    assert modes[0] == ("CHARGE_PV" if initial < 10 else "HOLD")


def test_full_pack_curtails_inside_the_commitment_above_the_export_cap():
    # The commitment is slots 0-3. The pack fills in slot 0 and the 0.8 kWh
    # surplus beside it is 0.3 kWh above the 0.5 kWh cap: CURTAIL, inside the
    # commitment, not after its deadline.
    problem = full_pack_problem((0.5, *(0.9,) * 7), initial=9.7, export_cap=2, lead=0)
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == [
        "CHARGE_PV",
        *("CURTAIL",) * 7,
    ]
    assert plan.flows[1].curtail_kwh == pytest.approx(0.3)


def test_released_hold_turns_to_curtail_inside_the_commitment():
    # Slot 1 has no surplus to curtail (HOLD, released by the full pack); slot 2 is
    # still inside the commitment and has, so the released HOLD ends in CURTAIL
    # without waiting for the commitment's deadline.
    problem = full_pack_problem(
        (0.5, 0.3, *(0.9,) * 6), initial=9.7, export_cap=2, lead=0
    )
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == [
        "CHARGE_PV",
        "HOLD",
        *("CURTAIL",) * 6,
    ]


def test_curtail_under_way_goes_on_while_standby_loss_drains_the_pack():
    # The pack drops below the full-pack ceiling in every CURTAIL interval (0.05 kW
    # standby loss), so "the pack is full" no longer licenses the next CURTAIL: the
    # CURTAIL already under way must.
    problem = full_pack_problem(
        (0.5, *(0.9,) * 7), initial=9.7, export_cap=2, lead=0, drain=0.05
    )
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == [
        "CHARGE_PV",
        *("CURTAIL",) * 7,
    ]
    assert [flow.curtail_kwh for flow in plan.flows[1:]] == pytest.approx([0.3] * 7)
    assert [flow.end_soc_kwh for flow in plan.flows[:4]] == pytest.approx(
        [10.0, 9.9875, 9.975, 9.9625]
    )


def test_curtail_under_way_goes_on_when_the_pack_falls_well_below_the_ceiling():
    # 0.15 kWh of standby loss per interval takes the pack out of "full" after two
    # intervals; the 0.9 kWh of PV still has 0.3 kWh above the cap to curtail, so the
    # CURTAIL under way must go on without the full pack's licence.
    problem = full_pack_problem(
        (0.5, *(0.9,) * 7), initial=9.7, export_cap=2, lead=0, drain=0.6
    )
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == [
        "CHARGE_PV",
        *("CURTAIL",) * 7,
    ]
    assert plan.flows[3].end_soc_kwh < 9.55


def test_released_hold_turns_to_curtail_after_standby_loss_drained_the_pack():
    # Slot 3 is the last committed interval; the pack (9.975 kWh) is below the
    # 9.9875 kWh ceiling there, so only the released HOLD allows the CURTAIL. The
    # 0.62 kWh of PV is 0.02 kWh above the cap, less than the minimum activity: the
    # pack could still take it, and a charge costs wear, so curtailing is the choice.
    problem = full_pack_problem(
        (0.5, 0.0, 0.0, *(0.62,) * 5), initial=9.7, export_cap=2, lead=0, drain=0.05
    )
    problem = replace(problem, battery=replace(problem.battery, wear_per_kwh=0.3))
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == [
        "CHARGE_PV",
        "HOLD",
        "HOLD",
        *("CURTAIL",) * 5,
    ]


def test_full_pack_curtails_inside_the_commitment_above_the_inverter_rating():
    # 2.2 kWh of PV against 2 kWh the 8 kW inverter passes per interval.
    problem = full_pack_problem((0.5, *(2.2,) * 7), initial=9.7, lead=0)
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == [
        "CHARGE_PV",
        *("CURTAIL",) * 7,
    ]
    assert plan.flows[1].curtail_kwh == pytest.approx(0.2)


def test_hold_released_from_a_depleted_pack_goes_back_to_the_commitment():
    # An empty pack and no surplus in slot 0 end the commitment in HOLD. The pack is
    # still empty when the surplus comes, and charging it is what the inverter does:
    # the HOLD is released with no clock of its own, so the plan goes back to the
    # commitment (T-050; before, it held until the commitment's deadline). The
    # surplus pays to store (sells at 0.1, dear import after the commitment).
    rows = (
        (1.0, 0.1, 0.0, 0.1),
        *((1.0, 0.1, 0.9, 0.1),) * 3,
        *((2.0, 1.9, 0.0, 0.5),) * 4,
    )
    problem = dispatch(rows, minimum_mode_minutes=60)
    problem = replace(
        problem,
        battery=replace(problem.battery, initial_kwh=0.0),
        initial_dispatch_mode="CHARGE_PV",
        initial_dispatch_mode_since=problem.slots[0].start,
    )
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows[:4]] == [
        "HOLD",
        *("CHARGE_PV",) * 3,
    ]


def test_a_pack_with_room_keeps_the_commitment_instead_of_curtailing():
    # CURTAIL follows a full pack only: with room left the pack takes the PV.
    problem = full_pack_problem((0.5, 2.2, 2.2, 2.2, 2.2, 2.2), initial=2.0)
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows[:3]] == ["CHARGE_PV"] * 3


def test_validator_accepts_curtail_after_a_full_pack_in_a_carried_commitment():
    problem = dispatch(((1, 0, 0.6, 0.1),) * 8, minimum_mode_minutes=60)
    problem = replace(
        problem,
        initial_dispatch_mode="CHARGE_PV",
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=15),
    )
    full = replace(problem.battery, initial_kwh=9.8)
    problem = replace(problem, battery=full)
    first = Flow(0, 0.3, 0.2, 0, 0, 10, "charge", "CHARGE_PV")
    hold = Flow(0, 0.5, 0, 0, 0, 10, None, "HOLD")
    curtail = Flow(0, 0, 0, 0, 0.5, 10, None, "CURTAIL")
    # A full pack ends the commitment in HOLD or CURTAIL, and HOLD may turn to
    # CURTAIL inside the commitment: the released HOLD has no clock of its own.
    tail = (curtail,) * 4
    validate_modes(problem, (first, hold, curtail, curtail, *tail))
    validate_modes(problem, (first, curtail, curtail, curtail, *tail))
    validate_modes(problem, (first, hold, hold, curtail, *tail))
    # HOLD is free again at the commitment's deadline, and not before it.
    validate_modes(problem, (first, hold, hold, *(first,) * 5))
    # It does not go back to the commitment before its end.
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (first, hold, first, hold, *tail))
    # CURTAIL needs a pack seen full: a pack with room keeps the commitment.
    room = replace(problem, battery=replace(problem.battery, initial_kwh=5))
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(
            room,
            (
                Flow(0, 0, 0.5, 0, 0, 5.5, "charge", "CHARGE_PV"),
                *(replace(curtail, end_soc_kwh=5.5),) * 7,
            ),
        )


def discharged_pack_problem():
    # The pack fills in slot 0, covers the house in slot 1 and has room again for
    # the surplus of slots 2 and 3, the rest of the commitment.
    rows = ((1.0, 0.9, 0.5, 0.1), (1.0, 0.9, 0.0, 0.6), *((1.0, 0.9, 0.6, 0.1),) * 6)
    problem = dispatch(rows, minimum_mode_minutes=60)
    return replace(
        problem,
        battery=replace(problem.battery, initial_kwh=9.7),
        initial_dispatch_mode="CHARGE_PV",
        initial_dispatch_mode_since=problem.slots[0].start,
    )


def test_a_pack_that_was_full_and_has_room_again_keeps_the_commitment():
    # CURTAIL is what a full pack does, not a way out of the minimum duration:
    # with nothing to curtail and room in the pack the commitment goes on.
    plan = solve(discharged_pack_problem())
    assert [flow.dispatch_mode for flow in plan.flows[:4]] == [
        "CHARGE_PV",
        "SELF_CONSUME",
        "CHARGE_PV",
        "CHARGE_PV",
    ]
    assert all(flow.curtail_kwh == pytest.approx(0) for flow in plan.flows[:4])


def test_validator_rejects_curtail_once_the_full_pack_has_room_again():
    problem = discharged_pack_problem()
    first = Flow(0, 0.1, 0.3, 0, 0, 10, "charge", "CHARGE_PV")
    follow = Flow(0, 0, 0, 0.6, 0, 9.4, "discharge", "SELF_CONSUME")
    curtail = Flow(0, 0.4, 0, 0, 0.1, 9.4, None, "CURTAIL")
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (first, follow, *(curtail,) * 6))
    # The interval that starts at the ceiling may curtail, and goes on curtailing.
    full = replace(curtail, end_soc_kwh=10)
    surplus = full_pack_problem((0.5, *(0.6,) * 7), initial=9.7, lead=0)
    validate_modes(surplus, (first, *(full,) * 7))


def test_hold_run_started_in_an_idle_committed_interval_has_no_clock():
    # The spent pack ends the commitment in the idle first interval. The HOLD run
    # that starts there is the released HOLD: it must not be held for its own
    # minimum duration into the intervals whose PV is above the 8 kW inverter.
    rows = ((1.0, 0.1, 0.0, 0.0), (1.0, 0.1, 0.02, 0.1), *((1.0, 0.1, 2.2, 0.1),) * 4)
    problem = dispatch(rows, minimum_mode_minutes=60)
    problem = replace(
        problem,
        battery=replace(problem.battery, initial_kwh=0.0),
        initial_dispatch_mode="CHARGE_PV",
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=45),
    )
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == [
        "HOLD",
        "HOLD",
        *("CURTAIL",) * 4,
    ]
    assert all(flow.curtail_kwh == pytest.approx(0.2) for flow in plan.flows[2:])
