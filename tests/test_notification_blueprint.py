"""Exercise the window notification blueprint through native HA automation scripts."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import automation
from homeassistant.components.blueprint import models
from homeassistant.core import callback
from homeassistant.setup import async_setup_component
from homeassistant.util import yaml as yaml_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

BLUEPRINT = (
    Path(__file__).resolve().parents[1]
    / "blueprints/automation/energy_compass/notifications.yaml"
)
PATH = "energy_compass/notifications.yaml"
ENTITIES = {
    "plan": "sensor.test_plan",
    "compass": "sensor.test_consumption_compass",
    "forecast_valid": "binary_sensor.test_forecast_valid",
    "next_boost": "sensor.test_next_boost_start",
    "next_cheap": "sensor.test_next_cheap_start",
    "next_limit": "sensor.test_next_limit_start",
    "last_favorable": "input_text.test_last_favorable_window",
    "last_limit": "input_text.test_last_limit_window",
    "daily_count": "input_number.test_daily_count",
    "daily_date": "input_datetime.test_daily_date",
    "last_sent": "input_datetime.test_last_sent",
}
DEFAULT_PREFERENCES = {
    "notify_enabled": True,
    "notify_events": ["favorable", "limit"],
    "notify_minimum_hours": 2,
    "notify_limit_lead_minutes": 30,
    "notify_daily_max": 3,
    "notify_cooldown_minutes": 60,
    "quiet_start": "22:00:00",
    "quiet_end": "08:00:00",
    "actions_configured": True,
}


@pytest.fixture(autouse=True)
async def stop_automation(hass):
    yield
    if hass.states.get("automation.test_compass") is not None:
        await hass.services.async_call(
            "automation",
            "turn_off",
            {"entity_id": "automation.test_compass"},
            blocking=True,
        )
        await hass.async_block_till_done()


@contextmanager
def loaded_blueprint():
    original = models.DomainBlueprints._load_blueprint

    @callback
    def load(self, path):
        if path != PATH:
            return original(self, path)
        return models.Blueprint(
            yaml_util.load_yaml(BLUEPRINT),
            expected_domain=self.domain,
            path=path,
            schema=automation.config.AUTOMATION_BLUEPRINT_SCHEMA,
        )

    with patch.object(models.DomainBlueprints, "_load_blueprint", load):
        yield


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


def update_plan(hass, changes):
    state = hass.states.get(ENTITIES["plan"])
    attributes = dict(state.attributes)
    attributes.update(changes)
    hass.states.async_set(ENTITIES["plan"], state.state, attributes)


async def setup_blueprint(
    hass,
    now,
    *,
    favorable=(),
    limit=(),
    outlook=(),
    valid=True,
    alert=False,
    optimizer="ready",
    preferences=None,
    inputs=None,
):
    await hass.config.async_set_time_zone("Europe/Warsaw")
    assert await async_setup_component(
        hass,
        "input_text",
        {
            "input_text": {
                "test_last_favorable_window": {"max": 255},
                "test_last_limit_window": {"max": 255},
            }
        },
    )
    assert await async_setup_component(
        hass,
        "input_number",
        {"input_number": {"test_daily_count": {"min": 0, "max": 100, "step": 1}}},
    )
    assert await async_setup_component(
        hass,
        "input_datetime",
        {
            "input_datetime": {
                "test_daily_date": {"has_date": True, "has_time": False},
                "test_last_sent": {"has_date": True, "has_time": True},
            }
        },
    )
    hass.states.async_set(ENTITIES["forecast_valid"], "on" if valid else "off")
    hass.states.async_set("binary_sensor.test_alert", "on" if alert else "off")
    hass.states.async_set("sensor.test_optimizer_status", optimizer)
    hass.states.async_set(ENTITIES["compass"], "NORMAL")
    plan_attributes = {
        "valid_until": (now + timedelta(hours=12)).isoformat(),
        "favorable_windows": list(favorable),
        "windows": {"limit": list(limit)},
        "outlook": list(outlook),
        "notification_preferences": {
            **DEFAULT_PREFERENCES,
            **(preferences or {}),
        },
    }
    hass.states.async_set(ENTITIES["plan"], now.isoformat(), plan_attributes)
    for key in ("next_boost", "next_cheap", "next_limit"):
        hass.states.async_set(ENTITIES[key], "unavailable")
    received = []
    hass.bus.async_listen("energy_compass_test_notification", received.append)
    selections = {
        **ENTITIES,
        "alert": "binary_sensor.test_alert",
        "optimizer_status": "sensor.test_optimizer_status",
        "language": "pl",
        "notification_actions": [
            {
                "event": "energy_compass_test_notification",
                "event_data": {
                    "kind": "{{ event_kind }}",
                    "title": "{{ notification_title }}",
                    "message": "{{ notification_message }}",
                },
            }
        ],
        **(inputs or {}),
    }
    with loaded_blueprint():
        assert await async_setup_component(
            hass,
            "automation",
            {
                "automation": {
                    "alias": "Test compass",
                    "use_blueprint": {"path": PATH, "input": selections},
                }
            },
        )
    await hass.async_block_till_done()
    return received


async def trigger(hass):
    await hass.services.async_call(
        "automation",
        "trigger",
        {"entity_id": "automation.test_compass"},
        blocking=True,
    )
    await hass.async_block_till_done()


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_favorable_message_uses_polish_copy_and_local_current_times(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    end = now + timedelta(hours=3)
    received = await setup_blueprint(
        hass,
        now,
        favorable=[window(now, end, ("CHEAP",))],
        outlook=[row(now, end, "CHEAP")],
    )

    await trigger(hass)

    assert len(received) == 1
    assert received[0].data["title"] == "Dobry moment na domowe urządzenia"
    assert "12:00–15:00" in received[0].data["message"]
    assert "trwa teraz" in received[0].data["message"]
    assert all(
        suggestion in received[0].data["message"]
        for suggestion in ("pranie", "zmywarkę", "ładowanie auta")
    )
    assert "darm" not in received[0].data["message"].lower()


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
@pytest.mark.parametrize(
    ("outlook", "expected_title"),
    [
        (("BOOST", "BOOST"), "Wykorzystaj tanią energię"),
        (("CHEAP", "BOOST"), "Dobry moment na domowe urządzenia"),
    ],
)
async def test_boost_copy_requires_whole_remaining_window_boost(
    hass, outlook, expected_title
):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    middle = now + timedelta(hours=1)
    end = now + timedelta(hours=3)
    received = await setup_blueprint(
        hass,
        now,
        favorable=[window(now - timedelta(hours=1), end, ("CHEAP", "BOOST"))],
        outlook=[row(now, middle, outlook[0]), row(middle, end, outlook[1])],
    )

    await trigger(hass)

    assert received[0].data["title"] == expected_title


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_future_limit_message_uses_local_times(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    start = now + timedelta(minutes=30)
    end = now + timedelta(hours=2)
    received = await setup_blueprint(
        hass,
        now,
        limit=[window(start, end, ("LIMIT",))],
        outlook=[row(start, end, "LIMIT", 1.1)],
    )

    await trigger(hass)

    assert received[0].data["title"] == "Zaplanuj większe zużycie na później"
    assert "12:30–14:00" in received[0].data["message"]
    assert "Według prognozy droższy okres" in received[0].data["message"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_ongoing_limit_says_energy_is_already_expensive(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    start = now - timedelta(minutes=30)
    end = now + timedelta(hours=1)
    received = await setup_blueprint(
        hass,
        now,
        limit=[window(start, end, ("LIMIT",))],
        outlook=[row(now, end, "LIMIT", 1.1)],
    )

    await trigger(hass)

    message = received[0].data["message"]
    assert "Według prognozy dodatkowe zużycie jest już droższe" in message
    assert "11:30–13:00" in message
    assert "za 30" not in message


@pytest.mark.freeze_time("2026-09-17T21:30:00+00:00")
async def test_overnight_window_includes_both_local_dates(hass):
    now = datetime(2026, 9, 17, 21, 30, tzinfo=UTC)
    end = now + timedelta(hours=2)
    received = await setup_blueprint(
        hass,
        now,
        favorable=[window(now, end, ("CHEAP",))],
        outlook=[row(now, end, "CHEAP")],
        inputs={
            "use_blueprint_overrides": True,
            "quiet_start": "00:00:00",
            "quiet_end": "00:00:00",
        },
    )

    await trigger(hass)

    assert "17.09 23:30–18.09 01:30" in received[0].data["message"]


@pytest.mark.freeze_time("2026-09-17T20:00:00+00:00")
async def test_quiet_hours_suppress_until_local_end(hass, freezer):
    now = datetime(2026, 9, 17, 20, tzinfo=UTC)
    end = now + timedelta(hours=14)
    received = await setup_blueprint(
        hass,
        now,
        favorable=[window(now, end, ("CHEAP",))],
        outlook=[row(now, end, "CHEAP")],
    )

    await trigger(hass)
    assert received == []

    boundary = now + timedelta(hours=10)
    freezer.move_to(boundary)
    async_fire_time_changed(hass, boundary)
    await hass.async_block_till_done()
    assert len(received) == 1


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
@pytest.mark.parametrize(("remaining", "expected"), [(120, 1), (119, 0)])
async def test_favorable_minimum_applies_to_remaining_time(hass, remaining, expected):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    start = now - timedelta(hours=2)
    end = now + timedelta(minutes=remaining)
    received = await setup_blueprint(
        hass,
        now,
        favorable=[window(start, end, ("CHEAP",))],
        outlook=[row(now, end, "CHEAP")],
    )

    await trigger(hass)

    assert len(received) == expected


@pytest.mark.freeze_time("2026-09-17T08:00:00+00:00")
async def test_daily_cap_resets_on_next_local_date(hass, freezer):
    now = datetime(2026, 9, 17, 8, tzinfo=UTC)
    received = await setup_blueprint(
        hass,
        now,
        inputs={
            "use_blueprint_overrides": True,
            "daily_cap": 1,
            "cooldown_minutes": 60,
            "quiet_start": "00:00:00",
            "quiet_end": "00:00:00",
        },
        favorable=[window(now, now + timedelta(hours=2), ("CHEAP",))],
        outlook=[row(now, now + timedelta(hours=2), "CHEAP")],
    )
    await trigger(hass)
    assert len(received) == 1

    second_start = now + timedelta(hours=3)
    update_plan(
        hass,
        {
            "favorable_windows": [
                window(second_start, second_start + timedelta(hours=2), ("CHEAP",))
            ],
            "outlook": [row(second_start, second_start + timedelta(hours=2), "CHEAP")],
        },
    )
    freezer.move_to(second_start)
    await trigger(hass)
    assert len(received) == 1

    next_day = now + timedelta(days=1)
    update_plan(
        hass,
        {
            "valid_until": (next_day + timedelta(hours=12)).isoformat(),
            "favorable_windows": [
                window(next_day, next_day + timedelta(hours=2), ("CHEAP",))
            ],
            "outlook": [row(next_day, next_day + timedelta(hours=2), "CHEAP")],
        },
    )
    freezer.move_to(next_day)
    await trigger(hass)
    assert len(received) == 2
    assert hass.states.get(ENTITIES["daily_count"]).state == "1.0"


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_cooldown_blocks_independent_window_until_boundary(hass, freezer):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    received = await setup_blueprint(
        hass,
        now,
        inputs={"use_blueprint_overrides": True, "cooldown_minutes": 60},
        favorable=[window(now, now + timedelta(hours=2), ("CHEAP",))],
        outlook=[row(now, now + timedelta(hours=2), "CHEAP")],
    )
    await trigger(hass)

    later = now + timedelta(minutes=30)
    update_plan(
        hass,
        {
            "favorable_windows": [],
            "windows": {
                "limit": [window(later, later + timedelta(hours=3), ("LIMIT",))]
            },
            "outlook": [row(later, later + timedelta(hours=3), "LIMIT")],
        },
    )
    freezer.move_to(later)
    await trigger(hass)
    assert len(received) == 1

    boundary = now + timedelta(hours=1)
    freezer.move_to(boundary)
    await trigger(hass)
    assert len(received) == 2


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_recalculation_and_boost_cheap_transition_keep_one_identity(
    hass, freezer
):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    received = await setup_blueprint(
        hass,
        now,
        inputs={"use_blueprint_overrides": True, "cooldown_minutes": 60},
        favorable=[window(now, now + timedelta(hours=4), ("BOOST",))],
        outlook=[row(now, now + timedelta(hours=4), "BOOST")],
    )
    await trigger(hass)
    stored = hass.states.get(ENTITIES["last_favorable"]).state
    stored_count = hass.states.get(ENTITIES["daily_count"]).state
    stored_last_sent = hass.states.get(ENTITIES["last_sent"]).state
    assert stored.startswith('{"s":') and '"e":' in stored

    shifted = now + timedelta(hours=1)
    update_plan(
        hass,
        {
            "favorable_windows": [
                window(shifted, now + timedelta(hours=4, minutes=15), ("CHEAP",))
            ],
            "outlook": [row(shifted, now + timedelta(hours=4, minutes=15), "CHEAP")],
        },
    )
    freezer.move_to(shifted)
    await trigger(hass)
    assert len(received) == 1
    merged = json.loads(hass.states.get(ENTITIES["last_favorable"]).state)
    assert merged["s"] == json.loads(stored)["s"]
    assert merged["e"] == int((now + timedelta(hours=4, minutes=15)).timestamp())
    assert hass.states.get(ENTITIES["daily_count"]).state == stored_count
    assert hass.states.get(ENTITIES["last_sent"]).state == stored_last_sent


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_overlapping_recalculations_extend_persistent_window_identity(
    hass, freezer
):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    received = await setup_blueprint(
        hass,
        now,
        inputs={"use_blueprint_overrides": True, "cooldown_minutes": 60},
        favorable=[window(now, now + timedelta(hours=2), ("CHEAP",))],
        outlook=[row(now, now + timedelta(hours=2), "CHEAP")],
    )
    await trigger(hass)

    one_hour = now + timedelta(hours=1)
    update_plan(
        hass,
        {
            "favorable_windows": [
                window(one_hour, now + timedelta(hours=4), ("BOOST",))
            ],
            "outlook": [row(one_hour, now + timedelta(hours=4), "BOOST")],
        },
    )
    freezer.move_to(one_hour)
    await trigger(hass)
    assert len(received) == 1
    metadata = json.loads(hass.states.get(ENTITIES["last_favorable"]).state)
    assert metadata["e"] == int((now + timedelta(hours=4)).timestamp())

    old_endpoint = now + timedelta(hours=2)
    update_plan(
        hass,
        {
            "favorable_windows": [
                window(old_endpoint, now + timedelta(hours=5), ("CHEAP",))
            ],
            "outlook": [row(old_endpoint, now + timedelta(hours=5), "CHEAP")],
        },
    )
    freezer.move_to(old_endpoint)
    await trigger(hass)
    assert len(received) == 1


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_safety_is_rechecked_after_helper_writes_before_dispatch(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    received = await setup_blueprint(
        hass,
        now,
        favorable=[window(now, now + timedelta(hours=2), ("CHEAP",))],
        outlook=[row(now, now + timedelta(hours=2), "CHEAP")],
    )

    @callback
    def raise_alert(event):
        if event.data.get("entity_id") == ENTITIES["last_sent"]:
            hass.states.async_set("binary_sensor.test_alert", "on")

    remove = hass.bus.async_listen("state_changed", raise_alert)
    await trigger(hass)
    remove()

    assert received == []


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_limit_selection_skips_ended_row_for_later_eligible_window(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    ended = window(now - timedelta(hours=1), now - timedelta(minutes=5), ("LIMIT",))
    upcoming = window(now + timedelta(minutes=20), now + timedelta(hours=1), ("LIMIT",))
    received = await setup_blueprint(hass, now, limit=[ended, upcoming])

    await trigger(hass)

    assert [event.data["kind"] for event in received] == ["limit"]


@pytest.mark.freeze_time("2026-09-17T09:30:00+00:00")
async def test_notified_active_limit_does_not_hide_independent_approaching_window(
    hass, freezer
):
    now = datetime(2026, 9, 17, 9, 30, tzinfo=UTC)
    first = window(
        now + timedelta(minutes=30), now + timedelta(hours=1, minutes=15), ("LIMIT",)
    )
    second = window(
        now + timedelta(hours=1, minutes=30),
        now + timedelta(hours=2, minutes=30),
        ("LIMIT",),
    )
    received = await setup_blueprint(hass, now, limit=[first, second])
    await trigger(hass)

    freezer.move_to(now + timedelta(hours=1))
    await trigger(hass)

    assert [event.data["kind"] for event in received] == ["limit", "limit"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_overlapping_limit_recalculations_extend_persistent_identity(
    hass, freezer
):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    first_start = now + timedelta(minutes=20)
    first_end = now + timedelta(hours=1)
    received = await setup_blueprint(
        hass,
        now,
        limit=[window(first_start, first_end, ("LIMIT",))],
    )
    await trigger(hass)

    shifted = now + timedelta(minutes=30)
    extended_end = now + timedelta(hours=2)
    update_plan(
        hass,
        {"windows": {"limit": [window(shifted, extended_end, ("LIMIT",))]}},
    )
    freezer.move_to(shifted)
    await trigger(hass)
    metadata = json.loads(hass.states.get(ENTITIES["last_limit"]).state)
    assert metadata["e"] == int(extended_end.timestamp())

    update_plan(
        hass,
        {
            "windows": {
                "limit": [
                    window(first_end, now + timedelta(hours=2, minutes=30), ("LIMIT",))
                ]
            }
        },
    )
    freezer.move_to(first_end)
    await trigger(hass)
    assert [event.data["kind"] for event in received] == ["limit"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_non_overlapping_later_window_is_independent(hass, freezer):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    received = await setup_blueprint(
        hass,
        now,
        inputs={"use_blueprint_overrides": True, "cooldown_minutes": 60},
        favorable=[window(now, now + timedelta(hours=2), ("CHEAP",))],
        outlook=[row(now, now + timedelta(hours=2), "CHEAP")],
    )
    await trigger(hass)

    later = now + timedelta(hours=3)
    update_plan(
        hass,
        {
            "favorable_windows": [
                window(later, later + timedelta(hours=2), ("BOOST",))
            ],
            "outlook": [row(later, later + timedelta(hours=2), "BOOST")],
        },
    )
    freezer.move_to(later)
    await trigger(hass)
    assert [event.data["kind"] for event in received] == ["favorable", "favorable"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_limit_wins_when_both_groups_qualify(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    received = await setup_blueprint(
        hass,
        now,
        favorable=[window(now, now + timedelta(hours=2), ("CHEAP",))],
        limit=[window(now, now + timedelta(hours=1), ("LIMIT",))],
        outlook=[row(now, now + timedelta(hours=2), "CHEAP")],
    )

    await trigger(hass)

    assert [event.data["kind"] for event in received] == ["limit"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
@pytest.mark.parametrize("failure", ["forecast", "alert", "optimizer", "expired"])
async def test_runtime_failures_stop_before_bookkeeping(hass, failure):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    received = await setup_blueprint(
        hass,
        now,
        valid=failure != "forecast",
        alert=failure == "alert",
        optimizer="failed" if failure == "optimizer" else "ready",
        favorable=[window(now, now + timedelta(hours=3), ("CHEAP",))],
        outlook=[row(now, now + timedelta(hours=3), "CHEAP")],
    )
    if failure == "expired":
        update_plan(hass, {"valid_until": (now - timedelta(seconds=1)).isoformat()})

    await trigger(hass)

    assert received == []
    assert hass.states.get(ENTITIES["daily_count"]).state == "0.0"
    assert hass.states.get(ENTITIES["last_favorable"]).state in ("unknown", "")


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
@pytest.mark.parametrize("failure", ["empty", "unknown_plan", "unknown_helper"])
async def test_empty_and_unknown_inputs_fail_closed(hass, failure):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    received = await setup_blueprint(
        hass,
        now,
        favorable=[]
        if failure == "empty"
        else [window(now, now + timedelta(hours=3), ("CHEAP",))],
        outlook=[]
        if failure == "empty"
        else [row(now, now + timedelta(hours=3), "CHEAP")],
    )
    if failure == "unknown_plan":
        hass.states.async_set(ENTITIES["plan"], "unknown")
    elif failure == "unknown_helper":
        hass.states.async_set(ENTITIES["daily_count"], "unknown")
        hass.states.async_set(ENTITIES["daily_date"], "2026-09-16")

    await trigger(hass)

    assert received == []


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_persistent_helper_identity_survives_automation_reload(hass, freezer):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    favorable = [window(now, now + timedelta(hours=3), ("CHEAP",))]
    outlook = [row(now, now + timedelta(hours=3), "CHEAP")]
    received = await setup_blueprint(
        hass,
        now,
        inputs={"use_blueprint_overrides": True, "cooldown_minutes": 60},
        favorable=favorable,
        outlook=outlook,
    )
    await trigger(hass)
    stored = hass.states.get(ENTITIES["last_favorable"]).state

    config = {
        "automation": {
            "alias": "Test compass",
            "use_blueprint": {
                "path": PATH,
                "input": {
                    **ENTITIES,
                    "alert": "binary_sensor.test_alert",
                    "optimizer_status": "sensor.test_optimizer_status",
                    "use_blueprint_overrides": True,
                    "cooldown_minutes": 60,
                    "notification_actions": [
                        {
                            "event": "energy_compass_test_notification",
                            "event_data": {"kind": "{{ event_kind }}"},
                        }
                    ],
                },
            },
        }
    }
    with (
        loaded_blueprint(),
        patch(
            "homeassistant.config.async_hass_config_yaml",
            new=AsyncMock(return_value=config),
        ),
    ):
        await hass.services.async_call("automation", "reload", blocking=True)
    await hass.async_block_till_done()
    freezer.move_to(now + timedelta(hours=1))
    await trigger(hass)

    assert hass.states.get(ENTITIES["last_favorable"]).state == stored
    assert len(received) == 1


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_native_test_action_never_invokes_phone_service(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    received = await setup_blueprint(
        hass,
        now,
        favorable=[window(now, now + timedelta(hours=2), ("CHEAP",))],
        outlook=[row(now, now + timedelta(hours=2), "CHEAP")],
    )

    await trigger(hass)

    assert len(received) == 1
    assert not hass.services.has_service("notify", "mobile_app_marcins_iphone")


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_run_logs_no_undefined_template_variable_warnings(hass, caplog):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    received = await setup_blueprint(
        hass,
        now,
        favorable=[window(now, now + timedelta(hours=3), ("CHEAP",))],
        outlook=[row(now, now + timedelta(hours=3), "CHEAP")],
    )
    caplog.clear()

    await trigger(hass)

    assert len(received) == 1
    assert received[0].data["title"] == "Dobry moment na domowe urządzenia"
    assert "Template variable warning" not in caplog.text
    assert "is undefined" not in caplog.text


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_attribute_only_updates_do_not_start_runs(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    await setup_blueprint(hass, now)
    runs = []
    hass.bus.async_listen("automation_triggered", runs.append)

    update_plan(hass, {"valid_until": (now + timedelta(hours=13)).isoformat()})
    hass.states.async_set(ENTITIES["forecast_valid"], "on", {"checked": 1})
    hass.states.async_set("sensor.test_optimizer_status", "ready", {"progress": 1})
    hass.states.async_set(ENTITIES["next_cheap"], "unavailable", {"detail": 1})
    await hass.async_block_till_done()

    assert runs == []

    state = hass.states.get(ENTITIES["plan"])
    hass.states.async_set(
        ENTITIES["plan"], (now + timedelta(seconds=1)).isoformat(), state.attributes
    )
    await hass.async_block_till_done()

    assert len(runs) == 1


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_no_configured_actions_skips_bookkeeping(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    await setup_blueprint(
        hass,
        now,
        favorable=[window(now, now + timedelta(hours=3), ("CHEAP",))],
        outlook=[row(now, now + timedelta(hours=3), "CHEAP")],
        inputs={"notification_actions": []},
    )

    await trigger(hass)

    assert hass.states.get(ENTITIES["daily_count"]).state == "0.0"
    assert hass.states.get(ENTITIES["last_favorable"]).state in ("unknown", "")


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_english_copy(hass):
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    start = now + timedelta(minutes=30)
    end = now + timedelta(hours=2)
    received = await setup_blueprint(
        hass,
        now,
        limit=[window(start, end, ("LIMIT",))],
        outlook=[row(start, end, "LIMIT", 1.1)],
        inputs={"language": "en"},
    )

    await trigger(hass)

    assert received[0].data["title"] == "Plan heavy use for later"
    assert "the more expensive period is 12:30–14:00" in received[0].data["message"]


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_integration_preferences_are_not_clamped(hass):
    """A LIMIT lead beyond 30 minutes and a shorter favorable window are honoured."""
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    start = now + timedelta(minutes=50)
    end = now + timedelta(hours=2)
    received = await setup_blueprint(
        hass,
        now,
        limit=[window(start, end, ("LIMIT",))],
        outlook=[row(start, end, "LIMIT", 1.1)],
        preferences={"notify_limit_lead_minutes": 60},
    )

    await trigger(hass)

    assert received and received[0].data["kind"] == "limit"


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
@pytest.mark.parametrize("overrides", [False, True])
async def test_blueprint_suppressed_when_native_sending_is_active(hass, overrides):
    """DD-2: the plan publishes `notify_enabled` false once the integration sends."""
    from custom_components.energy_compass.runtime import compute
    from custom_components.energy_compass.settings import default_configuration

    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        notify_enabled=True, notify_services=["mobile_app_pixel_8"]
    )
    published = compute(config, {}, now)["notification_preferences"]
    assert published["notify_sender"] == "integration"
    received = await setup_blueprint(
        hass,
        now,
        favorable=[window(now, now + timedelta(hours=3), ("CHEAP",))],
        outlook=[row(now, now + timedelta(hours=3), "CHEAP")],
        preferences=published,
        inputs={"use_blueprint_overrides": True} if overrides else None,
    )
    before = hass.states.get(ENTITIES["last_favorable"]).state

    await trigger(hass)

    assert received == []
    assert hass.states.get(ENTITIES["daily_count"]).state == "0.0"
    assert hass.states.get(ENTITIES["last_favorable"]).state == before
