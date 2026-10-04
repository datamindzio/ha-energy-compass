from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isfinite
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import STRATEGIES, BalanceWindow, InputError, Problem


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    value: float


def finite(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as err:
        raise InputError(f"{name} must be numeric") from err
    if not isfinite(number):
        raise InputError(f"{name} must be finite")
    return number


def aware(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InputError(f"{name} must be timezone-aware")
    return value


def rce_to_kwh(value: float, unit: str) -> float:
    price = finite(value, "price")
    if unit in ("PLN/MWh", "EUR/MWh"):
        return price / 1000
    if unit in ("PLN/kWh", "EUR/kWh"):
        return price
    raise InputError(f"unsupported price unit: {unit}")


def _validated(intervals: tuple[Interval, ...]) -> tuple[Interval, ...]:
    ordered = sorted(intervals, key=lambda row: row.start)
    for index, row in enumerate(ordered):
        aware(row.start, "interval start")
        aware(row.end, "interval end")
        finite(row.value, "interval value")
        if row.end <= row.start:
            raise InputError("interval end must follow start")
        if index and row.start < ordered[index - 1].end:
            raise InputError("overlapping source intervals")
    return tuple(ordered)


def _resample(
    intervals: tuple[Interval, ...],
    slots: tuple[tuple[datetime, datetime], ...],
    kind: str,
) -> tuple[float, ...]:
    source = _validated(intervals)
    result = []
    for start, end in slots:
        aware(start, "slot start")
        aware(end, "slot end")
        if end <= start:
            raise InputError("slot end must follow start")
        covered = 0.0
        total = 0.0
        prices = set()
        for row in source:
            overlap = (min(end, row.end) - max(start, row.start)).total_seconds()
            if overlap <= 0:
                continue
            covered += overlap
            if kind == "energy":
                total += row.value * overlap / (row.end - row.start).total_seconds()
            elif kind == "power":
                total += row.value * overlap / 3600
            else:
                prices.add(row.value)
        if abs(covered - (end - start).total_seconds()) > 1e-6:
            raise InputError("source coverage gap")
        if kind == "price":
            if len(prices) != 1:
                raise InputError("slot crosses different settlement prices")
            total = prices.pop()
        result.append(total)
    return tuple(result)


def resample_energy(
    intervals: tuple[Interval, ...], slots: tuple[tuple[datetime, datetime], ...]
) -> tuple[float, ...]:
    """Allocate interval energy to covered slots by elapsed time."""
    return _resample(intervals, slots, "energy")


def resample_power(
    intervals: tuple[Interval, ...], slots: tuple[tuple[datetime, datetime], ...]
) -> tuple[float, ...]:
    """Integrate interval-average power over covered slots."""
    return _resample(intervals, slots, "power")


def resample_prices(
    intervals: tuple[Interval, ...], slots: tuple[tuple[datetime, datetime], ...]
) -> tuple[float, ...]:
    """Require each requested slot to have one fully covered settlement price."""
    return _resample(intervals, slots, "price")


def validate_problem(problem: Problem) -> None:
    """Reject invalid physical, chronological, or nonfinite optimization inputs."""
    if not problem.slots:
        raise InputError("at least one slot is required")
    try:
        ZoneInfo(problem.timezone)
    except (ZoneInfoNotFoundError, TypeError) as err:
        raise InputError("invalid timezone") from err
    for name in ("inverter_kw", "grid_import_kw", "grid_export_kw"):
        if finite(getattr(problem.site, name), name) < 0:
            raise InputError(f"{name} must be nonnegative")
    if not isinstance(problem.site.allow_curtailment, bool):
        raise InputError("curtailment setting must be boolean")
    previous_end_utc = None
    for slot in problem.slots:
        aware(slot.start, "slot start")
        aware(slot.end, "slot end")
        start_utc = slot.start.astimezone(UTC)
        end_utc = slot.end.astimezone(UTC)
        if end_utc <= start_utc or (
            previous_end_utc is not None and start_utc != previous_end_utc
        ):
            raise InputError("slots must be contiguous and increasing")
        previous_end_utc = end_utc
        for name in ("buy_per_kwh", "sell_per_kwh", "pv_kwh", "load_kwh"):
            value = finite(getattr(slot, name), name)
            if name in ("pv_kwh", "load_kwh") and value < 0:
                raise InputError(f"{name} must be nonnegative")
    if problem.terminal_mode not in ("preserve_initial", "value"):
        raise InputError("invalid terminal mode")
    finite(problem.terminal_value_per_kwh, "terminal value")
    ceiling = problem.maximum_grid_charge_price
    if ceiling is not None and (
        isinstance(ceiling, bool)
        or not -1000 <= finite(ceiling, "maximum_grid_charge_price") <= 1000
    ):
        raise InputError("maximum_grid_charge_price must be in [-1000, 1000] or null")
    if (
        not 0
        <= finite(
            problem.minimum_export_episode_benefit, "minimum_export_episode_benefit"
        )
        <= 1000
    ):
        raise InputError("minimum_export_episode_benefit must be in [0, 1000]")
    if type(problem.initial_export_active) is not bool:
        raise InputError("initial_export_active must be boolean")
    if (
        not 0
        <= finite(
            problem.minimum_grid_charge_episode_benefit,
            "minimum_grid_charge_episode_benefit",
        )
        <= 1000
    ):
        raise InputError("minimum_grid_charge_episode_benefit must be in [0, 1000]")
    if type(problem.initial_grid_charge_active) is not bool:
        raise InputError("initial_grid_charge_active must be boolean")
    if problem.strategy not in STRATEGIES:
        raise InputError("invalid strategy")
    for name in (
        "import_weight",
        "export_weight",
        "import_kwh_weight",
        "battery_export_penalty_per_kwh",
        "soc_target_weight",
        "peak_import_weight",
        "cap_violation_weight",
    ):
        if finite(getattr(problem, name), name) < 0:
            raise InputError(f"{name} must be nonnegative")
    if finite(problem.pv_export_margin, "pv_export_margin") < 0:
        raise InputError("pv_export_margin must be nonnegative")
    for name in ("soft_import_cap_kw", "soft_export_cap_kw"):
        cap = getattr(problem, name)
        if cap is not None and (isinstance(cap, bool) or finite(cap, name) < 0):
            raise InputError(f"{name} must be nonnegative or null")
    if problem.soc_target_kwh:
        if len(problem.soc_target_kwh) != len(problem.slots):
            raise InputError("soc_target_kwh must cover every slot")
        for value in problem.soc_target_kwh:
            if finite(value, "soc_target_kwh") < 0:
                raise InputError("soc_target_kwh must cover every slot")
    if bool(problem.soc_target_window) != bool(problem.soc_target_kwh):
        raise InputError("soc_target_window must be contiguous and non-decreasing")
    if problem.soc_target_window:
        if len(problem.soc_target_window) != len(problem.soc_target_kwh):
            raise InputError("soc_target_window must be contiguous and non-decreasing")
        seen: set[int] = set()
        previous_id = None
        for window_id in problem.soc_target_window:
            if isinstance(window_id, bool) or not isinstance(window_id, int):
                raise InputError(
                    "soc_target_window must be contiguous and non-decreasing"
                )
            if window_id < 0:
                raise InputError(
                    "soc_target_window must be contiguous and non-decreasing"
                )
            if previous_id is not None and window_id < previous_id:
                raise InputError(
                    "soc_target_window must be contiguous and non-decreasing"
                )
            if window_id != previous_id:
                if window_id in seen:
                    raise InputError(
                        "soc_target_window must be contiguous and non-decreasing"
                    )
                seen.add(window_id)
            previous_id = window_id
    if type(problem.strategy_changed) is not bool:
        raise InputError("strategy_changed must be boolean")
    if type(problem.limit_export_to_pv) is not bool:
        raise InputError("PV export limit must be boolean")
    if problem.export_limit_scope not in ("local_day", "produced"):
        raise InputError("export_limit_scope must be 'local_day' or 'produced'")
    for name in ("pv_generated_today_kwh", "grid_exported_today_kwh"):
        if finite(getattr(problem, name), name) < 0:
            raise InputError(f"{name} must be nonnegative")
    if not 0 <= finite(problem.minimum_mode_minutes, "minimum_mode_minutes") <= 1440:
        raise InputError("minimum_mode_minutes must be in [0, 1440]")
    if (
        not 0.001
        <= finite(problem.minimum_mode_power_kw, "minimum_mode_power_kw")
        <= 1000
    ):
        raise InputError("minimum_mode_power_kw must be in [0.001, 1000]")
    if problem.initial_dispatch_mode not in (
        None,
        "CHARGE_GRID",
        "CHARGE_PV",
        "DISCHARGE_GRID",
        "SELF_CONSUME",
        "HOLD",
        "CURTAIL",
    ):
        raise InputError("invalid initial dispatch mode")
    if (problem.initial_dispatch_mode is None) != (
        problem.initial_dispatch_mode_since is None
    ):
        raise InputError("initial dispatch mode requires its start time")
    if problem.initial_dispatch_mode_since is not None:
        aware(problem.initial_dispatch_mode_since, "initial dispatch mode start")
        if problem.initial_dispatch_mode_since.astimezone(UTC) > problem.slots[
            0
        ].start.astimezone(UTC):
            raise InputError("initial dispatch mode starts in the future")
    if problem.initial_battery_mode not in (None, "charge", "discharge"):
        raise InputError("invalid initial battery mode")
    if (problem.initial_battery_mode is None) != (
        problem.initial_battery_mode_since is None
    ):
        raise InputError("initial battery mode requires its start time")
    if problem.initial_battery_mode_since is not None:
        aware(problem.initial_battery_mode_since, "initial battery mode start")
        if problem.initial_battery_mode_since.astimezone(UTC) > problem.slots[
            0
        ].start.astimezone(UTC):
            raise InputError("initial battery mode starts in the future")
    balance_active = bool(
        problem.balance_windows
        or problem.balance_threshold_kwh
        or problem.balance_fixed
        or problem.balance_lift_slots
    )
    if balance_active:
        if problem.battery is None:
            raise InputError("balance requires a battery")
        threshold = finite(problem.balance_threshold_kwh, "balance_threshold_kwh")
        if not 0 < threshold <= problem.battery.capacity_kwh:
            raise InputError("balance threshold must be within capacity")
        if finite(problem.balance_miss_cost, "balance_miss_cost") < 0:
            raise InputError("balance_miss_cost must be nonnegative")
        if type(problem.balance_fixed) is not bool:
            raise InputError("balance_fixed must be boolean")
        if problem.balance_fixed and len(problem.balance_windows) != 1:
            raise InputError("a fixed balance needs exactly one window")
        count = len(problem.slots)
        for window in problem.balance_windows:
            if (
                not isinstance(window, BalanceWindow)
                or window.mode not in ("CHARGE_PV", "CHARGE_GRID")
                or not window.slots
                or any(type(i) is not int for i in window.slots)
                or window.slots
                != tuple(range(window.slots[0], window.slots[0] + len(window.slots)))
                or window.slots[0] < 0
                or window.slots[-1] >= count
            ):
                raise InputError("invalid balance window")
        if any(
            type(i) is not int or not 0 <= i < count for i in problem.balance_lift_slots
        ):
            raise InputError("invalid balance lift slot")
    if problem.battery is not None:
        battery = problem.battery
        if finite(battery.capacity_kwh, "capacity_kwh") <= 0:
            raise InputError("capacity_kwh must be positive")
        for name in ("charge_kw", "discharge_kw"):
            if finite(getattr(battery, name), name) < 0:
                raise InputError(f"{name} must be nonnegative")
        for name in ("maximum_soc_fraction", "eta_charge", "eta_discharge"):
            value = finite(getattr(battery, name), name)
            if not 0 < value <= 1:
                raise InputError(f"{name} must be in (0, 1]")
        floor = finite(battery.minimum_soc_fraction, "minimum_soc_fraction")
        if not 0 <= floor < 1:
            raise InputError("battery floor must be in [0, 1)")
        if battery.minimum_soc_fraction >= battery.maximum_soc_fraction:
            raise InputError("battery floor must be below ceiling")
        initial = finite(battery.initial_kwh, "initial_kwh")
        # The reserve limits planned discharge, not valid observed depletion.
        ceiling = (
            battery.capacity_kwh
            if problem.balance_threshold_kwh
            else battery.capacity_kwh * battery.maximum_soc_fraction
        )
        if not 0 <= initial <= ceiling:
            raise InputError("initial SOC outside bounds")
        if finite(battery.wear_per_kwh, "wear_per_kwh") < 0:
            raise InputError("wear cost must be nonnegative")
        if finite(battery.idle_drain_kw, "idle_drain_kw") < 0:
            raise InputError("idle drain must be nonnegative")
        if not isinstance(battery.allow_grid_charge, bool) or not isinstance(
            battery.allow_battery_export, bool
        ):
            raise InputError("battery capabilities must be boolean")
    for day, value in problem.remaining_daily_throughput_kwh:
        try:
            canonical = date.fromisoformat(day).isoformat()
        except (TypeError, ValueError) as err:
            raise InputError("invalid throughput date") from err
        if day != canonical:
            raise InputError("invalid throughput date")
        if finite(value, "remaining throughput") < 0:
            raise InputError("remaining throughput must be nonnegative")
