from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from ..config_models import PriceSource, resolve_numeric
from ..engine.models import InputError
from ..engine.normalize import Interval, finite, resample_prices
from .bindings import IntervalBinding, merge_continuations, parse_intervals
from .tariffs import band_rows, validate_schedule


def price_series(
    states: Mapping[str, Any],
    binding: IntervalBinding,
    now: datetime,
    *,
    multiplier: float = 1.0,
    addition_per_kwh: float = 0.0,
    continuations: tuple[Mapping[str, Any], ...] = (),
    required_slots: tuple[tuple[datetime, datetime], ...] = (),
) -> tuple[Interval, ...]:
    """Normalize a selected settlement series and apply one explicit tariff transform."""
    factor = finite(multiplier, "price multiplier")
    addition = finite(addition_per_kwh, "price addition")
    selected = replace(binding, value_kind="price")
    all_series = (parse_intervals(states, selected, now),) + tuple(
        parse_intervals(snapshot, selected, now) for snapshot in continuations
    )
    merged = merge_continuations(all_series)
    transformed = tuple(
        Interval(row.start, row.end, row.value * factor + addition) for row in merged
    )
    if required_slots:
        resample_prices(transformed, required_slots)
    return transformed


def raw_rce_prices(
    states: Mapping[str, Any],
    binding: IntervalBinding,
    now: datetime,
    *,
    multiplier: float = 1.0,
    addition_per_kwh: float = 0.0,
    continuations: tuple[Mapping[str, Any], ...] = (),
) -> tuple[Interval, ...]:
    """Treat raw RCE as market input with an explicit buy or sell transform."""
    if not binding.unit.endswith("/MWh"):
        raise InputError("raw RCE must declare currency per MWh")
    return price_series(
        states,
        binding,
        now,
        multiplier=multiplier,
        addition_per_kwh=addition_per_kwh,
        continuations=continuations,
    )


def fixed_price(
    rate_per_kwh: float, slots: tuple[tuple[datetime, datetime], ...]
) -> tuple[Interval, ...]:
    """Apply a user-selected fixed tariff to each requested interval."""
    rate = finite(rate_per_kwh, "fixed price")
    return tuple(Interval(start, end, rate) for start, end in slots)


def price_for_slots(
    source: PriceSource,
    states: Mapping[str, Any],
    now: datetime,
    slots: tuple[tuple[datetime, datetime], ...],
    currency: str,
    *,
    timezone: str | None = None,
    schedule_rates: Mapping[str, float] | None = None,
) -> tuple[float, ...]:
    """Resolve the selected fixed, forecast or schedule tariff with explicit currency matching."""
    if source.mode == "schedule":
        if (
            source.schedule is None
            or source.forecast
            or source.fixed is not None
            or currency != "PLN"
            or timezone is None
            or schedule_rates is None
            or not slots
        ):
            raise InputError("schedule price selection is incomplete")
        validate_schedule(source.schedule)
        prices = tuple(
            Interval(row.start, row.end, finite(schedule_rates[row.band], "price"))
            for row in band_rows(source.schedule, slots[0][0], slots[-1][1], timezone)
        )
    elif source.mode == "fixed":
        if source.fixed is None or source.forecast:
            raise InputError("fixed price selection is incomplete")
        if source.fixed.unit != f"{currency}/kWh":
            raise InputError("price currency mismatch")
        rate = resolve_numeric(source.fixed, states, now)
        prices = fixed_price(rate, slots)
    elif source.mode == "forecast":
        if not source.forecast or source.fixed is not None:
            raise InputError("forecast price selection is incomplete")
        for binding in source.forecast:
            if binding.unit not in (f"{currency}/kWh", f"{currency}/MWh"):
                raise InputError("price currency mismatch")
        factor = resolve_numeric(source.multiplier, states, now)
        addition = resolve_numeric(source.addition_per_kwh, states, now)
        rows = merge_continuations(
            tuple(
                parse_intervals(states, replace(binding, value_kind="price"), now)
                for binding in source.forecast
            )
        )
        floor = source.floor_per_kwh
        prices = tuple(
            Interval(
                row.start,
                row.end,
                (row.value if floor is None else max(row.value, floor)) * factor
                + addition,
            )
            for row in rows
        )
    else:
        raise InputError("invalid price selection mode")
    return resample_prices(prices, slots)
