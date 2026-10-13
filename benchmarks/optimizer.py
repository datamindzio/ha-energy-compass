"""Measure deterministic advisory solve workloads in the target runtime image.

Invoked by path (`python benchmarks/optimizer.py`) because the test container's
entrypoint is bare `python`, which only puts this script's own directory on
`sys.path`, not the repository root `custom_components` lives under.

Each JSON line reports the plan in two units that must not be mixed: `objective`
is the plan cost in currency (grid cost + wear - terminal credit), while
`weighted_objective` is what the solver minimises (cost vector times solution, with
the strategy weights and soft-floor slacks) and the only quantity `mip_gap` bounds.
`--accept-incumbent` solves as the runtime does for the base plan: a time limit with
an incumbent returns it (`result == "time_limited"`); `--compare-proven` then re-solves
a time-limited case to its proof. docs/runtime-validation.md § Advisory optimizer
benchmark is built from these fields.
"""

import argparse
import json
import resource
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import pi, sin
from pathlib import Path
from time import perf_counter, process_time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.energy_compass.engine import optimize
from custom_components.energy_compass.engine.autonomy import (
    autonomy_targets,
    autonomy_weight,
    backup_floor_kwh,
)
from custom_components.energy_compass.engine.balance import (
    candidate_windows,
    lift_slots,
)
from custom_components.energy_compass.engine.consumption import analyze_consumption
from custom_components.energy_compass.engine.models import (
    STRATEGIES,
    Battery,
    CompassSettings,
    Plan,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
    Strategy,
)
from custom_components.energy_compass.engine.optimize import solve
from custom_components.energy_compass.engine.strategy import (
    resolve_flags,
    strategy_weights,
)
from custom_components.energy_compass.settings import BOOLEANS, NUMBERS

_TIMEZONE = "Europe/Warsaw"

# Mirrors settings.py:74 total_time_limit_s (default 60, bounds 1..300) — the
# shipped serial-solve budget the acceptance rule below is checked against.
_TOTAL_TIME_LIMIT_S = 60.0


# strategy_weights() and the autonomy floor read these shipped settings.py
# defaults; runtime.build_problem gets the same values from validated
# configuration, this benchmark has no configuration document to validate.
_STRATEGY_NUMBER_KEYS = (
    "self_sufficiency_import_price_per_kwh",
    "self_sufficiency_export_penalty_per_kwh",
    "pv_swap_margin_per_kwh",
    "backup_target_soc_percent",
    "backup_shortfall_price_per_kwh",
    "peak_import_price_per_kw",
    "cap_violation_price_per_kwh",
    "grid_friendly_import_cap_kw",
    "grid_friendly_export_cap_kw",
    "autonomy_margin_per_kwh",
    "minimum_export_episode_benefit",
    "maximum_grid_charge_price",
    "import_penalty_per_kwh",
)
_STRATEGY_FLAG_KEYS = (
    "limit_export_to_pv",
    "limit_grid_charge_price",
    "autonomy_reserve",
)


def _shipped_values() -> dict[str, object]:
    values = {key: NUMBERS[key][1] for key in _STRATEGY_NUMBER_KEYS}
    values.update({key: BOOLEANS[key][1] for key in _STRATEGY_FLAG_KEYS})
    return values


