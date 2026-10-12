"""SELF_CONSUME follows the load: no grid import beside a spare discharge power."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_dispatch_policy import dispatch

from custom_components.energy_compass.engine.dispatch_policy import validate_modes
from custom_components.energy_compass.engine.machine_state import machine_state
from custom_components.energy_compass.engine.models import (
    Flow,
    FlexibleLoadRequest,
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
        hours = (slot.end - slot.start).total_seconds() / 3600
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
