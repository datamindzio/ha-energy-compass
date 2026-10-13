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

from custom_components.energy_compass.engine import optimize
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


def test_pinned_limit_a_carried_curtail_run_needs_a_full_pack():
    # Pinned limit: a CURTAIL commitment still in its dwell cannot be kept while the pack
    # has room (the spill is not forced), and the dwell leaves no other mode. A run that
    # started from a plan began with a full pack; only a hand-set pack gets here.
    rows = ((1.0, 0.1, 1.2, 0),) * 4
    carried = {"initial_dispatch_mode": "CURTAIL", "initial_dispatch_mode_since": START}
    with pytest.raises(SolveError) as error:
        solve(problem(rows, initial=3.0, minutes=60, **carried))
    assert error.value.reason == "infeasible"
    plan = solve(problem(rows, initial=5.0, minutes=60, **carried))
    assert [flow.dispatch_mode for flow in plan.flows] == ["CURTAIL"] * 4
