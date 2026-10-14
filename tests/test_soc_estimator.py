from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.soc_estimator import (
    Anchor,
    EstimatorSettings,
    Inputs,
    counter_energy,
    initial_state,
    set_anchor,
    step,
)

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
SETTINGS = EstimatorSettings(capacity_kwh=24.0, empty_voltage_v=480.0, eta_charge=0.9)


def _state(energy=10.0, charge=1000.0, discharge=500.0, kind="full", confirmed=False):
    state = initial_state(SETTINGS, T0, charge, discharge, energy / 24)
    state.last_at = None
    set_anchor(
        state,
        Anchor(kind, T0, energy, charge, discharge),
        top_confirmed=confirmed,
    )
    return state


def _at(seconds):
    return T0 + timedelta(seconds=seconds)


def _step(state, seconds, charge, discharge, power=0.0):
    return step(state, SETTINGS, Inputs(_at(seconds), charge, discharge, power))


def test_energy_follows_counters_from_the_anchor():
    state = _state()
    estimate = _step(state, 60, 1002.0, 501.0)
    assert estimate.energy_kwh == pytest.approx(10 - 1 + 0.9 * 2)
    assert estimate.soc_fraction == pytest.approx(estimate.energy_kwh / 24)
    assert estimate.available
    assert estimate.anchor_kind == "full"
    assert estimate.throughput_since_anchor_kwh == pytest.approx(1.5)


def test_settings_reject_nonsense():
    with pytest.raises(InputError):
        EstimatorSettings(capacity_kwh=0, empty_voltage_v=480.0)
    with pytest.raises(InputError):
        EstimatorSettings(capacity_kwh=24, empty_voltage_v=480.0, eta_charge=1.2)
    with pytest.raises(InputError):
        EstimatorSettings(capacity_kwh=float("nan"), empty_voltage_v=480.0)
    with pytest.raises(InputError):
        EstimatorSettings(capacity_kwh=24, empty_voltage_v=0)


def test_interpolation_integrates_discharge_power_between_counter_steps():
    state = _state()
    _step(state, 0, 1000.0, 500.0, 7200)
    estimate = _step(state, 60, 1000.0, 500.0, 7200)
    assert estimate.pending_kwh == pytest.approx(-0.12)
    assert estimate.energy_kwh == pytest.approx(10 - 0.12)


def test_interpolation_uses_charge_efficiency_for_charging_power():
    state = _state()
    _step(state, 0, 1000.0, 500.0, -7200)
    estimate = _step(state, 60, 1000.0, 500.0, -7200)
    assert estimate.pending_kwh == pytest.approx(0.12 * 0.9)


def test_interpolation_ignores_the_part_of_a_gap_beyond_two_minutes():
    state = _state()
    _step(state, 0, 1000.0, 500.0, 3600)
    estimate = _step(state, 900, 1000.0, 500.0, 3600)
    assert estimate.pending_kwh == pytest.approx(-0.12)


def test_interpolation_is_bounded():
    state = _state()
    for second in range(0, 7200, 60):
        estimate = _step(state, second, 1000.0, 500.0, 40000)
    assert estimate.pending_kwh == -2.0


def test_pending_restarts_when_a_counter_changes():
    state = _state()
    _step(state, 0, 1000.0, 500.0, 7200)
    _step(state, 60, 1000.0, 500.0, 7200)
    estimate = _step(state, 120, 1000.0, 501.0, 7200)
    assert estimate.pending_kwh == 0.0
    assert estimate.energy_kwh == pytest.approx(9.0)


def test_missing_power_adds_nothing():
    state = _state()
    _step(state, 0, 1000.0, 500.0, None)
    estimate = _step(state, 60, 1000.0, 500.0, None)
    assert estimate.pending_kwh == 0.0


def test_unavailable_counters_give_no_estimate_and_resume_later():
    state = _state()
    estimate = _step(state, 60, None, 500.0)
    assert not estimate.available
    assert estimate.energy_kwh is None
    assert _step(state, 120, 1001.0, 500.0).energy_kwh == pytest.approx(10.9)


def test_a_reading_older_than_the_last_is_ignored():
    state = _state()
    _step(state, 120, 1001.0, 500.0)
    estimate = _step(state, 60, 1500.0, 500.0)
    assert estimate.energy_kwh == pytest.approx(10.9)
    assert state.last_counters == (1001.0, 500.0)


def test_counter_reset_re_anchors_at_the_current_energy():
    state = _state()
    _step(state, 3600, 1004.0, 500.0)
    estimate = _step(state, 3660, 3.0, 500.0)
    assert estimate.events == ("reset",)
    assert estimate.anchor_kind == "reset"
    assert estimate.energy_kwh == pytest.approx(10 + 0.9 * 4)
    assert state.anchor.charge_kwh == 3.0
    after = _step(state, 3720, 5.0, 500.0)
    assert after.energy_kwh == pytest.approx(10 + 0.9 * 4 + 0.9 * 2)
    assert after.events == ()


def test_reset_keeps_a_normal_step_of_the_other_counter():
    state = _state()
    estimate = _step(state, 60, 3.0, 502.0)
    assert estimate.energy_kwh == pytest.approx(8.0)


