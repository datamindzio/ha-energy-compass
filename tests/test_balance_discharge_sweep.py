"""T-048: a chosen balance window never fails `balance discharge` on real HiGHS.

A short slice of `benchmarks.balance_discharge`; the full sweep (see its docstring) found
no failure and a largest discharge inside a chosen window of 1.3e-11 kWh. The seeds are
fixed and solve in well under a second, so neither slot length can drop out of the slice
on a slow runner: each yields chosen windows, free and pinned, on both solve paths.
"""

import pytest

from benchmarks.balance_discharge import sweep

TOLERANCE_KWH = 1e-6
QUARTER_HOUR_SEEDS = (30, 71, 40, 61, 44)
HOURLY_SEEDS = (59, 32, 62, 67, 39, 70)


@pytest.mark.parametrize("accept_incumbent", [False, True])
def test_chosen_balance_windows_carry_no_discharge_noise(accept_incumbent):
    result = sweep(
        QUARTER_HOUR_SEEDS + HOURLY_SEEDS,
        time_limit_s=5.0,
        accept_incumbent=accept_incumbent,
    )
    assert not result.balance_failures(), result.balance_failures()
    assert not [k for k in result.outcomes if "timeout" in k[1]], result.outcomes
    assert result.windows[15] > 0, result.outcomes
    assert result.windows[60] > 0, result.outcomes
    assert result.max_bd <= TOLERANCE_KWH
