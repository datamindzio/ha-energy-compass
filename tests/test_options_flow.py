from copy import deepcopy
from datetime import UTC, datetime

import pytest
from homeassistant import config_entries
from homeassistant.helpers import selector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.settings import (
    default_configuration,
    merged_configuration,
    validate_configuration,
)
from custom_components.energy_compass.sources.bindings import EntityBinding


async def test_options_preserve_invalid_edit(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Synthetic", version=2
    )
    entry.add_to_hass(hass)
    before = deepcopy(dict(entry.data))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "battery"}
    )
    assert result["step_id"] == "battery"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"operating_floor": 90, "soc_ceiling": 80}
    )
    assert result["errors"]
    assert entry.data == before


async def test_battery_cycle_limit_requires_daily_measurement_before_draft_acceptance(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    config["sources"]["battery_enabled"] = True
    config["sources"]["soc"] = EntityBinding("sensor.soc").to_dict()
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Cycles", version=2
    )
    entry.add_to_hass(hass)
    before = deepcopy(dict(entry.data))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "battery"})

    rejected = await hass.config_entries.options.async_configure(
        fid, {"daily_cycles": 1}
    )

    assert rejected["step_id"] == "battery"
    assert rejected["errors"] == {"base": "invalid_source"}
    assert "Sources" in rejected["description_placeholders"]["detail"]
    assert entry.data == before


async def test_preview_shows_measured_daily_throughput_cap_and_remaining(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["sources"].update(
        battery_enabled=True,
        soc=EntityBinding("sensor.soc").to_dict(),
    )
    config["settings"].update(
        capacity_kwh=20,
        daily_cycles=1,
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
    )
    config["measurements"]["throughput_today"] = {
        "entity": EntityBinding("sensor.daily_throughput", attribute="total").to_dict(),
        "unit": "kWh",
        "source_unit": "kWh",
        "multiplier": 1,
        "minimum": 0,
        "max_age_seconds": 3600,
    }
    hass.states.async_set("sensor.soc", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.daily_throughput", "ready", {"total": 4})
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "preview"}
    )
    text = result["description_placeholders"]["preview"]
    assert "sensor.daily_throughput" in text
    assert "attribute total" in text
    assert "4 kWh" in text
    assert "40 kWh" in text
    assert "36 kWh" in text


async def test_notification_form_offers_only_dispatchable_events(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    config["settings"]["notify_events"] = ["favorable", "invalid"]
    config["settings"]["notify_actions"] = [{"event": "legacy_test"}]
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Synthetic", version=2
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "notifications"}
    )
    fields = {
        str(key): (key, value) for key, value in result["data_schema"].schema.items()
    }
    assert "notify_actions" not in fields
    events_marker, events_selector = fields["notify_events"]
    assert isinstance(events_selector, selector.SelectSelector)
    assert events_selector.config["options"] == ["favorable", "limit"]
    assert events_marker.default() == ["favorable"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"notify_enabled": True, "notify_events": ["limit"]}
    )
    assert result["step_id"] == "menu"
    assert entry.data["settings"]["notify_actions"] == [{"event": "legacy_test"}]


def test_helper_unavailable_and_disabled_age():
    config = default_configuration("EUR", "UTC")
    config["helpers"] = {
        "grid_import_kw": {
            "fixed": None,
            "entity": {"entity_id": "input_number.limit"},
            "unit": "kW",
            "max_age_seconds": None,
        }
    }
    state = {
        "input_number.limit": {
            "state": "5",
            "attributes": {"unit_of_measurement": "kW"},
            "last_updated": datetime(2020, 1, 1, tzinfo=UTC),
        }
    }
    assert (
        validate_configuration(config, state, datetime.now(UTC))["grid_import_kw"] == 5
    )
    state["input_number.limit"]["state"] = "unavailable"
    with pytest.raises(InputError):
        validate_configuration(config, state, datetime.now(UTC))


@pytest.mark.parametrize(
    "unit,source_unit", [("PLN/kWh", "PLN/kWh"), ("EUR/kWh", "PLN/kWh")]
)
def test_economic_helpers_cannot_silently_convert_currency(unit, source_unit):
    config = default_configuration("EUR", "UTC")
    now = datetime.now(UTC)
    config["helpers"]["buy_rate"] = {
        "entity": {"entity_id": "sensor.rate"},
        "unit": unit,
        "source_unit": source_unit,
        "max_age_seconds": None,
    }
    states = {
        "sensor.rate": {
            "state": ".4",
            "attributes": {"unit_of_measurement": source_unit},
            "last_updated": now,
        }
    }
    with pytest.raises(InputError, match="currency"):
        validate_configuration(config, states, now)


