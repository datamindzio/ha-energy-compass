"""Flows the solver's chosen mode leaves at zero, without the noise HiGHS leaves on them."""

import numpy as np

from .dispatch_policy import IDLE_FLOWS, MODES
from .machine_state import _TOL, noise_bound
from .models import Problem

# Zeroing a flow leaves the site balance short (+) or long (-) by its energy.
_SUPPLY_LEFT = {"curt": 1.0, "bc": 1.0, "gout": 1.0, "bd": -1.0}


def idle_flows(v: dict[str, int], values: np.ndarray) -> tuple[str, ...]:
    """The flows the solver's chosen state leaves at zero in one slot.

    With mode variables the chosen mode names them. Without (minimum duration 0) the
    rows that gate them are the battery direction `battery_mode` and, with the export
    benefit hurdle, the export indicator `export_active` (and `export_side`, which
    picks the gated flow when it is off): `bc` or `bd`, `bd` or `gout`.
    """
    present = [m for m in MODES if f"mode_{m}" in v]
    if present:
        mode = next((m for m in present if values[v[f"mode_{m}"]] > 0.5), None)
        return IDLE_FLOWS[mode] if mode else ()
    idle = ["bd" if values[v["battery_mode"]] > 0.5 else "bc"]
    if "export_side" in v and values[v["export_active"]] < 0.5:
        idle.append("gout" if values[v["export_side"]] > 0.5 else "bd")
    return tuple(dict.fromkeys(idle))


def _settle(
    idle: tuple[str, ...],
    flow: dict[str, float],
    upper: dict[str, float],
    moded: bool,
):
    """Zero the `idle` flows and close the balance; None if it cannot.

    With mode variables the balance closes through the grid and `validate_modes` holds
    the row to the chosen mode. Without them nothing names the chosen state, so a grid
    flow opened beside the working battery direction would publish another one (an
    import beside a charge reads CHARGE_GRID, an export beside a discharge
    DISCHARGE_GRID): the working direction gives the energy back instead.
    """
    settled = dict(flow)
    left = 0.0
    for name in idle:
        # `flow <= M * binary` rows: HiGHS accepts the binary within 1e-6 of 0 and each
        # row within 1e-6, so a flow gated by two unselected modes can carry
        # 1e-6 * (M1 + M2 + 1), and no M exceeds the variable's own bound. Up to 1e-6
        # the classifier already reads the flow as zero.
        if _TOL < flow[name] <= noise_bound(upper[name]):
            settled[name] = 0.0
            left += _SUPPLY_LEFT[name] * flow[name]
    if left > 0:
        # Too much supply: import less, then export the rest. SELF_CONSUME may not
        # export, so there the battery delivers less, as a discharge without mode
        # variables does.
        # A flow HiGHS returned a hair below zero has nothing to give.
        imported = max(min(settled["gin"], left), 0.0)
        settled["gin"] -= imported
        left -= imported
        if "gout" in idle or (not moded and settled["bd"] > _TOL):
            if left > settled["bd"]:
                return None
            settled["bd"] -= left
        else:
            settled["gout"] += left
    elif left < 0:
        # Too little supply: export less, then import the rest; a charge without mode
        # variables takes that much less.
        exported = max(min(settled["gout"], -left), 0.0)
        settled["gout"] -= exported
        left += exported
        if not moded and settled["bc"] > _TOL:
            if -left > settled["bc"]:
                return None
            settled["bc"] += left
        else:
            settled["gin"] -= left
    return settled


def settle_mode_noise(
    problem: Problem,
    vectors: list[dict[str, int]],
    values: np.ndarray,
    upper: list[float],
) -> list[dict[str, float]]:
    """Per slot grid, battery flows and end energy without mode noise on the idle flows.

    The `mode_X` rows (the battery direction and export indicator rows without mode
    variables) leave a flow up to `1e-6 * (M + 1)` kWh on a state the solver did not
    choose, which exceeds the 1e-6 kWh the display classifier calls zero once M > 1,
    so the slot would read as another state than the one the solver chose. That energy
    is not dispatched: the flow is zeroed, the site balance closes through the grid
    (through the battery when the idle flows include the export, and without mode
    variables whenever the other battery direction is working), and the end energy
    follows the battery flows that changed, so the plan stays one consistent set of
    rows. A grid import beside an export, the same noise on `grid_mode`, nets.
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
        idle = idle_flows(v, values) if battery else ()
        if idle:
            moded = any(f"mode_{m}" in v for m in MODES)
            settled = _settle(
                idle, flow, {name: upper[v[name]] for name in idle}, moded
            )
            if settled:
                shift += (
                    battery.eta_charge * (settled["bc"] - flow["bc"])
                    - (settled["bd"] - flow["bd"]) / battery.eta_discharge
                )
                flow = settled
        flow["energy"] += shift
        if min(flow["gin"], flow["gout"]) > _TOL:
            # `gin <= M * grid_mode` and `gout <= M * (1 - grid_mode)` let both carry
            # noise when the indicator is a hair off 0 or 1: the plan keeps the net.
            net = flow["gin"] - flow["gout"]
            flow["gin"] = net if net > 0 else 0.0
            flow["gout"] = -net if net < 0 else 0.0
        slots.append(flow)
    return slots
