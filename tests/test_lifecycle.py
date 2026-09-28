from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass import async_migrate_entry
from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.runtime import build_problem, compute
from custom_components.energy_compass.settings import default_configuration


@pytest.fixture(autouse=True)
def aligned_clock(freezer):
    freezer.move_to("2026-09-17T10:00:00+00:00")


def test_grid_only_snapshot_and_budget():
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
        grid_import_kw=5,
    )
    now = datetime(2026, 9, 17, tzinfo=UTC)
    problem, _, _ = build_problem(config, {}, now)
    assert problem.battery is None
    assert len(problem.slots) == 2
    result = compute(config, {}, now)
    assert result["generated_at"] == now.isoformat()
    assert result["status"] == "ready"
    assert len(result["outlook"]) == 2
    assert result["calibration"] == "unvalidated"


def test_missing_enabled_pv_is_invalid():
    config = default_configuration("EUR", "UTC")
    config["sources"]["pv"]["enabled"] = True
    with pytest.raises(InputError):
        build_problem(config, {}, datetime.now(UTC))


async def test_migration_preserves_choices(hass):
    config = default_configuration("EUR", "UTC")
    config["settings"]["operating_floor"] = 20
    entry = MockConfigEntry(domain="energy_compass", data=config, version=1)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 3
    assert entry.data["settings"]["operating_floor"] == 20
    assert entry.data["settings"]["strategy"] == "cost_min"


