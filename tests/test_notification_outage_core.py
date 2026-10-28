"""One message at the start and one at the end of a grid outage (pure core)."""

from dataclasses import replace

import pytest
from test_notification_core import FRESH, NOW, TZ, data

from custom_components.energy_compass import notifications as core

SINCE = "2026-09-17T08:00:15+00:00"
LOST = "2026-09-17T08:00:00+00:00"
BACK = "2026-09-17T09:30:00+00:00"
FACTS = core.OutageFacts(soc=64.0, load_kw=1.0, capacity_kwh=10.0)


def start(published=None, state=FRESH, facts=FACTS, **kwargs):
    arguments = {
        "active": True,
        "since": SINCE,
        "lost_at": LOST,
        "restored_at": None,
        "facts": facts,
        "now": NOW,
        "tz": TZ,
        "has_target": True,
        "language": "en",
        **kwargs,
    }
    return core.evaluate_outage(published or data(), state, **arguments)


def end(state, published=None, **kwargs):
    arguments = {
        "active": False,
        "since": None,
        "lost_at": LOST,
        "restored_at": BACK,
        "facts": FACTS,
        "now": NOW,
        "tz": TZ,
        "has_target": True,
        "language": "en",
        **kwargs,
    }
    return core.evaluate_outage(published or data(), state, **arguments)


def test_the_start_names_the_time_the_battery_and_the_runtime():
    decision = start()
    assert decision.code == "send"
    assert decision.title == "Grid outage: the house runs on the battery"
    # 10:00 local (UTC+2) is NOW; the grid was lost at 10:00 local too (08:00 UTC)
    assert decision.message == (
        "Grid lost at 10:00. Battery 64 %, about 5 h 24 min at the current 1.0 kW."
    )
    assert decision.state == replace(FRESH, outage_since=SINCE)


def test_the_polish_start_text():
    decision = start(language="pl")
    assert decision.title == "Awaria sieci: dom działa na baterii"
    assert decision.message == (
        "Sieć zanikła o 10:00. Bateria 64 %, wystarczy na ok. 5 godz. 24 min "
        "przy obecnym poborze 1.0 kW."
    )


@pytest.mark.parametrize(
    ("facts", "message"),
    [
        (
            core.OutageFacts(soc=64.0, load_kw=0.0, capacity_kwh=10.0),
            "Grid lost at 10:00. Battery 64 %.",
        ),
        (
            core.OutageFacts(soc=64.0, load_kw=None, capacity_kwh=10.0),
            "Grid lost at 10:00. Battery 64 %.",
        ),
        (
            core.OutageFacts(soc=64.0, load_kw=1.0, capacity_kwh=None),
            "Grid lost at 10:00. Battery 64 %.",
        ),
        (core.OutageFacts(), "Grid lost at 10:00."),
    ],
)
def test_what_is_unknown_is_left_out(facts, message):
    assert start(facts=facts).message == message


def test_a_battery_at_the_floor_has_no_runtime():
    facts = core.OutageFacts(soc=8.0, load_kw=1.0, capacity_kwh=10.0)
    assert start(facts=facts).message == "Grid lost at 10:00. Battery 8 %."


def test_an_episode_is_told_once():
    told = start().state
    assert start(state=told).code == "outage_told"
    assert start(state=told).state == told


def test_a_new_episode_is_told_again():
    told = start().state
    again = start(state=told, since="2026-09-17T09:59:00+00:00")
    assert again.code == "send"


def test_quiet_hours_the_daily_cap_and_the_cooldown_do_not_hold_an_outage_back():
    state = replace(
        FRESH,
        day="2026-09-17",
        count=99,
        last_sent=NOW.timestamp(),
    )
    published = data(prefs={"quiet_start": "00:00:00", "quiet_end": "23:59:00"})
    decision = start(published, state)
    assert decision.code == "send"
    assert decision.state.count == 99
    assert decision.state.last_sent == NOW.timestamp()


@pytest.mark.parametrize(
    ("published", "has_target", "code"),
    [
        (data(prefs={"notify_sender": "blueprint"}), True, "disabled"),
        (data(), False, "no_target"),
    ],
)
def test_without_notifications_or_a_target_the_episode_is_not_spent(
    published, has_target, code
):
    decision = start(published, has_target=has_target)
    assert decision.code == code
    assert decision.state == FRESH


def test_the_end_tells_the_time_and_the_duration_once():
    told = start().state
    decision = end(told)
    assert decision.code == "send"
    assert decision.title == "Grid is back"
    assert decision.message == "Grid back at 11:30 after 1 h 30 min. Battery 64 %."
    assert decision.state.outage_since is None
    assert end(decision.state).code == "outage_told"


def test_the_polish_end_text_and_a_short_outage():
    told = start().state
    decision = end(told, restored_at="2026-09-17T08:07:00+00:00", language="pl")
    assert decision.title == "Sieć wróciła"
    assert decision.message == "Sieć wróciła o 10:07 po 7 min. Bateria 64 %."


def test_the_end_without_a_battery_reading():
    decision = end(start().state, facts=core.OutageFacts())
    assert decision.message == "Grid back at 11:30 after 1 h 30 min."


def test_an_end_is_not_told_when_the_start_was_not():
    decision = end(FRESH)
    assert decision.code == "outage_told"
    assert decision.state == FRESH


@pytest.mark.parametrize(
    ("published", "has_target"),
    [(data(prefs={"notify_sender": "blueprint"}), True), (data(), False)],
)
def test_an_end_that_cannot_be_sent_is_forgotten(published, has_target):
    decision = end(start().state, published, has_target=has_target)
    assert decision.code == "outage_clear"
    assert decision.state == FRESH


def test_an_end_with_no_restore_time_is_forgotten():
    decision = end(start().state, restored_at=None)
    assert decision.code == "outage_clear"
    assert decision.state.outage_since is None


def test_the_episode_survives_the_store():
    told = start().state
    restored, valid = core.load_state(core.dump_state(told))
    assert valid and restored == told
    assert "outage_since" not in core.dump_state(FRESH)


@pytest.mark.parametrize("bad", [5, True, ["x"]])
def test_a_damaged_episode_is_a_fresh_state(bad):
    raw = {**core.dump_state(FRESH), "outage_since": bad}
    assert core.load_state(raw) == (FRESH, False)
