"""Notification targets, language and the sender the plan publishes (DD-1 to DD-4)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.runtime import compute
from custom_components.energy_compass.settings import (
    default_configuration,
    native_notifications_active,
    validate_configuration,
)

NOW = datetime(2026, 9, 17, tzinfo=UTC)
COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/energy_compass"


def _config(**settings):
    config = default_configuration("EUR", "UTC")
    config["settings"].update(settings)
    return config


def _validated(**settings):
    return validate_configuration(_config(**settings), {}, NOW, sources=False)


def test_new_keys_default_empty_and_auto():
    values = _validated()

    assert values["notify_services"] == []
    assert values["notify_entities"] == []
    assert values["notify_language"] == "auto"
    assert values["notify_enabled"] is False


def test_entry_without_new_keys_loads_and_publishes_off():
    """A 0.1.44 configuration carries none of the new keys."""
    config = default_configuration("EUR", "UTC")
    for key in ("notify_services", "notify_entities", "notify_language"):
        config["settings"].pop(key)

    preferences = compute(config, {}, NOW)["notification_preferences"]

    assert preferences["notify_sender"] == "off"
    assert preferences["notify_enabled"] is False
    assert preferences["actions_configured"] is False


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({"notify_services": ["Mobile_App"]}, id="upper_case"),
        pytest.param({"notify_services": ["send_message"]}, id="send_message"),
        pytest.param({"notify_services": ["notify.mobile_app"]}, id="with_domain"),
        pytest.param({"notify_entities": ["light.kitchen"]}, id="wrong_domain"),
        pytest.param({"notify_entities": ["notify."]}, id="no_object_id"),
        pytest.param({"notify_services": ["a", "a"]}, id="duplicate"),
        pytest.param(
            {"notify_entities": ["notify.a", "notify.a"]}, id="duplicate_entity"
        ),
        pytest.param({"notify_services": "mobile_app_a"}, id="not_a_list"),
    ],
)
def test_validation_rejects_bad_targets(settings):
    with pytest.raises(InputError, match="invalid notify target"):
        _validated(**settings)


def test_validation_accepts_services_and_entities():
    values = _validated(
        notify_services=["mobile_app_pixel_8", "mobile_app_tablet"],
        notify_entities=["notify.kitchen_display"],
        notify_language="pl",
    )

    assert values["notify_services"] == ["mobile_app_pixel_8", "mobile_app_tablet"]
    assert values["notify_entities"] == ["notify.kitchen_display"]


def test_invalid_language_is_rejected():
    with pytest.raises(InputError, match="notify_language"):
        _validated(notify_language="de")


@pytest.mark.parametrize(
    ("settings", "sender", "blueprint_may_send"),
    [
        ({}, "off", False),
        ({"notify_enabled": True}, "blueprint", True),
        (
            {"notify_enabled": True, "notify_services": ["mobile_app_a"]},
            "integration",
            False,
        ),
        (
            {"notify_enabled": True, "notify_entities": ["notify.kitchen"]},
            "integration",
            False,
        ),
        ({"notify_enabled": False, "notify_services": ["mobile_app_a"]}, "off", False),
    ],
)
def test_published_sender_matrix(settings, sender, blueprint_may_send):
    preferences = compute(_config(**settings), {}, NOW)["notification_preferences"]

    assert preferences["notify_sender"] == sender
    assert preferences["notify_enabled"] is blueprint_may_send
    # The other preferences are published unchanged.
    assert preferences["notify_events"] == ["favorable", "limit"]
    assert preferences["notify_minimum_hours"] == 2
    assert preferences["quiet_start"] == "22:00:00"


def test_one_published_dict_never_lets_both_senders_pass():
    for enabled in (False, True):
        for services in ([], ["mobile_app_a"]):
            preferences = compute(
                _config(notify_enabled=enabled, notify_services=services), {}, NOW
            )["notification_preferences"]
            both = (
                preferences["notify_enabled"] is True
                and preferences["notify_sender"] == "integration"
            )
            assert not both


def test_native_notifications_active_needs_switch_and_target():
    assert not native_notifications_active({})
    assert not native_notifications_active({"notify_enabled": True})
    assert not native_notifications_active({"notify_services": ["a"]})
    assert native_notifications_active(
        {"notify_enabled": True, "notify_entities": ["notify.a"]}
    )


FORM_KEYS = {
    "data": ("notify_services", "notify_entities", "notify_language"),
    "data_description": ("notify_services", "notify_entities"),
}


@pytest.mark.parametrize("name", ["strings", "en", "pl"])
def test_new_notification_strings_in_every_translation_file(name):
    path = COMPONENT / (
        "strings.json" if name == "strings" else f"translations/{name}.json"
    )
    document = json.loads(path.read_text(encoding="utf-8"))

    for flow in ("config", "options"):
        step = document[flow]["step"]["notifications"]
        for section, keys in FORM_KEYS.items():
            for key in keys:
                assert step[section][key], (flow, section, key)
        assert "energy_compass.send_test_notification" in step["description"]
        assert "{detail}" in step["description"]
        assert document[flow]["error"]["invalid_notify_target"]
    assert set(document["selector"]["notify_language"]["options"]) == {
        "auto",
        "en",
        "pl",
    }
