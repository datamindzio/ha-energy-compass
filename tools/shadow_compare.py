"""Compare the native Deye decision with the blueprint's own, minute by minute, offline.

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
    DIFF    different and not explained by timing: a real difference
    NOREF   no blueprint decision in the dump

Exit status: 0 all EQUAL, 1 any DIFF, 2 only TIMING or STALE differences, 3 nothing
could be compared. The setup file:

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
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.energy_compass import controller_profile as profile
from custom_components.energy_compass import controller_texts as texts
from custom_components.energy_compass.controller import timestamp

ATTRIBUTES = ("min", "max", "step", "options", "current")
STALE_SECONDS = 75
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


def compare(
    states: Mapping, setup: Mapping, limits: profile.Limits, now: datetime, stale: float
) -> tuple[str, str, float | None, str | None]:
    """(result, detail, runtime age in seconds, when the runtime was written)."""
    runtime, written = reference(states, setup)
    if runtime is None:
        return "NOREF", "no blueprint decision in the dump", None, None
    zone = now.tzinfo
    snapshot = snapshot_of(states, setup, now, runtime)
    ceiling = {}
    native = decision_fields(profile.decide(snapshot, limits, ceiling))
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
    return "DIFF", "; ".join(found[:3]), age, written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dumps", type=Path, help="directory of per-minute state dumps")
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--stale-seconds", type=float, default=STALE_SECONDS)
    args = parser.parse_args(argv)
    setup = load_setup(args.setup)
    limits = limits_from(setup)
    zone = ZoneInfo(setup.get("timezone") or "UTC")
    files = sorted(path for path in args.dumps.iterdir() if path.suffix == ".json")
    if not files:
        print("no dumps found", file=sys.stderr)
        return 3
    counts = {"EQUAL": 0, "TIMING": 0, "STALE": 0, "DIFF": 0, "NOREF": 0}
    print(f"{'dump':<26} {'runtime written':<26} {'age s':>6}  {'result':<6}  detail")
    for path in files:
        dump_time = parse_time(path.stem).astimezone(zone)
        states = states_of(json.loads(path.read_text()))
        result, detail, age, written = compare(
            states, setup, limits, dump_time, args.stale_seconds
        )
        counts[result] += 1
        shown = "" if age is None else f"{age:.0f}"
        print(
            f"{dump_time.isoformat():<26} {written or '-':<26} {shown:>6}  "
            f"{result:<6}  {detail}"
        )
    print(", ".join(f"{key} {value}" for key, value in counts.items()))
    if counts["EQUAL"] + counts["TIMING"] + counts["STALE"] + counts["DIFF"] == 0:
        return 3
    if counts["DIFF"]:
        return 1
    if counts["TIMING"] or counts["STALE"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
