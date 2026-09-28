"""Options step `energy_atlas_proof` (ADR-0019 §7): menu visibility, JWS shape, hygiene."""

import base64
import functools
import json
import logging

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.atlas.storage import (
    environment_dir,
    forget_environment,
)
from custom_components.energy_compass.atlas_sink.identity import Identity
from custom_components.energy_compass.atlas_sink.sink import register
from custom_components.energy_compass.settings import DOMAIN, default_configuration


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _register_ok(request):
    return httpx.Response(201, json={"site_id": "site-proof-1"})


def _entry(hass, atlas=None):
    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        options={"atlas": atlas} if atlas else {},
        version=2,
    )
    entry.add_to_hass(hass)
    return entry


async def _register_site(hass, entry, environment="staging"):
    directory = environment_dir(hass, entry.entry_id, environment)
    # `register()` runs its own private event loop (asyncio.run); off the hass loop.
    await hass.async_add_executor_job(
        functools.partial(
            register,
            directory,
            "https://atlas-api-staging.example.invalid",
            "secret",
            transport=httpx.MockTransport(_register_ok),
        )
    )
    return directory


async def test_proof_menu_entry_hidden_when_disabled(
    recorder_mock, hass, enable_custom_integrations
):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "energy_atlas_proof" not in result["menu_options"]


async def test_proof_menu_entry_hidden_when_enabled_but_not_registered(
    recorder_mock, hass, enable_custom_integrations
):
    entry = _entry(
        hass, atlas={"enabled": True, "environment": "staging", "pv_kwp": 5.0}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "energy_atlas_proof" not in result["menu_options"]


async def test_proof_menu_entry_shown_when_enabled_and_registered(
    recorder_mock, hass, enable_custom_integrations
):
    entry = _entry(
        hass, atlas={"enabled": True, "environment": "staging", "pv_kwp": 5.0}
    )
    await _register_site(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "energy_atlas_proof" in result["menu_options"]


async def test_proof_jws_verifies_and_shape_is_correct(
    recorder_mock, hass, enable_custom_integrations, caplog
):
    entry = _entry(
        hass, atlas={"enabled": True, "environment": "staging", "pv_kwp": 5.0}
    )
    directory = await _register_site(hass, entry)
    identity = Identity(directory)
    public_key = Ed25519PublicKey.from_public_bytes(_b64u_decode(identity.public_key))

    with caplog.at_level(logging.DEBUG):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "energy_atlas_proof"}
        )
        assert result["step_id"] == "energy_atlas_proof"
        jws = result["description_placeholders"]["proof"]

    header_b64, claims_b64, sig_b64 = jws.split(".")
    header = json.loads(_b64u_decode(header_b64))
    claims = json.loads(_b64u_decode(claims_b64))
    signature = _b64u_decode(sig_b64)
    public_key.verify(signature, f"{header_b64}.{claims_b64}".encode())

    assert header == {"alg": "EdDSA", "typ": "JWT", "kid": "site-proof-1"}
    assert claims["sub"] == "site-proof-1"
    assert claims["exp"] - claims["iat"] == 900
    assert jws not in caplog.text

    # Stores nothing: the entry's Atlas settings are exactly what they were before.
    assert entry.options["atlas"] == {
        "enabled": True,
        "environment": "staging",
        "pv_kwp": 5.0,
    }


async def test_proof_step_aborts_when_site_forgotten_after_menu_render(
    recorder_mock, hass, enable_custom_integrations
):
    """The site can be forgotten (another options flow, another tab) between the menu
    render and this step being submitted; `proof()` would otherwise raise ValueError
    (no site_id) as an unhandled exception."""
    entry = _entry(
        hass, atlas={"enabled": True, "environment": "staging", "pv_kwp": 5.0}
    )
    await _register_site(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "energy_atlas_proof" in result["menu_options"]

    forget_environment(hass, entry.entry_id, "staging")

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "energy_atlas_proof"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "site_not_registered"


async def test_proof_step_closes_back_to_the_menu(
    recorder_mock, hass, enable_custom_integrations
):
    entry = _entry(
        hass, atlas={"enabled": True, "environment": "staging", "pv_kwp": 5.0}
    )
    await _register_site(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "energy_atlas_proof"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == "menu"
