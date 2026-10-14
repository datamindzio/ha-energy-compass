"""Margin kept after a battery-export slot until the next cheap slot (T-093)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine.models import (
    Battery,
    Problem,
    SiteLimits,
    Slot,
)
from custom_components.energy_compass.engine.optimize import solve
from custom_components.energy_compass.engine.peak_reserve import (
    export_reserve_targets,
    export_reserve_weight,
)

_BUY = (1.24,) * 7 + (0.6, 0.6)
_SELL = (1.0, 1.1, 1.17, 1.05, 0.5, 0.5, 0.5, 0.5, 0.5)
_LOAD = (0.0,) * 4 + (0.5, 0.5, 0.5, 0.0, 0.0)
_RESERVE = 1.0


def _targets(margin, **changes):
    return export_reserve_targets(
        _BUY,
        _SELL,
        _LOAD,
        (0.0,) * 9,
        cheap_price=0.61,
        reserve_kwh=_RESERVE,
        usable_capacity_kwh=10.0,
        eta_discharge=1.0,
        margin_kwh=margin,
        **changes,
    )


def _problem(margin, minutes=0):
    start = datetime(2026, 10, 14, 16, tzinfo=UTC)
    slots = tuple(
        Slot(start + timedelta(hours=i), start + timedelta(hours=i + 1), b, s, 0.0, ld)
        for i, (b, s, ld) in enumerate(zip(_BUY, _SELL, _LOAD, strict=True))
    )
    targets = _targets(margin)
    return Problem(
        slots,
        SiteLimits(8, 8, 8, True),
        Battery(10, 0.1, 1.0, 8.0, 5, 2, 1, 1, 0.0, True, True),
        "value",
        0.6,
        (),
        "UTC",
        minimum_mode_minutes=minutes,
        limit_export_to_pv=False,
        maximum_grid_charge_price=0.61,
        export_reserve_kwh=targets.targets_kwh,
        export_reserve_window=targets.window_ids,
        export_reserve_weight=export_reserve_weight(_BUY, targets.window_ids),
    )


def test_targets_keep_the_deficit_after_each_export_capable_slot():
    result = _targets(0.5)
    # after slot 3 the run still has 3 x 0.5 kWh of deficit; the cheap slot 7 is not counted
    assert result.targets_kwh[:4] == pytest.approx((3.0, 3.0, 3.0, 3.0))
    # slots that cannot pay (sell 0.5 x eta below the cheap price 0.61) carry no floor
    assert result.targets_kwh[4:] == (0.0,) * 5
    assert result.window_ids == (0,) * 4 + (-1,) * 5
    assert export_reserve_weight(_BUY, result.window_ids) == pytest.approx(1.24)


def test_a_slot_that_cannot_pay_for_an_export_carries_no_floor():
    sell = (0.5,) + _SELL[1:]
    result = export_reserve_targets(
        _BUY,
        sell,
        _LOAD,
        (0.0,) * 9,
        cheap_price=0.61,
        reserve_kwh=_RESERVE,
        usable_capacity_kwh=10.0,
        eta_discharge=1.0,
        margin_kwh=0.5,
    )
    assert result.window_ids[:4] == (-1, 0, 0, 0)


def test_no_floor_without_margin_a_following_cheap_slot_or_deficit():
    assert set(_targets(0.0).window_ids) == {-1}
    assert set(
        export_reserve_targets(
            _BUY[:7],
            _SELL[:7],
            _LOAD[:7],
            (0.0,) * 7,
            cheap_price=0.61,
            reserve_kwh=1.0,
            usable_capacity_kwh=10.0,
            eta_discharge=1.0,
            margin_kwh=0.5,
        ).window_ids
    ) == {-1}
    no_deficit = export_reserve_targets(
        _BUY,
        _SELL,
        (0.0,) * 9,
        (0.0,) * 9,
        cheap_price=0.61,
        reserve_kwh=1.0,
        usable_capacity_kwh=10.0,
        eta_discharge=1.0,
        margin_kwh=0.5,
    )
    assert set(no_deficit.window_ids) == {-1}
    assert export_reserve_weight(_BUY, (-1,) * 9) == 0.0


def _exported(plan):
    return [flow.discharge_kwh for flow in plan.flows[:4]]


@pytest.mark.parametrize("minutes", [0, 60])
def test_margin_keeps_the_deficit_plus_margin_and_shrinks_the_cheapest_slot(minutes):
    margin = 0.5
    plan = solve(_problem(margin, minutes))
    plain = solve(replace(_problem(0.0, minutes)))
    assert plan.peak_reserve_shortfall_kwh == pytest.approx(0.0, abs=1e-6)
    # reserve + the three deficit slots + margin stay after the last export slot
    assert plan.flows[3].end_soc_kwh >= _RESERVE + 1.5 + margin - 1e-6
    # a token export later in the run (min activity 0.1 kWh) does not lift the floor
    assert plan.flows[6].end_soc_kwh >= _RESERVE + margin - 1e-6
    # without the margin the pack is emptied to reserve + deficit (today's plan)
    assert plain.flows[3].end_soc_kwh == pytest.approx(_RESERVE + 1.5, abs=1e-6)
    saved = [a - b for a, b in zip(_exported(plain), _exported(plan), strict=True)]
    # slot 0 (1.0) is not used by either plan; slot 3 earns least of those that are
    assert saved[3] == pytest.approx(margin, abs=1e-6)
    assert saved[:3] == pytest.approx([0.0] * 3, abs=1e-6)
    assert plan.flows[3].grid_import_kwh == pytest.approx(0.0, abs=1e-6)


def test_the_dispatch_mode_stands_in_when_the_episode_hurdle_is_off():
    plan = solve(replace(_problem(0.5, 60), minimum_export_episode_benefit=0))
    assert plan.flows[3].end_soc_kwh >= _RESERVE + 1.5 + 0.5 - 1e-6
    assert plan.peak_reserve_shortfall_kwh == pytest.approx(0.0, abs=1e-6)


def test_zero_margin_reproduces_the_plan_without_the_floor():
    start = _problem(0.0)
    bare = replace(
        start,
        export_reserve_kwh=(),
        export_reserve_window=(),
        export_reserve_weight=0.0,
    )
    a, b = solve(start), solve(bare)
    assert a.flows == b.flows
    assert a.objective == pytest.approx(b.objective)


def test_floor_is_soft_a_much_dearer_export_goes_below_it():
    # selling at 4.0 beats the shortfall price (1.24) plus the import it causes (1.24)
    base = _problem(0.5)
    dear = replace(
        base,
        slots=tuple(replace(s, sell_per_kwh=4.0) for s in base.slots[:4])
        + base.slots[4:],
    )
    plan = solve(dear)
    assert plan.flows[3].end_soc_kwh < _RESERVE + 1.5 + 0.5 - 1e-3
    assert plan.peak_reserve_shortfall_kwh > 0.4
