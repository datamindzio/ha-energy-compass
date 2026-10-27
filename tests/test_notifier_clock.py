"""One message per inverter clock episode through the native notifier."""

from datetime import UTC, datetime

import test_notifier as base
from test_notifier import NOW, eligible, publish, settle, stored, tick

from custom_components.energy_compass import controller_clock as clock
from custom_components.energy_compass import notifications as core

ACTIVE = clock.ClockWatch(
    skew_seconds=2940.0,
    inverter_clock="2026-09-17T11:00:00+00:00",
    read_at=NOW.isoformat(),
    over_since=NOW.isoformat(),
    active=True,
)
HOLDING = clock.ClockWatch(skew_seconds=2940.0, over_since=NOW.isoformat())
FINE = clock.ClockWatch(skew_seconds=12.0)


make = base.make


class Inverter:
    """What the notifier needs of the controller: the watch and whether one is kept."""

    def __init__(self, watch, watched=True):
        self.clock = watch
        self.clock_watched = watched

    async def async_stop(self):
        pass


def attach(entry, watch, watched=True):
    inverter = Inverter(watch, watched)
    entry.runtime_data.controller = inverter
    return inverter


def no_window():
    return eligible(favorable_windows=[], outlook=[])


async def test_an_episode_is_told_once_whatever_the_updates(make, hass, hass_storage):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    attach(entry, ACTIVE)

    for _ in range(3):
        publish(entry, no_window())
        await settle(hass)

    assert len(phones.to("mobile_app_a")) == 1
    assert len(phones.to("mobile_app_b")) == 1
    message = phones.to("mobile_app_a")[0]
    assert message["title"] == "Zegar falownika Deye jest przestawiony"
    assert "+49 min" in message["message"]
    assert stored(hass_storage, entry)["clock_since"] == NOW.isoformat()


async def test_a_hold_not_yet_served_sends_nothing(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    attach(entry, HOLDING)
    publish(entry, no_window())
    await settle(hass)
    assert phones.calls == []


async def test_a_new_episode_after_recovery_is_told_again(make, hass, hass_storage):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    inverter = attach(entry, ACTIVE)
    publish(entry, no_window())
    await settle(hass)

    inverter.clock = FINE
    publish(entry, no_window())
    await settle(hass)
    assert "clock_since" not in stored(hass_storage, entry)

    inverter.clock = ACTIVE
    publish(entry, no_window())
    await settle(hass)
    assert len(phones.to("mobile_app_a")) == 2


async def test_a_restart_in_the_middle_of_an_episode_does_not_tell_it_again(
    make, hass, hass_storage
):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    attach(entry, ACTIVE)
    publish(entry, no_window())
    await settle(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    # a new controller has not served its hold yet, then serves it
    attach(entry, clock.ClockWatch())
    publish(entry, no_window())
    await settle(hass)
    attach(entry, ACTIVE)
    publish(entry, no_window())
    await settle(hass)

    assert len(phones.to("mobile_app_a")) == 1


async def test_without_a_watched_clock_nothing_is_sent_and_the_episode_ends(
    make, hass, hass_storage
):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    inverter = attach(entry, ACTIVE)
    publish(entry, no_window())
    await settle(hass)
    inverter.clock_watched = False
    publish(entry, no_window())
    await settle(hass)
    assert "clock_since" not in stored(hass_storage, entry)


async def test_the_native_sender_must_be_the_published_one(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    attach(entry, ACTIVE)
    publish(entry, eligible(favorable_windows=[], prefs={"notify_sender": "blueprint"}))
    await settle(hass)
    assert phones.calls == []


async def test_quiet_hours_hold_the_message_until_they_end(make, hass, freezer):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    attach(entry, ACTIVE)
    night = datetime(2026, 9, 17, 20, tzinfo=UTC)  # 22:00 in Warsaw: quiet
    freezer.move_to(night)
    publish(entry, eligible(night, favorable_windows=[], outlook=[]))
    await settle(hass)
    assert phones.calls == []

    freezer.move_to(datetime(2026, 9, 18, 6, tzinfo=UTC))
    await tick(hass, freezer, 60)
    assert len(phones.to("mobile_app_a")) == 1


async def test_the_message_does_not_spend_the_window_counters(make, hass, hass_storage):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    attach(entry, ACTIVE)
    publish(entry, no_window())
    await settle(hass)
    saved = stored(hass_storage, entry)
    assert saved["count"] == 0
    assert saved["last_sent"] is None


def test_the_store_round_trips_the_episode_and_rejects_a_bad_one():
    state = core.SenderState(clock_since=NOW.isoformat())
    assert core.load_state(core.dump_state(state)) == (state, True)
    raw = core.dump_state(state)
    raw["clock_since"] = 5
    assert core.load_state(raw) == (core.SenderState(), False)
    assert "clock_since" not in core.dump_state(core.SenderState())


def test_the_english_text_names_the_skew_and_the_fix():
    title, message = core.clock_texts(-49, "en")
    assert "-49 min" in message
    assert "inverter panel" in message
    assert title == "Deye inverter clock is off"
    assert "+60 min" in core.clock_texts(60, "en")[1]
