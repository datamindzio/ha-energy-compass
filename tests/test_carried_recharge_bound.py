"""The recharge a carried commitment can count on is the one an interval can really take (T-070).

`dispatch_policy._owed` is the energy an interval must end with so that the later
reserve floors and the `preserve_initial` terminal can still be met, counted with the
best-case recharge. It counted every PV surplus the pack could take, also where no
PV-follow run can be under way: a surplus below the minimum activity, the last minimum
duration of the horizon, a run that an idle interval ends, a slot a HOLD started at a
closed slot holds. The lock floor was then lower than the truth, the release of the
commitment was refused and the problem had no plan (aimed generator of
`benchmarks/curtail_sweep.py`, `aimrun`).
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_carried_commitment_guard_probes import committed, modes

from custom_components.energy_compass.engine import dispatch_policy as policy
from custom_components.energy_compass.engine.models import Battery, SiteLimits
from custom_components.energy_compass.engine.optimize import solve

START = datetime(2026, 12, 18, 11, tzinfo=UTC)


def owed(rows, minutes=60, since=120, mode="SELF_CONSUME", **battery):
    pack = replace(
        Battery(5, 0.1, 1, 4.0, 3, 3, 0.95, 0.95, 0, False, False, 0.05), **battery
    )
    source = committed(
        [(pv, load, 1.0, 0.1) for pv, load in rows],
        pack,
        mode,
        minutes,
        since,
        start=START,
        terminal_mode="preserve_initial",
    )
    drains = policy.drain_schedule(source)
    held_until = source.initial_dispatch_mode_since + timedelta(minutes=minutes)
    return [round(x, 6) for x in policy._owed(source, drains, held_until)]


def test_a_surplus_below_the_minimum_activity_is_not_recharge():
    # 0.02 kWh of surplus against 0.1 kW x 0.25 h = 0.025 kWh: PV-follow cannot take it.
    below = owed([(0.12, 0.1)] * 4)
    assert below == owed([(0.1, 0.1)] * 4)
    assert below != owed([(0.15, 0.1)] * 4)


def test_a_surplus_no_run_can_start_on_in_the_last_minimum_duration_is_not_recharge():
    # 8 intervals, 60 minute duration, the first 5 idle: a run would have to start in one
    # of the last 3, which no longer holds for a minimum duration.
    idle = [(0.1, 0.1)] * 5
    assert owed(idle + [(0.3, 0.1)] * 3) == owed(idle + [(0.1, 0.1)] * 3)


def test_a_surplus_a_run_began_on_before_the_horizon_ends_is_recharge():
    assert owed([(0.3, 0.1)] * 4 + [(0.1, 0.1)] * 4) != owed([(0.1, 0.1)] * 8)


def test_an_idle_interval_ends_the_run_the_surplus_after_it_cannot_continue():
    gap = [(0.3, 0.1)] * 2 + [(0.1, 0.1)] + [(0.3, 0.1)] * 3
    plain = [(0.3, 0.1)] * 2 + [(0.1, 0.1)] * 4
    # The run of the first two intervals is recharge, the three after the gap start none:
    # fewer than one minimum duration is left and 60 minutes do not fit.
    assert owed(gap, minutes=60, since=120) == owed(plain, minutes=60, since=120)


# Draws of seed 101 of `benchmarks/curtail_sweep.py aimrun` that were `infeasible` on main
# 0778580 and are planned. (PV, load, buy, sell) per quarter hour.
DRAWS = {
    "241: an empty pack, SELF_CONSUME since 15 minutes of 60": (
        [
            (0.0, 0.1, 0.23, 0.48),
            (0.15, 0.1, 1.36, 0.71),
            (0.05, 0.1, 1.33, 0.64),
            (0.0, 0.1, 0.77, 0.52),
            (0.08, 0.1, 0.74, 0.43),
            (0.2, 0.1, 1.06, 0.2),
        ],
        Battery(10, 0, 1, 0.05, 3, 1, 0.95, 0.95, 0, False, False, 0),
        SiteLimits(8, 8, 8, False),
        "SELF_CONSUME",
        60,
        15,
    ),
    "392: a pack 0.01 kWh below the ceiling, 120 minutes": (
        [
            (0.08, 0.1, 1.19, 0.37),
            (0.05, 0.1, 0.24, 0.56),
            (0.0, 0.1, 0.89, 0.35),
            (0.2, 0.1, 0.68, 0.54),
            (0.05, 0.1, 1.29, 0.59),
            (0.12, 0.1, 0.44, 0.58),
        ],
        Battery(5, 0, 1, 4.99, 3, 3, 0.95, 0.95, 0.01, False, False, 0.05),
        SiteLimits(8, 8, 8, True),
        "SELF_CONSUME",
        120,
        0,
    ),
    "365: CHARGE_PV, an export cap of 0.5 kW, standby loss 0.2 kW": (
        [
            (0.05, 0.1, 1.17, 0.76),
            (0.0, 0.02, 1.0, 0.05),
            (0.15, 0.05, 0.77, 0.15),
            (0.03, 0.05, 0.69, 0.64),
            (0.5, 0.4, 1.48, 0.47),
            (0.1, 0.1, 0.27, 0.16),
            (0.02, 0.02, 0.73, 0.48),
        ],
        Battery(5, 0, 1, 4.7, 1, 4, 0.95, 0.95, 0.01, False, True, 0.2),
        SiteLimits(8, 8, 0.5, True),
        "CHARGE_PV",
        60,
        45,
    ),
    "71: a pack at the ceiling, no export, PV the site cannot pass": (
        [
            (0.08, 0.1, 0.59, 0.59),
            (0.1, 0.1, 1.27, 0.69),
            (0.15, 0.1, 0.84, 0.16),
            (0.15, 0.1, 1.07, 0.06),
            (0.2, 0.1, 1.41, 0.42),
            (0.4, 0.1, 1.32, 0.24),
            (0.0, 0.1, 0.33, 0.1),
            (0.0, 0.1, 0.61, 0.32),
        ],
        Battery(10, 0.1, 1, 9.99, 3, 1, 0.95, 0.95, 0, False, False, 0.02),
        SiteLimits(3, 8, 0, True),
        "SELF_CONSUME",
        60,
        45,
    ),
}


@pytest.mark.parametrize("name", DRAWS)
def test_a_commitment_the_best_case_recharge_overstated_is_planned(name):
    rows, pack, site, mode, minutes, since = DRAWS[name]
    source = committed(
        rows,
        pack,
        mode,
        minutes,
        since,
        power=0.05 if name.startswith("71") else 0.1,
        start=START,
        terminal_mode="preserve_initial",
    )
    source = replace(source, site=site)
    plan = solve(source)
    policy.validate_modes(source, plan.flows, plan.carried_bridges)
    drained = sum(policy.drain_schedule(source))
    assert plan.flows[-1].end_soc_kwh >= pack.initial_kwh - drained - 1e-6
    assert modes(plan)
