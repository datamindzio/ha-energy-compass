"""Daily export counters and local calendar boundaries."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_compass.config_models import NumericSetting
from custom_components.energy_compass.engine.daily_energy import day_fractions
from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.runtime import (
    build_problem,
    compute,
    freshness_deadline,
)
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import EntityBinding


def daily_config(now):
    config = default_configuration("PLN", "Europe/Warsaw")
    config["settings"].update(
        grid_export_kw=8,
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
    )
    states = {}
    for key, value in (("pv_energy_today", 8), ("grid_export_energy_today", 6)):
        entity_id = f"sensor.{key}"
        config["measurements"][key] = NumericSetting(
            entity=EntityBinding(entity_id), unit="kWh", max_age_seconds=86400
        ).to_dict()
        states[entity_id] = {
            "state": str(value),
            "attributes": {"unit_of_measurement": "kWh"},
            "last_updated": now,
            "last_reported": now,
        }
    return config, states


def test_today_counters_reach_optimizer_and_plan():
    now = datetime(2026, 9, 18, 12, tzinfo=UTC)
    config, states = daily_config(now)
    source, _, _ = build_problem(config, states, now)
    assert source.pv_generated_today_kwh == 8
    assert source.grid_exported_today_kwh == 6
    result = compute(config, states, now)
    policy = result["dispatch_policy"]
    assert policy["export_limit_scope"] == "local_day"
    assert policy["daily_balances"][0]["remaining_export_kwh"] == pytest.approx(2)
    states["sensor.grid_export_energy_today"]["state"] = "7"
    source, _, _ = build_problem(config, states, now + timedelta(minutes=15))
    assert source.grid_exported_today_kwh == 7


@pytest.mark.parametrize(
    "fault", ["missing", "unavailable", "yesterday", "negative", "future", "wrong_unit"]
)
def test_missing_or_invalid_daily_counter_blocks_daily_advice(fault):
    now = datetime(2026, 9, 18, 12, tzinfo=UTC)
    config, states = daily_config(now)
    row = states["sensor.grid_export_energy_today"]
    if fault == "missing":
        del config["measurements"]["grid_export_energy_today"]
    elif fault == "unavailable":
        row["state"] = "unavailable"
    elif fault == "yesterday":
        row["last_updated"] = row["last_reported"] = now - timedelta(days=1)
    elif fault == "negative":
        row["state"] = "-1"
    elif fault == "future":
        row["last_reported"] = now + timedelta(minutes=5)
    else:
        row["attributes"]["unit_of_measurement"] = "W"
    with pytest.raises(InputError):
        build_problem(config, states, now)


def test_disabling_sell_only_pv_removes_counter_requirement():
    now = datetime(2026, 9, 18, 12, tzinfo=UTC)
    config, _ = daily_config(now)
    config["measurements"] = {}
    config["settings"]["limit_export_to_pv"] = False
    assert build_problem(config, {}, now)[0].limit_export_to_pv is False


def test_zero_export_capability_does_not_require_daily_counters():
    config = default_configuration("PLN", "Europe/Warsaw")
    assert (
        build_problem(config, {}, datetime(2026, 9, 18, 12, tzinfo=UTC))[
            0
        ].site.grid_export_kw
        == 0
    )


@pytest.mark.parametrize(
    "date,hours",
    [(datetime(2026, 3, 29, tzinfo=UTC), 23), (datetime(2026, 10, 25, tzinfo=UTC), 25)],
)
def test_dst_days_use_elapsed_hours(date, hours):
    zone = ZoneInfo("Europe/Warsaw")
    start = date.replace(tzinfo=zone)
    end = (date + timedelta(days=1)).replace(tzinfo=zone)
    assert (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600 == hours
    assert day_fractions(start, end, zone) == {date.date().isoformat(): 1}


def test_cross_midnight_slot_is_split_by_local_day():
    zone = ZoneInfo("Europe/Warsaw")
    start = datetime(2026, 9, 18, 21, 30, tzinfo=UTC)
    assert day_fractions(start, start + timedelta(hours=2), zone) == {
        "2026-09-18": 0.25,
        "2026-09-19": 0.75,
    }


def test_validity_expires_at_local_midnight():
    now = datetime(2026, 9, 18, 21, 55, tzinfo=UTC)
    config, states = daily_config(now)
    _, values, _ = build_problem(config, states, now)
    assert freshness_deadline(config, states, values, now) == datetime(
        2026, 9, 18, 22, tzinfo=UTC
    )


def test_stable_counter_uses_a_fresh_native_report():
    now = datetime(2026, 9, 18, 12, tzinfo=UTC)
    config, states = daily_config(now)
    for row in states.values():
        row["last_updated"] = now - timedelta(days=2)
    result = compute(config, states, now)
    assert result["valid"]
    assert result["measurements"]["grid_export_energy_today"]["status"] == "available"


async def test_daily_sources_can_be_selected_and_saved_in_options(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.energy_compass.settings import merged_configuration

    freezer.move_to("2026-09-18T12:00:00+00:00")
    config = default_configuration("PLN", "Europe/Warsaw")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "sources"})
    for name, value in (("pv_energy_today", 8), ("grid_export_energy_today", 6)):
        hass.states.async_set(f"sensor.{name}", value, {"unit_of_measurement": "kWh"})
        await hass.config_entries.options.async_configure(
            fid, {"next_step_id": "source_add"}
        )
        mode = await hass.config_entries.options.async_configure(fid, {"target": name})
        assert mode["step_id"] == "source_mode"
        result = await hass.config_entries.options.async_configure(
            fid, {"mode": "measurement"}
        )
        assert result["step_id"] == "source_entity"
        await hass.config_entries.options.async_configure(
            fid, {"entity_id": f"sensor.{name}"}
        )
        result = await hass.config_entries.options.async_configure(fid, {})
        fields = {str(key): key for key in result["data_schema"].schema}
        assert fields["unit"].default() == "kWh"
        assert fields["max_age_seconds"].default() == 86400
        result = await hass.config_entries.options.async_configure(
            fid,
            {
                "unit": "kWh",
                "sign": 1,
                "timestamp_path": "last_updated",
                "max_age_seconds": 86400,
            },
        )
        assert result["step_id"] == "sources"
        flow = hass.config_entries.options._progress[fid]
        assert flow._draft["measurements"][name]["minimum"] == 0
    inventory = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_inventory"}
    )
    choices = next(
        field.config["options"]
        for key, field in inventory["data_schema"].schema.items()
        if str(key) == "source"
    )
    pv_source = next(
        choice["value"]
        for choice in choices
        if "sensor.pv_energy_today" in choice["label"]
    )
    await hass.config_entries.options.async_configure(fid, {"source": pv_source})
    await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_edit"}
    )
    await hass.config_entries.options.async_configure(
        fid, {"entity_id": "sensor.pv_energy_today"}
    )
    edit = await hass.config_entries.options.async_configure(fid, {})
    fields = {str(key): key for key in edit["data_schema"].schema}
    assert fields["max_age_seconds"].default() == 86400
    await hass.config_entries.options.async_configure(
        fid,
        {
            "unit": "kWh",
            "sign": 1,
            "timestamp_path": "last_updated",
            "max_age_seconds": 86400,
        },
    )
    assert set(flow._draft["measurements"]) == {
        "pv_energy_today",
        "grid_export_energy_today",
    }
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "menu"})
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "hardware"})
    await hass.config_entries.options.async_configure(fid, {"grid_export_kw": 8})
    result = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "preview"}
    )
    assert result["step_id"] == "preview" and not result.get("errors")
    result = await hass.config_entries.options.async_configure(fid, {"confirm": True})
    assert result["type"] == "create_entry"
    saved = merged_configuration(entry)
    assert (
        saved["measurements"]["grid_export_energy_today"]["entity"]["entity_id"]
        == "sensor.grid_export_energy_today"
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.data["valid"]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "name, label",
    [
        ("pv_energy_today", "Dzienna energia PV"),
        ("grid_export_energy_today", "Dzienna energia eksportu do sieci"),
    ],
)
async def test_daily_counter_roles_reject_forged_statistic_mode_and_submission(
    recorder_mock, hass, enable_custom_integrations, name, label
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    config = default_configuration("PLN", "Europe/Warsaw")
    hass.config.language = "pl"
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "sources"})
    selection = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "source_add"}
    )
    roles = next(
        field.config["options"]
        for key, field in selection["data_schema"].schema.items()
        if str(key) == "target"
    )
    assert {choice["value"]: choice["label"] for choice in roles}[name] == label
    mode = await hass.config_entries.options.async_configure(fid, {"target": name})
    choices = next(
        field.config["options"]
        for key, field in mode["data_schema"].schema.items()
        if str(key) == "mode"
    )
    assert [choice["value"] for choice in choices] == ["measurement", "back"]
    flow = hass.config_entries.options._progress[fid]
    rejected = await flow.async_step_source_mode({"mode": "statistic"})
    assert rejected["errors"] == {"base": "invalid_input"}
    rejected = await flow.async_step_source_statistic(
        {"statistic_id": f"sensor.{name}", "unit": "kWh", "sign": 1}
    )
    assert rejected["errors"] == {"base": "invalid_input"}
    assert flow._draft == config
    assert dict(entry.data) == config


async def test_counter_change_recalculates_without_resetting_daily_budget(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    freezer.move_to("2026-09-18T12:00:00+00:00")
    config, states = daily_config(datetime(2026, 9, 18, 12, tzinfo=UTC))
    config["settings"]["debounce_seconds"] = 0
    for entity_id, row in states.items():
        hass.states.async_set(entity_id, row["state"], row["attributes"])
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    assert (
        coordinator.data["dispatch_policy"]["daily_balances"][0]["remaining_export_kwh"]
        == 2
    )
    hass.states.async_set(
        "sensor.grid_export_energy_today", "7", {"unit_of_measurement": "kWh"}
    )
    await hass.async_block_till_done()
    assert coordinator.data["valid"]
    assert (
        coordinator.data["dispatch_policy"]["daily_balances"][0]["remaining_export_kwh"]
        == 1
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


MIDNIGHT = datetime(2026, 9, 18, 22, tzinfo=UTC)  # 00:00 Europe/Warsaw


def _counters(states, pv, export, stamp):
    for key, value in (("pv_energy_today", pv), ("grid_export_energy_today", export)):
        row = states[f"sensor.{key}"]
        row["state"] = str(value)
        row["last_updated"] = row["last_reported"] = stamp


@pytest.mark.parametrize("stamp_offset", [timedelta(minutes=-3), timedelta(minutes=2)])
def test_counters_not_yet_reset_after_midnight_use_conservative_values(stamp_offset):
    """Yesterday's totals, reported before midnight or on the inverter's lagging
    clock after it, never become today's budget and never block the plan."""
    now = MIDNIGHT + timedelta(minutes=5)
    config, states = daily_config(now)
    _counters(states, 16, 6, MIDNIGHT + stamp_offset)
    source, values, _ = build_problem(config, states, now)
    assert source.pv_generated_today_kwh == 0
    assert source.grid_exported_today_kwh == pytest.approx(8 * 5 / 60)
    assert freshness_deadline(config, states, values, now) == MIDNIGHT + timedelta(
        minutes=30
    )
    assert compute(config, states, now)["valid"]


