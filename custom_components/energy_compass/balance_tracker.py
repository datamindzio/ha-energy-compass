# custom_components/energy_compass/balance_tracker.py
"""Periodic LFP balance bookkeeping: pure state, no Home Assistant imports.

A balance counts once SOC stays at or above the threshold for the whole hold.
Only observed time counts: readings further apart than the SOC max age break
the hold, and a hold whose latest full reading went stale neither holds nor
completes. Completion is evaluated lazily while the hold is fresh, so an
unchanged full SOC completes the hold without needing another state event.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import inf

from .settings import NUMBERS

SENSOR_STATES = ("ok", "eligible", "scheduled", "holding", "overdue")
STATE_KEYS = ("last_completed_at", "hold_started_at", "last_full_at")
_LEAD = timedelta(days=2)
# Capacity-derived percentages land a hair under round thresholds (99 % of
# 24.6 kWh reads 98.99999999999999).
_EPSILON = 1e-6


@dataclass(frozen=True)
class BalanceSettings:
    interval_days: int
    hold_minutes: int
    threshold_percent: float
    # None: gap-tolerant, for changes-only recorder history where a pinned SOC
    # leaves no samples.
    max_gap_seconds: float | None

    @property
    def interval(self) -> timedelta:
        return timedelta(days=self.interval_days)

    @property
    def hold(self) -> timedelta:
        return timedelta(minutes=self.hold_minutes)

    @property
    def lead(self) -> timedelta:
        return min(_LEAD, self.interval / 2)

    @property
    def max_gap(self) -> timedelta | None:
        if self.max_gap_seconds is None:
            return None
        return timedelta(seconds=self.max_gap_seconds)


def balance_settings(values: Mapping) -> BalanceSettings:
    def get(key):
        return values.get(key, NUMBERS[key][1])

    return BalanceSettings(
        int(get("balance_interval_days")),
        int(get("balance_hold_minutes")),
        float(get("balance_soc_threshold")),
        float(get("soc_max_age_seconds")),
    )


def empty_state() -> dict:
    return dict.fromkeys(STATE_KEYS)


def valid_state(raw) -> bool:
    """Stored state is a dict of known keys, each None or an aware ISO time."""
    if not isinstance(raw, dict) or not set(raw) <= set(STATE_KEYS):
        return False
    for key in STATE_KEYS[:2]:
        if key not in raw:
            return False
    for value in raw.values():
        if value is None:
            continue
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return False
        if parsed.tzinfo is None:
            return False
    return True


def _time(raw: str | None) -> datetime | None:
    return None if raw is None else datetime.fromisoformat(raw)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _fresh(last_full: datetime | None, at: datetime, settings: BalanceSettings):
    if settings.max_gap is None:
        return True
    return last_full is not None and at - last_full <= settings.max_gap


def observe(
    state: dict, soc_percent: float | None, at: datetime, settings: BalanceSettings
) -> dict:
    """Advance the hold with one SOC reading; None (unavailable) freezes it."""
    if soc_percent is None:
        return dict(state)
    last = _time(state.get("last_completed_at"))
    started = _time(state.get("hold_started_at"))
    last_full = _time(state.get("last_full_at"))
    full = soc_percent >= settings.threshold_percent - _EPSILON
    fresh = _fresh(last_full, at, settings)
    if started is not None:
        # Time proven full: up to this reading while readings stay close
        # (or, in gap-tolerant history, until the value changed); otherwise
        # only up to the latest full reading.
        covered = at if fresh and (full or settings.max_gap is None) else last_full
        end = started + settings.hold
        if covered is not None and covered >= end:
            last = end if last is None else max(last, end)
            # Still full: the next hold continues from this completion.
            started = (end if fresh else at) if full else None
        elif not full:
            started = None
        elif not fresh:
            started = at
    elif full:
        started = at
    if full:
        last_full = at
    return {
        "last_completed_at": _iso(last),
        "hold_started_at": _iso(started),
        "last_full_at": _iso(last_full),
    }


def _fresh_hold(state: dict, now: datetime, settings: BalanceSettings):
    started = _time(state.get("hold_started_at"))
    if started is None or not _fresh(_time(state.get("last_full_at")), now, settings):
        return None
    return started


def _completed(state: dict, now: datetime, settings: BalanceSettings):
    last = _time(state.get("last_completed_at"))
    started = _fresh_hold(state, now, settings)
    if started is not None and now - started >= settings.hold:
        candidate = started + settings.hold
        return candidate if last is None or candidate > last else last
    return last


def _holding(state: dict, now: datetime, settings: BalanceSettings) -> bool:
    last = _time(state.get("last_completed_at"))
    started = _fresh_hold(state, now, settings)
    return (
        started is not None
        and (last is None or started > last)
        and now - started < settings.hold
    )


def _due_phase(last: datetime | None, now: datetime, settings: BalanceSettings):
    if last is None or now - last >= settings.interval:
        return "due"
    if now - last >= settings.interval - settings.lead:
        return "eligible"
    return "ok"


def status(state: dict, now: datetime, settings: BalanceSettings) -> dict:
    last = _completed(state, now, settings)
    holding = _holding(state, now, settings)
    due_phase = _due_phase(last, now, settings)
    overdue = 0
    if last is not None and now - last >= settings.interval:
        overdue = (now - last - settings.interval).days
    progress = 0.0
    if holding:
        started = _time(state["hold_started_at"])
        progress = round((now - started).total_seconds() / 60, 1)
    return {
        "phase": "holding" if holding else due_phase,
        # Phase ignoring an active hold: a hold right after a completion is
        # tracked but not worth scheduling.
        "due_phase": due_phase,
        "last_completed": _iso(last),
        "next_due": _iso(last + settings.interval) if last else None,
        "days_overdue": overdue,
        "hold_progress_minutes": progress,
        "hold_remaining_minutes": round(settings.hold_minutes - progress, 1)
        if holding
        else 0.0,
        "hold_required_minutes": settings.hold_minutes,
        "threshold_percent": settings.threshold_percent,
    }


def seed_from_history(
    samples: Iterable[tuple[datetime, float | None]], settings: BalanceSettings
) -> dict:
    """Replay recorder SOC history; no qualifying hold means due now.

    History stores changes only, so an unavailable sample is itself an
    observation: SOC was unknown from then on. Replay it as below threshold so
    a hold ends there instead of spanning the gap.
    """
    state = empty_state()
    for at, value in sorted(samples, key=lambda item: item[0]):
        state = observe(state, -inf if value is None else value, at, settings)
    return state


def confirmed_percent(
    percent: float, confirmed: bool, qualified: bool = False
) -> float:
    """SOC for the hold when only physical evidence of a full pack counts as full.

    The counter estimator reaches the threshold by arithmetic; a balance needs
    the pack proven full. A full anchor proves it; so does standing full
    evidence (a plausible full candidate or balance stop) even while the
    estimator only logs candidates and applies no anchor. Otherwise the reading
    counts as below the threshold.
    """
    if confirmed:
        return percent
    return 100.0 if qualified else -inf


def soc_percent(raw: float, unit: str, capacity_kwh: float) -> float | None:
    if unit == "%":
        return float(raw)
    if unit == "fraction":
        return float(raw) * 100
    if unit == "kWh":
        return float(raw) / capacity_kwh * 100
    return None


def planned_window(rows: list[dict]) -> dict | None:
    first = last = None
    for row in rows:
        if row.get("balance_hold") is True:
            first = first or row
            last = row
        elif first is not None:
            break
    if first is None:
        return None
    return {"start": first["start"], "end": last["end"], "mode": first["state"]}


def sensor_state(phase: str, scheduled: bool) -> str:
    if phase in ("ok", "holding"):
        return phase
    if scheduled:
        return "scheduled"
    return "overdue" if phase == "due" else "eligible"
