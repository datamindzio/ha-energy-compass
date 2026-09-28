"""HA state -> Atlas telemetry mapping (ADR-0019 §6 table)."""

from datetime import UTC, datetime

import pytest

from custom_components.energy_compass.atlas.mapping import (
    resolve_feed,
    tracked_entity_ids,
)
from custom_components.energy_compass.config_models import NumericSetting
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import EntityBinding

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


def _power_setting(entity_id, attribute=None):
    return NumericSetting(
        entity=EntityBinding(entity_id, attribute=attribute), unit="kW"
    ).to_dict()


def _energy_setting(entity_id):
    return NumericSetting(entity=EntityBinding(entity_id), unit="kWh").to_dict()


def _state(value, unit=None, **attrs):
    attributes = dict(attrs)
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    return {
        "state": value,
        "attributes": attributes,
        "last_updated": NOW.isoformat(),
    }


def test_pv_and_grid_power_fed_in_watts():
    config = default_configuration("EUR", "UTC")
    config["measurements"]["pv_power"] = _power_setting("sensor.pv")
    config["measurements"]["grid_import_power"] = _power_setting("sensor.grid_in")
    config["measurements"]["grid_export_power"] = _power_setting("sensor.grid_out")
    states = {
        "sensor.pv": _state("2.5", "kW"),
        "sensor.grid_in": _state("0.3", "kW"),
        "sensor.grid_out": _state("0", "kW"),
    }
    feed = resolve_feed(config, states, NOW)
    assert feed["pv_w"] == 2500.0
    assert feed["grid_import_w"] == 300.0
    assert feed["grid_export_w"] == 0.0


def _recorder_load(config, *, history_unit="kW", history_sign=1.0):
    config["sources"]["load"]["mode"] = "recorder"
    config["sources"]["load"]["power"] = EntityBinding("sensor.load").to_dict()
    config["sources"]["load"]["history_unit"] = history_unit
    config["sources"]["load"]["history_sign"] = history_sign


def test_load_power_fed_only_in_recorder_mode():
    config = default_configuration("EUR", "UTC")
    _recorder_load(config, history_unit="kW")
    states = {"sensor.load": _state("1.2")}
    feed = resolve_feed(config, states, NOW)
    assert feed["load_w"] == 1200.0
    assert "sensor.load" in tracked_entity_ids(config)


def test_load_power_not_fed_outside_recorder_mode():
    config = default_configuration("EUR", "UTC")  # default mode is daily_estimate
    states = {}
    assert "load_w" not in resolve_feed(config, states, NOW)


def test_load_power_watts_unit_is_not_rescaled():
    config = default_configuration("EUR", "UTC")
    _recorder_load(config, history_unit="W")
    states = {"sensor.load": _state("500")}
    feed = resolve_feed(config, states, NOW)
    assert feed["load_w"] == 500.0


def test_load_power_negative_history_sign_is_applied():
    config = default_configuration("EUR", "UTC")
    _recorder_load(config, history_unit="kW", history_sign=-1)
    states = {"sensor.load": _state("1.2")}
    feed = resolve_feed(config, states, NOW)
    assert feed["load_w"] == -1200.0


def test_battery_power_prefers_single_measurement():
    # Compass battery_power is + = discharge; Atlas batt_w is + = charge (ADR-0019 T-402).
    config = default_configuration("EUR", "UTC")
    config["measurements"]["battery_power"] = _power_setting("sensor.batt")
    config["measurements"]["battery_charge_power"] = _power_setting("sensor.charge")
    states = {
        "sensor.batt": _state("-1.0", "kW"),
        "sensor.charge": _state("5.0", "kW"),
    }
    feed = resolve_feed(config, states, NOW)
    assert feed["batt_w"] == 1000.0


def test_battery_power_discharge_is_negative_batt_w():
    config = default_configuration("EUR", "UTC")
    config["measurements"]["battery_power"] = _power_setting("sensor.batt")
    states = {"sensor.batt": _state("2.0", "kW")}
    feed = resolve_feed(config, states, NOW)
    assert feed["batt_w"] == -2000.0


def test_battery_power_falls_back_to_charge_minus_discharge():
    config = default_configuration("EUR", "UTC")
    config["measurements"]["battery_charge_power"] = _power_setting("sensor.charge")
    config["measurements"]["battery_discharge_power"] = _power_setting(
        "sensor.discharge"
    )
    states = {
        "sensor.charge": _state("0.5", "kW"),
        "sensor.discharge": _state("0.1", "kW"),
    }
    feed = resolve_feed(config, states, NOW)
    assert feed["batt_w"] == 400.0


