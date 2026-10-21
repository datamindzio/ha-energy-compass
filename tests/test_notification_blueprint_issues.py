"""Repair issues for the deprecated notification blueprint, and its deprecation text."""

import copy
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import test_notification_blueprint as bp
import test_notifier as base
from homeassistant.components import automation
from homeassistant.components.blueprint import models
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import yaml as yaml_util

make = base.make


@pytest.fixture(autouse=True)
async def stop_automation(make, hass):
    """Like the blueprint tests', but after `make`, which must set `hass` up first."""
    yield
    if hass.states.get("automation.test_compass") is not None:
        await hass.services.async_call(
            "automation",
            "turn_off",
            {"entity_id": "automation.test_compass"},
            blocking=True,
        )
        await hass.async_block_till_done()


SOURCE = (
    "https://github.com/datamindzio/ha-energy-compass/blob/main/"
    "blueprints/automation/energy_compass/notifications.yaml"
)
INPUTS = {
    key: "sensor.unused"
    for key in (
        "compass",
        "plan",
        "forecast_valid",
        "alert",
        "optimizer_status",
        "next_boost",
        "next_cheap",
        "next_limit",
        "last_favorable",
        "last_limit",
        "daily_count",
        "daily_date",
        "last_sent",
    )
}


@contextmanager
def blueprint_at(path, source_url=SOURCE):
    """Serve the shipped blueprint under `path`, with the given `source_url`."""
    original = models.DomainBlueprints._load_blueprint

    @callback
    def load(self, requested):
        if requested != path:
            return original(self, requested)
        data = yaml_util.load_yaml(bp.BLUEPRINT)
        data = copy.deepcopy(data)
        data["blueprint"]["source_url"] = source_url
        return models.Blueprint(
            data,
            expected_domain=self.domain,
            path=requested,
            schema=automation.config.AUTOMATION_BLUEPRINT_SCHEMA,
        )

    with patch.object(models.DomainBlueprints, "_load_blueprint", load):
        yield


async def add_automation(hass, path):
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "alias": "Test compass",
                "use_blueprint": {"path": path, "input": INPUTS},
            }
        },
    )
    await hass.async_block_till_done()
    assert hass.states.get("automation.test_compass").state == "on"


def issue(hass, entry, kind):
    return ir.async_get(hass).async_get_issue(
        "energy_compass", f"notify_blueprint_{kind}_{entry.entry_id}"
    )


def sender(entry, name):
    base.publish(
        entry,
        base.eligible(
            favorable_windows=[],
            prefs={"notify_sender": name, "notify_enabled": name == "blueprint"},
        ),
    )


async def tick(hass, freezer):
    freezer.tick(timedelta(minutes=1))
    base.async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_blueprint_automation_deprecated_issue(make, hass, freezer):
    load, _ = make
    entry = await load(services=[])
    sender(entry, "blueprint")
    with bp.loaded_blueprint():
        await add_automation(hass, bp.PATH)
        await tick(hass, freezer)

    raised = issue(hass, entry, "deprecated")
    assert raised is not None
    assert raised.translation_key == "notify_blueprint_deprecated"
    assert raised.translation_placeholders == {"automation": "automation.test_compass"}
    assert issue(hass, entry, "suppressed") is None


async def test_blueprint_automation_suppressed_issue(make, hass, freezer):
    load, phones = make
    entry = await load()
    phones.register("mobile_app_a", "mobile_app_b")
    with bp.loaded_blueprint():
        await add_automation(hass, bp.PATH)
        sender(entry, "blueprint")
        await tick(hass, freezer)
        assert issue(hass, entry, "deprecated") is not None

        sender(entry, "integration")
        await tick(hass, freezer)

    raised = issue(hass, entry, "suppressed")
    assert raised is not None
    assert raised.translation_key == "notify_blueprint_suppressed"
    assert raised.translation_placeholders == {"automation": "automation.test_compass"}
    assert issue(hass, entry, "deprecated") is None


async def test_blueprint_issue_detects_imported_path(make, hass, freezer):
    load, _ = make
    entry = await load(services=[])
    sender(entry, "blueprint")
    path = "datamindzio/notifications.yaml"
    with blueprint_at(path):
        await add_automation(hass, path)
        await tick(hass, freezer)

    assert issue(hass, entry, "deprecated") is not None


async def test_blueprint_issue_ignores_foreign_notifications_blueprint(
    make, hass, freezer
):
    load, _ = make
    entry = await load(services=[])
    sender(entry, "blueprint")
    path = "somebody/notifications.yaml"
    with blueprint_at(path, "https://example.com/other/notifications.yaml"):
        await add_automation(hass, path)
        await tick(hass, freezer)

    assert issue(hass, entry, "deprecated") is None
    assert issue(hass, entry, "suppressed") is None


async def test_blueprint_issues_cleared_when_automation_off_or_notify_disabled(
    make, hass, freezer
):
    load, _ = make
    entry = await load(services=[])
    sender(entry, "blueprint")
    with bp.loaded_blueprint():
        await add_automation(hass, bp.PATH)
        await tick(hass, freezer)
        assert issue(hass, entry, "deprecated") is not None

        base.settings_of(entry)["notify_enabled"] = False
        await tick(hass, freezer)
        assert issue(hass, entry, "deprecated") is None

        base.settings_of(entry)["notify_enabled"] = True
        await tick(hass, freezer)
        assert issue(hass, entry, "deprecated") is not None

        await hass.services.async_call(
            "automation",
            "turn_off",
            {"entity_id": "automation.test_compass"},
            blocking=True,
        )
        await tick(hass, freezer)
        assert issue(hass, entry, "deprecated") is None


async def test_no_blueprint_automation_raises_nothing(make, hass, freezer):
    load, _ = make
    entry = await load(services=[])
    sender(entry, "blueprint")
    await async_setup_component(hass, "automation", {})
    await tick(hass, freezer)

    assert issue(hass, entry, "deprecated") is None
    assert issue(hass, entry, "suppressed") is None


def test_blueprint_description_says_deprecated():
    description = yaml_util.load_yaml(bp.BLUEPRINT)["blueprint"]["description"]

    assert description.startswith(
        "Deprecated: Energy Compass now sends these notifications itself "
        "(Options → Notification preferences). "
        "This blueprint will be removed in a future release."
    )


@pytest.mark.freeze_time("2026-09-17T10:00:00+00:00")
async def test_blueprint_still_sends_while_it_is_the_sender(hass):
    """DD-2, the other side: notify on and no target keeps the blueprint working."""
    from custom_components.energy_compass.runtime import compute
    from custom_components.energy_compass.settings import default_configuration

    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    config = default_configuration("EUR", "UTC")
    config["settings"].update(notify_enabled=True)
    published = compute(config, {}, now)["notification_preferences"]
    assert published["notify_sender"] == "blueprint"
    assert published["notify_enabled"] is True
    received = await bp.setup_blueprint(
        hass,
        now,
        favorable=[bp.window(now, now + timedelta(hours=3), ("CHEAP",))],
        outlook=[bp.row(now, now + timedelta(hours=3), "CHEAP")],
        preferences=published,
    )

    await bp.trigger(hass)

    assert len(received) == 1
