"""Mode validators accept the noise HiGHS leaves on a binary, and nothing beyond it."""

from dataclasses import replace

import pytest
from test_dispatch_policy import dispatch

from custom_components.energy_compass.engine.dispatch_policy import validate_modes
from custom_components.energy_compass.engine.machine_state import activity_tolerance
from custom_components.energy_compass.engine.models import Flow, SolveError
from custom_components.energy_compass.engine.optimize import solve

# Random small problems on which HiGHS stops with an indicator inside its 1e-6
# integrality tolerance: the flow it gates falls 4.9e-7 short of its minimum
# activity, or a discharge/export pair sits just under the classifier threshold.
NOISY = {
    "mode short of minimum activity": (
        [0.9, 0.05, 0.05, 0.9, 0.9, 0.9, 0.9, 0.05, 0.05, 0.05, 0.9, 0.0],
        [0.176, 0.442, 0.419, 0.49, 0.279, 0.323, 0.417, 0.329, 0.105, 0.225]
        + [0.445, 0.439],
        [0.0, 1.4, 1.4, 0.5, 0.2, 0.2, 0.5, 1.4, 1.4, 0.5, 0.5, 1.4],
        [1.5, 1.0, 0.8, 0.4, 0.8, 1.5, 1.0, 0.8, 0.8, 1.0, 0.8, 1.5],
        2.0,
        10.0,
    ),
    "export indicator": (
        [0.9, 0.0, 0.05, 0.05, 0.05, 0.05, 0.0, 0.05, 0.05, 0.05, 0.0, 0.0],
        [0.381, 0.252, 0.328, 0.338, 0.29, 0.223, 0.209, 0.444, 0.122, 0.412]
        + [0.135, 0.17],
        [0.0, 0.5, 0.5, 0.0, 0.5, 0.2, 0.5, 0.5, 0.5, 0.2, 0.0, 0.0],
        [1.5, 0.4, 0.8, 1.0, 1.0, 0.4, 1.0, 0.8, 1.0, 1.0, 1.5, 1.0],
        0.0,
        2.7,
    ),
    "self-consume with micro charge": (
        [0.9, 0.0, 0.05, 0.0, 0.9, 0.9, 0.05, 0.9, 0.05, 0.0, 0.05, 0.9],
        [0.468, 0.256, 0.33, 0.23, 0.415, 0.179, 0.179, 0.237, 0.101, 0.355]
        + [0.405, 0.369],
        [0.5, 0.2, 0.2, 1.4, 0.5, 0.0, 0.0, 0.5, 0.0, 1.4, 0.2, 0.2],
        [0.8, 0.8, 1.0, 1.0, 0.8, 0.4, 0.4, 1.0, 1.0, 1.5, 0.8, 1.0],
        2.0,
        9.2,
    ),
}


@pytest.mark.parametrize("case", NOISY)
def test_solver_noise_does_not_fail_a_sound_plan(case):
    pv, load, sell, buy, terminal, initial = NOISY[case]
    problem = dispatch(
        tuple(zip(buy, sell, pv, load, strict=True)),
        minimum_mode_minutes=60,
        terminal_value_per_kwh=terminal,
    )
    problem = replace(
        problem,
        battery=replace(
            problem.battery, initial_kwh=initial, eta_charge=0.95, eta_discharge=0.95
        ),
    )
    assert len(solve(problem).flows) == 12


def test_activity_tolerance_is_the_solver_tolerance_and_scales_down():
    assert activity_tolerance(0.025) == 1e-6
    assert activity_tolerance(1e-3) == pytest.approx(1e-7)


def test_validator_tolerates_noise_up_to_the_solver_tolerance_only():
    problem = dispatch(((1, 0, 0, 0),) * 4, minimum_mode_minutes=60)

    def rows(charge):
        return tuple(
            Flow(charge, 0, charge, 0, 0, (i + 1) * charge, "charge", "CHARGE_GRID")
            for i in range(4)
        )

    # Minimum activity is 0.1 kW for a quarter hour: 0.025 kWh.
    validate_modes(problem, rows(0.025 - 5e-7))
    with pytest.raises(SolveError, match="physical dispatch mode"):
        validate_modes(problem, rows(0.025 - 5e-6))
