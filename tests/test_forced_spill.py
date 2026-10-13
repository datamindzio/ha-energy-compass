"""A site that cannot curtail still spills what it cannot pass once the pack is full (T-041).

An export cap (or an inverter rated below the PV) with `allow_curtailment` off leaves PV
no place to go when the pack is full: the inverter throttles it by itself. The plan reads
that as CURTAIL (the Deye controller runs its HOLD profile and the inverter does the rest),
but only for that PV and only into a full pack: curtailment stays off everywhere else.
"""

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
    rows, *, initial=5.0, cap_kw=1.0, inverter_kw=8.0, minutes=0, curtail=False, **kw
) -> Problem:
    """Quarter hours of `(buy, sell, pv, load)` on a 5 kWh pack that is full at 5 kWh."""
    return Problem(
        tuple(
            Slot(START + i * QUARTER, START + (i + 1) * QUARTER, *row)
            for i, row in enumerate(rows)
        ),
        SiteLimits(inverter_kw, 8, cap_kw, curtail),
        Battery(5, 0, 1, initial, 4, 4, 1, 1, 0, False, False, 0),
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
    # The first quarter fills the pack (0.95 of the surplus), the second spills.
    rows = ((1.0, 0.1, 1.2, 0), (1.0, 0.1, 1.2, 0))
    plan = solve(problem(rows, initial=4.05))
    assert plan.flows[0].curtail_kwh == pytest.approx(0, abs=1e-6)
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
