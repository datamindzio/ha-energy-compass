"""solve_flexible_load on a 25-40 kWh/slot battery: real HiGHS, no fakes (T-056).

A seeded sweep of the tests/test_solver_noise_regression.py corpus (seeds 1000-1799, 24 and
48 slots, 4 requests each, battery limit 25 kWh/cap 30 and limit 40/cap 45) found 12 failures
in 6400 solves on `main` 5c11c2e: `simultaneous grid directions` (9) and `physical export
indicator` (3). They are the indicator-noise class of T-052: the validators compared a binary
within 1e-6 of 0/1 and the M-gated flows beside it against a fixed 1e-6, while the rows allow
1e-6 * (M + 1) with M of tens of kWh. Each case below failed before the validators scaled the
tolerance with M; the request and PV scale are the sweep's own draws.
"""

from dataclasses import replace

import pytest
from test_solver_noise_regression import corpus_problem

from custom_components.energy_compass.engine.models import (
    FlexibleLoadRequest,
    SiteLimits,
)
from custom_components.energy_compass.engine.optimize import solve_flexible_load

# (seed, slots, limit kWh/slot, PV scale, energy kWh, max power kW)
CASES = {
    "1031 export indicator": (
        1031,
        48,
        25.0,
        28.51839424795234,
        9.299097623064444,
        20.63516456790571,
    ),
    "1036 grid directions": (
        1036,
        48,
        25.0,
        141.70590732631595,
        34.10985346179786,
        19.94961517155931,
    ),
    "1145 grid directions": (
        1145,
        48,
        40.0,
        8.646306771577704,
        13.930427588405513,
        31.032916283158546,
    ),
    "1152 grid directions": (
        1152,
        48,
        40.0,
        172.72246398276405,
        84.75234016765471,
        38.356374530816765,
    ),
    "1263 grid directions": (
        1263,
        24,
        25.0,
        13.773291092647876,
        59.88453422135988,
        11.60977843823018,
    ),
    "1290 export indicator": (
        1290,
        48,
        25.0,
        9.84162454854943,
        43.69802295785772,
        12.626000104873691,
    ),
    "1527 grid directions": (
        1527,
        48,
        40.0,
        21.99385894330213,
        39.92808275925308,
        36.697775051641614,
    ),
    "1606 export indicator": (
        1606,
        24,
        25.0,
        78.98400895756139,
        59.85781948318308,
        24.05780196563382,
    ),
    "1693 grid directions": (
        1693,
        24,
        25.0,
        9.084915131494855,
        22.85438978960601,
        5.498674781138739,
    ),
    "1707 grid directions": (
        1707,
        48,
        40.0,
        44.54043986251949,
        44.41863618181919,
        13.673484327430835,
    ),
    "1786 grid directions": (
        1786,
        48,
        40.0,
        3.3031336558541504,
        83.79559300863667,
        24.306863180412464,
    ),
}


CAPACITY = {25.0: 30.0, 40.0: 45.0}


def sweep_problem(seed: int, count: int, limit: float, pv_k: float):
    """The T-054 sweep's site: battery `limit` kWh/slot on the matching capacity, 3 x limit grid."""
    cap = CAPACITY[limit]
    base = corpus_problem(seed, 1.0, count)
    battery = replace(
        base.battery,
        capacity_kwh=cap,
        charge_kw=limit,
        discharge_kw=limit,
        initial_kwh=base.battery.initial_kwh * cap / 13.5,
    )
    base = replace(
        base, battery=battery, site=SiteLimits(3 * limit, 3 * limit, 3 * limit, True)
    )
    base = replace(
        base,
        slots=tuple(replace(s, load_kwh=s.load_kwh * limit / 3.3) for s in base.slots),
    )
    return replace(
        base, slots=tuple(replace(s, pv_kwh=s.pv_kwh * pv_k) for s in base.slots)
    )


@pytest.mark.timeout(60)
@pytest.mark.parametrize("name", CASES)
def test_flexible_load_solves_with_a_large_battery(name):
    seed, count, limit, pv_k, energy, power = CASES[name]
    result = solve_flexible_load(
        sweep_problem(seed, count, limit, pv_k),
        FlexibleLoadRequest(energy, power),
        time_limit_s=20.0,
    )
    assert result is not None
