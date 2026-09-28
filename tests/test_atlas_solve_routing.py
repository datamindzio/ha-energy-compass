"""Coordinator <-> AtlasBridge solve routing (ADR-0019 §6).

Only the coordinator's actual publish path may feed the sink or count a skip: a
result superseded by a fresher epoch was never published, so Atlas must not see
it either; a solve that failed (`InputError`/`SolveError`), even after the payload
builder already ran, must count as skipped and never be sent.
"""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass import coordinator as coordinator_module
from custom_components.energy_compass.engine.models import InputError, SolveError
from custom_components.energy_compass.settings import default_configuration


class _FakeAtlas:
    def __init__(self, solve_builder=None):
        self.sent = []
        self.solves_skipped = 0
        self.sink = object()  # registered: the coordinator only passes the builder then
        self.solve_builder = solve_builder or (lambda *a: None)

    def add_solve(self, payload):
        self.sent.append(payload)

    def skip_solve(self):
        self.solves_skipped += 1

    async def async_stop(self) -> None:
        pass


def _config():
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    return config


async def _setup(hass):
    entry = MockConfigEntry(domain="energy_compass", data=_config(), version=3)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done()
    return entry


async def test_input_error_is_not_sent_and_counts_a_skip(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    coordinator.atlas = _FakeAtlas()

    def fake_compute(*args, **kwargs):
        raise InputError("boom")

    monkeypatch.setattr(coordinator_module, "compute", fake_compute)
    await coordinator.async_recalculate()

    assert coordinator.atlas.sent == []
    assert coordinator.atlas.solves_skipped == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_solve_error_after_the_builder_ran_is_not_sent_and_counts_a_skip(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    """A `SolveError` raised after the payload builder ran (e.g. flexible-load
    analysis overrunning its deadline -- `runtime.compute()`'s own
    `raise SolveError("timeout")` after the atlas builder call) must still count
    as skipped, never sent: the payload never survives the exception to reach
    `compute()`'s return value."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    builder_calls = []
    coordinator.atlas = _FakeAtlas(
        solve_builder=lambda *a: builder_calls.append(1) or {"marker": "late-failure"}
    )

    def fake_compute(*args, **kwargs):
        kwargs["atlas_solve_builder"](None, None, None, {}, {}, None)
        raise SolveError("timeout")

    monkeypatch.setattr(coordinator_module, "compute", fake_compute)
    await coordinator.async_recalculate()

    assert builder_calls == [1]
    assert coordinator.atlas.sent == []
    assert coordinator.atlas.solves_skipped == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_superseded_epoch_discards_the_solve_without_a_send_or_skip(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    coordinator.atlas = _FakeAtlas(solve_builder=lambda *a: {"marker": "published"})
    real_compute = coordinator_module.compute
    calls = []

    def fake_compute(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            # A configuration change lands while this solve is still running:
            # the epoch this generation started with is now stale, so its
            # result (and any Atlas payload) must be discarded, not sent.
            coordinator._epoch += 1
            coordinator._generation += 1
            return {"atlas_solve_payload": {"marker": "discarded"}}
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(coordinator_module, "compute", fake_compute)
    await coordinator.async_recalculate()

    assert coordinator.atlas.sent == [{"marker": "published"}]
    assert coordinator.atlas.solves_skipped == 0
    assert await hass.config_entries.async_unload(entry.entry_id)
