from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.soc_estimator_config import ESTIMATE_KEY

START = "2026-10-14T10:00:00+00:00"
SOURCES = {
    "charge_counter": ("sensor.charged", "kWh"),
    "discharge_counter": ("sensor.discharged", "kWh"),
    "voltage": ("sensor.pack_v", "V"),
    "current": ("sensor.pack_a", "A"),
    "power": ("sensor.pack_w", "W"),
}


def _config(**estimator):
    config = default_configuration("EUR", "UTC")
    config["sources"].update(
        battery_enabled=True, soc=None, soc_mode="counter_estimator"
    )
    config["settings"].update(
        capacity_kwh=20,
        soc_ceiling=100,
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
        inverter_kw=5,
        grid_import_kw=7,
        minimum_mode_minutes=15,
    )
    config["soc_estimator"] = {
        "bindings": {
            role: {"entity_id": entity} for role, (entity, _) in SOURCES.items()
        },
        "empty_voltage": 480,
        "eta_charge": 0.9,
        "initial_soc_percent": 50,
        **estimator,
    }
    return config


def _set_sources(hass, charge=100.0, discharge=50.0, voltage=520, current=0, power=0):
    values = {
        "charge_counter": charge,
        "discharge_counter": discharge,
        "voltage": voltage,
        "current": current,
        "power": power,
    }
    for role, (entity, unit) in SOURCES.items():
        hass.states.async_set(
            entity, str(values[role]), {"unit_of_measurement": unit}, force_update=True
        )


