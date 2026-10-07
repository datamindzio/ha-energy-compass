from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import State
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.config_models import LoadSource
from custom_components.energy_compass.engine.models import ForecastSettings
from custom_components.energy_compass.runtime import async_history
from custom_components.energy_compass.settings import (
    NUMBERS,
    default_configuration,
    merged_configuration,
)
from custom_components.energy_compass.sources.history import (
    load_for_slots,
    power_samples_to_hours,
    recorder_statistics_to_hours,
)


@pytest.mark.parametrize("unit,scale", [("kWh", 1), ("Wh", 1000)])
def test_native_recorder_deltas_belong_to_second_row_hour(unit, scale):
    start = datetime(2026, 9, 16, 8, tzinfo=UTC)
    rows = tuple(
        {"start": start + timedelta(hours=index), "sum": value * scale}
        for index, value in enumerate((10, 11, 14, 21))
    )
    assert recorder_statistics_to_hours(
        rows, unit=unit, observed_before=start + timedelta(hours=3, minutes=30)
    ) == ((start + timedelta(hours=1), 1), (start + timedelta(hours=2), 3))
    assert recorder_statistics_to_hours(
        rows, unit=unit, observed_before=start + timedelta(hours=4)
    )[-1] == (start + timedelta(hours=3), 7)


def test_native_recorder_lookback_boundary_preserves_correct_hour():
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    start = now - timedelta(days=1, hours=1)
    rows = tuple(
        {"start": start + timedelta(hours=index), "sum": value}
        for index, value in enumerate((10, 12, 19))
    )
    assert load_for_slots(
        LoadSource("recorder", statistic_id="sensor.load"),
        {},
        now,
        ((now, now + timedelta(hours=1)),),
        "UTC",
        ForecastSettings(lookback_days=1, minimum_samples=1),
        statistics=rows,
    ) == (2,)


@pytest.mark.parametrize("outage", ["unknown", "unavailable"])
@pytest.mark.parametrize("attribute", [None, "power"])
async def test_native_power_history_retains_outage_and_unaffected_hours(
    recorder_mock, hass, outage, attribute
):
    config = default_configuration("EUR", "UTC")
    config["sources"]["load"] = {
        "mode": "recorder",
        "power": {"entity_id": "sensor.load", "attribute": attribute},
        "history_unit": "W",
    }
    start = datetime(2026, 9, 16, tzinfo=UTC)
    observations = [
        (0, "1000"),
        (15, outage),
        (45, "1000"),
        (60, "2000"),
        (120, "2000"),
    ]
    states = [
        State(
            "sensor.load",
            value,
            {"power": 1000 if value == outage else float(value)},
            last_updated=start + timedelta(minutes=minute),
            last_changed=start + timedelta(minutes=minute),
        )
        for minute, value in observations
    ]
    with patch(
        "custom_components.energy_compass.runtime.get_significant_states",
        return_value={"sensor.load": states},
    ):
        history, _ = await async_history(hass, config, {}, start + timedelta(hours=2))
    assert power_samples_to_hours(history["power_samples"]) == (
        (start + timedelta(hours=1), 2),
    )


MONETARY_VALUES = {
    "buy_rate": 1.3,
    "buy_off_peak_rate": 0.8,
    "sell_rate": 0.7,
    "buy_addition": 0.15,
    "sell_addition": 0.05,
    "wear_per_kwh": 0.14,
    "boost_ceiling": 0.05,
    "limit_floor": 0.8,
    "terminal_value_per_kwh": 0.3,
    "monthly_charge": 25,
    "minimum_export_episode_benefit": 1,
    "maximum_grid_charge_price": 0.61,
    "self_sufficiency_import_price_per_kwh": 6.0,
    "self_sufficiency_export_penalty_per_kwh": 4.0,
    "pv_swap_margin_per_kwh": 0.06,
    "backup_shortfall_price_per_kwh": 2.5,
    "peak_import_price_per_kw": 0.55,
    "cap_violation_price_per_kwh": 2.5,
    "autonomy_margin_per_kwh": 0.12,
    "minimum_grid_charge_episode_benefit": 3.0,
    "import_penalty_per_kwh": 0.12,
    "balance_value": 8.0,
}