def test_soc_percent_converts_fraction():
    config = default_configuration("EUR", "UTC")
    config["sources"]["soc"] = EntityBinding("sensor.soc").to_dict()
    config["soc_options"]["unit"] = "fraction"
    states = {"sensor.soc": _state("0.42")}
    feed = resolve_feed(config, states, NOW)
    assert feed["soc_pct"] == 42.0


def test_soc_percent_left_alone_when_already_percent():
    config = default_configuration("EUR", "UTC")
    config["sources"]["soc"] = EntityBinding("sensor.soc").to_dict()
    states = {"sensor.soc": _state("55")}
    feed = resolve_feed(config, states, NOW)
    assert feed["soc_pct"] == 55.0


def test_soc_percent_converts_kwh_using_capacity():
    config = default_configuration("EUR", "UTC")
    config["sources"]["soc"] = EntityBinding("sensor.soc").to_dict()
    config["soc_options"]["unit"] = "kWh"
    config["settings"]["capacity_kwh"] = 10.0
    states = {"sensor.soc": _state("5")}
    feed = resolve_feed(config, states, NOW)
    assert feed["soc_pct"] == 50.0


def test_soc_percent_not_fed_when_kwh_and_capacity_unknown():
    config = default_configuration("EUR", "UTC")
    config["sources"]["soc"] = EntityBinding("sensor.soc").to_dict()
    config["soc_options"]["unit"] = "kWh"
    config["settings"]["capacity_kwh"] = 0
    states = {"sensor.soc": _state("5")}
    feed = resolve_feed(config, states, NOW)
    assert "soc_pct" not in feed


def test_soc_percent_applies_sign():
    config = default_configuration("EUR", "UTC")
    config["sources"]["soc"] = EntityBinding("sensor.soc").to_dict()
    config["soc_options"]["sign"] = -1
    states = {"sensor.soc": _state("-55")}
    feed = resolve_feed(config, states, NOW)
    assert feed["soc_pct"] == 55.0


def test_soc_unit_mismatch_not_fed():
    # Same check as runtime._soc_value: entity reports kWh, Compass expects %.
    config = default_configuration("EUR", "UTC")
    config["sources"]["soc"] = EntityBinding("sensor.soc").to_dict()
    states = {"sensor.soc": _state("5", "kWh")}
    assert "soc_pct" not in resolve_feed(config, states, NOW)


def test_load_power_unit_mismatch_not_fed():
    # Same check as runtime._soc_value/backfill_service._load_stat: entity reports W,
    # sources.load.history_unit says kW -> must not be silently fed 1000x too high.
    config = default_configuration("EUR", "UTC")
    _recorder_load(config, history_unit="kW")
    states = {"sensor.load": _state("500", "W")}
    assert "load_w" not in resolve_feed(config, states, NOW)


def test_load_power_matching_or_unknown_unit_is_not_penalized():
    config = default_configuration("EUR", "UTC")
    _recorder_load(config, history_unit="kW")
    assert (
        resolve_feed(config, {"sensor.load": _state("1.2", "kW")}, NOW)["load_w"]
        == 1200.0
    )
    # No unit_of_measurement at all -> not rejected (same leniency resolve_numeric gives).
    assert resolve_feed(config, {"sensor.load": _state("1.2")}, NOW)["load_w"] == 1200.0


def test_load_power_without_history_unit_not_fed():
    config = default_configuration("EUR", "UTC")
    _recorder_load(config)
    del config["sources"]["load"]["history_unit"]
    states = {"sensor.load": _state("500")}
    assert "load_w" not in resolve_feed(config, states, NOW)


# Contract bounds of MetricValues (ADR-0019 Amendment 2026-09-28 (T-402)): every bounded
# key at bound - eps and bound + eps.
@pytest.mark.parametrize(
    ("measurement", "key"),
    [("grid_import_power", "grid_import_w"), ("grid_export_power", "grid_export_w")],
)
@pytest.mark.parametrize(
    ("reading_kw", "fed_w"), [("-0.003", 0.0), ("0", 0.0), ("0.001", 1.0)]
)
def test_grid_power_clamped_to_zero(measurement, key, reading_kw, fed_w):
    config = default_configuration("EUR", "UTC")
    config["measurements"][measurement] = _power_setting("sensor.grid")
    feed = resolve_feed(config, {"sensor.grid": _state(reading_kw, "kW")}, NOW)
    assert feed[key] == fed_w


