"""Actual flow regressions for the named operating-mode policy."""

from dataclasses import replace
from datetime import UTC, timedelta
from itertools import groupby

import pytest
from test_dispatch_policy import dispatch

from custom_components.energy_compass.engine.consumption import (
    analyze_consumption,
    machine_state,
)
from custom_components.energy_compass.engine.dispatch_policy import (
    dwell_group,
    validate_modes,
)
from custom_components.energy_compass.engine.models import CompassSettings, SolveError
from custom_components.energy_compass.engine.optimize import solve

ACTIVE = {"CHARGE_GRID", "CHARGE_PV", "DISCHARGE_GRID", "SELF_CONSUME"}


def assert_runs(problem, plan):
    rows = list(zip(problem.slots, plan.flows, strict=True))
    for _, group in groupby(rows, key=lambda row: dwell_group(machine_state(*row))):
        run = list(group)
        mode = machine_state(*run[0])
        elapsed = (
            run[-1][0].end.astimezone(UTC) - run[0][0].start.astimezone(UTC)
        ).total_seconds() / 60
        if mode in ACTIVE:
            assert elapsed >= problem.minimum_mode_minutes, (mode, elapsed)


def test_actual_flow_runs_cannot_use_idle_direction_credit():
    problem = dispatch(tuple((0.1 if i % 2 == 0 else 2, 0, 0, 1) for i in range(12)))
    plan = solve(problem)
    assert any(machine_state(s, f) in ACTIVE for s, f in zip(problem.slots, plan.flows))
    assert_runs(problem, plan)


def test_charge_source_changes_start_distinct_runs():
    problem = dispatch(
        tuple((0 if i < 4 else 4, 0, 1 if i % 2 else 0, 0.1) for i in range(12))
    )
    assert_runs(problem, solve(problem))


def test_discharge_sink_changes_start_distinct_runs():
    problem = dispatch(
        tuple((3, 4 if i % 2 else 0, 0, 0.5) for i in range(8)),
        limit_export_to_pv=False,
    )
    problem = replace(problem, battery=replace(problem.battery, initial_kwh=5))
    assert_runs(problem, solve(problem))


def test_new_active_tail_requires_full_forecast_coverage():
    problem = dispatch(((5, 0, 0, 1),) * 3)
    problem = replace(problem, battery=replace(problem.battery, initial_kwh=5))
    plan = solve(problem)
    assert all(machine_state(s, f) == "HOLD" for s, f in zip(problem.slots, plan.flows))


def test_zero_duration_retains_short_runs():
    problem = dispatch(
        tuple((0.1 if i % 2 == 0 else 2, 0, 0, 1) for i in range(8)),
        minimum_mode_minutes=0,
    )
    assert any(f.charge_kwh > 0.1 for f in solve(problem).flows)


@pytest.mark.parametrize("floor", [0.1, 0.4])
def test_active_power_floor_and_physical_balance(floor):
    problem = dispatch(
        tuple((0 if i == 0 else 2, 0, 0, 0.25) for i in range(8)),
        minimum_mode_power_kw=floor,
    )
    plan = solve(problem)
    assert_runs(problem, plan)
    for slot, flow in zip(problem.slots, plan.flows):
        hours = (slot.end - slot.start).total_seconds() / 3600
        mode = machine_state(slot, flow)
        assert flow.dispatch_mode == mode
        if mode in ACTIVE:
            assert max(flow.charge_kwh, flow.discharge_kwh) >= floor * hours - 1e-8
        assert slot.pv_kwh + flow.grid_import_kwh + flow.discharge_kwh == pytest.approx(
            slot.load_kwh + flow.grid_export_kwh + flow.charge_kwh + flow.curtail_kwh
        )
        assert 0 <= flow.end_soc_kwh <= problem.battery.capacity_kwh + 1e-8


def test_named_carried_mode_keeps_remaining_time():
    problem = dispatch(((5, 0, 0, 1),) * 8)
    problem = replace(
        problem,
        initial_dispatch_mode="CHARGE_GRID",
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=20),
    )
    plan = solve(problem)
    assert all(
        machine_state(s, f) == "CHARGE_GRID"
        for s, f in zip(problem.slots[:3], plan.flows[:3])
    )


