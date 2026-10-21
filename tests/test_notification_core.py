"""The pure window notification core against the blueprint's cases (frozen clock)."""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_compass import notifications as core

TZ = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 9, 17, 10, tzinfo=UTC)
# tests/test_notification_blueprint.py DEFAULT_PREFERENCES, published by the integration
PREFERENCES = {
    "notify_sender": "integration",
    "notify_enabled": False,
    "notify_events": ["favorable", "limit"],
    "notify_minimum_hours": 2,
    "notify_limit_lead_minutes": 30,
    "notify_daily_max": 3,
    "notify_cooldown_minutes": 60,
    "quiet_start": "22:00:00",
    "quiet_end": "08:00:00",
    "actions_configured": True,
}
FRESH = core.SenderState()


def row(start, end, level, cost=0.2):
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "level": level,
        "cost_per_kwh": cost,
    }


def window(start, end, levels):
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "levels": list(levels),
    }


def data(now=NOW, *, favorable=(), limit=(), outlook=(), prefs=None, **overrides):
    published = {
        "valid": True,
        "status": "ready",
        "alert": None,
        "generated_at": now.isoformat(),
        "valid_until": (now + timedelta(hours=12)).isoformat(),
        "favorable_windows": list(favorable),
        "windows": {"limit": list(limit)},
        "outlook": list(outlook),
        "notification_preferences": {**PREFERENCES, **(prefs or {})},
    }
    published.update(overrides)
    return published


def run(
    published, state=FRESH, now=NOW, *, has_target=True, language="pl"
) -> core.Decision:
    return core.evaluate(
        published, state, now, TZ, has_target=has_target, language=language
    )


def cheap(now=NOW, hours=3, levels=("CHEAP",), **kwargs):
    end = now + timedelta(hours=hours)
    return data(
        now,
        favorable=[window(now, end, levels)],
        outlook=[row(now, end, levels[0])],
        **kwargs,
    )


def epoch(moment):
    return int(moment.timestamp())


def test_favorable_polish_copy_and_local_times():
    decision = run(cheap())

    assert decision.code == "send"
    assert decision.kind == "favorable"
    assert decision.title == "Dobry moment na domowe urządzenia"
    for text in ("12:00–15:00", "trwa teraz", "pranie", "zmywarkę", "ładowanie auta"):
        assert text in decision.message
    assert "darm" not in decision.message.lower()
    assert decision.window == core.Span(epoch(NOW), epoch(NOW + timedelta(hours=3)))


@pytest.mark.parametrize(
    ("outlook", "expected_title"),
    [
        (("BOOST", "BOOST"), "Wykorzystaj tanią energię"),
        (("CHEAP", "BOOST"), "Dobry moment na domowe urządzenia"),
    ],
    ids=["boost-boost", "cheap-boost"],
)
def test_boost_title_requires_whole_rest_boost(outlook, expected_title):
    middle = NOW + timedelta(hours=1)
    end = NOW + timedelta(hours=3)
    published = data(
        favorable=[window(NOW - timedelta(hours=1), end, ("CHEAP", "BOOST"))],
        outlook=[row(NOW, middle, outlook[0]), row(middle, end, outlook[1])],
    )

    assert run(published).title == expected_title


def test_boost_rest_with_a_gap_or_missing_rows_is_not_all_boost():
    end = NOW + timedelta(hours=3)
    gap = [
        row(NOW, NOW + timedelta(hours=1), "BOOST"),
        row(NOW + timedelta(hours=2), end, "BOOST"),
    ]
    short = [row(NOW, NOW + timedelta(hours=2), "BOOST")]

    assert not core.all_boost_rest(gap, NOW, epoch(end))
    assert not core.all_boost_rest(short, NOW, epoch(end))
    assert not core.all_boost_rest([], NOW, epoch(end))
    assert core.all_boost_rest([row(NOW, end, "BOOST")], NOW, epoch(end))


