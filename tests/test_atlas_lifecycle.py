"""AtlasBridge lifecycle, environment switch and diagnostics (ADR-0019 §5/§9).

`SinkThread` is replaced by a test double for most of this file: what the glue owns and
must prove there is that it calls `stop()` with the ADR's timeouts at the right lifecycle
point, it never starts a thread/import when not registered, and diagnostics/status never
carry secrets. `atlas_sink` itself is vendored into this repo without its own test suite
(only `tests/atlas_contract/`), so the hang-bounded shutdown it's built on (ADR-0019 §9) is
proven for real, against the genuine `SinkThread`, at the bottom of this file instead.
"""

import asyncio
import logging
import threading
import time
from typing import ClassVar

import httpx
import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.atlas import bridge as bridge_module
from custom_components.energy_compass.atlas.storage import environment_dir
from custom_components.energy_compass.atlas_sink.sink import SinkThread, register
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
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()
    assert fake_sink[0].started

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert fake_sink[0].stop_calls == [10]


async def test_hass_stop_event_stops_the_sink_with_a_five_second_bound(
    recorder_mock, hass, enable_custom_integrations, fake_sink
):
    entry = _registered_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert fake_sink[0].stop_calls == [5]


async def test_first_solve_after_setup_carries_the_payload_builder(
    recorder_mock, hass, enable_custom_integrations, fake_sink, monkeypatch
):
    # The bridge is attached before coordinator.async_start() dispatches the first solve.
    from custom_components.energy_compass import coordinator as coordinator_module

    builders = []
    real_compute = coordinator_module.compute

    def _spy(*args, atlas_solve_builder=None, **kwargs):
        builders.append(atlas_solve_builder)
        return real_compute(*args, atlas_solve_builder=atlas_solve_builder, **kwargs)

    monkeypatch.setattr(coordinator_module, "compute", _spy)
    entry = _registered_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    # ADR-0019 §B4 (T-411): the first solve is backgrounded, so it must be waited
    # for explicitly here.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert builders and builders[0] is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


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
        await hass.async_block_till_done(wait_background_tasks=True)
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
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    section = diagnostics["energy_atlas"]
    assert section["enabled"] is True
    assert section["environment"] == "staging"
    assert section["sink"]["registered"] is True
    assert "site_id" not in section["sink"]
    assert "site-1" not in str(section)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_saving_atlas_settings_does_not_reload_the_entry(
    recorder_mock, hass, enable_custom_integrations, fake_sink, monkeypatch
):
    """ADR-0019 §B3 (amendment T-411): the Atlas step applies live, no reload —
    `async_reload`/setup does not run again, the coordinator object and every
    entity's state/last_changed are unchanged, and no new solve is dispatched."""
    from custom_components.energy_compass import coordinator as coordinator_module

    reload_calls = []
    real_reload = hass.config_entries.async_reload

    async def _spy_reload(entry_id):
        reload_calls.append(entry_id)
        return await real_reload(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", _spy_reload)

    recalculate_calls = []
    real_recalculate = coordinator_module.EnergyCompassCoordinator.async_recalculate

    async def _spy_recalculate(self):
        recalculate_calls.append(1)
        return await real_recalculate(self)

    monkeypatch.setattr(
        coordinator_module.EnergyCompassCoordinator,
        "async_recalculate",
        _spy_recalculate,
    )

    entry = _registered_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    recalculate_calls.clear()  # drop the initial setup solve
    before = {
        state.entity_id: (state.state, state.last_changed)
        for state in hass.states.async_all()
    }

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "energy_atlas"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled": True, "environment": "staging", "pv_kwp": 6.0}
    )
    assert result["type"] == "create_entry", result.get("errors")
    await hass.async_block_till_done()

    assert reload_calls == []
    assert recalculate_calls == []
    assert entry.runtime_data is coordinator
    after = {
        state.entity_id: (state.state, state.last_changed)
        for state in hass.states.async_all()
    }
    assert after == before
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_saving_atlas_settings_on_a_not_loaded_entry_retries_setup_via_reload(
    recorder_mock, hass, enable_custom_integrations, fake_sink, monkeypatch
):
    """ADR-0019 §B3 (amendment T-411): "not-loaded entry -> normal reload". A
    not-loaded entry (here: unloaded) must NOT go through the live-apply path —
    `runtime_data`/`coordinator.atlas` are never cleared on unload, so their mere
    presence is not "loaded"; only `config_entry.state` is. The save must instead
    keep the default `automatic_reload = True` and let the reload retry setup,
    rather than calling `async_apply_atlas` on a stale coordinator and starting an
    orphan sink thread for an entry that isn't set up."""
    from homeassistant.config_entries import ConfigEntryState

    from custom_components.energy_compass import coordinator as coordinator_module

    entry = _registered_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED

    apply_calls = []
    real_apply = coordinator_module.EnergyCompassCoordinator.async_apply_atlas

    async def _spy_apply(self, new_settings):
        apply_calls.append(new_settings)
        return await real_apply(self, new_settings)

    monkeypatch.setattr(
        coordinator_module.EnergyCompassCoordinator, "async_apply_atlas", _spy_apply
    )

    reload_calls = []
    real_reload = hass.config_entries.async_reload

    async def _spy_reload(entry_id):
        reload_calls.append(entry_id)
        return await real_reload(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", _spy_reload)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "energy_atlas"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled": True, "environment": "staging", "pv_kwp": 6.0}
    )
    assert result["type"] == "create_entry", result.get("errors")
    await hass.async_block_till_done(wait_background_tasks=True)

    assert apply_calls == []
    assert reload_calls == [entry.entry_id]
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_does_not_block_on_the_first_solve(
    recorder_mock, hass, enable_custom_integrations, fake_sink, monkeypatch
):
    """ADR-0019 §B4 (amendment T-411): `async_setup_entry` returns and the entry is
    LOADED with entities present (`status: calculating`) while the first solve is
    still blocked; releasing the block publishes the plan; an unload started while
    the solve is still blocked waits for it and publishes nothing."""
    import threading

    from homeassistant.config_entries import ConfigEntryState

    from custom_components.energy_compass import coordinator as coordinator_module

    block = threading.Event()
    real_compute = coordinator_module.compute

    def _blocking_compute(*args, **kwargs):
        block.wait()
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(coordinator_module, "compute", _blocking_compute)

    entry = _registered_entry(hass)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        status = hass.states.get("sensor.mock_title_optimizer_status")
        assert status is not None
        assert status.state == "calculating"
    finally:
        # Release the blocked executor thread on any assertion failure too:
        # otherwise fixture teardown hangs forever waiting for it (ADR-0019
        # §B4's own "unload waits for the in-flight solve" guarantee).
        block.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    status = hass.states.get("sensor.mock_title_optimizer_status")
    assert status.state != "calculating"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_during_a_blocked_first_solve_waits_and_publishes_nothing(
    recorder_mock, hass, enable_custom_integrations, fake_sink, monkeypatch
):
    import threading

    from custom_components.energy_compass import coordinator as coordinator_module

    block = threading.Event()
    real_compute = coordinator_module.compute
    entered = threading.Event()

    def _blocking_compute(*args, **kwargs):
        entered.set()
        block.wait()
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(coordinator_module, "compute", _blocking_compute)

    entry = _registered_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data
    # Wait for the executor job to actually reach the blocking compute before
    # unloading, matching "unload during the blocked first solve".
    for _ in range(200):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()
    try:
        assert coordinator.data.get("status") == "calculating"

        unload_task = hass.async_create_task(
            hass.config_entries.async_unload(entry.entry_id)
        )
        await asyncio.sleep(0.05)
        assert not unload_task.done()  # genuinely waiting on the blocked worker
    finally:
        # Release the blocked executor thread on any assertion failure too:
        # otherwise fixture teardown hangs forever waiting for it.
        block.set()
    assert await unload_task

    # The now-released compute must not have published through a closed coordinator.
    assert coordinator.data.get("status") == "calculating"


