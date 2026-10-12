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
    python tools/replay.py tools/replay_sample.json --set allow_grid_charge=false
"""

import argparse
import csv
import json
import math
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
from custom_components.energy_compass.engine.peak_reserve import (
    peak_reserve_targets,
    peak_reserve_weight,
)
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
_UNSUPPORTED = (("lfp_balance", "LFP balance windows"),)
_VALUES = ("buy", "sell", "pv_forecast", "load_forecast", "pv", "load")
_NON_NEGATIVE = ("pv_forecast", "load_forecast", "pv", "load")


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


def _number(raw, where: str, name: str, *, non_negative: bool = False) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as err:
        raise ReplayError(f"{where}: {name} must be a number, got {raw!r}") from err
    if not math.isfinite(value):
        raise ReplayError(f"{where}: {name} must be finite, got {raw!r}")
    if non_negative and value < 0:
        raise ReplayError(f"{where}: {name} must not be negative, got {raw!r}")
    return value


def _interval(start: datetime, row, where: str) -> Interval:
    if not isinstance(row, dict):
        raise ReplayError(f"{where}: expected an object with {', '.join(_VALUES)}")
    return Interval(
        start,
        *(
            _number(row.get(name), where, name, non_negative=name in _NON_NEGATIVE)
            for name in _VALUES
        ),
    )


def _aware(text: str, zone: ZoneInfo, where: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except (TypeError, ValueError) as err:
        raise ReplayError(f"{where}: bad timestamp {text!r}") from err
    return value if value.tzinfo else value.replace(tzinfo=zone)


def _zone(name) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as err:
        raise ReplayError(f"unknown timezone {name!r}") from err


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


def _object(value, where: str, key: str) -> dict:
    if not isinstance(value, dict):
        raise ReplayError(f"{where}: {key} must be an object")
    return value


def _list(value, where: str, key: str) -> list:
    if not isinstance(value, list):
        raise ReplayError(f"{where}: {key} must be a list")
    return value


def load_json(path: Path) -> tuple[list[Day], dict, str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ReplayError("the JSON root must be an object")
    zone_name = document.get("timezone", "UTC")
    zone = _zone(zone_name)
    minutes = _number(document.get("interval_minutes", 15), "file", "interval_minutes")
    if minutes <= 0:
        raise ReplayError("file: interval_minutes must be positive")
    file_soc = document.get("initial_soc_percent", 50)
    days = []
    for number, entry in enumerate(_list(document.get("days", []), "file", "days"), 1):
        where = f"days[{number}]"
        entry = _object(entry, where, "the day")
        start = _aware(entry.get("start"), zone, where)
        rows = _list(entry.get("intervals", []), where, "intervals")
        intervals = tuple(
            _interval(
                start + timedelta(minutes=minutes * index), row, f"{where}[{index}]"
            )
            for index, row in enumerate(rows)
        )
        soc = _number(
            entry.get("initial_soc_percent", file_soc), where, "initial_soc_percent"
        )
        date = entry.get("date") or start.astimezone(zone).date().isoformat()
        days.append(_check_day(Day(str(date), soc, intervals)))
    settings = _object(document.get("settings", {}), "file", "settings")
    return days, settings, zone_name


def load_csv(
    path: Path, zone_name: str, initial_soc: float
) -> tuple[list[Day], dict, str]:
    zone = _zone(zone_name)
    by_date: dict[str, list[Interval]] = {}
    socs: dict[str, float] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for number, row in enumerate(csv.DictReader(handle), 2):
            where = f"line {number}"
            start = _aware(row.get("start"), zone, where)
            date = start.astimezone(zone).date().isoformat()
            by_date.setdefault(date, []).append(_interval(start, row, where))
            if row.get("initial_soc_percent") and date not in socs:
                socs[date] = _number(
                    row["initial_soc_percent"], where, "initial_soc_percent"
                )
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


def _peak_reserve(values: dict, slots, battery: Battery):
    """Entry floor before each expensive run, built from the slots at hand.

    Mirrors runtime.py: needs `limit_grid_charge_price` (its ceiling is the cheap
    price) and stays inactive when no expensive run with a deficit is in view.
    """
    margin = values["peak_reserve_margin_kwh"]
    if margin <= 0 or not values["limit_grid_charge_price"]:
        return (), (), 0.0
    buy = tuple(slot.buy_per_kwh for slot in slots)
    cheap_price = values["maximum_grid_charge_price"]
    peak = peak_reserve_targets(
        buy,
        tuple(slot.load_kwh for slot in slots),
        tuple(slot.pv_kwh for slot in slots),
        cheap_price=cheap_price,
        reserve_kwh=battery.capacity_kwh * battery.minimum_soc_fraction,
        usable_capacity_kwh=battery.capacity_kwh * battery.maximum_soc_fraction,
        eta_discharge=battery.eta_discharge,
        margin_kwh=margin,
    )
    weight = peak_reserve_weight(buy, cheap_price=cheap_price)
    if weight > 0 and any(window != -1 for window in peak.window_ids):
        return peak.targets_kwh, peak.window_ids, weight
    return (), (), 0.0


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
    peak_kwh, peak_window, peak_weight = _peak_reserve(values, slots, battery)
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
        peak_reserve_kwh=peak_kwh,
        peak_reserve_window=peak_window,
        peak_reserve_weight=peak_weight,
        **weights,
    )


def realize(
    mode,
    flow: Flow | None,
    pv,
    load,
    hours,
    soc,
    values,
    export_room,
    forecast: tuple[float, float] | None = None,
) -> tuple[Flow, float]:
    """Execute one interval against realized PV/load; returns the flow and end SOC.

    Follows the controller (tools/deye_controller/build.py): the planned mode and
    quantities steer the battery, the realized PV and load decide everything else.
    CHARGE_PV and SELF_CONSUME (and every unplanned interval) follow the load: all
    PV surplus charges the battery, a deficit is covered from it. CHARGE_GRID
    follows the plan only for the grid share (planned charge minus the surplus
    the forecast `(pv, load)` promised); the realized surplus charges on top, so
    PV above forecast charges more and PV below forecast charges less, never a
    shortfall bought from the grid. DISCHARGE_GRID discharges the planned energy
    to the load and grid; HOLD parks the battery; CURTAIL curtails the planned PV
    energy. Site limits are applied last: export beyond the grid limit is
    curtailed, charge beyond the inverter or grid import limit is dropped.
    Without `forecast` the forecast equals the realized values.
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
        forecast_pv, forecast_load = forecast or (pv, load)
        planned_curtail = flow.curtail_kwh if flow else 0.0
        forecast_surplus = max(0.0, forecast_pv - planned_curtail - forecast_load)
        grid_share = max(0.0, plan_charge - forecast_surplus)
        charge = min(max(net, 0.0) + grid_share, max_charge)
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


