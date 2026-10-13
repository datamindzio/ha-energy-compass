r"""Compare the native Deye decision with the blueprint's own, minute by minute, offline.

The native engine cannot run in Simulation beside a blueprint in Auto (one mode per
controller), so the comparison replays recorded states instead. Input: a directory
of Home Assistant state dumps, one file per minute, each the JSON of
`GET /api/states`, named by its ISO timestamp (`2026-10-12T10:00:00+00:00.json`,
`20261012T100000Z.json`); and a JSON file describing this installation. Read-only:
nothing here talks to Home Assistant.

For every dump the script builds the snapshot the native executor would see, runs
`controller_profile.decide()` and compares the result with the blueprint's decision,
which the blueprint stored in the controller runtime sensor (`runtime.desired`,
`state`, `reason`, ...). Results per minute:

    EQUAL   same decision
    TIMING  different, but equal when decided at the time the runtime was written:
            the dump was taken between the blueprint's run and a change of time
    STALE   different and the runtime is older than --stale-seconds: the blueprint
            has not run since, so the two were never meant to agree
    REFRESH different only in `retained` and/or `slot_energy`, with `desired` and
            `state` equal: the plan was being refreshed when the dump was taken
    REPORT  a dump without `.reported.json` where the native decision is invalid for
            the telemetry rule (a report older than 30 s) and the blueprint's is
            valid: the REST `last_reported` is a cached copy of `last_updated`, so a
            sensor whose value changes rarely looks stale; not a real difference
    DIFF    different and not explained by timing: a real difference
    NOREF   no blueprint decision in the dump

Exit status: 0 all EQUAL, 1 any DIFF, STALE or REPORT (a stuck or switched-off
blueprint must not look acceptable; --allow-stale downgrades STALE and
--allow-rest-reported downgrades REPORT to the TIMING status), 2 only TIMING, REFRESH
(and allowed STALE or REPORT) differences, 3 nothing could be compared.

The REST `GET /api/states` serves `last_reported` equal to `last_updated` (the
serialized state is cached), while in memory Home Assistant updates it on every
report. The telemetry rule needs each telemetry entity reported within 30 s, so
record the in-memory value too: next to each dump put a sidecar of the same base
name with the suffix `.reported.json` (`2026-10-13T08:28:06+00:00.json` and
`2026-10-13T08:28:06+00:00.reported.json`), a JSON object
`{entity_id: unix timestamp or ISO string}` for at least the telemetry entities. Its
values replace the dump's `last_reported` for the listed entities. Without a sidecar
the script still runs, marks affected minutes REPORT and warns on stderr at the end.

Collection loop, one dump and sidecar per minute, 5 s after the minute (adjust the
token file, address and the entity list; the sidecar template reads the live state
machine, so list the telemetry entities of the setup file):

    TOKEN=$(cat ~/.ha_token); URL=https://192.168.1.110:8123; OUT=dumps; mkdir -p "$OUT"
    ENTITIES="['sensor.soc','sensor.battery_voltage']"
    while true; do
      sleep $(( (65 - 10#$(date +%S)) % 60 ))
      T=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
      curl -sk -H "Authorization: Bearer $TOKEN" "$URL/api/states" -o "$OUT/.$T.json.tmp" &&
      curl -sk -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
        -d "{\"template\": \"{% set ns = namespace(d={}) %}{% for e in $ENTITIES %}{% set ns.d = ns.d | combine({e: as_timestamp(states[e].last_reported, none)}) %}{% endfor %}{{ ns.d | tojson }}\"}" \
        "$URL/api/template" -o "$OUT/.$T.reported.json.tmp" &&
      mv "$OUT/.$T.reported.json.tmp" "$OUT/$T.reported.json" &&
      mv "$OUT/.$T.json.tmp" "$OUT/$T.json"
      sleep 1
    done

The sidecar is renamed first, so a dump is never visible without its sidecar. The
setup file:

    {
      "timezone": "Europe/Warsaw",
      "controller": "sensor.<name>_deye_controller",
      "runtime": "sensor.<name>_deye_controller_runtime",
      "entities": {"charge": "number...", "discharge": "number...",
                   "grid": "number...", "operation": "select...",
                   "soc": "sensor...", "voltage": "sensor..."},
      "prefix": "<TOU entity prefix; default: the controller's program_prefix>",
      "limits": {"max_power_w": ..., "max_current": ..., "max_grid_current": ...,
                 "relinquish_current": ..., "hold_grid_current": ..., ...}
    }

    python tools/shadow_compare.py DUMP_DIR --setup setup.json
"""

import argparse
import json
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.energy_compass import controller_profile as profile
from custom_components.energy_compass import controller_texts as texts
from custom_components.energy_compass.controller import timestamp

