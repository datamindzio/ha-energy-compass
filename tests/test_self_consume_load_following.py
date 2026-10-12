"""SELF_CONSUME follows the load: no grid import beside a spare discharge power."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

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


def flat_night(slots=28, *, initial=3.0, load=0.5, lock=True, **changes):
    """Flat buy price, a battery that must come back full, SELF_CONSUME carried in."""
    problem = dispatch(
        tuple((1.0, 0.0, 0, load) for _ in range(slots)),
        minimum_mode_minutes=60,
        terminal_mode="preserve_initial",
        **changes,
    )
    problem = replace(problem, battery=replace(problem.battery, initial_kwh=initial))
    if lock:
        problem = replace(
            problem,
            initial_dispatch_mode="SELF_CONSUME",
            initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=15),
        )
    return problem


def assert_load_following(problem, plan):
    battery = problem.battery
    reserve = battery.capacity_kwh * battery.minimum_soc_fraction
    for slot, flow in zip(problem.slots, plan.flows, strict=True):
        hours = (
            slot.end.astimezone(UTC) - slot.start.astimezone(UTC)
        ).total_seconds() / 3600
        if machine_state(slot, flow) != "SELF_CONSUME" or flow.grid_import_kwh < 1e-5:
            continue
        limit = min(
            battery.discharge_kw * hours, problem.site.inverter_kw * hours - slot.pv_kwh
        )
        assert (
            flow.discharge_kwh >= limit - 1e-5 or flow.end_soc_kwh <= reserve + 1e-4
        ), (slot.start, flow)


def test_carried_self_consume_covers_the_house_instead_of_trickling():
    # The previous model satisfied the carried lock with 0.025 kWh of discharge
    # and 0.475 kWh of import per slot, which the inverter cannot execute.
    problem = flat_night(slots=16)
    plan = solve(problem)
    rows = list(zip(problem.slots, plan.flows, strict=True))
    locked = [
        flow
        for slot, flow in rows
        if slot.start < rows[0][0].start + timedelta(minutes=45)
    ]
    assert [machine_state(s, f) for s, f in rows[:3]] == ["SELF_CONSUME"] * 3
    for flow in locked:
        assert flow.discharge_kwh == pytest.approx(0.5)
        assert flow.grid_import_kwh == pytest.approx(0)
    assert_load_following(problem, plan)


def test_flat_night_has_no_trickle_rows():
    problem = flat_night(slots=28)
    plan = solve(problem)
    for slot, flow in zip(problem.slots, plan.flows, strict=True):
        mode = machine_state(slot, flow)
        if mode == "SELF_CONSUME":
            assert flow.grid_import_kwh == pytest.approx(0)
        elif mode == "HOLD":
            assert flow.discharge_kwh == pytest.approx(0)
            assert flow.grid_import_kwh == pytest.approx(slot.load_kwh)
        assert not 0 < flow.discharge_kwh < 0.1 or flow.grid_import_kwh < 1e-6
    assert_load_following(problem, plan)


def test_import_is_allowed_once_discharge_is_at_its_power_limit():
    # House draws 2 kWh per slot, the battery gives at most 1 kWh.
    problem = dispatch(
        ((3, 0, 0, 2),) * 4,
        minimum_mode_minutes=60,
        initial_dispatch_mode="SELF_CONSUME",
        initial_dispatch_mode_since=None,
    )
    problem = replace(
        problem,
        battery=replace(problem.battery, initial_kwh=8, discharge_kw=4),
        slots=tuple(
            replace(slot, end=slot.start + timedelta(minutes=15))
            for slot in problem.slots
        ),
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=30),
    )
    plan = solve(problem)
    for flow in plan.flows[:2]:
        assert flow.dispatch_mode == "SELF_CONSUME"
        assert flow.discharge_kwh == pytest.approx(1)
        assert flow.grid_import_kwh == pytest.approx(1)


def test_import_is_allowed_when_the_battery_is_emptied_to_its_reserve():
    # 1.5 kWh above the reserve cannot cover a 2 kWh hour: the inverter drains
    # it and imports the rest.
    problem = flat_night(slots=4, initial=2.5, load=2.0)
    problem = replace(
        problem,
        minimum_mode_minutes=60,
        battery=replace(problem.battery, discharge_kw=8, minimum_soc_fraction=0.1),
        terminal_mode="value",
        terminal_value_per_kwh=0,
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=45),
    )
    plan = solve(problem)
    assert_load_following(problem, plan)
    assert plan.flows[0].dispatch_mode == "SELF_CONSUME"


def test_validator_rejects_partial_discharge_with_import():
    problem = dispatch(((1, 0, 0, 5),) * 2, minutes=60, minimum_mode_minutes=60)
    problem = replace(problem, battery=replace(problem.battery, initial_kwh=8))

    def rows(import_kwh, discharge_kwh):
        return tuple(
            Flow(
                import_kwh,
                0,
                0,
                discharge_kwh,
                0,
                8 - discharge_kwh * (i + 1),
                "discharge",
                "SELF_CONSUME",
            )
            for i in range(2)
        )

    # The 4 kW battery gives at most 4 kWh an hour: 0.5 kWh beside import is partial.
    with pytest.raises(SolveError, match="self-consume imports"):
        validate_modes(problem, rows(4.5, 0.5))
    with pytest.raises(SolveError, match="self-consume imports"):
        validate_modes(problem, rows(2, 3))
    validate_modes(problem, rows(1, 4))
    validate_modes(
        replace(problem, battery=replace(problem.battery, discharge_kw=8)),
        rows(0, 5),
    )


def test_flexible_load_probe_stays_feasible_in_self_consume():
    problem = flat_night(slots=12)
    plan = solve_flexible_load(problem, FlexibleLoadRequest(1.0, 2.0))
    assert sum(plan.schedule_kwh) == pytest.approx(1.0)
    slots = tuple(
        replace(slot, load_kwh=slot.load_kwh + extra)
        for slot, extra in zip(problem.slots, plan.schedule_kwh, strict=True)
    )
    assert_load_following(replace(problem, slots=slots), plan.plan)


def surplus_hours(**battery):
    """Two hours with 0.5 kWh of PV surplus; a 2 kW flexible load tips each over."""
    problem = dispatch(((1.0, 0.0, 1.0, 0.5),) * 2, minutes=60, minimum_mode_minutes=60)
    return replace(problem, battery=replace(problem.battery, initial_kwh=8, **battery))


def assert_flexible_follows_net_load(problem, plan):
    for flow in plan.plan.flows:
        # Load 0.5 + flexible 2 - PV 1: the battery covers the whole 1.5 kWh.
        assert flow.dispatch_mode == "SELF_CONSUME"
        assert flow.discharge_kwh == pytest.approx(1.5)
        assert flow.grid_import_kwh == pytest.approx(0)
    assert plan.schedule_kwh == pytest.approx((2.0, 2.0))


def test_flexible_load_above_pv_surplus_is_covered_by_the_battery():
    problem = surplus_hours()
    plan = solve_flexible_load(problem, FlexibleLoadRequest(4.0, 2.0))
    assert_flexible_follows_net_load(problem, plan)


def test_flexible_load_above_pv_surplus_with_a_carried_lock():
    problem = surplus_hours()
    problem = replace(
        problem,
        initial_dispatch_mode="SELF_CONSUME",
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=15),
    )
    plan = solve_flexible_load(problem, FlexibleLoadRequest(4.0, 2.0))
    assert_flexible_follows_net_load(problem, plan)


def test_flexible_load_above_pv_surplus_without_battery_export_or_grid_charge():
    problem = surplus_hours(allow_battery_export=False, allow_grid_charge=False)
    plan = solve_flexible_load(problem, FlexibleLoadRequest(4.0, 2.0))
    assert_flexible_follows_net_load(problem, plan)


def test_carried_lock_without_a_way_to_refill_has_no_plan():
    # A carried PV-follow commitment makes the inverter drain the battery for
    # the house. With no PV and no grid charge the preserve_initial terminal
    # cannot be met, so the answer is "infeasible" rather than a trickle plan
    # the inverter would not follow.
    problem = flat_night(slots=8)
    problem = replace(
        problem, battery=replace(problem.battery, allow_grid_charge=False)
    )
    with pytest.raises(SolveError) as caught:
        solve(problem)
    assert caught.value.reason == "infeasible"
    # Once the commitment has run out the same problem is plannable again.
    expired = replace(
        problem,
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(hours=2),
    )
    plan = solve(expired)
    assert [flow.dispatch_mode for flow in plan.flows] == ["HOLD"] * 8


def evening(initial, *, slots=32, **battery):
    """A carried SELF_CONSUME lock with 45 minutes left and a nearly empty battery."""
    problem = dispatch(
        (((1.2, 0, 0, 0.125),) * 8 + ((0.6, 0, 0, 0.125),) * 24)[:slots],
        minimum_mode_minutes=60,
        terminal_value_per_kwh=0.6,
    )
    return replace(
        problem,
        battery=replace(problem.battery, initial_kwh=initial, **battery),
        initial_dispatch_mode="SELF_CONSUME",
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=15),
    )


def assert_lock_ends_with_the_battery(problem, flows, emptied_in):
    # The inverter covers the house until the pack is empty, then imports: HOLD.
    modes = [flow.dispatch_mode for flow in flows]
    assert modes[: emptied_in + 1] == ["SELF_CONSUME"] * (emptied_in + 1)
    assert modes[emptied_in + 1 : emptied_in + 5] == ["HOLD"] * 4
    for flow in flows[:emptied_in]:
        assert flow.grid_import_kwh == pytest.approx(0)
    assert flows[emptied_in].end_soc_kwh == pytest.approx(0)
    for slot, flow in zip(problem.slots[emptied_in + 1 : 3], flows[emptied_in + 1 : 3]):
        assert flow.discharge_kwh == pytest.approx(0)
        assert flow.grid_import_kwh >= slot.load_kwh - 1e-6


@pytest.mark.parametrize(("initial", "emptied_in"), [(0.1, 0), (0.2, 1)])
def test_carried_lock_is_released_once_the_battery_is_emptied(initial, emptied_in):
    # Without the release the slot after the emptied one still had to discharge.
    problem = evening(initial)
    plan = solve(problem)
    assert_lock_ends_with_the_battery(problem, plan.flows, emptied_in)
    assert_load_following(problem, plan)


@pytest.mark.parametrize(("initial", "emptied_in"), [(0.1, 0), (0.2, 1)])
def test_flexible_load_under_a_lock_the_battery_cannot_finish(initial, emptied_in):
    problem = evening(initial)
    plan = solve_flexible_load(problem, FlexibleLoadRequest(1.0, 2.0))
    assert sum(plan.schedule_kwh) == pytest.approx(1.0)
    # The flexible load goes to the cheap slots, outside the lock.
    assert plan.schedule_kwh[:3] == pytest.approx((0, 0, 0))
    assert_lock_ends_with_the_battery(problem, plan.plan.flows, emptied_in)
    slots = tuple(
        replace(slot, load_kwh=slot.load_kwh + extra)
        for slot, extra in zip(problem.slots, plan.schedule_kwh, strict=True)
    )
    assert_load_following(replace(problem, slots=slots), plan.plan)


def test_validator_releases_a_carried_lock_only_after_an_emptied_battery():
    problem = evening(0.2, slots=6)

    def rows(first_discharge, initial=0.2):
        left = initial - first_discharge
        return (
            Flow(0, 0, 0, first_discharge, 0, left, "discharge", "SELF_CONSUME"),
            Flow(0.125 - left, 0, 0, left, 0, 0, "discharge", "SELF_CONSUME"),
            *(Flow(0.125, 0, 0, 0, 0, 0, "discharge", "HOLD") for _ in range(4)),
        )

    validate_modes(problem, rows(0.125))
    # The same rows from a fuller pack leave energy behind: the lock still holds.
    fuller = rows(0.125, initial=0.3)
    fuller = (
        fuller[0],
        replace(fuller[1], grid_import_kwh=0, discharge_kwh=0.125, end_soc_kwh=0.05),
        *(replace(flow, end_soc_kwh=0.05) for flow in fuller[2:]),
    )
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, fuller)
    # Nothing but HOLD may follow the emptied slot inside the lock.
    charging = rows(0.125)
    charging = (
        *charging[:2],
        *(Flow(1, 0, 0.875, 0, 0, 0, "charge", "CHARGE_GRID") for _ in range(4)),
    )
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, charging)


def test_standby_loss_owed_by_an_empty_reserve_keeps_the_lock_infeasible():
    # Known limit: with a 0 % reserve the pack must keep the standby loss of the
    # later slots, so the discharge can neither reach its power limit nor end on
    # the reserve, and a carried lock has no plan until it runs out.
    problem = dispatch(((1.0, 0, 0, 0.5),) * 3, minutes=30, minimum_mode_minutes=60)
    problem = replace(
        problem,
        battery=replace(
            problem.battery,
            initial_kwh=0.3,
            discharge_kw=0.5,
            allow_grid_charge=False,
            allow_battery_export=False,
            idle_drain_kw=0.05,
        ),
        initial_dispatch_mode="CHARGE_PV",
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=15),
    )
    with pytest.raises(SolveError) as caught:
        solve(problem)
    assert caught.value.reason == "infeasible"
    expired = replace(
        problem,
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(hours=2),
    )
    assert solve(expired).flows


def staggered_prices(**changes):
    """1.2 kWh of battery, 0.5 kWh an hour of load, dearer power last."""
    problem = dispatch(
        ((1, 0, 0, 0.5), (1, 0, 0, 0.5), (2, 0, 0, 0.5)),
        minutes=60,
        minimum_mode_minutes=0,
        terminal_mode="value",
        terminal_value_per_kwh=0,
        **changes,
    )
    return replace(problem, battery=replace(problem.battery, initial_kwh=1.2))


def test_load_following_holds_without_a_minimum_mode_duration():
    # Without the rule the cheap hour got 0.2 kWh of discharge and 0.3 kWh of import.
    problem = staggered_prices()
    plan = solve(problem)
    assert_load_following(problem, plan)
    assert plan.flows[2].discharge_kwh == pytest.approx(0.5)
    assert sum(f.discharge_kwh for f in plan.flows) == pytest.approx(1.0)
    for slot, flow in zip(problem.slots, plan.flows, strict=True):
        if 0 < flow.discharge_kwh < slot.load_kwh - 1e-6:
            assert flow.grid_import_kwh == pytest.approx(0)


def test_load_following_without_a_duration_allows_import_at_the_power_limit():
    problem = dispatch(((3, 0, 0, 2),) * 4, minimum_mode_minutes=0)
    problem = replace(
        problem,
        battery=replace(problem.battery, initial_kwh=8, discharge_kw=4),
        slots=tuple(
            replace(slot, end=slot.start + timedelta(minutes=15))
            for slot in problem.slots
        ),
    )
    plan = solve(problem)
    assert_load_following(problem, plan)
    assert plan.flows[0].discharge_kwh == pytest.approx(1)
    assert plan.flows[0].grid_import_kwh == pytest.approx(1)


def test_load_following_without_a_duration_allows_import_once_emptied():
    problem = staggered_prices()
    problem = replace(
        problem,
        slots=tuple(replace(slot, load_kwh=2.0) for slot in problem.slots),
        battery=replace(
            problem.battery,
            discharge_kw=8,
            minimum_soc_fraction=0.1,
            initial_kwh=2.5,
            allow_grid_charge=False,
        ),
    )
    plan = solve(problem)
    assert_load_following(problem, plan)
    last = plan.flows[-1]
    assert last.end_soc_kwh == pytest.approx(1.0)
    assert last.grid_import_kwh == pytest.approx(0.5)


def test_flexible_load_probe_follows_the_load_without_a_duration():
    problem = staggered_prices()
    problem = replace(problem, battery=replace(problem.battery, initial_kwh=3.0))
    plan = solve_flexible_load(problem, FlexibleLoadRequest(1.0, 2.0))
    assert sum(plan.schedule_kwh) == pytest.approx(1.0)
    slots = tuple(
        replace(slot, load_kwh=slot.load_kwh + extra)
        for slot, extra in zip(problem.slots, plan.schedule_kwh, strict=True)
    )
    assert_load_following(replace(problem, slots=slots), plan.plan)


def test_validator_rejects_partial_discharge_with_import_without_a_duration():
    problem = dispatch(((1, 0, 0, 5),) * 2, minutes=60, minimum_mode_minutes=0)
    problem = replace(problem, battery=replace(problem.battery, initial_kwh=8))

    def rows(import_kwh, discharge_kwh):
        return tuple(
            Flow(
                import_kwh,
                0,
                0,
                discharge_kwh,
                0,
                8 - discharge_kwh * (i + 1),
                "discharge",
                None,
            )
            for i in range(2)
        )

    with pytest.raises(SolveError, match="self-consume imports"):
        validate_modes(problem, rows(4.5, 0.5))
    validate_modes(problem, rows(1, 4))


@pytest.mark.parametrize("minimum_mode_minutes", [0, 60])
def test_load_following_counts_local_slots_across_spring_forward_in_utc(
    minimum_mode_minutes,
):
    # Aware datetimes sharing one tzinfo subtract as wall clock: the slot from
    # 01:00 CET to 03:00 CEST read as 2 h and its power limit as 8 kWh, not 4.
    zone = ZoneInfo("Europe/Warsaw")
    first = datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
    problem = dispatch(
        ((3, 0, 0, 6),) * 4, minutes=60, minimum_mode_minutes=minimum_mode_minutes
    )
    slots = tuple(
        replace(
            slot,
            start=(first + timedelta(hours=i)).astimezone(zone),
            end=(first + timedelta(hours=i + 1)).astimezone(zone),
        )
        for i, slot in enumerate(problem.slots)
    )
    problem = replace(
        problem,
        slots=slots,
        timezone="Europe/Warsaw",
        battery=replace(problem.battery, initial_kwh=10, discharge_kw=4),
    )
    plan = solve(problem)
    assert_load_following(problem, plan)
    # The second slot is the 01:00 CET - 03:00 CEST one: 4 kWh at the power limit.
    for flow in plan.flows[:2]:
        assert flow.discharge_kwh == pytest.approx(4)
        assert flow.grid_import_kwh == pytest.approx(2)
    assert sum(f.discharge_kwh for f in plan.flows) == pytest.approx(10)