async def test_options_commit_reloads_and_preserves_sources(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Options", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    before = entry.runtime_data
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "tariffs"})
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "tariff_values"}
    )
    await hass.config_entries.options.async_configure(fid, {"buy_rate": 0.4})
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "menu"})
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "preview"}
    )
    assert "import 0.4" in result["description_placeholders"]["preview"]
    result = await hass.config_entries.options.async_configure(fid, {"confirm": True})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data is not before
    assert entry.runtime_data.configuration["sources"] == config["sources"]
    assert float(
        hass.states.get("sensor.options_consumption_cost").state
    ) == pytest.approx(0.4)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_commits_atomically(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from homeassistant import config_entries

    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Reconfigure", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    before = entry.runtime_data
    result = await hass.config_entries.flow.async_init(
        "energy_compass",
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "tariffs"})
    await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "tariff_values"}
    )
    await hass.config_entries.flow.async_configure(fid, {"buy_rate": 0.7})
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "menu"})
    assert entry.data["settings"]["buy_rate"] == 0
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "preview"})
    result = await hass.config_entries.flow.async_configure(fid, {"confirm": True})
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data["settings"]["buy_rate"] == 0.7
    assert entry.runtime_data is not before
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_installation_name_updates_entry_title(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["name"] = "Original"
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Original", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "installation"}
    )
    await hass.config_entries.options.async_configure(
        fid,
        {
            "name": "Renamed installation",
            "currency": "EUR",
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "preview"})
    await hass.config_entries.options.async_configure(fid, {"confirm": True})
    await hass.async_block_till_done()
    assert entry.title == "Renamed installation"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_presentation_options_update_existing_entities(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from homeassistant.helpers import entity_registry as er

    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = default_configuration("EUR", "UTC")
    config["name"] = "Visibility"
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Visibility", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    registry = er.async_get(hass)
    registry.async_update_entity(
        "sensor.visibility_expected_net_cost", disabled_by=er.RegistryEntryDisabler.USER
    )
    for enabled in (False, True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        fid = result["flow_id"]
        await hass.config_entries.options.async_configure(
            fid, {"next_step_id": "presentation"}
        )
        await hass.config_entries.options.async_configure(
            fid, {"expose_costs": enabled, "expose_windows": enabled}
        )
        await hass.config_entries.options.async_configure(
            fid, {"next_step_id": "preview"}
        )
        await hass.config_entries.options.async_configure(fid, {"confirm": True})
        await hass.async_block_till_done()
        for key in (
            "consumption_cost",
            "expected_wear_cost",
            "next_boost_start",
            "next_cheap_start",
            "next_limit_start",
            "next_change",
        ):
            assert registry.async_get(f"sensor.visibility_{key}").disabled_by == (
                None if enabled else er.RegistryEntryDisabler.INTEGRATION
            )
        assert (
            registry.async_get("sensor.visibility_expected_net_cost").disabled_by
            == er.RegistryEntryDisabler.USER
        )
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_attribute_helper_uses_explicit_unit_not_parent_state_unit():
    config = default_configuration("EUR", "UTC")
    now = datetime.now(UTC)
    config["helpers"]["grid_import_kw"] = {
        "entity": {"entity_id": "sensor.controller", "attribute": "limit"},
        "unit": "kW",
        "source_unit": "kW",
        "max_age_seconds": None,
    }
    states = {
        "sensor.controller": {
            "state": "5",
            "attributes": {"unit_of_measurement": "kWh", "limit": 8},
            "last_updated": now,
        }
    }
    assert validate_configuration(config, states, now)["grid_import_kw"] == 8
    config["helpers"]["grid_import_kw"]["entity"]["attribute"] = None
    with pytest.raises(InputError, match="unit"):
        validate_configuration(config, states, now)


@pytest.mark.parametrize("mode", ["options", "reconfigure"])
async def test_soc_exact_timestamp_policy_survives_existing_flow_save(
    recorder_mock, hass, enable_custom_integrations, mode
):
    config = default_configuration("EUR", "UTC")
    config["sources"].update(battery_enabled=True, soc={"entity_id": "sensor.soc"})
    config["soc_options"].update(
        timestamp_path="attributes.reported_at", timestamp_policy="exact_path"
    )
    hass.states.async_set(
        "sensor.soc", "50", {"reported_at": datetime.now(UTC).isoformat()}
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="SOC", version=2
    )
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
    fid = result["flow_id"]
    await manager.async_configure(fid, {"next_step_id": "sources"})
    await manager.async_configure(fid, {"next_step_id": "source_add"})
    await manager.async_configure(fid, {"target": "soc"})
    await manager.async_configure(fid, {"mode": "measurement"})
    await manager.async_configure(fid, {"entity_id": "sensor.soc"})
    form = await manager.async_configure(fid, {})
    defaults = {str(key): key.default() for key in form["data_schema"].schema}
    assert defaults["timestamp_path"] == "attributes.reported_at"
    assert defaults["timestamp_policy"] == "exact_path"
    await manager.async_configure(
        fid,
        {
            "unit": "%",
            "sign": 1,
            "timestamp_path": "attributes.reported_at",
            "max_age_seconds": 600,
        },
    )
    await manager.async_configure(fid, {"next_step_id": "menu"})
    form = await manager.async_configure(fid, {"next_step_id": "preview"})
    assert form["step_id"] == "preview"
    assert not form["errors"]
    result = await manager.async_configure(fid, {"confirm": True})
    assert result["type"] == ("create_entry" if mode == "options" else "abort")
    saved = merged_configuration(entry)
    assert saved["soc_options"]["timestamp_path"] == "attributes.reported_at"
    assert saved["soc_options"]["timestamp_policy"] == "exact_path"


@pytest.mark.parametrize("mode", ["options", "reconfigure"])
async def test_infeasible_existing_edit_preserves_entry_and_runtime(
    recorder_mock, hass, enable_custom_integrations, mode
):
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Original", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    runtime = entry.runtime_data
    before_data = deepcopy(dict(entry.data))
    before_options = deepcopy(dict(entry.options))
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
    fid = result["flow_id"]
    await manager.async_configure(fid, {"next_step_id": "installation"})
    await manager.async_configure(
        fid,
        {
            "name": "Rejected",
            "currency": "EUR",
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    await manager.async_configure(fid, {"next_step_id": "hardware"})
    await manager.async_configure(fid, {"grid_import_kw": 0})
    result = await manager.async_configure(fid, {"next_step_id": "preview"})
    assert result["errors"] == {"base": "plan_infeasible"}
    result = await manager.async_configure(fid, {"confirm": True})
    assert result["errors"] == {"base": "plan_infeasible"}
    assert entry.data == before_data
    assert entry.options == before_options
    assert entry.title == "Original"
    assert entry.runtime_data is runtime
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_preview_rechecks_live_helper_at_existing_entry_submit(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    config["helpers"]["grid_import_kw"] = {
        "entity": {"entity_id": "input_number.limit"},
        "unit": "kW",
        "max_age_seconds": None,
    }
    hass.states.async_set("input_number.limit", "10")
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Helper", version=2
    )
    entry.add_to_hass(hass)
    before = deepcopy(dict(entry.data))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    preview = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "preview"}
    )
    assert "Base plan: feasible" in preview["description_placeholders"]["preview"]
    hass.states.async_set("input_number.limit", "0")
    rejected = await hass.config_entries.options.async_configure(fid, {"confirm": True})
    assert rejected["errors"] == {"base": "plan_infeasible"}
    assert entry.data == before
    assert not entry.options


async def test_soc_measurement_submission_keeps_exact_policy_when_field_omitted(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    config["soc_options"].update(
        timestamp_path="last_updated", timestamp_policy="exact_path"
    )
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow = hass.config_entries.options._progress[result["flow_id"]]
    flow._source = {"target": "soc"}
    flow._binding = EntityBinding("sensor.soc")
    await flow.async_step_source_measurement(
        {
            "unit": "%",
            "sign": 1,
            "timestamp_path": "last_updated",
            "max_age_seconds": 600,
        }
    )
    assert flow._draft["soc_options"]["timestamp_policy"] == "exact_path"