ATTRIBUTES = ("min", "max", "step", "options", "current")
STALE_SECONDS = 75
RESULTS = ("EQUAL", "TIMING", "REFRESH", "REPORT", "STALE", "DIFF", "NOREF")
ROLES = ("charge", "discharge", "grid", "operation", "soc", "voltage")
# The blueprint writes its decision into the runtime in Polish, whatever the language.
LANGUAGE = "pl"


def parse_time(name: str) -> datetime:
    """The dump time from a file stem; both `10:00:00` and `100000` forms."""
    stem = re.sub(r"(?<=T)(\d\d)-(\d\d)-(\d\d)", r"\1:\2:\3", name)
    try:
        parsed = datetime.fromisoformat(stem)
    except ValueError:
        raise SystemExit(f"not an ISO timestamp: {name}") from None
    if parsed.tzinfo is None:
        raise SystemExit(f"timestamp without a time zone: {name}")
    return parsed


def load_setup(path: Path) -> dict:
    setup = json.loads(path.read_text())
    entities = setup.get("entities") or {}
    missing = [role for role in ROLES if not entities.get(role)]
    if missing or not setup.get("controller") or not setup.get("runtime"):
        raise SystemExit(
            "setup needs controller, runtime and entities for " + ", ".join(ROLES)
        )
    return setup


def limits_from(setup: Mapping) -> profile.Limits:
    values = dict(setup.get("limits") or {})
    entities = setup["entities"]
    for key in ("commissioned_battery_modes", "old_writers", "telemetry_entities"):
        if key in values:
            values[key] = tuple(values[key])
    values.setdefault("telemetry_entities", (entities["soc"], entities["voltage"]))
    try:
        return profile.Limits(**values)
    except TypeError as err:
        raise SystemExit(f"limits: {err}") from None


def states_of(dump: list) -> dict:
    return {item["entity_id"]: item for item in dump}


def sidecar_of(path: Path) -> Path:
    return path.with_name(f"{path.stem}.reported.json")


def apply_reported(states: dict, reported: Mapping) -> None:
    """Replace `last_reported` with the in-memory values from the sidecar."""
    for entity, value in reported.items():
        if entity not in states:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        if not isinstance(value, str):
            value = datetime.fromtimestamp(value, UTC).isoformat()
        states[entity] = {**states[entity], "last_reported": value}


def reference(states: Mapping, setup: Mapping) -> tuple[dict | None, str | None]:
    """The blueprint's stored decision and the time its runtime was written."""
    sensor = states.get(setup["runtime"])
    if sensor is None:
        return None, None
    attributes = sensor.get("attributes") or {}
    runtime = attributes.get("runtime")
    if not isinstance(runtime, Mapping) or "desired" not in runtime:
        return None, None
    return dict(runtime), attributes.get("updated_at") or sensor.get("last_updated")


def snapshot_of(
    states: Mapping,
    setup: Mapping,
    now: datetime,
    runtime: Mapping,
    shift: float = 0.0,
) -> profile.Snapshot:
    """The snapshot at `now`; `shift` moves every report time back, keeping the ages."""
    facts = {
        entity: profile.Fact(
            str(item.get("state")),
            MappingProxyType(
                {
                    key: (item.get("attributes") or {})[key]
                    for key in ATTRIBUTES
                    if key in (item.get("attributes") or {})
                }
            ),
            timestamp(item.get("last_reported") or item.get("last_updated"), 0.0)
            - shift,
        )
        for entity, item in states.items()
    }
    controller = states.get(setup["controller"])
    view = (
        None
        if controller is None or controller.get("state") == "unavailable"
        else dict(controller.get("attributes") or {})
    )
    attributes = view or {}
    mode_entity = attributes.get("mode_entity")
    mode = (
        states[mode_entity]["state"]
        if mode_entity in states
        else attributes.get("mode", "Off")
    )
    entities = setup["entities"]
    return profile.Snapshot(
        now=now,
        mode=mode,
        entities=profile.Entities(
            **{role: entities[role] for role in ROLES},
            prefix=setup.get("prefix") or attributes.get("program_prefix") or "",
        ),
        states=facts,
        controller=view,
        runtime=dict(runtime),
    )


def decision_fields(decision: profile.Decision) -> dict:
    """What the blueprint stores of its decision, with its Polish reason text."""
    return {
        "desired": decision.desired,
        "state": decision.state,
        "reason": texts.text(decision.reason_code, LANGUAGE, decision.reason_args),
        "code": "ok" if decision.valid else "blocked",
        "active_tou": decision.active_tou,
        "retained": decision.retained,
        "requested_mode": decision.requested_mode,
        "battery_mode_commissioned": decision.commissioned,
        "takeover_blocked": not decision.writers_safe,
        "accepted_generation": decision.generation,
        "original_deadline": decision.deadline,
        "slot_energy": decision.slot_energy,
    }