async def test_environment_switch_keeps_separate_directories_and_no_reregistration(
    recorder_mock, hass, enable_custom_integrations, fake_sink, monkeypatch
):
    from custom_components.energy_compass.atlas_sink import sink as atlas_sink_module

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("register() must not run for an already-registered env")

    monkeypatch.setattr(atlas_sink_module, "register", _fail_if_called)

    entry = _registered_entry(hass, environment="staging")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
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


async def test_environment_switch_rows_1_to_4(
    recorder_mock, hass, enable_custom_integrations, fake_sink, monkeypatch
):
    """ADR-0019 §5 environment switch table, rows 1-4, walked as one user journey:
    enabling staging registers it with the baked secret (row 1); switching to
    production registers production (also baked) and stops the staging sink, live,
    without a reload (row 2, amendment T-411 §B3); switching back to the
    already-registered staging resumes it without calling `register()` again and
    stops production (row 3); disabling starts no sink and keeps both directories
    (row 4)."""
    from custom_components.energy_compass.atlas_env import ENROLLMENT_SECRETS
    from custom_components.energy_compass.atlas_sink import sink as atlas_sink_module

    # production has no baked secret by default (ADR-0019 amendment T-411 §B1); give
    # it one here so this test can walk the full row-2 switch like staging.
    monkeypatch.setitem(ENROLLMENT_SECRETS, "production", "p1")

    register_calls = []

    def _register(directory, base_url, secret):
        register_calls.append((str(directory), base_url, secret))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "site.json").write_text('{"site_id": "site-x"}')
        return "site-x"

    monkeypatch.setattr(atlas_sink_module, "register", _register)

    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(domain="energy_compass", data=config, options={}, version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()
    assert fake_sink == []

    async def _configure(**fields):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "energy_atlas"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], fields
        )
        assert result["type"] == "create_entry", result.get("errors")
        await hass.async_block_till_done()

    staging_dir = environment_dir(hass, entry.entry_id, "staging")
    production_dir = environment_dir(hass, entry.entry_id, "production")

    # Row 1: enable staging, not yet registered -> registers with the baked secret,
    # sink starts there.
    await _configure(enabled=True, environment="staging", pv_kwp=5.0)
    assert register_calls == [
        (
            str(staging_dir),
            "https://atlas-api-staging.datamindz.io",
            ENROLLMENT_SECRETS["staging"],
        )
    ]
    assert staging_dir.exists()
    assert len(fake_sink) == 1
    assert fake_sink[0].started
    assert entry.runtime_data.atlas.environment == "staging"

    # Row 2: switch to production -> registers production, stops staging live (no
    # reload: same coordinator object, same entity states unaffected by a reload).
    coordinator = entry.runtime_data
    await _configure(enabled=True, environment="production", pv_kwp=5.0)
    assert entry.runtime_data is coordinator
    assert len(register_calls) == 2
    assert register_calls[1] == (
        str(production_dir),
        "https://atlas-api.datamindz.io",
        "p1",
    )
    assert production_dir.exists()
    assert fake_sink[0].stop_calls == [10]  # staging sink stopped live
    assert len(fake_sink) == 2
    assert fake_sink[1].started
    assert entry.runtime_data.atlas.environment == "production"

    # Row 3: switch back to the already-registered staging -> no new register(),
    # staging resumes, production stops.
    await _configure(enabled=True, environment="staging", pv_kwp=5.0)
    assert len(register_calls) == 2  # unchanged: staging was already registered
    assert fake_sink[1].stop_calls == [10]  # production sink stopped live
    assert len(fake_sink) == 3
    assert fake_sink[2].started
    assert entry.runtime_data.atlas.environment == "staging"

    # Row 4: disable -> no sink started, both directories kept.
    await _configure(enabled=False, environment="staging", pv_kwp=5.0)
    assert len(fake_sink) == 3  # no fourth SinkThread constructed
    assert fake_sink[2].stop_calls == [10]  # the running staging sink stopped
    assert entry.runtime_data.atlas is None
    assert staging_dir.exists()
    assert production_dir.exists()

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_remove_entry_deletes_every_environment_directory(
    recorder_mock, hass, enable_custom_integrations, fake_sink
):
    entry = _registered_entry(hass)
    production_dir = environment_dir(hass, entry.entry_id, "production")
    production_dir.mkdir(parents=True)
    (production_dir / "site.json").write_text('{"site_id": "site-2"}')
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    from custom_components.energy_compass.atlas.storage import entry_dir

    assert not entry_dir(hass, entry.entry_id).exists()


