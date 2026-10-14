"""Backtest the per-hour load forecast: plain mean vs recency weighting vs level correction.

Every evaluation day is forecast at local midnight from the hours that ended before it
(the same input the coordinator gives `forecast_load`), then compared with the measured
hourly energy. Reported per window: MAE and bias (forecast - measured) in kWh per hour.

    python -m benchmarks.load_forecast_backtest --scenario level-change
    python -m benchmarks.load_forecast_backtest --scenario stable
    python -m benchmarks.load_forecast_backtest --recorder-db home-assistant_v2.db \
        --statistic-id sensor.house_energy --since 2026-09-01
    python -m benchmarks.load_forecast_backtest --csv hours.csv --since 2026-09-01

`--csv` holds `start_iso,kwh` rows of complete hours; `--recorder-db` reads the cumulative
`sum` of a statistic and differences it (hours with a gap are dropped, like the integration
does). The two synthetic scenarios are deterministic and only show the mechanism: a result
that decides a default must come from the household's own history.
"""

import argparse
import csv
import json
import random
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.energy_compass.engine.forecast import forecast_load
from custom_components.energy_compass.engine.models import ForecastSettings

TIMEZONE = "Europe/Warsaw"
MORNING = range(6, 13)
EVENING = range(15, 22)
HALF_LIVES = (2.0, 3.0, 5.0, 7.0, 10.0)
LEVEL_WINDOWS = (3, 7)


def synthetic_history(scenario: str, days: int = 70, seed: int = 7):
    """Hourly kWh for `days` days from local midnight 2026-08-01; `level-change` steps up."""
    rng = random.Random(seed)
    local = ZoneInfo(TIMEZONE)
    first = datetime(2026, 8, 1, tzinfo=local)
    change_day = days - 21 if scenario == "level-change" else None
    rows = []
    for index in range(days * 24):
        start = first + timedelta(hours=index)
        wall = start.astimezone(local)
        base = 0.45 + 0.35 * (6 <= wall.hour < 9) + 0.9 * (17 <= wall.hour < 22)
        if wall.weekday() >= 5:
            base *= 1.15
        if change_day is not None and index // 24 >= change_day:
            # Heating starts: mornings and nights rise a lot, the evening a little.
            base *= 1.9 if (wall.hour < 13 or wall.hour >= 22) else 1.1
        rows.append((start.astimezone(UTC), max(0.0, base * rng.uniform(0.75, 1.25))))
    return tuple(rows)


def read_csv(path: str):
    with open(path, newline="") as handle:
        return tuple(
            (datetime.fromisoformat(row[0]).astimezone(UTC), float(row[1]))
            for row in csv.reader(handle)
            if row and not row[0].startswith("start")
        )


def read_recorder(path: str, statistic_id: str):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT s.start_ts, s.sum FROM statistics s JOIN statistics_meta m "
            "ON s.metadata_id = m.id WHERE m.statistic_id = ? AND s.sum IS NOT NULL "
            "ORDER BY s.start_ts",
            (statistic_id,),
        ).fetchall()
    finally:
        db.close()
    hours = []
    previous_ts, previous_sum = None, None
    for start_ts, total in rows:
        if previous_ts is not None and start_ts - previous_ts == 3600:
            hours.append(
                (datetime.fromtimestamp(start_ts, UTC), max(0.0, total - previous_sum))
            )
        previous_ts, previous_sum = start_ts, total
    return tuple(hours)


def level_correction_forecast(history, day_start, lookback_days, window_days):
    """Plain profile times (mean of the last `window_days` / mean of the lookback)."""
    slots = tuple(
        (day_start + timedelta(hours=h), day_start + timedelta(hours=h + 1))
        for h in range(24)
    )
    settings = ForecastSettings(lookback_days=lookback_days, minimum_samples=2)
    profile = forecast_load(history, slots, TIMEZONE, None, settings)
    lookback = [
        e
        for s, e in history
        if day_start - timedelta(days=lookback_days)
        <= s
        < day_start - timedelta(hours=1)
    ]
    recent = [
        e
        for s, e in history
        if day_start - timedelta(days=window_days) <= s < day_start
    ]
    if not lookback or not recent:
        return profile
    ratio = (sum(recent) / len(recent)) / (sum(lookback) / len(lookback))
    ratio = min(2.0, max(0.5, ratio))
    return tuple(value * ratio for value in profile)