def differences(expected: Mapping, native: Mapping) -> list[str]:
    found = []
    for key, value in native.items():
        stored = expected.get(key)
        if key == "desired" and isinstance(stored, Mapping):
            for entity in sorted({*stored, *value}):
                if stored.get(entity) != value.get(entity):
                    found.append(
                        f"desired[{entity}]: blueprint {stored.get(entity)!r}"
                        f" native {value.get(entity)!r}"
                    )
        elif stored != value:
            found.append(f"{key}: blueprint {stored!r} native {value!r}")
    return found


def differing_keys(expected: Mapping, native: Mapping) -> set[str]:
    return {key for key, value in native.items() if expected.get(key) != value}


def compare(
    states: Mapping,
    setup: Mapping,
    limits: profile.Limits,
    now: datetime,
    stale: float,
    reported: bool = True,
) -> tuple[str, str, float | None, str | None]:
    """(result, detail, runtime age in seconds, when the runtime was written)."""
    runtime, written = reference(states, setup)
    if runtime is None:
        return "NOREF", "no blueprint decision in the dump", None, None
    zone = now.tzinfo
    snapshot = snapshot_of(states, setup, now, runtime)
    ceiling = {}
    decision = profile.decide(snapshot, limits, ceiling)
    native = decision_fields(decision)
    found = differences(runtime, native)
    stamp = timestamp(written, 0.0)
    age = now.timestamp() - stamp if stamp else None
    if not found:
        return "EQUAL", "", age, written
    if stamp:
        then = datetime.fromtimestamp(stamp, zone)
        # The same states with the same ages, decided when the blueprint ran.
        earlier = decision_fields(
            profile.decide(
                snapshot_of(
                    states, setup, then, runtime, shift=now.timestamp() - stamp
                ),
                limits,
                ceiling,
            )
        )
        if not differences(runtime, earlier):
            return "TIMING", "; ".join(found[:3]), age, written
    if age is not None and age > stale:
        return "STALE", "; ".join(found[:3]), age, written
    if (
        not reported
        and decision.reason_code == "telemetry_stale"
        and not decision.valid
        and runtime.get("code") == "ok"
    ):
        return "REPORT", "; ".join(found[:3]), age, written
    keys = differing_keys(runtime, native)
    if keys <= {"retained", "slot_energy"}:
        return "REFRESH", "; ".join(found[:3]), age, written
    return "DIFF", "; ".join(found[:3]), age, written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dumps", type=Path, help="directory of per-minute state dumps")
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--stale-seconds", type=float, default=STALE_SECONDS)
    parser.add_argument(
        "--allow-stale",
        action="store_true",
        help="report STALE minutes with exit status 2 instead of 1",
    )
    parser.add_argument(
        "--allow-rest-reported",
        action="store_true",
        help="report REPORT minutes with exit status 2 instead of 1",
    )
    args = parser.parse_args(argv)
    setup = load_setup(args.setup)
    limits = limits_from(setup)
    zone = ZoneInfo(setup.get("timezone") or "UTC")
    files = sorted(
        path
        for path in args.dumps.iterdir()
        if path.suffix == ".json" and not path.name.endswith(".reported.json")
    )
    if not files:
        print("no dumps found", file=sys.stderr)
        return 3
    counts = dict.fromkeys(RESULTS, 0)
    unreported = 0
    print(f"{'dump':<26} {'runtime written':<26} {'age s':>6}  {'result':<6}  detail")
    for path in files:
        dump_time = parse_time(path.stem).astimezone(zone)
        states = states_of(json.loads(path.read_text()))
        sidecar = sidecar_of(path)
        if sidecar.is_file():
            apply_reported(states, json.loads(sidecar.read_text()))
        else:
            unreported += 1
        result, detail, age, written = compare(
            states, setup, limits, dump_time, args.stale_seconds, sidecar.is_file()
        )
        counts[result] += 1
        shown = "" if age is None else f"{age:.0f}"
        print(
            f"{dump_time.isoformat():<26} {written or '-':<26} {shown:>6}  "
            f"{result:<6}  {detail}"
        )
    print(", ".join(f"{key} {value}" for key, value in counts.items()))
    if unreported:
        print(
            f"{unreported} dumps without .reported.json: last_reported comes from the"
            " REST cache and may be stale; telemetry-age differences are not reliable",
            file=sys.stderr,
        )
    if counts["NOREF"] == sum(counts.values()):
        return 3
    if (
        counts["DIFF"]
        or (counts["STALE"] and not args.allow_stale)
        or (counts["REPORT"] and not args.allow_rest_reported)
    ):
        return 1
    if any(counts[key] for key in ("TIMING", "REFRESH", "STALE", "REPORT")):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
