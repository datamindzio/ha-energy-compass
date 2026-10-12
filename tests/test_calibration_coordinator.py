from datetime import timedelta

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass import calibration
from custom_components.energy_compass.config_models import NumericSetting
from custom_components.energy_compass.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import EntityBinding

START = "2026-09-24T10:00:00+00:00"
KWH = {"unit_of_measurement": "kWh"}


def _config(counters=True):
    config = default_configuration("EUR", "UTC")
    config["sources"].update(
        battery_enabled=True, soc=EntityBinding("sensor.soc").to_dict()
    )
    config["settings"].update(
        capacity_kwh=20,
        soc_ceiling=100,
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
        inverter_kw=5,
        grid_import_kw=7,
        allow_grid_charge=True,
        limit_export_to_pv=False,
        eta_charge=0.97,
        eta_discharge=0.98,
    )
    if counters:
        for role, entity in (
            ("battery_charge_energy", "sensor.charged"),
            ("battery_discharge_energy", "sensor.discharged"),
        ):
            config["measurements"][role] = NumericSetting(
                entity=EntityBinding(entity),
                unit="kWh",
                source_unit="kWh",
                minimum=0,
                max_age_seconds=86400,
            ).to_dict()
    return config


async def _setup(hass, config):
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Home", version=3
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def _report(hass, charged, discharged):
    hass.states.async_set("sensor.soc", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.charged", str(charged), KWH)
    hass.states.async_set("sensor.discharged", str(discharged), KWH)


async def test_counters_feed_the_store_attributes_and_diagnostics(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _report(hass, 100.0, 90.0)
    entry = await _setup(hass, _config())
    coordinator = entry.runtime_data
    for hour in range(1, 6):
        freezer.tick(timedelta(hours=1))
        _report(hass, 100.0 + 10 * hour, 90.0 + 9 * hour)
        await hass.async_block_till_done()
    assert len(coordinator._calibration["hours"]) >= 4
    report = coordinator.calibration_report()
    trip = report["battery_round_trip"]
    assert trip["status"] == "insufficient_data"
    assert trip["configured"] == round(0.97 * 0.98, 4)
    assert trip["charge_kwh"] == 50.0
    assert trip["discharge_kwh"] == 45.0
    assert "pv_forecast_accuracy" not in report

    status = hass.states.get("sensor.home_optimizer_status")
    assert status.attributes["measured_calibration"]["battery_round_trip"] == trip
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["measured_calibration"]["battery_round_trip"] == trip

    assert await hass.config_entries.async_unload(entry.entry_id)
    key = f"energy_compass.{entry.entry_id}.calibration"
    assert hass_storage[key]["data"]["hours"] == coordinator._calibration["hours"]


async def test_state_survives_restart_and_bad_store_is_dropped(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _report(hass, 100.0, 90.0)
    entry = MockConfigEntry(
        domain="energy_compass", data=_config(), title="Home", version=3
    )
    entry.add_to_hass(hass)
    key = f"energy_compass.{entry.entry_id}.calibration"
    stored = {
        "last": {},
        "hours": {"2026-09-24T09:00:00+00:00": {"charge": 1.0, "discharge": 0.9}},
        "forecast": {},
    }
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": stored}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "2026-09-24T09:00:00+00:00" in entry.runtime_data._calibration["hours"]
    assert await hass.config_entries.async_unload(entry.entry_id)

    hass_storage[key]["data"] = {"hours": "garbage"}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "2026-09-24T09:00:00+00:00" not in entry.runtime_data._calibration["hours"]


async def test_block_is_omitted_without_sources(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _report(hass, 100.0, 90.0)
    entry = await _setup(hass, _config(counters=False))
    assert entry.runtime_data.calibration_report() is None
    status = hass.states.get("sensor.home_optimizer_status")
    assert "measured_calibration" not in status.attributes
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert "measured_calibration" not in diagnostics


async def test_published_forecast_hour_is_recorded_once(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _report(hass, 100.0, 90.0)
    entry = await _setup(hass, _config())
    coordinator = entry.runtime_data
    now = dt_util.utcnow()
    row = {"hour_start": START, "kwh_hour": 2.5, "fraction": 1.0}
    coordinator._record_pv_forecast({"pv_forecast_hours": [row]}, now)
    coordinator._record_pv_forecast(
        {"pv_forecast_hours": [{**row, "kwh_hour": 9.0, "fraction": 0.5}]}, now
    )
    assert coordinator._calibration["forecast"][START] == [2.5, 1.0]


async def test_pv_forecast_accuracy_end_to_end(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    from custom_components.energy_compass.config_models import PvSource
    from custom_components.energy_compass.sources.bindings import IntervalBinding

    freezer.move_to(START)
    config = _config(counters=False)
    config["sources"]["pv"] = PvSource(
        True,
        (
            (
                IntervalBinding(
                    EntityBinding("sensor.pv_forecast", attribute="rows"),
                    start_path="start",
                    interval_minutes=60,
                    value_path="kwh",
                    unit="kWh",
                    value_kind="energy",
                ),
            ),
        ),
    ).to_dict()
    config["measurements"]["pv_energy"] = NumericSetting(
        entity=EntityBinding("sensor.pv_total"),
        unit="kWh",
        source_unit="kWh",
        minimum=0,
        max_age_seconds=86400,
    ).to_dict()

    def publish_inputs(produced):
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        hass.states.async_set("sensor.soc", "50", {"unit_of_measurement": "%"})
        hass.states.async_set("sensor.pv_total", str(produced), KWH)
        hass.states.async_set(
            "sensor.pv_forecast",
            "ok",
            {
                "rows": [
                    {"start": now + timedelta(hours=i), "kwh": 2.0}
                    for i in range(-1, 3)
                ]
            },
        )

    publish_inputs(1000.0)
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Home", version=3
    )
    entry.add_to_hass(hass)
    # A day of history from the store: the ratio needs 24 covered hours.
    first = dt_util.utcnow() - timedelta(hours=24)
    stored = calibration.empty_state()
    for back in range(24):
        hour = first + timedelta(hours=back)
        stored["hours"][hour.isoformat()] = {"pv": 976.0 + back}
        stored["forecast"][hour.isoformat()] = [2.0, 1.0]
    stored["hours"][dt_util.utcnow().isoformat()] = {"pv": 1000.0}
    key = f"energy_compass.{entry.entry_id}.calibration"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": stored}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    assert coordinator.data["pv_forecast_hours"][0]["kwh_hour"] == 2.0
    for hour in range(1, 4):
        freezer.tick(timedelta(hours=1))
        publish_inputs(1000.0 + hour)
        await hass.async_block_till_done(wait_background_tasks=True)
    report = coordinator.calibration_report()
    accuracy = report["pv_forecast_accuracy"]
    assert "battery_round_trip" not in report
    assert accuracy["status"] == "ok"
    assert accuracy["covered_hours"] >= 24
    assert accuracy["ratio"] == 0.5
    assert accuracy["by_local_hour"]["10"]["ratio"] == 0.5
    status = hass.states.get("sensor.home_optimizer_status")
    assert status.attributes["measured_calibration"]["pv_forecast_accuracy"] == (
        accuracy
    )