def make_problem(
    count: int,
    *,
    strategy: Strategy = "cost_min",
    allow_battery_export: bool = False,
) -> Problem:
    start = datetime(2026, 9, 17, tzinfo=UTC)
    slots = []
    for index in range(count):
        hour = (index % 96) / 4
        pv = 1.1 * max(0.0, sin(pi * (hour - 6) / 12))
        load = 0.38 + (0.18 if 6 <= hour < 9 or 17 <= hour < 22 else 0)
        buy = 0.09 if hour < 6 else 0.85 if 17 <= hour < 21 else 0.32
        sell = 0.07 if hour < 17 else 0.22
        slots.append(
            Slot(
                start + timedelta(minutes=15 * index),
                start + timedelta(minutes=15 * (index + 1)),
                buy,
                sell,
                pv,
                load,
            )
        )
    slots = tuple(slots)
    site = SiteLimits(5, 8, 8, True)
    battery = Battery(
        13.5, 0.1, 0.9, 6.75, 3.3, 3.3, 0.94, 0.94, 0.08, True, allow_battery_export
    )
    base_values = _shipped_values()
    values = {**base_values, **resolve_flags(strategy, base_values, ())}
    weights = strategy_weights(
        strategy,
        values,
        max_abs_buy_per_kwh=max((abs(slot.buy_per_kwh) for slot in slots), default=0.0),
        site_import_kw=site.grid_import_kw,
        site_export_kw=site.grid_export_kw,
    )
    soc_target_kwh: tuple[float, ...] = ()
    soc_target_window: tuple[int, ...] = ()
    soc_target_weight = 0.0
    if values["autonomy_reserve"]:
        pv_kwh = tuple(slot.pv_kwh for slot in slots)
        load_kwh = tuple(slot.load_kwh for slot in slots)
        starts = tuple(slot.start for slot in slots)
        durations_h = tuple(
            (slot.end - slot.start).total_seconds() / 3600 for slot in slots
        )
        floor = autonomy_targets(
            starts,
            durations_h,
            load_kwh,
            pv_kwh,
            reserve_kwh=battery.capacity_kwh * battery.minimum_soc_fraction,
            usable_capacity_kwh=battery.capacity_kwh * battery.maximum_soc_fraction,
            eta_discharge=battery.eta_discharge,
            timezone=_TIMEZONE,
        )
        soc_target_kwh = floor.targets_kwh[: len(slots)]
        soc_target_window = floor.window_ids[: len(slots)]
        soc_target_weight = autonomy_weight(
            slots, margin=values["autonomy_margin_per_kwh"], timezone=_TIMEZONE
        )
        if strategy == "backup_ready":
            floor_kwh = backup_floor_kwh(
                battery.capacity_kwh, values["backup_target_soc_percent"]
            )
            soc_target_kwh = tuple(max(t, floor_kwh) for t in soc_target_kwh)
            soc_target_weight = max(
                soc_target_weight, values["backup_shortfall_price_per_kwh"]
            )
    return Problem(
        slots,
        site,
        battery,
        "preserve_initial",
        0,
        (),
        _TIMEZONE,
        strategy=strategy,
        limit_export_to_pv=values["limit_export_to_pv"],
        minimum_export_episode_benefit=values["minimum_export_episode_benefit"],
        maximum_grid_charge_price=values["maximum_grid_charge_price"]
        if values["limit_grid_charge_price"]
        else None,
        soc_target_kwh=soc_target_kwh,
        soc_target_window=soc_target_window,
        soc_target_weight=soc_target_weight,
        **weights,
    )


def _compass_settings_for_probes(probes: int) -> CompassSettings:
    """Whole-hour display/reference horizon giving exactly `probes` probes.

    Fixed at 15-minute resolution — the same width as the synthetic problem's
    slots — so the horizon in minutes (`probes * 15`) never exceeds source
    coverage as long as the caller keeps `--probes <= --slots` (both counted
    in 15-minute units). A coarser interval would let the horizon outrun
    source coverage while still reporting a "valid" whole-hour count, silently
    clipping the tail probes (`probe_completed < probe_requested`).

    Requires `probes` to be a multiple of 4 and the resulting horizon to fit
    `_settings`'s `1 <= hours <= 48` bound (`consumption.py:80-89`).
    """
    if probes % 4 or not 4 <= probes <= 192:
        raise ValueError(
            f"--probes {probes} must be a positive multiple of 4, at most 192 "
            "(15-minute display interval, <=48h horizon)"
        )
    hours = probes // 4
    return CompassSettings(
        display_horizon_hours=hours,
        reference_horizon_hours=hours,
        display_interval_minutes=15,
    )


