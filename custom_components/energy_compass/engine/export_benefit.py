"""Decision reserve for additional physical battery-export periods."""

from datetime import UTC

import numpy as np

from .machine_state import activity_tolerance
from .models import Problem, SolveError

_TOL = 1e-6
_CLASSIFIER_RESOLUTION = 2e-6


def enabled(problem: Problem) -> bool:
    """A battery and a positive hurdle enable export-period accounting."""
    return problem.battery is not None and problem.minimum_export_episode_benefit > 0


def constrain_export_benefit(model, problem: Problem, vectors) -> None:
    """Bind exact physical export indicators and charge only rising edges."""
    if not enabled(problem):
        return
    model.exact_mip_gap = True
    for i, (slot, v) in enumerate(zip(problem.slots, vectors, strict=True)):
        if "mode_DISCHARGE_GRID" in v:
            v["export_active"] = v["mode_DISCHARGE_GRID"]
        else:
            e = v["export_active"] = model.variable(binary=True)
            z = v["export_side"] = model.variable(binary=True)
            hours = (
                slot.end.astimezone(UTC) - slot.start.astimezone(UTC)
            ).total_seconds() / 3600
            activity = problem.minimum_mode_power_kw * hours
            discharge_max, export_max = model.upper[v["bd"]], model.upper[v["gout"]]
            if (
                activity <= _CLASSIFIER_RESOLUTION
                or min(discharge_max, export_max) < activity
            ):
                model.upper[e] = 0
            model.constrain(
                {v["bd"]: 1, e: -discharge_max, z: -discharge_max}, -np.inf, 0
            )
            model.constrain(
                {v["gout"]: 1, e: -export_max, z: export_max}, -np.inf, export_max
            )
            model.constrain({v["bd"]: 1, e: -activity}, 0, np.inf)
            model.constrain({v["gout"]: 1, e: -activity}, 0, np.inf)
        e = v["export_active"]
        s = v["export_start"] = model.variable(
            binary=True, cost=problem.minimum_export_episode_benefit
        )
        previous = vectors[i - 1]["export_active"] if i else None
        initial = int(problem.initial_export_active) if i == 0 else 0
        lower = {s: 1, e: -1}
        upper = {s: 1}
        if previous is not None:
            lower[previous] = 1
            upper[previous] = 1
        model.constrain(lower, -initial, np.inf)
        model.constrain({s: 1, e: -1}, -np.inf, 0)
        model.constrain(upper, -np.inf, 1 - initial)


def validate_export_benefit(problem: Problem, vectors, values) -> tuple[int, float]:
    """Certify physical activity, exact starts and reserve independently of rows."""
    if not enabled(problem):
        return 0, 0.0
    previous = problem.initial_export_active
    starts = 0
    for slot, v in zip(problem.slots, vectors, strict=True):
        for key in ("export_active", "export_start", "export_side"):
            if key in v:
                value = values[v[key]]
                if not np.isfinite(value) or min(abs(value), abs(value - 1)) > _TOL:
                    raise SolveError(
                        "solver_failure", f"invalid solver result: fractional {key}"
                    )
        active = values[v["export_active"]] > 0.5
        start = values[v["export_start"]] > 0.5
        discharge, export = values[v["bd"]], values[v["gout"]]
        hours = (
            slot.end.astimezone(UTC) - slot.start.astimezone(UTC)
        ).total_seconds() / 3600
        activity = problem.minimum_mode_power_kw * hours
        tolerance = activity_tolerance(activity)
        physical = min(discharge, export) > tolerance
        if active != physical or (
            active
            and (
                activity <= _CLASSIFIER_RESOLUTION
                or min(discharge, export) < activity - tolerance
            )
        ):
            raise SolveError(
                "solver_failure", "invalid solver result: physical export indicator"
            )
        if start != (active and not previous):
            raise SolveError(
                "solver_failure", "invalid solver result: export start identity"
            )
        starts += int(start)
        previous = active
    return starts, starts * problem.minimum_export_episode_benefit