async def _setup(hass, config=None, hass_storage=None, stored=None):
    entry = MockConfigEntry(
        domain="energy_compass", data=config or _config(), title="Home", version=3
    )
    entry.add_to_hass(hass)
    if stored is not None:
        hass_storage[f"energy_compass.{entry.entry_id}.soc_estimator"] = {
            "version": 1,
            "minor_version": 1,
            "key": f"energy_compass.{entry.entry_id}.soc_estimator",
            "data": stored,
        }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def test_planner_reads_the_estimator_without_an_entity(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _set_sources(hass)
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    assert hass.states.get("sensor.soc") is None
    assert coordinator.soc_estimator.estimate.soc_fraction == pytest.approx(0.5)
    assert coordinator.data["status"] == "ready"
    _, states, now, _ = coordinator._inputs()
    view = states[ESTIMATE_KEY]
    assert view["fraction"] == pytest.approx(0.5)
    assert view["reported_at"] <= now
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_counters_move_the_estimate_through_the_planner(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _set_sources(hass)
    entry = await _setup(hass)
    estimator = entry.runtime_data.soc_estimator
    freezer.tick(timedelta(minutes=1))
    _set_sources(hass, charge=100.4)
    await hass.async_block_till_done()
    # 0.4 kWh at eta 0.9 on 20 kWh.
    assert estimator.estimate.soc_fraction == pytest.approx(0.5 + 0.36 / 20)
    _, states, _, _ = entry.runtime_data._inputs()
    assert states[ESTIMATE_KEY]["fraction"] == pytest.approx(0.518)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reported_at_is_the_newest_input_report(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _set_sources(hass)
    entry = await _setup(hass)
    freezer.tick(timedelta(seconds=30))
    hass.states.async_set(
        "sensor.pack_v", "521", {"unit_of_measurement": "V"}, force_update=True
    )
    await hass.async_block_till_done()
    estimator = entry.runtime_data.soc_estimator
    assert estimator.reported_at == hass.states.get("sensor.pack_v").last_reported
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unreadable_counter_makes_the_source_unavailable(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _set_sources(hass)
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    freezer.tick(timedelta(seconds=30))
    hass.states.async_set("sensor.charged", "unavailable")
    await hass.async_block_till_done()
    assert coordinator.soc_estimator.view() is None
    with pytest.raises(InputError, match="soc_estimator"):
        coordinator._inputs()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_state_survives_a_restart(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _set_sources(hass)
    entry = await _setup(hass)
    freezer.tick(timedelta(minutes=1))
    _set_sources(hass, charge=100.3)
    await hass.async_block_till_done()
    expected = entry.runtime_data.soc_estimator.estimate.soc_fraction
    assert await hass.config_entries.async_unload(entry.entry_id)
    stored = hass_storage[f"energy_compass.{entry.entry_id}.soc_estimator"]["data"]
    assert stored["state"]["anchor"]["kind"] == "initial"

    freezer.tick(timedelta(minutes=5))
    _set_sources(hass, charge=100.3)
    restored = _config(initial_soc_percent=90)
    second = await _setup(hass, restored, hass_storage, stored)
    estimator = second.runtime_data.soc_estimator
    assert estimator.estimate.soc_fraction == pytest.approx(expected)
    assert await hass.config_entries.async_unload(second.entry_id)


async def test_a_counter_that_went_down_during_downtime_re_anchors(
    recorder_mock, hass, hass_storage, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _set_sources(hass)
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    stored = hass_storage[f"energy_compass.{entry.entry_id}.soc_estimator"]["data"]
    freezer.tick(timedelta(hours=1))
    _set_sources(hass, charge=5.0, discharge=2.0)
    second = await _setup(hass, _config(), hass_storage, stored)
    estimate = second.runtime_data.soc_estimator.estimate
    assert estimate.anchor_kind == "reset"
    assert await hass.config_entries.async_unload(second.entry_id)


async def test_balance_needs_a_full_anchor_not_a_full_reading(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    config = _config(initial_soc_percent=100)
    config["settings"].update(lfp_balance=True)
    _set_sources(hass)
    entry = await _setup(hass, config)
    coordinator = entry.runtime_data
    estimate = coordinator.soc_estimator.estimate
    assert estimate.unanchored_top
    assert estimate.top_confirmed is False
    assert coordinator._balance["hold_started_at"] is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_moving_estimate_triggers_a_recalculation(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _set_sources(hass)
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    before = coordinator._calculations
    charge = 100.0
    for _ in range(4):
        freezer.tick(timedelta(minutes=2))
        charge += 0.45
        _set_sources(hass, charge=charge)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator._calculations > before
    assert coordinator.data["status"] == "ready", coordinator.data
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_shadow_mode_leaves_the_planner_on_the_bound_entity(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _set_sources(hass)
    hass.states.async_set("sensor.soc", "60", {"unit_of_measurement": "%"})
    config = _config(shadow=True)
    config["sources"].update(
        soc_mode="entity", soc={"entity_id": "sensor.soc", "registry_id": None}
    )
    entry = await _setup(hass, config)
    coordinator = entry.runtime_data
    assert coordinator.soc_estimator.estimate.soc_fraction == pytest.approx(0.5)
    _, states, _, _ = coordinator._inputs()
    assert ESTIMATE_KEY not in states
    assert coordinator.data["status"] == "ready", coordinator.data
    assert await hass.config_entries.async_unload(entry.entry_id)


def _anchoring_sources(hass):
    hass.states.async_set(
        "sensor.pv", "3000", {"unit_of_measurement": "W"}, force_update=True
    )
    hass.states.async_set(
        "sensor.grid", "-2500", {"unit_of_measurement": "W"}, force_update=True
    )
    hass.states.async_set(
        "number.charge_limit", "14", {"unit_of_measurement": "A"}, force_update=True
    )
    _set_sources(hass, voltage=545)


async def test_a_physical_full_anchor_starts_the_balance_hold(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    config = _config(initial_soc_percent=99, anchor_on_candidates=True)
    config["soc_estimator"]["bindings"].update(
        pv_power={"entity_id": "sensor.pv"},
        grid_power={"entity_id": "sensor.grid"},
        charge_limit={"entity_id": "number.charge_limit"},
    )
    config["settings"].update(lfp_balance=True)
    _anchoring_sources(hass)
    entry = await _setup(hass, config)
    coordinator = entry.runtime_data
    estimator = coordinator.soc_estimator
    assert coordinator._balance["hold_started_at"] is None
    for _ in range(12):
        freezer.tick(timedelta(minutes=1))
        _anchoring_sources(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert estimator.estimate.anchor_kind == "full"
    assert estimator.estimate.top_confirmed
    assert estimator.estimate.soc_fraction == pytest.approx(1.0)
    coordinator._inputs()
    assert coordinator._balance["hold_started_at"] is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_shadow_run_starts_from_the_template_anchor(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _set_sources(hass, charge=6290.3, discharge=5662.4)
    hass.states.async_set("sensor.soc", "60", {"unit_of_measurement": "%"})
    config = _config(
        shadow=True,
        capacity_kwh=24,
        eta_charge=0.937,
        seed={
            "anchor_kwh": 21.3,
            "anchor_charge": 6288.3,
            "anchor_discharge": 5662.4,
            "anchor_kind": "corrected",
            "anchor_at": "2026-10-12T19:06:30+00:00",
            "pending_kwh": 0.0,
        },
    )
    config["sources"].update(
        soc_mode="entity", soc={"entity_id": "sensor.soc", "registry_id": None}
    )
    entry = await _setup(hass, config)
    estimate = entry.runtime_data.soc_estimator.estimate
    assert estimate.anchor_kind == "seed"
    assert estimate.energy_kwh == pytest.approx(21.3 + 0.937 * 2.0)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_export_limit_follows_the_grid_export_setting(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    _set_sources(hass)
    config = _config()
    config["settings"].update(grid_export_kw=3.5)
    entry = await _setup(hass, config)
    assert entry.runtime_data.soc_estimator.settings.export_limit_w == 3500.0
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_logged_full_candidate_confirms_the_balance_without_an_anchor(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    config = _config(initial_soc_percent=90)
    config["soc_estimator"]["bindings"].update(
        pv_power={"entity_id": "sensor.pv"},
        grid_power={"entity_id": "sensor.grid"},
        charge_limit={"entity_id": "number.charge_limit"},
    )
    config["settings"].update(lfp_balance=True)
    _anchoring_sources(hass)
    entry = await _setup(hass, config)
    coordinator = entry.runtime_data
    estimator = coordinator.soc_estimator
    for _ in range(12):
        freezer.tick(timedelta(minutes=1))
        _anchoring_sources(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert estimator.estimate.anchor_kind == "initial"
    assert estimator.state.candidates[0]["outcome"] == "candidate"
    assert estimator.view()["full_qualified"] is True
    coordinator._inputs()
    assert coordinator._balance["hold_started_at"] is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_charging_at_a_confirmed_top_adds_no_phantom_energy_or_history(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to(START)
    config = _config(initial_soc_percent=99, anchor_on_candidates=True)
    config["soc_estimator"]["bindings"].update(
        pv_power={"entity_id": "sensor.pv"},
        grid_power={"entity_id": "sensor.grid"},
        charge_limit={"entity_id": "number.charge_limit"},
    )
    _anchoring_sources(hass)
    entry = await _setup(hass, config)
    estimator = entry.runtime_data.soc_estimator
    for _ in range(12):
        freezer.tick(timedelta(minutes=1))
        _anchoring_sources(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert estimator.estimate.anchor_kind == "full"
    anchors = len(estimator.history)
    for extra in range(1, 4):
        freezer.tick(timedelta(minutes=1))
        _anchoring_sources(hass)
        _set_sources(hass, charge=100 + extra, voltage=545)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert estimator.estimate.excess_kwh == 0.0
    assert estimator.estimate.soc_fraction == pytest.approx(1.0)
    assert len(estimator.history) == anchors
    assert await hass.config_entries.async_unload(entry.entry_id)