def _solve_once(
    problem: Problem, *, time_limit_s: float, accept_incumbent: bool
) -> tuple[Plan | None, dict]:
    """Solve once; report wall and CPU time, model size and both objectives."""
    original_model_solve = optimize._Model.solve
    captured: dict[str, float] = {}

    def _spy(self, time_limit):
        captured["variable_count"] = len(self.cost)
        captured["row_count"] = len(self.rows)
        values = original_model_solve(self, time_limit)
        captured["weighted_objective"] = float(np.dot(self.cost, values))
        return values

    optimize._Model.solve = _spy
    started = perf_counter()
    cpu_started = process_time()
    try:
        plan = solve(
            problem, time_limit_s=time_limit_s, accept_incumbent=accept_incumbent
        )
    except SolveError as err:
        outcome: dict = {"result": err.reason}
        plan = None
    else:
        outcome = {
            "result": "time_limited" if plan.time_limited else "optimal",
            "objective": plan.objective,
            "flow_count": len(plan.flows),
            "time_limited": plan.time_limited,
            "mip_gap": plan.mip_gap,
        }
    finally:
        optimize._Model.solve = original_model_solve
    outcome["elapsed_s"] = perf_counter() - started
    outcome["cpu_s"] = process_time() - cpu_started
    outcome["weighted_objective"] = captured.get("weighted_objective")
    outcome["variable_count"] = int(captured.get("variable_count", 0))
    outcome["row_count"] = int(captured.get("row_count", 0))
    return plan, outcome


def _plan_difference(limited: Plan, proven: Plan) -> dict:
    """How far two plans of the same problem are apart, in kWh and in intervals."""
    pairs = list(zip(limited.flows, proven.flows, strict=True))
    return {
        "intervals_changed": sum(
            1
            for a, b in pairs
            if any(
                abs(x - y) > 1e-6
                for x, y in (
                    (a.grid_import_kwh, b.grid_import_kwh),
                    (a.grid_export_kwh, b.grid_export_kwh),
                    (a.charge_kwh, b.charge_kwh),
                    (a.discharge_kwh, b.discharge_kwh),
                )
            )
        ),
        "grid_difference_kwh": sum(
            abs(a.grid_import_kwh - b.grid_import_kwh)
            + abs(a.grid_export_kwh - b.grid_export_kwh)
            for a, b in pairs
        ),
        "end_soc_difference_kwh": limited.flows[-1].end_soc_kwh
        - proven.flows[-1].end_soc_kwh,
    }


def _compare_to_proof(
    problem: Problem, limited: Plan, outcome: dict, proof_limit_s: float
) -> dict:
    """Re-solve a time-limited plan strictly and report what the limit cost."""
    proven_plan, proven = _solve_once(
        problem, time_limit_s=proof_limit_s, accept_incumbent=False
    )
    comparison = {
        key: proven[key]
        for key in ("result", "cpu_s", "objective", "weighted_objective", "mip_gap")
        if key in proven
    }
    if proven_plan is not None:
        weighted = proven["weighted_objective"]
        comparison.update(
            {
                "weighted_excess": outcome["weighted_objective"] - weighted,
                # Relative to the incumbent, as HiGHS defines `mip_gap`, so it can be
                # compared with `mip_gap` directly (the excess never exceeds it).
                "weighted_excess_relative": (outcome["weighted_objective"] - weighted)
                / abs(outcome["weighted_objective"])
                if outcome["weighted_objective"]
                else None,
                "objective_difference": limited.objective - proven_plan.objective,
                **_plan_difference(limited, proven_plan),
            }
        )
    return comparison