def test_future_limit_local_times():
    start = NOW + timedelta(minutes=30)
    end = NOW + timedelta(hours=2)
    published = data(
        limit=[window(start, end, ("LIMIT",))], outlook=[row(start, end, "LIMIT", 1.1)]
    )

    decision = run(published)

    assert decision.title == "Zaplanuj większe zużycie na później"
    assert "12:30–14:00" in decision.message
    assert "Według prognozy droższy okres" in decision.message


def test_ongoing_limit_says_already_expensive():
    start = NOW - timedelta(minutes=30)
    end = NOW + timedelta(hours=1)
    published = data(
        limit=[window(start, end, ("LIMIT",))], outlook=[row(NOW, end, "LIMIT", 1.1)]
    )

    message = run(published).message

    assert "Według prognozy dodatkowe zużycie jest już droższe" in message
    assert "11:30–13:00" in message
    assert "za 30" not in message


def test_overnight_window_names_both_local_dates():
    now = datetime(2026, 9, 17, 21, 30, tzinfo=UTC)
    published = cheap(
        now, hours=2, prefs={"quiet_start": "00:00:00", "quiet_end": "00:00:00"}
    )

    decision = run(published, now=now)

    assert decision.code == "send"
    assert "17.09 23:30–18.09 01:30" in decision.message


def test_quiet_hours_suppress_until_local_end():
    now = datetime(2026, 9, 17, 20, tzinfo=UTC)
    first = run(cheap(now, hours=14), now=now)
    assert first.code == "quiet"
    assert first.state == FRESH

    boundary = now + timedelta(hours=10)
    later = run(cheap(now, hours=14), first.state, now=boundary)
    assert later.code == "send"


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [(120, "send"), (119, "no_window")],
    ids=["120-send", "119-no_window"],
)
def test_favorable_minimum_applies_to_remaining_time(remaining, expected):
    end = NOW + timedelta(minutes=remaining)
    published = data(
        favorable=[window(NOW - timedelta(hours=2), end, ("CHEAP",))],
        outlook=[row(NOW, end, "CHEAP")],
    )

    assert run(published).code == expected


def test_daily_cap_resets_on_next_local_date():
    now = datetime(2026, 9, 17, 8, tzinfo=UTC)
    prefs = {
        "notify_daily_max": 1,
        "notify_cooldown_minutes": 60,
        "quiet_start": "00:00:00",
        "quiet_end": "00:00:00",
    }
    first = run(cheap(now, hours=2, prefs=prefs), now=now)

    second_at = now + timedelta(hours=3)
    second = run(cheap(second_at, hours=2, prefs=prefs), first.state, now=second_at)

    next_day = now + timedelta(days=1)
    third = run(cheap(next_day, hours=2, prefs=prefs), second.state, now=next_day)

    assert [first.code, second.code, third.code] == ["send", "cap", "send"]
    assert third.state.count == 1
    assert third.state.day == "2026-09-18"


def test_cooldown_blocks_independent_window_until_boundary():
    first = run(cheap(hours=2))
    assert first.code == "send"

    later = NOW + timedelta(minutes=30)
    limit = data(
        later,
        limit=[window(later, later + timedelta(hours=3), ("LIMIT",))],
        outlook=[row(later, later + timedelta(hours=3), "LIMIT")],
    )
    blocked = run(limit, first.state, now=later)
    assert blocked.code == "cooldown"
    assert blocked.state == first.state

    boundary = NOW + timedelta(hours=1)
    allowed = run(
        {**limit, "generated_at": boundary.isoformat()}, first.state, now=boundary
    )
    assert allowed.code == "send"
    assert allowed.kind == "limit"


