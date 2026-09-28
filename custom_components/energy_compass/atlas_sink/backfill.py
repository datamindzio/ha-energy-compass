"""Recorder-statistics backfill converter (S-011). Pure conversion plus idempotent enqueue.

5-minute statistics become one backfill-origin window each; hourly statistics become hourly rows
only for hours with no 5-minute data (5-minute wins, ADR-0009). Nothing is ever fabricated.
Re-runs are safe: the collector dedups by window_start / hour_start.
"""

from datetime import UTC, datetime
from typing import Any

from .outbox import Outbox

_HOUR_S = 3600


def _instant(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def _iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _metrics(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "start"}


def convert(stats: dict[str, list[dict]]) -> tuple[list[dict], list[dict]]:
    five = sorted(
        ((_instant(r["start"]), r) for r in stats.get("five_minute", [])),
        key=lambda p: p[0],
    )
    hourly = sorted(
        ((_instant(r["start"]), r) for r in stats.get("hourly", [])),
        key=lambda p: p[0],
    )
    covered = {int(t.timestamp()) // _HOUR_S for t, _ in five}
    windows = [
        {
            "window_start": _iso(t),
            "samples": 0,
            "origin": "backfill",
            **_metrics(r),
        }
        for t, r in five
    ]
    rows = [
        {"hour_start": _iso(t), **_metrics(r)}
        for t, r in hourly
        if int(t.timestamp()) // _HOUR_S not in covered
    ]
    return windows, rows


class Backfill:
    def __init__(self, stats: dict[str, Any], outbox: Outbox, clock: Any):
        self._stats = stats
        self._ob = outbox
        self._clock = clock

    async def run(self) -> None:
        windows, rows = convert(self._stats)
        for w in windows:
            self._ob.add("telemetry", w, _instant(w["window_start"]))
        for r in rows:
            self._ob.add("hourly", r, _instant(r["hour_start"]))
