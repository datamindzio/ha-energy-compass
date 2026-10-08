import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_detect_ha import (  # noqa: F401
    PLATFORMS,
    forecast_platform,
    prefs_sources,
    save_prefs,
    site,
)

from custom_components.energy_compass import config_flow
from custom_components.energy_compass.detect import (
    ROWS,
    SKIP,
    option_label,
    row_label,
)
from custom_components.energy_compass.settings import default_configuration

COMPONENT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "energy_compass"
)


def payload(**overrides):
    values = {
        "name": "Fresh",
        "currency": "PLN",
        "timezone": "Europe/Warsaw",
        "preset": "generic",
        "settlement": "generic",
        "buy_tariff": "generic",
        "inverter": "generic",
        "pv_enabled": True,
        "battery_enabled": True,
    }
    values.update(overrides)
    return values


def rce_records():
    start = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    rows = []
    for index in range(8):
        end = start + timedelta(minutes=15 * (index + 1))
        rows.append(
            {
                "dtime": (end + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                "rce_pln": 400.0 + index,
            }
        )
    return rows


@pytest.fixture
def rich_site(hass, site):  # noqa: F811
    registry = er.async_get(hass)
    rce = MockConfigEntry(domain="rce_pse")
    rce.add_to_hass(hass)
    for key, uid, object_id in (
        ("rce_pse_today_price", "rce-today", "fx_rce_today"),
        ("rce_pse_tomorrow_price", "rce-tomorrow", "fx_rce_tomorrow"),
    ):
        registry.async_get_or_create(
            "sensor",
            "rce_pse",
            uid,
            suggested_object_id=object_id,
            translation_key=key,
            config_entry=rce,
        )
    hass.states.async_set(
        "sensor.fx_rce_today",
        "400",
        {"unit_of_measurement": "PLN/MWh", "prices": rce_records()},
    )
    hass.states.async_set(
        "sensor.fx_rce_tomorrow",
        "unknown",
        {"unit_of_measurement": "PLN/MWh", "prices": []},
    )
    registry.async_get_or_create(
        "sensor",
        "solarman",
        "fx-inverter-soc",
        suggested_object_id="fx_inverter_soc",
        config_entry=site["solarman"],
        device_id=site["inverter"].id,
        translation_key="battery",
    )
    for key, object_id in (
        ("today_production", "fx_prod_today"),
        ("today_energy_export", "fx_export_today"),
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
            f"sensor.{object_id}",
            "0",
            {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
        )
    hass.states.async_set(
        "sensor.fx_inverter_soc",
        "40",
        {"unit_of_measurement": "%", "BMS SOC": 44},
    )
    return site


async def new_flow(hass, **overrides):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(fid, payload(**overrides))
    return fid, result


async def detected_form(hass, **overrides):
    await save_prefs(hass, prefs_sources(hass_site_forecast(hass)))
    return await new_flow(hass, **overrides)


def hass_site_forecast(hass):
    return next(
        entry.entry_id for entry in hass.config_entries.async_entries("fx_forecast")
    )


def defaults(result):
    return {str(key): key.default() for key in result["data_schema"].schema}


def fields(result):
    return {str(key): value for key, value in result["data_schema"].schema.items()}


def options_of(result, row):
    return [
        (item["value"], item["label"]) for item in fields(result)[row].config["options"]
    ]


@pytest.fixture
def platform():
    with patch(PLATFORMS, AsyncMock(return_value={"fx_forecast": forecast_platform()})):
        yield


async def test_form_shows_checkboxes_for_one_option_and_selects_for_several(
    recorder_mock, hass, enable_custom_integrations, rich_site, platform
):
    fid, result = await detected_form(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "detected_sources"
    assert list(fields(result)) == [
        "pv",
        "sell",
        "load",
        "soc",
        "bms_soc",
        "battery_power",
        "grid_import_energy",
        "pv_energy_today",
        "grid_export_energy_today",
    ]
    assert defaults(result) == {
        "pv": True,
        "sell": "0",
        "load": True,
        "soc": "0",
        "bms_soc": False,
        "battery_power": True,
        "grid_import_energy": True,
        "pv_energy_today": True,
        "grid_export_energy_today": True,
    }
    flow = hass.config_entries.flow._progress[fid]
    offers = {offer.row: offer for offer in flow._detection.offers}
    assert options_of(result, "sell") == [
        ("0", option_label(offers["sell"].options[0], "en")),
        ("1", option_label(offers["sell"].options[1], "en")),
        ("skip", SKIP["en"]),
    ]
    assert options_of(result, "soc")[-1] == ("skip", "Do not bind")
    assert [kind for kind in (o.kind for o in offers["sell"].options)] == [
        "template_sell",
        "rce",
    ]
    placeholders = result["description_placeholders"]
    assert "- Sell price: Template — sensor.fx_ec_sell" in placeholders["detected"]
    assert "BMS SOC is offered unticked" in placeholders["notes"]
    await hass.async_stop()


async def test_quick_path_binds_defaults_and_opens_preview(
    recorder_mock, hass, enable_custom_integrations, rich_site, platform
):
    fid, result = await detected_form(hass, preset="pse", settlement="pl_net_billing")
    flow = hass.config_entries.flow._progress[fid]
    assert flow._draft["settings"]["sell_multiplier"] == 1.23
    flow._draft["settings"].update(
        allow_fallback=True,
        inverter_kw=10,
        grid_import_kw=10,
        grid_export_kw=10,
        capacity_kwh=10,
        charge_kw=5,
        discharge_kw=5,
    )
    result = await hass.config_entries.flow.async_configure(fid, defaults(result))
    assert result["step_id"] == "preview", result
    assert flow._draft["settings"]["sell_multiplier"] == 1
    assert "setup_profiles" in flow._draft
    result = await hass.config_entries.flow.async_configure(
        fid,
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    data = result["data"]
    assert data["setup_profiles"]["revision"] == 3
    assert data["settings"]["sell_multiplier"] == 1
    sources = data["sources"]
    assert sources["pv"]["arrays"] == []
    assert sources["pv"]["solar_forecasts"] == [
        {
            "config_entry_id": hass_site_forecast(hass),
            "domain": "fx_forecast",
        }
    ]
    assert sources["sell"]["forecast"][0]["entity"]["entity_id"] == "sensor.fx_ec_sell"
    assert sources["load"]["statistic_id"] == "sensor.fx_load"
    assert sources["soc"]["entity_id"] == "sensor.fx_soc"
    assert sources["bms_soc"] is None
    assert set(data["measurements"]) == {
        "battery_power",
        "grid_import_energy",
        "pv_energy_today",
        "grid_export_energy_today",
    }
    await hass.async_stop()


async def test_selecting_the_rce_option_restores_the_raw_rce_multiplier(
    recorder_mock, hass, enable_custom_integrations, rich_site, platform
):
    fid, result = await detected_form(hass, settlement="pl_net_billing")
    flow = hass.config_entries.flow._progress[fid]
    assert flow._draft["settings"]["sell_multiplier"] == 1
    result = await hass.config_entries.flow.async_configure(
        fid, {**defaults(result), "sell": "1"}
    )
    assert result["step_id"] == "preview"
    sell = flow._draft["sources"]["sell"]
    assert sell["floor_per_kwh"] == 0.0
    assert flow._draft["settings"]["sell_multiplier"] == 1.23
    await hass.async_stop()


async def test_skipping_soc_fails_the_preview_and_back_to_menu_leaves(
    recorder_mock, hass, enable_custom_integrations, rich_site, platform
):
    fid, result = await detected_form(hass)
    result = await hass.config_entries.flow.async_configure(
        fid, {**defaults(result), "soc": "skip"}
    )
    assert result["step_id"] == "preview"
    assert result["errors"] == {"base": "invalid_source"}
    assert "enabled battery needs SOC" in result["description_placeholders"]["preview"]
    result = await hass.config_entries.flow.async_configure(
        fid, {"back_to_menu": True, "confirm": True}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "menu"
    await hass.async_stop()


async def test_ticking_bms_soc_binds_the_attribute(
    recorder_mock, hass, enable_custom_integrations, rich_site, platform
):
    fid, result = await detected_form(hass)
    flow = hass.config_entries.flow._progress[fid]
    await hass.config_entries.flow.async_configure(
        fid, {**defaults(result), "bms_soc": True}
    )
    bms = flow._draft["sources"]["bms_soc"]
    assert (bms["entity_id"], bms["attribute"]) == (
        "sensor.fx_inverter_soc",
        "BMS SOC",
    )
    await hass.async_stop()


async def test_invalid_values_reshow_the_form(
    recorder_mock, hass, enable_custom_integrations, rich_site, platform
):
    fid, _ = await detected_form(hass)
    flow = hass.config_entries.flow._progress[fid]
    for bad in ({"sell": "9"}, {"sell": True}, {"pv": "0"}, {"soc": 3}):
        values = {"pv": True, "sell": "0", "soc": "0", **bad}
        result = await flow.async_step_detected_sources(values)
        assert result["step_id"] == "detected_sources"
        assert result["errors"] == {"base": "invalid_input"}
    assert flow._draft["sources"]["pv"] == {"enabled": True, "arrays": []}
    await hass.async_stop()


async def test_nothing_chosen_goes_to_the_menu(
    recorder_mock, hass, enable_custom_integrations, rich_site, platform
):
    fid, result = await detected_form(hass)
    values = {
        key: ("skip" if isinstance(value, str) else False)
        for key, value in defaults(result).items()
    }
    result = await hass.config_entries.flow.async_configure(fid, values)
    assert result["type"] is FlowResultType.MENU
    flow = hass.config_entries.flow._progress[fid]
    assert flow._draft["sources"]["pv"] == {"enabled": True, "arrays": []}
    await hass.async_stop()


async def test_nothing_found_goes_to_the_menu(
    recorder_mock, hass, enable_custom_integrations
):
    _, result = await new_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "menu"


async def test_adapter_failure_continues_to_the_menu_with_one_debug_record(
    recorder_mock, hass, enable_custom_integrations, caplog
):
    caplog.set_level(logging.DEBUG)
    with patch.object(
        config_flow,
        "async_detection_snapshot",
        AsyncMock(side_effect=RuntimeError("x")),
    ):
        _, result = await new_flow(hass)
    assert result["type"] is FlowResultType.MENU
    records = [r for r in caplog.records if r.name == config_flow.__name__]
    assert [r.levelno for r in records] == [logging.DEBUG]
    assert records[0].getMessage() == (
        "Source detection failed; continuing without detected sources"
    )
    assert records[0].exc_info is not None
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "detect" in r.getMessage().lower()
    ]


async def test_installation_error_reshows_user_and_a_valid_submit_detects(
    recorder_mock, hass, enable_custom_integrations, rich_site, platform
):
    await save_prefs(hass, prefs_sources(hass_site_forecast(hass)))
    with patch.object(
        config_flow,
        "async_detection_snapshot",
        wraps=config_flow.async_detection_snapshot,
    ) as spy:
        result = await hass.config_entries.flow.async_init(
            "energy_compass", context={"source": config_entries.SOURCE_USER}
        )
        fid = result["flow_id"]
        result = await hass.config_entries.flow.async_configure(
            fid, payload(timezone="Nowhere/Land")
        )
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "invalid_input"}
        assert not spy.called
        result = await hass.config_entries.flow.async_configure(fid, payload())
        assert result["step_id"] == "detected_sources"
        assert spy.call_count == 1
    await hass.async_stop()


async def test_buy_schedule_and_bms_are_untouched(
    recorder_mock, hass, enable_custom_integrations, rich_site, platform
):
    fid, result = await detected_form(
        hass, settlement="pl_net_billing", buy_tariff="pge_g12"
    )
    flow = hass.config_entries.flow._progress[fid]
    before = flow._draft["sources"]["buy"]
    await hass.config_entries.flow.async_configure(fid, defaults(result))
    assert flow._draft["sources"]["buy"] == before
    assert before["mode"] == "schedule"
    assert flow._draft["sources"]["bms_soc"] is None
    await hass.async_stop()


@pytest.mark.parametrize("kind", ["options", "reconfigure"])
async def test_existing_installations_never_detect(
    recorder_mock, hass, enable_custom_integrations, rich_site, platform, kind
):
    entry = MockConfigEntry(
        domain="energy_compass",
        data=default_configuration("PLN", "Europe/Warsaw"),
        version=3,
    )
    entry.add_to_hass(hass)
    with (
        patch.object(config_flow, "async_detection_snapshot", AsyncMock()) as snap,
        patch.object(config_flow, "detect") as detect_spy,
        patch.object(config_flow, "apply_detection") as apply_spy,
    ):
        if kind == "options":
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
        fid = result["flow_id"]
        form = await manager.async_configure(fid, {"next_step_id": "installation"})
        values = form["data_schema"]({})
        result = await manager.async_configure(fid, values)
        assert result["type"] is FlowResultType.MENU
        assert result["step_id"] == "menu"
        for spy in (snap, detect_spy, apply_spy):
            assert not spy.called


@pytest.mark.parametrize("kind", ["config", "options", "reconfigure"])
async def test_preview_back_to_menu_saves_nothing(
    recorder_mock, hass, enable_custom_integrations, kind
):
    config = default_configuration("EUR", "UTC")
    if kind == "config":
        result = await hass.config_entries.flow.async_init(
            "energy_compass", context={"source": config_entries.SOURCE_USER}
        )
        manager = hass.config_entries.flow
        result = await manager.async_configure(
            result["flow_id"],
            payload(
                currency="EUR", timezone="UTC", pv_enabled=False, battery_enabled=False
            ),
        )
    else:
        entry = MockConfigEntry(domain="energy_compass", data=config, version=3)
        entry.add_to_hass(hass)
        if kind == "options":
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
    fid = result["flow_id"]
    preview = await manager.async_configure(fid, {"next_step_id": "preview"})
    assert preview["step_id"] == "preview"
    assert "back_to_menu" in {str(key) for key in preview["data_schema"].schema}
    values = {"confirm": True, "back_to_menu": True}
    if kind == "config":
        values.update(confirm_buy_source=True, confirm_load_source=True)
    result = await manager.async_configure(fid, values)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "menu"
    assert not hass.config_entries.async_entries("energy_compass") or kind != "config"


def test_detected_sources_strings_are_exact():
    for name, language in (
        ("strings.json", "en"),
        ("translations/en.json", "en"),
        ("translations/pl.json", "pl"),
    ):
        data = json.loads((COMPONENT / name).read_text())
        step = data["config"]["step"]["detected_sources"]
        assert step["title"] == (
            "Detected sources" if language == "en" else "Wykryte źródła"
        )
        assert step["description"].endswith("\n\n{detected}\n\n{notes}")
        assert step["description"].startswith(
            "Energy Compass found these sources by integration identity"
            if language == "en"
            else "Energy Compass znalazł te źródła po tożsamości integracji"
        )
        assert step["data"] == {row: row_label(row, language) for row in ROWS}
        for flow in ("config", "options"):
            assert data[flow]["step"]["preview"]["data"]["back_to_menu"] == (
                "Back to the main menu (nothing is saved)"
                if language == "en"
                else "Wróć do menu głównego (nic nie zostanie zapisane)"
            )
