"""SELF_CONSUME follows the load: no grid import beside a spare discharge power."""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_dispatch_policy import dispatch

from custom_components.energy_compass.engine.dispatch_policy import validate_modes
from custom_components.energy_compass.engine.machine_state import machine_state
from custom_components.energy_compass.engine.models import (
    Battery,
    FlexibleLoadRequest,
    Flow,
    Problem,
    SiteLimits,
    Slot,
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


@pytest.mark.parametrize("limit", [10, 25, 40])
def test_validator_accepts_the_discharge_a_follow_row_leaves_short_by_its_noise(limit):
    """T-049: `bd >= M * (x_SELF_CONSUME - empty)` is met within 1e-6 * (2M + 1), M the limit.

    The shortfall is the most that row allows, above the fixed 1e-5 kWh the check used before.
    """
    problem = dispatch(((1, 0, 0, 99),), minutes=60, minimum_mode_minutes=60)
    problem = replace(
        problem,
        site=replace(problem.site, inverter_kw=4 * limit, grid_import_kw=4 * limit),
        battery=replace(
            problem.battery,
            capacity_kwh=10 * limit,
            initial_kwh=10 * limit,
            charge_kw=limit,
            discharge_kw=limit,
        ),
    )
    short = 0.99e-6 * (2 * limit + 1)
    assert short > 1e-5
    discharge = limit - short
    flow = Flow(
        99 - discharge,
        0,
        0,
        discharge,
        0,
        10 * limit - discharge,
        "discharge",
        "SELF_CONSUME",
    )

    validate_modes(problem, (flow,))

    # Twice the noise a row of this M leaves is a real partial discharge.
    short = 1e-6 * (2 * 99 + 1) * 2
    flow = replace(
        flow, grid_import_kwh=99 - (limit - short), discharge_kwh=limit - short
    )
    with pytest.raises(SolveError, match="self-consume imports"):
        validate_modes(problem, (flow,))


def test_validator_widens_the_discharge_noise_to_the_flexible_rows():
    """T-054: the flexible row's M is `load - PV + flex_max`, with three binaries.

    Load 1 kWh, PV 0, a flexible load of at most 26 kWh that drew nothing, limit 25 kWh:
    the row (M = 27) leaves up to 1e-6 * (3 * 27 + 1) = 8.2e-5 kWh of the realised load
    uncovered, above the 5.1e-5 kWh (2M + 1 with M the limit) the realised load alone gives.
    """
    problem = dispatch(((1, 0, 0, 1),), minutes=60, minimum_mode_minutes=60)
    problem = replace(
        problem,
        site=replace(problem.site, inverter_kw=100, grid_import_kw=100),
        battery=replace(
            problem.battery,
            capacity_kwh=250,
            initial_kwh=250,
            charge_kw=25,
            discharge_kw=25,
        ),
    )
    short = 0.99e-6 * (3 * 27 + 1)
    assert short > 1e-6 * (2 * 25 + 1)

    def flow(short):
        return Flow(short, 0, 0, 1 - short, 0, 249 + short, "discharge", "SELF_CONSUME")

    validate_modes(problem, (flow(short),), flex_max=(26.0,))
    with pytest.raises(SolveError, match="self-consume imports"):
        validate_modes(problem, (flow(short),))
    # Twice the noise of the three binaries is a real partial discharge.
    with pytest.raises(SolveError, match="self-consume imports"):
        validate_modes(problem, (flow(2 * 1e-6 * (3 * 27 + 1)),), flex_max=(26.0,))


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


def test_carried_lock_without_a_way_to_refill_releases_to_hold():
    # A carried PV-follow commitment makes the inverter drain the battery for
    # the house. With no PV and no grid charge the preserve_initial terminal
    # cannot be met after that (T-065: this had no plan, "infeasible", until the
    # commitment ran out), so the interval is a HOLD released from the pack, as
    # it is once the commitment has run out, not a trickle plan the inverter
    # would not follow.
    problem = flat_night(slots=8)
    problem = replace(
        problem, battery=replace(problem.battery, allow_grid_charge=False)
    )
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == ["HOLD"] * 8
    assert all(flow.discharge_kwh == pytest.approx(0) for flow in plan.flows)
    assert plan.flows[-1].end_soc_kwh >= problem.battery.initial_kwh - 1e-6
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


def zero_reserve(initial):
    """A carried PV-follow lock over two of three dark slots, 0 % reserve, standby loss."""
    problem = dispatch(((1.0, 0, 0, 0.5),) * 3, minutes=30, minimum_mode_minutes=60)
    return replace(
        problem,
        battery=replace(
            problem.battery,
            initial_kwh=initial,
            discharge_kw=0.5,
            allow_grid_charge=False,
            allow_battery_export=False,
            idle_drain_kw=0.05,
        ),
        initial_dispatch_mode="CHARGE_PV",
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=15),
    )