def _register_ok(request):
    return httpx.Response(201, json={"site_id": "site-1"})


@pytest.mark.parametrize("timeout_s", [5, 10])
def test_real_sink_thread_stops_within_its_bound_despite_a_hanging_transport(
    tmp_path, timeout_s
):
    """ADR-0019 §9: `stop()` must return within its bound (the HA-stop 5s and the
    unload 10s call sites) even if the transport never reaches a completed await.
    This drives the genuine vendored `SinkThread`, not the test double used above.

    `stop()`'s own bound (asserted below) cannot interrupt a truly hung transport
    (ADR-0019 §9: the IO thread may be left running in the background); `release`
    lets this specific request resolve only *after* that assertion, purely so the
    abandoned thread doesn't outlive the test and trip the test harness's leftover
    -thread check.
    """
    release = threading.Event()

    async def _hang_until_released(request):
        while not release.is_set():
            await asyncio.sleep(0.05)
        return httpx.Response(200, json={})

    register(
        tmp_path,
        "https://atlas-api-staging.example.invalid",
        "secret",
        transport=httpx.MockTransport(_register_ok),
    )
    sink = SinkThread(
        tmp_path,
        "https://atlas-api-staging.example.invalid",
        {"pv_kwp": 5.0},
        transport=httpx.MockTransport(_hang_until_released),
        tick_s=0.01,
    )
    sink.start()
    time.sleep(0.2)  # let the IO loop reach the hung request
    started = time.monotonic()
    sink.stop(timeout_s)
    elapsed = time.monotonic() - started

    assert elapsed < timeout_s + 0.5
    assert not sink.is_alive()

    release.set()
    for _ in range(100):
        if not any(t.name == "atlas-sink-io" for t in threading.enumerate()):
            break
        time.sleep(0.05)
