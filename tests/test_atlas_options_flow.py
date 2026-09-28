"""Options step `energy_atlas` (ADR-0019 §2/§4, amendment T-411 §B): baked-in credential,
the §B2 form-error table, WARNING-per-failure log hygiene, and settings preservation.
"""

import functools
import logging

import httpx
import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.atlas import bridge as bridge_module
from custom_components.energy_compass.atlas.storage import environment_dir
from custom_components.energy_compass.atlas_env import ENROLLMENT_SECRETS
from custom_components.energy_compass.atlas_sink import sink as atlas_sink_module
from custom_components.energy_compass.atlas_sink.identity import Identity
from custom_components.energy_compass.atlas_sink.sink import (
    RegistrationError,
    SinkThread,
)
from custom_components.energy_compass.settings import default_configuration

STAGING_SECRET = ENROLLMENT_SECRETS["staging"]


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


# ADR-0019 §B2 form-error table, every row that is a registration failure (the 201/200
# row saves; the "no baked secret" row is covered separately below, it never calls
# register()). `status` is the RegistrationError.status this row is raised with.
_FAILURE_ROWS = [
    ("invalid_enrollment_secret", "enrollment_rejected", 401),
    ("site_key_revoked", "site_key_revoked", 409),
    ("site_conflict", "site_conflict", 409),
    ("rate_limited", "rate_limited", 429),
    ("cannot_connect", "cannot_connect", None),
    ("unknown", "unknown", 599),
]


@pytest.mark.parametrize("kind, form_error, status", _FAILURE_ROWS)
async def test_registration_failure_kinds_map_to_form_errors_and_log_one_warning(
    recorder_mock,
    hass,
    enable_custom_integrations,
    monkeypatch,
    caplog,
    kind,
    form_error,
    status,
):
    def _raise(*args, **kwargs):
        raise RegistrationError(kind, status)

    monkeypatch.setattr(atlas_sink_module, "register", _raise)
    entry = _entry(hass)
    result = await _open_energy_atlas(hass, entry)
    with caplog.at_level(logging.WARNING):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"enabled": True, "environment": "staging", "pv_kwp": 5.0},
        )
    assert result["step_id"] == "energy_atlas"
    assert result["errors"] == {"base": form_error}
    assert "atlas" not in entry.options

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "custom_components.energy_compass.config_flow"
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "staging" in message
    assert form_error in message
    assert (str(status) if status is not None else "-") in message
    assert STAGING_SECRET not in caplog.text
    assert "site_id" not in caplog.text
    assert "https://" not in caplog.text


async def test_no_baked_secret_for_environment_is_environment_unavailable(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, caplog
):
    # production has no baked secret (ADR-0019 amendment T-411 §B1): the form must
    # reject before any HTTP attempt, never calling register().
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("register() must not be called with no baked secret")

    monkeypatch.setattr(atlas_sink_module, "register", _fail_if_called)
    entry = _entry(hass)
    result = await _open_energy_atlas(hass, entry)
    with caplog.at_level(logging.WARNING):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"enabled": True, "environment": "production", "pv_kwp": 5.0},
        )
    assert result["errors"] == {"base": "environment_unavailable"}
    assert "atlas" not in entry.options
    directory = environment_dir(hass, entry.entry_id, "production")
    assert not (directory / "site.json").exists()

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "custom_components.energy_compass.config_flow"
    ]
    assert len(warnings) == 1
    assert "environment_unavailable" in warnings[0].getMessage()