def evaluate(history, first_day, last_day, lookback_days):
    """{method: {window: [errors]}} for local days first_day..last_day inclusive."""
    local = ZoneInfo(TIMEZONE)
    measured = {start: energy for start, energy in history}
    errors = {}
    day = first_day
    while day <= last_day:
        day_start = datetime(day.year, day.month, day.day, tzinfo=local).astimezone(UTC)
        slots = tuple(
            (day_start + timedelta(hours=h), day_start + timedelta(hours=h + 1))
            for h in range(24)
        )
        known = tuple((s, e) for s, e in history if s + timedelta(hours=1) <= day_start)
        forecasts = {
            "plain": forecast_load(
                known,
                slots,
                TIMEZONE,
                None,
                ForecastSettings(lookback_days=lookback_days),
            )
        }
        for half_life in HALF_LIVES:
            forecasts[f"half-life {half_life:g} d"] = forecast_load(
                known,
                slots,
                TIMEZONE,
                None,
                ForecastSettings(lookback_days=lookback_days, half_life_days=half_life),
            )
        for window in LEVEL_WINDOWS:
            forecasts[f"level x last {window} d"] = level_correction_forecast(
                known, day_start, lookback_days, window
            )
        for name, values in forecasts.items():
            for hour, (start, _) in enumerate(slots):
                if start not in measured:
                    continue
                wall_hour = start.astimezone(local).hour
                for window, hours in (
                    ("morning 06-13", MORNING),
                    ("evening 15-22", EVENING),
                ):
                    if wall_hour in hours:
                        errors.setdefault(name, {}).setdefault(window, []).append(
                            values[hour] - measured[start]
                        )
        day += timedelta(days=1)
    return errors


def summarise(errors):
    rows = {}
    for name, windows in errors.items():
        rows[name] = {
            window: {
                "mae": sum(abs(e) for e in values) / len(values),
                "bias": sum(values) / len(values),
                "hours": len(values),
            }
            for window, values in windows.items()
        }
    return rows


def render(rows):
    plain = rows["plain"]
    lines = [f"{'method':<22}{'window':<15}{'MAE':>8}{'bias':>9}{'MAE vs plain':>14}"]
    for name, windows in rows.items():
        for window, stat in windows.items():
            change = stat["mae"] / plain[window]["mae"] - 1
            lines.append(
                f"{name:<22}{window:<15}{stat['mae']:>8.3f}{stat['bias']:>+9.3f}"
                f"{change:>+14.1%}"
            )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--scenario", choices=("level-change", "stable"))
    source.add_argument("--csv")
    source.add_argument("--recorder-db")
    parser.add_argument("--statistic-id")
    parser.add_argument("--since", help="first evaluation day, YYYY-MM-DD")
    parser.add_argument(
        "--until", help="last evaluation day, default the last full day"
    )
    parser.add_argument("--lookback-days", type=int, default=28)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.scenario:
        history = synthetic_history(args.scenario)
        last = history[-1][0].astimezone(ZoneInfo(TIMEZONE)).date()
        first = last - timedelta(days=20)
        last -= timedelta(days=1)
    else:
        if args.csv:
            history = read_csv(args.csv)
        else:
            if not args.statistic_id:
                parser.error("--recorder-db needs --statistic-id")
            history = read_recorder(args.recorder_db, args.statistic_id)
        if not args.since:
            parser.error("--since is required for recorded history")
        first = datetime.fromisoformat(args.since).date()
        last = (
            datetime.fromisoformat(args.until).date()
            if args.until
            else history[-1][0].astimezone(ZoneInfo(TIMEZONE)).date()
            - timedelta(days=1)
        )
    rows = summarise(evaluate(history, first, last, args.lookback_days))
    print(json.dumps(rows, indent=2) if args.json else render(rows))


if __name__ == "__main__":
    main()
