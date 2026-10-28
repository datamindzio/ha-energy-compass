"""Grid presence: sources, debounce, outage state, restart round trip."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass import controller_grid as grid

T0 = datetime(2026, 10, 27, 19, 12, tzinfo=UTC)
ABSENT = grid.GridReading(grid.SOURCE_ENTITY, "off", False)
PRESENT = grid.GridReading(grid.SOURCE_ENTITY, "on", True)
UNKNOWN = grid.GridReading(grid.SOURCE_ENTITY, "unavailable", None)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def feed(watch, steps):
    """Fold `(seconds, reading)` steps into the watch, returning every watch."""
    seen = []
    for seconds, reading in steps:
        watch = grid.observe(watch, reading, at(seconds))
        seen.append(watch)
    return seen


def test_the_empty_watch_assumes_the_grid_is_present():
    watch = grid.GridWatch()
    assert not watch.active and watch.present is None
    assert grid.next_deadline(watch) is None


@pytest.mark.parametrize(
    "presence,voltages,expected",
    [
        ("on", [], (grid.SOURCE_ENTITY, True)),
        ("off", ["230"], (grid.SOURCE_ENTITY, False)),
        # The binary sensor wins while it speaks; the voltages stand in when it does not.
        ("unavailable", ["231", "0"], (grid.SOURCE_VOLTAGE, True)),
        (None, ["0.0", "2", "12"], (grid.SOURCE_VOLTAGE, False)),
        (None, ["0", "99.9", "100"], (grid.SOURCE_VOLTAGE, True)),
        (None, ["unavailable", "unknown", "231"], (grid.SOURCE_VOLTAGE, True)),
        (None, ["unavailable", None, "abc"], (None, None)),
        ("unknown", [], (None, None)),
        (None, [], (None, None)),
    ],
)
def test_the_sources_fold_into_one_reading(presence, voltages, expected):
    reading = grid.read(presence, voltages)
    assert (reading.source, reading.present) == expected


def test_a_short_loss_is_ignored():
    watch = grid.GridWatch()
    states = feed(watch, [(0, ABSENT), (5, ABSENT), (10, PRESENT), (14, PRESENT)])
    assert not any(w.active for w in states)
    assert states[-1].off_since is None


def test_a_flap_restarts_the_debounce():
    watch = grid.GridWatch()
    states = feed(
        watch,
        [(0, ABSENT), (10, ABSENT), (11, PRESENT), (12, ABSENT), (26, ABSENT)],
    )
    assert not states[-1].active
    assert states[-1].off_since == at(12).isoformat()
    assert grid.observe(states[-1], ABSENT, at(27)).active


def test_an_outage_enters_after_the_debounce():
    watch = grid.GridWatch()
    first = grid.observe(watch, ABSENT, at(0))
    assert not first.active and first.off_since == at(0).isoformat()
    assert grid.next_deadline(first) == at(15)
    early = grid.observe(first, ABSENT, at(14.9))
    assert not early.active
    entered = grid.observe(early, ABSENT, at(15))
    assert entered.active
    assert entered.since == at(15).isoformat()
    assert entered.lost_at == at(0).isoformat()
    assert grid.next_deadline(entered) is None


def test_the_deadline_alone_enters_the_outage():
    """The timer fires with the same reading: the watch needs no new state change."""
    first = grid.observe(grid.GridWatch(), ABSENT, at(0))
    assert grid.observe(first, ABSENT, grid.next_deadline(first)).active


def test_the_outage_ends_after_five_minutes_of_grid():
    out = feed(grid.GridWatch(), [(0, ABSENT), (15, ABSENT)])[-1]
    assert out.active
    back = grid.observe(out, PRESENT, at(100))
    assert back.active and back.on_since == at(100).isoformat()
    assert grid.next_deadline(back) == at(400)
    assert grid.observe(back, PRESENT, at(399)).active
    ended = grid.observe(back, PRESENT, at(400))
    assert not ended.active
    assert ended.restored_at == at(100).isoformat()
    assert ended.lost_at == at(0).isoformat()
    assert ended.since is None and ended.on_since is None
    assert ended.as_attribute()["lost_at"] is None


def test_a_dip_while_restoring_restarts_the_stable_period():
    out = feed(grid.GridWatch(), [(0, ABSENT), (15, ABSENT)])[-1]
    states = feed(out, [(100, PRESENT), (200, ABSENT), (210, PRESENT), (500, PRESENT)])
    assert states[1].active and states[1].on_since is None
    assert states[-1].active
    assert states[-1].on_since == at(210).isoformat()
    assert grid.observe(states[-1], PRESENT, at(510)).active is False


def test_an_unreadable_source_keeps_what_was_known():
    out = feed(grid.GridWatch(), [(0, ABSENT), (15, ABSENT)])[-1]
    kept = grid.observe(out, UNKNOWN, at(3000))
    assert kept.active and kept.present is False
    assert kept.since == out.since
    present = grid.observe(grid.GridWatch(), PRESENT, at(0))
    assert grid.observe(present, UNKNOWN, at(3000)).present is True


def test_unreadable_after_a_loss_still_enters_on_time():
    first = grid.observe(grid.GridWatch(), ABSENT, at(0))
    assert grid.observe(first, UNKNOWN, at(20)).active


def test_no_source_resets_the_watch():
    out = feed(grid.GridWatch(), [(0, ABSENT), (15, ABSENT)])[-1]
    assert grid.observe(out, None, at(20)) == grid.GridWatch()


def test_the_debounce_follows_the_options():
    first = grid.observe(grid.GridWatch(), ABSENT, at(0), enter_seconds=60)
    assert not grid.observe(first, ABSENT, at(59), enter_seconds=60).active
    assert grid.observe(first, ABSENT, at(60), enter_seconds=60).active
    assert grid.next_deadline(first, enter_seconds=60) == at(60)


@pytest.mark.parametrize(
    "options,enter,stable",
    [
        (None, 15, 300),
        ({}, 15, 300),
        ({"outage_enter_seconds": 30, "outage_stable_minutes": 10}, 30, 600),
        ({"outage_enter_seconds": 5, "outage_stable_minutes": 1}, 5, 60),
        ({"outage_enter_seconds": 600, "outage_stable_minutes": 120}, 600, 7200),
        # Out of range or not a number: the default, never an exception.
        ({"outage_enter_seconds": 4, "outage_stable_minutes": 0}, 15, 300),
        ({"outage_enter_seconds": 601, "outage_stable_minutes": 121}, 15, 300),
        ({"outage_enter_seconds": "x", "outage_stable_minutes": True}, 15, 300),
    ],
)
def test_the_options_are_bounded(options, enter, stable):
    assert grid.enter_seconds_of(options) == enter
    assert grid.stable_seconds_of(options) == stable


def test_the_watch_survives_a_restart():
    out = feed(grid.GridWatch(), [(0, ABSENT), (15, ABSENT), (100, PRESENT)])[-1]
    again = grid.from_dict(out.to_dict())
    assert again == out
    assert again.active and again.on_since == at(100).isoformat()
    assert grid.observe(again, PRESENT, at(400)).active is False


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "x",
        [],
        {},
        {"active": True},
        {"active": True, "since": "not a time"},
        {"active": "yes", "since": "2026-10-27T19:12:00+00:00"},
    ],
)
def test_a_damaged_store_is_the_empty_watch(raw):
    assert not grid.from_dict(raw).active


def test_a_damaged_timer_is_dropped():
    watch = grid.from_dict({"present": False, "off_since": "garbage"})
    assert watch.off_since is None and grid.next_deadline(watch) is None


def test_a_naive_stored_time_reads_as_utc():
    watch = grid.GridWatch(off_since="2026-10-27T19:12:00")
    assert grid.next_deadline(watch) == at(15)


def test_the_attribute_names_what_other_automations_need():
    out = feed(grid.GridWatch(), [(0, ABSENT), (15, ABSENT), (100, PRESENT)])[-1]
    attribute = out.as_attribute()
    assert attribute["outage"] is True
    assert attribute["since"] == at(15).isoformat()
    assert attribute["lost_at"] == at(0).isoformat()
    assert attribute["restored_pending_since"] == at(100).isoformat()
    assert attribute["source"] == grid.SOURCE_ENTITY