def test_recalculation_and_boost_cheap_transition_keep_one_identity():
    first = run(cheap(hours=4, levels=("BOOST",)))
    assert first.code == "send"

    shifted = NOW + timedelta(hours=1)
    end = NOW + timedelta(hours=4, minutes=15)
    published = data(
        shifted,
        favorable=[window(shifted, end, ("CHEAP",))],
        outlook=[row(shifted, end, "CHEAP")],
    )
    second = run(published, first.state, now=shifted)

    assert second.code == "extended"
    assert second.state.favorable.s == first.state.favorable.s
    assert second.state.favorable.e == epoch(end)
    assert second.state.count == first.state.count
    assert second.state.last_sent == first.state.last_sent
    assert second.message is None


def test_overlapping_recalculations_extend_identity():
    first = run(cheap(hours=2))

    one_hour = NOW + timedelta(hours=1)
    four = NOW + timedelta(hours=4)
    second = run(
        data(
            one_hour,
            favorable=[window(one_hour, four, ("BOOST",))],
            outlook=[row(one_hour, four, "BOOST")],
        ),
        first.state,
        now=one_hour,
    )
    assert second.code == "extended"
    assert second.state.favorable.e == epoch(four)

    old_endpoint = NOW + timedelta(hours=2)
    five = NOW + timedelta(hours=5)
    third = run(
        data(
            old_endpoint,
            favorable=[window(old_endpoint, five, ("CHEAP",))],
            outlook=[row(old_endpoint, five, "CHEAP")],
        ),
        second.state,
        now=old_endpoint,
    )
    assert third.code == "extended"
    assert third.state.count == 1


def test_limit_selection_skips_ended_row():
    ended = window(NOW - timedelta(hours=1), NOW - timedelta(minutes=5), ("LIMIT",))
    upcoming = window(NOW + timedelta(minutes=20), NOW + timedelta(hours=1), ("LIMIT",))

    decision = run(data(limit=[ended, upcoming]))

    assert decision.code == "send"
    assert decision.kind == "limit"
    assert decision.window.s == epoch(NOW + timedelta(minutes=20))


def test_notified_active_limit_does_not_hide_independent_window():
    now = datetime(2026, 9, 17, 9, 30, tzinfo=UTC)
    first = window(
        now + timedelta(minutes=30), now + timedelta(hours=1, minutes=15), ("LIMIT",)
    )
    second = window(
        now + timedelta(hours=1, minutes=30),
        now + timedelta(hours=2, minutes=30),
        ("LIMIT",),
    )
    one = run(data(now, limit=[first, second]), now=now)

    later = now + timedelta(hours=1)
    two = run(data(later, limit=[first, second]), one.state, now=later)

    assert [one.kind, two.kind] == ["limit", "limit"]
    assert [one.code, two.code] == ["send", "send"]
    assert two.window.s == epoch(now + timedelta(hours=1, minutes=30))


def test_overlapping_limit_recalculations_extend_identity():
    first_start = NOW + timedelta(minutes=20)
    first_end = NOW + timedelta(hours=1)
    first = run(data(limit=[window(first_start, first_end, ("LIMIT",))]))
    assert first.code == "send"

    shifted = NOW + timedelta(minutes=30)
    extended_end = NOW + timedelta(hours=2)
    second = run(
        data(shifted, limit=[window(shifted, extended_end, ("LIMIT",))]),
        first.state,
        now=shifted,
    )
    assert second.code == "extended"
    assert second.state.limit.e == epoch(extended_end)

    third = run(
        data(
            first_end,
            limit=[window(first_end, NOW + timedelta(hours=2, minutes=30), ("LIMIT",))],
        ),
        second.state,
        now=first_end,
    )
    assert third.code == "extended"
    assert third.state.count == 1


def test_non_overlapping_later_window_is_independent():
    first = run(cheap(hours=2))

    later = NOW + timedelta(hours=3)
    second = run(cheap(later, hours=2, levels=("BOOST",)), first.state, now=later)

    assert [first.kind, second.kind] == ["favorable", "favorable"]
    assert [first.code, second.code] == ["send", "send"]
    assert second.state.count == 2


