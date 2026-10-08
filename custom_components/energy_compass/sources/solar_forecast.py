"""Hourly PV intervals from an Energy dashboard solar forecast platform."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..engine.models import InputError
from ..engine.normalize import Interval, aware, finite

_HISTORY_DAYS = 2


def _local_hour_start(moment: datetime, zone: ZoneInfo) -> datetime:
    local = moment.astimezone(zone)
    return moment - timedelta(
        minutes=local.minute, seconds=local.second, microseconds=local.microsecond
    )


def _parse_key(key: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(key).strip())
    except ValueError as err:
        raise InputError("Energy solar forecast timestamp invalid") from err
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InputError("Energy solar forecast timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def solar_forecast_intervals(
    wh_hours: Mapping[str, Any],
    now: datetime,
    timezone: str,
    *,
    horizon: timedelta = timedelta(hours=49),
) -> tuple[Interval, ...]:
    """Sum Wh per local hour as the Energy dashboard does; empty hours are 0 kWh."""
    aware(now, "now")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise InputError("invalid installation timezone") from err
    if not isinstance(wh_hours, Mapping):
        raise InputError("Energy solar forecast wh_hours must be a mapping")
    now_utc = now.astimezone(UTC)
    current = _local_hour_start(now_utc, zone)
    oldest = (now_utc - timedelta(days=_HISTORY_DAYS)).date().isoformat()
    limit = now_utc + horizon
    buckets: dict[datetime, float] = {}
    for key, raw in wh_hours.items():
        if str(key)[:10] < oldest:
            continue
        moment = _parse_key(key)
        if moment >= limit:
            continue
        try:
            value = finite(raw, "Energy solar forecast value")
        except InputError as err:
            raise InputError("Energy solar forecast value invalid") from err
        if value < 0:
            raise InputError("Energy solar forecast value invalid")
        bucket = _local_hour_start(moment, zone)
        if bucket >= current:
            buckets[bucket] = buckets.get(bucket, 0.0) + value
    if not buckets:
        raise InputError("Energy solar forecast has no current or future values")
    last = max(buckets)
    hours = int((last - current).total_seconds() // 3600) + 1
    return tuple(
        Interval(
            current + timedelta(hours=index),
            current + timedelta(hours=index + 1),
            buckets.get(current + timedelta(hours=index), 0.0) / 1000,
        )
        for index in range(hours)
    )
