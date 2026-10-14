"""The estimator's configuration block: required voltages and the hold range."""

import pytest

from custom_components.energy_compass import soc_estimator_config as cfg
from custom_components.energy_compass.engine.models import InputError

BINDINGS = {role: {"entity_id": f"sensor.{role}"} for role in cfg.REQUIRED_ROLES}


def _config(**block):
    return {
        "sources": {"battery_enabled": True, "soc_mode": cfg.COUNTER_ESTIMATOR},
        "settings": {"capacity_kwh": 20.0},
        "soc_estimator": {
            "bindings": BINDINGS,
            "empty_voltage": 480,
            "full_min_voltage": 540,
            **block,
        },
    }


def test_a_complete_block_gives_the_voltage_and_the_one_hour_hold():
    config = _config()
    cfg.validate(config)
    settings = cfg.settings(config)
    assert settings.full_min_voltage_v == 540.0
    assert settings.full_hold_seconds == 3600.0


def test_the_full_voltage_has_no_default():
    config = _config()
    del config["soc_estimator"]["full_min_voltage"]
    with pytest.raises(InputError, match="full pack"):
        cfg.validate(config)


@pytest.mark.parametrize("value", [480, 400, 0, -1, True, "540", float("nan")])
def test_the_full_voltage_must_be_a_number_above_the_empty_voltage(value):
    with pytest.raises(InputError):
        cfg.validate(_config(full_min_voltage=value))


@pytest.mark.parametrize("seconds", [3599, 600, 60, 100000])
def test_the_hold_is_at_least_an_hour_and_at_most_a_day(seconds):
    with pytest.raises(InputError, match="full_hold_seconds"):
        cfg.validate(_config(full_hold_seconds=seconds))


@pytest.mark.parametrize("seconds", [3600, 7200, 86400])
def test_a_longer_hold_is_allowed(seconds):
    config = _config(full_hold_seconds=seconds)
    cfg.validate(config)
    assert cfg.settings(config).full_hold_seconds == seconds


def test_the_balance_window_covers_the_full_hold_under_the_estimator_only():
    assert cfg.balance_lead_minutes(_config()) == 60.0
    assert cfg.balance_lead_minutes(_config(full_hold_seconds=5400)) == 90.0
    entity = _config()
    entity["sources"]["soc_mode"] = "entity"
    assert cfg.balance_lead_minutes(entity) == 0.0
