r"""Compare the counter SOC estimator's shadow run with the SOC template it replaces.

Read-only and offline: nothing here talks to Home Assistant. With the estimator in
shadow mode (Battery SOC source -> Shadow run) its diagnostic sensor
`sensor.<name>_soc_estimator` runs beside the template that still plans. This tool
aligns both series minute by minute and checks the acceptance of the migration:
the energy of the two differs by at most `--tolerance` kWh (default 0.2) in at
least `--share` of the minutes (default 95 %), and no full or empty anchor of one
is missing from the other within `--anchor-window` minutes.

Input is either the recorder history of the two entities

    curl -sk -H "Authorization: Bearer $TOKEN" \
      "$URL/api/history/period/2026-10-14T08:00:00Z?end_time=2026-10-15T08:00:00Z&filter_entity_id=sensor.a,sensor.b" \
      -o history.json
    python tools/soc_shadow.py --history history.json \
      --estimator sensor.a --template sensor.b --capacity 24

(a list of per-entity lists of states with `attributes` and `last_updated`, or an
object `{entity_id: [states]}`), or a directory of state dumps as
`tools/shadow_compare.py` reads them (one `GET /api/states` JSON per minute, named
by its ISO timestamp):

    python tools/soc_shadow.py DUMP_DIR --estimator sensor.a --template sensor.b

The estimator sensor records its state in percent only, so its energy is the state
times `--capacity`, the usable capacity the estimator was configured with (or the
sensor's own `capacity_kwh` attribute when the dump has it). The template's energy
is its `energy_kwh` attribute, else its state times the same capacity. Anchors are
read from the `anchor_kind` attribute and `anchor_at` where present.

Exit status: 0 acceptance met, 1 not met, 3 nothing could be compared.
"""

import argparse
import json
import re
import sys
from bisect import bisect_right
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path

MAX_STALE = timedelta(minutes=30)
ANCHOR_KINDS = ("full", "empty")
UNAVAILABLE = (None, "unknown", "unavailable", "")


def parse_time(raw: str) -> datetime:
    stem = re.sub(r"(?<=T)(\d\d)-(\d\d)-(\d\d)", r"\1:\2:\3", raw)
    stem = stem.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(stem)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _number(raw) -> float | None:
    if isinstance(raw, bool) or raw in UNAVAILABLE:
        return None
    try:
        value = float(raw)
    except TypeError, ValueError:
        return None
    return value if isfinite(value) else None


def load_history(path: Path, entities: tuple[str, ...]) -> dict[str, list[tuple]]:
    """`{entity: [(time, state, attributes)]}` from a recorder history response."""
    raw = json.loads(path.read_text())
    groups = raw.items() if isinstance(raw, Mapping) else enumerate(raw)
    series: dict[str, list[tuple]] = {entity: [] for entity in entities}
    for _, states in groups:
        for state in states:
            entity = state.get("entity_id")
            if entity not in series:
                continue
            stamp = state.get("last_updated") or state.get("last_changed")
            if stamp:
                series[entity].append(
                    (
                        parse_time(stamp),
                        state.get("state"),
                        state.get("attributes") or {},
                    )
                )
    if isinstance(raw, Mapping):
        for entity, states in raw.items():
            if entity in series and not series[entity]:
                series[entity] = [
                    (
                        parse_time(item.get("last_updated") or item["last_changed"]),
                        item.get("state"),
                        item.get("attributes") or {},
                    )
                    for item in states
                ]
    return {
        entity: sorted(rows, key=lambda row: row[0]) for entity, rows in series.items()
    }


def load_dumps(directory: Path, entities: tuple[str, ...]) -> dict[str, list[tuple]]:
    series: dict[str, list[tuple]] = {entity: [] for entity in entities}
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".reported.json"):
            continue
        stamp = parse_time(path.stem)
        for state in json.loads(path.read_text()):
            if state.get("entity_id") in series:
                series[state["entity_id"]].append(
                    (stamp, state.get("state"), state.get("attributes") or {})
                )
    return series


def _at(rows: list[tuple], times: list[datetime], moment: datetime):
    index = bisect_right(times, moment) - 1
    if index < 0 or moment - rows[index][0] > MAX_STALE:
        return None
    return rows[index]


def anchors(rows: list[tuple]) -> list[tuple[datetime, str]]:
    """Moments where a full or empty anchor appeared, read from `anchor_kind`."""
    found = []
    previous = None
    for stamp, _, attributes in rows:
        kind = attributes.get("anchor_kind")
        at = attributes.get("anchor_at")
        key = (kind, at)
        if kind in ANCHOR_KINDS and key != previous:
            found.append((parse_time(at) if isinstance(at, str) else stamp, kind))
        if kind is not None:
            previous = key
    return found


