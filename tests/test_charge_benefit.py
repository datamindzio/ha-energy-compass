"""Economic and native proofs for additional grid-fed battery-charge periods."""

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


def cycle(buy=0.57, **changes):
    """Mirror test_export_benefit.cycle with grid-charge and export roles swapped."""
    start = datetime(2026, 9, 18, tzinfo=UTC)
    return replace(
        Problem(
            tuple(
                Slot(
                    start + timedelta(hours=i),
                    start + timedelta(hours=i + 1),
                    price,
                    0.61,
                    0,
                    0,
                )
                for i, price in enumerate((buy, 100))
            ),
            SiteLimits(2, 2, 2, True),
            Battery(1, 0, 1, 0, 1, 1, 1, 1, 0, True, True),
            "preserve_initial",
            0,
            (),
            "UTC",
            minimum_mode_minutes=60,
            limit_export_to_pv=False,
            minimum_export_episode_benefit=0,
        ),
        **changes,
    )


def test_default_disabled_allows_cycle_but_hurdle_removes_it():
    source = cycle()
    low = solve(source)
    assert low.flows[0].charge_kwh == pytest.approx(1)
    assert low.new_grid_charge_episodes == 0
    assert low.grid_charge_episode_reserve == 0
    guarded = solve(replace(source, minimum_grid_charge_episode_benefit=1))
    assert sum(f.charge_kwh for f in guarded.flows) < 1e-6
    assert guarded.new_grid_charge_episodes == 0
    assert guarded.grid_charge_episode_reserve == 0


def test_hurdle_is_inert_without_named_mode_tracking():
    """Modes off means no CHARGE_GRID indicator to hang the hurdle on."""
    source = cycle(minimum_mode_minutes=0, minimum_grid_charge_episode_benefit=1)
    result = solve(source)
    assert result.flows[0].charge_kwh == pytest.approx(1)
    assert result.new_grid_charge_episodes == 0
    assert result.grid_charge_episode_reserve == 0


@pytest.mark.parametrize(
    "profit,threshold,charges",
    [(1.39, 1, True), (1.39, 2, False), (0.999, 1, False), (1.001, 1, True)],
)
def test_incremental_full_horizon_boundary(profit, threshold, charges):
    buy = 0.61 - profit
    result = solve(cycle(buy, minimum_grid_charge_episode_benefit=threshold))
    assert (result.flows[0].charge_kwh > 0.9) is charges
    assert result.new_grid_charge_episodes == int(charges)
    assert result.grid_charge_episode_reserve == threshold * int(charges)
    assert result.objective == pytest.approx(buy - 0.61 if charges else 0)
    assert result.objective == pytest.approx(
        result.grid_cost + result.wear_cost - result.terminal_credit
    )


def test_losses_and_wear_are_included_in_benefit():
    source = cycle(0.3, minimum_grid_charge_episode_benefit=0.1)
    inefficient = replace(
        source,
        battery=replace(
            source.battery,
            eta_charge=0.5,
            eta_discharge=0.5,
            wear_per_kwh=0.5,
        ),
    )
    assert solve(source).new_grid_charge_episodes == 1
    result = solve(inefficient)
    assert result.new_grid_charge_episodes == 0
    assert result.objective == pytest.approx(0)


def test_two_separated_profitable_periods_each_pay_once():
    source = cycle(-1, minimum_grid_charge_episode_benefit=1)
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
    assert [f.charge_kwh for f in result.flows] == pytest.approx([1, 0, 1, 0])
    assert result.new_grid_charge_episodes == 2
    assert result.grid_charge_episode_reserve == 2


def test_current_period_is_exempt_but_future_period_is_not():
    source = cycle(
        -1, minimum_grid_charge_episode_benefit=1, initial_grid_charge_active=True
    )
    result = solve(source)
    assert result.flows[0].charge_kwh == pytest.approx(1)
    assert result.new_grid_charge_episodes == 0
    assert result.grid_charge_episode_reserve == 0