@pytest.mark.parametrize(
    ("initial", "modes"),
    [
        # Discharging at the power limit would take the standby loss of the later slots.
        (0.3, ["HOLD", "HOLD", "HOLD"]),
        # One slot at the limit fits, a second one does not.
        (0.4, ["SELF_CONSUME", "HOLD", "HOLD"]),
        (0.6, ["SELF_CONSUME", "SELF_CONSUME", "HOLD"]),
    ],
)
def test_standby_loss_owed_by_an_empty_reserve_releases_the_lock(initial, modes):
    # With a 0 % reserve the pack cannot end on the reserve: it has to keep the
    # standby loss of the later slots, so the lock ends where the house would take it.
    problem = zero_reserve(initial)
    plan = solve(problem)
    assert [flow.dispatch_mode for flow in plan.flows] == modes
    assert min(flow.end_soc_kwh for flow in plan.flows) >= 0
    assert_load_following(problem, plan)


def winter_night(slots, *, drain=0.03):
    """A carried SELF_CONSUME lock 0.15 kWh above a 10 % reserve, nothing to recharge from."""
    problem = dispatch(
        ((1.0, 0, 0, 0.5),) + ((1.0, 0, 0, 0.05),) * (slots - 1),
        minimum_mode_minutes=30,
        terminal_value_per_kwh=0.3,
    )
    return replace(
        problem,
        battery=replace(
            problem.battery,
            capacity_kwh=5,
            minimum_soc_fraction=0.1,
            initial_kwh=0.65,
            charge_kw=1,
            discharge_kw=1,
            eta_charge=0.95,
            eta_discharge=0.95,
            allow_grid_charge=False,
            allow_battery_export=False,
            idle_drain_kw=drain,
        ),
        initial_dispatch_mode="SELF_CONSUME",
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=15),
    )


def test_carried_lock_ends_at_once_when_later_standby_loss_needs_the_battery():
    # 80 slots lose 0.6 kWh on standby, more than the 0.5 kWh reserve: covering
    # the house down to the reserve would leave the later loss unpaid.
    problem = winter_night(80)
    plan = solve(problem)
    assert {flow.dispatch_mode for flow in plan.flows[:4]} == {"HOLD"}
    assert plan.flows[0].discharge_kwh == pytest.approx(0)
    assert plan.flows[0].grid_import_kwh == pytest.approx(0.5)
    assert min(flow.end_soc_kwh for flow in plan.flows) >= 0


@pytest.mark.parametrize(("slots", "drain"), [(40, 0.03), (80, 0.0)])
def test_carried_lock_holds_while_the_reserve_covers_the_standby_loss(slots, drain):
    problem = winter_night(slots, drain=drain)
    plan = solve(problem)
    assert plan.flows[0].dispatch_mode == "SELF_CONSUME"
    # The house empties the pack to its reserve inside the committed slot.
    assert plan.flows[0].end_soc_kwh == pytest.approx(0.5)
    assert plan.flows[0].grid_import_kwh > 0.3
    assert_load_following(problem, plan)


def test_validator_releases_a_first_slot_only_for_a_pack_that_cannot_go_on():
    problem = winter_night(80)
    held = solve(problem).flows
    validate_modes(problem, held)
    # Without the standby loss the same pack can cover the house: the lock holds.
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(winter_night(80, drain=0.0), held)


def test_carried_lock_never_lacks_a_plan_without_a_way_to_recharge():
    rng = random.Random(19)
    for _ in range(40):
        problem = winter_night(rng.choice((8, 24, 80)), drain=rng.choice((0.03, 0.13)))
        reserve = rng.choice((0.0, 0.1, 0.2))
        problem = replace(
            problem,
            slots=tuple(
                replace(slot, load_kwh=rng.choice((0.05, 0.2, 0.5)))
                for slot in problem.slots
            ),
            battery=replace(
                problem.battery,
                minimum_soc_fraction=reserve,
                # From just above the reserve (at it the safety exception applies) to full.
                initial_kwh=5 * reserve + rng.choice((0.002, 0.02, 0.15, 0.6, 2.5)),
                discharge_kw=rng.choice((0.5, 2)),
            ),
            minimum_mode_minutes=rng.choice((30, 60)),
        )
        # solve() certifies its own rows with validate_modes.
        plan = solve(problem)
        assert_load_following(problem, plan)


