import pytest

from custom_components.energy_compass.engine.peak_reserve import (
    PeakReserveResult,
    peak_reserve_targets,
    peak_reserve_weight,
)


def _targets(buy, load, pv, *, margin=0.5, cheap=0.61, reserve=2.5, cap=25.0, eta=1.0):
    return peak_reserve_targets(
        tuple(buy),
        tuple(load),
        tuple(pv),
        cheap_price=cheap,
        reserve_kwh=reserve,
        usable_capacity_kwh=cap,
        eta_discharge=eta,
        margin_kwh=margin,
    )


def test_empty_input():
    assert _targets((), (), ()) == PeakReserveResult((), ())


def test_zero_margin_returns_no_floor():
    result = _targets((0.61, 1.25, 1.25), (0, 1, 1), (0, 0, 0), margin=0.0)
    assert result == PeakReserveResult((0.0, 0.0, 0.0), (-1, -1, -1))


def test_run_with_cheap_slot_before_it():
    # cheap, expensive, expensive, cheap
    result = _targets((0.61, 1.25, 1.25, 0.61), (0.5, 1.0, 2.0, 0.5), (0, 0, 0, 0))
    # end of slot 0 must hold reserve + margin + (1 + 2); nothing inside the run
    assert result.targets_kwh == pytest.approx((6.0, 0.0, 0.0, 0.0))
    assert result.window_ids == (0, -1, -1, -1)


def test_eta_scales_the_deficit_not_the_margin():
    result = _targets((0.61, 1.25), (0.0, 0.9), (0.0, 0.0), eta=0.9)
    assert result.targets_kwh == pytest.approx((2.5 + 0.5 + 1.0, 0.0))


def test_run_already_in_progress_gets_no_floor():
    # a re-solve inside a run must not buy at the expensive price to rebuild it
    result = _targets((1.25, 1.25, 0.61), (1.0, 1.0, 1.0), (0, 0, 0))
    assert result.targets_kwh == (0.0, 0.0, 0.0)
    assert result.window_ids == (-1, -1, -1)


def test_pv_covered_run_gets_no_floor():
    result = _targets((0.61, 1.25, 1.25, 0.61), (0.3, 0.3, 0.3, 0.3), (0, 1.0, 1.0, 0))
    assert result.window_ids == (-1, -1, -1, -1)
    assert result.targets_kwh == (0.0, 0.0, 0.0, 0.0)


def test_two_runs_get_separate_windows():
    buy = (0.61, 1.25, 0.61, 1.25)
    result = _targets(buy, (0, 1.0, 0, 1.0), (0, 0, 0, 0))
    assert result.window_ids == (0, -1, 1, -1)
    assert result.targets_kwh == pytest.approx((4.0, 0.0, 4.0, 0.0))


def test_surplus_slot_inside_run_does_not_offset_deficit():
    # deficits are max(0, load - pv) per slot, like engine/autonomy.py
    result = _targets((0.61, 1.25, 1.25, 1.25), (0, 1.0, 0.0, 1.0), (0, 0, 2.0, 0))
    assert result.targets_kwh[0] == pytest.approx(2.5 + 0.5 + 2.0)


def test_target_clamped_to_usable_capacity():
    result = _targets((0.61, 1.25), (0, 30.0), (0, 0), cap=20.0)
    assert result.targets_kwh[0] == pytest.approx(20.0)


def test_flat_tariff_has_no_expensive_period():
    result = _targets((0.61, 0.61), (1.0, 1.0), (0, 0))
    assert result.window_ids == (-1, -1)


def test_price_equal_to_threshold_is_cheap():
    result = _targets((0.61 + 1e-12, 1.25), (0, 1.0), (0, 0))
    assert result.window_ids == (0, -1)


def test_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        _targets((1.25,), (1.0, 1.0), (0.0,))


def test_rejects_non_positive_eta():
    with pytest.raises(ValueError):
        _targets((1.25,), (1.0,), (0.0,), eta=0.0)


def test_weight_is_median_expensive_price_minus_threshold():
    weight = peak_reserve_weight((0.61, 1.25, 1.25, 1.10), cheap_price=0.61)
    assert weight == pytest.approx(0.64)


def test_weight_zero_without_expensive_slots():
    assert peak_reserve_weight((0.61, 0.61), cheap_price=0.61) == 0.0
