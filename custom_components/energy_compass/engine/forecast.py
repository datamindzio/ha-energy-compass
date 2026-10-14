import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    ForecastSettings,
    InputError,
    LoadCoverageSegment,
    LoadForecastResult,
)
from .normalize import aware, finite


def _recency_weight(age_days: float, half_life_days: float) -> float:
    """Weight of a sample `age_days` old; 1.0 for every sample when the option is off."""
    if half_life_days == 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def forecast_load(
    history: tuple[tuple[datetime, float], ...],
    slots: tuple[tuple[datetime, datetime], ...],
    timezone: str,
    fallback_daily_kwh: float | None,
    settings: ForecastSettings,
) -> tuple[float, ...]:
    """Predict interval energy from complete local-hour weekday/weekend means.

    With a half-life each sample is weighted by 0.5 ** (age / half-life), so a level
    change shows up in the profile within a few days; the result stays a mean.
    """
    return forecast_load_with_quality(
        history, slots, timezone, fallback_daily_kwh, settings
    ).values


def forecast_load_with_quality(
    history: tuple[tuple[datetime, float], ...],
    slots: tuple[tuple[datetime, datetime], ...],
    timezone: str,
    fallback_daily_kwh: float | None,
    settings: ForecastSettings,
) -> LoadForecastResult:
    """Keep each history decision with the predicted interval energy."""
    try:
        local = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, TypeError) as err:
        raise InputError("invalid forecast timezone") from err
    if settings.lookback_days <= 0 or settings.minimum_samples <= 0:
        raise InputError("invalid forecast sample settings")
    if not slots:
        return LoadForecastResult((), ())
    first = aware(slots[0][0], "slot start").astimezone(UTC)
    cutoff = first - timedelta(days=settings.lookback_days)
    if settings.half_life_days < 0 or not math.isfinite(settings.half_life_days):
        raise InputError("invalid forecast half-life")
    buckets: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    seen = set()
    for start, energy in history:
        instant = aware(start, "history timestamp").astimezone(UTC)
        amount = finite(energy, "history energy")
        if amount < 0:
            raise InputError("history energy must be nonnegative")
        if instant in seen:
            raise InputError("duplicate history hour")
        seen.add(instant)
        if cutoff <= instant and instant + timedelta(hours=1) <= first:
            wall = instant.astimezone(local)
            group = (
                int(wall.weekday() >= 5) if settings.group_weekends else wall.weekday()
            )
            age_days = (first - instant).total_seconds() / 86400
            buckets[(group, wall.hour)].append(
                (amount, _recency_weight(age_days, settings.half_life_days))
            )
    if fallback_daily_kwh is not None:
        fallback = finite(fallback_daily_kwh, "fallback daily load")
        if fallback < 0:
            raise InputError("fallback daily load must be nonnegative")
    else:
        fallback = None
    result = []
    coverage = []
    for start, end in slots:
        cursor = aware(start, "slot start").astimezone(UTC)
        stop = aware(end, "slot end").astimezone(UTC)
        if stop <= cursor:
            raise InputError("slot end must follow start")
        energy = 0.0
        while cursor < stop:
            next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(
                hours=1
            )
            section_end = min(next_hour, stop)
            wall = cursor.astimezone(local)
            group = (
                int(wall.weekday() >= 5) if settings.group_weekends else wall.weekday()
            )
            samples = buckets[(group, wall.hour)]
            if len(samples) >= settings.minimum_samples:
                hourly = sum(amount * weight for amount, weight in samples) / sum(
                    weight for _, weight in samples
                )
                method = "history"
            elif settings.allow_fallback and fallback is not None:
                hourly = fallback / 24
                method = "fallback"
            else:
                raise InputError(
                    f"insufficient load history at {cursor.isoformat()}: "
                    f"{len(samples)}/{settings.minimum_samples} samples available/required"
                )
            energy += hourly * (section_end - cursor).total_seconds() / 3600
            coverage.append(
                LoadCoverageSegment(
                    cursor, section_end, method, len(samples), settings.minimum_samples
                )
            )
            cursor = section_end
        result.append(energy)
    return LoadForecastResult(tuple(result), tuple(coverage))
