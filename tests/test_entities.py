from custom_components.energy_compass.entity import PLAN_ATTRIBUTES
from custom_components.energy_compass.sensor import SENSOR_KEYS


def test_public_contract():
    assert set(SENSOR_KEYS) == {
        "consumption_compass",
        "consumption_cost",
        "flexible_energy_depth",
        "next_change",
        "next_boost_start",
        "next_cheap_start",
        "next_limit_start",
        "energy_compass",
        "plan",
        "expected_net_cost",
        "expected_wear_cost",
        "optimizer_status",
        "battery_balance",
    }
    assert {
        "intervals",
        "outlook",
        "favorable_windows",
        "flexible_load_profiles",
        "load_quality",
    } <= PLAN_ATTRIBUTES


async def test_absent_window_is_unavailable_with_coverage_status(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.energy_compass.settings import default_configuration

    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Windows", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    window = hass.states.get("sensor.windows_next_limit_start")
    assert window.state == "unavailable"
    depth = hass.states.get("sensor.windows_flexible_energy_depth")
    assert depth.state == "3.0"
    assert depth.attributes["device_class"] == "energy"
    assert depth.attributes["unit_of_measurement"] == "kWh"
    assert "state_class" not in depth.attributes
    assert depth.attributes["anchor_energy_kwh"] == 3
    assert len(depth.attributes["flexible_load_profiles"]) == 5
    assert (
        hass.states.get("sensor.windows_plan").attributes["window_status"]["limit"]
        == "none_in_coverage"
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unknown_current_probe_keeps_known_future_advice(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.energy_compass.config_models import LoadSource
    from custom_components.energy_compass.settings import default_configuration
    from custom_components.energy_compass.sources.bindings import (
        EntityBinding,
        IntervalBinding,
    )

    freezer.move_to("2026-09-17T00:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
        grid_import_kw=1.5,
    )
    binding = IntervalBinding(
        EntityBinding("sensor.household_forecast", attribute="rows"),
        start_path="start",
        interval_minutes=60,
        value_path="load",
        unit="kWh",
    )
    config["sources"]["load"] = LoadSource("forecast", forecast=binding).to_dict()
    hass.states.async_set(
        "sensor.household_forecast",
        "ok",
        {
            "rows": [
                {"start": "2026-09-17T00:00:00+00:00", "load": 0.9},
                {"start": "2026-09-17T01:00:00+00:00", "load": 0.1},
            ]
        },
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Future", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert hass.states.get("sensor.future_consumption_compass").state == "unavailable"
    assert hass.states.get("sensor.future_consumption_cost").state == "unavailable"
    assert (
        hass.states.get("sensor.future_next_boost_start").state
        == "2026-09-17T01:00:00+00:00"
    )
    assert hass.states.get("sensor.future_energy_compass").state == "HOLD"
    assert hass.states.get("sensor.future_plan").state != "unavailable"
    assert hass.states.get("sensor.future_expected_net_cost").state == "0.0"
    validity = hass.states.get("binary_sensor.future_forecast_valid")
    assert validity.state == "on"
    assert validity.attributes["current_guidance_valid"] is False
    plan_quality = hass.states.get("sensor.future_plan").attributes["load_quality"]
    assert plan_quality == validity.attributes["load_quality"]
    assert plan_quality["source_mode"] == "forecast"
    assert plan_quality["method"] == "forecast"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_helper_precision_and_blueprint_preferences(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.energy_compass.sensor import EnergyCompassSensor
    from custom_components.energy_compass.settings import default_configuration

    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        notify_minimum_hours=3,
        window_label="Flexible use",
        boost_color="#123456",
        debounce_seconds=0,
    )
    config["helpers"]["cost_precision"] = {
        "entity": {"entity_id": "input_number.precision"},
        "unit": "",
        "max_age_seconds": None,
    }
    hass.states.async_set("input_number.precision", "1")
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Precision", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    sensor = EnergyCompassSensor(entry.runtime_data, "consumption_cost")
    assert sensor.suggested_display_precision == 1
    assert (
        registry.async_get("sensor.precision_consumption_cost").options["sensor"][
            "suggested_display_precision"
        ]
        == 1
    )
    registry.async_update_entity_options(
        "sensor.precision_consumption_cost",
        "sensor",
        {"display_precision": 4, "suggested_display_precision": 1},
    )
    hass.states.async_set("input_number.precision", "2")
    await hass.async_block_till_done()
    assert sensor.suggested_display_precision == 2
    options = registry.async_get("sensor.precision_consumption_cost").options["sensor"]
    assert options["suggested_display_precision"] == 2
    assert options["display_precision"] == 4
    plan = hass.states.get("sensor.precision_plan")
    assert plan.attributes["presentation"]["window_label"] == "Flexible use"
    assert plan.attributes["presentation"]["boost_color"] == "#123456"
    assert plan.attributes["notification_preferences"]["notify_minimum_hours"] == 3
    assert plan.attributes["notification_preferences"]["notify_enabled"] is False
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_plan_attributes_include_strategy_fields(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.energy_compass.settings import default_configuration

    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Strategy", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    plan = hass.states.get("sensor.strategy_plan")
    assert plan.attributes["strategy"] == "cost_min"
    assert plan.attributes["autonomy_shortfall_kwh"] == 0.0
    assert plan.attributes["peak_reserve_shortfall_kwh"] == 0.0
    assert plan.attributes["cap_violation_kwh"] == 0.0
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_plan_strategy_attribute_is_a_scalar_not_a_list(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.energy_compass.settings import default_configuration

    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Scalar", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    plan = hass.states.get("sensor.scalar_plan")
    assert isinstance(plan.attributes["strategy"], str)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_attribute_schema_version_is_three(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.energy_compass.settings import default_configuration

    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Schema", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    plan = hass.states.get("sensor.schema_plan")
    assert plan.attributes["attribute_schema_version"] == 3
    assert await hass.config_entries.async_unload(entry.entry_id)
