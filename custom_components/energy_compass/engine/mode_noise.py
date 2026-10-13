"""Flows the solver's chosen mode leaves at zero, without the noise HiGHS leaves on them."""

import numpy as np

from .dispatch_policy import IDLE_FLOWS, MODES
from .machine_state import _TOL
from .models import Problem

# Zeroing a flow leaves the site balance short (+) or long (-) by its energy.
_SUPPLY_LEFT = {"curt": 1.0, "bc": 1.0, "gout": 1.0, "bd": -1.0}


def _settle(mode: str, flow: dict[str, float], upper: dict[str, float]):
    """Zero what `mode` forbids and close the balance through the grid; None if it cannot."""
    idle = IDLE_FLOWS[mode]
    settled = dict(flow)
    left = 0.0
    for name in idle:
        # `flow <= M * binary` rows: HiGHS accepts the binary within 1e-6 of 0 and each
        # row within 1e-6, so a flow gated by two unselected modes can carry
        # 1e-6 * (M1 + M2 + 1), and no M exceeds the variable's own bound. Up to 1e-6
        # the classifier already reads the flow as zero.
        if _TOL < flow[name] <= _TOL * (2 * upper[name] + 1):
            settled[name] = 0.0
            left += _SUPPLY_LEFT[name] * flow[name]
    if left > 0:
        # Too much supply: import less, then export the rest. SELF_CONSUME may not
        # export, so there the battery delivers less.
        # A flow HiGHS returned a hair below zero has nothing to give.
        imported = max(min(settled["gin"], left), 0.0)
        settled["gin"] -= imported
        left -= imported
        if "gout" in idle:
            if left > settled["bd"]:
                return None
            settled["bd"] -= left
        else:
            settled["gout"] += left
    elif left < 0:
        # Too little supply: export less, then import the rest.
        exported = max(min(settled["gout"], -left), 0.0)
        settled["gout"] -= exported
        settled["gin"] += -left - exported
    return settled


def settle_mode_noise(
    problem: Problem,
    vectors: list[dict[str, int]],
    values: np.ndarray,
    upper: list[float],
) -> list[dict[str, float]]:
    """Per slot grid, battery flows and end energy without mode noise on the idle flows.

    The `mode_X` rows leave a flow up to `1e-6 * (M + 1)` kWh on a mode the solver did
    not choose, which exceeds the 1e-6 kWh the display classifier calls zero once M > 1,
    so the slot would read as another mode than the one the solver chose. That energy
    is not dispatched: the flow is zeroed, the site balance closes through the grid
    (through the battery when SELF_CONSUME may not export), and the end energy follows
    the battery flows that changed, so the plan stays one consistent set of rows.
    """
    battery = problem.battery
    names = ("gin", "gout", "curt", *(("bc", "bd", "energy") if battery else ()))
    shift = 0.0
    slots = []
    for v in vectors:
        flow = {name: float(values[v[name]]) for name in names}
        flow.setdefault("bc", 0.0)
        flow.setdefault("bd", 0.0)
        flow.setdefault("energy", 0.0)
        mode = next(
            (m for m in MODES if f"mode_{m}" in v and values[v[f"mode_{m}"]] > 0.5),
            None,
        )
        if battery and mode:
            settled = _settle(
                mode, flow, {name: upper[v[name]] for name in IDLE_FLOWS[mode]}
            )
            if settled:
                shift += (
                    battery.eta_charge * (settled["bc"] - flow["bc"])
                    - (settled["bd"] - flow["bd"]) / battery.eta_discharge
                )
                flow = settled
        flow["energy"] += shift
        slots.append(flow)
    return slots
