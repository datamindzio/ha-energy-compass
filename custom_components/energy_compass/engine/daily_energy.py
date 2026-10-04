"""Local-day energy budgets, using elapsed time across midnight and DST."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import InputError, Problem


def day_fractions(start: datetime, end: datetime, zone: ZoneInfo) -> dict[str, float]:
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    elapsed = (end_utc - start_utc).total_seconds()
    fractions: dict[str, float] = {}
    cursor = start_utc
    while cursor < end_utc:
        local_day = cursor.astimezone(zone).date()
        following_midnight = datetime.combine(
            local_day + timedelta(days=1), time.min, tzinfo=zone
        ).astimezone(UTC)
        boundary = min(end_utc, following_midnight)
        if boundary <= cursor:
            raise InputError("invalid local-day boundary")
        key = local_day.isoformat()
        fractions[key] = (
            fractions.get(key, 0.0) + (boundary - cursor).total_seconds() / elapsed
        )
        cursor = boundary
    return fractions


def daily_export_rows(problem: Problem, flows=None) -> list[dict]:
    """Combine observed energy today with each local day's planned flows."""
    zone = ZoneInfo(problem.timezone)
    today = problem.slots[0].start.astimezone(zone).date().isoformat()
    rows = {}
    for index, slot in enumerate(problem.slots):
        flow = flows[index] if flows is not None else None
        for day, fraction in day_fractions(slot.start, slot.end, zone).items():
            row = rows.setdefault(
                day,
                {
                    "date": day,
                    "observed_pv_kwh": problem.pv_generated_today_kwh
                    if day == today
                    else 0.0,
                    "observed_export_kwh": problem.grid_exported_today_kwh
                    if day == today
                    else 0.0,
                    "forecast_pv_kwh": 0.0,
                    "forecast_export_kwh": 0.0,
                },
            )
            row["forecast_pv_kwh"] += fraction * (
                slot.pv_kwh - (flow.curtail_kwh if flow else 0)
            )
            row["forecast_export_kwh"] += fraction * (
                flow.grid_export_kwh if flow else 0
            )
    for row in rows.values():
        row["remaining_export_kwh"] = (
            row["observed_pv_kwh"]
            + row["forecast_pv_kwh"]
            - row["observed_export_kwh"]
            - row["forecast_export_kwh"]
        )
    return list(rows.values())


def cumulative_export_bounds(problem: Problem) -> list[dict]:
    """Per-slot, per-overlapping-day prefix PV ceilings for `export_limit_scope="produced"`.

    One entry per (slot, local day) pair, in chronological slot order. Each
    entry's `export_ceiling_kwh` is the maximum cumulative export (grid export
    plus curtailment) allowed from that day's local midnight through the end
    of the slot: PV observed so far today plus the forecast accrued up to and
    including this slot, minus export already observed today, floored at zero
    so export already above PV is never retroactively forbidden -- only
    further export is constrained. The entries for one day sum, at its final
    slot, to the same bound as `daily_export_rows`' whole-day rule.
    """
    zone = ZoneInfo(problem.timezone)
    today = problem.slots[0].start.astimezone(zone).date().isoformat()
    cumulative_pv: dict[str, float] = {}
    entries = []
    for index, slot in enumerate(problem.slots):
        for day, fraction in day_fractions(slot.start, slot.end, zone).items():
            cumulative_pv[day] = cumulative_pv.get(day, 0.0) + fraction * slot.pv_kwh
            observed_pv = problem.pv_generated_today_kwh if day == today else 0.0
            observed_export = problem.grid_exported_today_kwh if day == today else 0.0
            entries.append(
                {
                    "slot_index": index,
                    "date": day,
                    "fraction": fraction,
                    "export_ceiling_kwh": max(
                        0.0, observed_pv + cumulative_pv[day] - observed_export
                    ),
                }
            )
    return entries
