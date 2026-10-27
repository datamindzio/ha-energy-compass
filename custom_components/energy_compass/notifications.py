"""Window notification decisions, ported from the notification blueprint.

Pure: no Home Assistant import, the clock and the time zone are arguments. The
shell that stores the state and calls the notify targets lives elsewhere. Each
step of `evaluate` is the blueprint's action at the same position
(`blueprints/automation/energy_compass/notifications.yaml`, actions 257-505).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, tzinfo

EVENTS = ("favorable", "limit")
PREFERENCE_KEYS = (
    "notify_events",
    "notify_minimum_hours",
    "notify_limit_lead_minutes",
    "notify_daily_max",
    "notify_cooldown_minutes",
    "quiet_start",
    "quiet_end",
)
FAILED_ATTEMPTS_FOR_ISSUE = 3
SEND_TIMEOUT_S = 30
STORE_FORMAT = 1
SENDER_INTEGRATION = "integration"


@dataclass(frozen=True)
class Span:
    """A notified window: epoch seconds, the blueprint helper value {"s", "e"}."""

    s: int
    e: int

    def overlaps(self, s: float, e: float) -> bool:
        return s < self.e and e > self.s


@dataclass(frozen=True)
class SenderState:
    favorable: Span | None = None
    limit: Span | None = None
    day: str | None = None  # local ISO date the count belongs to
    count: int = 0
    last_sent: float | None = None  # epoch seconds of the last attempt
    failures: int = 0  # consecutive failed attempts
    clock_since: str | None = None  # start of the inverter clock episode already told


@dataclass(frozen=True)
class Preferences:
    events: frozenset[str]
    minimum_hours: float
    limit_lead_minutes: float
    daily_max: int
    cooldown_minutes: float
    quiet_start: time
    quiet_end: time


@dataclass(frozen=True)
class Decision:
    code: str  # gated|disabled|no_target|no_window|extended|quiet|cap|cooldown|send
    state: SenderState  # the state to keep (changed only by extended and send)
    kind: str | None = None  # "favorable" | "limit"
    window: Span | None = None
    title: str | None = None
    message: str | None = None


# Texts (spec §1.6). A notification has no translation context, so they live here.
_TITLES = {
    "en": {
        "limit": "Plan heavy use for later",
        "boost": "Use the cheap energy",
        "favorable": "A good time for household appliances",
        "test": "Energy Compass test",
        "clock": "Deye inverter clock is off",
    },
    "pl": {
        "limit": "Zaplanuj większe zużycie na później",
        "boost": "Wykorzystaj tanią energię",
        "favorable": "Dobry moment na domowe urządzenia",
        "test": "Test Energy Compass",
        "clock": "Zegar falownika Deye jest przestawiony",
    },
}
_MESSAGES = {
    "en": {
        "limit_ongoing": (
            "According to the forecast, extra use is already more expensive — "
            "period {label}. If you can, move laundry, the dishwasher or car "
            "charging to later."
        ),
        "limit_ahead": (
            "According to the forecast, the more expensive period is {label}. "
            "If you can, run laundry, the dishwasher or car charging before or "
            "after it."
        ),
        "boost": (
            "According to the forecast, a very favorable period {label} is on "
            "now. A good time for laundry, the dishwasher or car charging."
        ),
        "favorable": (
            "According to the forecast, a favorable period {label} is on now. "
            "A good time for laundry, the dishwasher or car charging."
        ),
        "test": "Notifications from Energy Compass reach this device.",
        "clock": (
            "The inverter clock differs from Home Assistant time by {skew} min "
            "(plus: the inverter is ahead), so its programs run at the wrong "
            "time. Set the time on the inverter panel or in the Deye app."
        ),
    },
    "pl": {
        "limit_ongoing": (
            "Według prognozy dodatkowe zużycie jest już droższe — okres {label}. "
            "Jeśli możesz, przełóż pranie, zmywarkę lub ładowanie auta na później."
        ),
        "limit_ahead": (
            "Według prognozy droższy okres to {label}. Jeśli możesz, zaplanuj "
            "pranie, zmywarkę lub ładowanie auta przed nim albo później."
        ),
        "boost": (
            "Według prognozy bardzo korzystny okres {label} trwa teraz. To dobry "
            "moment na pranie, zmywarkę lub ładowanie auta."
        ),
        "favorable": (
            "Według prognozy korzystny okres {label} trwa teraz. To dobry moment "
            "na pranie, zmywarkę lub ładowanie auta."
        ),
        "test": "Powiadomienia z Energy Compass docierają na to urządzenie.",
        "clock": (
            "Zegar falownika różni się od czasu Home Assistant o {skew} min "
            "(plus: falownik jest do przodu), więc jego programy działają o "
            "złej porze. Ustaw godzinę w panelu falownika albo w aplikacji Deye."
        ),
    },
}


def _language(language: str) -> str:
    return "pl" if language == "pl" else "en"


def _timestamp(value: object) -> float:
    """Epoch seconds of a published time, 0 when it does not parse.

    Like `as_timestamp(x, 0)` in the blueprint. A naive time is treated as
    unparsable: the plan always publishes aware times.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return 0.0
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.timestamp()
    return 0.0


