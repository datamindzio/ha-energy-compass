from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.config_models import PvSource
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)

START = "2026-09-24T10:00:00+00:00"
FIRST = {"unit_of_measurement": "kWh"}
DUE = {
    "last_completed_at": "2026-09-15T10:00:00+00:00",
    "hold_started_at": None,
    "last_full_at": None,
}


def _binding(entity, **extra):
    return IntervalBinding(
        EntityBinding(entity, attribute="rows"),
        start_path="start",
        end_path="end",
        value_path="value",
        unit="kWh",
        **extra,
    )


def _config():
    config = default_configuration("EUR", "UTC")
    config["sources"].update(
        battery_enabled=True, soc=EntityBinding("sensor.soc").to_dict()
    )
    config["sources"]["pv"] = PvSource(
        True,
        ((_binding("sensor.pv"),),),
        pessimistic=(_binding("sensor.pv_low", max_age_seconds=3600),),
    ).to_dict()
    config["settings"].update(
        capacity_kwh=20,
        operating_floor=15,
        hardware_floor=10,
        soc_ceiling=100,
        inverter_kw=5,
        grid_import_kw=7,
        grid_export_kw=2,
        horizon_hours=4,
        display_horizon_hours=4,
        reference_horizon_hours=4,
        eta_charge=0.9,
        eta_discharge=0.8,
        wear_per_kwh=0.04,
        allow_grid_charge=True,
        allow_battery_export=True,
        limit_export_to_pv=False,
        lfp_balance=True,
        balance_value=50.0,
        minimum_mode_minutes=15,
    )
    return config


def _rows(value, field="value"):
    return [
        {
            "start": f"2026-09-24T{10 + i:02d}:00:00+00:00",
            "end": f"2026-09-24T{11 + i:02d}:00:00+00:00",
            field: value,
        }
        for i in range(8)
    ]


def _publish(hass, *, low=1.0, field="value"):
    hass.states.async_set("sensor.soc", "95", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.pv", "ok", {"rows": _rows(1.0)})
    if low is not None:
        hass.states.async_set("sensor.pv_low", "ok", {"rows": _rows(low, field)})


async def _setup(hass, hass_storage):
    entry = MockConfigEntry(
        domain="energy_compass", data=_config(), title="Home", version=3
    )
    entry.add_to_hass(hass)
    key = f"energy_compass.{entry.entry_id}.balance"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": DUE}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def _assert_median_fallback(coordinator):
    assert coordinator.data["valid"], coordinator.data.get("reason")
    assert coordinator.data["status"] != "invalid_input"
    assert coordinator.data["balance_pv_series"] == "median"
    assert (
        "balance_pessimistic_pv_unavailable" in coordinator.data["quality"]["warnings"]
    )
    assert coordinator.balance_report()["pv_series"] == "median"


async def test_pessimistic_series_is_published_on_both_sensors(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _publish(hass)
    entry = await _setup(hass, hass_storage)
    coordinator = entry.runtime_data
    assert coordinator.data["valid"], coordinator.data.get("reason")
    assert coordinator.balance_report()["pv_series"] == "pessimistic"
    balance = hass.states.get("sensor.home_battery_balance")
    assert balance.attributes["pv_series"] == "pessimistic"
    plan = hass.states.get("sensor.home_plan")
    assert plan.attributes["balance_pv_series"] == "pessimistic"
    assert "balance_pessimistic_pv_unavailable" not in plan.attributes["reasons"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_stale_pessimistic_source_does_not_invalidate_the_plan(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _publish(hass)
    entry = await _setup(hass, hass_storage)
    coordinator = entry.runtime_data
    assert coordinator.balance_report()["pv_series"] == "pessimistic"
    # The pessimistic source ages past its 1 h limit; the median source and SOC
    # stay current.
    freezer.tick(timedelta(hours=2))
    hass.states.async_set(
        "sensor.soc", "95", {"unit_of_measurement": "%"}, force_update=True
    )
    hass.states.async_set("sensor.pv", "ok", {"rows": _rows(1.0)}, force_update=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    # The inputs stay valid: no expired_inputs, no invalid_input.
    coordinator._inputs()
    assert coordinator.data["valid"], coordinator.data.get("reason")
    assert coordinator.data["status"] != "invalid_input"
    await coordinator.async_recalculate()
    _assert_median_fallback(coordinator)
    assert hass.states.get("sensor.home_plan").state != "invalid_input"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_pessimistic_rows_without_the_value_field_keep_the_plan_valid(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _publish(hass, field="wrong")
    entry = await _setup(hass, hass_storage)
    _assert_median_fallback(entry.runtime_data)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_pessimistic_source_going_bad_changes_the_fingerprint(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _publish(hass)
    entry = await _setup(hass, hass_storage)
    coordinator = entry.runtime_data

    def content():
        _, states, _, values = coordinator._inputs()
        return coordinator._content(states, values)

    healthy = content()
    hass.states.async_set("sensor.pv_low", "ok", {"rows": _rows(0.4)})
    changed = content()
    hass.states.async_set("sensor.pv_low", "ok", {"rows": _rows(0.4, "wrong")})
    malformed = content()
    hass.states.async_set("sensor.pv_low", "ok", {"rows": _rows(0.4)})
    assert content() == changed
    assert len({healthy, changed, malformed}) == 3
    assert await hass.config_entries.async_unload(entry.entry_id)


async def _setup_unavailable(hass, hass_storage, *, lfp_balance):
    config = _config()
    config["settings"]["lfp_balance"] = lfp_balance
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Home", version=3
    )
    entry.add_to_hass(hass)
    key = f"energy_compass.{entry.entry_id}.balance"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": DUE}
    hass.states.async_set("sensor.pv_low", "unavailable", {"rows": _rows(1.0)})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def test_unavailable_pessimistic_entity_with_balance_only_warns_its_own_code(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _publish(hass, low=None)
    entry = await _setup_unavailable(hass, hass_storage, lfp_balance=True)
    data = entry.runtime_data.data
    assert data["valid"], data.get("reason")
    warnings = data["quality"]["warnings"]
    assert "balance_pessimistic_pv_unavailable" in warnings
    assert "missing_forecast_continuation" not in warnings
    assert data["quality"]["missing_sources"] == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unavailable_pessimistic_entity_without_balance_raises_no_warning(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _publish(hass, low=None)
    entry = await _setup_unavailable(hass, hass_storage, lfp_balance=False)
    data = entry.runtime_data.data
    assert data["valid"], data.get("reason")
    warnings = data["quality"]["warnings"]
    assert "balance_pessimistic_pv_unavailable" not in warnings
    assert "missing_forecast_continuation" not in warnings
    assert data["quality"]["missing_sources"] == []
    assert await hass.config_entries.async_unload(entry.entry_id)
