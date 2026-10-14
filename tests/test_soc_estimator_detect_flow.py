# ruff: noqa: F811
"""The detected-sources step offers the counter SOC estimator and asks for its settings."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from test_detect_flow import (  # noqa: F401
    defaults,
    detected_form,
    fields,
    rich_site,
)
from test_detect_ha import PLATFORMS, forecast_platform, site  # noqa: F401

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")


@pytest.fixture
def platform():
    with patch(PLATFORMS, AsyncMock(return_value={"fx_forecast": forecast_platform()})):
        yield


def _estimator_entities(hass, site):
    registry = er.async_get(hass)
    for key, object_id, state, unit in (
        ("total_battery_charge", "fx_batt_charge_total", "6300.0", "kWh"),
        ("total_battery_discharge", "fx_batt_discharge_total", "5670.0", "kWh"),
        ("battery_voltage", "fx_pack_voltage", "520.0", "V"),
        ("battery_current", "fx_pack_current", "1.0", "A"),
    ):
        registry.async_get_or_create(
            "sensor",
            "solarman",
            f"fx-{key}",
            suggested_object_id=object_id,
            config_entry=site["solarman"],
            device_id=site["inverter"].id,
            translation_key=key,
        )
        hass.states.async_set(
            f"sensor.{object_id}", state, {"unit_of_measurement": unit}
        )


async def test_the_detected_offer_opens_the_estimator_settings_before_the_preview(
    hass, rich_site, platform
):
    _estimator_entities(hass, rich_site)
    fid, form = await detected_form(hass)
    assert "soc_estimator" in fields(form)
    assert defaults(form)["soc_estimator"] is False
    assert "pack voltage that is 0 %" in form["description_placeholders"]["notes"]
    manager = hass.config_entries.flow
    result = await manager.async_configure(
        fid, {**defaults(form), "soc_estimator": True, "soc": "skip"}
    )
    assert result["step_id"] == "soc_estimator"
    draft = manager._progress[fid]._draft
    assert draft["sources"]["soc_mode"] == "counter_estimator"
    assert draft["sources"]["soc"] is None
    assert draft["soc_estimator"]["bindings"]["voltage"]["entity_id"] == (
        "sensor.fx_pack_voltage"
    )
    form = result
    schema = {str(key): key for key in form["data_schema"].schema}
    assert (
        schema["charge_counter"].description["suggested_value"]
        == "sensor.fx_batt_charge_total"
    )
    result = await manager.async_configure(
        fid,
        {
            "soc_source": "counter_estimator",
            "charge_counter": "sensor.fx_batt_charge_total",
            "discharge_counter": "sensor.fx_batt_discharge_total",
            "voltage": "sensor.fx_pack_voltage",
            "current": "sensor.fx_pack_current",
            "empty_voltage": 496,
            "anchor_on_candidates": False,
            "shadow": False,
        },
    )
    assert result["step_id"] == "preview"
    await hass.async_stop()


async def test_not_ticking_it_leaves_the_flow_unchanged(hass, rich_site, platform):
    _estimator_entities(hass, rich_site)
    fid, form = await detected_form(hass)
    result = await hass.config_entries.flow.async_configure(fid, defaults(form))
    assert result["step_id"] == "preview"
    assert "soc_mode" not in hass.config_entries.flow._progress[fid]._draft["sources"]
    await hass.async_stop()
