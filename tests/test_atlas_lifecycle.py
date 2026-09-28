"""AtlasBridge lifecycle, environment switch and diagnostics (ADR-0019 §5/§9).

`SinkThread` itself is replaced by a test double here: its own hang-bounded shutdown is
atlas_sink's contract, proven by that package's own tests (energy-atlas edge/tests, T-401).
What the glue owns and must prove is: it calls `stop()` with the ADR's timeouts at the right
lifecycle point, it never starts a thread/import when not registered, and diagnostics/status
never carry secrets.
"""

import logging
from typing import ClassVar

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.atlas import bridge as bridge_module
from custom_components.energy_compass.atlas.storage import environment_dir
from custom_components.energy_compass.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.energy_compass.settings import default_configuration


class _FakeSinkThread:
    instances: ClassVar[list] = []

    def __init__(self, dir, base_url, attrs, **kwargs):
        self.dir = dir
        self.base_url = base_url
        self.attrs = attrs
        self.started = False
        self.stop_calls = []
        self.fed = []
        self.solves = []
        _FakeSinkThread.instances.append(self)

    def start(self):
        self.started = True

    def stop(self, timeout_s=10):
        self.stop_calls.append(timeout_s)

    def feed(self, ts, values):
        self.fed.append((ts, dict(values)))

    def add_solve(self, payload):
        self.solves.append(payload)

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
    yield _FakeSinkThread.instances


def _registered_entry(hass, environment="staging"):
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(
        domain="energy_compass",
        data=config,
        options={"atlas": {"enabled": True, "environment": environment, "pv_kwp": 5.0}},
        version=2,
    )
    entry.add_to_hass(hass)
    directory = environment_dir(hass, entry.entry_id, environment)
    directory.mkdir(parents=True)
    (directory / "site.json").write_text('{"site_id": "site-1"}')
    return entry


async def test_unload_stops_the_sink_with_a_ten_second_bound(
    recorder_mock, hass, enable_custom_integrations, fake_sink
):
    entry = _registered_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert fake_sink[0].started

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert fake_sink[0].stop_calls == [10]


async def test_hass_stop_event_stops_the_sink_with_a_five_second_bound(
    recorder_mock, hass, enable_custom_integrations, fake_sink
):
    entry = _registered_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert fake_sink[0].stop_calls == [5]


async def test_enabled_but_not_registered_warns_and_does_not_start(
    recorder_mock, hass, enable_custom_integrations, fake_sink, caplog
):
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(
        domain="energy_compass",
        data=config,
        options={"atlas": {"enabled": True, "environment": "staging", "pv_kwp": 5.0}},
        version=2,
    )
    entry.add_to_hass(hass)
    with caplog.at_level(logging.WARNING):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert fake_sink == []
    assert sum("not registered" in r.message for r in caplog.records) == 1
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["energy_atlas"] == {
        "enabled": True,
        "environment": "staging",
        "sink": {"registered": False},
        "solves_skipped": 0,
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_never_carry_secrets_or_site_id(
    recorder_mock, hass, enable_custom_integrations, fake_sink
):
    entry = _registered_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    section = diagnostics["energy_atlas"]
    assert section["enabled"] is True
    assert section["environment"] == "staging"
    assert section["sink"]["registered"] is True
    assert "site_id" not in section["sink"]
    assert "site-1" not in str(section)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_environment_switch_keeps_separate_directories_and_no_reregistration(
    recorder_mock, hass, enable_custom_integrations, fake_sink, monkeypatch
):
    from custom_components.energy_compass import config_flow

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("register() must not run for an already-registered env")

    monkeypatch.setattr(config_flow, "register", _fail_if_called)

    entry = _registered_entry(hass, environment="staging")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    staging_dir = environment_dir(hass, entry.entry_id, "staging")
    assert staging_dir.exists()

    # Pre-register production too (as if it had been enabled before).
    production_dir = environment_dir(hass, entry.entry_id, "production")
    production_dir.mkdir(parents=True)
    (production_dir / "site.json").write_text('{"site_id": "site-2"}')

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "energy_atlas"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": True, "environment": "production", "pv_kwp": 5.0},
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    assert staging_dir.exists()  # kept, not deleted, on switch
    assert production_dir.exists()
    assert entry.runtime_data.atlas.environment == "production"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_remove_entry_deletes_every_environment_directory(
    recorder_mock, hass, enable_custom_integrations, fake_sink
):
    entry = _registered_entry(hass)
    production_dir = environment_dir(hass, entry.entry_id, "production")
    production_dir.mkdir(parents=True)
    (production_dir / "site.json").write_text('{"site_id": "site-2"}')
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    from custom_components.energy_compass.atlas.storage import entry_dir

    assert not entry_dir(hass, entry.entry_id).exists()