def test_limit_wins_when_both_qualify():
    published = data(
        favorable=[window(NOW, NOW + timedelta(hours=2), ("CHEAP",))],
        limit=[window(NOW, NOW + timedelta(hours=1), ("LIMIT",))],
        outlook=[row(NOW, NOW + timedelta(hours=2), "CHEAP")],
    )

    assert run(published).kind == "limit"


def test_favorable_is_sent_when_limit_is_not_enabled_for_the_event():
    published = {
        **cheap(),
        "windows": {
            "limit": [window(NOW, NOW + timedelta(hours=1), ("LIMIT",))],
        },
    }
    published["notification_preferences"] = {
        **PREFERENCES,
        "notify_events": ["favorable"],
    }

    assert run(published).kind == "favorable"

    published["notification_preferences"]["notify_events"] = []
    assert run(published).code == "no_window"


@pytest.mark.parametrize("failure", ["forecast", "alert", "optimizer", "expired"])
def test_gates_stop_before_bookkeeping(failure):
    overrides = {
        "forecast": {"valid": False},
        "alert": {"alert": {"code": "x", "reason": "y", "since": NOW.isoformat()}},
        "optimizer": {"status": "failed"},
        "expired": {"valid_until": (NOW - timedelta(seconds=1)).isoformat()},
    }[failure]
    state = core.SenderState(count=1, day="2026-09-17", last_sent=1.0)

    decision = run({**cheap(), **overrides}, state)

    assert decision.code == "gated"
    assert decision.state is state
    assert decision.message is None


@pytest.mark.parametrize("failure", ["empty", "no_generation"])
def test_empty_windows_and_unknown_plan_fail_closed(failure):
    if failure == "empty":
        decision = run(data())
        assert decision.code == "no_window"
    else:
        decision = run({**cheap(), "generated_at": None})
        assert decision.code == "gated"
    assert decision.state == FRESH


def test_no_target_skips_bookkeeping():
    decision = run(cheap(), has_target=False)

    assert decision.code == "no_target"
    assert decision.state == FRESH


def test_english_copy():
    start = NOW + timedelta(minutes=30)
    end = NOW + timedelta(hours=2)
    published = data(
        limit=[window(start, end, ("LIMIT",))], outlook=[row(start, end, "LIMIT", 1.1)]
    )

    decision = run(published, language="en")

    assert decision.title == "Plan heavy use for later"
    assert "the more expensive period is 12:30–14:00" in decision.message


def test_integration_preferences_are_not_clamped():
    start = NOW + timedelta(minutes=50)
    end = NOW + timedelta(hours=2)
    published = data(
        limit=[window(start, end, ("LIMIT",))],
        outlook=[row(start, end, "LIMIT", 1.1)],
        prefs={"notify_limit_lead_minutes": 60},
    )

    decision = run(published)

    assert decision.code == "send"
    assert decision.kind == "limit"


@pytest.mark.parametrize("case", ["blueprint", "off", "missing_key", "not_mapping"])
def test_disabled_unless_published_sender_is_integration(case):
    published = cheap()
    prefs = dict(published["notification_preferences"])
    if case in ("blueprint", "off"):
        prefs["notify_sender"] = case
    elif case == "missing_key":
        del prefs["notify_events"]
    if case == "not_mapping":
        published["notification_preferences"] = "integration"
    else:
        published["notification_preferences"] = prefs

    decision = run(published)

    assert decision.code == "disabled"
    assert decision.state == FRESH


def test_disabled_when_a_preference_is_unusable():
    for key, value in (("quiet_start", "late"), ("notify_daily_max", "many")):
        published = cheap(prefs={key: value})
        assert run(published).code == "disabled"


def test_cap_zero_never_sends():
    assert run(cheap(prefs={"notify_daily_max": 0})).code == "cap"


