"""Net-billing settlement factors in the planning objective (ADR-0001)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import sqrt

import pytest

from custom_components.energy_compass.engine.models import (
    Battery,
    InputError,
    Problem,
    SiteLimits,
    Slot,
)
from custom_components.energy_compass.engine.normalize import validate_problem
from custom_components.energy_compass.engine.optimize import solve
from custom_components.energy_compass.engine.settlement import (
    auto_pressure,
    factors,
    hold_pressure,
    manual_pressure,
    planning_buy,
    planning_sell,
)

START = datetime(2026, 10, 12, tzinfo=UTC)
# PGE G12 2026, gross (ADR-0001 worked example): day, night.
DAY_BUY, DAY_ENERGY = 1.2418, 0.7018
NIGHT_BUY, NIGHT_ENERGY = 0.6065, 0.4635
RHO = 0.30
ETA = sqrt(0.89)


def slots(rows):
    return tuple(
        Slot(
            START + timedelta(hours=index),
            START + timedelta(hours=index + 1),
            buy,
            sell,
            pv,
            load,
            energy,
        )
        for index, (buy, sell, pv, load, energy) in enumerate(rows)
    )


def problem(rows, **changes) -> Problem:
    battery = Battery(10, 0.2, 0.8, 5, 4, 4, ETA, ETA, 0.05, True, True)
    return Problem(
        slots(rows),
        SiteLimits(8, 8, 8, True),
        battery,
        "preserve_initial",
        0.0,
        (),
        "UTC",
        minimum_mode_minutes=0,
        minimum_export_episode_benefit=0,
        limit_export_to_pv=False,
        **changes,
    )


def cap_binding(rho=RHO) -> dict:
    export_factor, energy_factor = factors(1.0, rho)
    return {"export_value_factor": export_factor, "import_energy_factor": energy_factor}


def sale_and_rebuy(sell: float, **changes) -> Problem:
    """Sell from the battery by day, buy the energy back at night."""
    return problem(
        [
            (DAY_BUY, sell, 0, 0, DAY_ENERGY),
            (NIGHT_BUY, 0.1, 0, 0, NIGHT_ENERGY),
        ],
        **changes,
    )


def sells(sell: float, **changes) -> bool:
    plan = solve(sale_and_rebuy(sell, **changes))
    return plan.flows[0].grid_export_kwh > 1e-6


def threshold(**changes) -> float:
    """Lowest sell price at which the battery sale pays (bisection)."""
    low, high = 0.0, 3.0
    for _ in range(14):
        middle = (low + high) / 2
        if sells(middle, **changes):
            high = middle
        else:
            low = middle
    return high


def test_planning_prices_follow_the_adr_formulas():
    slot = Slot(START, START + timedelta(hours=1), 1.2418, 0.40, 0, 0, 0.7018)
    capped = problem([(1.2418, 0.40, 0, 0, 0.7018)], **cap_binding())
    assert planning_buy(capped, slot) == pytest.approx(0.540)
    assert planning_sell(capped, slot) == pytest.approx(0.12)
    short = problem([(1.2418, 0.40, 0, 0, 0.7018)])
    assert planning_buy(short, slot) == slot.buy_per_kwh
    assert planning_sell(short, slot) == slot.sell_per_kwh


def test_a_negative_sell_price_is_a_real_charge_and_stays_whole():
    slot = Slot(START, START + timedelta(hours=1), 1.0, -0.2, 0, 0, 0.5)
    assert (
        planning_sell(problem([(1.0, -0.2, 0, 0, 0.5)], **cap_binding()), slot) == -0.2
    )


def test_the_energy_part_never_exceeds_a_nonpositive_buy_price():
    slot = Slot(START, START + timedelta(hours=1), -0.1, 0.0, 0, 0, 0.5)
    capped = problem([(-0.1, 0.0, 0, 0, 0.5)], **cap_binding())
    assert planning_buy(capped, slot) == -0.1


def test_factors_and_pressure_for_the_adr_worked_example():
    assert auto_pressure(2214, 2150, RHO)[0] == 0
    pressure, unused = auto_pressure(2800, 2150, RHO)
    assert unused == pytest.approx(0.232, abs=1e-3)
    assert pressure == pytest.approx(0.55, abs=0.01)
    export_factor, energy_factor = factors(pressure, RHO)
    assert export_factor == pytest.approx(0.62, abs=0.01)
    assert energy_factor == pytest.approx(0.45, abs=0.01)
    assert auto_pressure(3300, 2150, RHO)[0] == 1
    assert factors(1.0, RHO) == (pytest.approx(0.30), 0.0)


def test_degenerate_refund_caps():
    assert auto_pressure(3000, 1000, 1.0)[0] == 0
    assert auto_pressure(3000, 1000, 0.0)[0] == 1
    assert auto_pressure(1000, 1000, 0.0)[0] == 0
    assert manual_pressure(0.3, 1.0) == 0
    assert manual_pressure(0.3, RHO) == 1
    assert manual_pressure(0.65, RHO) == pytest.approx(0.5)
    assert manual_pressure(1.0, RHO) == 0


def test_dead_band_holds_small_moves_and_passes_the_extremes():
    assert hold_pressure(None, 0.42) == 0.42
    assert hold_pressure(0.40, 0.45) == 0.40
    assert hold_pressure(0.40, 0.50) == 0.50
    assert hold_pressure(0.05, 0.0) == 0.0
    assert hold_pressure(0.95, 1.0) == 1.0
    assert hold_pressure(0.5, 0.5) == 0.5


def test_default_factors_keep_the_plan_byte_identical():
    rows = [
        (0.6, 0.3, 0, 1.0, 0.0),
        (0.3, 0.5, 2.0, 0.4, 0.0),
        (1.2, 1.1, 0.5, 2.0, 0.0),
        (0.4, -0.05, 3.0, 0.2, 0.0),
    ]
    baseline = solve(problem(rows))
    with_split = solve(
        problem(
            [(buy, sell, pv, load, buy * 0.6) for buy, sell, pv, load, _ in rows],
            export_value_factor=1.0,
            import_energy_factor=1.0,
        )
    )
    assert with_split.flows == baseline.flows
    assert with_split.objective == baseline.objective
    assert with_split.grid_cost == baseline.grid_cost


def test_short_deposit_state_plans_like_invoice_prices():
    # Short or within the cap: π = 0, so both factors are exactly 1.
    export_factor, energy_factor = factors(0.0, RHO)
    assert (export_factor, energy_factor) == (1.0, 1.0)
    reference = solve(sale_and_rebuy(0.9))
    short = solve(
        sale_and_rebuy(
            0.9, export_value_factor=export_factor, import_energy_factor=energy_factor
        )
    )
    assert short.flows == reference.flows


def test_cap_binding_moves_the_battery_export_threshold_as_in_the_adr():
    short = threshold()
    capped = threshold(**cap_binding())
    # ADR worked example: 0.73 invoice break-even, 0.71 once the cap binds.
    assert short == pytest.approx(0.73, abs=0.02)
    assert capped == pytest.approx(0.71, abs=0.02)
    assert capped < short
    # A sale between the two thresholds pays only when the cap binds.
    between = (short + capped) / 2
    assert not sells(between)
    assert sells(between, **cap_binding())


def test_surplus_pv_is_worth_less_when_the_cap_binds():
    # 1 kWh of PV surplus at 0.30 sell, night load 1 kWh at the night price.
    rows = [
        (DAY_BUY, 0.30, 1.0, 0.0, DAY_ENERGY),
        (NIGHT_BUY, 0.0, 0.0, 1.0, NIGHT_ENERGY),
    ]
    invoice = solve(problem(rows))
    capped = solve(problem(rows, **cap_binding()))
    assert invoice.flows[0].charge_kwh > invoice.flows[0].grid_export_kwh
    assert capped.flows[0].grid_export_kwh > capped.flows[0].charge_kwh
    # Reported cost stays at invoice prices in both plans.
    assert capped.grid_cost == pytest.approx(
        sum(
            slot.buy_per_kwh * flow.grid_import_kwh
            - slot.sell_per_kwh * flow.grid_export_kwh
            for slot, flow in zip(slots(rows), capped.flows, strict=True)
        )
    )


def test_validator_bounds_the_factors_and_the_energy_part():
    rows = [(1.0, 0.2, 0, 0.5, 0.4)]
    validate_problem(problem(rows))
    for name in ("export_value_factor", "import_energy_factor"):
        for bad in (-0.01, 1.01, float("nan")):
            with pytest.raises(InputError):
                validate_problem(problem(rows, **{name: bad}))
    with pytest.raises(InputError):
        validate_problem(problem([(1.0, 0.2, 0, 0.5, 1.1)]))
    with pytest.raises(InputError):
        validate_problem(problem([(1.0, 0.2, 0, 0.5, -0.1)]))
    with pytest.raises(InputError):
        validate_problem(replace(problem(rows), import_energy_factor=2))