def test_first_slot_with_pv_surplus_keeps_a_spent_pack_on_the_lock():
    # With surplus to charge from the inverter still follows PV, however well the
    # export would pay: a pack too empty for SELF_CONSUME is no reason for HOLD.
    problem = evening(0.01)
    problem = replace(
        problem,
        slots=(
            replace(problem.slots[0], sell_per_kwh=5.0, pv_kwh=1.0),
            *problem.slots[1:],
        ),
    )
    plan = solve(problem)
    assert plan.flows[0].dispatch_mode == "CHARGE_PV"
    exported = Flow(0, 0.875, 0, 0, 0, 0.01, None, "HOLD")
    resting = tuple(
        Flow(0.125, 0, 0, 0, 0, 0.01, None, "HOLD") for _ in problem.slots[1:]
    )
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (exported, *resting))
    # Without the surplus the same pack ends the lock in its first slot.
    dark = evening(0.01)
    assert solve(dark).flows[0].dispatch_mode == "HOLD"
    validate_modes(dark, (resting[0], *resting))


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


QUARTER = timedelta(minutes=15)
DUSK = datetime(2026, 12, 18, 19, tzinfo=UTC)


def committed(rows, *, initial_mode="SELF_CONSUME", **changes):
    """A carried PV-follow commitment with 45 minutes left, rows are (PV, load) kWh."""
    slots = tuple(
        Slot(DUSK + i * QUARTER, DUSK + (i + 1) * QUARTER, 1.0, 0.0, pv, load)
        for i, (pv, load) in enumerate(rows)
    )
    problem = Problem(
        slots,
        SiteLimits(8, 8, 8, True),
        Battery(5, 0.1, 1, 3.0, 1, 1, 0.95, 0.95, 0.01, False, False, 0.0),
        "value",
        0.3,
        (),
        "UTC",
        minimum_mode_minutes=60,
        initial_dispatch_mode=initial_mode,
        initial_dispatch_mode_since=DUSK - QUARTER,
    )
    return replace(problem, **changes)


def modes(plan):
    return [flow.dispatch_mode for flow in plan.flows]


@pytest.mark.parametrize(
    "rows",
    (
        [(0.0, 0.02)] * 8,  # night base load under the minimum activity
        [(0.2, 0.2)] + [(0.0, 0.2)] * 7,  # PV equals the load at dusk
        [(0.0, 0.2)] * 8,  # the committed intervals have a load to follow
    ),
    ids=("low load", "pv equals load", "normal"),
)
@pytest.mark.parametrize("initial_mode", ("SELF_CONSUME", "CHARGE_PV"))
def test_carried_commitment_has_a_plan_when_no_pv_follow_mode_is_open(
    rows, initial_mode
):
    # solve() certifies its own rows with validate_modes.
    assert solve(committed(rows, initial_mode=initial_mode)).flows


def test_an_interval_without_a_pv_follow_mode_does_not_start_a_hold_clock():
    # Dusk: PV equals the load for 15 minutes, then the house draws 0.2 kWh again.
    # HOLD in the first interval is by classification, not a mode change: the next
    # committed intervals still have to follow the load.
    plan = solve(committed([(0.2, 0.2)] + [(0.0, 0.2)] * 7))
    assert modes(plan)[:3] == ["HOLD", "SELF_CONSUME", "SELF_CONSUME"]
    for flow in plan.flows[1:3]:
        assert flow.discharge_kwh == pytest.approx(0.2)


def test_a_low_load_night_stays_idle_through_the_commitment():
    plan = solve(committed([(0.0, 0.02)] * 8))
    assert modes(plan)[:3] == ["HOLD"] * 3
    assert all(flow.discharge_kwh == 0 for flow in plan.flows[:3])


