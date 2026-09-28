"""A purchase ceiling constrains physical grid charging, never household supply."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine.dispatch_policy import safety_exception
from custom_components.energy_compass.engine.models import (
    Battery,
    InputError,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import solve
from custom_components.energy_compass.runtime import build_problem, compute
from custom_components.energy_compass.settings import default_configuration


def charging_problem(prices, *, cap=0.61, dwell=0, pv=0, load=0):
    start = datetime(2026, 9, 18, tzinfo=UTC)
    return Problem(
        tuple(
            Slot(
                start + timedelta(hours=i),
                start + timedelta(hours=i + 1),
                p,
                0,
                pv,
                load,
            )
            for i, p in enumerate(prices)
        ),
        SiteLimits(10, 10, 10, True),
        Battery(10, 0, 1, 0, 1, 1, 1, 1, 0, True, False),
        "value",
        10,
        (),
        "UTC",
        minimum_mode_minutes=dwell,
        maximum_grid_charge_price=cap,
    )


@pytest.mark.parametrize("dwell", [0, 60])
@pytest.mark.parametrize(
    "price, charge", [(0.60, 1), (0.61, 1), (0.610001, 0), (1.25, 0)]
)
def test_effective_purchase_price_caps_grid_charging(price, charge, dwell):
    plan = solve(charging_problem([price], dwell=dwell, load=2))
    assert plan.flows[0].charge_kwh == pytest.approx(charge)
    assert plan.flows[0].grid_import_kwh == pytest.approx(2 + charge)


def test_disabled_ceiling_preserves_expensive_charging():
    assert solve(charging_problem([1.25], cap=None)).flows[
        0
    ].charge_kwh == pytest.approx(1)


def test_independent_validation_rejects_expensive_grid_charge(monkeypatch):
    from custom_components.energy_compass.engine import optimize

    original = optimize._Model.solve
    captured = []

    def capture(model, limit):
        result = original(model, limit)
        captured.append(result)
        return result

    monkeypatch.setattr(optimize._Model, "solve", capture)
    solve(charging_problem([1.25], cap=None))
    monkeypatch.setattr(optimize._Model, "solve", lambda *args: captured[0])
    with pytest.raises(SolveError, match="grid charging capability"):
        solve(charging_problem([1.25]))


@pytest.mark.parametrize("price, charge", [(-0.01, 1), (0, 1), (0.01, 0)])
def test_zero_ceiling_means_free_or_negative_prices(price, charge):
    assert solve(charging_problem([price], cap=0)).flows[0].charge_kwh == pytest.approx(
        charge
    )


def test_equivalent_float_price_is_not_above_ceiling():
    assert solve(charging_problem([0.1 + 0.2], cap=0.3)).flows[
        0
    ].charge_kwh == pytest.approx(1)


@pytest.mark.parametrize("pv, load, charge, grid", [(2, 1, 1, 0), (1, 2, 0, 1)])
def test_pv_surplus_can_charge_but_cannot_disguise_grid_charging(
    pv, load, charge, grid
):
    plan = solve(charging_problem([1.25], pv=pv, load=load))
    assert plan.flows[0].charge_kwh == pytest.approx(charge)
    assert plan.flows[0].grid_import_kwh == pytest.approx(grid)


def test_charge_run_cannot_start_without_full_cheap_dwell():
    source = charging_problem([0.5, 1.25, 0.5, 0.5], dwell=60)
    start = source.slots[0].start
    source = replace(
        source,
        slots=tuple(
            replace(
                s,
                start=start + timedelta(minutes=30 * i),
                end=start + timedelta(minutes=30 * (i + 1)),
            )
            for i, s in enumerate(source.slots)
        ),
    )
    flows = solve(source).flows
    assert [f.charge_kwh for f in flows] == pytest.approx([0, 0, 0.5, 0.5])


def test_curtailment_cannot_disguise_expensive_grid_charging():
    plan = solve(charging_problem([-0.05], cap=-0.1, pv=2, load=1))
    assert plan.flows[0].charge_kwh == pytest.approx(1)
    assert plan.flows[0].grid_import_kwh == pytest.approx(0)
    assert plan.flows[0].curtail_kwh == pytest.approx(0)


def test_ceiling_allows_curtailment_and_household_import_without_charging():
    source = charging_problem([-0.05], cap=-0.1, pv=2, load=1)
    source = replace(source, battery=replace(source.battery, initial_kwh=10))
    plan = solve(source)
    assert plan.flows[0].charge_kwh == pytest.approx(0)
    assert plan.flows[0].grid_import_kwh == pytest.approx(1)
    assert plan.flows[0].curtail_kwh == pytest.approx(2)


@pytest.mark.parametrize("first_price", [0.5, 1.25])
def test_carried_grid_charge_lock_pauses_when_ceiling_conflicts(first_price):
    source = charging_problem([first_price, 1.25, 0.5, 0.5], dwell=60)
    now = source.slots[0].start
    source = replace(
        source,
        slots=tuple(
            replace(
                s,
                start=now + timedelta(minutes=15 * i),
                end=now + timedelta(minutes=15 * (i + 1)),
            )
            for i, s in enumerate(source.slots)
        ),
        initial_dispatch_mode="CHARGE_GRID",
        initial_dispatch_mode_since=now - timedelta(minutes=30),
    )
    exception = safety_exception(source)
    assert exception["reason"] == "grid_charge_price_limit"
    assert exception["deadline"] == (now + timedelta(minutes=30)).isoformat()
    flows = solve(source).flows
    assert all(f.dispatch_mode == "HOLD" for f in flows)
    assert all(f.charge_kwh == pytest.approx(0) for f in flows)


def test_price_ceiling_does_not_interrupt_pv_charge_lock():
    source = charging_problem([1.25], dwell=60, pv=2, load=1)
    source = replace(
        source,
        initial_dispatch_mode="CHARGE_PV",
        initial_dispatch_mode_since=source.slots[0].start - timedelta(minutes=15),
    )
    assert safety_exception(source) is None
    assert solve(source).flows[0].charge_kwh == pytest.approx(1)


@pytest.mark.parametrize("cap", [True, float("nan"), float("inf"), -1001, 1001])
def test_invalid_ceiling_rejected_even_without_battery(cap):
    with pytest.raises(InputError):
        solve(replace(charging_problem([1], cap=cap), battery=None))


def native_config():
    config = default_configuration("PLN", "UTC")
    config["sources"].update(battery_enabled=True, soc={"entity_id": "sensor.soc"})
    config["settings"].update(
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
        capacity_kwh=10,
        charge_kw=1,
        discharge_kw=1,
        inverter_kw=10,
        allow_grid_charge=True,
        terminal_mode="value",
        terminal_value_per_kwh=10,
        daily_load_kwh=24,
        buy_rate=0.5,
        buy_addition=0.2,
        minimum_mode_minutes=60,
        limit_grid_charge_price=True,
        maximum_grid_charge_price=0.61,
    )
    return config


def test_native_cap_uses_final_tariff_and_exposes_policy():
    config = native_config()
    now = datetime(2026, 9, 18, tzinfo=UTC)
    states = {"sensor.soc": {"state": "0", "attributes": {}, "last_updated": now}}
    result = compute(config, states, now)
    assert result["valid"]
    assert result["dispatch_policy"]["maximum_grid_charge_price"] == 0.61
    assert result["dispatch_policy"]["limit_grid_charge_price"] is True
    assert all(row["charge_kwh"] == pytest.approx(0) for row in result["intervals"])
    assert sum(row["grid_import_kwh"] for row in result["intervals"]) == pytest.approx(
        2
    )


def test_existing_configuration_keeps_ceiling_disabled_and_helper_can_enable_it():
    config = default_configuration("PLN", "UTC")
    config["settings"].pop("limit_grid_charge_price", None)
    config["settings"].pop("maximum_grid_charge_price", None)
    now = datetime(2026, 9, 18, tzinfo=UTC)
    assert build_problem(config, {}, now)[0].maximum_grid_charge_price is None
    config["settings"]["limit_grid_charge_price"] = True
    config["helpers"]["maximum_grid_charge_price"] = {
        "entity": {"entity_id": "input_number.cap"},
        "unit": "PLN/kWh",
        "max_age_seconds": None,
    }
    states = {
        "input_number.cap": {
            "state": "-0.05",
            "attributes": {"unit_of_measurement": "PLN/kWh"},
            "last_updated": now,
        }
    }
    assert build_problem(config, states, now)[0].maximum_grid_charge_price == -0.05
    config["helpers"]["maximum_grid_charge_price"]["unit"] = "EUR/kWh"
    with pytest.raises(InputError):
        build_problem(config, states, now)


async def test_price_pause_preserves_carried_clock_across_reload(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from homeassistant.helpers.storage import Store
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    freezer.move_to("2026-09-18T00:30:00+00:00")
    hass.states.async_set("sensor.soc", "10")
    entry = MockConfigEntry(
        domain="energy_compass", data=native_config(), title="Price cap", version=2
    )
    entry.add_to_hass(hass)
    record = {"mode": "CHARGE_GRID", "since": "2026-09-18T00:00:00+00:00"}
    await Store(hass, 1, f"energy_compass.{entry.entry_id}.dispatch").async_save(record)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    try:
        assert entry.runtime_data.data["valid"]
        assert entry.runtime_data.data["intervals"][0]["state"] == "HOLD"
        assert entry.runtime_data._battery_commitment == record
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.runtime_data.data["valid"]
        assert entry.runtime_data._battery_commitment == record
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
