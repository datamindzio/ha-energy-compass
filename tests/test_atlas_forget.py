"""Options step `energy_atlas`: "Forget site on <environment>" recovery (ADR-0019 §9)."""

from typing import ClassVar

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.atlas import bridge as bridge_module
from custom_components.energy_compass.atlas.storage import environment_dir
from custom_components.energy_compass.atlas_sink import sink as atlas_sink_module
from custom_components.energy_compass.settings import DOMAIN, default_configuration


class _FakeSinkThread:
    instances: ClassVar[list] = []

    def __init__(self, dir, base_url, attrs, **kwargs):
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

    def backfill(self, stats):
        pass

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


def _registered_entry(hass, environment="staging"):
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        options={"atlas": {"enabled": True, "environment": environment, "pv_kwp": 5.0}},
        version=2,
    )
    entry.add_to_hass(hass)
    directory = environment_dir(hass, entry.entry_id, environment)
    directory.mkdir(parents=True)
    (directory / "site.json").write_text('{"site_id": "site-1"}')
    return entry


async def _open_energy_atlas(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "energy_atlas"}
    )


async def test_forget_unchecked_leaves_the_site_untouched(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("register() must not run: nothing was forgotten")

    monkeypatch.setattr(atlas_sink_module, "register", _fail_if_called)
    entry = _registered_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    directory = environment_dir(hass, entry.entry_id, "staging")

    result = await _open_energy_atlas(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "enabled": True,
            "environment": "staging",
            "pv_kwp": 5.0,
            "forget_site": False,
        },
    )
    assert result["type"] == "create_entry"
    assert directory.exists()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_forget_deletes_the_directory_only_when_confirmed(
    recorder_mock, hass, enable_custom_integrations
):
    entry = _registered_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    directory = environment_dir(hass, entry.entry_id, "staging")
    assert directory.exists()

    result = await _open_energy_atlas(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": False, "environment": "staging", "forget_site": True},
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert not directory.exists()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_forget_stops_the_running_sink_for_the_active_environment(
    recorder_mock, hass, enable_custom_integrations, fake_sink
):
    entry = _registered_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(fake_sink) == 1

    result = await _open_energy_atlas(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": False, "environment": "staging", "forget_site": True},
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    # forget_site's own stop, then the reload's unload stops the (new) sink again.
    assert 10 in fake_sink[0].stop_calls
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_forget_then_save_without_secret_is_a_form_error(
    recorder_mock, hass, enable_custom_integrations
):
    entry = _registered_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await _open_energy_atlas(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": True, "environment": "staging", "pv_kwp": 5.0, "forget_site": True},
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_input"}
    directory = environment_dir(hass, entry.entry_id, "staging")
    assert not directory.exists()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_forget_then_save_with_secret_registers_a_new_site(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    calls = []

    def _register(directory, base_url, secret, timeout_s=30, **kwargs):
        calls.append((directory, secret))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "site.json").write_text('{"site_id": "site-2"}')
        return "site-2"

    entry = _registered_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await _open_energy_atlas(hass, entry)
    monkeypatch.setattr(atlas_sink_module, "register", _register)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "enabled": True,
            "environment": "staging",
            "pv_kwp": 5.0,
            "forget_site": True,
            "enrollment_secret": "new-secret",
        },
    )
    assert result["type"] == "create_entry", result.get("errors")
    assert len(calls) == 1
    assert calls[0][1] == "new-secret"
    directory = environment_dir(hass, entry.entry_id, "staging")
    assert directory.exists()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_forget_only_affects_the_selected_environment(
    recorder_mock, hass, enable_custom_integrations
):
    entry = _registered_entry(hass, environment="staging")
    production_dir = environment_dir(hass, entry.entry_id, "production")
    production_dir.mkdir(parents=True)
    (production_dir / "site.json").write_text('{"site_id": "site-prod"}')
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await _open_energy_atlas(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": False, "environment": "production", "forget_site": True},
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert environment_dir(hass, entry.entry_id, "staging").exists()
    assert not production_dir.exists()
    assert await hass.config_entries.async_unload(entry.entry_id)