async def start_existing_flow(hass, mode, *, monetary_helper=False):
    config = default_configuration("PLN", "UTC")
    config["settings"].update(MONETARY_VALUES)
    config["settings"]["calibration"] = "verified"
    if monetary_helper:
        config["helpers"]["buy_rate"] = {
            "entity": {"entity_id": "sensor.helper_price"},
            "unit": "PLN/kWh",
            "source_unit": "PLN/kWh",
            "max_age_seconds": None,
        }
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    if mode == "options":
        manager = hass.config_entries.options
        result = await manager.async_init(entry.entry_id)
    else:
        manager = hass.config_entries.flow
        result = await manager.async_init(
            "energy_compass",
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
    return entry, manager, result["flow_id"]


async def change_currency(manager, fid, currency):
    await manager.async_configure(fid, {"next_step_id": "installation"})
    return await manager.async_configure(
        fid,
        {
            "name": "Currency review",
            "currency": currency,
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )


@pytest.mark.parametrize("mode", ["options", "reconfigure"])
async def test_currency_change_blocks_direct_preview_save(
    recorder_mock, hass, enable_custom_integrations, mode
):
    entry, manager, fid = await start_existing_flow(hass, mode)
    before = deepcopy(dict(entry.data))
    await change_currency(manager, fid, "EUR")
    flow = manager._progress[fid]
    result = await flow.async_step_preview({"confirm": True})
    assert result["type"] == "form"
    assert result["step_id"] == "currency_review"
    assert entry.data == before
    assert not entry.options


@pytest.mark.parametrize("mode", ["options", "reconfigure"])
async def test_currency_review_requires_acknowledgement_and_saves_edited_amounts(
    recorder_mock, hass, enable_custom_integrations, mode
):
    entry, manager, fid = await start_existing_flow(hass, mode)
    result = await change_currency(manager, fid, "EUR")
    assert result["step_id"] == "currency_review"
    fields = {
        str(key): (key, value) for key, value in result["data_schema"].schema.items()
    }
    assert set(fields) == {*MONETARY_VALUES, "confirm_currency_values"}
    for name, old_value in MONETARY_VALUES.items():
        marker, value_selector = fields[name]
        assert marker.default() == old_value
        expected_unit = NUMBERS[name][4].replace("currency", "EUR")
        assert value_selector.config["unit_of_measurement"] == expected_unit
    reviewed = {name: value / 5 for name, value in MONETARY_VALUES.items()}
    result = await manager.async_configure(
        fid, {**reviewed, "confirm_currency_values": False}
    )
    assert result["step_id"] == "currency_review"
    assert result["errors"] == {"base": "currency_review_required"}
    assert entry.data["currency"] == "PLN"
    result = await manager.async_configure(
        fid, {**reviewed, "confirm_currency_values": True}
    )
    assert result["type"] == "menu"
    result = await manager.async_configure(fid, {"next_step_id": "preview"})
    assert result["step_id"] == "preview"
    assert not result["errors"]
    result = await manager.async_configure(fid, {"confirm": True})
    assert result["type"] == ("create_entry" if mode == "options" else "abort")
    saved = merged_configuration(entry)
    assert saved["currency"] == "EUR"
    assert saved["settings"]["calibration"] == "unvalidated"
    assert {name: saved["settings"][name] for name in reviewed} == reviewed


@pytest.mark.parametrize("mode", ["options", "reconfigure"])
async def test_currency_switch_invalidates_prior_review(
    recorder_mock, hass, enable_custom_integrations, mode
):
    entry, manager, fid = await start_existing_flow(hass, mode)
    result = await change_currency(manager, fid, "EUR")
    assert result["step_id"] == "currency_review"
    await manager.async_configure(
        fid, {**MONETARY_VALUES, "confirm_currency_values": True}
    )
    result = await change_currency(manager, fid, "USD")
    assert result["step_id"] == "currency_review"
    result = await manager._progress[fid].async_step_preview({"confirm": True})
    assert result["step_id"] == "currency_review"
    assert entry.data["currency"] == "PLN"


@pytest.mark.parametrize("mode", ["options", "reconfigure"])
async def test_unchanged_currency_needs_no_review(
    recorder_mock, hass, enable_custom_integrations, mode
):
    _, manager, fid = await start_existing_flow(hass, mode)
    result = await change_currency(manager, fid, "PLN")
    assert result["type"] == "menu"
    result = await manager.async_configure(fid, {"next_step_id": "preview"})
    assert result["step_id"] == "preview"
    assert not result["errors"]


@pytest.mark.parametrize("mode", ["options", "reconfigure"])
async def test_currency_review_does_not_convert_helper_currency(
    recorder_mock, hass, enable_custom_integrations, mode
):
    hass.states.async_set(
        "sensor.helper_price", "1", {"unit_of_measurement": "PLN/kWh"}
    )
    entry, manager, fid = await start_existing_flow(hass, mode, monetary_helper=True)
    result = await change_currency(manager, fid, "EUR")
    assert result["step_id"] == "currency_review"
    result = await manager.async_configure(
        fid, {**MONETARY_VALUES, "confirm_currency_values": True}
    )
    assert result["type"] == "menu"
    result = await manager.async_configure(fid, {"next_step_id": "preview"})
    assert result["errors"] == {"base": "invalid_source"}
    assert "helper currency" in result["description_placeholders"]["preview"]
    result = await manager.async_configure(fid, {"confirm": True})
    assert result["errors"] == {"base": "invalid_source"}
    assert entry.data["currency"] == "PLN"
    assert not entry.options
