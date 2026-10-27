"""Inverter clock against Home Assistant time: skew, threshold and hold.

No Home Assistant imports. The inverter runs its TOU programs by its own clock, so a
clock that disagrees with Home Assistant (DST not followed, drift, no cloud time sync)
moves every window the controller writes. This module only measures and judges; it
changes nothing the controller writes.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any

CODE = "inverter_clock_skew"
ENTITY_KEY = "date_time"
THRESHOLD_SECONDS = 300
HOLD_SECONDS = 600
STALE_SECONDS = 300
_UNREADABLE = frozenset({None, "", "unknown", "unavailable"})


@dataclass(frozen=True)
class ClockWatch:
    """What the last readable clock said and since when it is over the threshold."""

    skew_seconds: float | None = None
    inverter_clock: str | None = None
    read_at: str | None = None
    over_since: str | None = None
    active: bool = False

    @property
    def skew_minutes(self) -> int | None:
        if self.skew_seconds is None:
            return None
        return round(self.skew_seconds / 60)

    @property
    def settled(self) -> bool:
        """A readable clock is under the threshold: nothing is waiting to be judged."""
        return self.skew_seconds is not None and self.over_since is None

    def as_attribute(self) -> dict[str, Any]:
        """The `clock` attribute: the diagnostics block too."""
        return {
            "clock_skew_seconds": self.skew_seconds,
            "clock_skew_minutes": self.skew_minutes,
            "inverter_clock": self.inverter_clock,
            "clock_read_at": self.read_at,
            "over_threshold_since": self.over_since,
            "alert": self.active,
        }


def parse_clock(value: Any, tz: tzinfo) -> datetime | None:
    """The inverter clock as an aware time; a naive one reads in the HA time zone."""
    if value in _UNREADABLE or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed


def observe(
    watch: ClockWatch,
    state: Mapping[str, Any] | None,
    now: datetime,
    tz: tzinfo,
) -> ClockWatch:
    """Fold one look at the clock entity into the watch.

    `state` is `{"state": str, "reported": aware datetime}`, None when the entity does
    not exist (the feature is off and the watch starts over). An unreadable or stale
    state leaves the watch as it was: nothing is known, so nothing is cleared or raised.
    The skew is the clock minus the time Home Assistant got it, so a state that nobody
    refreshed cannot grow a skew of its own.
    """
    if state is None:
        return ClockWatch()
    clock = parse_clock(state.get("state"), tz)
    reported = state.get("reported")
    if clock is None or reported is None:
        return watch
    if (now - reported).total_seconds() > STALE_SECONDS:
        return watch
    skew = (clock - reported).total_seconds()
    over = abs(skew) > THRESHOLD_SECONDS
    since = (watch.over_since or reported.astimezone(UTC).isoformat()) if over else None
    held = (
        over
        and (reported - datetime.fromisoformat(since)).total_seconds() >= HOLD_SECONDS
    )
    return ClockWatch(
        skew_seconds=skew,
        inverter_clock=clock.isoformat(),
        read_at=reported.astimezone(UTC).isoformat(),
        over_since=since,
        active=held,
    )


def problem(watch: ClockWatch) -> str | None:
    return CODE if watch.active else None
