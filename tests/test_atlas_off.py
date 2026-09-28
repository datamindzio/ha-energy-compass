"""Off path (ADR-0019 §3): default options behave exactly as Atlas-absent Compass."""

import threading
from datetime import UTC, datetime

import httpx
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.atlas.storage import entry_dir
from custom_components.energy_compass.runtime import compute
from custom_components.energy_compass.settings import default_configuration


def test_compute_result_key_set_is_identical_with_and_without_a_builder():
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    baseline = compute(config, {}, now)
    called = []
    with_builder = compute(
        config, {}, now, atlas_solve_builder=lambda *a: called.append(a) or None
    )
    assert called  # the builder really ran
    assert set(baseline.keys()) == set(with_builder.keys())


async def test_default_options_start_no_thread_no_storage_no_network(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    def _boom(*args, **kwargs):
        raise AssertionError("httpx must never be called when Atlas is disabled")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _boom)

    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    entry = MockConfigEntry(domain="energy_compass", data=config, version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.atlas is None
    assert not any(t.name == "atlas-sink" for t in threading.enumerate())
    assert not entry_dir(hass, entry.entry_id).exists()

    assert await hass.config_entries.async_unload(entry.entry_id)