def test_quiet_equal_start_end_never_quiet():
    prefs = {"quiet_start": "12:00:00", "quiet_end": "12:00:00"}

    assert run(cheap(prefs=prefs)).code == "send"


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        (datetime(2026, 10, 25, 20, 59, tzinfo=UTC), "send"),  # 21:59 local
        (datetime(2026, 10, 25, 21, 0, tzinfo=UTC), "quiet"),  # 22:00 local
        (datetime(2026, 10, 25, 6, 59, tzinfo=UTC), "quiet"),  # 07:59 local
        (datetime(2026, 10, 25, 7, 0, tzinfo=UTC), "send"),  # 08:00 local
    ],
)
def test_quiet_hours_on_dst_day(utc, expected):
    # 2026-10-25 is the night the clocks go back (UTC+2 to UTC+1). Quiet hours
    # follow local wall time, so the 22:00 and 08:00 edges stay where they are.
    assert run(cheap(utc), now=utc).code == expected


def test_cap_reported_before_cooldown():
    state = core.SenderState(day="2026-09-17", count=1, last_sent=NOW.timestamp() - 60)

    decision = run(cheap(prefs={"notify_daily_max": 1}), state)

    assert decision.code == "cap"


def test_send_records_window_count_day_and_time():
    decision = run(cheap())

    assert decision.state.favorable == decision.window
    assert decision.state.limit is None
    assert decision.state.count == 1
    assert decision.state.day == "2026-09-17"
    assert decision.state.last_sent == NOW.timestamp()


def test_failures_survive_a_decision():
    state = core.SenderState(failures=2)

    assert run(cheap(), state).state.failures == 2


def test_all_texts_exact():
    label = "12:00–15:00"
    en_titles = {
        "limit": "Plan heavy use for later",
        "boost": "Use the cheap energy",
        "favorable": "A good time for household appliances",
        "test": "Energy Compass test",
    }
    pl_titles = {
        "limit": "Zaplanuj większe zużycie na później",
        "boost": "Wykorzystaj tanią energię",
        "favorable": "Dobry moment na domowe urządzenia",
        "test": "Test Energy Compass",
    }
    en = {
        "limit_ongoing": f"According to the forecast, extra use is already more expensive — period {label}. If you can, move laundry, the dishwasher or car charging to later.",
        "limit_ahead": f"According to the forecast, the more expensive period is {label}. If you can, run laundry, the dishwasher or car charging before or after it.",
        "boost": f"According to the forecast, a very favorable period {label} is on now. A good time for laundry, the dishwasher or car charging.",
        "favorable": f"According to the forecast, a favorable period {label} is on now. A good time for laundry, the dishwasher or car charging.",
        "test": "Notifications from Energy Compass reach this device.",
    }
    pl = {
        "limit_ongoing": f"Według prognozy dodatkowe zużycie jest już droższe — okres {label}. Jeśli możesz, przełóż pranie, zmywarkę lub ładowanie auta na później.",
        "limit_ahead": f"Według prognozy droższy okres to {label}. Jeśli możesz, zaplanuj pranie, zmywarkę lub ładowanie auta przed nim albo później.",
        "boost": f"Według prognozy bardzo korzystny okres {label} trwa teraz. To dobry moment na pranie, zmywarkę lub ładowanie auta.",
        "favorable": f"Według prognozy korzystny okres {label} trwa teraz. To dobry moment na pranie, zmywarkę lub ładowanie auta.",
        "test": "Powiadomienia z Energy Compass docierają na to urządzenie.",
    }
    for language, titles, messages in (("en", en_titles, en), ("pl", pl_titles, pl)):
        assert core.texts("limit", True, False, label, language) == (
            titles["limit"],
            messages["limit_ongoing"],
        )
        assert core.texts("limit", False, False, label, language) == (
            titles["limit"],
            messages["limit_ahead"],
        )
        assert core.texts("favorable", True, True, label, language) == (
            titles["boost"],
            messages["boost"],
        )
        assert core.texts("favorable", True, False, label, language) == (
            titles["favorable"],
            messages["favorable"],
        )
        assert core.test_texts(language) == (titles["test"], messages["test"])
    # a limit window is never worded as a favorable one, boost or not
    assert core.texts("limit", False, True, label, "en")[0] == en_titles["limit"]