def test_reset_clamps_the_re_anchored_energy():
    state = _state(energy=1.0)
    _step(state, 3600, 1000.0, 504.0)
    estimate = _step(state, 3660, 3.0, 504.0)
    assert estimate.energy_kwh == 0.0
    assert state.anchor.energy_kwh == 0.0


def test_a_rise_faster_than_any_battery_is_a_glitch():
    state = _state()
    _step(state, 60, 1001.0, 500.0)
    glitch = _step(state, 120, 6000.0, 500.0)
    assert glitch.energy_kwh == pytest.approx(10.9)
    assert glitch.events == ()
    recovered = _step(state, 180, 1002.0, 500.0)
    assert recovered.energy_kwh == pytest.approx(11.8)
    assert recovered.anchor_kind == "full"


def test_a_glitch_value_that_persists_is_a_re_base():
    state = _state()
    _step(state, 60, 1001.0, 500.0)
    _step(state, 120, 6000.0, 500.0)
    rebased = _step(state, 180, 6000.0, 500.0)
    assert rebased.events == ("reset",)
    assert rebased.energy_kwh == pytest.approx(10.9)
    assert _step(state, 240, 6001.0, 500.0).energy_kwh == pytest.approx(11.8)


def test_rise_limit_scales_with_the_time_since_the_last_reading():
    state = _state()
    estimate = _step(state, 3600, 1050.0, 500.0)
    assert estimate.events == ()
    assert estimate.unanchored_top


def test_the_top_without_a_full_anchor_shows_just_below_full():
    state = _state(energy=22.0, kind="initial")
    estimate = _step(state, 3600, 1010.0, 500.0)
    assert estimate.unanchored_top
    assert estimate.energy_kwh == pytest.approx(24 * 0.999)
    assert estimate.soc_fraction == pytest.approx(0.999)
    assert estimate.anchor_kind == "initial"
    assert state.anchor.energy_kwh == 22.0


def test_clamping_at_the_top_creates_no_anchor_and_keeps_counting():
    state = _state(energy=22.0, kind="initial")
    _step(state, 3600, 1010.0, 500.0)
    back = _step(state, 7200, 1010.0, 503.0)
    assert state.anchor.kind == "initial"
    assert counter_energy(state, SETTINGS, 1010.0, 503.0) == pytest.approx(28.0)
    assert back.unanchored_top
    away = _step(state, 10800, 1010.0, 509.0)
    assert not away.unanchored_top
    assert away.energy_kwh == pytest.approx(22.0)


def test_a_full_anchor_allows_one_hundred_percent_until_the_pack_is_left():
    state = _state(energy=24.0, confirmed=True)
    top = _step(state, 60, 1001.0, 500.0)
    assert not top.unanchored_top
    assert top.energy_kwh == 24.0
    assert top.soc_fraction == 1.0
    left = _step(state, 3720, 1001.0, 503.0)
    assert left.energy_kwh < 24.0
    assert not left.unanchored_top
    again = _step(state, 7320, 1010.0, 503.0)
    assert again.unanchored_top
    assert again.soc_fraction == pytest.approx(0.999)


def test_energy_above_capacity_after_a_confirmed_full_is_losses_not_storage():
    state = _state(energy=24.0, confirmed=True)
    top = _step(state, 60, 1002.0, 500.0)
    assert top.energy_kwh == 24.0
    assert top.excess_kwh == 0.0
    assert top.top_confirmed
    assert top.anchor_kind == "full"
    assert top.anchor_at == T0
    assert state.anchor.energy_kwh == 24.0
    assert state.anchor.charge_kwh == 1002.0
    left = _step(state, 120, 1002.0, 501.0)
    assert left.energy_kwh == pytest.approx(23.0)
    assert left.excess_kwh == 0.0


def test_the_last_full_follows_the_refreshed_top():
    state = _state(energy=24.0, confirmed=True)
    state.last_full = state.anchor
    _step(state, 60, 1002.0, 500.0)
    assert state.last_full is state.anchor
    assert state.last_full.charge_kwh == 1002.0


def test_excess_without_a_confirmation_is_kept():
    state = _state(energy=24.0, confirmed=False)
    top = _step(state, 60, 1002.0, 500.0)
    assert top.unanchored_top
    assert top.excess_kwh == pytest.approx(1.8)
    assert state.anchor.charge_kwh == 1000.0


def test_clamping_at_the_bottom_creates_no_anchor():
    state = _state(energy=2.0)
    estimate = _step(state, 3600, 1000.0, 510.0)
    assert estimate.energy_kwh == 0.0
    assert estimate.anchor_kind == "full"
    assert not estimate.unanchored_top


def test_initial_state_starts_unanchored_from_the_stated_soc():
    state = initial_state(SETTINGS, T0, 7.0, 3.0, 0.5)
    assert state.anchor.kind == "initial"
    assert state.anchor.energy_kwh == 12.0
    assert step(state, SETTINGS, Inputs(_at(60), 7.0, 3.0)).energy_kwh == 12.0
    assert initial_state(SETTINGS, T0, 7.0, 3.0, 4.0).anchor.energy_kwh == 24.0