def test_reset_counters_after_midnight_are_used_as_reported():
    now = MIDNIGHT + timedelta(minutes=10)
    config, states = daily_config(now)
    _counters(states, 0, 0.1, MIDNIGHT + timedelta(minutes=9))
    source, _, _ = build_problem(config, states, now)
    assert source.pv_generated_today_kwh == 0
    assert source.grid_exported_today_kwh == pytest.approx(0.1)


@pytest.mark.parametrize(
    "stamp_offset,message",
    [
        (timedelta(minutes=-3), "must have a report from the current local day"),
        (timedelta(minutes=35), "did not reset for the current local day"),
    ],
)
def test_counters_still_not_reset_after_the_grace_block_advice(stamp_offset, message):
    now = MIDNIGHT + timedelta(minutes=40)
    config, states = daily_config(now)
    _counters(states, 16, 6, MIDNIGHT + stamp_offset)
    with pytest.raises(InputError, match=message):
        build_problem(config, states, now)


def test_daytime_counters_are_not_mistaken_for_yesterday():
    now = datetime(2026, 9, 18, 10, tzinfo=UTC)  # 12:00 local
    config, states = daily_config(now)
    _counters(states, 30, 20, now)
    source, _, _ = build_problem(config, states, now)
    assert source.pv_generated_today_kwh == 30
    assert source.grid_exported_today_kwh == 20


def test_export_already_above_todays_pv_forbids_more_instead_of_failing():
    """PV that fell short of its forecast can leave observed export above
    observed PV plus the rest of the day's forecast; the plan must still solve."""
    now = datetime(2026, 9, 18, 16, tzinfo=UTC)
    config, states = daily_config(now)
    _counters(states, 5, 7, now)
    result = compute(config, states, now)
    assert result["valid"]
    assert all(row["grid_export_kwh"] <= 1e-6 for row in result["intervals"])