@pytest.mark.parametrize(
    "path,policy",
    [
        ("last_updated", None),
        ("attributes.reported_at", None),
        ("last_updated", "exact_path"),
    ],
)
async def test_version_one_migration_retains_soc_timestamp_paths_in_data_and_options(
    hass, path, policy
):
    config = default_configuration("EUR", "UTC")
    config["soc_options"] = {"timestamp_path": path, "bms_timestamp_path": path}
    if policy:
        config["soc_options"].update(
            timestamp_policy=policy, bms_timestamp_policy=policy
        )
    options_config = deepcopy(config)
    entry = MockConfigEntry(
        domain="energy_compass",
        data=config,
        options={"configuration": options_config},
        version=1,
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    for stored in (entry.data, entry.options["configuration"]):
        assert stored["soc_options"]["timestamp_path"] == path
        assert stored["soc_options"]["bms_timestamp_path"] == path
        assert stored["soc_options"]["timestamp_policy"] == (policy or "auto")
        assert stored["soc_options"]["bms_timestamp_policy"] == (policy or "auto")


async def test_two_entries_unload(recorder_mock, hass, enable_custom_integrations):
    service_calls = []
    unsubscribe = hass.bus.async_listen(
        "call_service", lambda event: service_calls.append(event.data)
    )
    entries = []
    for title in ["First", "Second"]:
        config = default_configuration("EUR", "UTC")
        config["settings"].update(
            horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
        )
        entry = MockConfigEntry(
            domain="energy_compass", data=config, title=title, version=2
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        entries.append(entry)
    await hass.async_block_till_done()
    assert entries[0].runtime_data is not entries[1].runtime_data
    await entries[1].runtime_data.async_recalculate()
    stopped = entries[0].runtime_data
    assert await hass.config_entries.async_unload(entries[0].entry_id)
    await hass.async_block_till_done()
    assert all(
        state.state == "unavailable"
        for state in hass.states.async_all()
        if state.entity_id.startswith("sensor.first_")
    )
    assert stopped._closed
    assert stopped._source_unsub is stopped._registry_unsub is None
    assert stopped._boundary is stopped._debounce is None
    assert await hass.config_entries.async_remove(entries[0].entry_id)
    await hass.async_block_till_done()
    assert not [
        state
        for state in hass.states.async_all()
        if state.entity_id.startswith("sensor.first_")
    ]
    assert hass.states.get("sensor.second_consumption_compass")
    assert await hass.config_entries.async_unload(entries[1].entry_id)
    unsubscribe()
    assert service_calls == []


def test_native_settlement_boundaries_are_preserved():
    from custom_components.energy_compass.config_models import PriceSource
    from custom_components.energy_compass.sources.bindings import (
        EntityBinding,
        IntervalBinding,
    )

    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    binding = IntervalBinding(
        EntityBinding("sensor.market", attribute="rows"),
        start_path="start",
        end_path="end",
        value_path="price",
        unit="EUR/kWh",
        value_kind="price",
    )
    config["sources"]["buy"] = PriceSource("forecast", (binding,)).to_dict()
    now = datetime(2026, 9, 17, tzinfo=UTC)
    rows = [
        {
            "start": f"2026-09-17T00:{i:02d}:00+00:00",
            "end": f"2026-09-17T00:{i + 15:02d}:00+00:00"
            if i < 45
            else "2026-09-17T01:00:00+00:00",
            "price": i / 100,
        }
        for i in (0, 15, 30, 45)
    ]
    problem, _, _ = build_problem(
        config,
        {
            "sensor.market": {
                "state": "ok",
                "attributes": {"rows": rows},
                "last_updated": now,
            }
        },
        now,
    )
    assert len(problem.slots) == 4
    assert [s.buy_per_kwh for s in problem.slots] == [0, 0.15, 0.30, 0.45]


async def test_source_disappears_and_recovers(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
        debounce_seconds=0,
    )
    config["helpers"] = {
        "grid_import_kw": {
            "fixed": None,
            "entity": {"entity_id": "input_number.limit"},
            "unit": "kW",
            "max_age_seconds": None,
        }
    }
    hass.states.async_set("input_number.limit", "5", {"unit_of_measurement": "kW"})
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Synthetic", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.synthetic_forecast_valid").state == "on", (
        entry.runtime_data.data
    )
    hass.states.async_set("input_number.limit", "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.synthetic_forecast_valid").state == "on"
    assert hass.states.get("binary_sensor.synthetic_alert").state == "on"
    assert (
        hass.states.get("sensor.synthetic_consumption_compass").state != "unavailable"
    )
    hass.states.async_set("input_number.limit", "5", {"unit_of_measurement": "kW"})
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.synthetic_forecast_valid").state == "on", (
        entry.runtime_data.data
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_solver_exception_is_diagnostic(
    recorder_mock, hass, enable_custom_integrations
):
    from custom_components.energy_compass.engine.models import SolveError

    config = default_configuration("EUR", "UTC")
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Synthetic", version=2
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.energy_compass.runtime.solve",
        side_effect=SolveError("timeout"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.synthetic_optimizer_status").state == "timeout"
        assert (
            hass.states.get("sensor.synthetic_consumption_compass").state
            == "unavailable"
        )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_rapid_updates_serialize_jobs_and_discard_old_generation(
    recorder_mock, hass, enable_custom_integrations
):
    import asyncio
    import threading

    from custom_components.energy_compass import coordinator as module

    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
        debounce_seconds=0,
    )
    config["helpers"] = {
        "grid_import_kw": {
            "entity": {"entity_id": "input_number.limit"},
            "unit": "kW",
            "max_age_seconds": None,
        }
    }
    hass.states.async_set("input_number.limit", "5", {"unit_of_measurement": "kW"})
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Rapid", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    # ADR-0019 §B4: the initial solve is backgrounded; let it finish with the
    # real `compute` before patching it and driving a controlled recalculation.
    await hass.async_block_till_done(wait_background_tasks=True)
    started, release = threading.Event(), threading.Event()
    observed = []
    active = []
    original = module.compute

    def delayed(config, states, now, **kwargs):
        assert not active
        active.append(True)
        observed.append(states["input_number.limit"]["state"])
        try:
            if len(observed) == 1:
                started.set()
                assert release.wait(5)
            return original(config, states, now, **kwargs)
        finally:
            active.pop()

    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(entry.runtime_data.async_recalculate())
        try:
            assert await hass.async_add_executor_job(started.wait, 2)
            for value in ("6", "7", "8"):
                hass.states.async_set(
                    "input_number.limit", value, {"unit_of_measurement": "kW"}
                )
                await asyncio.sleep(0)
            assert entry.runtime_data.data["valid"]
            assert entry.runtime_data.data["refreshing"]
        finally:
            release.set()
        await job
        await hass.async_block_till_done()
    assert observed == ["5", "8"]
    assert entry.runtime_data.data["valid"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_registry_rename_follows_identity_and_removal_requires_reconfigure(
    recorder_mock, hass, enable_custom_integrations
):
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    selected = registry.async_get_or_create(
        "sensor", "template", "synthetic-input", suggested_object_id="selected_limit"
    )
    hass.states.async_set(selected.entity_id, "5", {"unit_of_measurement": "kW"})
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
        debounce_seconds=0,
    )
    config["helpers"] = {
        "grid_import_kw": {
            "entity": {"entity_id": selected.entity_id, "registry_id": selected.id},
            "unit": "kW",
            "max_age_seconds": None,
        }
    }
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Identity", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    registry.async_update_entity(
        selected.entity_id, new_entity_id="sensor.renamed_limit"
    )
    hass.states.async_set("sensor.renamed_limit", "6", {"unit_of_measurement": "kW"})
    await hass.async_block_till_done()
    assert entry.runtime_data.data["valid"]
    assert (
        entry.runtime_data.configuration["helpers"]["grid_import_kw"]["entity"][
            "entity_id"
        ]
        == "sensor.renamed_limit"
    )
    registry.async_remove("sensor.renamed_limit")
    hass.states.async_set("sensor.renamed_limit", "9", {"unit_of_measurement": "kW"})
    await hass.async_block_till_done()
    assert entry.runtime_data.data["valid"]
    assert entry.runtime_data.data["alert"]["code"] == "invalid_input"
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("worker_error", [False, True])
async def test_unload_discards_late_worker(
    recorder_mock, hass, enable_custom_integrations, worker_error
):
    import asyncio
    import threading

    from custom_components.energy_compass import coordinator as module

    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Late", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data
    started, release = threading.Event(), threading.Event()
    original = module.compute

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(5)
        if worker_error:
            from custom_components.energy_compass.engine.models import SolveError

            raise SolveError("timeout")
        return original(*args, **kwargs)

    with patch.object(module, "compute", new=delayed):
        job = hass.async_create_task(coordinator.async_recalculate())
        try:
            assert await hass.async_add_executor_job(started.wait, 2)
            unload = hass.async_create_task(
                hass.config_entries.async_unload(entry.entry_id)
            )
            for _ in range(10):
                await asyncio.sleep(0)
        finally:
            release.set()
        assert await unload
        await job
    assert coordinator._closed
    assert coordinator._source_unsub is coordinator._registry_unsub is None
    assert hass.states.get("sensor.late_consumption_compass").state == "unavailable"


