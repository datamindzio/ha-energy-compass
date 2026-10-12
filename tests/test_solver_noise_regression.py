"""Random problems on which HiGHS stops inside its 1e-6 tolerances; real HiGHS, no fakes.

Each case is a benchmark site over 24 hourly slots with buy and sell drawn per slot
from 0.3-1.2, battery export allowed and a PV scale picked by a seeded scan. They failed
as `solver_failure: invalid solver result` before the validators and the plan read the
noise the way the solver leaves it:

- seed 16: a discharge/export pair of 7.2e-7 kWh beside an export indicator at 0 sat
  above the old 1e-7 validator tolerance (T-031: `activity_tolerance`);
- seeds 7 and 24: CHARGE_PV with 1.6e-6 / 7.5e-6 kWh of curtailment, the binary of
  `curt <= M * mode_CURTAIL` at 1e-6 times M > 1, which the classifier reads as CURTAIL.
  Seed 7 passes on a changed vertex alone since T-030; seed 24 failed on `main`.
"""

import random
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from benchmarks.optimizer import make_problem
from custom_components.energy_compass.engine.machine_state import machine_state
from custom_components.energy_compass.engine.optimize import _settle_curtail, solve

CASES = {
    "seed 7 curtailment noise": (7, 3.2068840553749727),
    "seed 16 export indicator noise": (16, 0.5105639826334869),
    "seed 24 curtailment noise": (24, 15.666677533806073),
}


def corpus_problem(seed: int, pv_scale: float, count: int = 24):
    """The T-036 random corpus (high prices, battery export) at one PV scale."""
    r = random.Random(seed * 1000 + count)
    p = make_problem(96, allow_battery_export=r.random() < 0.3)
    p = replace(
        p,
        battery=replace(p.battery, allow_battery_export=True),
        limit_export_to_pv=False,
    )
    pv_k, load_k = r.uniform(0.3, 2.0), r.uniform(0.6, 2.0)
    start = p.slots[0].start
    slots = []
    for i in range(count):
        quarter = p.slots[4 * (i % 24)]
        slots.append(
            replace(
                quarter,
                start=start + timedelta(hours=i),
                end=start + timedelta(hours=i + 1),
                pv_kwh=4 * quarter.pv_kwh * pv_k * r.uniform(0.6, 1.2) * pv_scale,
                load_kwh=4 * quarter.load_kwh * load_k * r.uniform(0.7, 1.4),
                buy_per_kwh=r.uniform(0.3, 1.2),
                sell_per_kwh=r.uniform(0.3, 1.2),
            )
        )
    battery = replace(p.battery, initial_kwh=r.uniform(1.35, 12))
    return replace(p, slots=tuple(slots), battery=battery)


@pytest.mark.parametrize("case", CASES)
def test_noisy_solution_publishes_a_plan_whose_rows_match_their_modes(case):
    seed, pv_scale = CASES[case]
    problem = corpus_problem(seed, pv_scale)

    plan = solve(problem, time_limit_s=20.0, accept_incumbent=True)

    assert len(plan.flows) == 24
    for slot, flow in zip(problem.slots, plan.flows, strict=True):
        assert flow.dispatch_mode == machine_state(slot, flow)


def test_curtailment_noise_is_given_back_to_the_grid_not_curtailed():
    model = SimpleNamespace(upper={5: 7.5})

    def values(mode, gin, gout, curt):
        return {1: gin, 2: gout, 5: curt, 6: mode}

    v = {"gin": 1, "gout": 2, "curt": 5, "mode_CURTAIL": 6}

    # Noise on an unselected mode: import is returned first, the rest is exported.
    assert _settle_curtail(values(0, 5e-6, 0, 7.5e-6), v, model) == (
        0.0,
        pytest.approx(2.5e-6),
        0.0,
    )
    # A selected CURTAIL mode, or curtailment above the noise bound, is untouched.
    assert _settle_curtail(values(1, 0, 1, 7.5e-6), v, model) == (0, 1, 7.5e-6)
    assert _settle_curtail(values(0, 0, 1, 1e-3), v, model) == (0, 1, 1e-3)