def test_observed_bound_safety_hold_preserves_unexpired_lock():
    problem = dispatch(((5, 0, 0, 1),) * 8)
    problem = replace(
        problem,
        battery=replace(problem.battery, initial_kwh=10),
        initial_dispatch_mode="CHARGE_GRID",
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=20),
    )
    plan = solve(problem)
    assert all(
        machine_state(s, f) == "HOLD" for s, f in zip(problem.slots[:3], plan.flows[:3])
    )
    assert machine_state(problem.slots[3], plan.flows[3]) == "SELF_CONSUME"


def test_future_bound_is_not_a_safety_shortcut():
    problem = dispatch(((0, 0, 0, 1),) * 4 + ((5, 0, 0, 1),) * 4)
    problem = replace(
        problem,
        battery=replace(problem.battery, initial_kwh=9.99),
        initial_dispatch_mode="CHARGE_GRID",
        initial_dispatch_mode_since=problem.slots[0].start,
    )
    with pytest.raises(SolveError, match="infeasible|Infeasible"):
        solve(problem)


def test_curtail_cannot_mask_battery_activity():
    problem = dispatch(((0, -1, 3, 0),) * 8)
    plan = solve(problem)
    assert all(
        abs(f.charge_kwh) < 1e-8 and abs(f.discharge_kwh) < 1e-8
        for f in plan.flows
        if f.curtail_kwh > 1e-6
    )


def native_config():
    from custom_components.energy_compass.settings import default_configuration
    from custom_components.energy_compass.sources.bindings import EntityBinding

    config = default_configuration("PLN", "UTC")
    config["sources"].update(
        battery_enabled=True, soc=EntityBinding("sensor.soc").to_dict()
    )
    config["settings"].update(
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
        inverter_kw=5,
        allow_grid_charge=True,
        buy_rate=1,
        terminal_mode="value",
        soc_max_age_seconds=3600,
    )
    return config