async def test_successful_registration_saves_with_the_baked_secret_and_never_logs_it(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, caplog
):
    calls = []

    def _register(directory, base_url, secret, timeout_s=30, **kwargs):
        calls.append((directory, base_url, secret))
        from pathlib import Path

        # Like the real register(): the key is persisted before site.json, so the
        # sink started by the save-triggered reload finds a complete identity.
        Identity(Path(directory))._ensure_key()
        (Path(directory) / "site.json").write_text('{"site_id": "abc"}')
        return "abc"

    monkeypatch.setattr(atlas_sink_module, "register", _register)
    # The save reloads the entry and the genuine SinkThread starts on the registered
    # identity. Keep it off the network (no DNS in tests) and make it stop promptly on
    # unload instead of after its default 30 s tick.
    monkeypatch.setattr(
        bridge_module,
        "SinkThread",
        functools.partial(
            SinkThread,
            tick_s=0.05,
            transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        ),
    )
    entry = _entry(hass)
    with caplog.at_level(logging.DEBUG):
        result = await _open_energy_atlas(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"enabled": True, "environment": "staging", "pv_kwp": 7.5},
        )
    assert result["type"] == "create_entry"
    assert entry.options["atlas"] == {
        "enabled": True,
        "environment": "staging",
        "pv_kwp": 7.5,
    }
    assert calls and calls[0][2] == STAGING_SECRET
    assert STAGING_SECRET not in str(entry.options)
    assert STAGING_SECRET not in str(entry.data)
    assert STAGING_SECRET not in caplog.text
    # The save reloads the not-loaded entry (ADR-0019 §B3): let that setup and its
    # first solve finish, then unload, so no Store write timer outlives the test.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert "sink IO loop crashed" not in caplog.text


async def test_form_has_no_secret_field(
    recorder_mock, hass, enable_custom_integrations
):
    entry = _entry(hass)
    result = await _open_energy_atlas(hass, entry)
    keys = {str(key) for key in result["data_schema"].schema}
    assert "enrollment_secret" not in keys


async def test_already_registered_site_does_not_register_again(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("register() must not be called when already registered")

    monkeypatch.setattr(atlas_sink_module, "register", _fail_if_called)
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
    # The save reloads the not-loaded entry (ADR-0019 §B3): let that setup and its
    # first solve finish, then unload, so no Store write timer outlives the test.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_live_apply_start_failure_shows_unknown_and_does_not_save(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, caplog
):
    """ADR-0019 §B3 (amendment T-411): a bridge start failure during the live
    apply logs exactly one WARNING (`coordinator.async_apply_atlas`'s own, not a
    second one from the options step) and shows form error `unknown`; nothing is
    saved and `coordinator.atlas` stays `None` for a later save/restart to retry.
    """
    from custom_components.energy_compass.atlas import bridge as bridge_module

    async def _raise(self, attrs):
        raise RuntimeError("boom")

    entry = _entry(hass)
    directory = environment_dir(hass, entry.entry_id, "staging")
    directory.mkdir(parents=True)
    (directory / "site.json").write_text('{"site_id": "site-1"}')
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    monkeypatch.setattr(bridge_module.AtlasBridge, "async_start", _raise)
    with caplog.at_level(logging.DEBUG):
        result = await _open_energy_atlas(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"enabled": True, "environment": "staging", "pv_kwp": 5.0},
        )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "unknown"}
    assert "atlas" not in entry.options
    assert entry.runtime_data.atlas is None

    # Own records only (ADR-0019 amendment T-401 hygiene scope): HA's loader
    # logs its own "custom integration not tested" WARNING on first load.
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name.startswith("custom_components.energy_compass")
    ]
    assert len(warnings) == 1
    assert warnings[0].name == "custom_components.energy_compass.coordinator"
    assert await hass.config_entries.async_unload(entry.entry_id)


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
    # The save reloads the not-loaded entry (ADR-0019 §B3): let that setup and its
    # first solve finish, then unload, so no Store write timer outlives the test.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await hass.config_entries.async_unload(entry.entry_id)


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


async def test_pv_kwp_has_no_default_and_is_required_when_enabled(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    # ADR-0019 §2: pv_kwp default none, required when enabled.
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("register() must not be called without pv_kwp")

    monkeypatch.setattr(atlas_sink_module, "register", _fail_if_called)
    entry = _entry(hass)
    result = await _open_energy_atlas(hass, entry)
    (pv_kwp,) = [key for key in result["data_schema"].schema if key == "pv_kwp"]
    assert pv_kwp.default is vol.UNDEFINED
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": True, "environment": "staging"},
    )
    assert result["errors"] == {"pv_kwp": "invalid_input"}
    assert "atlas" not in entry.options


async def test_disabled_without_pv_kwp_saves(
    recorder_mock, hass, enable_custom_integrations
):
    entry = _entry(hass)
    result = await _open_energy_atlas(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled": False, "environment": "staging"}
    )
    assert result["type"] == "create_entry"
    assert entry.options["atlas"] == {"enabled": False, "environment": "staging"}