def soc_price(day: Day) -> float:
    """Price of the day-end SOC drift in the reported cost: the day's mean buy price.

    Fixed by the data, never by a planner setting, so runs with different
    `terminal_mode` / `terminal_value_per_kwh` stay comparable.
    """
    return sum(interval.buy for interval in day.intervals) / len(day.intervals)


def _outcome(day: Day, values: dict, flows, failures: int = 0) -> Outcome:
    """Sum a day's executed flows and value the SOC drift from the initial SOC."""
    initial = values["capacity_kwh"] * day.initial_soc_percent / 100
    end = flows[-1].end_soc_kwh
    cost = sum(
        flow_cost(interval, flow, values["wear_per_kwh"])
        for interval, flow in zip(day.intervals, flows, strict=True)
    )
    return Outcome(
        cost + (initial - end) * soc_price(day),
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
        except SolveError:
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
                (interval.pv_forecast, interval.load_forecast),
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
    """Settings the solves of one day use, with the day-end stored energy valued.

    A day boundary is arbitrary for the battery, so `preserve_initial` would forbid
    using stored energy in the last hours of every day. Each solve values the
    end-of-day SOC at `terminal_value_per_kwh` when the terminal mode is `value`,
    else at the day's mean buy price. The reported cost does not follow this
    choice: it always prices the SOC drift at `soc_price`.
    """
    if values["terminal_mode"] == "value":
        return values
    return {
        **values,
        "terminal_mode": "value",
        "terminal_value_per_kwh": soc_price(day),
    }


def replay(days: list[Day], values: dict, zone_name: str) -> list[DayResult]:
    results = []
    for day in days:
        valued = day_values(day, values)
        try:
            results.append(
                DayResult(
                    day.date,
                    run_plan(day, valued, zone_name),
                    run_self_use(day, valued),
                    run_hindsight(day, valued, zone_name),
                )
            )
        except (SolveError, InputError) as err:
            raise ReplayError(f"{day.date}: {err}") from err
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
        if (
            values["peak_reserve_margin_kwh"] > 0
            and not values["limit_grid_charge_price"]
        ):
            print(
                "warning: peak reserve needs limit_grid_charge_price (its ceiling is "
                "the cheap price); it has no effect (peak_reserve_margin_kwh)",
                file=sys.stderr,
            )
        results = replay(days, values, zone_name)
    except (
        ReplayError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        csv.Error,
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
                        "solve_failures": r.plan.solve_failures,
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
