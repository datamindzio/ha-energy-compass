"""The window notification sender in Home Assistant: triggers, store, calls, issue."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.energy_compass import notifications_ha
from custom_components.energy_compass.coordinator import EnergyCompassCoordinator
from custom_components.energy_compass.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.energy_compass.settings import default_configuration

NOW = datetime(2026, 9, 17, 10, tzinfo=UTC)
PREFERENCES = {
    "notify_sender": "integration",
    "notify_enabled": False,
    "notify_events": ["favorable", "limit"],
    "notify_minimum_hours": 2,
    "notify_limit_lead_minutes": 30,
    "notify_daily_max": 3,
    "notify_cooldown_minutes": 0,
    "quiet_start": "22:00:00",
    "quiet_end": "08:00:00",
    "actions_configured": True,
}
TITLE = "Dobry moment na domowe urządzenia"
MESSAGE = (
    "Według prognozy korzystny okres 12:00–15:00 trwa teraz. "
    "To dobry moment na pranie, zmywarkę lub ładowanie auta."
)


def eligible(now=NOW, hours=3, generated=None, prefs=None, **overrides):
    end = now + timedelta(hours=hours)
    published = {
        "valid": True,
        "status": "ready",
        "alert": None,
        "generated_at": (generated or now).isoformat(),
        "valid_until": (now + timedelta(hours=12)).isoformat(),
        "favorable_windows": [
            {
                "start": now.isoformat(),
                "end": end.isoformat(),
                "levels": ["CHEAP"],
            }
        ],
        "windows": {"limit": []},
        "intervals": [
            {
                "start": now.isoformat(),
                "end": end.isoformat(),
                "state": "HOLD",
                "buy_per_kwh": 0.2,
                "sell_per_kwh": 0.1,
            }
        ],
        "outlook": [
            {
                "start": now.isoformat(),
                "end": end.isoformat(),
                "level": "CHEAP",
                "cost_per_kwh": 0.2,
            }
        ],
        "notification_preferences": {**PREFERENCES, **(prefs or {})},
    }
    published.update(overrides)
    return published


class Phones:
    """Fake notify services that record their calls, in order."""

    def __init__(self, hass):
        self.hass = hass
        self.calls: list[tuple[str, dict]] = []
        self.gate: asyncio.Event | None = None
        self.delay = 0.0

    def register(self, *names):
        for name in names:

            async def handler(call, name=name):
                self.calls.append((name, dict(call.data)))
                if self.gate is not None:
                    await self.gate.wait()
                if self.delay:
                    await asyncio.sleep(self.delay)

            self.hass.services.async_register("notify", name, handler)

    def to(self, name):
        return [data for service, data in self.calls if service == name]


def settings_of(entry):
    return entry.runtime_data.configuration["settings"]


def key_of(entry):
    return f"energy_compass.{entry.entry_id}.notifications"


def stored(hass_storage, entry):
    return hass_storage[key_of(entry)]["data"]


def publish(entry, data):
    entry.runtime_data.async_set_updated_data(data)


async def settle(hass):
    await hass.async_block_till_done()


async def tick(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


@pytest.fixture
async def make(recorder_mock, hass, hass_storage, enable_custom_integrations, freezer):
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Europe/Warsaw")
    await async_setup_component(hass, "notify", {})
    phones = Phones(hass)
    recalc = patch.object(EnergyCompassCoordinator, "async_recalculate", AsyncMock())
    recalc.start()
    entries = []

    async def load(
        services=("mobile_app_a", "mobile_app_b"), entities=(), entry_id=None, **extra
    ):
        config = default_configuration("EUR", "UTC")
        config["settings"].update(
            notify_enabled=True,
            notify_services=list(services),
            notify_entities=list(entities),
            notify_language="pl",
            **extra,
        )
        entry = MockConfigEntry(
            domain="energy_compass",
            data=config,
            title="Synthetic",
            version=3,
            **({"entry_id": entry_id} if entry_id else {}),
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await settle(hass)
        entries.append(entry)
        return entry

    yield load, phones
    for entry in entries:
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
    recalc.stop()


async def test_coordinator_update_sends_once(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")

    publish(entry, eligible())
    await settle(hass)
    publish(entry, eligible())
    await settle(hass)

    expected = {"title": TITLE, "message": MESSAGE}
    assert phones.to("mobile_app_a") == [expected]
    assert phones.to("mobile_app_b") == [expected]


async def test_minute_tick_sends_at_quiet_end(make, hass, freezer):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    start = datetime(2026, 9, 17, 20, tzinfo=UTC)
    freezer.move_to(start)

    publish(entry, eligible(start, hours=12))
    await settle(hass)
    assert phones.calls == []

    freezer.move_to(datetime(2026, 9, 18, 6, tzinfo=UTC))
    async_fire_time_changed(hass)
    await settle(hass)

    assert len(phones.to("mobile_app_a")) == 1


async def test_gates_rechecked_after_save_before_send(make, hass, hass_storage):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    real = Store.async_save

    async def save(self, data, *args, **kwargs):
        await real(self, data, *args, **kwargs)
        if self.key == key_of(entry):
            publish(entry, eligible(alert={"code": "stale"}))

    with patch.object(Store, "async_save", save):
        publish(entry, eligible())
        await settle(hass)

    assert phones.calls == []
    assert stored(hass_storage, entry)["count"] == 1
    assert stored(hass_storage, entry)["favorable"] is not None


async def test_new_plan_between_save_and_send_stops(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    real = Store.async_save

    async def save(self, data, *args, **kwargs):
        await real(self, data, *args, **kwargs)
        if self.key == key_of(entry):
            publish(entry, eligible(generated=NOW + timedelta(seconds=5)))

    with patch.object(Store, "async_save", save):
        publish(entry, eligible())
        await settle(hass)

    assert phones.calls == []


async def test_save_happens_before_call(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    order = []
    real = Store.async_save

    async def save(self, data, *args, **kwargs):
        if self.key == key_of(entry):
            order.append("save")
        await real(self, data, *args, **kwargs)

    with patch.object(Store, "async_save", save):
        publish(entry, eligible())
        await settle(hass)
        order.extend(service for service, _ in phones.calls)

    assert order[:3] == ["save", "mobile_app_a", "mobile_app_b"]


async def test_save_failure_sends_nothing(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    real = Store.async_save

    async def save(self, data, *args, **kwargs):
        if self.key == key_of(entry):
            raise OSError("disk full")
        await real(self, data, *args, **kwargs)

    with patch.object(Store, "async_save", save):
        publish(entry, eligible())
        await settle(hass)

    assert phones.calls == []
    assert entry.runtime_data.notifier.state.failures == 1


async def test_restart_keeps_window_identity(make, hass, freezer, hass_storage):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    publish(entry, eligible())
    await settle(hass)
    first = stored(hass_storage, entry)
    assert len(phones.to("mobile_app_a")) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    freezer.tick(timedelta(hours=1))
    publish(entry, eligible(NOW, hours=4, generated=NOW + timedelta(hours=1)))
    await settle(hass)

    assert len(phones.to("mobile_app_a")) == 1
    assert stored(hass_storage, entry)["favorable"]["s"] == first["favorable"]["s"]
    assert stored(hass_storage, entry)["favorable"]["e"] > first["favorable"]["e"]


async def test_reload_keeps_window_identity(make, hass, freezer, hass_storage):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    publish(entry, eligible())
    await settle(hass)
    first = stored(hass_storage, entry)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    freezer.tick(timedelta(hours=1))
    publish(entry, eligible(NOW, hours=4, generated=NOW + timedelta(hours=1)))
    await settle(hass)

    assert len(phones.to("mobile_app_a")) == 1
    assert stored(hass_storage, entry)["favorable"]["s"] == first["favorable"]["s"]


async def test_corrupt_store_starts_fresh(make, hass, hass_storage, caplog):
    load, phones = make
    key = "energy_compass.fixed.notifications"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"format": 2},
    }
    with caplog.at_level(logging.WARNING):
        entry = await load(entry_id="fixed")
    phones.register("mobile_app_a", "mobile_app_b")
    warnings = [r for r in caplog.records if "Invalid stored notification" in r.message]
    publish(entry, eligible())
    await settle(hass)

    assert len(warnings) == 1
    assert len(phones.to("mobile_app_a")) == 1


async def test_store_removed_with_entry(make, hass, hass_storage):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    publish(entry, eligible())
    await settle(hass)
    assert key_of(entry) in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await settle(hass)

    assert key_of(entry) not in hass_storage


async def test_legacy_and_entity_targets_called(make, hass):
    load, phones = make
    entry = await load(services=["mobile_app_a"], entities=["notify.kitchen"])
    phones.register("mobile_app_a", "send_message")

    publish(entry, eligible())
    await settle(hass)

    assert phones.to("mobile_app_a") == [{"title": TITLE, "message": MESSAGE}]
    assert phones.to("send_message") == [
        {"title": TITLE, "message": MESSAGE, "entity_id": "notify.kitchen"}
    ]


async def test_one_failed_target_others_sent(make, hass, hass_storage):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a")
    await async_setup_component(hass, "logbook", {})
    events = async_capture_events(hass, "logbook_entry")

    publish(entry, eligible())
    await settle(hass)

    assert len(phones.to("mobile_app_a")) == 1
    assert entry.runtime_data.notifier.state.failures == 1
    assert stored(hass_storage, entry)["failures"] == 1
    assert len(events) == 1
    assert "sent to 1 of 2 targets" in events[0].data["message"]


async def test_target_timeout_counts_as_failure(make, hass, freezer):
    load, phones = make
    entry = await load(services=["mobile_app_a"])
    phones.register("mobile_app_a")
    phones.gate = asyncio.Event()

    with patch.object(notifications_ha, "SEND_TIMEOUT_S", 0.01):
        publish(entry, eligible())
        for _ in range(20):
            await asyncio.sleep(0)
            freezer.tick(timedelta(seconds=1))
            async_fire_time_changed(hass)
        await settle(hass)

    assert entry.runtime_data.notifier.state.failures == 1


async def test_three_failed_attempts_raise_issue_and_success_clears(
    make, hass, freezer
):
    load, phones = make
    entry = await load(services=["mobile_app_a"])
    notifier = entry.runtime_data.notifier
    registry = ir.async_get(hass)
    issue = ("energy_compass", f"notify_send_failed_{entry.entry_id}")

    await fail_three_times(hass, entry, freezer)
    assert notifier.state.failures == 3
    raised = registry.async_get_issue(*issue)
    assert raised is not None
    assert raised.translation_placeholders["targets"] == "notify.mobile_app_a"

    phones.register("mobile_app_a")
    start = NOW + timedelta(hours=12)
    freezer.move_to(start)
    never_quiet = {"notify_daily_max": 9, "quiet_start": "00:00", "quiet_end": "00:00"}
    publish(entry, eligible(start, hours=3, prefs=never_quiet))
    await settle(hass)
    assert notifier.state.failures == 0
    assert registry.async_get_issue(*issue) is None


async def test_failure_does_not_retry_same_window(make, hass, freezer):
    load, phones = make
    entry = await load(services=["mobile_app_a"])
    publish(entry, eligible())
    await settle(hass)
    assert entry.runtime_data.notifier.state.failures == 1
    phones.register("mobile_app_a")

    for _ in range(10):
        await tick(hass, freezer, 60)

    assert phones.calls == []
    assert entry.runtime_data.notifier.state.failures == 1


async def fail_three_times(hass, entry, freezer):
    for index in range(3):
        start = NOW + timedelta(hours=4 * index)
        freezer.move_to(start)
        publish(entry, eligible(start, hours=3))
        await settle(hass)


async def test_failures_survive_restart(make, hass, freezer, hass_storage):
    load, _ = make
    entry = await load(services=["mobile_app_a"])
    await fail_three_times(hass, entry, freezer)
    assert stored(hass_storage, entry)["failures"] == 3

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)

    assert entry.runtime_data.notifier.state.failures == 3
    issue = ("energy_compass", f"notify_send_failed_{entry.entry_id}")
    assert ir.async_get(hass).async_get_issue(*issue) is not None


async def test_targets_change_resets_failures_and_issue(make, hass, freezer):
    load, _ = make
    entry = await load(services=["mobile_app_a"])
    registry = ir.async_get(hass)
    await fail_three_times(hass, entry, freezer)
    issue = ("energy_compass", f"notify_send_failed_{entry.entry_id}")
    assert registry.async_get_issue(*issue) is not None

    settings_of(entry)["notify_services"] = ["mobile_app_b"]
    await tick(hass, freezer, 60)

    assert entry.runtime_data.notifier.state.failures == 0
    assert registry.async_get_issue(*issue) is None


async def test_clearing_targets_stops_immediately(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    settings_of(entry)["notify_services"] = []

    publish(entry, eligible())
    await settle(hass)

    assert phones.calls == []


async def test_sender_switch_waits_for_published_plan(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")

    publish(
        entry, eligible(prefs={"notify_sender": "blueprint", "notify_enabled": True})
    )
    await settle(hass)
    assert phones.calls == []

    publish(entry, eligible())
    await settle(hass)
    assert len(phones.to("mobile_app_a")) == 1


async def test_language_auto_follows_ha(make, hass):
    load, phones = make
    entry = await load(services=["mobile_app_a"])
    phones.register("mobile_app_a")
    settings_of(entry)["notify_language"] = "auto"
    hass.config.language = "pl"

    publish(entry, eligible())
    await settle(hass)

    assert phones.to("mobile_app_a")[0]["title"] == TITLE


async def test_logbook_entry_on_send(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    await async_setup_component(hass, "logbook", {})
    events = async_capture_events(hass, "logbook_entry")

    publish(entry, eligible())
    await settle(hass)

    assert len(events) == 1
    data = events[0].data
    assert data["name"] == "Energy Compass notification"
    assert data["message"] == "favorable 12:00–15:00: sent to 2 of 2 targets"
    assert data["entity_id"].startswith("sensor.")


async def test_no_target_calls_no_service(make, hass, hass_storage):
    load, phones = make
    entry = await load(services=[])
    phones.register("mobile_app_a", "mobile_app_b")

    publish(entry, eligible())
    await settle(hass)

    assert phones.calls == []
    assert entry.runtime_data.notifier.state.count == 0
    assert key_of(entry) not in hass_storage


async def test_send_logs_no_warning(make, hass, caplog):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")

    with caplog.at_level(logging.WARNING):
        publish(entry, eligible())
        await settle(hass)

    assert len(phones.calls) == 2
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_single_flight_drops_overlapping_evaluation(make, hass):
    load, phones = make
    entry = await load(services=["mobile_app_a"])
    phones.register("mobile_app_a")
    phones.gate = asyncio.Event()

    publish(entry, eligible())
    for _ in range(5):
        await asyncio.sleep(0)
    publish(entry, eligible(generated=NOW + timedelta(seconds=1)))
    for _ in range(5):
        await asyncio.sleep(0)
    phones.gate.set()
    await settle(hass)

    assert len(phones.to("mobile_app_a")) == 1
    assert entry.runtime_data.notifier.state.count == 1


async def test_unload_stops_tick(make, hass, freezer):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    start = datetime(2026, 9, 17, 20, tzinfo=UTC)
    freezer.move_to(start)
    publish(entry, eligible(start, hours=12))
    await settle(hass)
    notifier = entry.runtime_data.notifier

    assert await hass.config_entries.async_unload(entry.entry_id)
    freezer.move_to(datetime(2026, 9, 18, 6, tzinfo=UTC))
    async_fire_time_changed(hass)
    await settle(hass)

    assert phones.calls == []
    assert notifier._unsub_tick is None


async def test_diagnostics_include_notification_state(make, hass):
    load, phones = make
    entry = await load(services=["mobile_app_a"], entities=["notify.kitchen"])
    phones.register("mobile_app_a", "send_message")
    publish(entry, eligible())
    await settle(hass)

    report = await async_get_config_entry_diagnostics(hass, entry)

    block = report["notifications"]
    assert block["state"]["count"] == 1
    assert block["state"]["favorable"]["s"] == int(NOW.timestamp())
    assert block["last_decision"]["code"] == "send"
    assert block["targets"] == ["notify.mobile_app_a", "notify.kitchen"]