async def test_reload_preserves_entity_identity_and_observed_window(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from homeassistant.helpers import entity_registry as er

    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Stable", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    before = {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    start = hass.states.get("sensor.stable_next_boost_start").state
    freezer.move_to("2026-09-17T10:05:00+00:00")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    after = {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert after == before
    assert hass.states.get("sensor.stable_next_boost_start").state == start
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostic_measurements_do_not_supersede_plans(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
        debounce_seconds=0,
    )
    config["measurements"]["grid_import_power"] = {
        "entity": {"entity_id": "sensor.grid_meter"},
        "unit": "kW",
        "max_age_seconds": 600,
    }
    hass.states.async_set("sensor.grid_meter", "1", {"unit_of_measurement": "kW"})
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Measured", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    generation = entry.runtime_data._generation
    for value in ("2", "3", "4"):
        hass.states.async_set("sensor.grid_meter", value, {"unit_of_measurement": "kW"})
        await hass.async_block_till_done()
    assert entry.runtime_data._generation == generation
    assert entry.runtime_data.data["measurements"]["grid_import_power"]["value"] == 4
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_equivalent_forecast_records_do_not_recalculate(
    recorder_mock, hass, enable_custom_integrations
):
    from custom_components.energy_compass.config_models import PriceSource
    from custom_components.energy_compass.sources.bindings import (
        EntityBinding,
        IntervalBinding,
    )

    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
        debounce_seconds=0,
    )
    binding = IntervalBinding(
        EntityBinding("sensor.market", attribute="rows"),
        start_path="start",
        interval_minutes=60,
        value_path="price",
        unit="EUR/kWh",
        value_kind="price",
    )
    config["sources"]["buy"] = PriceSource("forecast", (binding,)).to_dict()
    hass.states.async_set(
        "sensor.market",
        "ok",
        {
            "rows": [
                {
                    "start": "2026-09-17T10:00:00+00:00",
                    "price": ".2",
                    "provider_update": "old",
                }
            ]
        },
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Normalized", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    generation = entry.runtime_data._generation
    hass.states.async_set(
        "sensor.market",
        "ok",
        {
            "rows": [
                {
                    "start": "2026-09-17T10:00:00Z",
                    "price": 0.2,
                    "provider_update": "new",
                }
            ]
        },
    )
    await hass.async_block_till_done()
    assert entry.runtime_data._generation == generation
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_optional_registry_removal_does_not_adopt_replacement(
    recorder_mock, hass, enable_custom_integrations
):
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    selected = registry.async_get_or_create(
        "sensor", "template", "optional-meter", suggested_object_id="optional_meter"
    )
    hass.states.async_set(selected.entity_id, "1", {"unit_of_measurement": "kW"})
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
        debounce_seconds=0,
    )
    config["measurements"]["grid_import_power"] = {
        "entity": {"entity_id": selected.entity_id, "registry_id": selected.id},
        "unit": "kW",
        "max_age_seconds": 600,
    }
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Optional", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    try:
        registry.async_remove(selected.entity_id)
        hass.states.async_set(selected.entity_id, "999", {"unit_of_measurement": "kW"})
        await hass.async_block_till_done()
        assert entry.runtime_data.data["valid"]
        measurement = entry.runtime_data.data["measurements"]["grid_import_power"]
        assert measurement["status"] == "unavailable"
        assert measurement["value"] is None
        assert hass.states.get("binary_sensor.optional_forecast_valid").state == "on"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_missing_future_continuation_recovers_without_losing_current(
    recorder_mock, hass, enable_custom_integrations
):
    from homeassistant.util import dt as dt_util

    from custom_components.energy_compass.config_models import PriceSource
    from custom_components.energy_compass.sources.bindings import (
        EntityBinding,
        IntervalBinding,
    )

    now = dt_util.utcnow()
    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=4,
        display_horizon_hours=4,
        reference_horizon_hours=4,
        debounce_seconds=0,
    )
    bindings = tuple(
        IntervalBinding(
            EntityBinding(name, attribute="rows"),
            start_path="start",
            end_path="end",
            value_path="price",
            unit="EUR/kWh",
            value_kind="price",
        )
        for name in ("sensor.today", "sensor.tomorrow")
    )
    config["sources"]["buy"] = PriceSource("forecast", bindings).to_dict()
    hass.states.async_set(
        "sensor.today",
        "ok",
        {"rows": [{"start": now, "end": now + timedelta(hours=2), "price": 0.2}]},
    )
    hass.states.async_set("sensor.tomorrow", "unknown")
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Continuation", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    try:
        assert entry.runtime_data.data["valid"]
        assert entry.runtime_data.data["quality"]["missing_sources"] == [
            "sensor.tomorrow"
        ]
        hass.states.async_set(
            "sensor.tomorrow",
            "ok",
            {
                "rows": [
                    {
                        "start": now + timedelta(hours=2),
                        "end": now + timedelta(hours=4),
                        "price": 0.3,
                    }
                ]
            },
        )
        await hass.async_block_till_done()
        assert entry.runtime_data.data["valid"]
        assert entry.runtime_data.data["quality"]["coverage_complete"]
        assert entry.runtime_data.data["quality"]["missing_sources"] == []
        hass.states.async_set("sensor.today", "unavailable")
        await hass.async_block_till_done()
        assert entry.runtime_data.data["valid"]
        assert entry.runtime_data.data["alert"]["code"] == "invalid_input"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_inputs_changing_faster_than_a_calculation_still_publish(
    recorder_mock, hass, enable_custom_integrations
):
    """A result superseded only by newer inputs is published before the rerun.

    Live failure: at 8 kW the SOC crossed the trigger every ~4 minutes while one
    calculation took ~5, so every result was discarded and the retained plan
    expired after two hours without a publication.
    """
    import asyncio

    from custom_components.energy_compass import coordinator as module

    config = default_configuration("EUR", "UTC")
    config["settings"].update(
        horizon_hours=1,
        display_horizon_hours=1,
        reference_horizon_hours=1,
        debounce_seconds=0,
    )
    config["helpers"] = {
        "grid_import_kw": {
            "entity": {"entity_id": "input_number.limit"},
            "unit": "kW",
            "max_age_seconds": None,
        }
    }
    hass.states.async_set("input_number.limit", "5", {"unit_of_measurement": "kW"})
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Starved", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    original = module.compute
    statuses = []
    published_before = []
    changes = iter(("6", "7", "8"))

    async def change(value):
        hass.states.async_set(
            "input_number.limit", value, {"unit_of_measurement": "kW"}
        )
        await asyncio.sleep(0)

    def busy(config, states, now, **kwargs):
        published_before.append(statuses.count("ready"))
        result = original(config, states, now, **kwargs)
        value = next(changes, None)
        if value is not None:
            asyncio.run_coroutine_threadsafe(change(value), hass.loop).result(5)
        return result

    unsubscribe = coordinator.async_add_listener(
        lambda: statuses.append(coordinator.data.get("status"))
    )
    with patch.object(module, "compute", new=busy):
        await coordinator.async_recalculate()
        await hass.async_block_till_done()
    unsubscribe()

    assert len(published_before) == 4
    # Every superseded result is published before the next run starts.
    assert published_before == [0, 1, 2, 3]
    assert coordinator.data["valid"]
    assert coordinator.data["status"] == "ready"
    assert await hass.config_entries.async_unload(entry.entry_id)
