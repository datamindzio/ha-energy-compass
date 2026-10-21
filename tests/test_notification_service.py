"""`energy_compass.send_test_notification`: one fixed message, whatever the sender state."""

from datetime import timedelta

import pytest
import test_notifier as base
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir

make = base.make
NOW = base.NOW

TEST_PL = (
    "Test Energy Compass",
    "Powiadomienia z Energy Compass docierają na to urządzenie.",
)
TEST_EN = (
    "Energy Compass test",
    "Notifications from Energy Compass reach this device.",
)


async def send_test(hass, **data):
    return await hass.services.async_call(
        "energy_compass",
        "send_test_notification",
        data,
        blocking=True,
        return_response=True,
    )


def message(title_message):
    title, text = title_message
    return {"title": title, "message": text}


async def test_send_test_notification_ignores_gates_quiet_and_state(
    make, hass, freezer, hass_storage
):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    base.publish(entry, base.eligible(prefs={"notify_daily_max": 1}))
    await base.settle(hass)
    assert len(phones.to("mobile_app_a")) == 1
    before = base.stored(hass_storage, entry)
    state = entry.runtime_data.notifier.state
    # Quiet hours (00:30 local), an Alert on, and the daily cap already reached.
    freezer.move_to(NOW + timedelta(hours=12, minutes=30))
    base.publish(entry, base.eligible(alert={"code": "stale"}))
    await base.settle(hass)

    response = await send_test(hass)

    assert phones.to("mobile_app_a")[-1] == message(TEST_PL)
    assert phones.to("mobile_app_b")[-1] == message(TEST_PL)
    assert [result["ok"] for result in response["results"]] == [True, True]
    assert base.stored(hass_storage, entry) == before
    assert entry.runtime_data.notifier.state == state


async def test_send_test_notification_works_with_notify_disabled(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    base.settings_of(entry)["notify_enabled"] = False

    response = await send_test(hass)

    assert phones.to("mobile_app_a") == [message(TEST_PL)]
    assert phones.to("mobile_app_b") == [message(TEST_PL)]
    assert len(response["results"]) == 2


async def test_send_test_notification_no_targets_raises(make, hass):
    load, phones = make
    await load(services=[])
    phones.register("mobile_app_a")

    with pytest.raises(ServiceValidationError) as raised:
        await send_test(hass)

    assert raised.value.translation_key == "notify_no_target"
    assert phones.calls == []


async def test_send_test_notification_returns_per_target_result(make, hass):
    load, phones = make
    await load(entities=["notify.kitchen"])
    phones.register("mobile_app_a")
    hass.services.async_register("notify", "send_message", phones_entity(phones))

    response = await send_test(hass)

    assert response == {
        "results": [
            {"target": "notify.mobile_app_a", "ok": True},
            {
                "target": "notify.mobile_app_b",
                "ok": False,
                "error": "Service not found",
            },
            {"target": "notify.kitchen", "ok": True},
        ]
    }
    assert phones.to("send_message") == [
        {**message(TEST_PL), "entity_id": "notify.kitchen"}
    ]


def phones_entity(phones):
    async def handler(call):
        phones.calls.append(("send_message", dict(call.data)))

    return handler


async def test_send_test_notification_requires_entry_when_several(make, hass):
    load, phones = make
    first = await load(services=["mobile_app_a"], entry_id="first")
    second = await load(services=["mobile_app_b"], entry_id="second")
    phones.register("mobile_app_a", "mobile_app_b")

    with pytest.raises(ServiceValidationError) as raised:
        await send_test(hass)
    assert raised.value.translation_key == "notify_entry_required"
    assert phones.calls == []

    response = await send_test(hass, config_entry_id=second.entry_id)

    assert response["results"] == [{"target": "notify.mobile_app_b", "ok": True}]
    assert phones.to("mobile_app_a") == []
    assert phones.to("mobile_app_b") == [message(TEST_PL)]
    assert first.entry_id == "first"


async def test_send_test_notification_unknown_entry_raises(make, hass):
    load, _ = make
    await load()

    with pytest.raises(ServiceValidationError) as raised:
        await send_test(hass, config_entry_id="missing")

    assert raised.value.translation_key == "notify_entry_not_found"


async def test_send_test_notification_success_clears_failure_issue(
    make, hass, freezer, hass_storage
):
    load, phones = make
    entry = await load(services=["mobile_app_a"])
    await base.fail_three_times(hass, entry, freezer)
    issue = ("energy_compass", f"notify_send_failed_{entry.entry_id}")
    registry = ir.async_get(hass)
    assert registry.async_get_issue(*issue) is not None

    failed = await send_test(hass)
    assert failed["results"][0]["ok"] is False
    assert registry.async_get_issue(*issue) is not None
    assert entry.runtime_data.notifier.state.failures == 3

    phones.register("mobile_app_a")
    ok = await send_test(hass)

    assert ok["results"] == [{"target": "notify.mobile_app_a", "ok": True}]
    assert registry.async_get_issue(*issue) is None
    assert entry.runtime_data.notifier.state.failures == 0
    assert base.stored(hass_storage, entry)["failures"] == 0


async def test_send_test_notification_language(make, hass):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    base.settings_of(entry)["notify_language"] = "en"

    await send_test(hass)

    assert phones.to("mobile_app_a") == [message(TEST_EN)]
    assert phones.to("mobile_app_b") == [message(TEST_EN)]