@pytest.mark.parametrize("value", [-1, 1001, float("nan"), float("inf"), "bad", None])
@pytest.mark.parametrize("battery", [True, False])
def test_invalid_hurdle_rejected_even_without_battery(value, battery):
    source = cycle(minimum_grid_charge_episode_benefit=value)
    if not battery:
        source = replace(source, battery=None)
    with pytest.raises(InputError):
        solve(source)


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_continuity_is_strict_boolean(value):
    with pytest.raises(InputError):
        solve(cycle(initial_grid_charge_active=value))


def test_partial_slot_uses_elapsed_power_floor():
    source = cycle(-100, minimum_grid_charge_episode_benefit=1, minimum_mode_minutes=5)
    first = replace(source.slots[0], start=source.slots[0].end - timedelta(minutes=5))
    result = solve(replace(source, slots=(first, source.slots[1])))
    assert result.flows[0].charge_kwh == pytest.approx(1 / 12)
    assert result.new_grid_charge_episodes == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("charge_start", 0),
        ("charge_active", 0),
        ("charge_start", 0.5),
    ],
)
def test_independent_validation_rejects_corrupt_charge_vectors(monkeypatch, key, value):
    from custom_components.energy_compass.engine import optimize

    original = optimize._validate_solution

    def corrupt(problem, vectors, values, fractions, budgets):
        values[vectors[0][key]] = value
        return original(problem, vectors, values, fractions, budgets)

    monkeypatch.setattr(optimize, "_validate_solution", corrupt)
    with pytest.raises(SolveError, match="charge"):
        solve(cycle(-1, minimum_grid_charge_episode_benefit=1))


def test_solver_requests_zero_gap_only_for_enabled_battery_policy(monkeypatch):
    from custom_components.energy_compass.engine import optimize

    original = optimize.milp
    options = []

    def capture(**kwargs):
        options.append(kwargs["options"])
        return original(**kwargs)

    monkeypatch.setattr(optimize, "milp", capture)
    solve(cycle(-1, minimum_grid_charge_episode_benefit=1))
    solve(cycle(-1, minimum_grid_charge_episode_benefit=0))
    solve(replace(cycle(-1, minimum_grid_charge_episode_benefit=1), battery=None))
    assert options[0]["mip_rel_gap"] == 0
    assert all("mip_rel_gap" not in option for option in options[1:])


def test_probe_difference_excludes_decision_reserve():
    from custom_components.energy_compass.engine.consumption import (
        consumption_outlook,
    )
    from custom_components.energy_compass.engine.models import (
        CompassSettings,
        plan_monetary_cost,
    )

    source = cycle(-1, minimum_grid_charge_episode_benefit=1)
    base = solve(source)
    assert base.grid_charge_episode_reserve == 1
    assert plan_monetary_cost(base) == pytest.approx(base.objective)
    candidate = solve(
        replace(
            source,
            slots=(source.slots[0], replace(source.slots[1], sell_per_kwh=0)),
        )
    )
    assert plan_monetary_cost(candidate) == pytest.approx(candidate.objective)
    result = consumption_outlook(
        source,
        base,
        settings=CompassSettings(display_horizon_hours=2, reference_horizon_hours=2),
    )
    assert result[0].cost_per_kwh is not None


def test_native_grid_charge_hurdle_defaults_to_zero():
    from custom_components.energy_compass.runtime import build_problem
    from custom_components.energy_compass.settings import default_configuration

    config = default_configuration("PLN", "UTC")
    assert config["settings"]["minimum_grid_charge_episode_benefit"] == 0
    source, values, _ = build_problem(config, {}, datetime(2026, 9, 18, tzinfo=UTC))
    assert (
        source.minimum_grid_charge_episode_benefit
        == values["minimum_grid_charge_episode_benefit"]
        == 0
    )


