"""Measure deterministic advisory solve workloads in the target runtime image.

Invoked by path (`python benchmarks/optimizer.py`) because the test container's
entrypoint is bare `python`, which only puts this script's own directory on
`sys.path`, not the repository root `custom_components` lives under.
"""

import argparse
import json
import resource
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import pi, sin
from pathlib import Path
from time import perf_counter

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


def run_one(
    strategy: Strategy,
    *,
    slots: int,
    time_limit_s: float,
    battery_export: bool,
    probes: int,
    balance: bool,
    minimum_mode_minutes: float | None = None,
) -> dict:
    problem = make_problem(
        slots, strategy=strategy, allow_battery_export=battery_export
    )
    if minimum_mode_minutes is not None:
        problem = replace(problem, minimum_mode_minutes=minimum_mode_minutes)
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
    original_model_solve = optimize._Model.solve
    captured: dict[str, int] = {}

    def _spy(self, time_limit):
        captured["variable_count"] = len(self.cost)
        captured["row_count"] = len(self.rows)
        return original_model_solve(self, time_limit)

    optimize._Model.solve = _spy
    started = perf_counter()
    try:
        plan = solve(problem, time_limit_s=time_limit_s)
    except SolveError as err:
        outcome = {"result": err.reason}
        plan = None
    else:
        outcome = {
            "result": "optimal",
            "objective": plan.objective,
            "flow_count": len(plan.flows),
        }
    finally:
        optimize._Model.solve = original_model_solve
    elapsed_s = perf_counter() - started
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
            "elapsed_s": elapsed_s,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "variable_count": captured.get("variable_count", 0),
            "row_count": captured.get("row_count", 0),
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
        outcome["result"] == "optimal"
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
        "--minimum-mode-minutes",
        type=float,
        help="override the shipped minimum mode duration (0 measures the unmoded rules)",
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
            minimum_mode_minutes=args.minimum_mode_minutes,
        )
        for strategy in strategies
    ]
    for outcome in outcomes:
        print(json.dumps(outcome, sort_keys=True))
    if not all(_accepted(outcome) for outcome in outcomes):
        raise SystemExit(
            "acceptance rule failed for at least one strategy "
            f"(result == 'optimal', probe_completed == probe_requested, "
            f"elapsed_s + probe_elapsed_s <= {_TOTAL_TIME_LIMIT_S})"
        )


if __name__ == "__main__":
    main()
