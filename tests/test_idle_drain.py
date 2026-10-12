"""Standby loss: a parked battery must lose energy in the plan, as it does live.

The home inverter draws ~0.13 kW from the battery pack whenever it is
connected. That draw is not gated by the discharge-current limit and it does
not serve site load, so a plan that models `HOLD` as a flat SOC walks the
battery down to empty overnight and wakes up with nothing to spend.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine.idle_drain import (
    cumulative,
    drain_schedule,
)
from custom_components.energy_compass.engine.models import (
    Battery,
    InputError,
    Problem,
    SiteLimits,
    Slot,
)
from custom_components.energy_compass.engine.normalize import validate_problem
from custom_components.energy_compass.engine.optimize import solve

_START = datetime(2026, 9, 21, tzinfo=UTC)


def _problem(rows, battery, *, site=None, minutes=60):
    slots = tuple(
        Slot(
            _START + timedelta(minutes=minutes * index),
            _START + timedelta(minutes=minutes * (index + 1)),
            buy,
            sell,
            pv,
            load,
        )
        for index, (buy, sell, pv, load) in enumerate(rows)
    )
    return Problem(
        slots,
        site or SiteLimits(8, 8, 8, True),
        battery,
        "preserve_initial",
        0.0,
        (),
        "UTC",
    )


def _battery(**changes):
    return replace(
        Battery(10.0, 0.2, 0.8, 5.0, 4.0, 4.0, 1.0, 1.0, 0.0, True, True), **changes
    )


def _night(hours, battery, *, buy=1.25, sell=1.0):
    return _problem([(buy, sell, 0.0, 0.3)] * hours, battery)


def test_schedule_is_zero_when_the_setting_is_off():
    source = _night(4, _battery())
    assert drain_schedule(source) == (0.0, 0.0, 0.0, 0.0)


def test_schedule_follows_the_interval_length():
    source = _problem(
        [(1.0, 0.5, 0.0, 0.3)] * 3, _battery(idle_drain_kw=0.12), minutes=15
    )
    assert drain_schedule(source) == pytest.approx((0.03, 0.03, 0.03))


def test_schedule_stops_at_an_empty_pack():
    # 0.5 kWh of charge against 1 kW of standby draw: the leak lasts half an
    # hour of modelled time and then the pack has nothing left to lose.
    source = _night(3, _battery(initial_kwh=0.5, minimum_soc_fraction=0.0))
    source = replace(source, battery=replace(source.battery, idle_drain_kw=1.0))
    assert drain_schedule(source) == pytest.approx((0.5, 0.0, 0.0))
    assert cumulative(drain_schedule(source)) == pytest.approx((0.5, 0.5, 0.5))


def test_parked_battery_loses_energy_overnight():
    """`HOLD` at the reserve floor must not report a flat SOC."""
    # Grid-only import is cheapest, so nothing wants to charge or discharge.
    battery = _battery(initial_kwh=2.0, minimum_soc_fraction=0.2, idle_drain_kw=0.13)
    source = _night(8, battery)
    plan = solve(source)
    assert all(flow.charge_kwh == pytest.approx(0.0) for flow in plan.flows)
    assert all(flow.discharge_kwh == pytest.approx(0.0) for flow in plan.flows)
    expected = 2.0 - 8 * 0.13
    assert plan.flows[-1].end_soc_kwh == pytest.approx(expected, abs=1e-6)
    assert plan.flows[-1].end_soc_kwh < plan.flows[0].end_soc_kwh


def test_reserve_still_blocks_discharge_below_the_floor():
    """Standby loss relaxes the floor; it must not unlock planned discharge."""
    battery = _battery(initial_kwh=2.0, minimum_soc_fraction=0.2, idle_drain_kw=0.13)
    # Expensive import and a rich export price: the plan would sell the pack
    # down if the drain-relaxed floor were open to discharge.
    source = _night(8, battery, buy=2.0, sell=5.0)
    plan = solve(source)
    for flow in plan.flows:
        assert flow.discharge_kwh == pytest.approx(0.0)


def test_solver_stays_feasible_when_the_drain_exceeds_the_pack():
    battery = _battery(initial_kwh=0.4, minimum_soc_fraction=0.2, idle_drain_kw=2.0)
    plan = solve(_night(6, battery))
    assert len(plan.flows) == 6
    assert all(flow.end_soc_kwh >= -1e-6 for flow in plan.flows)


def test_arbitrage_still_charges_through_the_drain():
    """Standby loss must not stop a cheap hour from filling the pack."""
    battery = _battery(initial_kwh=5.0, minimum_soc_fraction=0.2, idle_drain_kw=0.13)
    rows = [(0.10, 0.05, 0.0, 0.3)] * 2 + [(2.50, 2.00, 0.0, 3.0)] * 2
    plan = solve(_problem(rows, battery))
    assert sum(flow.charge_kwh for flow in plan.flows[:2]) > 0
    assert sum(flow.discharge_kwh for flow in plan.flows[2:]) > 0
    assert plan.flows[1].end_soc_kwh > plan.flows[0].end_soc_kwh


def test_terminal_preserve_is_relaxed_only_by_the_modelled_drain():
    """`preserve_initial` stays feasible with no way to charge."""
    battery = _battery(
        initial_kwh=5.0,
        minimum_soc_fraction=0.2,
        idle_drain_kw=0.13,
        allow_grid_charge=False,
    )
    source = _problem(
        [(1.25, 1.0, 0.0, 0.3)] * 6, battery, site=SiteLimits(8, 8, 8, True)
    )
    plan = solve(source)
    assert plan.flows[-1].end_soc_kwh == pytest.approx(5.0 - 6 * 0.13, abs=1e-6)


def test_negative_drain_is_rejected():
    source = _night(2, _battery(idle_drain_kw=-0.1))
    with pytest.raises(InputError, match="idle drain"):
        validate_problem(source)


def test_default_keeps_the_battery_model_backward_compatible():
    assert (
        Battery(10.0, 0.2, 0.8, 5.0, 4, 4, 1, 1, 0.0, True, True).idle_drain_kw == 0.0
    )
