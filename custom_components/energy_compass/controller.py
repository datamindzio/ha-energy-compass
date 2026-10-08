"""Pure Deye controller state: plan acceptance, revocation, timing and runtime rules.

No Home Assistant imports: every rule is a port of the v0.1.35 blueprint templates
and is proven against that blueprint by the parity tests. `controller_ha` wires it
to Home Assistant.
"""

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from types import MappingProxyType
from typing import Any, Literal

PLAN_STATES = (
    "CHARGE_GRID",
    "CHARGE_PV",
    "SELF_CONSUME",
    "DISCHARGE_GRID",
    "HOLD",
    "CURTAIL",
)
ROW_NUMBERS = (
    "pv_kwh",
    "load_kwh",
    "charge_kwh",
    "discharge_kwh",
    "curtail_kwh",
    "end_soc_kwh",
    "grid_import_kwh",
    "grid_export_kwh",
    "buy_per_kwh",
    "sell_per_kwh",
)
SIGNED_ROW_NUMBERS = frozenset({"buy_per_kwh", "sell_per_kwh"})
MODES = ("Off", "Simulation", "Auto")
PLAN_REASONS = ("ok", "session", "revoked", "sources")
BATTERY_KEYS = (
    "capacity_kwh",
    "eta_charge",
    "eta_discharge",
    "charge_kw",
    "discharge_kw",
)
RUNTIME_KEYS = frozenset(
    [
        "accepted_generation",
        "original_deadline",
        "state",
        "desired",
        "requested_mode",
        "active_tou",
        "retained",
        "code",
        "reason",
        "warning",
        "takeover_blocked",
        "since",
        "battery_mode_commissioned",
        "reached_key",
        "slot_energy",
        "owned_session",
        "uncertain",
        "confirmed",
        "last_confirmation",
        "confirmed_mode",
    ]
)
RUNTIME_MAX_BYTES = 32768
SNAPSHOT_SCHEMA = 2
CONTROLLER_SCHEMA = 1
HISTORY_LIMIT = 50
ADVANCE_DELAY_SECONDS = 1.0
TOU_FIELDS = (
    ("time", "time"),
    ("power", "number"),
    ("voltage", "number"),
    ("soc", "number"),
    ("charging", "select"),
)
CONTROLLER_ATTRIBUTES = (
    "controller_schema",
    "mode",
    "mode_entity",
    "restore_pending",
    "session",
    "plan_reason",
    "status",
    "retained",
    "generation",
    "valid_until",
    "coverage_end",
    "accepted_at",
    "revoked_generation",
    "revoked_at",
    "revoked_reason",
    "capacity_kwh",
    "eta_charge",
    "eta_discharge",
    "charge_kw",
    "discharge_kw",
    "device_id",
    "program_prefix",
    "tou_problem",
    "next_tou",
    "accepted",
    "tou",
)
RUNTIME_SUMMARY_KEYS = (
    "reason",
    "state",
    "requested_mode",
    "confirmed_mode",
    "since",
    "warning",
    "takeover_blocked",
    "accepted_generation",
)
PACKAGE_IDENTITIES: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "mode": ("input_select", "energy_compass_deye_mode"),
        "session": ("input_boolean", "energy_compass_deye_session"),
        "session_start": ("input_datetime", "energy_compass_deye_session_start"),
        "restore_pending": ("input_boolean", "energy_compass_deye_restore_pending"),
        "snapshot": ("template", "energy_compass_deye_plan"),
        "runtime": ("template", "energy_compass_deye_runtime"),
    }
)
_STALE_RUNTIME_KEYS = frozenset({"revoked_generation", "revoked_at", "revoked_reason"})


@dataclass(frozen=True)
class Revocation:
    generation: str | None
    at: str
    reason: str


@dataclass(frozen=True)
class Event:
    at: str
    kind: Literal[
        "accepted",
        "revoked",
        "dropped",
        "mode",
        "imported",
        "auto_enabled",
        "device",
    ]
    generation: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class ControllerState:
    mode: str = "Off"
    restore_pending: bool = False
    accepted: Mapping[str, Any] | None = None
    revoked: Revocation | None = None
    runtime: Mapping[str, Any] = MappingProxyType({})
    runtime_written_at: str | None = None
    imported_at: str | None = None
    history: tuple[Event, ...] = field(default=())

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "restore_pending": self.restore_pending,
            "accepted": None if self.accepted is None else dict(self.accepted),
            "revoked": None
            if self.revoked is None
            else {
                "generation": self.revoked.generation,
                "at": self.revoked.at,
                "reason": self.revoked.reason,
            },
            "runtime": dict(self.runtime),
            "runtime_written_at": self.runtime_written_at,
            "imported_at": self.imported_at,
            "history": [
                {
                    "at": event.at,
                    "kind": event.kind,
                    "generation": event.generation,
                    "detail": event.detail,
                }
                for event in self.history
            ],
        }


