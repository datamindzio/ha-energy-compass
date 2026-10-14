"""T-091: a HOLD at the peak price is charged the forecast miss it buys from the grid."""

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.energy_compass.engine import optimize
from custom_components.energy_compass.engine.hold_exposure import (
    HOLD_FORECAST_MISS,
    hold_exposure,
)
from custom_components.energy_compass.engine.models import (
    Battery,
    InputError,
    Problem,
    SiteLimits,
    Slot,
)
from custom_components.energy_compass.engine.optimize import solve

_ZONE = ZoneInfo("Europe/Warsaw")
_START = datetime(2026, 10, 14, 6, 15, tzinfo=UTC)  # 08:15 local


def _slots(rows, hours=0.25):
    return tuple(
        Slot(
            _START + timedelta(hours=hours * i),
            _START + timedelta(hours=hours * (i + 1)),
            buy,
            sell,
            pv,
            load,
        )
        for i, (buy, sell, pv, load) in enumerate(rows)
    )


def _battery(**changes):
    base = Battery(24, 0.1, 1.0, 11.7, 8, 8, 0.943, 0.943, 0.0, True, False)
    return replace(base, **changes)


def _problem(slots, battery=None, **changes):
    problem = Problem(
        slots,
        SiteLimits(10, 10, 10, True),
        battery or _battery(),
        "preserve_initial",
        0.0,
        (),
        "Europe/Warsaw",
    )
    return replace(problem, **changes)


def _live_day():
    """2026-10-14 from 08:15: G12 buy 1.242 until 13:00, 0.606 13-15, 1.242 evening;
    PV a little above the load in the morning, then a bell; load up in the evening."""
    rows = []
    for i in range(4 * 14):
        at = _START + timedelta(minutes=15 * i)
        hour = at.astimezone(_ZONE).hour + at.astimezone(_ZONE).minute / 60
        buy = 0.606 if 13 <= hour < 15 else 1.242
        sell = 1.08 if hour < 17 else 1.175
        pv = 0.28 if hour < 10 else max(0.0, 0.4 * math.sin(math.pi * (hour - 7) / 11))
        load = 0.19 if hour < 11 else (0.25 if hour < 17 else 0.42)
        rows.append((buy, sell, pv, load))
    return _slots(rows)


def _modes(problem):
    plan = solve(problem)
    return {
        slot.start.astimezone(_ZONE).strftime("%H:%M"): flow.dispatch_mode
        for slot, flow in zip(problem.slots, plan.flows, strict=True)
    }, plan


def _local(label):
    hour, minute = label.split(":")
    return int(hour) * 60 + int(minute)


def test_exposure_is_off_until_a_problem_carries_the_share():
    assert not any(hold_exposure(_problem(_live_day())))


def test_exposure_charges_a_peak_interval_by_the_miss_the_pack_could_have_covered():
    problem = _problem(_live_day(), hold_miss_share=HOLD_FORECAST_MISS)
    exposure = hold_exposure(problem)
    # 08:15: worth = refill at 13:00, 0.606 / eta_charge; the later use (evening import
    # avoided, eta_discharge x 1.242) is higher and does not bind.
    worth = 0.606 / 0.943
    assert exposure[0] == pytest.approx(HOLD_FORECAST_MISS * 0.19 * (1.242 - worth))
    # The cheap interval itself and the intervals after the last later use: none.
    cheap = next(i for i, s in enumerate(problem.slots) if s.buy_per_kwh == 0.606)
    assert exposure[cheap] == 0.0
    assert exposure[-1] == 0.0


def test_exposure_is_only_on_intervals_with_pv_above_the_load():
    problem = _problem(_live_day(), hold_miss_share=HOLD_FORECAST_MISS)
    exposure = hold_exposure(problem)
    for slot, charged in zip(problem.slots, exposure, strict=True):
        if slot.pv_kwh <= slot.load_kwh:
            assert charged == 0.0
    assert any(charged > 0 for charged in exposure)


def test_exposure_is_the_later_use_when_the_pack_has_no_room_to_refill():
    full = _battery(initial_kwh=24)
    problem = _problem(_live_day(), full, hold_miss_share=0.5)
    exposure = hold_exposure(problem)
    # Worth is the evening import avoided: eta_discharge x 1.242, so the peak price is
    # above it by the round trip only.
    assert exposure[0] == pytest.approx(0.5 * 0.19 * (1.242 - 0.943 * 1.242))


def test_exposure_needs_a_later_use():
    problem = _problem(
        _slots([(1.242, 0.3, 0.28, 0.19)] * 4), hold_miss_share=0.5
    )  # no deficit and no battery export later: nothing to compare with
    assert not any(hold_exposure(problem))


def test_exposure_uses_the_terminal_value_as_the_last_use():
    problem = _problem(
        _slots([(1.242, 0.3, 0.28, 0.19)] * 4),
        hold_miss_share=0.5,
        terminal_mode="value",
        terminal_value_per_kwh=0.2,
    )
    assert hold_exposure(problem)[-1] == pytest.approx(0.5 * 0.19 * (1.242 - 0.2))