@pytest.mark.parametrize(
    "stored",
    [
        None,
        {"mode": "charge", "since": "2026-09-18T09:40:00+00:00"},
        {"mode": "HOLD", "since": "2026-09-19T10:00:00+00:00"},
        {"mode": "oops", "since": "bad"},
        [],
        {"mode": "HOLD", "since": "2026-09-18T09:40:00"},
    ],
)
async def test_native_named_commitment_replan_reload(
    recorder_mock, hass, enable_custom_integrations, freezer, stored
):
    from homeassistant.helpers.storage import Store
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    freezer.move_to("2026-09-18T10:00:00+00:00")
    hass.states.async_set("sensor.soc", "50")
    entry = MockConfigEntry(
        domain="energy_compass", data=native_config(), title="Named", version=2
    )
    entry.add_to_hass(hass)
    await Store(hass, 1, f"energy_compass.{entry.entry_id}.dispatch").async_save(stored)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    assert coordinator.data["valid"]
    mode = coordinator.data["intervals"][0]["state"]
    commitment = coordinator._battery_commitment.copy()
    assert commitment["mode"] == mode
    assert commitment["since"] == "2026-09-18T10:00:00+00:00"
    assert coordinator.data["dispatch_policy"]["mode_scope"] == "actual_operating_mode"
    assert coordinator.data["dispatch_policy"]["minimum_mode_power_kw"] == 0.1
    freezer.move_to("2026-09-18T10:20:00+00:00")
    await coordinator.async_recalculate()
    assert coordinator.data["valid"]
    assert coordinator._battery_commitment == commitment
    assert coordinator.data["intervals"][0]["state"] == mode
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data._battery_commitment == commitment
    assert entry.runtime_data.data["intervals"][0]["state"] == mode
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_safety_hold_preserves_original_clock(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from homeassistant.helpers.storage import Store
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    freezer.move_to("2026-09-18T10:00:00+00:00")
    config = native_config()
    hass.states.async_set("sensor.soc", str(config["settings"]["soc_ceiling"]))
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Safety", version=2
    )
    entry.add_to_hass(hass)
    commitment = {"mode": "CHARGE_GRID", "since": "2026-09-18T09:40:00+00:00"}
    await Store(hass, 1, f"energy_compass.{entry.entry_id}.dispatch").async_save(
        commitment
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    assert coordinator.data["valid"]
    assert coordinator.data["intervals"][0]["state"] == "HOLD"
    assert coordinator._battery_commitment == commitment
    assert coordinator.data["dispatch_policy"]["safety_exception"] == {
        "reason": "observed_soc_maximum",
        "interrupted_mode": "CHARGE_GRID",
        "deadline": "2026-09-18T10:40:00+00:00",
    }
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data._battery_commitment == commitment
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_options_save_power_floor(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    freezer.move_to("2026-09-18T10:00:00+00:00")
    config = native_config()
    config["sources"].update(battery_enabled=False, soc=None)
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Floor", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    form = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "planning"}
    )
    fields = {key.schema: value for key, value in form["data_schema"].schema.items()}
    assert fields["minimum_mode_power_kw"].config["min"] == 0.001
    await hass.config_entries.options.async_configure(
        fid, {"minimum_mode_power_kw": 0.4}
    )
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "preview"})
    result = await hass.config_entries.options.async_configure(fid, {"confirm": True})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.runtime_data.configuration["settings"]["minimum_mode_power_kw"] == 0.4
    assert entry.runtime_data._battery_commitment is None
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "zone_name,start_iso",
    [
        ("UTC", "2026-09-18T00:10:00+00:00"),
        ("Europe/Warsaw", "2026-10-25T00:50:00+00:00"),
        ("Europe/Warsaw", "2026-03-29T00:50:00+00:00"),
    ],
)
def test_native_partial_slots_and_dst_use_elapsed_time(zone_name, start_iso):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    start = datetime.fromisoformat(start_iso)
    zone = ZoneInfo(zone_name)
    widths = [5] + [15] * 11
    problem = dispatch(tuple((0 if i == 0 else 3, 0, 0, 0.5) for i in range(12)))
    slots = []
    cursor = start
    for slot, width in zip(problem.slots, widths):
        end = cursor + timedelta(minutes=width)
        slots.append(
            replace(
                slot,
                start=cursor.astimezone(zone),
                end=end.astimezone(zone),
                load_kwh=width / 30,
            )
        )
        cursor = end
    problem = replace(problem, slots=tuple(slots), timezone=zone_name)
    assert_runs(problem, solve(problem))


def test_legacy_guard_never_donates_age_to_named_hold():
    problem = dispatch(((5, 0, 0, 1),) * 8)
    problem = replace(
        problem,
        battery=replace(problem.battery, initial_kwh=5),
        initial_battery_mode="charge",
        initial_battery_mode_since=problem.slots[0].start - timedelta(minutes=45),
    )
    plan = solve(problem)
    assert all(
        machine_state(s, f) == "HOLD" for s, f in zip(problem.slots[:4], plan.flows[:4])
    )
    assert machine_state(problem.slots[4], plan.flows[4]) == "SELF_CONSUME"


def test_unresolvable_partial_interval_does_not_get_active_label():
    problem = dispatch(((0, 0, 0, 0),) + ((5, 0, 0, 1),) * 8)
    first = problem.slots[0]
    problem = replace(
        problem,
        slots=(
            replace(first, start=first.end - timedelta(milliseconds=1)),
            *problem.slots[1:],
        ),
    )
    plan = solve(problem)
    assert machine_state(problem.slots[0], plan.flows[0]) == "HOLD"
    assert_runs(problem, plan)


def test_zero_duration_permits_simultaneous_curtail_charge():
    problem = dispatch(
        ((0, -1, 3, 0),) * 4, minimum_mode_minutes=0, terminal_value_per_kwh=5
    )
    plan = solve(problem)
    assert any(f.curtail_kwh > 0.1 and f.charge_kwh > 0.1 for f in plan.flows)


@pytest.mark.parametrize("floor", [0, 0.0009, 1001, float("inf"), float("nan")])
def test_invalid_power_floor_rejected(floor):
    from datetime import datetime

    from custom_components.energy_compass.settings import validate_configuration

    config = native_config()
    config["settings"]["minimum_mode_power_kw"] = floor
    with pytest.raises(ValueError):
        validate_configuration(config, {}, datetime.now(UTC))


