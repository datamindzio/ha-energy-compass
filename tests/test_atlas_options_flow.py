"""Options step `energy_atlas` (ADR-0019 §2/§4): form errors, secret hygiene, preservation."""

import logging

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass import config_flow
from custom_components.energy_compass.atlas.storage import environment_dir
from custom_components.energy_compass.atlas_sink.sink import RegistrationError
from custom_components.energy_compass.settings import default_configuration

SECRET = "s3cr3t-enrollment-value"


def _entry(hass, atlas=None):
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(
        domain="energy_compass",
        data=config,
        options={"atlas": atlas} if atlas else {},
        version=2,
    )
    entry.add_to_hass(hass)
    return entry


async def _open_energy_atlas(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "energy_atlas"}
    )


@pytest.mark.parametrize(
    "kind",
    [
        "invalid_enrollment_secret",
        "site_key_revoked",
        "site_conflict",
        "rate_limited",
        "cannot_connect",
        "unknown",
    ],
)
async def test_registration_failure_kinds_map_to_form_errors(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, kind
):
    def _raise(*args, **kwargs):
        raise RegistrationError(kind)

    monkeypatch.setattr(config_flow, "register", _raise)
    entry = _entry(hass)
    result = await _open_energy_atlas(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "enabled": True,
            "environment": "staging",
            "pv_kwp": 5.0,
            "enrollment_secret": SECRET,
        },
    )
    assert result["step_id"] == "energy_atlas"
    assert result["errors"] == {"base": kind}
    assert "atlas" not in entry.options


async def test_missing_secret_when_not_registered_is_invalid_input(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("register() must not be called without a secret")

    monkeypatch.setattr(config_flow, "register", _fail_if_called)
    entry = _entry(hass)
    result = await _open_energy_atlas(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": True, "environment": "staging", "pv_kwp": 5.0},
    )
    assert result["errors"] == {"base": "invalid_input"}


async def test_successful_registration_saves_and_never_stores_the_secret(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, caplog
):
    calls = []

    def _register(directory, base_url, secret, timeout_s=30, **kwargs):
        calls.append((directory, base_url, secret))
        from pathlib import Path

        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / "site.json").write_text('{"site_id": "abc"}')
        return "abc"

    monkeypatch.setattr(config_flow, "register", _register)
    entry = _entry(hass)
    with caplog.at_level(logging.DEBUG):
        result = await _open_energy_atlas(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "enabled": True,
                "environment": "staging",
                "pv_kwp": 7.5,
                "enrollment_secret": SECRET,
            },
        )
    assert result["type"] == "create_entry"
    assert entry.options["atlas"] == {
        "enabled": True,
        "environment": "staging",
        "pv_kwp": 7.5,
    }
    assert calls and calls[0][2] == SECRET
    assert SECRET not in str(entry.options)
    assert SECRET not in str(entry.data)
    assert SECRET not in caplog.text


async def test_already_registered_site_ignores_the_secret_field(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("register() must not be called when already registered")

    monkeypatch.setattr(config_flow, "register", _fail_if_called)
    entry = _entry(hass)
    directory = environment_dir(hass, entry.entry_id, "staging")
    directory.mkdir(parents=True)
    (directory / "site.json").write_text('{"site_id": "abc"}')

    result = await _open_energy_atlas(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": True, "environment": "staging", "pv_kwp": 3.0},
    )
    assert result["type"] == "create_entry"
    assert entry.options["atlas"]["enabled"] is True


async def test_atlas_survives_a_preview_save(
    recorder_mock, hass, enable_custom_integrations
):
    entry = _entry(
        hass, atlas={"enabled": True, "environment": "staging", "pv_kwp": 5.0}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "preview"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    assert result["type"] == "create_entry"
    assert entry.options["atlas"] == {
        "enabled": True,
        "environment": "staging",
        "pv_kwp": 5.0,
    }


async def test_atlas_survives_reconfigure(
    recorder_mock, hass, enable_custom_integrations
):
    entry = _entry(
        hass, atlas={"enabled": True, "environment": "staging", "pv_kwp": 5.0}
    )
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "preview"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "confirm": True,
        },
    )
    assert result["type"] == "abort"
    assert entry.options.get("atlas") == {
        "enabled": True,
        "environment": "staging",
        "pv_kwp": 5.0,
    }
