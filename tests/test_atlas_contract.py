"""Every payload the glue builds validates against the vendored contract (ADR-0019 §10).

Validator setup mirrors edge/tests/test_contract_payloads.py: load
tests/atlas_contract/openapi.yaml with PyYAML, register it in a `referencing.Registry` and
resolve `$ref` against it with `jsonschema.Draft202012Validator`.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import yaml
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from custom_components.energy_compass.atlas.attrs_builder import build_attrs
from custom_components.energy_compass.atlas.mapping import resolve_feed
from custom_components.energy_compass.atlas.solve_builder import build_solve_payload
from custom_components.energy_compass.atlas_sink.windows import Aggregator
from custom_components.energy_compass.config_models import NumericSetting
from custom_components.energy_compass.engine.models import (
    Battery,
    Flow,
    Opportunity,
    Plan,
    Problem,
    SiteLimits,
    Slot,
)
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import EntityBinding

OPENAPI_URI = "urn:atlas-openapi"
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


def _power_setting(entity_id, unit="kW"):
    return NumericSetting(entity=EntityBinding(entity_id), unit=unit).to_dict()


def test_full_telemetry_window_is_contract_valid():
    config = default_configuration("EUR", "UTC")
    config["measurements"]["pv_power"] = _power_setting("sensor.pv")
    config["measurements"]["grid_import_power"] = _power_setting("sensor.grid_in")
    config["measurements"]["grid_export_power"] = _power_setting("sensor.grid_out")
    config["measurements"]["battery_power"] = _power_setting("sensor.batt")
    config["measurements"]["pv_energy"] = NumericSetting(
        entity=EntityBinding("sensor.pv_energy"), unit="kWh"
    ).to_dict()
    config["sources"]["soc"] = EntityBinding("sensor.soc").to_dict()
    config["sources"]["load"]["mode"] = "recorder"
    config["sources"]["load"]["power"] = EntityBinding("sensor.load").to_dict()
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    states = {
        "sensor.pv": {"state": "2.0", "attributes": {"unit_of_measurement": "kW"}},
        "sensor.grid_in": {"state": "0.5", "attributes": {"unit_of_measurement": "kW"}},
        "sensor.grid_out": {
            "state": "0.0",
            "attributes": {"unit_of_measurement": "kW"},
        },
        "sensor.batt": {"state": "-0.2", "attributes": {"unit_of_measurement": "kW"}},
        "sensor.pv_energy": {
            "state": "12.3",
            "attributes": {
                "unit_of_measurement": "kWh",
                "state_class": "total_increasing",
            },
        },
        "sensor.soc": {"state": "55", "attributes": {}},
        "sensor.load": {"state": "1.1", "attributes": {}},
    }
    feed = resolve_feed(config, states, now)
    assert feed  # sanity: something was actually fed

    agg = Aggregator()
    agg.feed(now, feed)
    (window,) = agg.close(now + timedelta(minutes=5))
    payload = {
        "windows": [
            {
                "window_start": window.window_start.isoformat(),
                "samples": window.samples,
                **window.values,
            }
        ]
    }
    assert schema_errors("TelemetryBatch", payload) == []


def test_solve_payload_is_contract_valid():
    start = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    slots = tuple(
        Slot(
            start + timedelta(hours=i),
            start + timedelta(hours=i + 1),
            0.2,
            0.1,
            1.0,
            0.5,
        )
        for i in range(2)
    )
    flows = tuple(
        Flow(0.1, 0.0, 0.0, 0.2, 0.0, 5.0, dispatch_mode="SELF_CONSUME") for _ in slots
    )
    battery = Battery(10.0, 0.1, 1.0, 4.0, 3.0, 3.0, 0.95, 0.95, 0.01, True, True)
    problem = Problem(
        slots,
        SiteLimits(5.0, 5.0, 5.0, False),
        battery,
        "preserve_initial",
        0.0,
        (),
        "UTC",
    )
    plan = Plan(flows, 1.0, 1.0, 0.0, 0.0)
    analysis = SimpleNamespace(
        opportunities=(Opportunity(start, start + timedelta(hours=2), 0.1, "CHEAP"),)
    )
    values = {
        "boost_ceiling": 0.01,
        "limit_floor": 0.8,
        "cheap_percentile": 25,
        "limit_percentile": 75,
        "minimum_mode_minutes": 60,
        "minimum_mode_power_kw": 0.1,
    }
    payload = build_solve_payload(problem, plan, analysis, values, {}, start)
    assert payload is not None
    assert schema_errors("Solve", payload) == []


def test_attrs_are_contract_valid():
    config = default_configuration("EUR", "UTC")
    config["sources"]["battery_enabled"] = True
    config["settings"].update(capacity_kwh=10.0, operating_floor=10, soc_ceiling=95)
    attrs = build_attrs(config, {"pv_kwp": 6.5})
    assert schema_errors("AttrsV1", attrs) == []


def test_attrs_disabled_battery_are_contract_valid():
    config = default_configuration("EUR", "UTC")
    attrs = build_attrs(config, {"pv_kwp": 4.0})
    assert attrs["battery_kwh_nominal"] == 0.0
    assert schema_errors("AttrsV1", attrs) == []
