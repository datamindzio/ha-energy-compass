"""One message at the start and one at the end of a grid outage through the native notifier."""

from types import SimpleNamespace

import test_notifier as base
from test_notifier import eligible, publish, settings_of, settle, stored

from custom_components.energy_compass import controller_grid as grid

SINCE = "2026-09-17T09:59:00+00:00"
LOST = "2026-09-17T09:58:45+00:00"
OUT = grid.GridWatch(
    source="entity",
    raw="off",
    present=False,
    active=True,
    since=SINCE,
    lost_at=LOST,
    off_since=LOST,
)
BACK = grid.GridWatch(
    source="entity",
    raw="on",
    present=True,
    lost_at=LOST,
    restored_at="2026-09-17T10:30:00+00:00",
)
FINE = grid.GridWatch(source="entity", raw="on", present=True)

make = base.make


class Inverter:
    """What the notifier needs of the controller: the grid watch and whether one is kept."""

    def __init__(self, watch, watched=True):
        self.state = SimpleNamespace(grid=watch)
        self.grid_watched = watched
        self.resolution = SimpleNamespace(prefix=None)
        self.device_id = None

    async def async_stop(self):
        pass


def attach(entry, watch, watched=True):
    inverter = Inverter(watch, watched)
    entry.runtime_data.controller = inverter
    return inverter


def no_window(**kwargs):
    return eligible(favorable_windows=[], outlook=[], **kwargs)


async def test_the_start_and_the_end_are_told_once_each(make, hass, hass_storage):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    inverter = attach(entry, OUT)

    for _ in range(3):
        publish(entry, no_window())
        await settle(hass)
    assert len(phones.to("mobile_app_a")) == 1
    assert len(phones.to("mobile_app_b")) == 1
    start = phones.to("mobile_app_a")[0]
    assert start["title"] == "Awaria sieci: dom działa na baterii"
    assert start["message"].startswith("Sieć zanikła o 11:58.")
    assert stored(hass_storage, entry)["outage_since"] == SINCE

    inverter.state.grid = BACK
    for _ in range(2):
        publish(entry, no_window())
        await settle(hass)
    assert len(phones.to("mobile_app_a")) == 2
    ended = phones.to("mobile_app_a")[1]
    assert ended["title"] == "Sieć wróciła"
    assert ended["message"] == "Sieć wróciła o 12:30 po 31 min."
    assert "outage_since" not in stored(hass_storage, entry)


async def test_quiet_hours_and_the_cap_do_not_hold_an_outage_back(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    attach(entry, OUT)
    publish(
        entry,
        no_window(prefs={"quiet_start": "00:00:00", "quiet_end": "23:59:00"}),
    )
    await settle(hass)
    assert len(phones.to("mobile_app_a")) == 1


async def test_a_restart_during_the_outage_does_not_tell_it_again(
    make, hass, hass_storage
):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    attach(entry, OUT)
    publish(entry, no_window())
    await settle(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    attach(entry, OUT)
    publish(entry, no_window())
    await settle(hass)
    assert len(phones.to("mobile_app_a")) == 1


async def test_without_a_grid_source_nothing_is_sent(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    attach(entry, OUT, watched=False)
    publish(entry, no_window())
    await settle(hass)
    assert phones.calls == []


async def test_without_a_target_the_start_is_not_spent(make, hass, hass_storage):
    load, phones = make
    entry = await load()
    settings_of(entry)["notify_services"] = []
    attach(entry, OUT)
    publish(entry, no_window())
    await settle(hass)
    assert phones.calls == []
    assert entry.runtime_data.notifier.state.outage_since is None


async def test_the_blueprint_sender_stays_silent(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    attach(entry, OUT)
    publish(entry, no_window(prefs={"notify_sender": "blueprint"}))
    await settle(hass)
    assert phones.calls == []


async def test_a_grid_that_never_left_sends_nothing(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    attach(entry, FINE)
    publish(entry, no_window())
    await settle(hass)
    assert phones.calls == []