def _rows(value: object) -> list[Mapping]:
    if not isinstance(value, (list, tuple)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def gates_open(data: Mapping, now: datetime) -> bool:
    """Step 1, also the re-check before the send."""
    return bool(
        data.get("valid")
        and not data.get("alert")
        and data.get("status") == "ready"
        and data.get("generated_at")
        and _timestamp(data.get("valid_until")) > now.timestamp()
    )


def _time(value: object) -> time | None:
    if isinstance(value, time):
        return value.replace(microsecond=0, tzinfo=None)
    if isinstance(value, str):
        try:
            return time.fromisoformat(value).replace(microsecond=0, tzinfo=None)
        except ValueError:
            return None
    return None


def preferences(data: Mapping) -> Preferences | None:
    """Step 2: None when the integration is not the published sender."""
    prefs = data.get("notification_preferences")
    if not isinstance(prefs, Mapping):
        return None
    if prefs.get("notify_sender") != SENDER_INTEGRATION:
        return None
    if any(key not in prefs for key in PREFERENCE_KEYS):
        return None
    events = prefs["notify_events"]
    quiet_start = _time(prefs["quiet_start"])
    quiet_end = _time(prefs["quiet_end"])
    if not isinstance(events, (list, tuple, set, frozenset)):
        return None
    if quiet_start is None or quiet_end is None:
        return None
    try:
        return Preferences(
            events=frozenset(str(event) for event in events),
            minimum_hours=float(prefs["notify_minimum_hours"]),
            limit_lead_minutes=float(prefs["notify_limit_lead_minutes"]),
            daily_max=int(prefs["notify_daily_max"]),
            cooldown_minutes=float(prefs["notify_cooldown_minutes"]),
            quiet_start=quiet_start,
            quiet_end=quiet_end,
        )
    except TypeError, ValueError:
        return None


def in_quiet_hours(prefs: Preferences, local: time) -> bool:
    """Step 8 on local wall time at second precision."""
    local = local.replace(microsecond=0, tzinfo=None)
    start, end = prefs.quiet_start, prefs.quiet_end
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


def all_boost_rest(outlook: list, now: datetime, end: int) -> bool:
    """Step 11: the rest of the window, from now to `end`, is BOOST throughout."""
    now_ts = now.timestamp()
    edge = now_ts
    valid = True
    seen = False
    for row in _rows(outlook):
        start = _timestamp(row.get("start"))
        row_end = _timestamp(row.get("end"))
        if row_end > now_ts and start < end:
            left = max(start, now_ts)
            right = min(row_end, end)
            if right > left:
                if left > edge + 1 or row.get("level") != "BOOST":
                    valid = False
                edge = max(edge, right)
                seen = True
    return seen and valid and edge >= end - 1


def window_label(s: int, e: int, tz: tzinfo) -> str:
    start = datetime.fromtimestamp(s, tz)
    end = datetime.fromtimestamp(e, tz)
    if start.date() == end.date():
        return f"{start:%H:%M}–{end:%H:%M}"
    return f"{start:%d.%m %H:%M}–{end:%d.%m %H:%M}"


def texts(
    kind: str, ongoing: bool, all_boost: bool, label: str, language: str
) -> tuple[str, str]:
    lang = _language(language)
    if kind == "limit":
        title = _TITLES[lang]["limit"]
        key = "limit_ongoing" if ongoing else "limit_ahead"
    elif all_boost:
        title = _TITLES[lang]["boost"]
        key = "boost"
    else:
        title = _TITLES[lang]["favorable"]
        key = "favorable"
    return title, _MESSAGES[lang][key].format(label=label)


def clock_texts(skew_minutes: int, language: str) -> tuple[str, str]:
    lang = _language(language)
    return _TITLES[lang]["clock"], _MESSAGES[lang]["clock"].format(
        skew=f"{skew_minutes:+d}"
    )


def test_texts(language: str) -> tuple[str, str]:
    lang = _language(language)
    return _TITLES[lang]["test"], _MESSAGES[lang]["test"]


# Not a test, although the name starts with "test_": keep pytest from collecting it.
test_texts.__test__ = False  # type: ignore[attr-defined]


def resolve_language(setting: str, ha_language: str) -> str:
    """`auto` follows the Home Assistant language: pl, otherwise en."""
    if setting == "auto":
        return "pl" if ha_language.lower().startswith("pl") else "en"
    return "pl" if setting == "pl" else "en"


def _limit_candidate(
    rows: list[Mapping], now_ts: float, lead_minutes: float, previous: Span | None
) -> Mapping | None:
    """Step 5: the first row not overlapping the notified span, else the first that does."""
    fresh: Mapping | None = None
    overlap: Mapping | None = None
    for row in rows:
        start = _timestamp(row.get("start"))
        end = _timestamp(row.get("end"))
        if start > 0 and end > now_ts and start <= now_ts + lead_minutes * 60:
            seen = previous is not None and previous.overlaps(start, end)
            if seen and overlap is None:
                overlap = row
            elif not seen and fresh is None:
                fresh = row
    return fresh if fresh is not None else overlap


def _favorable_candidate(
    rows: list[Mapping], now_ts: float, minimum_hours: float
) -> Mapping | None:
    """Step 4: the first row running now with the minimum time left."""
    for row in rows:
        start = _timestamp(row.get("start"))
        end = _timestamp(row.get("end"))
        if start > 0 and start <= now_ts and end - now_ts >= minimum_hours * 3600:
            return row
    return None


def evaluate(
    data: Mapping,
    state: SenderState,
    now: datetime,
    tz: tzinfo,
    *,
    has_target: bool,
    language: str,
) -> Decision:
    """The blueprint's actions in order; the first step that stops names the code."""
    if not gates_open(data, now):
        return Decision("gated", state)
    prefs = preferences(data)
    if prefs is None:
        return Decision("disabled", state)
    if not has_target:
        return Decision("no_target", state)

    now_ts = now.timestamp()
    windows = data.get("windows")
    limit_rows = _rows(windows.get("limit")) if isinstance(windows, Mapping) else []
    favorable = _favorable_candidate(
        _rows(data.get("favorable_windows")), now_ts, prefs.minimum_hours
    )
    limit = _limit_candidate(limit_rows, now_ts, prefs.limit_lead_minutes, state.limit)

    if "limit" in prefs.events and limit is not None:
        kind, row = "limit", limit
    elif "favorable" in prefs.events and favorable is not None:
        kind, row = "favorable", favorable
    else:
        return Decision("no_window", state)

    span = Span(int(_timestamp(row.get("start"))), int(_timestamp(row.get("end"))))
    previous = state.limit if kind == "limit" else state.favorable
    if previous is not None and previous.overlaps(span.s, span.e):
        merged = Span(min(previous.s, span.s), max(previous.e, span.e))
        kept = replace(state, **{kind: merged})
        return Decision("extended", kept, kind, merged)

    local = now.astimezone(tz)
    if in_quiet_hours(prefs, local.time()):
        return Decision("quiet", state, kind, span)

    local_day = local.date().isoformat()
    today = state.count if state.day == local_day else 0
    if today >= prefs.daily_max:
        return Decision("cap", state, kind, span)
    if (
        state.last_sent is not None
        and now_ts - state.last_sent < prefs.cooldown_minutes * 60
    ):
        return Decision("cooldown", state, kind, span)

    boost = kind == "favorable" and all_boost_rest(
        _rows(data.get("outlook")), now, span.e
    )
    title, message = texts(
        kind, span.s <= now_ts, boost, window_label(span.s, span.e, tz), language
    )
    sent = replace(
        state,
        day=local_day,
        count=today + 1,
        last_sent=now_ts,
        **{kind: span},
    )
    return Decision("send", sent, kind, span, title, message)


def evaluate_clock(
    data: Mapping,
    state: SenderState,
    since: str | None,
    skew_minutes: int | None,
    clear: bool,
    now: datetime,
    tz: tzinfo,
    *,
    has_target: bool,
    language: str,
) -> Decision:
    """One message per inverter clock episode.

    `since` and `skew_minutes` are set while the skew is over the threshold and held;
    `clear` says the clock is known to be fine (or is no longer watched), which ends
    an episode. Neither (a restart, a hold not yet served) keeps the stored episode,
    so a reload in the middle of an episode does not send it again.

    Codes: `clock_clear` (the stored episode is forgotten), `clock_told` (nothing to
    do), `disabled`, `no_target`, `quiet` (try again after the quiet hours, the
    episode is not spent) and `send`. The window counters, the daily cap and the
    cooldown are not touched: they belong to window advice.
    """
    if since is None or skew_minutes is None:
        if clear and state.clock_since is not None:
            return Decision("clock_clear", replace(state, clock_since=None))
        return Decision("clock_told", state)
    if state.clock_since is not None:
        return Decision("clock_told", state)
    prefs = preferences(data)
    if prefs is None:
        return Decision("disabled", state)
    if not has_target:
        return Decision("no_target", state)
    if in_quiet_hours(prefs, now.astimezone(tz).time()):
        return Decision("quiet", state)
    title, message = clock_texts(skew_minutes, language)
    return Decision(
        "send", replace(state, clock_since=since), None, None, title, message
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _load_span(raw: object) -> tuple[Span | None, bool]:
    if raw is None:
        return None, True
    if not isinstance(raw, Mapping) or not (
        _is_int(raw.get("s")) and _is_int(raw.get("e"))
    ):
        return None, False
    if raw["s"] >= raw["e"]:
        return None, False
    return Span(raw["s"], raw["e"]), True


def load_state(raw: object) -> tuple[SenderState, bool]:
    """Stored dict -> (state, valid). Any violation gives a fresh state and False."""
    fresh = (SenderState(), False)
    if not isinstance(raw, Mapping) or raw.get("format") != STORE_FORMAT:
        return fresh
    favorable, ok_favorable = _load_span(raw.get("favorable"))
    limit, ok_limit = _load_span(raw.get("limit"))
    if not (ok_favorable and ok_limit):
        return fresh
    day = raw.get("day")
    if day is not None:
        if not isinstance(day, str):
            return fresh
        try:
            date.fromisoformat(day)
        except ValueError:
            return fresh
    count = raw.get("count", 0)
    failures = raw.get("failures", 0)
    last_sent = raw.get("last_sent")
    if not (_is_int(count) and count >= 0 and _is_int(failures) and failures >= 0):
        return fresh
    if count > 0 and day is None:
        return fresh
    if last_sent is not None and (
        isinstance(last_sent, bool) or not isinstance(last_sent, (int, float))
    ):
        return fresh
    clock_since = raw.get("clock_since")
    if clock_since is not None and not isinstance(clock_since, str):
        return fresh
    state = SenderState(
        clock_since=clock_since,
        favorable=favorable,
        limit=limit,
        day=day,
        count=count,
        last_sent=None if last_sent is None else float(last_sent),
        failures=failures,
    )
    return state, True


def _dump_span(span: Span | None) -> dict | None:
    return None if span is None else {"s": span.s, "e": span.e}


def dump_state(state: SenderState) -> dict:
    return {
        "format": STORE_FORMAT,
        "favorable": _dump_span(state.favorable),
        "limit": _dump_span(state.limit),
        "day": state.day,
        "count": state.count,
        "last_sent": state.last_sent,
        "failures": state.failures,
        **({"clock_since": state.clock_since} if state.clock_since else {}),
    }