def test_disabled_battery_ignores_named_restrictions():
    problem = dispatch(((1, 0, 0, 1),), battery=None, minimum_mode_power_kw=1000)
    flow = solve(problem).flows[0]
    assert flow.dispatch_mode is None
    assert flow.grid_import_kwh == pytest.approx(1)


def test_hold_cannot_precredit_a_discharge_run():
    problem = dispatch(((0, 0, 0, 0),) * 4 + ((5, 0, 0, 1),) * 2)
    problem = replace(problem, battery=replace(problem.battery, initial_kwh=5))
    plan = solve(problem)
    assert all(machine_state(s, f) == "HOLD" for s, f in zip(problem.slots, plan.flows))


def test_carried_active_mode_can_finish_in_short_coverage():
    problem = dispatch(((5, 0, 0, 1),) * 2)
    problem = replace(
        problem,
        battery=replace(problem.battery, initial_kwh=5),
        initial_dispatch_mode="SELF_CONSUME",
        initial_dispatch_mode_since=problem.slots[0].start - timedelta(minutes=20),
    )
    plan = solve(problem)
    assert all(
        f.dispatch_mode == "SELF_CONSUME" and f.discharge_kwh >= 0.025 - 1e-8
        for f in plan.flows
    )


@pytest.mark.parametrize(
    "mode",
    ["CHARGE_GRID", "CHARGE_PV", "SELF_CONSUME", "DISCHARGE_GRID", "HOLD", "CURTAIL"],
)
def test_each_named_mode_has_real_flows(mode):
    rows = {
        "CHARGE_GRID": (0, 0, 0, 0.1),
        "CHARGE_PV": (2, 0, 1, 0.1),
        "SELF_CONSUME": (3, 0, 0, 1),
        "DISCHARGE_GRID": (2, 3, 0, 0.1),
        "HOLD": (2, 0, 0, 0.1),
        "CURTAIL": (2, -1, 1, 0.1),
    }
    problem = dispatch((rows[mode],) * 4, limit_export_to_pv=False)
    problem = replace(
        problem,
        battery=replace(problem.battery, initial_kwh=5),
        initial_dispatch_mode=mode,
        initial_dispatch_mode_since=problem.slots[0].start,
    )
    plan = solve(problem)
    assert all(
        machine_state(s, f) == mode == f.dispatch_mode
        for s, f in zip(problem.slots, plan.flows)
    )