def compare(
    estimator: list[tuple],
    template: list[tuple],
    *,
    capacity: float | None,
    tolerance: float = 0.2,
    share: float = 0.95,
    anchor_window: int = 30,
) -> dict:
    """The acceptance report of one shadow period."""
    est_times = [row[0] for row in estimator]
    tpl_times = [row[0] for row in template]
    if not estimator or not template:
        return {"minutes": 0}
    start = max(estimator[0][0], template[0][0]).replace(second=0, microsecond=0)
    end = min(estimator[-1][0], template[-1][0])
    errors = []
    moment = start
    while moment <= end:
        a, b = _at(estimator, est_times, moment), _at(template, tpl_times, moment)
        moment += timedelta(minutes=1)
        if a is None or b is None:
            continue
        percent = _number(a[1])
        cap = capacity or _number(a[2].get("capacity_kwh"))
        energy = _number(b[2].get("energy_kwh"))
        if energy is None and _number(b[1]) is not None and cap:
            energy = _number(b[1]) * cap / 100
        if percent is None or cap is None or energy is None:
            continue
        errors.append(abs(percent * cap / 100 - energy))
    if not errors:
        return {"minutes": 0}
    ordered = sorted(errors)

    def percentile(fraction: float) -> float:
        return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]

    inside = sum(1 for error in errors if error <= tolerance) / len(errors)
    window = timedelta(minutes=anchor_window)
    ours, theirs = anchors(estimator), anchors(template)
    missing_in_estimator = [
        (at.isoformat(), kind)
        for at, kind in theirs
        if not any(k == kind and abs(a - at) <= window for a, k in ours)
        and start <= at <= end
    ]
    missing_in_template = [
        (at.isoformat(), kind)
        for at, kind in ours
        if not any(k == kind and abs(a - at) <= window for a, k in theirs)
        and start <= at <= end
    ]
    return {
        "minutes": len(errors),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "p50_kwh": round(percentile(0.50), 3),
        "p90_kwh": round(percentile(0.90), 3),
        "p95_kwh": round(percentile(0.95), 3),
        "p99_kwh": round(percentile(0.99), 3),
        "max_kwh": round(ordered[-1], 3),
        "share_within_tolerance": round(inside, 4),
        "tolerance_kwh": tolerance,
        "anchors_missing_in_estimator": missing_in_estimator,
        "anchors_missing_in_template": missing_in_template,
        "accepted": inside >= share
        and not missing_in_estimator
        and not missing_in_template,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dumps", nargs="?", type=Path, help="directory of state dumps")
    parser.add_argument("--history", type=Path, help="recorder history JSON")
    parser.add_argument("--estimator", required=True, help="estimator sensor entity id")
    parser.add_argument("--template", required=True, help="template SOC entity id")
    parser.add_argument("--capacity", type=float, help="estimator capacity in kWh")
    parser.add_argument("--tolerance", type=float, default=0.2)
    parser.add_argument("--share", type=float, default=0.95)
    parser.add_argument("--anchor-window", type=int, default=30, help="minutes")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    if (args.dumps is None) == (args.history is None):
        parser.error("give a dump directory or --history, not both")
    entities = (args.estimator, args.template)
    series = (
        load_history(args.history, entities)
        if args.history
        else load_dumps(args.dumps, entities)
    )
    report = compare(
        series[args.estimator],
        series[args.template],
        capacity=args.capacity,
        tolerance=args.tolerance,
        share=args.share,
        anchor_window=args.anchor_window,
    )
    if not report["minutes"]:
        print("nothing could be compared", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"{report['minutes']} minutes {report['from']} -> {report['to']}\n"
            f"|dE| kWh: p50 {report['p50_kwh']}  p90 {report['p90_kwh']}  "
            f"p95 {report['p95_kwh']}  p99 {report['p99_kwh']}  max {report['max_kwh']}\n"
            f"within {args.tolerance} kWh: {report['share_within_tolerance']:.1%} "
            f"(needs {args.share:.0%})\n"
            f"anchors missing in the estimator: {report['anchors_missing_in_estimator']}\n"
            f"anchors missing in the template: {report['anchors_missing_in_template']}\n"
            f"{'ACCEPTED' if report['accepted'] else 'NOT ACCEPTED'}"
        )
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
