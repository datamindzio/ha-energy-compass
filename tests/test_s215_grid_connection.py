"""S-215 acceptance: Energy Compass sends `grid_connection_kw` in the Atlas attributes.

Source: energy-atlas docs/PRD-hexmap.md rev 3 §S-215, ARCHITECTURE.md "Hex map iteration 2"
(attribute flow of `grid_connection_kw`), ADR-0040..0042, contract 1.3.0-hexmap2 (vendored in
tests/atlas_contract/openapi.yaml). One group of tests per acceptance criterion; the criterion
is in the test name. Nothing here depends on an Energy Compass release or on a Home Assistant
update (ADR-0042 §4).
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import jsonschema
import pytest
import voluptuous as vol
import yaml
from homeassistant import data_entry_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from custom_components.energy_compass.atlas import bridge as bridge_module
from custom_components.energy_compass.atlas.attrs_builder import build_attrs
from custom_components.energy_compass.atlas.storage import environment_dir
from custom_components.energy_compass.atlas_sink.attributes import AttributeTracker
from custom_components.energy_compass.atlas_sink.outbox import Outbox
from custom_components.energy_compass.settings import default_configuration

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "tests" / "atlas_contract" / "openapi.yaml"
COMPONENT = ROOT / "custom_components" / "energy_compass"
OPENAPI_URI = "urn:atlas-openapi"

FIELD = "grid_connection_kw"
LABEL_EN = "Grid connection power (kW)"
LABEL_PL = "Moc przyłączeniowa (kW)"

VALID_VALUES = [0.1, 11.0, 14.5, 1000.0]
INVALID_VALUES = [0.0, 0.09, 1000.1, -5.0, 100000.0]


# --- contract helpers (same validator setup as tests/test_atlas_contract.py) -------------


def _registry() -> Registry:
    doc = yaml.safe_load(CONTRACT.read_text())
    resource = Resource.from_contents(doc, default_specification=DRAFT202012)
    return Registry().with_resource(OPENAPI_URI, resource)


REGISTRY = _registry()


def attrs_errors(instance: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(
        {"$ref": f"{OPENAPI_URI}#/components/schemas/AttrsV1"}, registry=REGISTRY
    )
    return [
        f"{'/'.join(str(p) for p in e.path)}: {e.message}"
        for e in validator.iter_errors(instance)
    ]


def _config() -> dict:
    config = default_configuration("EUR", "UTC")
    config["sources"]["battery_enabled"] = True
    config["settings"].update(capacity_kwh=10.0, operating_floor=10, soc_ceiling=95)
    return config


# --- attribute builder + contract --------------------------------------------------------


def test_S215_vendored_contract_is_1_3_0_hexmap2():
    doc = yaml.safe_load(CONTRACT.read_text())
    assert doc["info"]["version"] == "1.3.0-hexmap2"
    assert FIELD in doc["components"]["schemas"]["AttrsV1"]["properties"]


@pytest.mark.parametrize("value", VALID_VALUES)
def test_S215_snapshot_carries_the_saved_value_and_is_valid_attrs_v1(value):
    attrs = build_attrs(_config(), {"pv_kwp": 6.5, FIELD: value})
    assert attrs[FIELD] == value
    assert attrs_errors(attrs) == []


def test_S215_snapshot_with_location_and_grid_connection_is_valid_attrs_v1():
    attrs = build_attrs(_config(), {"pv_kwp": 6.5, FIELD: 11.0}, "861f8d947ffffff")
    assert attrs[FIELD] == 11.0
    assert attrs["location"] == {"h3_res6": "861f8d947ffffff"}
    assert attrs_errors(attrs) == []


@pytest.mark.parametrize("settings", [{"pv_kwp": 6.5}, {"pv_kwp": 6.5, FIELD: None}])
def test_S215_empty_field_gives_attributes_without_the_key(settings):
    attrs = build_attrs(_config(), settings)
    assert FIELD not in attrs  # the contract has no null: absent = not known
    assert attrs_errors(attrs) == []


def test_S215_grid_connection_does_not_change_the_other_attributes():
    without = build_attrs(_config(), {"pv_kwp": 6.5})
    with_field = build_attrs(_config(), {"pv_kwp": 6.5, FIELD: 11.0})
    assert {k: v for k, v in with_field.items() if k != FIELD} == without


# --- unchanged value -> no new attribute version ----------------------------------------


class _Clock:
    def __init__(self):
        self.at = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)

    def now(self):
        return self.at


def _tracker(tmp_path, settings: dict):
    outbox = Outbox(tmp_path / "outbox.db")
    clock = _Clock()
    tracker = AttributeTracker(
        outbox, clock, lambda: build_attrs(_config(), dict(settings))
    )
    return tracker, outbox


def test_S215_unchanged_value_queues_no_new_attribute_version(tmp_path):
    settings = {"pv_kwp": 6.5, FIELD: 11.0}
    tracker, outbox = _tracker(tmp_path, settings)
    assert tracker.check() is True
    assert tracker.check() is False
    assert tracker.check() is False
    (item,) = outbox.pending("attributes")
    assert item.payload["attrs"][FIELD] == 11.0


def test_S215_changed_value_queues_a_new_attribute_version(tmp_path):
    settings = {"pv_kwp": 6.5, FIELD: 11.0}
    tracker, outbox = _tracker(tmp_path, settings)
    assert tracker.check() is True
    settings[FIELD] = 14.0
    assert tracker.check() is True
    values = [i.payload["attrs"].get(FIELD) for i in outbox.pending("attributes")]
    assert values == [11.0, 14.0]


def test_S215_adding_then_clearing_the_value_each_queue_one_version(tmp_path):
    settings = {"pv_kwp": 6.5}
    tracker, outbox = _tracker(tmp_path, settings)
    assert tracker.check() is True
    settings[FIELD] = 11.0
    assert tracker.check() is True
    del settings[FIELD]
    assert tracker.check() is True
    values = [i.payload["attrs"].get(FIELD) for i in outbox.pending("attributes")]
    assert values == [None, 11.0, None]


# --- options form ------------------------------------------------------------------------


class _FakeSinkThread:
    instances: ClassVar[list] = []

    def __init__(self, dir, base_url, attrs, **kwargs):
        self.attrs = attrs
        self.stop_calls = []
        _FakeSinkThread.instances.append(self)

    def start(self):
        pass

    def stop(self, timeout_s=10):
        self.stop_calls.append(timeout_s)

    def feed(self, ts, values):
        pass

    def add_solve(self, payload):
        pass

    def status(self):
        return {
            "registered": True,
            "pending": {"telemetry": 0},
            "dead": 0,
            "last_success_at": None,
            "halted": {},
        }


@pytest.fixture(autouse=True)
def fake_sink(monkeypatch):
    _FakeSinkThread.instances.clear()
    monkeypatch.setattr(bridge_module, "SinkThread", _FakeSinkThread)
    return _FakeSinkThread.instances


def _atlas(**extra):
    return {"enabled": True, "environment": "staging", "pv_kwp": 5.0, **extra}


def _entry(hass, atlas):
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(
        domain="energy_compass", data=config, options={"atlas": atlas}, version=2
    )
    entry.add_to_hass(hass)
    directory = environment_dir(hass, entry.entry_id, "staging")
    directory.mkdir(parents=True)
    (directory / "site.json").write_text('{"site_id": "site-1"}')
    return entry


async def _loaded(hass, atlas):
    entry = _entry(hass, atlas)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def _open(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "energy_atlas"}
    )


def _schema_key(result, name):
    (key,) = [k for k in result["data_schema"].schema if k == name]
    return key


async def test_S215_field_follows_pv_capacity_and_is_not_prefilled_from_grid_limits(
    recorder_mock, hass, enable_custom_integrations
):
    entry = await _loaded(hass, _atlas())
    # The grid import/export limits are set (defaults 10 / 0) and must not leak in.
    assert entry.data["settings"]["grid_import_kw"] > 0
    result = await _open(hass, entry)
    keys = [str(k) for k in result["data_schema"].schema]
    assert FIELD in keys
    assert keys.index(FIELD) == keys.index("pv_kwp") + 1
    key = _schema_key(result, FIELD)
    assert key.default is vol.UNDEFINED  # no default
    assert (key.description or {}).get("suggested_value") is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_S215_field_is_optional_in_the_schema(
    recorder_mock, hass, enable_custom_integrations
):
    entry = await _loaded(hass, _atlas())
    result = await _open(hass, entry)
    assert isinstance(_schema_key(result, FIELD), vol.Optional)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_S215_field_shows_the_value_saved_before(
    recorder_mock, hass, enable_custom_integrations
):
    entry = await _loaded(hass, _atlas(**{FIELD: 11.0}))
    result = await _open(hass, entry)
    key = _schema_key(result, FIELD)
    assert key.description["suggested_value"] == 11.0
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("value", VALID_VALUES)
async def test_S215_valid_value_is_stored_and_applied_live_without_reload(
    recorder_mock, hass, enable_custom_integrations, fake_sink, monkeypatch, value
):
    reloads = []
    real_reload = hass.config_entries.async_reload

    async def _spy_reload(entry_id):
        reloads.append(entry_id)
        return await real_reload(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", _spy_reload)
    entry = await _loaded(hass, _atlas())
    coordinator = entry.runtime_data
    before = len(fake_sink)
    assert FIELD not in fake_sink[-1].attrs

    result = await _open(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": True, "environment": "staging", "pv_kwp": 5.0, FIELD: value},
    )
    assert result["type"] == "create_entry", result.get("errors")
    await hass.async_block_till_done()

    assert entry.options["atlas"][FIELD] == value
    assert reloads == []
    assert entry.runtime_data is coordinator
    assert len(fake_sink) == before + 1  # live apply started a fresh sink
    assert fake_sink[-1].attrs[FIELD] == value
    assert attrs_errors(fake_sink[-1].attrs) == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_S215_empty_field_saves_and_attributes_carry_no_key(
    recorder_mock, hass, enable_custom_integrations, fake_sink
):
    entry = await _loaded(hass, _atlas())
    before = len(fake_sink)
    result = await _open(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": True, "environment": "staging", "pv_kwp": 6.0},
    )
    assert result["type"] == "create_entry", result.get("errors")
    await hass.async_block_till_done()
    assert FIELD not in entry.options["atlas"]
    assert entry.options["atlas"]["pv_kwp"] == 6.0
    assert len(fake_sink) == before + 1
    assert FIELD not in fake_sink[-1].attrs
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_S215_clearing_a_saved_value_removes_it_from_options_and_attributes(
    recorder_mock, hass, enable_custom_integrations, fake_sink
):
    entry = await _loaded(hass, _atlas(**{FIELD: 11.0}))
    assert fake_sink[-1].attrs[FIELD] == 11.0
    result = await _open(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": True, "environment": "staging", "pv_kwp": 5.0},
    )
    assert result["type"] == "create_entry", result.get("errors")
    await hass.async_block_till_done()
    assert FIELD not in entry.options["atlas"]
    assert FIELD not in fake_sink[-1].attrs
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("value", INVALID_VALUES)
async def test_S215_out_of_range_value_shows_a_field_error_and_saves_nothing(
    recorder_mock, hass, enable_custom_integrations, fake_sink, value
):
    entry = await _loaded(hass, _atlas())
    original = dict(entry.options["atlas"])
    before = len(fake_sink)
    result = await _open(hass, entry)
    flow_id = result["flow_id"]
    try:
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {"enabled": True, "environment": "staging", "pv_kwp": 5.0, FIELD: value},
        )
    except data_entry_flow.InvalidData as err:
        # Schema-level range check: the frontend renders this on the field.
        assert FIELD in err.schema_errors
    else:
        assert result["type"] == "form"
        assert result["step_id"] == "energy_atlas"
        assert FIELD in result["errors"]
    assert entry.options["atlas"] == original
    assert len(fake_sink) == before  # no new bridge/sink either
    assert await hass.config_entries.async_unload(entry.entry_id)


# --- translations (EN + PL) --------------------------------------------------------------


def _step(path: Path) -> dict:
    return json.loads(path.read_text())["options"]["step"]["energy_atlas"]


@pytest.mark.parametrize(
    ("file", "label"),
    [
        ("strings.json", LABEL_EN),
        ("translations/en.json", LABEL_EN),
        ("translations/pl.json", LABEL_PL),
    ],
)
def test_S215_field_has_label_and_description_in_every_translation_file(file, label):
    step = _step(COMPONENT / file)
    assert step["data"][FIELD] == label
    assert step["data_description"][FIELD].strip()


def test_S215_label_comes_right_after_pv_capacity_in_every_translation_file():
    for file in ("strings.json", "translations/en.json", "translations/pl.json"):
        keys = list(_step(COMPONENT / file)["data"])
        assert keys.index(FIELD) == keys.index("pv_kwp") + 1, file


# --- docs (guide EN + PL, README parity) -------------------------------------------------


def _atlas_section(path: str, heading: str) -> str:
    text = (ROOT / path).read_text()
    match = re.search(rf"^## {re.escape(heading)}\s*$", text, re.MULTILINE)
    assert match, f"{path}: no '## {heading}' section"
    rest = text[match.end() :]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def test_S215_guide_en_lists_the_field_in_the_atlas_settings_table_and_explains_without_max():
    section = _atlas_section("docs/guide.en.md", "Energy Atlas (optional)")
    rows = [line for line in section.splitlines() if line.startswith("|")]
    row = next((r for r in rows if r.startswith(f"| {LABEL_EN} |")), None)
    assert row, "guide.en.md Atlas settings table has no grid connection row"
    pv_row = next(r for r in rows if r.startswith("| PV capacity (kWp) |"))
    assert rows.index(row) == rows.index(pv_row) + 1
    assert re.search(r"0\.1", row) and re.search(r"1000", row)  # the accepted range
    assert "optional" in row.lower()
    assert "without max" in section


def test_S215_guide_pl_lists_the_field_in_the_atlas_settings_table_and_explains_bez_max():
    section = _atlas_section("docs/guide.pl.md", "Energy Atlas (opcjonalnie)")
    rows = [line for line in section.splitlines() if line.startswith("|")]
    row = next((r for r in rows if r.startswith(f"| {LABEL_PL} |")), None)
    assert row, "guide.pl.md Atlas settings table has no grid connection row"
    pv_row = next(r for r in rows if r.startswith("| Moc instalacji PV (kWp) |"))
    assert rows.index(row) == rows.index(pv_row) + 1
    assert re.search(r"0[.,]1", row) and re.search(r"1000", row)
    assert "bez max" in section


def test_S215_readmes_agree_on_whether_they_list_the_field():
    en = (ROOT / "README.md").read_text()
    pl = (ROOT / "README.pl.md").read_text()
    # README paragraphs name the Atlas form but do not list its fields today; if either one
    # starts naming the field, the other must too (AGENTS.md: EN and PL are mirrors).
    assert (LABEL_EN in en or "grid connection" in en.lower()) == (
        LABEL_PL in pl or "przyłączeniow" in pl.lower()
    )
