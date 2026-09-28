"""Off path (ADR-0019 §3): default options behave exactly as Atlas-absent Compass."""

import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.atlas.storage import entry_dir
from custom_components.energy_compass.runtime import compute
from custom_components.energy_compass.settings import default_configuration


def test_config_flow_module_never_imports_atlas_at_top_level():
    """`config_flow` is eagerly imported by HA for any config-flow integration
    (ADR-0019 §3): a fresh interpreter that only imports it must never pull in
    `.atlas`/`.atlas_sink` as a side effect (both stay inside the
    `async_step_energy_atlas` method body)."""
    repo_root = Path(__file__).resolve().parent.parent
    script = (
        "import sys\n"
        "import custom_components.energy_compass.config_flow\n"
        "prefixes = ("
        "'custom_components.energy_compass.atlas.', "
        "'custom_components.energy_compass.atlas_sink', "
        ")\n"
        "leaked = [m for m in sys.modules "
        "if m == 'custom_components.energy_compass.atlas' "
        "or m.startswith(prefixes)]\n"
        "assert not leaked, leaked\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_compute_result_key_set_is_unchanged_when_atlas_is_off():
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
    # `atlas_solve_payload` is returned beside the result only when Atlas is
    # enabled (a builder was passed); the off-path key set never gains it.
    assert "atlas_solve_payload" not in baseline
    assert set(with_builder.keys()) - set(baseline.keys()) == {"atlas_solve_payload"}


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