async def test_native_initial_flow_saves_power_floor(
    recorder_mock, hass, enable_custom_integrations
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": "user"}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Initial floor",
            "currency": "PLN",
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "planning"})
    await hass.config_entries.flow.async_configure(fid, {"minimum_mode_power_kw": 0.25})
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "preview"})
    result = await hass.config_entries.flow.async_configure(
        fid, {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True}
    )
    assert result["type"] == "create_entry"
    assert result["data"]["settings"]["minimum_mode_power_kw"] == 0.25
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_native_failed_and_superseded_results_preserve_clock(
    recorder_mock, hass, enable_custom_integrations, freezer, monkeypatch
):
    import asyncio
    from threading import Event

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.energy_compass import coordinator as module

    freezer.move_to("2026-09-18T10:00:00+00:00")
    hass.states.async_set("sensor.soc", "50")
    entry = MockConfigEntry(
        domain="energy_compass", data=native_config(), title="Generation", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    original = coordinator._battery_commitment.copy()
    compute = module.compute
    completed = asyncio.Event()
    release = Event()

    def delayed(*args, **kwargs):
        result = compute(*args, **kwargs)
        hass.loop.call_soon_threadsafe(completed.set)
        assert release.wait(10)
        return result

    monkeypatch.setattr(module, "compute", delayed)
    task = hass.async_create_task(coordinator.async_recalculate())
    try:
        await completed.wait()
        coordinator._generation += 1
        coordinator._closed = True
    finally:
        release.set()
        await task
    assert coordinator._battery_commitment == original
    coordinator._closed = False
    monkeypatch.setattr(module, "compute", compute)
    hass.states.async_set("sensor.soc", "unavailable")
    await coordinator.async_recalculate()
    assert coordinator.data["valid"]
    assert coordinator.data["alert"]["code"] == "invalid_input"
    assert coordinator._battery_commitment == original
    assert await hass.config_entries.async_unload(entry.entry_id)


def carried_pv_problem(rows, *, locked_minutes=10, **changes):
    """Replay 2026-09-22: a carried CHARGE_PV lock meets a PV deficit."""
    problem = dispatch(rows, minimum_mode_minutes=15, **changes)
    return replace(
        problem,
        battery=replace(problem.battery, initial_kwh=5),
        initial_dispatch_mode="CHARGE_PV",
        initial_dispatch_mode_since=problem.slots[0].start
        - timedelta(minutes=15 - locked_minutes),
    )


def test_carried_charge_pv_backfills_deficit_from_battery():
    # CHARGE_PV and SELF_CONSUME write the same inverter registers: when PV
    # falls below load inside the lock, the inverter covers it from the battery.
    # Cheap import now and dear import later make HOLD the tempting shortcut.
    problem = carried_pv_problem(((0.1, 0, 0.1, 0.35),) + ((5.0, 0, 0, 0.5),) * 7)
    plan = solve(problem)
    assert plan.flows[0].dispatch_mode == "SELF_CONSUME"
    assert plan.flows[0].discharge_kwh >= 0.025 - 1e-8


def test_carried_pv_family_still_forbids_early_hold():
    problem = carried_pv_problem(((0.1, 0, 0.1, 0.35),) + ((5.0, 0, 0, 0.5),) * 7)
    plan = solve(problem)
    flows = (replace(plan.flows[0], discharge_kwh=0, dispatch_mode="HOLD"),)
    with pytest.raises(SolveError, match="minimum mode duration"):
        validate_modes(problem, flows + plan.flows[1:])


def test_probe_under_carried_charge_pv_lock_is_priced():
    # Thin PV surplus keeps the baseline in CHARGE_PV; the 1 kWh probe turns it
    # into a deficit, which used to make the current hour infeasible.
    problem = carried_pv_problem(((1.25, 0.8, 0.1, 0.07),) * 8)
    baseline = solve(problem)
    assert baseline.flows[0].dispatch_mode == "CHARGE_PV"
    result = analyze_consumption(
        problem,
        baseline,
        settings=CompassSettings(display_horizon_hours=2, reference_horizon_hours=2),
    )
    assert result.opportunities[0].cost_per_kwh is not None
    assert result.coverage_reason == "complete"


def test_pv_family_shares_one_dwell_run():
    # Alternating surplus and deficit: the inverter follows PV either way, so
    # CHARGE_PV <-> SELF_CONSUME flips need no minimum duration of their own.
    problem = dispatch(
        tuple((2.0, 0, 1.0 if i % 2 == 0 else 0, 0.5) for i in range(8)),
        limit_export_to_pv=False,
    )
    problem = replace(problem, battery=replace(problem.battery, initial_kwh=5))
    plan = solve(problem)
    modes = [machine_state(s, f) for s, f in zip(problem.slots, plan.flows)]
    assert modes[:4] == ["CHARGE_PV", "SELF_CONSUME"] * 2
    assert_runs(problem, plan)


def test_pv_family_flip_keeps_commitment_clock():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from custom_components.energy_compass.coordinator import EnergyCompassCoordinator

    since = "2026-09-22T06:45:00+00:00"
    coordinator = SimpleNamespace(
        _battery_commitment={"mode": "CHARGE_PV", "since": since},
        _dispatch_store=Mock(),
    )

    def publish(mode, at):
        EnergyCompassCoordinator._commit_battery_direction(
            coordinator,
            {
                "generated_at": at,
                "intervals": [{"dispatch_mode": mode}],
                "dispatch_policy": {"enabled": True},
            },
        )
        return coordinator._battery_commitment

    assert publish("SELF_CONSUME", "2026-09-22T06:50:00+00:00") == {
        "mode": "SELF_CONSUME",
        "since": since,
    }
    assert publish("HOLD", "2026-09-22T07:05:00+00:00") == {
        "mode": "HOLD",
        "since": "2026-09-22T07:05:00+00:00",
    }
