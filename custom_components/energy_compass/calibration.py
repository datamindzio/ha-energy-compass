"""Measured calibration: battery round trip and PV forecast accuracy.

Read-only bookkeeping, no Home Assistant imports and no effect on the plan.
Cumulative counters are reduced to their value at each full UTC hour boundary
by interpolating between consecutive readings, so the state is bounded by the
window (one small row per hour) however often a counter reports. A counter
that goes down is a reset or a glitch: no boundary is derived from the step
down or from the segment that recovers from it, and a rise faster than any
household device can deliver is rejected the same way, so the hours around
them drop out of both ratios instead of becoming negative or invented energy.
A counter bound to another source starts over. An hour is used only when every
value it needs exists; missing hours are skipped, never filled.
"""

from datetime import datetime, timedelta
from math import isfinite
from typing import NamedTuple
from zoneinfo import ZoneInfo

from .config_models import NumericSetting, resolve_numeric
from .engine.models import InputError
from .sources.bindings import parse_timestamp

COUNTER_KEYS = ("pv", "charge", "discharge")
# Measurement roles feeding each counter key.
COUNTER_MEASUREMENTS = {
    "pv": "pv_energy",
    "charge": "battery_charge_energy",
    "discharge": "battery_discharge_energy",
}
BATTERY_COUNTER_MEASUREMENTS = ("battery_charge_energy", "battery_discharge_energy")

ROUND_TRIP_WINDOW = timedelta(days=30)
PV_WINDOW = timedelta(days=14)
_HOUR = timedelta(hours=1)
_RETENTION = ROUND_TRIP_WINDOW + timedelta(days=1)
# A counter that moved between two readings further apart than this has no
# trustworthy value at the hour boundaries in between; one that did not move is
# exact however long the gap.
MAX_INTERPOLATION_GAP = timedelta(hours=2)
# A counter that rises faster than this between two readings is a glitch, not
# energy: no home battery or PV array delivers it. The slack absorbs the
# resolution of a counter that is read twice in the same second.
MAX_COUNTER_KW = 100.0
COUNTER_SLACK_KWH = 1.0
_MAX_HOUR_KWH = MAX_COUNTER_KW + COUNTER_SLACK_KWH
# A plan published late in the hour carries only the remainder of it; scaling a
# short remainder up to a full hour amplifies noise, so it is not recorded.
MIN_FORECAST_FRACTION = 0.5
# A week of overlapping hours before the round trip is called measured, and a
# minimum of energy so that two idle counters cannot produce a ratio.
MIN_ROUND_TRIP_HOURS = 7 * 24
MIN_ROUND_TRIP_CHARGE_KWH = 1.0
# A day of hours before the PV ratio is called measured.
MIN_PV_HOURS = 24
# Hours of the day whose total forecast is below this have no meaningful ratio.
MIN_RATIO_FORECAST_KWH = 0.05
_STATE_KEYS = ("last", "hours", "forecast")


def empty_state() -> dict:
    return {"last": {}, "hours": {}, "forecast": {}}


def valid_state(raw) -> bool:
    """Stored state has the three known maps with well-formed rows."""
    if not isinstance(raw, dict) or set(raw) != set(_STATE_KEYS):
        return False
    try:
        for key, row in raw["last"].items():
            if key not in COUNTER_KEYS:
                return False
            _stamp(row[0])
            _number(row[1])
            if not isinstance(row[2], str) or not isinstance(row[3], bool):
                return False
            _stamp(row[4])
        for hour, counters in raw["hours"].items():
            _stamp(hour)
            if not set(counters) <= set(COUNTER_KEYS):
                return False
            for value in counters.values():
                _number(value)
        for hour, row in raw["forecast"].items():
            _stamp(hour)
            _number(row[0])
            _number(row[1])
    except AttributeError, IndexError, KeyError, TypeError, ValueError:
        return False
    return all(isinstance(raw[key], dict) for key in _STATE_KEYS)


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("not a number")
    if not isfinite(value):
        raise ValueError("not finite")
    return float(value)


def _stamp(raw) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed


def _hour_floor(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


class Reading(NamedTuple):
    """One counter value in kWh as the source reported it."""

    at: datetime
    value: float
    # Identity of the bound source; a counter bound to another one starts over.
    source: str = ""
    # Latest time a counter that has not changed for longer than the
    # interpolation gap is still taken to hold its value. A source that writes
    # its state only on change does not report while idle.
    until: datetime | None = None


def observe(state: dict, readings: dict, now: datetime) -> bool:
    """Fold counter readings {key: Reading | None} into the state.

    Returns True when the stored state changed (worth persisting).
    """
    changed = False
    for key in COUNTER_KEYS:
        reading = readings.get(key)
        if reading is None:
            continue
        last = state["last"].get(key)
        if last is not None and last[2] != reading.source:
            _forget(state, key)
            last = None
            changed = True
        previous = None
        at, value = reading.at, reading.value
        reported = at
        if last is not None:
            held, reported = _stamp(last[0]), _stamp(last[4])
            previous = (held, last[1], last[3])
            if value == previous[1]:
                # Idle for longer than a moving counter may go unreported: a
                # source that writes only on change holds its value until the
                # reading's limit without saying so.
                if (reading.until or at) - at > MAX_INTERPOLATION_GAP:
                    at = reading.until
                if at <= held:
                    continue
                reported = max(reported, reading.at)
            elif at <= reported:
                continue
            else:
                # A change that lands inside the held span starts after it.
                at = max(at, held)
                reported = reading.at
        # A step down is a reset or a glitch; the segment that follows it is
        # not trusted either, whether it recovers or restarts the counter.
        trusted = previous is None or value >= previous[1]
        state["last"][key] = [
            at.isoformat(),
            value,
            reading.source,
            trusted,
            reported.isoformat(),
        ]
        # Walk the hour starts the segment (previous, at] spans; a first
        # reading only fixes a boundary it lands on exactly.
        boundary = _hour_floor(previous[0]) + _HOUR if previous else _hour_floor(at)
        boundary = max(boundary, _hour_floor(now - _RETENTION) + _HOUR)
        while boundary <= at:
            if previous is None:
                derived = value if boundary == at else None
            else:
                derived = _at_boundary(previous, (at, value), boundary)
            if derived is not None:
                state["hours"].setdefault(boundary.isoformat(), {})[key] = derived
                changed = True
            boundary += _HOUR
    if changed:
        _prune(state, now)
    return changed


def _forget(state: dict, key: str) -> None:
    """Drop everything stored for a counter whose source changed."""
    del state["last"][key]
    for stamp in list(state["hours"]):
        state["hours"][stamp].pop(key, None)
        if not state["hours"][stamp]:
            del state["hours"][stamp]


def _at_boundary(previous, current, boundary) -> float | None:
    (t0, v0, trusted), (t1, v1) = previous, current
    if v1 < v0:
        return None
    if v1 - v0 > MAX_COUNTER_KW * ((t1 - t0) / _HOUR) + COUNTER_SLACK_KWH:
        return None
    if boundary == t1:
        return v1
    if not trusted:
        return None
    if v1 == v0:
        return v1
    if t1 - t0 > MAX_INTERPOLATION_GAP:
        return None
    return v0 + (v1 - v0) * ((boundary - t0) / (t1 - t0))


def record_forecast(state: dict, rows, now: datetime) -> bool:
    """Keep the plan's forecast for the current hour; the fullest plan wins."""
    hour = _hour_floor(now)
    changed = False
    for row in rows or ():
        try:
            start = _stamp(row["hour_start"])
            kwh = _number(row["kwh_hour"])
            fraction = _number(row["fraction"])
        except KeyError, TypeError, ValueError:
            continue
        if start != hour or fraction < MIN_FORECAST_FRACTION or kwh < 0:
            continue
        stored = state["forecast"].get(start.isoformat())
        if stored is None or fraction > stored[1] + 1e-9:
            state["forecast"][start.isoformat()] = [round(kwh, 4), round(fraction, 4)]
            changed = True
    if changed:
        _prune(state, now)
    return changed


def _prune(state: dict, now: datetime) -> None:
    for name, keep in (("hours", _RETENTION), ("forecast", PV_WINDOW + _HOUR)):
        cutoff = now - keep
        rows = state[name]
        for stale in [k for k in rows if _stamp(k) < cutoff]:
            del rows[stale]
        # A hard ceiling too: a clock that ran ahead must not grow the store.
        limit = int(keep / _HOUR) + 24
        if len(rows) > limit:
            for stale in sorted(rows)[: len(rows) - limit]:
                del rows[stale]


def _hours(window: timedelta, now: datetime):
    first = _hour_floor(now - window)
    last = _hour_floor(now) - _HOUR
    cursor = first
    while cursor <= last:
        yield cursor
        cursor += _HOUR


def _delta(state: dict, hour: datetime, key: str) -> float | None:
    start = state["hours"].get(hour.isoformat(), {}).get(key)
    end = state["hours"].get((hour + _HOUR).isoformat(), {}).get(key)
    if start is None or end is None or not 0 <= end - start <= _MAX_HOUR_KWH:
        return None
    return end - start


def round_trip_report(state: dict, now: datetime, configured: float | None) -> dict:
    """Discharge over charge energy across the rolling window."""
    charge = discharge = 0.0
    hours = 0
    for hour in _hours(ROUND_TRIP_WINDOW, now):
        c, d = _delta(state, hour, "charge"), _delta(state, hour, "discharge")
        if c is None or d is None:
            continue
        charge += c
        discharge += d
        hours += 1
    enough = hours >= MIN_ROUND_TRIP_HOURS and charge >= MIN_ROUND_TRIP_CHARGE_KWH
    measured = discharge / charge if enough else None
    return {
        "status": "ok" if enough else "insufficient_data",
        "window_days": ROUND_TRIP_WINDOW.days,
        "covered_hours": hours,
        "minimum_hours": MIN_ROUND_TRIP_HOURS,
        "charge_kwh": round(charge, 3),
        "discharge_kwh": round(discharge, 3),
        "measured": None if measured is None else round(measured, 4),
        "configured": None if configured is None else round(configured, 4),
        "difference": None
        if measured is None or configured is None
        else round(measured - configured, 4),
    }


def pv_accuracy_report(state: dict, now: datetime, timezone: str) -> dict:
    """Real over forecast PV energy, in total and per local hour of the day."""
    zone = ZoneInfo(timezone)
    totals = [0.0, 0.0]
    per_hour: dict[int, list] = {}
    hours = 0
    for hour in _hours(PV_WINDOW, now):
        forecast = state["forecast"].get(hour.isoformat())
        measured = _delta(state, hour, "pv")
        if forecast is None or measured is None:
            continue
        hours += 1
        totals[0] += forecast[0]
        totals[1] += measured
        row = per_hour.setdefault(hour.astimezone(zone).hour, [0.0, 0.0, 0])
        row[0] += forecast[0]
        row[1] += measured
        row[2] += 1

    enough = hours >= MIN_PV_HOURS

    def ratio(forecast, measured):
        if not enough or forecast < MIN_RATIO_FORECAST_KWH:
            return None
        return round(measured / forecast, 3)

    return {
        "status": "ok" if enough else "insufficient_data",
        "window_days": PV_WINDOW.days,
        "covered_hours": hours,
        "minimum_hours": MIN_PV_HOURS,
        "forecast_kwh": round(totals[0], 3),
        "measured_kwh": round(totals[1], 3),
        "ratio": ratio(*totals),
        "by_local_hour": {
            f"{hour:02d}": {
                "ratio": ratio(row[0], row[1]),
                "hours": row[2],
                "forecast_kwh": round(row[0], 3),
                "measured_kwh": round(row[1], 3),
            }
            for hour, row in sorted(per_hour.items())
        },
    }


def forecast_hours(slots, now: datetime, hours: int = 3) -> list[dict]:
    """PV forecast per UTC hour as the plan used it, scaled to a full hour.

    The first slot starts at the plan time, so the current hour carries only its
    remainder; `fraction` says how much of the hour the slots cover.
    """
    rows = []
    first = _hour_floor(now)
    for offset in range(hours):
        start = first + offset * _HOUR
        end = start + _HOUR
        covered = energy = 0.0
        for slot in slots:
            overlap = (min(slot.end, end) - max(slot.start, start)).total_seconds()
            if overlap <= 0:
                continue
            length = (slot.end - slot.start).total_seconds()
            covered += overlap
            energy += slot.pv_kwh * overlap / length
        if covered > 0:
            rows.append(
                {
                    "hour_start": start.isoformat(),
                    "kwh_hour": energy * 3600 / covered,
                    "fraction": covered / 3600,
                }
            )
    return rows


def _selected(config: dict, role: str) -> dict | None:
    selected = config.get("measurements", {}).get(role)
    if (
        not selected
        or not selected.get("entity")
        or selected.get("_missing_registry")
        or selected.get("statistic_id")
    ):
        return None
    return selected


def configured_blocks(config: dict) -> tuple[bool, bool]:
    """(round trip, PV accuracy) are active when their live sources are bound."""
    round_trip = all(_selected(config, n) for n in BATTERY_COUNTER_MEASUREMENTS)
    pv = bool(
        config["sources"]["pv"]["enabled"]
        and _selected(config, COUNTER_MEASUREMENTS["pv"])
    )
    return round_trip and bool(config["sources"]["battery_enabled"]), pv


def _identity(setting: NumericSetting) -> str:
    """What a stored counter value was read from; any change restarts its history."""
    binding = setting.entity
    return "|".join(
        (
            binding.entity_id,
            binding.attribute or "",
            repr(setting.multiplier),
            setting.source_unit or setting.unit,
        )
    )


def read_counters(config: dict, states: dict, now: datetime) -> dict:
    """Latest counter readings in kWh stamped with the source's own report time.

    An unavailable, mismatched-unit or negative counter is simply not read: a
    diagnostic never raises into the plan. A counter that has not moved for longer
    than MAX_INTERPOLATION_GAP is taken to hold its value until now, while its
    last report is no older than the role's maximum age: a source that writes
    only on change is idle, not dead, until then (no age limit: until now).
    """
    readings = {}
    for key, role in COUNTER_MEASUREMENTS.items():
        selected = _selected(config, role)
        if selected is None:
            continue
        try:
            setting = NumericSetting.from_dict({**selected, "max_age_seconds": None})
            value = resolve_numeric(setting, states, now)
            state = states[setting.entity.entity_id]
            stamp = parse_timestamp(
                state["last_reported"]
                if "last_reported" in state
                else state.get("last_updated")
            )
        except InputError, KeyError, TypeError, ValueError:
            continue
        if value >= 0:
            stamp = min(stamp, now)
            max_age = selected.get("max_age_seconds")
            until = now if not max_age else stamp + timedelta(seconds=max_age)
            readings[key] = Reading(
                stamp, value, _identity(setting), max(stamp, min(now, until))
            )
    return readings