def test_alternating_grid_charge_intervals_consolidate_when_hurdle_raised():
    """Motivating case: floor-sized grid-charge intervals separated by cheaper
    slots. Without a hurdle the cheapest slots are used, however scattered;
    once episode starts cost more than the price spread, the solver bunches
    the same floor-sized imports into one contiguous run instead.
    """
    start = datetime(2026, 9, 18, tzinfo=UTC)
    duration = timedelta(minutes=15)
    prices = (1, 9, 1, 9, 1, 9)
    slots = tuple(
        Slot(start + i * duration, start + (i + 1) * duration, price, 0, 0, 0)
        for i, price in enumerate(prices)
    )
    problem = Problem(
        slots,
        SiteLimits(1, 0.1, 0, True),
        Battery(0.075, 0, 1, 0, 1, 0, 1, 1, 0, True, False),
        "value",
        100,
        (),
        "UTC",
        minimum_mode_minutes=15,
        minimum_mode_power_kw=0.1,
        minimum_export_episode_benefit=0,
        limit_export_to_pv=False,
        # A negligible hurdle enables episode accounting without perturbing the
        # physical choice, mirroring a practically-disabled "0" hurdle.
        minimum_grid_charge_episode_benefit=1e-6,
    )
    scattered = solve(problem)
    assert sum(f.charge_kwh for f in scattered.flows) == pytest.approx(0.075)
    assert scattered.new_grid_charge_episodes == 3
    assert [f.charge_kwh > 1e-6 for f in scattered.flows] == [
        True,
        False,
        True,
        False,
        True,
        False,
    ]

    consolidated = solve(replace(problem, minimum_grid_charge_episode_benefit=0.15))
    assert sum(f.charge_kwh for f in consolidated.flows) == pytest.approx(0.075)
    assert consolidated.new_grid_charge_episodes < scattered.new_grid_charge_episodes
    assert consolidated.new_grid_charge_episodes == 1


@pytest.fixture
async def grid_charge_entry(recorder_mock, hass, enable_custom_integrations, freezer):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.energy_compass.settings import default_configuration
    from custom_components.energy_compass.sources.bindings import EntityBinding

    freezer.move_to("2026-09-18T00:00:00+00:00")
    hass.states.async_set("sensor.soc", "0")
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
        grid_export_kw=0,
        allow_battery_export=False,
        allow_grid_charge=True,
        limit_export_to_pv=False,
        minimum_mode_minutes=15,
        daily_load_kwh=0,
        buy_rate=-1,
        sell_rate=0,
        terminal_mode="value",
        terminal_value_per_kwh=10,
        soc_max_age_seconds=3600,
        minimum_grid_charge_episode_benefit=1,
    )
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="ChargeBenefit", version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)


async def test_current_grid_charge_survives_recalculation_and_restart(
    grid_charge_entry, hass, freezer
):
    from homeassistant.helpers.storage import Store

    coordinator = grid_charge_entry.runtime_data
    assert coordinator.data["valid"]
    policy = coordinator.data["dispatch_policy"]
    assert policy["minimum_grid_charge_episode_benefit"] == 1
    assert policy["grid_charge_benefit_scope"] == "grid_fed_battery_charge_period"
    assert policy["new_grid_charge_episodes"] == policy["grid_charge_episode_reserve"]
    assert policy["new_grid_charge_episodes"] == 1
    record = coordinator._grid_charge_commitment
    assert record is not None
    freezer.move_to("2026-09-18T00:15:00+00:00")
    await coordinator.async_recalculate()
    assert coordinator.data["dispatch_policy"]["new_grid_charge_episodes"] == 0
    record = coordinator._grid_charge_commitment
    assert await hass.config_entries.async_unload(grid_charge_entry.entry_id)
    assert (
        await Store(
            hass, 1, f"energy_compass.{grid_charge_entry.entry_id}.grid_charge"
        ).async_load()
        == record
    )
    assert await hass.config_entries.async_setup(grid_charge_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (
        grid_charge_entry.runtime_data.data["dispatch_policy"][
            "new_grid_charge_episodes"
        ]
        == 0
    )
