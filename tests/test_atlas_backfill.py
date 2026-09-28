"""Backfill service `energy_compass.atlas_backfill` (ADR-0019 §8).

Mapping tests drive `_atlas_row`/`_merge_rows` directly with sanitized recorder-statistics
fixtures (StatisticsRow-shaped dicts: epoch `start`, `mean`/`max`/`state`) and validate the
result both against the vendored contract and by round-tripping it through the real
`atlas_sink.backfill.convert()` (LESSONS producer-keys-by-rule-never-checked-against-schema).
The service-level tests drive the actual `hass.services.async_call` with
`statistics_during_period` monkeypatched (recorder DB internals are HA's own concern, not
this glue's), mirroring `test_atlas_options_flow.py`'s style.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import jsonschema
import pytest
import voluptuous as vol
import yaml
from pytest_homeassistant_custom_component.common import MockConfigEntry
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from custom_components.energy_compass.atlas import backfill_service
from custom_components.energy_compass.atlas import bridge as bridge_module
from custom_components.energy_compass.atlas.backfill_service import (
    SERVICE_ATLAS_BACKFILL,
    SERVICE_SCHEMA,
    _atlas_row,
    _merge_rows,
)
from custom_components.energy_compass.atlas.storage import environment_dir
from custom_components.energy_compass.atlas_sink.backfill import convert
from custom_components.energy_compass.config_models import NumericSetting
from custom_components.energy_compass.settings import DOMAIN, default_configuration
from custom_components.energy_compass.sources.bindings import EntityBinding

OPENAPI_URI = "urn:atlas-openapi-backfill"
CONTRACT = Path(__file__).resolve().parent / "atlas_contract" / "openapi.yaml"


def _registry() -> Registry:
    doc = yaml.safe_load(CONTRACT.read_text())
    resource = Resource.from_contents(doc, default_specification=DRAFT202012)
    return Registry().with_resource(OPENAPI_URI, resource)


REGISTRY = _registry()


def schema_errors(schema_name: str, instance: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(
        {"$ref": f"{OPENAPI_URI}#/components/schemas/{schema_name}"}, registry=REGISTRY
    )
    return [
        f"{'/'.join(str(p) for p in e.path)}: {e.message}"
        for e in validator.iter_errors(instance)
    ]


def _power_setting(entity_id, unit="kW", multiplier=1.0):
    return NumericSetting(
        entity=EntityBinding(entity_id), unit=unit, multiplier=multiplier
    ).to_dict()


def _full_config():
    config = default_configuration("EUR", "UTC")
    config["measurements"]["pv_power"] = _power_setting("sensor.pv")
    config["measurements"]["grid_import_power"] = _power_setting("sensor.grid_in")
    config["measurements"]["grid_export_power"] = _power_setting("sensor.grid_out")
    config["measurements"]["battery_power"] = _power_setting("sensor.batt")
    config["measurements"]["pv_energy"] = _power_setting("sensor.pv_energy", unit="kWh")
    config["measurements"]["grid_import_energy"] = _power_setting(
        "sensor.import_energy", unit="kWh"
    )
    config["sources"]["soc"] = EntityBinding("sensor.soc").to_dict()
    config["sources"]["load"]["mode"] = "recorder"
    config["sources"]["load"]["power"] = EntityBinding("sensor.load").to_dict()
    config["sources"]["load"]["history_unit"] = "kW"
    return config


# --- pure mapping: _atlas_row / _merge_rows -----------------------------------------


def test_atlas_row_maps_every_bound_key_with_pv_load_max():
    config = _full_config()
    entity_at = {
        "sensor.pv": {"mean": 2.0, "max": 3.0},
        "sensor.load": {"mean": 1.1, "max": 1.5},
        "sensor.grid_in": {"mean": 0.5},
        "sensor.grid_out": {"mean": 0.0},
        "sensor.batt": {"mean": -0.2},  # Compass +discharge -> Atlas batt_w +charge
        "sensor.soc": {"mean": 55},
        "sensor.pv_energy": {"state": 12.3},
        "sensor.import_energy": {"state": 4.0},
    }
    row = _atlas_row(config, entity_at)
    assert row == {
        "pv_w_avg": 2000.0,
        "pv_w_max": 3000.0,
        "load_w_avg": 1100.0,
        "load_w_max": 1500.0,
        "grid_import_w_avg": 500.0,
        "grid_export_w_avg": 0.0,
        "batt_w_avg": 200.0,
        "soc_pct_last": 55.0,
        "pv_kwh_total": 12.3,
        "import_kwh_total": 4.0,
    }
    payload = {
        "window_start": "2026-09-28T12:00:00Z",
        "samples": 0,
        "origin": "backfill",
        **row,
    }
    assert schema_errors("TelemetryWindow", payload) == []
    hourly_payload = {"hour_start": "2026-09-28T12:00:00Z", **row}
    assert schema_errors("HourlyRow", hourly_payload) == []


def test_atlas_row_applies_t402_bounds():
    # ADR-0019 Amendment T-402: idle meter -3 W import, BMS 100.4 %, glitching counter.
    config = _full_config()
    entity_at = {
        "sensor.grid_in": {"mean": -0.003},
        "sensor.soc": {"mean": 100.4},
        "sensor.import_energy": {"state": -0.1},
    }
    row = _atlas_row(config, entity_at)
    assert row["grid_import_w_avg"] == 0.0
    assert row["soc_pct_last"] == 100.0
    assert "import_kwh_total" not in row  # negative counter reading -> not fed


def test_atlas_row_battery_split_pair():
    config = _full_config()
    del config["measurements"]["battery_power"]
    config["measurements"]["battery_charge_power"] = _power_setting("sensor.chg")
    config["measurements"]["battery_discharge_power"] = _power_setting("sensor.dis")
    entity_at = {"sensor.chg": {"mean": 0.5}, "sensor.dis": {"mean": 0.1}}
    row = _atlas_row(config, entity_at)
    assert row["batt_w_avg"] == 400.0  # 500 W charge - 100 W discharge


def test_atlas_row_non_finite_and_unbound_are_absent():
    config = _full_config()
    row = _atlas_row(config, {"sensor.pv": {"mean": float("nan")}})
    assert row == {}


def test_merge_rows_groups_by_timestamp_and_round_trips_through_the_vendored_converter():
    config = _full_config()
    per_entity = {
        "sensor.pv": [{"start": 1_800_000_000.0, "mean": 2.0, "max": 2.5}],
        "sensor.grid_in": [{"start": 1_800_000_000.0, "mean": 0.1}],
    }
    merged = _merge_rows(config, per_entity)
    assert len(merged) == 1
    assert merged[0]["start"] == "2027-01-15T08:00:00Z"
    assert merged[0]["pv_w_avg"] == 2000.0
    assert merged[0]["pv_w_max"] == 2500.0
    assert merged[0]["grid_import_w_avg"] == 100.0

    windows, rows = convert({"five_minute": merged, "hourly": []})
    assert rows == []
    assert len(windows) == 1
    payload = {"windows": [windows[0]]}
    assert schema_errors("TelemetryBatch", payload) == []


def test_merge_rows_hourly_wins_only_where_five_minute_is_missing():
    config = _full_config()
    hour_start = datetime(2026, 9, 28, 10, tzinfo=UTC).timestamp()
    five_ts = hour_start + 300
    per_entity_five = {"sensor.pv": [{"start": five_ts, "mean": 1.0}]}
    per_entity_hourly = {
        "sensor.pv": [
            {"start": hour_start, "mean": 1.0},
            {"start": hour_start + 3600, "mean": 2.0},
        ]
    }
    five = _merge_rows(config, per_entity_five)
    hourly = _merge_rows(config, per_entity_hourly)
    windows, rows = convert({"five_minute": five, "hourly": hourly})
    assert len(windows) == 1  # the 5-minute sample
    assert len(rows) == 1  # only the hour without any 5-minute data survives
    assert rows[0]["hour_start"] == "2026-09-28T11:00:00Z"


# --- service: registration, validation, wiring ---------------------------------------


def test_days_bounds_enforced():
    assert SERVICE_SCHEMA({}) == {}
    assert SERVICE_SCHEMA({"days": 1})["days"] == 1
    assert SERVICE_SCHEMA({"days": 3650})["days"] == 3650
    with pytest.raises(vol.Invalid):
        SERVICE_SCHEMA({"days": 0})
    with pytest.raises(vol.Invalid):
        SERVICE_SCHEMA({"days": 3651})


class _FakeSinkThread:
    instances: ClassVar[list] = []

    def __init__(self, dir, base_url, attrs, **kwargs):
        self.backfill_calls = []
        _FakeSinkThread.instances.append(self)

    def start(self):
        pass

    def stop(self, timeout_s=10):
        pass

    def feed(self, ts, values):
        pass

    def add_solve(self, payload):
        pass

    def backfill(self, stats):
        self.backfill_calls.append(stats)

    def status(self):
        return {
            "registered": True,
            "pending": {},
            "dead": 0,
            "last_success_at": None,
            "halted": {},
        }


@pytest.fixture(autouse=True)
def fake_sink(monkeypatch):
    _FakeSinkThread.instances.clear()
    monkeypatch.setattr(bridge_module, "SinkThread", _FakeSinkThread)
    yield _FakeSinkThread.instances


def _registered_entry(hass, config):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        options={"atlas": {"enabled": True, "environment": "staging", "pv_kwp": 5.0}},
        version=2,
    )
    entry.add_to_hass(hass)
    directory = environment_dir(hass, entry.entry_id, "staging")
    directory.mkdir(parents=True)
    (directory / "site.json").write_text('{"site_id": "site-1"}')
    return entry


async def test_service_raises_when_nothing_enabled_and_registered(
    recorder_mock, hass, enable_custom_integrations
):
    entry = MockConfigEntry(
        domain=DOMAIN, data=default_configuration("EUR", "UTC"), version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()

    from homeassistant.exceptions import ServiceValidationError

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_ATLAS_BACKFILL, {}, blocking=True
        )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_service_feeds_merged_statistics_to_the_running_sink(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    config = _full_config()
    entry = _registered_entry(hass, config)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()

    def _fake_statistics_during_period(
        hass_, start, end, statistic_ids, period, units, types
    ):
        if period == "5minute":
            return {"sensor.pv": [{"start": 1_800_000_000.0, "mean": 2.0, "max": 2.5}]}
        return {}

    monkeypatch.setattr(
        backfill_service, "statistics_during_period", _fake_statistics_during_period
    )

    await hass.services.async_call(
        DOMAIN, SERVICE_ATLAS_BACKFILL, {"days": 30}, blocking=True
    )

    (sink,) = _FakeSinkThread.instances
    assert len(sink.backfill_calls) == 1
    stats = sink.backfill_calls[0]
    assert stats["hourly"] == []
    assert stats["five_minute"] == [
        {"start": "2027-01-15T08:00:00Z", "pv_w_avg": 2000.0, "pv_w_max": 2500.0}
    ]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_service_skips_entries_that_were_never_loaded(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    """A config entry that is only `add_to_hass`-ed (never `async_setup`) has no
    `runtime_data` attribute at all (HA sets it in `async_setup_entry`, deletes it on
    unload); the service must skip it, not raise `AttributeError`."""
    never_loaded = MockConfigEntry(
        domain=DOMAIN, data=default_configuration("EUR", "UTC"), version=2
    )
    never_loaded.add_to_hass(hass)

    config = _full_config()
    on_entry = _registered_entry(hass, config)
    assert await hass.config_entries.async_setup(on_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()

    monkeypatch.setattr(
        backfill_service,
        "statistics_during_period",
        lambda *a, **k: {},
    )
    await hass.services.async_call(DOMAIN, SERVICE_ATLAS_BACKFILL, {}, blocking=True)

    assert len(_FakeSinkThread.instances) == 1  # only the loaded, registered entry
    assert await hass.config_entries.async_unload(on_entry.entry_id)


async def test_service_raises_when_only_never_loaded_entries_exist(
    recorder_mock, hass, enable_custom_integrations
):
    from homeassistant.exceptions import ServiceValidationError
    from homeassistant.setup import async_setup_component

    never_loaded = MockConfigEntry(
        domain=DOMAIN, data=default_configuration("EUR", "UTC"), version=2
    )
    never_loaded.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_ATLAS_BACKFILL, {}, blocking=True
        )


async def test_service_skips_entries_not_enabled_or_not_registered(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    off_entry = MockConfigEntry(
        domain=DOMAIN, data=default_configuration("EUR", "UTC"), version=2
    )
    off_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(off_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()

    config = _full_config()
    on_entry = _registered_entry(hass, config)
    assert await hass.config_entries.async_setup(on_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()

    monkeypatch.setattr(
        backfill_service,
        "statistics_during_period",
        lambda *a, **k: {},
    )
    await hass.services.async_call(DOMAIN, SERVICE_ATLAS_BACKFILL, {}, blocking=True)

    assert len(_FakeSinkThread.instances) == 1  # only the enabled+registered entry
    assert await hass.config_entries.async_unload(off_entry.entry_id)
    assert await hass.config_entries.async_unload(on_entry.entry_id)