def test_validator_accepts_an_idle_interval_in_the_commitment_only_when_idle():
    problem = committed([(0.2, 0.2)] + [(0.0, 0.2)] * 7)
    hold = Flow(0, 0, 0, 0, 0, 3.0, None, "HOLD")
    import_hold = Flow(0.2, 0, 0, 0, 0, 3.0, None, "HOLD")
    follow = Flow(0, 0, 0, 0.2, 0, 3.0 - 0.2 / 0.95, "discharge", "SELF_CONSUME")
    rest = (import_hold,) * 5
    # The idle interval may be HOLD and the commitment goes on after it ...
    validate_modes(problem, (hold, follow, follow, *rest))
    # ... but HOLD in a committed interval with a load to follow is leaving it.
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (hold, import_hold, follow, *rest))


def test_hold_may_not_start_on_a_chargeable_surplus_inside_a_commitment():
    # Interval 0 takes the pack down to its reserve, which releases HOLD. Interval 1
    # is still committed and has surplus the pack can take: that is CHARGE_PV, not
    # a HOLD that would waste it.
    rows = [(0.0, 0.5), (0.1, 0.05), (0.0, 0.2), (0.0, 0.2)]
    problem = committed(
        rows,
        minimum_mode_minutes=30,
        initial_dispatch_mode_since=DUSK - timedelta(minutes=1),
    )
    problem = replace(
        problem,
        battery=replace(problem.battery, minimum_soc_fraction=0.2, initial_kwh=1.05),
    )
    plan = solve(problem)
    assert modes(plan)[:2] == ["SELF_CONSUME", "CHARGE_PV"]
    validate_modes(problem, plan.flows)
    # HOLD started in interval 1 is rejected, HOLD that went on from interval 0 is not.
    sc = Flow(0, 0, 0, 0.05, 0, 1.0, "discharge", "SELF_CONSUME")
    start = Flow(0, 0.05, 0, 0, 0, 1.0, None, "HOLD")
    rest = (Flow(0.2, 0, 0, 0, 0, 1.0, None, "HOLD"),) * 2
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (sc, start, *rest))


def test_idle_slots_are_the_committed_intervals_with_nothing_to_follow():
    from custom_components.energy_compass.engine.dispatch_policy import idle_slots

    problem = committed([(0.2, 0.2), (0.0, 0.2), (0.0, 0.02), (0.0, 0.02), (0.0, 0.02)])
    # The commitment ends at 19:45: the third interval is the last one inside it.
    assert idle_slots(problem) == {0, 2}
    assert idle_slots(replace(problem, initial_dispatch_mode="HOLD")) == set()
    assert idle_slots(replace(problem, initial_dispatch_mode=None)) == set()
    assert idle_slots(replace(problem, minimum_mode_minutes=0)) == set()


def spent_pack_problem(rows, battery, *, site=None, mode="CHARGE_PV", **changes):
    """Carried commitment, one minute in, a pack that leaves nothing to follow."""
    slots = tuple(
        Slot(DUSK + i * QUARTER, DUSK + (i + 1) * QUARTER, 1.0, 0.5, pv, load)
        for i, (pv, load) in enumerate(rows)
    )
    problem = Problem(
        slots,
        site or SiteLimits(8, 8, 8, True),
        battery,
        "value",
        0.3,
        (),
        "UTC",
        minimum_mode_minutes=30,
        initial_dispatch_mode=mode,
        initial_dispatch_mode_since=DUSK - timedelta(minutes=1),
    )
    return replace(problem, **changes)


def test_a_spent_pack_in_an_idle_first_interval_ends_the_commitment_before_the_surplus():
    # The pack ends the commitment in HOLD and the CURTAIL interval follows at the
    # deadline, on a zero-export site that could not hold through the surplus. The
    # released HOLD starts no clock of its own (ADR-0005), so the idle interval may
    # be the HOLD run or a bridge before it: both read the same flows as valid.
    problem = spent_pack_problem(
        [(0.024, 0.025), (0.0, 0.2), (0.1, 0.05)],
        Battery(5, 0, 1, 0.02, 0.5, 3, 0.95, 0.95, 0, False, True, 0.0),
        site=SiteLimits(8, 8, 0, True),
    )
    plan = solve(problem)
    assert modes(plan) == ["HOLD", "HOLD", "CURTAIL"]
    validate_modes(problem, plan.flows, plan.carried_bridges)
    validate_modes(problem, plan.flows, ())
    validate_modes(problem, plan.flows, {0})


