"""Economic and native proofs for additional physical battery-export periods."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine.models import (
    Battery,
    InputError,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import solve


def cycle(sell=0.65, **changes):
    start = datetime(2026, 9, 18, tzinfo=UTC)
    return replace(
        Problem(
            tuple(
                Slot(
                    start + timedelta(hours=i),
                    start + timedelta(hours=i + 1),
                    0.61,
                    price,
                    0,
                    0,
                )
                for i, price in enumerate((sell, 0))
            ),
            SiteLimits(2, 2, 2, True),
            Battery(1, 0, 1, 1, 1, 1, 1, 1, 0, True, True),
            "preserve_initial",
            0,
            (),
            "UTC",
            minimum_mode_minutes=0,
            limit_export_to_pv=False,
        ),
        **changes,
    )


@pytest.mark.parametrize("minutes", [0, 60])
def test_default_hurdle_removes_four_cent_cycle(minutes):
    source = cycle(minimum_mode_minutes=minutes)
    low = solve(replace(source, minimum_export_episode_benefit=0))
    assert low.flows[0].discharge_kwh == pytest.approx(1)
    assert low.objective == pytest.approx(-0.04)
    guarded = solve(source)
    assert sum(f.discharge_kwh for f in guarded.flows) < 1e-6
    assert guarded.new_export_episodes == 0
    assert guarded.export_episode_reserve == 0


@pytest.mark.parametrize(
    "sell,threshold,exports",
    [(2, 1, True), (2, 2, False), (1.609, 1, False), (1.611, 1, True)],
)
def test_incremental_full_horizon_boundary(sell, threshold, exports):
    result = solve(cycle(sell, minimum_export_episode_benefit=threshold))
    assert (result.flows[0].discharge_kwh > 0.9) is exports
    assert result.new_export_episodes == int(exports)
    assert result.export_episode_reserve == threshold * int(exports)
    assert result.objective == pytest.approx(0.61 - sell if exports else 0)
    assert result.objective == pytest.approx(
        result.grid_cost + result.wear_cost - result.terminal_credit
    )


def test_losses_and_wear_are_included_in_benefit():
    source = cycle(2)
    inefficient = replace(
        source,
        battery=replace(
            source.battery,
            eta_charge=0.8,
            eta_discharge=0.8,
            wear_per_kwh=0.2,
            charge_kw=2,
        ),
    )
    assert solve(source).new_export_episodes == 1
    result = solve(inefficient)
    assert result.new_export_episodes == 0
    assert result.objective == pytest.approx(0)


def test_two_separated_profitable_periods_each_pay_once():
    source = cycle(3)
    source = replace(
        source,
        slots=source.slots
        + tuple(
            replace(
                s, start=s.start + timedelta(hours=2), end=s.end + timedelta(hours=2)
            )
            for s in source.slots
        ),
    )
    result = solve(source)
    assert [f.discharge_kwh for f in result.flows] == pytest.approx([1, 0, 1, 0])
    assert result.new_export_episodes == 2
    assert result.export_episode_reserve == 2
    assert result.objective == pytest.approx(-4.78)


def test_current_period_is_exempt_but_future_period_is_not():
    source = cycle(initial_export_active=True)
    result = solve(source)
    assert result.flows[0].discharge_kwh == pytest.approx(1)
    assert result.new_export_episodes == 0
    assert result.export_episode_reserve == 0
    future = replace(
        source,
        slots=tuple(
            replace(s, sell_per_kwh=0 if i == 0 else 0.65)
            for i, s in enumerate(source.slots)
        ),
        terminal_mode="value",
        terminal_value_per_kwh=0.61,
    )
    assert solve(future).flows[1].discharge_kwh == pytest.approx(0)


@pytest.mark.parametrize("value", [-1, 1001, float("nan"), float("inf"), "bad", None])
@pytest.mark.parametrize("battery", [True, False])
def test_invalid_hurdle_rejected_even_without_battery(value, battery):
    source = cycle(minimum_export_episode_benefit=value)
    if not battery:
        source = replace(source, battery=None)
    with pytest.raises(InputError):
        solve(source)


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_continuity_is_strict_boolean(value):
    with pytest.raises(InputError):
        solve(cycle(initial_export_active=value))


def test_pv_export_and_household_discharge_do_not_pay_reserve():
    source = cycle()
    solar = replace(
        source, battery=None, slots=tuple(replace(s, pv_kwh=1) for s in source.slots)
    )
    result = solve(solar)
    assert result.flows[0].grid_export_kwh == pytest.approx(1)
    assert result.export_episode_reserve == result.new_export_episodes == 0
    household = replace(
        source,
        terminal_mode="value",
        slots=tuple(replace(s, load_kwh=0.01, sell_per_kwh=0) for s in source.slots),
    )
    result = solve(household)
    assert sum(f.discharge_kwh for f in result.flows) == pytest.approx(0.02)
    assert result.export_episode_reserve == 0


def test_partial_slot_uses_elapsed_power_floor():
    source = cycle(100)
    first = replace(source.slots[0], start=source.slots[0].end - timedelta(minutes=5))
    result = solve(replace(source, slots=(first, source.slots[1])))
    assert result.flows[0].discharge_kwh == pytest.approx(1 / 12)
    assert result.new_export_episodes == 1


def test_zero_flow_gap_cannot_hide_a_new_period():
    source = cycle(5)
    start = source.slots[0].start
    source = replace(
        source,
        terminal_mode="value",
        battery=replace(source.battery, capacity_kwh=2, initial_kwh=2),
        slots=tuple(
            Slot(
                start + timedelta(hours=i),
                start + timedelta(hours=i + 1),
                0.61,
                sell,
                0,
                0,
            )
            for i, sell in enumerate((5, -100, 5))
        ),
    )
    result = solve(source)
    assert [f.discharge_kwh for f in result.flows] == pytest.approx([1, 0, 1])
    assert result.new_export_episodes == 2


def test_real_low_power_export_can_bridge_a_valley_without_recharge():
    source = cycle(5)
    start = source.slots[0].start
    source = replace(
        source,
        terminal_mode="value",
        battery=replace(source.battery, capacity_kwh=3, initial_kwh=3),
        slots=tuple(
            Slot(
                start + timedelta(hours=i),
                start + timedelta(hours=i + 1),
                0.61,
                sell,
                0,
                0,
            )
            for i, sell in enumerate((5, -1, 5))
        ),
    )
    result = solve(source)
    assert result.flows[1].discharge_kwh == pytest.approx(0.1)
    assert result.flows[1].grid_export_kwh == pytest.approx(0.1)
    assert result.new_export_episodes == 1


def test_duration_zero_curtailment_can_charge_battery():
    source = cycle()
    source = replace(
        source,
        battery=replace(source.battery, initial_kwh=0),
        terminal_mode="value",
        terminal_value_per_kwh=10,
        slots=(replace(source.slots[0], pv_kwh=4, sell_per_kwh=-1),),
    )
    result = solve(source)
    assert result.flows[0].charge_kwh == pytest.approx(1)
    assert result.flows[0].curtail_kwh == pytest.approx(3)
    assert result.new_export_episodes == 0


def test_physical_export_cannot_hide_behind_curtail_label(monkeypatch):
    from custom_components.energy_compass.engine import optimize

    original = optimize._Model.solve

    def force_curtail(model, deadline):
        # Variable ordering is the public test seam used by existing solver validation tests.
        model.lower[2] = model.upper[2] = 0.2
        return original(model, deadline)

    source = cycle(5)
    source = replace(
        source, slots=(replace(source.slots[0], pv_kwh=0.2), source.slots[1])
    )
    monkeypatch.setattr(optimize._Model, "solve", force_curtail)
    result = solve(source)
    assert result.flows[0].curtail_kwh == pytest.approx(0.2)
    assert result.flows[0].discharge_kwh == pytest.approx(1)
    assert result.new_export_episodes == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("export_start", 0),
        ("export_active", 0),
        ("export_start", 0.5),
        ("export_side", 0.5),
    ],
)
def test_independent_validation_rejects_corrupt_export_vectors(monkeypatch, key, value):
    from custom_components.energy_compass.engine import optimize

    original = optimize._validate_solution

    def corrupt(problem, vectors, values, fractions, budgets):
        values[vectors[0][key]] = value
        return original(problem, vectors, values, fractions, budgets)

    monkeypatch.setattr(optimize, "_validate_solution", corrupt)
    with pytest.raises(SolveError, match="export"):
        solve(cycle(5))


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
        capacity_kwh=2,
        charge_kw=1,
        discharge_kw=1,
        inverter_kw=2,
        grid_export_kw=2,
        allow_battery_export=True,
        allow_grid_charge=False,
        limit_export_to_pv=False,
        minimum_mode_minutes=0,
        daily_load_kwh=0,
        buy_rate=10,
        sell_rate=5,
        terminal_mode="value",
        soc_max_age_seconds=3600,
    )
    return config


def test_native_default_migration_and_currency_selector():
    from custom_components.energy_compass.flow_schema import settings_schema
    from custom_components.energy_compass.runtime import build_problem
    from custom_components.energy_compass.settings import default_configuration

    config = default_configuration("PLN", "UTC")
    assert config["settings"].pop("minimum_export_episode_benefit") == 1
    source, values, _ = build_problem(config, {}, datetime(2026, 9, 18, tzinfo=UTC))
    assert (
        source.minimum_export_episode_benefit
        == values["minimum_export_episode_benefit"]
        == 1
    )
    fields = {
        str(k): v for k, v in settings_schema("planning", values, "PLN").schema.items()
    }
    assert (
        fields["minimum_export_episode_benefit"].config["unit_of_measurement"] == "PLN"
    )
    assert fields["minimum_export_episode_benefit"].config["min"] == 0
    assert fields["minimum_export_episode_benefit"].config["max"] == 1000


@pytest.mark.parametrize("value", [-1, 1001, float("nan"), float("inf")])
@pytest.mark.parametrize("helper", [False, True])
def test_native_hurdle_and_helpers_are_bounded(value, helper):
    from custom_components.energy_compass.settings import (
        default_configuration,
        validate_configuration,
    )

    config = default_configuration("PLN", "UTC")
    now = datetime(2026, 9, 18, tzinfo=UTC)
    states = {}
    if helper:
        config["helpers"]["minimum_export_episode_benefit"] = {
            "entity": {"entity_id": "input_number.benefit"},
            "unit": "PLN",
            "max_age_seconds": None,
        }
        states["input_number.benefit"] = {
            "state": str(value),
            "attributes": {"unit_of_measurement": "PLN"},
            "last_updated": now,
        }
    else:
        config["settings"]["minimum_export_episode_benefit"] = value
    with pytest.raises(InputError):
        validate_configuration(config, states, now)


def test_native_helper_resolves_currency_amount():
    from custom_components.energy_compass.runtime import build_problem

    config = native_config()
    config["helpers"]["minimum_export_episode_benefit"] = {
        "entity": {"entity_id": "input_number.benefit"},
        "unit": "PLN",
        "max_age_seconds": None,
    }
    now = datetime(2026, 9, 18, tzinfo=UTC)
    states = {
        "sensor.soc": {"state": "100", "attributes": {}, "last_updated": now},
        "input_number.benefit": {
            "state": "2.5",
            "attributes": {"unit_of_measurement": "PLN"},
            "last_updated": now,
        },
    }
    assert build_problem(config, states, now)[0].minimum_export_episode_benefit == 2.5
    config["helpers"]["minimum_export_episode_benefit"]["unit"] = "PLN/kWh"
    with pytest.raises(InputError):
        build_problem(config, states, now)


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"mode": "DISCHARGE_GRID", "since": "2026-09-18T00:00:00+00:00"},
        {
            "generated_at": "2026-09-18T01:01:00+00:00",
            "until": "2026-09-18T02:00:00+00:00",
        },
        {
            "generated_at": "2026-09-18T00:00:00+00:00",
            "until": "2026-09-18T01:00:00+00:00",
        },
        {"generated_at": "2026-09-18T00:00:00", "until": "2026-09-18T02:00:00"},
        {"generated_at": "bad", "until": []},
        {
            "generated_at": "2026-09-18T00:00:00+00:00",
            "until": "2026-09-20T00:00:01+00:00",
        },
    ],
)
def test_invalid_export_continuity_does_not_exempt_new_period(record):
    from custom_components.energy_compass.runtime import build_problem

    config = native_config()
    now = datetime(2026, 9, 18, 1, tzinfo=UTC)
    states = {"sensor.soc": {"state": "100", "attributes": {}, "last_updated": now}}
    source, _, _ = build_problem(config, states, now, export_commitment=record)
    assert source.initial_export_active is False


def test_legacy_dwell_record_does_not_prove_export_continuity():
    from custom_components.energy_compass.runtime import build_problem

    config = native_config()
    now = datetime(2026, 9, 18, 1, tzinfo=UTC)
    states = {"sensor.soc": {"state": "100", "attributes": {}, "last_updated": now}}
    source, _, _ = build_problem(
        config,
        states,
        now,
        battery_commitment={"mode": "DISCHARGE_GRID", "since": now.isoformat()},
    )
    assert not source.initial_export_active


def test_probe_difference_excludes_decision_reserve():
    from custom_components.energy_compass.engine.consumption import consumption_outlook
    from custom_components.energy_compass.engine.models import CompassSettings

    source = cycle(2)
    base = solve(source)
    assert base.export_episode_reserve == 1
    candidate = solve(
        replace(source, slots=(replace(source.slots[0], load_kwh=1), source.slots[1]))
    )
    assert candidate.export_episode_reserve == 0
    assert candidate.objective - base.objective == pytest.approx(2)
    result = consumption_outlook(
        source,
        base,
        settings=CompassSettings(display_horizon_hours=2, reference_horizon_hours=2),
    )
    assert result[0].cost_per_kwh == pytest.approx(2)


@pytest.fixture
async def export_entry(recorder_mock, hass, enable_custom_integrations, freezer):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    freezer.move_to("2026-09-18T00:00:00+00:00")
    hass.states.async_set("sensor.soc", "100")
    entry = MockConfigEntry(
        domain="energy_compass", data=native_config(), title="Benefit", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)


async def test_current_export_survives_recalculation_and_restart(
    export_entry, hass, freezer
):
    from homeassistant.helpers.storage import Store

    coordinator = export_entry.runtime_data
    assert coordinator.data["valid"]
    policy = coordinator.data["dispatch_policy"]
    assert policy["minimum_export_episode_benefit"] == 1
    assert policy["new_export_episodes"] == policy["export_episode_reserve"] == 1
    assert policy["export_benefit_scope"] == "additional_battery_export_period"
    record = coordinator._export_commitment
    assert record == {
        "generated_at": "2026-09-18T00:00:00+00:00",
        "until": "2026-09-18T02:00:00+00:00",
    }
    assert coordinator._battery_commitment is None
    freezer.move_to("2026-09-18T00:15:00+00:00")
    await coordinator.async_recalculate()
    assert coordinator.data["dispatch_policy"]["new_export_episodes"] == 0
    record = coordinator._export_commitment
    assert await hass.config_entries.async_unload(export_entry.entry_id)
    assert (
        await Store(
            hass, 1, f"energy_compass.{export_entry.entry_id}.export"
        ).async_load()
        == record
    )
    assert await hass.config_entries.async_setup(export_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert export_entry.runtime_data.data["dispatch_policy"]["new_export_episodes"] == 0


async def test_hurdle_can_be_enabled_mid_export(export_entry, hass):
    from copy import deepcopy

    coordinator = export_entry.runtime_data
    config = deepcopy(dict(export_entry.data))
    config["settings"]["minimum_export_episode_benefit"] = 0
    hass.config_entries.async_update_entry(export_entry, data=config)
    await coordinator.async_recalculate()
    assert coordinator._export_commitment
    config = deepcopy(config)
    config["settings"]["minimum_export_episode_benefit"] = 100
    hass.config_entries.async_update_entry(export_entry, data=config)
    await coordinator.async_recalculate()
    assert coordinator.data["valid"]
    assert coordinator.data["intervals"][0]["discharge_kwh"] > 0
    assert coordinator.data["dispatch_policy"]["export_episode_reserve"] == 0


async def test_hold_clears_current_export_record(export_entry, hass):
    from copy import deepcopy

    coordinator = export_entry.runtime_data
    assert coordinator._export_commitment
    config = deepcopy(dict(export_entry.data))
    config["settings"].update(sell_rate=-1, terminal_value_per_kwh=10)
    hass.config_entries.async_update_entry(export_entry, data=config)
    await coordinator.async_recalculate()
    assert coordinator.data["valid"]
    assert coordinator.data["intervals"][0]["discharge_kwh"] == pytest.approx(0)
    assert coordinator._export_commitment is None


async def test_failed_result_preserves_current_record(export_entry, monkeypatch):
    from custom_components.energy_compass import coordinator as module

    coordinator = export_entry.runtime_data
    before = coordinator._export_commitment.copy()

    def fail(*args, **kwargs):
        raise SolveError("timeout")

    monkeypatch.setattr(module, "compute", fail)
    await coordinator.async_recalculate()
    assert coordinator.data["status"] == "timeout"
    assert coordinator._export_commitment == before


@pytest.mark.parametrize("change", ["configuration", "inputs"])
async def test_superseded_hold_result_cannot_clear_current_record(
    export_entry, hass, monkeypatch, change
):
    """A result made wrong by a configuration change never touches the record.

    A result that is only older than newer inputs is published before the rerun,
    so the record must follow the plan that is actually published.
    """
    import asyncio
    from copy import deepcopy

    from custom_components.energy_compass import coordinator as module

    coordinator = export_entry.runtime_data
    before = coordinator._export_commitment.copy()
    entered, release = asyncio.Event(), asyncio.Event()
    original_history, original_compute = module.async_history, module.compute
    calls = 0

    async def pause(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
        return await original_history(*args)

    def held_then_failed(config, *args, **kwargs):
        if calls > 1:
            raise SolveError("timeout")
        config = deepcopy(config)
        config["settings"].update(sell_rate=-1, terminal_value_per_kwh=10)
        result = original_compute(config, *args, **kwargs)
        assert result["intervals"][0]["discharge_kwh"] < 1e-6
        return result

    monkeypatch.setattr(module, "async_history", pause)
    monkeypatch.setattr(module, "compute", held_then_failed)
    task = hass.async_create_task(coordinator.async_recalculate())
    await asyncio.wait_for(entered.wait(), 10)
    coordinator._generation += 1
    if change == "configuration":
        coordinator._epoch += 1
    release.set()
    await task
    assert coordinator.data["status"] == "timeout"
    if change == "configuration":
        assert coordinator._export_commitment == before
    else:
        assert coordinator.data["intervals"][0]["discharge_kwh"] < 1e-6
        assert coordinator._export_commitment != before


def test_export_start_cannot_be_invented_in_an_idle_slot(monkeypatch):
    from custom_components.energy_compass.engine import optimize

    original = optimize._validate_solution

    def corrupt(problem, vectors, values, fractions, budgets):
        values[vectors[1]["export_start"]] = 1
        return original(problem, vectors, values, fractions, budgets)

    monkeypatch.setattr(optimize, "_validate_solution", corrupt)
    with pytest.raises(SolveError, match="export start"):
        solve(cycle(5))


def test_solver_requests_zero_gap_only_for_enabled_battery_policy(monkeypatch):
    from custom_components.energy_compass.engine import optimize

    original = optimize.milp
    options = []

    def capture(**kwargs):
        options.append(kwargs["options"])
        return original(**kwargs)

    monkeypatch.setattr(optimize, "milp", capture)
    solve(cycle(5))
    solve(cycle(5, minimum_export_episode_benefit=0))
    solve(replace(cycle(5), battery=None))
    assert options[0]["mip_rel_gap"] == 0
    assert all("mip_rel_gap" not in option for option in options[1:])


def test_below_classifier_resolution_cannot_create_export_indicator():
    source = cycle(100)
    first = replace(
        source.slots[0], start=source.slots[0].end - timedelta(milliseconds=1)
    )
    result = solve(
        replace(source, initial_export_active=True, slots=(first, source.slots[1]))
    )
    assert min(result.flows[0].discharge_kwh, result.flows[0].grid_export_kwh) <= 1e-6
    assert result.new_export_episodes == 0


def test_export_power_floor_applies_even_when_continuing_with_duration_zero():
    source = cycle(100, initial_export_active=True)
    source = replace(source, battery=replace(source.battery, discharge_kw=0.05))
    assert solve(source).flows[0].discharge_kwh == pytest.approx(0)
    assert solve(replace(source, minimum_export_episode_benefit=0)).flows[
        0
    ].discharge_kwh == pytest.approx(0.05)


async def test_future_export_does_not_create_a_current_record(export_entry, hass):
    from copy import deepcopy

    from custom_components.energy_compass.config_models import PriceSource
    from custom_components.energy_compass.sources.bindings import (
        EntityBinding,
        IntervalBinding,
    )

    coordinator = export_entry.runtime_data
    config = deepcopy(dict(export_entry.data))
    config["settings"].update(sell_rate=-1, terminal_value_per_kwh=10)
    hass.config_entries.async_update_entry(export_entry, data=config)
    await coordinator.async_recalculate()
    assert coordinator._export_commitment is None
    config = deepcopy(config)
    config["settings"].update(capacity_kwh=1, terminal_value_per_kwh=0)
    config["sources"]["sell"] = PriceSource(
        "forecast",
        (
            IntervalBinding(
                EntityBinding("sensor.sale", attribute="rows"),
                start_path="start",
                interval_minutes=60,
                value_path="price",
                unit="PLN/kWh",
                value_kind="price",
            ),
        ),
    ).to_dict()
    hass.states.async_set(
        "sensor.sale",
        "ready",
        {
            "rows": [
                {"start": "2026-09-18T00:00:00+00:00", "price": -1},
                {"start": "2026-09-18T01:00:00+00:00", "price": 5},
            ]
        },
    )
    hass.config_entries.async_update_entry(export_entry, data=config)
    await coordinator.async_recalculate()
    assert coordinator.data["valid"]
    assert coordinator.data["intervals"][0]["discharge_kwh"] == pytest.approx(0)
    assert sum(
        row["discharge_kwh"] for row in coordinator.data["intervals"]
    ) == pytest.approx(1)
    assert coordinator.data["dispatch_policy"]["new_export_episodes"] == 1
    assert coordinator._export_commitment is None


async def test_pv_only_export_clears_current_record(export_entry, hass):
    from copy import deepcopy

    from custom_components.energy_compass.config_models import PvSource
    from custom_components.energy_compass.sources.bindings import (
        EntityBinding,
        IntervalBinding,
    )

    coordinator = export_entry.runtime_data
    config = deepcopy(dict(export_entry.data))
    config["settings"].update(discharge_kw=0)
    config["sources"]["pv"] = PvSource(
        True,
        (
            (
                IntervalBinding(
                    EntityBinding("sensor.sun", attribute="rows"),
                    start_path="start",
                    interval_minutes=60,
                    value_path="energy",
                    unit="kWh",
                    value_kind="energy",
                ),
            ),
        ),
    ).to_dict()
    hass.states.async_set(
        "sensor.sun",
        "ready",
        {
            "rows": [
                {"start": "2026-09-18T00:00:00+00:00", "energy": 1},
                {"start": "2026-09-18T01:00:00+00:00", "energy": 1},
            ]
        },
    )
    hass.config_entries.async_update_entry(export_entry, data=config)
    await coordinator.async_recalculate()
    assert coordinator.data["valid"]
    assert coordinator.data["intervals"][0]["grid_export_kwh"] > 0
    assert coordinator.data["intervals"][0]["discharge_kwh"] == pytest.approx(0)
    assert coordinator._export_commitment is None


async def test_expired_stored_export_record_cannot_pre_pay_a_new_period(
    export_entry, hass, freezer
):
    from copy import deepcopy

    coordinator = export_entry.runtime_data
    assert coordinator._export_commitment
    config = deepcopy(dict(export_entry.data))
    config["settings"]["minimum_export_episode_benefit"] = 100
    hass.config_entries.async_update_entry(export_entry, data=config)
    assert await hass.config_entries.async_unload(export_entry.entry_id)
    freezer.move_to("2026-09-18T02:00:00+00:00")
    hass.states.async_set("sensor.soc", "100")
    assert await hass.config_entries.async_setup(export_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = export_entry.runtime_data
    assert coordinator.data["valid"]
    assert coordinator.data["intervals"][0]["discharge_kwh"] == pytest.approx(0)
    assert coordinator._export_commitment is None


async def test_continuity_uses_physical_rows_not_display_label(export_entry):
    from copy import deepcopy

    coordinator = export_entry.runtime_data
    before = coordinator._export_commitment.copy()
    result = deepcopy(coordinator.data)
    for row in result["intervals"]:
        row["state"] = "CURTAIL"
        row["dispatch_mode"] = None
    coordinator._commit_current_export(result)
    assert coordinator._export_commitment == before