def test_window_label_uses_en_dash():
    start = epoch(datetime(2026, 9, 17, 10, tzinfo=UTC))
    same_day = core.window_label(start, start + 3600, TZ)
    next_day = core.window_label(start, start + 20 * 3600, TZ)

    assert same_day == "12:00–13:00"
    assert next_day == "17.09 12:00–18.09 08:00"
    assert "–" in same_day and "-" not in same_day


def test_unparsable_row_times_are_skipped():
    end = NOW + timedelta(hours=3)
    published = data(
        favorable=[
            {"start": "garbage", "end": end.isoformat()},
            {"start": None, "end": None},
            "not a row",
            window(NOW, end, ("CHEAP",)),
        ],
        limit=[{"start": "garbage", "end": "garbage"}],
        outlook=[{"start": "garbage"}, row(NOW, end, "BOOST")],
    )

    decision = run(published)

    assert decision.code == "send"
    assert decision.kind == "favorable"
    assert decision.title == "Wykorzystaj tanią energię"


def test_windows_attribute_of_the_wrong_type_is_empty():
    for windows in (None, "x", {"limit": None}, {"limit": "x"}):
        assert run({**data(), "windows": windows}).code == "no_window"


STORED = {
    "format": 1,
    "favorable": {"s": 1789639200, "e": 1789650000},
    "limit": None,
    "day": "2026-09-17",
    "count": 1,
    "last_sent": 1789639200.0,
    "failures": 0,
}


def test_load_state_roundtrip():
    state, valid = core.load_state(STORED)

    assert valid
    assert state == core.SenderState(
        favorable=core.Span(1789639200, 1789650000),
        day="2026-09-17",
        count=1,
        last_sent=1789639200.0,
    )
    assert core.dump_state(state) == STORED
    assert core.load_state(core.dump_state(state)) == (state, True)
    assert core.dump_state(core.SenderState()) == {
        "format": 1,
        "favorable": None,
        "limit": None,
        "day": None,
        "count": 0,
        "last_sent": None,
        "failures": 0,
    }


@pytest.mark.parametrize(
    "case",
    ["not_dict", "format_2", "bad_span", "count_without_day", "negative_count"],
)
def test_load_state_rejects(case):
    raw = {
        "not_dict": ["x"],
        "format_2": {**STORED, "format": 2},
        "bad_span": {**STORED, "favorable": {"s": 5, "e": 5}},
        "count_without_day": {**STORED, "day": None},
        "negative_count": {**STORED, "count": -1},
    }[case]

    assert core.load_state(raw) == (core.SenderState(), False)


def test_load_state_rejects_other_type_violations():
    for change in (
        {"favorable": {"s": "1", "e": 2}},
        {"limit": [1, 2]},
        {"day": "yesterday"},
        {"day": 5},
        {"count": True},
        {"count": 1.5},
        {"last_sent": "now"},
        {"last_sent": True},
        {"failures": -1},
    ):
        assert core.load_state({**STORED, **change}) == (core.SenderState(), False)
    assert core.load_state(None) == (core.SenderState(), False)


def test_load_state_defaults_missing_failures():
    raw = {key: value for key, value in STORED.items() if key != "failures"}

    state, valid = core.load_state(raw)

    assert valid
    assert state.failures == 0


@pytest.mark.parametrize(
    ("setting", "ha_language", "expected"),
    [
        ("auto", "pl", "pl"),
        ("auto", "pl-PL", "pl"),
        ("auto", "en-GB", "en"),
        ("en", "pl", "en"),
        ("pl", "en", "pl"),
    ],
    ids=["auto-pl", "auto-pl-PL", "auto-en-GB", "en", "pl"],
)
def test_resolve_language(setting, ha_language, expected):
    assert core.resolve_language(setting, ha_language) == expected


def test_core_imports_no_homeassistant():
    source = Path(core.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert imported
    assert not [name for name in imported if name.split(".")[0] == "homeassistant"]