def test_the_share_must_be_in_unit_range():
    with pytest.raises(InputError, match="hold_miss_share"):
        solve(_problem(_live_day(), hold_miss_share=1.5))


def test_the_live_shape_without_the_charge_holds_in_the_peak():
    """The shape the plan had on 2026-10-14: the model with no charge HOLDs the PV surplus
    through the morning peak to export it."""
    modes, _ = _modes(_problem(_live_day()))
    peak = [m for t, m in modes.items() if _local(t) < 12 * 60]
    assert peak.count("HOLD") >= 8


def test_the_live_shape_follows_pv_in_the_peak_instead_of_holding():
    problem = _problem(_live_day(), hold_miss_share=HOLD_FORECAST_MISS)
    modes, plan = _modes(problem)
    # Every interval up to the lead-in of the cheap slot at 13:00 (its minimum duration
    # puts the plan's last peak interval in the run that follows) takes the PV-follow
    # mode, which draws a miss from the pack.
    peak = {t: m for t, m in modes.items() if _local(t) < 12 * 60 + 30}
    assert "HOLD" not in peak.values()
    assert set(peak.values()) <= {"CHARGE_PV", "SELF_CONSUME"}
    # The pack still ends where it started: the preserve rule is untouched.
    assert plan.flows[-1].end_soc_kwh == pytest.approx(11.7, abs=1e-6)


def test_a_pack_with_no_room_is_charged_the_round_trip_loss_only():
    """Nothing refills a full pack at the cheap slot, so a kWh kept is worth its later
    use at the evening peak: HOLD is charged the round trip, not the refill gap, and the
    plan stays close to the unpriced one."""
    full = _battery(initial_kwh=24)
    priced = _problem(_live_day(), full, hold_miss_share=HOLD_FORECAST_MISS)
    unpriced = _problem(_live_day(), full)
    _, priced_plan = _modes(priced)
    _, unpriced_plan = _modes(unpriced)
    assert (
        hold_exposure(priced)[0]
        < 0.15 * hold_exposure(replace(priced, battery=_battery()))[0]
    )
    assert priced_plan.objective == pytest.approx(unpriced_plan.objective, abs=0.15)


def _weighted_objective(problem):
    """The solver's own objective of the plan, charge included (`Plan.objective` has none)."""
    original = optimize._Model.solve
    captured = {}

    def spy(self, time_limit):
        values = original(self, time_limit)
        captured["weighted"] = float(np.dot(self.cost, values))
        return values

    optimize._Model.solve = spy
    try:
        plan = solve(problem)
    finally:
        optimize._Model.solve = original
    return captured["weighted"], plan


def test_a_hold_in_a_pack_that_cannot_take_the_charge_is_not_charged():
    """A pack at its ceiling cannot take what a PV-follow charge would store, so a HOLD
    there is not a choice and carries no charge: the solver's objective with the share is
    the one without it, though `hold_exposure` prices the intervals (T-094)."""
    full = _battery(initial_kwh=24)
    priced = _problem(_live_day(), full, hold_miss_share=HOLD_FORECAST_MISS)
    assert max(hold_exposure(priced)) > 0
    with_share, _ = _weighted_objective(priced)
    without, _ = _weighted_objective(replace(priced, hold_miss_share=0.0))
    assert with_share == pytest.approx(without, abs=1e-6)


def test_the_charge_stays_on_a_pack_with_room_to_take_it():
    """The same day from a pack with room: the HOLD is a choice and the objective carries
    the share of the miss it would have bought."""
    room = _problem(_live_day(), _battery(), hold_miss_share=HOLD_FORECAST_MISS)
    with_share, _ = _weighted_objective(room)
    without, _ = _weighted_objective(replace(room, hold_miss_share=0.0))
    assert with_share > without + 0.01


def test_a_pack_with_room_for_the_minimum_charge_keeps_the_charge():
    """Between the minimum activity and the full PV-follow step the pack still takes a
    CHARGE_PV (it ends on the ceiling), so a HOLD there is a choice and the charge stays
    in the objective (T-095)."""
    # 0.05 kWh of room at 08:15, a surplus of 0.09 kWh: its step is 0.085, the minimum
    # activity (0.1 kW for 15 minutes) stores 0.0236.
    band = _problem(
        _live_day(), _battery(initial_kwh=23.95), hold_miss_share=HOLD_FORECAST_MISS
    )
    with_share, _ = _weighted_objective(band)
    without, _ = _weighted_objective(replace(band, hold_miss_share=0.0))
    assert with_share > without + 0.01


def test_a_pack_with_less_room_than_the_minimum_charge_is_not_charged():
    """The minimum PV-follow charge no longer fits: CHARGE_PV is closed, so the HOLD is
    no choice and the charge is given back, as for a full pack."""
    tight = _problem(
        _live_day(), _battery(initial_kwh=23.99), hold_miss_share=HOLD_FORECAST_MISS
    )
    with_share, _ = _weighted_objective(tight)
    without, _ = _weighted_objective(replace(tight, hold_miss_share=0.0))
    assert with_share == pytest.approx(without, abs=1e-6)