def run_one(
    strategy: Strategy,
    *,
    slots: int,
    time_limit_s: float,
    battery_export: bool,
    probes: int,
    balance: bool,
    accept_incumbent: bool = False,
    proof_limit_s: float | None = None,
) -> dict:
    problem = make_problem(
        slots, strategy=strategy, allow_battery_export=battery_export
    )
    if balance and problem.battery:
        windows = candidate_windows(
            problem.slots,
            phase="eligible",
            now=problem.slots[0].start,
            hold_minutes=60,
            remaining_minutes=0,
            grid_charge_ok=(True,) * len(problem.slots),
        )
        problem = replace(
            problem,
            balance_windows=windows,
            balance_threshold_kwh=0.99 * problem.battery.capacity_kwh,
            balance_miss_cost=0.5,
            balance_lift_slots=lift_slots(windows, problem.battery, len(problem.slots)),
        )
    plan, outcome = _solve_once(
        problem, time_limit_s=time_limit_s, accept_incumbent=accept_incumbent
    )
    elapsed_s = outcome["elapsed_s"]
    if plan is not None and plan.time_limited and proof_limit_s is not None:
        outcome["proven"] = _compare_to_proof(problem, plan, outcome, proof_limit_s)
    probe_elapsed_s = 0.0
    probe_requested = 0
    probe_completed = 0
    classification_mode = None
    coverage_reason = None
    if plan is not None and probes > 0:
        compass = _compass_settings_for_probes(probes)
        probe_started = perf_counter()
        analysis = analyze_consumption(
            problem,
            plan,
            settings=compass,
            budget_s=max(0.0, _TOTAL_TIME_LIMIT_S - elapsed_s),
        )
        probe_elapsed_s = perf_counter() - probe_started
        probe_requested = len(analysis.opportunities)
        probe_completed = sum(
            1 for o in analysis.opportunities if o.cost_per_kwh is not None
        )
        classification_mode = analysis.classification_mode
        coverage_reason = analysis.coverage_reason
    outcome.update(
        {
            "strategy": strategy,
            "battery_export": battery_export,
            "balance": balance,
            "slots": slots,
            "time_limit_s": time_limit_s,
            "accept_incumbent": accept_incumbent,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "probe_requested": probe_requested,
            "probe_completed": probe_completed,
            "probe_elapsed_s": probe_elapsed_s,
            "classification_mode": classification_mode,
            "coverage_reason": coverage_reason,
        }
    )
    return outcome


def _accepted(outcome: dict) -> bool:
    return (
        outcome["result"] in ("optimal", "time_limited")
        and outcome["probe_completed"] == outcome["probe_requested"]
        and outcome["elapsed_s"] + outcome["probe_elapsed_s"] <= _TOTAL_TIME_LIMIT_S
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slots", type=int, choices=(48, 96, 192, 384), default=192)
    parser.add_argument("--time-limit", type=float, default=10.0)
    parser.add_argument("--strategy", choices=(*STRATEGIES, "all"), default="cost_min")
    parser.add_argument("--battery-export", action="store_true")
    parser.add_argument("--probes", type=int, default=0)
    parser.add_argument("--balance", action="store_true")
    parser.add_argument(
        "--accept-incumbent",
        action="store_true",
        help="return the incumbent at the time limit, as the runtime does for the base plan",
    )
    parser.add_argument(
        "--compare-proven",
        type=float,
        metavar="SECONDS",
        help="re-solve every time-limited plan strictly with this limit and report the difference",
    )
    args = parser.parse_args()

    strategies = STRATEGIES if args.strategy == "all" else (args.strategy,)
    outcomes = [
        run_one(
            strategy,
            slots=args.slots,
            time_limit_s=args.time_limit,
            battery_export=args.battery_export,
            probes=args.probes,
            balance=args.balance,
            accept_incumbent=args.accept_incumbent,
            proof_limit_s=args.compare_proven,
        )
        for strategy in strategies
    ]
    for outcome in outcomes:
        print(json.dumps(outcome, sort_keys=True))
    if not all(_accepted(outcome) for outcome in outcomes):
        raise SystemExit(
            "acceptance rule failed for at least one strategy "
            f"(result == 'optimal' or 'time_limited', probe_completed == probe_requested, "
            f"elapsed_s + probe_elapsed_s <= {_TOTAL_TIME_LIMIT_S})"
        )


if __name__ == "__main__":
    main()
