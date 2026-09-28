"""Physical proofs for operating-mode dwell and the daily PV export budget."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from custom_components.energy_compass.engine.models import (
    Battery,
    Problem,
    SiteLimits,
    Slot,
)
from custom_components.energy_compass.engine.optimize import solve
from custom_components.energy_compass.runtime import build_problem
from custom_components.energy_compass.settings import (
    default_configuration,
    validate_configuration,
)
from custom_components.energy_compass.sources.bindings import EntityBinding


def dispatch(rows, *, minutes=15, **changes):
    start = datetime(2026, 9, 18, tzinfo=UTC)
    return replace(
        Problem(
            tuple(
                Slot(
                    start + timedelta(minutes=i * minutes),
                    start + timedelta(minutes=(i + 1) * minutes),
                    *row,
                )
                for i, row in enumerate(rows)
            ),
            SiteLimits(8, 8, 8, True),
            Battery(10, 0, 1, 0, 4, 4, 1, 1, 0.01, True, True),
            "value",
            0,
            (),
            "UTC",
        ),
        **changes,
    )


def test_defaults_reach_existing_entry_and_engine():
    config = default_configuration("PLN", "UTC")
    assert config["settings"]["minimum_mode_minutes"] == 60
    assert config["settings"]["limit_export_to_pv"] is True
    del config["settings"]["minimum_mode_minutes"]
    del config["settings"]["limit_export_to_pv"]
    config["sources"].update(
        battery_enabled=True, soc=EntityBinding("sensor.soc").to_dict()
    )
    now = datetime(2026, 9, 18, tzinfo=UTC)
    states = {"sensor.soc": {"state": "50", "attributes": {}, "last_updated": now}}
    source, values, _ = build_problem(config, states, now)
    assert values["limit_export_to_pv"] is True
    assert source.minimum_mode_minutes == 60
    assert source.limit_export_to_pv is True


def test_zero_pv_means_zero_export_but_grid_charge_can_supply_home():
    source = dispatch(((0.1, 0, 0, 0), (2, 5, 0, 1)), minutes=60)
    result = solve(source)
    assert result.flows[0].charge_kwh == pytest.approx(1)
    assert result.flows[1].discharge_kwh == pytest.approx(1)
    assert sum(f.grid_export_kwh for f in result.flows) == pytest.approx(0)


def test_solar_can_be_stored_and_exported_with_losses():
    source = dispatch(((1, 0, 4, 0), (1, 5, 0, 0)), minutes=60)
    source = replace(
        source, battery=replace(source.battery, eta_charge=0.9, eta_discharge=0.8)
    )
    result = solve(source)
    assert result.flows[0].end_soc_kwh == pytest.approx(3.6)
    assert result.flows[1].grid_export_kwh == pytest.approx(2.88)
    assert result.flows[1].end_soc_kwh == pytest.approx(0)


def test_initial_soc_does_not_create_an_extra_export_budget():
    source = dispatch(((1, 5, 0, 0),), minutes=60)
    source = replace(source, battery=replace(source.battery, initial_kwh=5))
    assert solve(source).flows[0].grid_export_kwh == pytest.approx(0)


def test_export_budget_uses_gross_pv_generation_not_surplus_or_origin():
    source = dispatch(((1, 5, 1, 1),), minutes=60)
    source = replace(source, battery=replace(source.battery, initial_kwh=5))
    result = solve(source)
    assert result.flows[0].discharge_kwh == pytest.approx(1)
    assert result.flows[0].grid_export_kwh == pytest.approx(1)


def test_grid_charged_energy_can_be_exported_within_pv_budget():
    source = dispatch(((0, 0, 1, 1), (1, 5, 0, 0)), minutes=60)
    result = solve(source)
    assert result.flows[0].grid_import_kwh == pytest.approx(1)
    assert result.flows[1].grid_export_kwh == pytest.approx(1)


def test_policy_can_be_explicitly_disabled():
    source = dispatch(((0.1, 0, 0, 0), (1, 5, 0, 0)), minutes=60)
    source = replace(source, limit_export_to_pv=False)
    assert solve(source).flows[1].grid_export_kwh > 0


def test_initial_soc_can_be_exported_against_same_day_pv_generation():
    source = dispatch(((1, 10, 0, 0), (1, 0, 2, 2)), minutes=60)
    source = replace(source, battery=replace(source.battery, initial_kwh=5))
    result = solve(source)
    assert result.flows[0].grid_export_kwh == pytest.approx(2)
    assert sum(f.grid_export_kwh for f in result.flows) == pytest.approx(2)


def test_tomorrows_pv_cannot_fund_todays_export():
    source = dispatch(((1, 10, 0, 0), (1, 0, 2, 2)), minutes=60)
    shift = timedelta(hours=23)
    source = replace(
        source,
        battery=replace(source.battery, initial_kwh=5),
        slots=tuple(
            replace(s, start=s.start + shift, end=s.end + shift) for s in source.slots
        ),
    )
    result = solve(source)
    assert result.flows[0].grid_export_kwh == pytest.approx(0)


def test_today_credit_cannot_carry_to_tomorrow():
    source = dispatch(((1, 0, 2, 2), (1, 10, 0, 0)), minutes=60)
    shift = timedelta(hours=23)
    source = replace(
        source,
        battery=replace(source.battery, initial_kwh=5),
        slots=tuple(
            replace(s, start=s.start + shift, end=s.end + shift) for s in source.slots
        ),
    )
    assert solve(source).flows[1].grid_export_kwh == pytest.approx(0)


def test_observed_pv_minus_export_sets_remaining_today_credit():
    source = dispatch(((1, 10, 0, 0),), minutes=60)
    source = replace(
        source,
        battery=replace(source.battery, initial_kwh=5),
        pv_generated_today_kwh=8,
        grid_exported_today_kwh=6,
    )
    assert solve(source).flows[0].grid_export_kwh == pytest.approx(2)


def test_observed_deficit_is_not_reset_to_zero():
    source = dispatch(((1, 10, 1, 1),), minutes=60)
    source = replace(
        source,
        battery=replace(source.battery, initial_kwh=5),
        pv_generated_today_kwh=5,
        grid_exported_today_kwh=6,
    )
    assert solve(source).flows[0].grid_export_kwh == pytest.approx(0)


def test_direct_pv_export_and_battery_export_share_one_budget():
    source = dispatch(((1, 100, 2, 0), (1, 10, 0, 0)), minutes=60)
    source = replace(source, battery=replace(source.battery, initial_kwh=5))
    result = solve(source)
    assert result.flows[0].grid_export_kwh == pytest.approx(2)
    assert result.flows[1].grid_export_kwh == pytest.approx(0)


def test_curtailed_pv_cannot_increase_the_export_budget():
    source = dispatch(((1, 100, 10, 0), (1, 10, 0, 0)), minutes=60)
    source = replace(
        source,
        site=replace(source.site, inverter_kw=1),
        battery=replace(source.battery, initial_kwh=5, charge_kw=0),
    )
    result = solve(source)
    assert result.flows[0].curtail_kwh == pytest.approx(9)
    assert result.flows[0].grid_export_kwh == pytest.approx(1)
    assert result.flows[1].grid_export_kwh == pytest.approx(0)


def test_minimum_named_duration_stops_quarter_hour_arbitrage():
    source = dispatch(tuple((0.1 if i % 2 == 0 else 2, 0, 0, 1) for i in range(12)))
    result = solve(source)
    starts = [source.slots[0].start]
    for index in range(1, len(result.flows)):
        if result.flows[index].dispatch_mode != result.flows[index - 1].dispatch_mode:
            starts.append(source.slots[index].start)
    assert len(starts) > 1
    assert all(b - a >= timedelta(hours=1) for a, b in pairwise(starts))
    assert result.objective > solve(replace(source, minimum_mode_minutes=0)).objective


def test_short_first_interval_uses_elapsed_time_not_slot_count():
    source = dispatch(tuple((0 if i == 0 else 3, 0, 0, 1) for i in range(8)))
    source = replace(
        source,
        slots=(
            replace(
                source.slots[0],
                start=source.slots[0].start + timedelta(minutes=10),
                load_kwh=1 / 3,
            ),
            *source.slots[1:],
        ),
    )
    result = solve(source)
    first = result.flows[0].dispatch_mode
    assert all(
        f.dispatch_mode == first
        for s, f in zip(source.slots, result.flows)
        if s.start < source.slots[0].start + timedelta(hours=1)
    )


def test_replanning_respects_remaining_direction_hold():
    source = dispatch(tuple((5, 0, 0, 1) for _ in range(8)))
    source = replace(
        source,
        battery=replace(source.battery, initial_kwh=5),
        initial_battery_mode="charge",
        initial_battery_mode_since=source.slots[0].start - timedelta(minutes=20),
    )
    result = solve(source)
    assert all(f.discharge_kwh == pytest.approx(0) for f in result.flows[:3])
    assert result.flows[3].discharge_kwh == pytest.approx(0)
    assert result.flows[4].discharge_kwh > 0


@pytest.mark.parametrize("value", [-1, float("nan"), 1441])
def test_invalid_duration_rejected(value):
    config = default_configuration("PLN", "UTC")
    config["settings"]["minimum_mode_minutes"] = value
    with pytest.raises(ValueError):
        validate_configuration(config, {}, datetime.now(UTC))


def test_legacy_charge_guard_can_publish_hold_at_soc_ceiling():
    source = dispatch(((5, 0, 0, 1),) * 4)
    source = replace(
        source,
        battery=replace(source.battery, initial_kwh=10),
        initial_battery_mode="charge",
        initial_battery_mode_since=source.slots[0].start,
    )
    result = solve(source)
    assert all(
        f.dispatch_mode == "HOLD"
        and abs(f.charge_kwh) < 1e-6
        and abs(f.discharge_kwh) < 1e-6
        for f in result.flows
    )


async def test_direction_commitment_survives_recalculate_and_reload(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    from homeassistant.helpers.storage import Store
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    freezer.move_to("2026-09-18T10:00:00+00:00")
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
    hass.states.async_set("sensor.soc", "50")
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Policy", version=2
    )
    entry.add_to_hass(hass)
    commitment = {"mode": "charge", "since": "2026-09-18T09:40:00+00:00"}
    await Store(hass, 1, f"energy_compass.{entry.entry_id}.dispatch").async_save(
        commitment
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    assert coordinator.data["valid"]
    assert coordinator.data["intervals"][0]["state"] == "HOLD"
    expected = {
        "mode": "HOLD",
        "since": "2026-09-18T10:00:00+00:00",
        "legacy_direction": commitment,
    }
    assert coordinator._battery_commitment == expected
    assert (
        hass.states.get("sensor.policy_plan").attributes["dispatch_policy"][
            "minimum_mode_minutes"
        ]
        == 60
    )
    freezer.move_to("2026-09-18T10:20:00+00:00")
    await coordinator.async_recalculate()
    assert coordinator.data["valid"]
    assert coordinator.data["intervals"][0]["state"] == "HOLD"
    assert coordinator._battery_commitment == expected
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data._battery_commitment == expected
    assert entry.runtime_data.data["intervals"][0]["state"] == "HOLD"
    assert await hass.config_entries.async_unload(entry.entry_id)
