"""HA state -> Atlas telemetry mapping (ADR-0019 §6 table)."""

from datetime import UTC, datetime

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
    config = default_configuration("EUR", "UTC")
    config["measurements"]["battery_power"] = _power_setting("sensor.batt")
    states = {"sensor.batt": _state("-1.0", "kW")}
    feed = resolve_feed(config, states, NOW)
    assert feed["batt_w"] == -1000.0


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
    states = {"sensor.soc": _state("55")}
    feed = resolve_feed(config, states, NOW)
    assert feed["soc_pct"] == -55.0


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
