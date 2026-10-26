"""`load_power` measurement: Atlas telemetry only (T-122).

The load forecast keeps its own source (here a long-term statistic); the measurement is
added, edited and removed in the sources flow like `pv_power` and never touches
`sources.load`.
"""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.config_models import NumericSetting
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import EntityBinding

STATISTIC = "sensor.inverter_total_load_consumption"


def _config():
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    config["sources"]["load"].update(
        mode="recorder", statistic_id=STATISTIC, power=None, daily_estimate=None
    )
    return config


def _source_value(inventory, entity_id):
    choices = next(
        field.config["options"]
        for key, field in inventory["data_schema"].schema.items()
        if str(key) == "source"
    )
    return next(choice["value"] for choice in choices if entity_id in choice["label"])


async def _open_sources(hass, config):
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "sources"})
    return entry, fid


async def _select_source(hass, fid, entity_id):
    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    await hass.config_entries.options.async_configure(
        fid, {"source": _source_value(inventory, entity_id)}
    )


async def test_load_power_is_offered_as_a_source_target(
    recorder_mock, hass, enable_custom_integrations
):
    _, fid = await _open_sources(hass, _config())
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_add"}
    )
    targets = next(
        field.config["options"]
        for key, field in result["data_schema"].schema.items()
        if str(key) == "target"
    )
    assert "load_power" in [choice["value"] for choice in targets]


async def test_add_load_power_in_watts_beside_a_statistic_load_source(
    recorder_mock, hass, enable_custom_integrations
):
    config = _config()
    entry, fid = await _open_sources(hass, config)
    hass.states.async_set(
        "sensor.inverter_load_power", "850", {"unit_of_measurement": "W"}
    )
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_add"}
    )
    mode = await hass.config_entries.options.async_configure(
        fid, {"target": "load_power"}
    )
    assert mode["step_id"] == "source_mode"
    result = await hass.config_entries.options.async_configure(
        fid, {"mode": "measurement"}
    )
    assert result["step_id"] == "source_entity"
    await hass.config_entries.options.async_configure(
        fid, {"entity_id": "sensor.inverter_load_power"}
    )
    result = await hass.config_entries.options.async_configure(fid, {})
    fields = {str(key): key for key in result["data_schema"].schema}
    assert fields["max_age_seconds"].default() == 600
    result = await hass.config_entries.options.async_configure(
        fid,
        {
            "unit": "W",
            "sign": 1,
            "timestamp_path": "last_updated",
            "max_age_seconds": 900,
        },
    )
    assert result["step_id"] == "sources"
    flow = hass.config_entries.options._progress[fid]
    setting = NumericSetting.from_dict(flow._draft["measurements"]["load_power"])
    assert setting.entity.entity_id == "sensor.inverter_load_power"
    assert setting.unit == "kW"
    assert setting.source_unit == "W"
    assert setting.multiplier == pytest.approx(0.001)
    assert setting.max_age_seconds == 900
    # The load forecast keeps its long-term statistic.
    load = flow._draft["sources"]["load"]
    assert load["statistic_id"] == STATISTIC
    assert load.get("power") is None
    assert dict(entry.data) == config


async def test_edit_load_power_changes_entity_and_sign(
    recorder_mock, hass, enable_custom_integrations
):
    config = _config()
    config["measurements"]["load_power"] = NumericSetting(
        entity=EntityBinding("sensor.old_load_power"),
        unit="kW",
        source_unit="W",
        multiplier=0.001,
        max_age_seconds=600,
    ).to_dict()
    hass.states.async_set("sensor.old_load_power", "100", {"unit_of_measurement": "W"})
    hass.states.async_set(
        "sensor.new_load_power", "-0.3", {"unit_of_measurement": "kW"}
    )
    _, fid = await _open_sources(hass, config)
    await _select_source(hass, fid, "old_load_power")
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_edit"}
    )
    await hass.config_entries.options.async_configure(
        fid, {"entity_id": "sensor.new_load_power"}
    )
    result = await hass.config_entries.options.async_configure(fid, {})
    result = await hass.config_entries.options.async_configure(
        fid,
        {
            "unit": "kW",
            "sign": -1,
            "timestamp_path": "last_updated",
            "max_age_seconds": 600,
        },
    )
    assert result["step_id"] == "sources"
    flow = hass.config_entries.options._progress[fid]
    setting = NumericSetting.from_dict(flow._draft["measurements"]["load_power"])
    assert setting.entity.entity_id == "sensor.new_load_power"
    assert setting.multiplier == pytest.approx(-1)
    assert flow._draft["sources"]["load"]["statistic_id"] == STATISTIC


async def test_remove_load_power_leaves_the_load_source(
    recorder_mock, hass, enable_custom_integrations
):
    config = _config()
    config["measurements"]["load_power"] = NumericSetting(
        entity=EntityBinding("sensor.inverter_load_power"),
        unit="kW",
        source_unit="W",
        multiplier=0.001,
        max_age_seconds=600,
    ).to_dict()
    _, fid = await _open_sources(hass, config)
    await _select_source(hass, fid, "inverter_load_power")
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_remove"}
    )
    result = await hass.config_entries.options.async_configure(fid, {"confirm": True})
    assert result["type"] == "menu"
    flow = hass.config_entries.options._progress[fid]
    assert "load_power" not in flow._draft["measurements"]
    assert flow._draft["sources"]["load"]["statistic_id"] == STATISTIC