class RuntimeInvalid(ValueError):
    def __init__(self, code: str, key: str | None = None):
        super().__init__(code if key is None else f"{code}: {key}")
        self.code = code
        self.key = key


class PackageError(ValueError):
    def __init__(self, code: str, role: str):
        super().__init__(f"{code}: {role}")
        self.code = code
        self.role = role


def is_number(value: Any) -> bool:
    """Home Assistant's `is_number` template test: any finite float()-able value."""
    try:
        number = float(value)
    except ValueError, TypeError:
        return False
    return math.isfinite(number)


def timestamp(value: Any, default: float = 0.0) -> float:
    """Home Assistant's `as_timestamp(value, default)` for datetimes and ISO strings."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return default
    else:
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _utc_iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat()


def valid_now(data: Mapping, now: datetime) -> bool:
    """The `forecast_valid` binary sensor: valid and before `valid_until`."""
    return bool(data.get("valid")) and now.timestamp() < timestamp(
        data.get("valid_until")
    )


def _rows_valid(rows: Any, t: float) -> tuple[bool, float | None, bool]:
    if not isinstance(rows, (list, tuple)) or not rows:
        return False, None, False
    ok = True
    covered = False
    previous_end = None
    for row in rows:
        if not isinstance(row, Mapping):
            return False, None, False
        start = timestamp(row.get("start"))
        end = timestamp(row.get("end"))
        if (
            start <= 0
            or end <= start
            or (previous_end is not None and start != previous_end)
        ):
            ok = False
        previous_end = end
        if start <= t < end:
            covered = True
        if row.get("state") not in PLAN_STATES:
            ok = False
        if not isinstance(row.get("balance_hold", False), bool):
            ok = False
        for key in ROW_NUMBERS:
            value = row.get(key)
            if (
                not is_number(value)
                or key not in SIGNED_ROW_NUMBERS
                and float(value) < -0.000001
            ):
                ok = False
    return ok, previous_end, covered


def _battery(data: Mapping, fallback: Mapping[str, float]) -> dict[str, float]:
    given = data.get("controller_parameters")
    if isinstance(given, Mapping) and all(
        is_number(given.get(key)) for key in BATTERY_KEYS
    ):
        return {key: float(given[key]) for key in BATTERY_KEYS}
    return {key: float(fallback[key]) for key in BATTERY_KEYS if key in fallback}


def _flag(data: Mapping, key: str) -> Any:
    return data.get(key, False)


def candidate(
    data: Mapping,
    *,
    session: float,
    accepted: Mapping | None,
    revoked: Revocation | None,
    now: datetime,
    battery: Mapping[str, float],
) -> dict | None:
    """Accept a publication as the executable plan, or None (v0.1.35 CANDIDATE)."""
    t = now.timestamp()
    rows = data.get("intervals", [])
    ok, coverage_end, covered = _rows_valid(rows, t)
    if not (ok and covered and isinstance(data.get("dispatch_policy"), Mapping)):
        return None
    generation = data.get("generated_at")
    g = timestamp(generation)
    if not (
        session <= g <= t
        and g > timestamp(accepted.get("generated_at") if accepted else None)
    ):
        return None
    if revoked is not None and (
        generation == revoked.generation or g <= timestamp(revoked.at)
    ):
        return None
    if not valid_now(data, now) or data.get("alert"):
        return None
    status = data.get("status")
    refreshing = _flag(data, "refreshing")
    retained = _flag(data, "plan_retained")
    if not (
        (status == "ready" and refreshing is False and retained is False)
        or (status == "calculating" and refreshing is True and retained is True)
    ):
        return None
    if timestamp(data.get("valid_until")) <= t:
        return None
    return {
        "schema": SNAPSHOT_SCHEMA,
        "session": session,
        "accepted_at": now.isoformat(),
        "generated_at": generation,
        "valid_until": data.get("valid_until"),
        "coverage_end": coverage_end,
        "intervals": json.loads(json.dumps(list(rows))),
        "dispatch_policy": json.loads(json.dumps(data["dispatch_policy"])),
        "battery": _battery(data, battery),
    }


def revoke(
    data: Mapping,
    *,
    accepted: Mapping | None,
    revoked: Revocation | None,
    now: datetime,
) -> Revocation | None:
    """Revocation for an error publication; None when the publication is no error."""
    status = data.get("status")
    alert = data.get("alert")
    if status in ("ready", "calculating") and not alert:
        return None
    generation = accepted.get("generated_at") if accepted else None
    reason = data.get("reason") or (alert or {}).get("reason") or ""
    at = (
        revoked.at
        if revoked is not None and revoked.generation == generation
        else _utc_iso(now)
    )
    return Revocation(generation, at, f"{status}: {reason}")


def plan_reason(
    data: Mapping,
    *,
    session: float,
    accepted: Mapping | None,
    revoked: Revocation | None,
    now: datetime,
) -> str:
    if not accepted or accepted.get("session") != session:
        return "session"
    if revoked is not None and accepted.get("generated_at") == revoked.generation:
        return "revoked"
    status = data.get("status")
    live_ok = not data.get("alert") and (
        status == "calculating"
        or (
            status == "ready"
            and valid_now(data, now)
            and data.get("generated_at") == accepted.get("generated_at")
            and _flag(data, "plan_retained") is False
            and _flag(data, "refreshing") is False
        )
    )
    return "ok" if live_ok else "sources"


def _remember(history: tuple[Event, ...], event: Event) -> tuple[Event, ...]:
    return (*history, event)[-HISTORY_LIMIT:]


def step(
    state: ControllerState,
    data: Mapping,
    *,
    session: float,
    now: datetime,
    fallback_battery: Mapping[str, float],
) -> ControllerState:
    """One publication: revoke first, then accept, recording what changed."""
    history = state.history
    revoked = state.revoked
    revocation = revoke(data, accepted=state.accepted, revoked=revoked, now=now)
    if revocation is not None:
        if revoked is None or revoked.generation != revocation.generation:
            history = _remember(
                history,
                Event(
                    _utc_iso(now),
                    "revoked",
                    revocation.generation,
                    revocation.reason,
                ),
            )
        revoked = revocation
    accepted = state.accepted
    snapshot = candidate(
        data,
        session=session,
        accepted=accepted,
        revoked=revoked,
        now=now,
        battery=fallback_battery,
    )
    if snapshot is not None:
        accepted = snapshot
        history = _remember(
            history, Event(_utc_iso(now), "accepted", snapshot["generated_at"])
        )
    if (accepted, revoked, history) == (state.accepted, state.revoked, state.history):
        return state
    return replace(state, accepted=accepted, revoked=revoked, history=history)


def validate_runtime(runtime: Any) -> dict:
    if not isinstance(runtime, Mapping):
        raise RuntimeInvalid("runtime_invalid")
    for key in runtime:
        if key not in RUNTIME_KEYS:
            raise RuntimeInvalid("runtime_invalid_key", str(key))
    try:
        text = json.dumps(dict(runtime))
    except TypeError, ValueError:
        raise RuntimeInvalid("runtime_invalid") from None
    if len(text.encode()) > RUNTIME_MAX_BYTES:
        raise RuntimeInvalid("runtime_too_large")
    return json.loads(text)


def runtime_summary(runtime: Mapping, restore_pending: bool) -> dict:
    return {
        **{key: runtime.get(key) for key in RUNTIME_SUMMARY_KEYS},
        "restore_pending": restore_pending,
    }


def _valid_snapshot(snapshot: Any) -> bool:
    return (
        isinstance(snapshot, Mapping)
        and snapshot.get("schema") == SNAPSHOT_SCHEMA
        and isinstance(snapshot.get("session"), (int, float))
        and isinstance(snapshot.get("intervals"), list)
        and isinstance(snapshot.get("dispatch_policy"), Mapping)
        and isinstance(snapshot.get("battery"), Mapping)
        and isinstance(snapshot.get("generated_at"), str)
    )


def load_state(
    raw: Any, *, session: float, now: datetime
) -> tuple[ControllerState, bool]:
    """Validate a stored document; unusable data yields the safe default."""
    if raw is None:
        return ControllerState(), True
    safe = ControllerState(mode="Off", restore_pending=True)
    try:
        if not isinstance(raw, Mapping) or raw.get("mode") not in MODES:
            return safe, False
        if not isinstance(raw.get("restore_pending"), bool):
            return safe, False
        accepted = raw.get("accepted")
        if accepted is not None and not _valid_snapshot(accepted):
            return safe, False
        revoked = None
        if raw.get("revoked") is not None:
            item = raw["revoked"]
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("at"), str)
                or not isinstance(item.get("reason"), str)
                or not (
                    item.get("generation") is None
                    or isinstance(item["generation"], str)
                )
            ):
                return safe, False
            revoked = Revocation(item["generation"], item["at"], item["reason"])
        runtime = validate_runtime(raw.get("runtime", {}))
        for key in ("runtime_written_at", "imported_at"):
            if raw.get(key) is not None and not isinstance(raw[key], str):
                return safe, False
        history = []
        for item in raw.get("history", []):
            if not isinstance(item, Mapping):
                return safe, False
            history.append(
                Event(
                    str(item["at"]),
                    item["kind"],
                    item.get("generation"),
                    str(item.get("detail", "")),
                )
            )
    except KeyError, TypeError, ValueError, RuntimeInvalid:
        return safe, False
    state = ControllerState(
        mode=raw["mode"],
        restore_pending=raw["restore_pending"],
        accepted=None if accepted is None else dict(accepted),
        revoked=revoked,
        runtime=runtime,
        runtime_written_at=raw.get("runtime_written_at"),
        imported_at=raw.get("imported_at"),
        history=tuple(history[-HISTORY_LIMIT:]),
    )
    if state.accepted is not None and state.accepted["session"] != session:
        state = replace(
            state,
            accepted=None,
            history=_remember(
                state.history,
                Event(_utc_iso(now), "dropped", state.accepted["generated_at"]),
            ),
        )
    return state, True


def next_tou(raw_times: Sequence[str | None], now_local: datetime) -> datetime | None:
    """Earliest next TOU program boundary (the package `next_tou` template)."""
    boundaries = []
    for raw in raw_times:
        if raw in (None, "unknown", "unavailable"):
            continue
        try:
            parsed = time.fromisoformat(raw)
        except ValueError, TypeError:
            continue
        at = datetime.combine(now_local.date(), parsed, tzinfo=now_local.tzinfo)
        if at <= now_local:
            at += timedelta(days=1)
        boundaries.append(at)
    return min(boundaries) if boundaries else None


def next_event(
    accepted: Mapping | None, tou: datetime | None, now: datetime
) -> datetime | None:
    """Earliest instant after `now` at which the blueprint must run again."""
    candidates = []
    if accepted:
        for row in accepted.get("intervals", []):
            candidates.append(timestamp(row.get("end")))
        candidates.append(timestamp(accepted.get("valid_until")))
        coverage_end = accepted.get("coverage_end")
        if is_number(coverage_end):
            candidates.append(float(coverage_end))
    if tou is not None:
        candidates.append(tou.timestamp())
    later = [value for value in candidates if value > now.timestamp()]
    return datetime.fromtimestamp(min(later), UTC) if later else None


@dataclass(frozen=True)
class TouFact:
    entity_id: str
    domain: str
    platform: str | None
    translation_key: str | None
    disabled: bool


@dataclass(frozen=True)
class TouResolution:
    prefix: str | None
    entities: tuple[str, ...]
    problem: str | None


def resolve_tou(facts: Iterable[TouFact]) -> TouResolution:
    """Find the 30 Solarman program entities and the shared entity id prefix."""
    by_key: dict[str, list[TouFact]] = {}
    for fact in facts:
        if fact.platform == "solarman" and fact.translation_key:
            by_key.setdefault(fact.translation_key, []).append(fact)
    found = []
    for number in range(1, 7):
        for name, domain in TOU_FIELDS:
            matches = [
                fact
                for fact in by_key.get(f"program_{number}_{name}", [])
                if fact.domain == domain and not fact.disabled
            ]
            if len(matches) != 1:
                return TouResolution(None, (), "tou_incomplete")
            found.append((number, name, matches[0].entity_id))
    first = found[0][2].partition(".")[2]
    prefix = first.removesuffix("1_time")
    if prefix == first or not all(
        entity_id.partition(".")[2] == f"{prefix}{number}_{name}"
        for number, name, entity_id in found
    ):
        return TouResolution(None, (), "tou_prefix")
    return TouResolution(prefix, tuple(item[2] for item in found), None)


@dataclass(frozen=True)
class PackageFacts:
    mode: str | None
    session: str | None
    session_start: float | None
    restore_pending: str | None
    snapshot: Mapping | None
    runtime: Mapping | None


@dataclass(frozen=True)
class PackageImport:
    state: ControllerState
    session: float
    dropped_keys: tuple[str, ...]
    snapshot_kept: bool


def import_package(
    facts: PackageFacts, *, battery: Mapping[str, float], now: datetime
) -> PackageImport:
    """Map the 0.1.36 package helpers onto controller state (design §1.7)."""
    for role in PACKAGE_IDENTITIES:
        if getattr(facts, role) is None:
            raise PackageError("package_missing", role)
    if facts.session != "on":
        raise PackageError("package_session_inactive", "session")
    if facts.mode not in MODES:
        raise PackageError("package_mode_invalid", "mode")
    if not is_number(facts.session_start) or float(facts.session_start) <= 0:
        raise PackageError("package_missing", "session_start")
    if facts.restore_pending not in ("on", "off"):
        raise PackageError("package_missing", "restore_pending")
    session = float(facts.session_start)
    snapshot = facts.snapshot
    kept = (
        isinstance(snapshot, Mapping)
        and snapshot.get("schema") == 1
        and is_number(snapshot.get("session"))
        and float(snapshot["session"]) == session
        and isinstance(snapshot.get("intervals"), list)
        and bool(snapshot["intervals"])
    )
    accepted = None
    if kept:
        accepted = json.loads(
            json.dumps(
                {
                    **snapshot,
                    "schema": SNAPSHOT_SCHEMA,
                    "session": session,
                    "battery": {key: float(battery[key]) for key in BATTERY_KEYS},
                }
            )
        )
    runtime = facts.runtime
    revoked = None
    if runtime.get("revoked_at"):
        revoked = Revocation(
            runtime.get("revoked_generation"),
            str(runtime["revoked_at"]),
            str(runtime.get("revoked_reason") or ""),
        )
    copied = {key: value for key, value in runtime.items() if key in RUNTIME_KEYS}
    dropped = tuple(
        sorted(
            key
            for key in runtime
            if key not in RUNTIME_KEYS and key not in _STALE_RUNTIME_KEYS
        )
    )
    stamp = _utc_iso(now)
    state = ControllerState(
        mode=facts.mode,
        restore_pending=facts.restore_pending == "on",
        accepted=accepted,
        revoked=revoked,
        runtime=validate_runtime(copied),
        imported_at=stamp,
        history=(Event(stamp, "imported", None, "package"),),
    )
    return PackageImport(state, session, dropped, kept)


def controller_options_change(
    old: Mapping | None, new: Mapping | None
) -> Literal["none", "live", "reload"]:
    old, new = old or {}, new or {}
    if bool(old.get("enabled")) != bool(new.get("enabled")):
        return "reload"
    if new.get("enabled") and old.get("device_id") != new.get("device_id"):
        return "live"
    return "none"


def controller_attributes(
    state: ControllerState,
    data: Mapping,
    *,
    session: float,
    now: datetime,
    mode_entity: str | None,
    fallback_battery: Mapping[str, float],
    tou: Mapping[str, Any],
    device_id: str | None,
    program_prefix: str | None,
    tou_problem: str | None,
    next_tou_at: datetime | None,
) -> dict:
    """The controller sensor attributes, in `CONTROLLER_ATTRIBUTES` order."""
    accepted = state.accepted or {}
    battery = accepted.get("battery") or fallback_battery
    values = {
        "controller_schema": CONTROLLER_SCHEMA,
        "mode": state.mode,
        "mode_entity": mode_entity,
        "restore_pending": state.restore_pending,
        "session": session,
        "plan_reason": plan_reason(
            data,
            session=session,
            accepted=state.accepted,
            revoked=state.revoked,
            now=now,
        ),
        "status": data.get("status"),
        "retained": data.get("status") == "calculating",
        "generation": accepted.get("generated_at"),
        "valid_until": accepted.get("valid_until"),
        "coverage_end": accepted.get("coverage_end"),
        "accepted_at": accepted.get("accepted_at"),
        "revoked_generation": state.revoked.generation if state.revoked else None,
        "revoked_at": state.revoked.at if state.revoked else None,
        "revoked_reason": state.revoked.reason if state.revoked else None,
        **{key: battery.get(key) for key in BATTERY_KEYS},
        "device_id": device_id,
        "program_prefix": program_prefix,
        "tou_problem": tou_problem,
        "next_tou": None if next_tou_at is None else next_tou_at.isoformat(),
        "accepted": dict(accepted),
        "tou": dict(tou),
    }
    return {key: values[key] for key in CONTROLLER_ATTRIBUTES}
