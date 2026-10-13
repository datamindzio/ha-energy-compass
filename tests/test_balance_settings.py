# tests/test_balance_settings.py
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.settings import (
    default_configuration,
    validate_configuration,
)

NOW = datetime(2026, 9, 24, tzinfo=UTC)
KEYS = (
    "lfp_balance",
    "balance_interval_days",
    "balance_hold_minutes",
    "balance_soc_threshold",
    "balance_value",
)


def _config():
    return default_configuration("PLN", "Europe/Warsaw")


def test_balance_defaults():
    values = validate_configuration(_config(), {}, NOW, sources=False)
    assert values["lfp_balance"] is False
    assert values["balance_interval_days"] == 7
    assert values["balance_hold_minutes"] == 60
    assert values["balance_soc_threshold"] == 99
    assert values["balance_value"] == 5.0


def test_entries_without_balance_keys_get_defaults():
    config = _config()
    for key in KEYS:
        del config["settings"][key]
    values = validate_configuration(config, {}, NOW, sources=False)
    assert values["lfp_balance"] is False
    assert values["balance_hold_minutes"] == 60


@pytest.mark.parametrize(
    "key,value",
    [
        ("balance_soc_threshold", 89),
        ("balance_soc_threshold", 101),
        ("balance_hold_minutes", 10),
        ("balance_hold_minutes", 30.5),
        ("balance_interval_days", 0),
        ("balance_value", -1),
        ("lfp_balance", "yes"),
    ],
)
def test_balance_bounds(key, value):
    config = _config()
    config["settings"][key] = value
    with pytest.raises(InputError):
        validate_configuration(config, {}, NOW, sources=False)


def test_balance_labels_in_every_translation_section():
    root = Path("custom_components/energy_compass")
    anchors = {
        "strings.json": '"idle_drain_kw"',
        "translations/en.json": '"idle_drain_kw"',
        "translations/pl.json": '"idle_drain_kw"',
    }
    for name, anchor in anchors.items():
        text = (root / name).read_text()
        json.loads(text)
        sections = text.count(anchor)
        assert sections > 0, (name, anchor)
        for key in KEYS:
            assert text.count(f'"{key}"') == sections, (name, key)