def test_a_spent_pack_in_an_idle_first_interval_keeps_a_terminal_rule_feasible():
    problem = spent_pack_problem(
        [(0.025, 0.025), (0.05, 0.02), (0.025, 0.02), (0.0, 0.2), (0.02, 0.024)]
        + [(0.024, 0.5)],
        Battery(5, 0.1, 1, 0.52, 3, 1, 0.95, 0.95, 0, False, True, 0.0),
        mode="SELF_CONSUME",
        terminal_mode="preserve_initial",
        minimum_mode_minutes=120,
    )
    plan = solve(problem)
    assert modes(plan) == ["HOLD"] * 6
    assert plan.carried_bridges == ()


def test_a_legacy_direction_guard_does_not_cost_the_idle_first_interval_its_release():
    # CHARGE_PV is blocked by the guard, so the idle interval is the pack's only way
    # out of the commitment.
    problem = spent_pack_problem(
        [(0.025, 0.025), (0.05, 0.02), (0.025, 0.02), (0.0, 0.2), (0.02, 0.024)]
        + [(0.024, 0.5)],
        Battery(5, 0.1, 1, 0.52, 3, 1, 0.95, 0.95, 0, False, True, 0.0),
        mode="SELF_CONSUME",
        terminal_mode="preserve_initial",
        minimum_mode_minutes=120,
        initial_battery_mode="discharge",
        initial_battery_mode_since=DUSK - QUARTER,
    )
    assert modes(solve(problem)) == ["HOLD"] * 6


def test_an_idle_interval_after_a_depleting_one_may_start_the_hold_run():
    # The pack is spent after the first interval; the next one is idle. The HOLD run
    # may start there with its clock, not only in the interval after it.
    problem = spent_pack_problem(
        [(0.0, 0.5), (0.0, 0.02), (0.1, 0.05), (0.0, 0.2)],
        Battery(5, 0.1, 1, 0.55, 3, 3, 0.95, 0.95, 0, False, False, 0.0),
        mode="SELF_CONSUME",
        minimum_mode_minutes=45,
        site=SiteLimits(8, 8, 0, True),
    )
    plan = solve(problem)
    assert modes(plan)[:2] == ["SELF_CONSUME", "HOLD"]
    assert modes(plan)[2:] != ["HOLD", "HOLD"] or plan.carried_bridges == (1,)
    validate_modes(problem, plan.flows, plan.carried_bridges)


def test_a_flexible_load_probe_agrees_with_its_own_validator():
    # The model reads a row as a bridge only when it is idle as realised, which is
    # what the validator sees; the rows it reads as bridges are the ones it hands over.
    rows = [(0.01, 0.0), (0.5, 0.1), (0.02, 0.02), (0.03, 0.01), (0.02, 0.05)]
    problem = spent_pack_problem(
        rows,
        Battery(5, 0.1, 1, 0.505, 3, 3, 0.95, 0.95, 0, False, False, 0.05),
        site=SiteLimits(8, 8, 8, False),
        minimum_mode_minutes=60,
        initial_dispatch_mode_since=DUSK,
    )
    result = solve_flexible_load(problem, FlexibleLoadRequest(0.3, 0.5))
    # The first row is a bridge, so the commitment goes on into the surplus after it.
    assert modes(result.plan) == [
        "HOLD",
        "CHARGE_PV",
        "SELF_CONSUME",
        "HOLD",
        "SELF_CONSUME",
    ]
    assert result.plan.carried_bridges == (0, 3)
    validate_modes(
        replace(
            problem,
            slots=tuple(
                replace(slot, load_kwh=slot.load_kwh + extra)
                for slot, extra in zip(problem.slots, result.schedule_kwh, strict=True)
            ),
        ),
        result.plan.flows,
        result.plan.carried_bridges,
    )


def test_validator_takes_only_hold_as_a_bridge():
    problem = committed([(0.2, 0.2)] + [(0.0, 0.2)] * 7)
    export = Flow(0, 0.2, 0, 0.2, 0, 3.0 - 0.2 / 0.95, "discharge", "DISCHARGE_GRID")
    follow = Flow(0, 0, 0, 0.2, 0, 2.5, "discharge", "SELF_CONSUME")
    hold = Flow(0.2, 0, 0, 0, 0, 2.5, None, "HOLD")
    rows = (follow, follow, *(hold,) * 5)
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (export, *rows))
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (export, *rows), {0})
    # A bridge is a row of the committed interval it names, nothing else.
    quiet = Flow(0, 0, 0, 0, 0, 3.0, None, "HOLD")
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, (quiet, quiet, follow, *(hold,) * 5), {0, 1})


