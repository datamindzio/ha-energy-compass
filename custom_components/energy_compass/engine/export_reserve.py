"""Margin kept under a battery-export slot until the next cheap slot.

`peak_reserve.export_reserve_targets` puts a target on every export-capable slot of an
expensive run that a cheap slot follows. Here the target binds only where the plan
exports from the pack in that slot (`export_active`, or the DISCHARGE_GRID mode):
`energy + slack >= target * export_active`, one slack per run, priced at
`export_reserve_weight`. The plan then keeps the reserve, the forecast deficit to the
cheap slot and the margin whenever it exports, and gives up the export slot that earns
less than the shortfall costs. It is soft and model-only: no published cost holds it,
and a slot with no export carries no floor.
"""

import numpy as np

from .models import Problem, SolveError

_TOL = 1e-6


def _indicator(variables: dict[str, int]) -> int | None:
    """The physical export indicator of a slot: `export_active`, else the DISCHARGE_GRID mode."""
    return variables.get("export_active", variables.get("mode_DISCHARGE_GRID"))


def _windows(problem: Problem) -> tuple[tuple[int, ...], ...]:
    if not (
        problem.battery
        and problem.export_reserve_kwh
        and problem.export_reserve_weight > 0
    ):
        return ()
    runs: dict[int, list[int]] = {}
    for index, window_id in enumerate(problem.export_reserve_window):
        if window_id != -1 and problem.export_reserve_kwh[index] > 0:
            runs.setdefault(window_id, []).append(index)
    return tuple(tuple(run) for run in runs.values())


def constrain_export_reserve(model, problem: Problem, vectors) -> None:
    """Add the conditional rows; call after `constrain_export_benefit`."""
    targets = problem.export_reserve_kwh
    for window in _windows(problem):
        exporting = [t for t in window if _indicator(vectors[t]) is not None]
        if not exporting:
            continue
        slack = model.variable(
            upper=max(targets[t] for t in exporting),
            cost=problem.export_reserve_weight,
        )
        vectors[window[0]]["export_reserve_slack"] = slack
        for t in exporting:
            model.constrain(
                {
                    vectors[t]["energy"]: 1,
                    slack: 1,
                    _indicator(vectors[t]): -targets[t],
                },
                0,
                np.inf,
            )


def validate_export_reserve(problem: Problem, vectors, values) -> None:
    """Certify each row from the solved values, not from the model."""
    for window in _windows(problem):
        if "export_reserve_slack" not in vectors[window[0]]:
            continue
        slack = float(values[vectors[window[0]]["export_reserve_slack"]])
        for t in window:
            indicator = _indicator(vectors[t])
            if indicator is None or values[indicator] <= 0.5:
                continue
            energy = float(values[vectors[t]["energy"]])
            if slack < problem.export_reserve_kwh[t] - energy - _TOL:
                raise SolveError(
                    "solver_failure", "invalid solver result: export reserve slack"
                )