@pytest.mark.parametrize(
    ("reading", "fed"),
    [("-0.5", 0.0), ("0", 0.0), ("99.9", 99.9), ("100.4", 100.0), ("250", 100.0)],
)
def test_soc_percent_clamped_to_0_100(reading, fed):
    config = default_configuration("EUR", "UTC")
    config["sources"]["soc"] = EntityBinding("sensor.soc").to_dict()
    feed = resolve_feed(config, {"sensor.soc": _state(reading)}, NOW)
    assert feed["soc_pct"] == pytest.approx(fed)


def test_soc_kwh_above_capacity_clamped_to_100():
    config = default_configuration("EUR", "UTC")
    config["sources"]["soc"] = EntityBinding("sensor.soc").to_dict()
    config["soc_options"]["unit"] = "kWh"
    config["settings"]["capacity_kwh"] = 7.5
    feed = resolve_feed(config, {"sensor.soc": _state("7.9")}, NOW)
    assert feed["soc_pct"] == 100.0


@pytest.mark.parametrize(
    ("measurement", "key"),
    [
        ("pv_energy", "pv_kwh_total"),
        ("grid_import_energy", "import_kwh_total"),
        ("grid_export_energy", "export_kwh_total"),
    ],
)
@pytest.mark.parametrize(("reading", "fed"), [("-0.1", None), ("0", 0.0), ("0.1", 0.1)])
def test_negative_energy_counter_not_fed(measurement, key, reading, fed):
    config = default_configuration("EUR", "UTC")
    config["measurements"][measurement] = _energy_setting("sensor.counter")
    states = {"sensor.counter": _state(reading, "kWh", state_class="total_increasing")}
    assert resolve_feed(config, states, NOW).get(key) == fed


@pytest.mark.parametrize("reading", ["nan", "inf", "-inf"])
def test_non_finite_readings_not_fed(reading):
    config = default_configuration("EUR", "UTC")
    config["measurements"]["pv_power"] = _power_setting("sensor.pv")
    config["sources"]["soc"] = EntityBinding("sensor.soc").to_dict()
    _recorder_load(config)
    states = {
        "sensor.pv": _state(reading, "kW"),
        "sensor.soc": _state(reading),
        "sensor.load": _state(reading),
    }
    assert resolve_feed(config, states, NOW) == {}


def test_energy_counters_require_total_state_class():
    config = default_configuration("EUR", "UTC")
    config["measurements"]["pv_energy"] = _energy_setting("sensor.pv_energy")
    states = {"sensor.pv_energy": _state("12.3", "kWh", state_class="total_increasing")}
    feed = resolve_feed(config, states, NOW)
    assert feed["pv_kwh_total"] == 12.3


def test_energy_counter_not_fed_without_state_class():
    config = default_configuration("EUR", "UTC")
    config["measurements"]["pv_energy"] = _energy_setting("sensor.pv_energy")
    states = {"sensor.pv_energy": _state("12.3", "kWh")}  # no state_class
    feed = resolve_feed(config, states, NOW)
    assert "pv_kwh_total" not in feed


def test_unbound_measurement_never_fed():
    config = default_configuration("EUR", "UTC")  # nothing bound
    feed = resolve_feed(config, {}, NOW)
    assert feed == {}


def test_unavailable_state_not_fed():
    config = default_configuration("EUR", "UTC")
    config["measurements"]["pv_power"] = _power_setting("sensor.pv")
    states = {"sensor.pv": _state("unavailable", "kW")}
    feed = resolve_feed(config, states, NOW)
    assert "pv_w" not in feed


def test_today_bindings_are_never_looked_up():
    config = default_configuration("EUR", "UTC")
    # A *_today measurement is a real Compass target but not an Atlas key: even if
    # bound, resolve_feed never reads it because it never looks it up by that name.
    config["measurements"]["pv_energy_today"] = _energy_setting("sensor.pv_today")
    states = {"sensor.pv_today": _state("3.0", "kWh", state_class="total_increasing")}
    feed = resolve_feed(config, states, NOW)
    assert feed == {}