def test_a_hold_release_after_a_bridge_needs_a_release():
    problem = committed([(0.0, 0.02), (0.0, 0.2), (0.0, 0.2), (0.0, 0.2), (0.0, 0.2)])
    problem = replace(
        problem,
        minimum_mode_minutes=30,
        initial_dispatch_mode_since=DUSK - timedelta(minutes=1),
    )

    def rows(kwh):
        return (
            Flow(0, 0, 0, 0, 0, kwh, None, "HOLD"),
            *(Flow(0.2, 0, 0, 0, 0, kwh, None, "HOLD"),) * 4,
        )

    # The pack is spent, so HOLD may start in the second interval, after the bridge.
    spent = replace(problem, battery=replace(problem.battery, initial_kwh=0.52))
    validate_modes(spent, rows(0.52), {0})
    # A bridge is no HOLD run: with a pack that can go on, the second interval is
    # leaving the commitment.
    full = replace(problem, battery=replace(problem.battery, initial_kwh=3.0))
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(full, rows(3.0), {0})


def test_a_plan_names_the_rows_that_bridge_the_carried_commitment():
    plan = solve(committed([(0.0, 0.02)] * 3 + [(0.0, 0.2)] * 5))
    assert modes(plan)[:3] == ["HOLD"] * 3
    assert plan.carried_bridges == (0, 1, 2)
    # After the commitment (45 minutes) nothing is a bridge any more.
    assert 3 not in plan.carried_bridges


def test_the_runtime_publishes_the_bridge_rows_of_the_plan():
    from datetime import UTC, datetime

    from test_export_benefit import native_config

    from custom_components.energy_compass.runtime import compute

    config = native_config()
    config["settings"].update(minimum_mode_minutes=60)
    now = datetime(2026, 9, 18, 1, tzinfo=UTC)
    states = {"sensor.soc": {"state": "50", "attributes": {}, "last_updated": now}}
    commitment = {
        "mode": "SELF_CONSUME",
        "since": (now - timedelta(minutes=15)).isoformat(),
    }
    result = compute(config, states, now, battery_commitment=commitment)
    assert result["intervals"][0]["dispatch_mode"] == "HOLD"
    # No load and no PV: nothing to follow in the committed hour.
    assert result["dispatch_policy"]["carried_idle_starts"] == [
        result["intervals"][0]["start"]
    ]
    assert (
        result["intervals"][1]["start"]
        not in (result["dispatch_policy"]["carried_idle_starts"])
    )


def test_a_hold_run_after_a_bridge_row_starts_where_the_bridge_ends():
    from custom_components.energy_compass.engine.dispatch_policy import mode_run_start

    rows = [
        {"start": f"t{i}", "dispatch_mode": mode}
        for i, mode in enumerate(("SELF_CONSUME", "HOLD", "HOLD", "HOLD", "CURTAIL"))
    ]
    assert mode_run_start(rows, 3) == 1
    assert mode_run_start(rows, 3, ("t1",)) == 2
    assert mode_run_start(rows, 2, ("t1",)) == 2
    assert mode_run_start(rows, 1, ("t1",)) == 1
    assert mode_run_start(rows, 4, ("t1",)) == 4


def test_a_bridge_row_keeps_the_carried_commitment_and_its_clock():
    from types import SimpleNamespace

    from custom_components.energy_compass.coordinator import EnergyCompassCoordinator

    commitment = {"mode": "SELF_CONSUME", "since": "2026-12-18T18:45:00+00:00"}
    coordinator = SimpleNamespace(
        _battery_commitment=dict(commitment),
        _dispatch_store=SimpleNamespace(async_delay_save=lambda *_: None),
    )
    result = {
        "intervals": [{"dispatch_mode": "HOLD"}],
        "dispatch_policy": {"enabled": True},
        "generated_at": "2026-12-18T19:00:00+00:00",
    }
    EnergyCompassCoordinator._commit_battery_direction(
        coordinator, result, carried_idle=True
    )
    assert coordinator._battery_commitment == commitment
    EnergyCompassCoordinator._commit_battery_direction(coordinator, result)
    assert coordinator._battery_commitment == {
        "mode": "HOLD",
        "since": "2026-12-18T19:00:00+00:00",
    }
