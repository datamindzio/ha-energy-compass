"""Replay recorded days offline: Energy Compass plan vs self-consumption vs hindsight.

No Home Assistant needed; the optimizer in `custom_components/energy_compass/engine`
is imported directly. For every day in the input file the tool reports, in the
installation's currency:

    plan         receding horizon: re-solve every `refresh_minutes` on the forecast
                 known at plan time, execute the plan against realized PV and load
    self_use     plain self-consumption: battery follows the load, no grid charge,
                 no battery export
    hindsight    one solve of the same objective on the realized PV and load

Use `--set key=value` (repeatable) to override a planner setting, so two runs can
be compared. The input format is described in docs/guide.en.md#offline-replay-tool.

    python tools/replay.py tools/replay_sample.json
    python tools/replay.py tools/replay_sample.json --set import_penalty_per_kwh=0.1
"""

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.energy_compass.engine.autonomy import (
    autonomy_targets,
    autonomy_weight,
    backup_floor_kwh,
)
from custom_components.energy_compass.engine.dispatch_policy import (
    dwell_group,
)
from custom_components.energy_compass.engine.machine_state import (
    machine_state,
)
from custom_components.energy_compass.engine.models import (
    Battery,
    Flow,
    InputError,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import solve
from custom_components.energy_compass.engine.strategy import (
    STRATEGY_OWNED_KEYS,
    resolve_flags,
    strategy_weights,
)
from custom_components.energy_compass.settings import (
    BOOLEANS,
    CHOICES,
    NUMBERS,
)

_TOL = 1e-9
_HINDSIGHT_TIME_LIMIT_S = 60.0
_UNSUPPORTED = (
    ("lfp_balance", "LFP balance windows"),
    ("peak_reserve_margin_kwh", "peak reserve"),
)


class ReplayError(ValueError):
    """The input file or an override is not usable."""


@dataclass(frozen=True)
class Interval:
    start: datetime
    buy: float
    sell: float
    pv_forecast: float
    load_forecast: float
    pv: float
    load: float


@dataclass(frozen=True)
class Day:
    date: str
    initial_soc_percent: float
    intervals: tuple[Interval, ...]

    @property
    def minutes(self) -> float:
        first, second = self.intervals[0].start, self.intervals[1].start
        return (second - first).total_seconds() / 60


@dataclass(frozen=True)
class Outcome:
    cost: float
    import_kwh: float
    export_kwh: float
    end_soc_kwh: float
    solve_failures: int = 0


def default_settings() -> dict:
    values = {key: spec[1] for key, spec in NUMBERS.items()}
    values.update({key: spec[1] for key, spec in BOOLEANS.items()})
    values.update({key: spec[1][0] for key, spec in CHOICES.items()})
    return values


def _coerce(key: str, raw):
    if key in BOOLEANS:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
        raise ReplayError(f"{key} must be true or false, got {raw!r}")
    if key in CHOICES:
        if raw not in CHOICES[key][1]:
            raise ReplayError(f"{key} must be one of {CHOICES[key][1]}, got {raw!r}")
        return raw
    if key in NUMBERS:
        try:
            value = float(raw)
        except (TypeError, ValueError) as err:
            raise ReplayError(f"{key} must be a number, got {raw!r}") from err
        low, high = NUMBERS[key][2:4]
        if not low <= value <= high:
            raise ReplayError(f"{key} must be within {low}..{high}, got {value}")
        return value
    raise ReplayError(f"unknown setting {key!r}")


def resolve_settings(file_settings: dict, overrides: dict) -> tuple[dict, set]:
    """Defaults, then file settings, then overrides; strategy bundle applied last."""
    values = default_settings()
    explicit = set()
    for source in (file_settings, overrides):
        for key, raw in source.items():
            values[key] = _coerce(key, raw)
            if key in STRATEGY_OWNED_KEYS:
                explicit.add(key)
    values.update(resolve_flags(values["strategy"], values, explicit))
    return values, explicit


def parse_overrides(items) -> dict:
    overrides = {}
    for item in items:
        key, separator, raw = item.partition("=")
        if not separator or not key:
            raise ReplayError(f"--set expects key=value, got {item!r}")
        overrides[key.strip()] = raw.strip()
    return overrides


def _interval(start: datetime, row: dict, where: str) -> Interval:
    try:
        return Interval(
            start,
            float(row["buy"]),
            float(row["sell"]),
            float(row["pv_forecast"]),
            float(row["load_forecast"]),
            float(row["pv"]),
            float(row["load"]),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise ReplayError(f"{where}: bad or missing value ({err})") from err


def _aware(text: str, zone: ZoneInfo, where: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except (TypeError, ValueError) as err:
        raise ReplayError(f"{where}: bad timestamp {text!r}") from err
    return value if value.tzinfo else value.replace(tzinfo=zone)


def _check_day(day: Day) -> Day:
    if len(day.intervals) < 2:
        raise ReplayError(f"{day.date}: at least two intervals are required")
    step = day.intervals[1].start - day.intervals[0].start
    if step <= timedelta(0) or any(
        later.start - earlier.start != step
        for earlier, later in zip(day.intervals, day.intervals[1:])
    ):
        raise ReplayError(f"{day.date}: intervals must be contiguous and equal")
    if not 0 <= day.initial_soc_percent <= 100:
        raise ReplayError(f"{day.date}: initial_soc_percent must be within 0..100")
    return day


def load_json(path: Path) -> tuple[list[Day], dict, str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    zone_name = document.get("timezone", "UTC")
    zone = ZoneInfo(zone_name)
    minutes = float(document.get("interval_minutes", 15))
    days = []
    for number, entry in enumerate(document.get("days", []), 1):
        where = f"days[{number}]"
        start = _aware(entry.get("start"), zone, where)
        intervals = tuple(
            _interval(
                start + timedelta(minutes=minutes * index), row, f"{where}[{index}]"
            )
            for index, row in enumerate(entry.get("intervals", []))
        )
        days.append(
            _check_day(
                Day(
                    entry.get("date") or start.astimezone(zone).date().isoformat(),
                    float(
                        entry.get(
                            "initial_soc_percent",
                            document.get("initial_soc_percent", 50),
                        )
                    ),
                    intervals,
                )
            )
        )
    return days, document.get("settings", {}), zone_name


def load_csv(
    path: Path, zone_name: str, initial_soc: float
) -> tuple[list[Day], dict, str]:
    zone = ZoneInfo(zone_name)
    by_date: dict[str, list[Interval]] = {}
    socs: dict[str, float] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for number, row in enumerate(csv.DictReader(handle), 2):
            start = _aware(row.get("start"), zone, f"line {number}")
            date = start.astimezone(zone).date().isoformat()
            by_date.setdefault(date, []).append(_interval(start, row, f"line {number}"))
            if row.get("initial_soc_percent") and date not in socs:
                socs[date] = float(row["initial_soc_percent"])
    days = [
        _check_day(Day(date, socs.get(date, initial_soc), tuple(rows)))
        for date, rows in by_date.items()
    ]
    return days, {}, zone_name


def _autonomy(values: dict, slots, battery: Battery, zone_name: str):
    """Autonomy / backup floor over the slots at hand (no tail beyond the data)."""
    if not values["autonomy_reserve"] or battery is None:
        return (), (), 0.0
    floor = autonomy_targets(
        tuple(slot.start for slot in slots),
        tuple((slot.end - slot.start).total_seconds() / 3600 for slot in slots),
        tuple(slot.load_kwh for slot in slots),
        tuple(slot.pv_kwh for slot in slots),
        reserve_kwh=battery.capacity_kwh * battery.minimum_soc_fraction,
        usable_capacity_kwh=battery.capacity_kwh * battery.maximum_soc_fraction,
        eta_discharge=battery.eta_discharge,
        timezone=zone_name,
    )
    targets = floor.targets_kwh[: len(slots)]
    weight = autonomy_weight(
        slots, margin=values["autonomy_margin_per_kwh"], timezone=zone_name
    )
    if values["strategy"] == "backup_ready":
        floor_kwh = backup_floor_kwh(
            battery.capacity_kwh, values["backup_target_soc_percent"]
        )
        targets = tuple(max(target, floor_kwh) for target in targets)
        weight = max(weight, values["backup_shortfall_price_per_kwh"])
    return targets, floor.window_ids[: len(slots)], weight


def build_problem(
    values: dict,
    slots: tuple[Slot, ...],
    soc_kwh: float,
    zone_name: str,
    *,
    used_throughput_kwh: float = 0.0,
    pv_generated_kwh: float = 0.0,
    exported_kwh: float = 0.0,
    mode: str | None = None,
    mode_since: datetime | None = None,
) -> Problem:
    battery = Battery(
        values["capacity_kwh"],
        values["operating_floor"] / 100,
        values["soc_ceiling"] / 100,
        soc_kwh,
        values["charge_kw"],
        values["discharge_kw"],
        values["eta_charge"],
        values["eta_discharge"],
        values["wear_per_kwh"],
        values["allow_grid_charge"],
        values["allow_battery_export"],
        values["idle_drain_kw"],
    )
    budgets = ()
    if values["daily_cycles"]:
        cap = 2 * values["capacity_kwh"] * values["daily_cycles"]
        zone = ZoneInfo(zone_name)
        days = sorted({slot.start.astimezone(zone).date() for slot in slots})
        budgets = tuple(
            (
                day.isoformat(),
                max(0.0, cap - used_throughput_kwh) if index == 0 else cap,
            )
            for index, day in enumerate(days)
        )
    weights = strategy_weights(
        values["strategy"],
        values,
        max_abs_buy_per_kwh=max(abs(slot.buy_per_kwh) for slot in slots),
        site_import_kw=values["grid_import_kw"],
        site_export_kw=values["grid_export_kw"],
    )
    targets, windows, target_weight = _autonomy(values, slots, battery, zone_name)
    return Problem(
        slots,
        SiteLimits(
            values["inverter_kw"],
            values["grid_import_kw"],
            values["grid_export_kw"],
            values["allow_curtailment"],
        ),
        battery,
        values["terminal_mode"],
        values["terminal_value_per_kwh"],
        budgets,
        zone_name,
        minimum_mode_minutes=values["minimum_mode_minutes"],
        limit_export_to_pv=values["limit_export_to_pv"],
        export_limit_scope=values["export_limit_scope"],
        pv_generated_today_kwh=pv_generated_kwh,
        grid_exported_today_kwh=exported_kwh,
        minimum_mode_power_kw=values["minimum_mode_power_kw"],
        initial_dispatch_mode=mode,
        initial_dispatch_mode_since=mode_since,
        minimum_export_episode_benefit=values["minimum_export_episode_benefit"],
        minimum_grid_charge_episode_benefit=values[
            "minimum_grid_charge_episode_benefit"
        ],
        maximum_grid_charge_price=values["maximum_grid_charge_price"]
        if values["limit_grid_charge_price"]
        else None,
        strategy=values["strategy"],
        soc_target_kwh=targets,
        soc_target_window=windows,
        soc_target_weight=target_weight,
        **weights,
    )


def realize(
    mode, flow: Flow | None, pv, load, hours, soc, values, export_room
) -> tuple[Flow, float]:
    """Execute one interval against realized PV/load; returns the flow and end SOC.

    The planned mode and quantities steer the battery, the realized PV and load
    decide everything else. CHARGE_GRID charges the planned energy from PV first,
    grid second; CHARGE_PV charges from realized PV surplus up to the planned
    energy; DISCHARGE_GRID discharges the planned energy to the load and grid;
    HOLD parks the battery; CURTAIL curtails the planned PV energy; SELF_CONSUME
    (and every unplanned interval) follows the load. Site limits are applied last:
    export beyond the grid limit is curtailed, charge beyond the inverter or grid
    import limit is dropped.
    """
    eta_c, eta_d = values["eta_charge"], values["eta_discharge"]
    capacity = values["capacity_kwh"]
    floor = capacity * values["operating_floor"] / 100
    ceiling = capacity * values["soc_ceiling"] / 100
    drain = min(values["idle_drain_kw"] * hours, max(soc, 0.0))
    max_charge = min(
        values["charge_kw"] * hours, max(0.0, (ceiling - soc + drain) / eta_c)
    )
    max_discharge = min(
        values["discharge_kw"] * hours, max(0.0, (soc - drain - floor) * eta_d)
    )
    plan_charge = flow.charge_kwh if flow else 0.0
    plan_discharge = flow.discharge_kwh if flow else 0.0
    curtail = min(flow.curtail_kwh, pv) if flow and mode == "CURTAIL" else 0.0
    net = pv - curtail - load
    charge = discharge = 0.0
    if mode == "CHARGE_GRID":
        charge = min(plan_charge, max_charge)
    elif mode == "CHARGE_PV":
        charge = min(plan_charge, max(net, 0.0), max_charge)
    elif mode == "DISCHARGE_GRID":
        discharge = min(plan_discharge, max_discharge)
    elif mode != "HOLD":
        charge = min(max(net, 0.0), max_charge)
        discharge = min(max(-net, 0.0), max_discharge)

    inverter = values["inverter_kw"] * hours
    import_cap = values["grid_import_kw"] * hours
    export_cap = values["grid_export_kw"] * hours

    def ac() -> float:
        return pv - curtail + discharge - charge

    if ac() < -inverter:
        charge = max(0.0, charge - (-inverter - ac()))
    if ac() > inverter:
        curtail = min(pv, curtail + ac() - inverter)
        if ac() > inverter:
            discharge = max(0.0, discharge - (ac() - inverter))

    def balance() -> float:
        return pv - curtail + discharge - charge - load

    if balance() > export_cap:
        curtail = min(pv, curtail + balance() - export_cap)
    if values["limit_export_to_pv"]:
        # Stored energy never leaves the site beyond what PV has produced so far.
        excess = max(balance(), 0.0) + curtail - export_room
        if excess > 0 and discharge > 0:
            discharge = max(0.0, discharge - excess)
    if -balance() > import_cap and charge > 0:
        charge = max(0.0, charge - (-balance() - import_cap))
    surplus = balance()
    end = soc + charge * eta_c - discharge / eta_d - drain
    return (
        Flow(
            max(-surplus, 0.0),
            max(surplus, 0.0),
            charge,
            discharge,
            curtail,
            end,
        ),
        end,
    )


def flow_cost(interval: Interval, flow: Flow, wear_per_kwh: float) -> float:
    return (
        interval.buy * flow.grid_import_kwh
        - interval.sell * flow.grid_export_kwh
        + wear_per_kwh * (flow.charge_kwh + flow.discharge_kwh) / 2
    )


def _outcome(day: Day, values: dict, flows, failures: int = 0) -> Outcome:
    """Sum a day's executed flows and value the SOC drift from the initial SOC."""
    initial = values["capacity_kwh"] * day.initial_soc_percent / 100
    end = flows[-1].end_soc_kwh
    cost = sum(
        flow_cost(interval, flow, values["wear_per_kwh"])
        for interval, flow in zip(day.intervals, flows, strict=True)
    )
    return Outcome(
        cost + (initial - end) * values["terminal_value_per_kwh"],
        sum(flow.grid_import_kwh for flow in flows),
        sum(flow.grid_export_kwh for flow in flows),
        end,
        failures,
    )


def _slots(day: Day, begin: int, count: int, realized: bool) -> tuple[Slot, ...]:
    step = timedelta(minutes=day.minutes)
    return tuple(
        Slot(
            interval.start,
            interval.start + step,
            interval.buy,
            interval.sell,
            interval.pv if realized else interval.pv_forecast,
            interval.load if realized else interval.load_forecast,
        )
        for interval in day.intervals[begin : begin + count]
    )


def run_self_use(day: Day, values: dict) -> Outcome:
    hours = day.minutes / 60
    soc = values["capacity_kwh"] * day.initial_soc_percent / 100
    flows, produced, exported = [], 0.0, 0.0
    for interval in day.intervals:
        produced += interval.pv
        flow, soc = realize(
            "SELF_CONSUME",
            None,
            interval.pv,
            interval.load,
            hours,
            soc,
            values,
            produced - exported,
        )
        exported += flow.grid_export_kwh + flow.curtail_kwh
        flows.append(flow)
    return _outcome(day, values, flows)


def run_plan(day: Day, values: dict, zone_name: str) -> Outcome:
    hours = day.minutes / 60
    count = len(day.intervals)
    horizon = max(1, int(values["horizon_hours"] * 60 / day.minutes))
    refresh = max(1, round(values["refresh_minutes"] / day.minutes))
    soc = values["capacity_kwh"] * day.initial_soc_percent / 100
    flows: list[Flow] = []
    produced = exported = throughput = 0.0
    mode = mode_since = None
    failures = 0
    index = 0
    while index < count:
        window = min(horizon, count - index)
        problem = build_problem(
            values,
            _slots(day, index, window, realized=False),
            soc,
            zone_name,
            used_throughput_kwh=throughput,
            pv_generated_kwh=produced,
            exported_kwh=exported,
            mode=mode if values["minimum_mode_minutes"] else None,
            mode_since=mode_since if values["minimum_mode_minutes"] else None,
        )
        try:
            plan = solve(
                problem,
                time_limit_s=values["solve_time_limit_s"],
                accept_incumbent=True,
            )
            planned = plan.flows
        except SolveError, InputError:
            failures += 1
            planned = ()
        for step in range(min(refresh, count - index)):
            interval = day.intervals[index + step]
            planned_flow = planned[step] if step < len(planned) else None
            planned_mode = (
                machine_state(problem.slots[step], planned_flow)
                if planned_flow
                else None
            )
            produced += interval.pv
            flow, soc = realize(
                planned_mode or "SELF_CONSUME",
                planned_flow,
                interval.pv,
                interval.load,
                hours,
                soc,
                values,
                produced - exported,
            )
            exported += flow.grid_export_kwh + flow.curtail_kwh
            throughput += flow.charge_kwh + flow.discharge_kwh
            flows.append(flow)
            if planned_mode and (
                mode is None or dwell_group(planned_mode) != dwell_group(mode)
            ):
                mode, mode_since = planned_mode, interval.start
            elif planned_mode:
                mode = planned_mode
        index += refresh
    return _outcome(day, values, flows, failures)


def run_hindsight(day: Day, values: dict, zone_name: str) -> Outcome:
    soc = values["capacity_kwh"] * day.initial_soc_percent / 100
    problem = build_problem(
        values, _slots(day, 0, len(day.intervals), realized=True), soc, zone_name
    )
    plan = solve(problem, time_limit_s=_HINDSIGHT_TIME_LIMIT_S)
    return _outcome(day, values, plan.flows)


@dataclass(frozen=True)
class DayResult:
    date: str
    plan: Outcome
    self_use: Outcome
    hindsight: Outcome


def day_values(day: Day, values: dict) -> dict:
    """Settings for one day with the day-end stored energy valued.

    A day boundary is arbitrary for the battery, so `preserve_initial` would forbid
    using stored energy in the last hours of every day. Each solve and the final
    cost value the end-of-day SOC at `terminal_value_per_kwh` when the terminal mode
    is `value`, else at the day's mean buy price.
    """
    if values["terminal_mode"] == "value":
        return values
    mean_buy = sum(interval.buy for interval in day.intervals) / len(day.intervals)
    return {**values, "terminal_mode": "value", "terminal_value_per_kwh": mean_buy}


def replay(days: list[Day], values: dict, zone_name: str) -> list[DayResult]:
    results = []
    for day in days:
        valued = day_values(day, values)
        results.append(
            DayResult(
                day.date,
                run_plan(day, valued, zone_name),
                run_self_use(day, valued),
                run_hindsight(day, valued, zone_name),
            )
        )
    return results


def format_table(results: list[DayResult], currency: str) -> str:
    header = (
        f"{'date':<12}{'plan':>10}{'self_use':>10}{'hindsight':>11}"
        f"{'vs self_use':>13}{'vs hindsight':>14}  [{currency}]"
    )
    lines = [header]
    total = [0.0, 0.0, 0.0]
    failures = 0
    for result in results:
        costs = (result.plan.cost, result.self_use.cost, result.hindsight.cost)
        total = [t + c for t, c in zip(total, costs)]
        failures += result.plan.solve_failures
        lines.append(_row(result.date, costs))
    lines.append("-" * len(header))
    lines.append(_row("total", total))
    if failures:
        lines.append(
            f"note: {failures} plan solve(s) failed; those steps ran as self-consumption"
        )
    return "\n".join(lines)


def _row(label: str, costs) -> str:
    plan, self_use, hindsight = costs
    return (
        f"{label:<12}{plan:>10.3f}{self_use:>10.3f}{hindsight:>11.3f}"
        f"{plan - self_use:>+13.3f}{plan - hindsight:>+14.3f}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "input",
        type=Path,
        help="JSON or CSV file (see docs/guide.en.md#offline-replay-tool)",
    )
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="key=value",
        help="override a planner setting (repeatable)",
    )
    parser.add_argument("--timezone", default="UTC", help="CSV only (JSON has its own)")
    parser.add_argument(
        "--initial-soc",
        type=float,
        default=50.0,
        help="CSV only: initial SOC %% of days without an initial_soc_percent column",
    )
    parser.add_argument("--currency", default="")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    try:
        if args.input.suffix.lower() == ".csv":
            days, file_settings, zone_name = load_csv(
                args.input, args.timezone, args.initial_soc
            )
        else:
            days, file_settings, zone_name = load_json(args.input)
        if not days:
            raise ReplayError("the input has no days")
        values, _ = resolve_settings(file_settings, parse_overrides(args.overrides))
        for key, label in _UNSUPPORTED:
            if values[key]:
                print(f"warning: {label} is not replayed ({key})", file=sys.stderr)
        results = replay(days, values, zone_name)
    except (
        ReplayError,
        OSError,
        json.JSONDecodeError,
        ZoneInfoNotFoundError,
    ) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "date": r.date,
                        "plan": r.plan.cost,
                        "self_use": r.self_use.cost,
                        "hindsight": r.hindsight.cost,
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        print(format_table(results, args.currency or "currency"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
