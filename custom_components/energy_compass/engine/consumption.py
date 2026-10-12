"""Incremental consumption costs and advisory display queries."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import ceil, floor, isclose, isfinite
from time import perf_counter
from zoneinfo import ZoneInfo

from .machine_state import machine_state
from .models import (
    CompassSettings,
    FlexibleLoadRequest,
    Flow,
    InputError,
    Level,
    MachineState,
    Opportunity,
    Plan,
    Problem,
    Slot,
    SolveError,
    Window,
    plan_monetary_cost,
)
from .normalize import aware, validate_problem
from .optimize import solve, solve_flexible_load

_UTC = UTC
_TOL = 1e-6
_PRICE_REL_TOL = 1e-12
_PRICE_ABS_TOL = 1e-12
_MAX_PROBES = 96
_FLEXIBLE_LOAD_TARGETS = (3.0, 5.0, 10.0, 15.0, 20.0)


@dataclass(frozen=True)
class ConsumptionAnalysis:
    opportunities: tuple[Opportunity, ...]
    classification_mode: str
    coverage_reason: str
    reference_complete: bool


@dataclass(frozen=True)
class FlexibleLoadProfile:
    energy_kwh: float
    group: str
    incremental_cost: float | None
    average_cost_per_kwh: float | None
    block_cost_per_kwh: float | None
    price_status: str
    reason: str | None
    grid_import_delta_kwh: float | None
    grid_export_delta_kwh: float | None
    battery_throughput_delta_kwh: float | None
    schedule_kwh: tuple[float, ...]


@dataclass(frozen=True)
class FlexibleLoadAnalysis:
    depth_kwh: float | None
    anchor_price_per_kwh: float | None
    allowed_block_price_per_kwh: float | None
    profiles: tuple[FlexibleLoadProfile, ...]


def _finite(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as err:
        raise InputError(f"{name} must be finite") from err
    if not isfinite(number):
        raise InputError(f"{name} must be finite")
    return number


def _settings(settings: CompassSettings, budget_s: float) -> float:
    if not isinstance(settings, CompassSettings):
        raise InputError("invalid compass settings")
    for name in ("display_horizon_hours", "reference_horizon_hours"):
        value = getattr(settings, name)
        if type(value) is not int or not 1 <= value <= 48:
            raise InputError(f"{name} must be an integer from 1 to 48")
    interval = settings.display_interval_minutes
    if type(interval) is not int or interval not in (15, 30, 60):
        raise InputError("display interval must be 15, 30, or 60 minutes")
    horizon = max(settings.display_horizon_hours, settings.reference_horizon_hours)
    if ceil(horizon * 60 / interval) > _MAX_PROBES:
        raise InputError("horizon and interval require too many probes")
    probe = _finite(settings.probe_kwh, "probe_kwh")
    if not 0 < probe <= 5:
        raise InputError("probe_kwh must be in (0, 5]")
    for name in ("boost_ceiling", "limit_floor"):
        _finite(getattr(settings, name), name)
    if settings.boost_ceiling >= settings.limit_floor:
        raise InputError("boost ceiling must be below limit floor")
    cheap = _finite(settings.cheap_percentile, "cheap_percentile")
    limit = _finite(settings.limit_percentile, "limit_percentile")
    if not 0 <= cheap <= limit <= 100:
        raise InputError("percentiles must be ordered within [0, 100]")
    if settings.short_coverage not in ("absolute_fallback", "unavailable"):
        raise InputError("invalid short-coverage policy")
    probe_time = _finite(settings.probe_time_limit_s, "probe_time_limit_s")
    if not 0 < probe_time <= 30:
        raise InputError("probe_time_limit_s must be in (0, 30]")
    if type(settings.flexible_load_enabled) is not bool:
        raise InputError("flexible_load_enabled must be boolean")
    flexible_power = _finite(
        settings.flexible_load_max_power_kw, "flexible_load_max_power_kw"
    )
    if not 0 < flexible_power <= 100:
        raise InputError("flexible_load_max_power_kw must be in (0, 100]")
    degradation = _finite(
        settings.flexible_price_degradation_percent,
        "flexible_price_degradation_percent",
    )
    if not 0 <= degradation <= 100:
        raise InputError("flexible_price_degradation_percent must be in [0, 100]")
    budget = _finite(budget_s, "budget_s")
    if not 0 <= budget <= 300:
        raise InputError("budget_s must be in [0, 300]")
    return budget


def _flexible_group(energy_kwh: float) -> str:
    if energy_kwh <= 5:
        return "small"
    if energy_kwh <= 15:
        return "medium"
    return "large"


def analyze_flexible_loads(
    problem: Problem,
    plan: Plan,
    *,
    settings: CompassSettings,
    budget_s: float = 50.0,
) -> FlexibleLoadAnalysis:
    """Measure cost depth for independently optimized flexible energy targets."""
    budget = _settings(settings, budget_s)
    validate_problem(problem)
    if len(plan.flows) != len(problem.slots) or not isfinite(plan.objective):
        raise InputError("baseline plan does not match problem")
    if not settings.flexible_load_enabled:
        return FlexibleLoadAnalysis(None, None, None, ())

    deadline = perf_counter() + budget
    capacity_kwh = sum(
        settings.flexible_load_max_power_kw
        * (slot.end.astimezone(_UTC) - slot.start.astimezone(_UTC)).total_seconds()
        / 3600
        for slot in problem.slots
    )
    profiles: list[FlexibleLoadProfile] = []
    previous_energy: float | None = None
    previous_cost: float | None = None
    anchor_price: float | None = None
    allowed_price: float | None = None
    depth: float | None = None
    chain_open = True
    baseline_grid_import = sum(flow.grid_import_kwh for flow in plan.flows)
    baseline_grid_export = sum(flow.grid_export_kwh for flow in plan.flows)
    baseline_throughput = sum(
        flow.charge_kwh + flow.discharge_kwh for flow in plan.flows
    )

    for energy_kwh in _FLEXIBLE_LOAD_TARGETS:
        reason = None
        candidate = None
        if energy_kwh > capacity_kwh + _TOL:
            reason = "insufficient_time"
        else:
            remaining = deadline - perf_counter()
            if remaining <= 0:
                reason = "timeout"
            else:
                try:
                    candidate = solve_flexible_load(
                        problem,
                        FlexibleLoadRequest(
                            energy_kwh,
                            settings.flexible_load_max_power_kw,
                        ),
                        time_limit_s=min(remaining, settings.probe_time_limit_s),
                    )
                except SolveError as err:
                    reason = err.reason
        if candidate is None or perf_counter() > deadline:
            reason = reason or "timeout"
            profiles.append(
                FlexibleLoadProfile(
                    energy_kwh,
                    _flexible_group(energy_kwh),
                    None,
                    None,
                    None,
                    "UNKNOWN",
                    reason,
                    None,
                    None,
                    None,
                    (),
                )
            )
            previous_energy = previous_cost = None
            chain_open = False
            continue

        incremental_cost = plan_monetary_cost(candidate.plan) - plan_monetary_cost(plan)
        average_cost = incremental_cost / energy_kwh
        block_cost = (
            None
            if previous_energy is None or previous_cost is None
            else (incremental_cost - previous_cost) / (energy_kwh - previous_energy)
        )
        if energy_kwh == _FLEXIBLE_LOAD_TARGETS[0]:
            anchor_price = average_cost
            allowed_price = anchor_price + (
                settings.flexible_price_degradation_percent / 100 * abs(anchor_price)
            )
            status = "ANCHOR"
            depth = energy_kwh
        elif block_cost is None or allowed_price is None:
            status = "UNKNOWN"
            chain_open = False
        elif block_cost <= allowed_price + _TOL:
            status = "STABLE"
            if chain_open:
                depth = energy_kwh
        else:
            status = "DEGRADED"
            chain_open = False
        profiles.append(
            FlexibleLoadProfile(
                energy_kwh,
                _flexible_group(energy_kwh),
                incremental_cost,
                average_cost,
                block_cost,
                status,
                None,
                sum(flow.grid_import_kwh for flow in candidate.plan.flows)
                - baseline_grid_import,
                sum(flow.grid_export_kwh for flow in candidate.plan.flows)
                - baseline_grid_export,
                sum(
                    flow.charge_kwh + flow.discharge_kwh
                    for flow in candidate.plan.flows
                )
                - baseline_throughput,
                candidate.schedule_kwh,
            )
        )
        previous_energy = energy_kwh
        previous_cost = incremental_cost

    if not profiles or profiles[0].price_status != "ANCHOR":
        depth = anchor_price = allowed_price = None
    return FlexibleLoadAnalysis(depth, anchor_price, allowed_price, tuple(profiles))


def classify_cost(
    cost: float,
    *,
    q25: float | None,
    q75: float | None,
    minimum_purchase_price: float | None = None,
    boost_ceiling: float = 0.01,
    limit_floor: float = 0.80,
) -> Level:
    """Classify a finite incremental cost using the configured precedence."""
    value = _finite(cost, "cost")
    boost = _finite(boost_ceiling, "boost_ceiling")
    limit = _finite(limit_floor, "limit_floor")
    if boost >= limit:
        raise InputError("boost ceiling must be below limit floor")
    low = None if q25 is None else _finite(q25, "q25")
    high = None if q75 is None else _finite(q75, "q75")
    purchase = (
        None
        if minimum_purchase_price is None
        else _finite(minimum_purchase_price, "minimum_purchase_price")
    )
    if (low is None) != (high is None):
        raise InputError("both percentile values must be supplied together")
    if low is not None and low > high:
        raise InputError("percentile values must be ordered")
    if value < boost:
        return "BOOST"
    # Real-currency probe differences can retain negligible residue at equality,
    # independent of any strategy weighting on the solver objective.
    if any(
        threshold is not None
        and (
            value <= threshold
            or isclose(
                value,
                threshold,
                rel_tol=_PRICE_REL_TOL,
                abs_tol=_PRICE_ABS_TOL,
            )
        )
        for threshold in (low, purchase)
    ):
        return "CHEAP"
    if value > limit and (high is None or value > high):
        return "LIMIT"
    return "NORMAL"


def _percentile(values: tuple[float, ...], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower = floor(position)
    upper = ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _intervals(
    start: datetime, hours: int, minutes: int
) -> tuple[tuple[datetime, datetime], ...]:
    cursor = start.astimezone(_UTC)
    end = cursor + timedelta(hours=hours)
    width = timedelta(minutes=minutes)
    intervals = []
    while cursor < end:
        following = min(cursor + width, end)
        intervals.append((cursor, following))
        cursor = following
    return tuple(intervals)


def _probe(
    problem: Problem,
    baseline: Plan,
    start: datetime,
    end: datetime,
    amount: float,
    deadline: float,
) -> float | None:
    """Marginal PLN/kWh cost of adding `amount` kWh of load over [start, end).

    Differences plan_monetary_cost (real currency), not the solver objective,
    so the result stays honest under any strategy weighting. The candidate is
    solved from `replace(problem, slots=...)`, which carries every other field
    of `problem` — including `strategy_changed` — unchanged, so baseline and
    candidate always see the same (released or carried) mode-lock state.
    """
    elapsed = (end - start).total_seconds()
    additions = []
    for slot in problem.slots:
        slot_start = slot.start.astimezone(_UTC)
        slot_end = slot.end.astimezone(_UTC)
        overlap = max(
            0.0, (min(end, slot_end) - max(start, slot_start)).total_seconds()
        )
        additions.append(amount * overlap / elapsed)
    actual = sum(additions)
    if abs(actual - amount) > max(_TOL, amount * 1e-9):
        return None
    slots = tuple(
        replace(slot, load_kwh=slot.load_kwh + extra)
        for slot, extra in zip(problem.slots, additions, strict=True)
    )
    remaining = deadline - perf_counter()
    if remaining <= 0:
        return None
    try:
        candidate = solve(replace(problem, slots=slots), time_limit_s=remaining)
    except SolveError:
        return None
    if perf_counter() >= deadline:
        return None
    cost = (plan_monetary_cost(candidate) - plan_monetary_cost(baseline)) / actual
    return cost if isfinite(cost) and perf_counter() < deadline else None


def analyze_consumption(
    problem: Problem, plan: Plan, *, settings: CompassSettings, budget_s: float = 50.0
) -> ConsumptionAnalysis:
    """Probe display and reference intervals under one serial time budget."""
    budget = _settings(settings, budget_s)
    validate_problem(problem)
    if len(plan.flows) != len(problem.slots) or not isfinite(plan.objective):
        raise InputError("baseline plan does not match problem")
    start = problem.slots[0].start
    display_requested = _intervals(
        start, settings.display_horizon_hours, settings.display_interval_minutes
    )
    reference_requested = _intervals(
        start, settings.reference_horizon_hours, settings.display_interval_minutes
    )
    source_end = problem.slots[-1].end.astimezone(_UTC)

    def available(interval):
        interval_start, interval_end = interval
        if interval_start >= source_end:
            return None
        return interval_start, min(interval_end, source_end)

    reference = tuple(
        (item, probe)
        for item in reference_requested
        if (probe := available(item)) is not None
    )
    display = tuple((item, available(item)) for item in display_requested)
    requested = tuple(
        dict.fromkeys(
            (
                *(probe for _, probe in reference),
                *(probe for _, probe in display if probe is not None),
            )
        )
    )
    if len(requested) > _MAX_PROBES:
        raise InputError("horizon and interval require too many probes")
    zone = ZoneInfo(problem.timezone)
    costs: dict[tuple[datetime, datetime], float | None] = {}
    deadline = perf_counter() + budget
    for interval_start, interval_end in requested:
        probe_start = perf_counter()
        remaining = deadline - probe_start
        if remaining <= 0:
            costs[(interval_start, interval_end)] = None
            continue
        probe_deadline = min(deadline, probe_start + settings.probe_time_limit_s)
        costs[(interval_start, interval_end)] = _probe(
            problem,
            plan,
            interval_start,
            interval_end,
            settings.probe_kwh,
            probe_deadline,
        )
    reference_results = tuple((item, probe, costs[probe]) for item, probe in reference)
    known = tuple(cost for _, _, cost in reference_results if cost is not None)
    failed = tuple(
        (item, probe) for item, probe, cost in reference_results if cost is None
    )
    probes_succeeded = not failed
    complete = reference_requested[-1][1] <= source_end and probes_succeeded
    clipped_failures_only = (
        bool(known)
        and bool(failed)
        and all(probe[1] < item[1] for item, probe in failed)
    )
    reference_start = reference[0][1][0]
    reference_end = reference[-1][1][1]
    minimum_purchase_price = min(
        slot.buy_per_kwh
        for slot in problem.slots
        if slot.start.astimezone(_UTC) < reference_end
        and slot.end.astimezone(_UTC) > reference_start
    )
    if probes_succeeded or clipped_failures_only:
        mode = "percentile"
        if complete:
            reason = "complete"
        elif failed:
            reason = "reference_probe_failed"
        else:
            reason = "available_reference_horizon"
        low = _percentile(known, settings.cheap_percentile)
        high = _percentile(known, settings.limit_percentile)
    else:
        mode = settings.short_coverage
        reason = "reference_probe_failed"
        low = high = None
    opportunities = tuple(
        Opportunity(
            (probe or item)[0].astimezone(zone),
            (probe or item)[1].astimezone(zone),
            None if probe is None else costs[probe],
            (
                None
                if probe is None or costs[probe] is None or mode == "unavailable"
                else classify_cost(
                    costs[probe],
                    q25=low,
                    q75=high,
                    minimum_purchase_price=minimum_purchase_price,
                    boost_ceiling=settings.boost_ceiling,
                    limit_floor=settings.limit_floor,
                )
            ),
        )
        for item, probe in display
    )
    return ConsumptionAnalysis(opportunities, mode, reason, complete)


def consumption_outlook(
    problem: Problem, plan: Plan, *, settings: CompassSettings, budget_s: float = 50.0
) -> tuple[Opportunity, ...]:
    """Return incremental consumption opportunities at display resolution."""
    return analyze_consumption(
        problem, plan, settings=settings, budget_s=budget_s
    ).opportunities


def merge_windows(
    opportunities: tuple[Opportunity, ...], levels: frozenset[Level]
) -> tuple[Window, ...]:
    """Join adjacent selected levels, retaining each observed level in order."""
    windows: list[Window] = []
    for item in opportunities:
        if item.level not in levels:
            continue
        if windows and windows[-1].end.astimezone(_UTC) == item.start.astimezone(_UTC):
            previous = windows[-1]
            labels = (
                previous.levels
                if item.level in previous.levels
                else (*previous.levels, item.level)
            )
            windows[-1] = Window(previous.start, item.end, labels)
        else:
            windows.append(Window(item.start, item.end, (item.level,)))
    return tuple(windows)


def next_transition(
    opportunities: tuple[Opportunity, ...], now: datetime
) -> Opportunity | None:
    """Return the next different known level before the first unknown interval."""
    instant = aware(now, "now").astimezone(_UTC)
    current = next(
        (
            index
            for index, item in enumerate(opportunities)
            if item.start.astimezone(_UTC) <= instant < item.end.astimezone(_UTC)
        ),
        None,
    )
    if current is None:
        return None
    level = opportunities[current].level
    if level is None:
        return None
    previous_end = opportunities[current].end.astimezone(_UTC)
    for item in opportunities[current + 1 :]:
        if item.start.astimezone(_UTC) != previous_end or item.level is None:
            return None
        if item.level != level:
            return item
        previous_end = item.end.astimezone(_UTC)
    return None


def next_window(windows: tuple[Window, ...], now: datetime) -> Window | None:
    """Return an active or upcoming window, including its original start."""
    instant = aware(now, "now").astimezone(_UTC)
    return next(
        (window for window in windows if window.end.astimezone(_UTC) > instant), None
    )


def machine_snapshot(slot: Slot, flow: Flow) -> dict[str, float | MachineState]:
    """Pair a display state with the aggregate flows it summarizes."""
    return {
        "state": machine_state(slot, flow),
        "pv_kwh": slot.pv_kwh,
        "load_kwh": slot.load_kwh,
        "grid_import_kwh": flow.grid_import_kwh,
        "grid_export_kwh": flow.grid_export_kwh,
        "charge_kwh": flow.charge_kwh,
        "discharge_kwh": flow.discharge_kwh,
        "curtail_kwh": flow.curtail_kwh,
        "end_soc_kwh": flow.end_soc_kwh,
    }


def serialize_opportunity(item: Opportunity) -> dict[str, str | float | None]:
    """Serialize an opportunity with its original timestamp offsets."""
    return {
        "start": item.start.isoformat(),
        "end": item.end.isoformat(),
        "cost_per_kwh": item.cost_per_kwh,
        "level": item.level,
    }
