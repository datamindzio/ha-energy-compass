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


def full_pack_problem(pv, *, initial, export_cap=None, **site):
    problem = dispatch(
        tuple((1.0, 0.9, value, 0.1) for value in pv), minimum_mode_minutes=60
    )
    if export_cap is not None:
        site["grid_export_kw"] = export_cap
    return replace(
        problem,
        site=replace(problem.site, **site),
        battery=replace(problem.battery, initial_kwh=initial),
        initial_dispatch_mode="CHARGE_PV",
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=15),
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
