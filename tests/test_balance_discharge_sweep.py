"""T-048: a chosen balance window never fails `balance discharge` on real HiGHS.

A short slice of `benchmarks.balance_discharge`; the full sweep (1600 seeds) found no
failure and a largest discharge inside a chosen window of 7.4e-11 kWh.
"""

from benchmarks.balance_discharge import sweep

TOLERANCE_KWH = 1e-6


def test_chosen_balance_windows_carry_no_discharge_noise():
    outcomes, max_bd, windows = sweep(range(12), time_limit_s=5.0)
    failures = {k: v for k, v in outcomes.items() if "balance" in k}
    assert not failures, failures
    assert windows > 0
    assert max_bd <= TOLERANCE_KWH
