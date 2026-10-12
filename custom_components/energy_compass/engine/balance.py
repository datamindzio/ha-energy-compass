"""Candidate LFP balance windows and their objective weight."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from .models import BalanceWindow, Battery, Plan, Problem, Slot

DUE_LOOKAHEAD = timedelta(hours=24)
# Share of the pessimistic shortfall cost that ranks otherwise equal windows,
# and the share of the balance miss cost the dearest ranking may not exceed so
# the ranking alone never makes the solver skip the balance.
FALLBACK_WEIGHT = 0.05
FALLBACK_MISS_SHARE = 0.5


def _aligned(moment: datetime) -> bool:
    moment = moment.astimezone(UTC)
    return moment.minute == 0 and moment.second == 0 and moment.microsecond == 0


def candidate_windows(
    slots: tuple[Slot, ...],
    *,
    phase: str,
    now: datetime,
    hold_minutes: float,
    remaining_minutes: float,
    grid_charge_ok: tuple[bool, ...],
    pessimistic_pv_kwh: tuple[float, ...] | None = None,
    battery: Battery | None = None,
    threshold_kwh: float = 0.0,
) -> tuple[BalanceWindow, ...]:
    """Offer hour-aligned hold windows the inverter can execute.

    With a pessimistic PV series (and the battery to simulate on it) a
    CHARGE_PV window is offered only if the pack still reaches the threshold by
    the window start and PV still covers load inside the window on that series.
    A window offered as CHARGE_GRID on that series carries the pessimistic
    shortfall cost the solver uses to rank it against the others: the energy
    the pack still lacks at the window start plus the load PV leaves inside
    the window, at the window's buy prices.
    """
    pessimistic = pessimistic_pv_kwh is not None and battery is not None
    reach = _best_case_energy(slots, battery, pessimistic_pv_kwh) if pessimistic else ()
    if phase == "ok" or not slots:
        return ()
    if phase == "holding":
        starts = [0]
        length = timedelta(minutes=remaining_minutes)
    else:
        length = timedelta(minutes=hold_minutes)
        limit = now + DUE_LOOKAHEAD if phase == "due" else None
        starts = [
            index
            for index, slot in enumerate(slots)
            if _aligned(slot.start) and (limit is None or slot.start < limit)
        ]
    windows = []
    for first in starts:
        end = slots[first].start + length
        members = tuple(
            index for index in range(first, len(slots)) if slots[index].start < end
        )
        if not members or slots[members[-1]].end < end:
            continue
        if all(slots[i].pv_kwh >= slots[i].load_kwh for i in members) and (
            not pessimistic
            or (
                reach[first] >= threshold_kwh - 1e-9
                and all(pessimistic_pv_kwh[i] >= slots[i].load_kwh for i in members)
            )
        ):
            mode = "CHARGE_PV"
        elif all(grid_charge_ok[i] for i in members):
            mode = "CHARGE_GRID"
        else:
            continue
        fallback = 0.0
        if pessimistic and mode == "CHARGE_GRID":
            mean_buy = sum(slots[i].buy_per_kwh for i in members) / len(members)
            missing = max(0.0, threshold_kwh - reach[first]) / battery.eta_charge
            fallback = missing * mean_buy + sum(
                max(0.0, slots[i].load_kwh - pessimistic_pv_kwh[i])
                * slots[i].buy_per_kwh
                for i in members
            )
        windows.append(BalanceWindow(members, mode, fallback))
    return tuple(windows)


def fallback_costs(
    windows: tuple[BalanceWindow, ...], miss_cost: float
) -> tuple[float, ...]:
    """Solver cost of choosing each window, relative to the cheapest one.

    The solver prices a window on the median PV series, where a vetoed
    CHARGE_GRID window can look free, so every start would tie. This weighted
    excess over the cheapest window's pessimistic shortfall picks the start that
    is cheapest if the pessimistic series comes true. The weight shrinks so the
    dearest window never costs more than a share of the miss cost. Zero for a
    single window (a pinned probe) and when no window carries a shortfall.
    """
    if not windows:
        return ()
    floor = min(window.fallback_cost for window in windows)
    spread = max(window.fallback_cost for window in windows) - floor
    if spread <= 0:
        return (0.0,) * len(windows)
    weight = min(FALLBACK_WEIGHT, FALLBACK_MISS_SHARE * max(0.0, miss_cost) / spread)
    return tuple(weight * (window.fallback_cost - floor) for window in windows)


def _best_case_energy(
    slots: tuple[Slot, ...], battery: Battery, pv_kwh: tuple[float, ...]
) -> tuple[float, ...]:
    """Pack energy at each slot start when only PV surplus charges it.

    The pack never discharges (the grid covers load), so this is the most the
    given PV series can deliver before a window. The ceiling lift lets a hold
    window go up to capacity, so that is the cap.
    """
    energy = battery.initial_kwh
    result = []
    for slot, pv in zip(slots, pv_kwh):
        result.append(energy)
        hours = (slot.end - slot.start).total_seconds() / 3600
        gain = min(
            max(0.0, pv - slot.load_kwh) * battery.eta_charge,
            battery.charge_kw * hours,
        )
        energy = min(max(energy, battery.capacity_kwh), energy + gain)
    return tuple(result)


def miss_cost(phase: str, value: float, days_overdue: int) -> float:
    """Price of skipping a balance in this horizon."""
    if phase == "eligible":
        return 0.1 * value
    if phase == "due":
        return value * (1 + days_overdue)
    if phase == "holding":
        return 10 * value
    return 0.0


def lift_slots(
    windows: tuple[BalanceWindow, ...], battery: Battery, slot_count: int
) -> tuple[int, ...]:
    """Slots whose energy may exceed the configured ceiling, up to capacity.

    From the slot before the earliest window to the horizon end: after a hold
    the pack can only drain back under the ceiling at load pace, so capping the
    following slots would make any window infeasible without battery export.
    """
    if battery.initial_kwh > battery.capacity_kwh * battery.maximum_soc_fraction + 1e-9:
        return tuple(range(slot_count))
    if not windows:
        return ()
    first = max(0, min(window.slots[0] for window in windows) - 1)
    return tuple(range(first, slot_count))


def pin_balance(problem: Problem, plan: Plan) -> Problem:
    """Freeze the dispatch plan's balance choice for consumption probes.

    A probe adds load; letting it re-decide the balance would price the
    balance, not the load. The lift slots stay so energy bounds match.
    """
    if not problem.balance_windows:
        return problem
    if plan.balance_start is None:
        return replace(problem, balance_windows=(), balance_miss_cost=0.0)
    window = next(
        w for w in problem.balance_windows if w.slots[0] == plan.balance_start
    )
    return replace(
        problem, balance_windows=(window,), balance_fixed=True, balance_miss_cost=0.0
    )
