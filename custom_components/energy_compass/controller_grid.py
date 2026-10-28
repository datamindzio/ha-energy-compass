"""Grid presence for the native Deye controller: debounce, outage state, sources.

No Home Assistant imports. While the grid is out the battery is the only source of the
house, so the controller must know. This module only judges readings: it enters an
outage after `enter_seconds` of continuous absence and leaves it after `stable_seconds`
of continuous presence; an unreadable source keeps what was known. What the controller
does about an outage (BACKUP, no solve, notifications) lives with its callers.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

ENTITY_KEY = "grid"
VOLTAGE_KEYS = ("grid_l1_voltage", "grid_l2_voltage", "grid_l3_voltage")
SOURCE_ENTITY = "entity"
SOURCE_VOLTAGE = "voltage"
# A phase under this voltage carries no grid; any phase at or above it does.
PRESENT_VOLTS = 100.0
ENTER_SECONDS = 15
ENTER_SECONDS_RANGE = (5, 600)
STABLE_MINUTES = 5
STABLE_MINUTES_RANGE = (1, 120)
HISTORY_LIMIT = 10
_UNREADABLE = frozenset({None, "", "unknown", "unavailable"})


@dataclass(frozen=True)
class GridReading:
    """One look at the sources: which one spoke, what it said, and whether grid is present."""

    source: str | None = None
    raw: str | None = None
    present: bool | None = None


@dataclass(frozen=True)
class GridWatch:
    """The last known presence and the outage it has led to.

    `off_since` is the start of the current continuous absence and `on_since` the start
    of the current continuous presence during an outage; both are UTC ISO strings so the
    watch survives a restart. `since` is when the outage was declared and `lost_at` the
    first absent reading behind it. When the outage ends `lost_at` stays and `restored_at`
    is when the grid came back (the start of the stable presence), so the end can be told.
    """

    source: str | None = None
    raw: str | None = None
    present: bool | None = None
    active: bool = False
    since: str | None = None
    lost_at: str | None = None
    off_since: str | None = None
    on_since: str | None = None
    restored_at: str | None = None

    def as_attribute(self) -> dict[str, Any]:
        """The `grid` attribute of the controller sensor: the diagnostics block too."""
        return {
            "source": self.source,
            "raw": self.raw,
            "present": self.present,
            "outage": self.active,
            "since": self.since,
            "lost_at": self.lost_at if self.active else None,
            "off_since": self.off_since,
            "restored_pending_since": self.on_since,
            "restored_at": self.restored_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "raw": self.raw,
            "present": self.present,
            "active": self.active,
            "since": self.since,
            "lost_at": self.lost_at,
            "off_since": self.off_since,
            "on_since": self.on_since,
            "restored_at": self.restored_at,
        }


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _stamp(value: Any) -> str | None:
    """A stored time, kept only when it parses: a damaged one must not stall a timer."""
    if not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def from_dict(raw: Any) -> GridWatch:
    """A stored watch; anything unusable is the empty watch (the grid is assumed present)."""
    if not isinstance(raw, Mapping):
        return GridWatch()
    present = raw.get("present")
    active = raw.get("active") is True
    since = _stamp(raw.get("since")) if active else None
    if active and since is None:
        return GridWatch()
    return GridWatch(
        source=_text(raw.get("source")),
        raw=_text(raw.get("raw")),
        present=present if isinstance(present, bool) else None,
        active=active,
        since=since,
        lost_at=_stamp(raw.get("lost_at")),
        off_since=_stamp(raw.get("off_since")),
        on_since=_stamp(raw.get("on_since")) if active else None,
        restored_at=_stamp(raw.get("restored_at")),
    )


def _volts(value: Any) -> float | None:
    if value in _UNREADABLE:
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def read(presence: str | None, voltages: Iterable[str | None] = ()) -> GridReading:
    """Fold the sources into one reading.

    `presence` is the state of the grid binary sensor (on = grid present), None when
    there is none. It speaks when readable; otherwise the phase voltages do: every
    numeric phase under 100 V means absent, any at or above it means present, none
    numeric means nothing is known.
    """
    if presence in ("on", "off"):
        return GridReading(SOURCE_ENTITY, presence, presence == "on")
    numbers = [v for v in map(_volts, voltages) if v is not None]
    if numbers:
        raw = ", ".join(f"{v:g}" for v in numbers)
        return GridReading(
            SOURCE_VOLTAGE, raw, any(v >= PRESENT_VOLTS for v in numbers)
        )
    return GridReading(None, _text(presence), None)


def _parse(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat()


def observe(
    watch: GridWatch,
    reading: GridReading | None,
    now: datetime,
    *,
    enter_seconds: float = ENTER_SECONDS,
    stable_seconds: float = STABLE_MINUTES * 60,
) -> GridWatch:
    """Fold one reading into the watch.

    `reading` None means there is no source at all: the watch starts over. An unknown
    presence leaves the watch as it was, apart from the source it reports; its timers
    keep running, so an outage that began before the source went dark still enters.
    """
    if reading is None:
        return GridWatch()
    if reading.present is None:
        return _tick(
            GridWatch(
                **{
                    **watch.to_dict(),
                    "source": reading.source or watch.source,
                    "raw": reading.raw,
                }
            ),
            now,
            enter_seconds,
            stable_seconds,
        )
    stamp = _iso(now)
    fields = {**watch.to_dict(), "source": reading.source, "raw": reading.raw}
    fields["present"] = reading.present
    if reading.present:
        fields["off_since"] = None
        if watch.active:
            fields["on_since"] = watch.on_since or stamp
    else:
        fields["on_since"] = None
        fields["off_since"] = watch.off_since or stamp
    return _tick(GridWatch(**fields), now, enter_seconds, stable_seconds)


def _tick(
    watch: GridWatch, now: datetime, enter_seconds: float, stable_seconds: float
) -> GridWatch:
    """Enter or leave the outage once the debounce has run out."""
    if (
        not watch.active
        and watch.off_since
        and (now - _parse(watch.off_since)).total_seconds() >= enter_seconds
    ):
        return GridWatch(
            **{
                **watch.to_dict(),
                "active": True,
                "since": _iso(now),
                "lost_at": watch.off_since,
                "on_since": None,
                "restored_at": None,
            }
        )
    if (
        watch.active
        and watch.on_since
        and (now - _parse(watch.on_since)).total_seconds() >= stable_seconds
    ):
        return GridWatch(
            **{
                **watch.to_dict(),
                "active": False,
                "since": None,
                "off_since": None,
                "on_since": None,
                "restored_at": watch.on_since,
            }
        )
    return watch


def next_deadline(
    watch: GridWatch,
    *,
    enter_seconds: float = ENTER_SECONDS,
    stable_seconds: float = STABLE_MINUTES * 60,
) -> datetime | None:
    """When the watch next changes without a new reading, None when it waits for one."""
    if not watch.active and watch.off_since:
        return _parse(watch.off_since) + timedelta(seconds=enter_seconds)
    if watch.active and watch.on_since:
        return _parse(watch.on_since) + timedelta(seconds=stable_seconds)
    return None


def enter_seconds_of(options: Mapping[str, Any] | None) -> float:
    return _bounded(options, "outage_enter_seconds", ENTER_SECONDS, ENTER_SECONDS_RANGE)


def stable_seconds_of(options: Mapping[str, Any] | None) -> float:
    return 60 * _bounded(
        options, "outage_stable_minutes", STABLE_MINUTES, STABLE_MINUTES_RANGE
    )


def _bounded(
    options: Mapping[str, Any] | None,
    key: str,
    default: float,
    bounds: tuple[float, float],
) -> float:
    value = (options or {}).get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return value if bounds[0] <= value <= bounds[1] else default


def range_invalid(options: Mapping[str, Any] | None) -> bool:
    """Whether a stored debounce setting is a number outside its range (unset is fine)."""
    for key, bounds in (
        ("outage_enter_seconds", ENTER_SECONDS_RANGE),
        ("outage_stable_minutes", STABLE_MINUTES_RANGE),
    ):
        value = (options or {}).get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            return True
        if not bounds[0] <= value <= bounds[1]:
            return True
    return False
