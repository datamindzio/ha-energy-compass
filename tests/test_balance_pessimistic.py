from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.config_models import (
    PvSource,
    SourceConfig,
    validate_sources,
)
from custom_components.energy_compass.engine.balance import candidate_windows
from custom_components.energy_compass.engine.models import (
    Battery,
    InputError,
    Problem,
    SiteLimits,
    Slot,
)
from custom_components.energy_compass.engine.optimize import solve
from custom_components.energy_compass.runtime import (
    available_forecasts,
    build_problem,
    compute,
)
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.source_management import (
    SourceRef,
    remove_source,
    replace_source,
    selected_source,
    source_inventory,
)
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)

T0 = datetime(2026, 9, 24, 0, tzinfo=UTC)
CAP = 25.0


def _slots(rows):
    return tuple(
        Slot(
            T0 + timedelta(hours=i),
            T0 + timedelta(hours=i + 1),
            buy,
            0.1,
            pv,
            load,
        )
        for i, (buy, pv, load) in enumerate(rows)
    )


def _battery(initial):
    return Battery(CAP, 0.1, 1.0, initial, 8, 8, 1.0, 1.0, 0.0, True, False)


def _windows(slots, pessimistic=None, *, initial=20.0, hold=60, ok=None):
    return candidate_windows(
        slots,
        phase="eligible",
        now=T0,
        hold_minutes=hold,
        remaining_minutes=0,
        grid_charge_ok=ok or (True,) * len(slots),
        pessimistic_pv_kwh=pessimistic,
        battery=_battery(initial),
        threshold_kwh=0.99 * CAP,
    )


MEDIAN = [(0.5, 3.0, 1.0)] * 4


def test_no_pessimistic_source_keeps_todays_candidates():
    slots = _slots(MEDIAN)
    today = candidate_windows(
        slots,
        phase="eligible",
        now=T0,
        hold_minutes=60,
        remaining_minutes=0,
        grid_charge_ok=(True,) * 4,
    )
    assert today == _windows(slots)
    assert {w.mode for w in today} == {"CHARGE_PV"}


def test_pessimistic_covering_load_and_reaching_threshold_keeps_pv_windows():
    slots = _slots(MEDIAN)
    windows = _windows(slots, (3.0,) * 4, initial=25.0)
    assert windows == _windows(slots)


def test_pessimistic_pv_below_load_turns_window_into_grid_window():
    slots = _slots(MEDIAN)
    windows = _windows(slots, (0.2,) * 4, initial=24.0)
    assert [w.mode for w in windows] == ["CHARGE_GRID"] * 4


def test_pack_cannot_reach_threshold_on_pessimistic_series():
    slots = _slots(MEDIAN)
    # Surplus of 0.05 kWh per slot never lifts 20 kWh to 24.75 kWh.
    windows = _windows(slots, (1.05,) * 4, initial=20.0)
    assert [w.mode for w in windows] == ["CHARGE_GRID"] * 4


def test_pessimistic_surplus_before_the_window_is_enough_to_reach_threshold():
    slots = _slots(MEDIAN)
    windows = _windows(slots, (3.0,) * 4, initial=22.0)
    # 22 kWh + 2 kWh surplus per slot (limited by 8 kW charge) passes 24.75 in
    # two slots, so only the later starts stay PV windows.
    assert [(w.slots, w.mode) for w in windows] == [
        ((0,), "CHARGE_GRID"),
        ((1,), "CHARGE_GRID"),
        ((2,), "CHARGE_PV"),
        ((3,), "CHARGE_PV"),
    ]


def test_pessimistic_failure_without_grid_charge_leaves_no_window():
    slots = _slots(MEDIAN)
    assert _windows(slots, (0.2,) * 4, initial=24.0, ok=(False,) * 4) == ()


def test_solver_picks_cheap_grid_window_when_pessimistic_pv_is_insufficient():
    # PV below the load, so the window's price matters: with a PV surplus every
    # feasible start costs the same and the pick is a solver tie-break.
    rows = [(0.9, 0.5, 1.0), (0.2, 0.5, 1.0), (0.2, 0.5, 1.0), (0.9, 0.5, 1.0)]
    slots = _slots(rows)
    windows = _windows(slots, (0.2,) * 4, initial=24.0)
    assert all(w.mode == "CHARGE_GRID" for w in windows)
    problem = Problem(
        slots,
        SiteLimits(8, 25, 8, False),
        _battery(24.0),
        "value",
        0.0,
        (),
        "UTC",
        minimum_mode_minutes=15,
        balance_windows=windows,
        balance_threshold_kwh=0.99 * CAP,
        balance_miss_cost=100.0,
    )
    plan = solve(problem)
    assert plan.balance_start in (1, 2)
    window = next(w for w in windows if w.slots[0] == plan.balance_start)
    assert window.mode == "CHARGE_GRID"


# --- runtime ---------------------------------------------------------------

NOW = datetime(2026, 9, 24, 10, tzinfo=UTC)
DUE = {
    "last_completed_at": (NOW - timedelta(days=8)).isoformat(),
    "hold_started_at": None,
}


def _binding(entity):
    return IntervalBinding(
        EntityBinding(entity, attribute="rows"),
        start_path="start",
        end_path="end",
        value_path="value",
        unit="kWh",
    )


def _rows(value):
    return [
        {
            "start": NOW + timedelta(hours=i),
            "end": NOW + timedelta(hours=i + 1),
            "value": value,
        }
        for i in range(4)
    ]


def _config(*, pessimistic=True):
    config = default_configuration("EUR", "UTC")
    config["sources"].update(
        battery_enabled=True, soc=EntityBinding("sensor.soc").to_dict()
    )
    config["sources"]["pv"] = PvSource(
        True,
        ((_binding("sensor.pv"),),),
        pessimistic=(_binding("sensor.pv_low"),) if pessimistic else (),
    ).to_dict()
    config["settings"].update(
        capacity_kwh=20,
        operating_floor=15,
        hardware_floor=10,
        soc_ceiling=100,
        inverter_kw=5,
        grid_import_kw=7,
        grid_export_kw=2,
        horizon_hours=4,
        display_horizon_hours=4,
        reference_horizon_hours=4,
        eta_charge=0.9,
        eta_discharge=0.8,
        wear_per_kwh=0.04,
        allow_grid_charge=True,
        allow_battery_export=True,
        limit_export_to_pv=False,
        lfp_balance=True,
        balance_value=50.0,
        minimum_mode_minutes=15,
    )
    return config


def _states(low=0.1, median=1.0, soc="95"):
    def entity(rows):
        return {"state": "ok", "attributes": {"rows": rows}, "last_updated": NOW}

    states = {
        "sensor.soc": {"state": soc, "attributes": {}, "last_updated": NOW},
        "sensor.pv": entity(_rows(median)),
    }
    if low is not None:
        states["sensor.pv_low"] = entity(_rows(low))
    return states


def _modes(problem):
    return [w.mode for w in problem.balance_windows]


def test_without_the_source_runtime_candidates_are_unchanged():
    problem, _, quality = build_problem(
        _config(pessimistic=False), _states(), NOW, balance_state=DUE
    )
    assert "CHARGE_PV" in _modes(problem)
    assert quality["balance_pv_series"] == "median"
    assert "balance_pessimistic_pv_unavailable" not in quality["warnings"]


def test_low_pessimistic_series_offers_only_grid_windows():
    problem, _, quality = build_problem(
        _config(), _states(low=0.1), NOW, balance_state=DUE
    )
    assert problem.balance_windows
    assert set(_modes(problem)) == {"CHARGE_GRID"}
    assert quality["balance_pv_series"] == "pessimistic"
    # The dispatch plan itself keeps using the median series.
    assert all(slot.pv_kwh == pytest.approx(1.0) for slot in problem.slots)


def test_sufficient_pessimistic_series_keeps_pv_windows():
    problem, _, quality = build_problem(
        _config(), _states(low=1.0), NOW, balance_state=DUE
    )
    assert "CHARGE_PV" in _modes(problem)
    assert quality["balance_pv_series"] == "pessimistic"


def test_unavailable_pessimistic_entity_falls_back_with_a_warning():
    reference, _, _ = build_problem(
        _config(pessimistic=False), _states(), NOW, balance_state=DUE
    )
    problem, _, quality = build_problem(
        _config(), _states(low=None), NOW, balance_state=DUE
    )
    assert problem.balance_windows == reference.balance_windows
    assert quality["balance_pv_series"] == "median"
    assert "balance_pessimistic_pv_unavailable" in quality["warnings"]


def test_pessimistic_gap_falls_back_with_a_warning():
    states = _states()
    states["sensor.pv_low"]["attributes"]["rows"] = _rows(0.1)[:2]
    problem, _, quality = build_problem(_config(), states, NOW, balance_state=DUE)
    assert "CHARGE_PV" in _modes(problem)
    assert "balance_pessimistic_pv_unavailable" in quality["warnings"]


def _holding():
    return {
        "last_completed_at": (NOW - timedelta(days=8)).isoformat(),
        "hold_started_at": (NOW - timedelta(minutes=20)).isoformat(),
        "last_full_at": (NOW - timedelta(minutes=2)).isoformat(),
    }


def test_hold_in_progress_on_a_low_pessimistic_series_is_a_grid_window():
    problem, _, quality = build_problem(
        _config(), _states(low=0.1, soc="100"), NOW, balance_state=_holding()
    )
    assert quality["balance_phase"] == "holding"
    assert quality["balance_pv_series"] == "pessimistic"
    assert _modes(problem) == ["CHARGE_GRID"]


def test_hold_in_progress_on_a_sufficient_pessimistic_series_stays_pv():
    problem, _, _ = build_problem(
        _config(), _states(low=1.0, soc="100"), NOW, balance_state=_holding()
    )
    assert _modes(problem) == ["CHARGE_PV"]


def test_hold_in_progress_without_grid_charge_has_no_window_on_a_low_series():
    config = _config()
    config["settings"]["allow_grid_charge"] = False
    problem, _, _ = build_problem(
        config, _states(low=0.1, soc="100"), NOW, balance_state=_holding()
    )
    assert problem.balance_windows == ()


def test_plan_publishes_the_series_that_decided_the_window():
    plan = compute(_config(), _states(low=0.1), NOW, balance_state=DUE)
    assert plan["balance_pv_series"] == "pessimistic"
    assert plan["quality"]["balance_pv_series"] == "pessimistic"


def test_missing_pessimistic_entity_is_not_a_required_input():
    source, missing = available_forecasts(
        SourceConfig.from_dict(_config()["sources"]), _states(low=None)
    )
    assert source.pv.pessimistic == ()
    assert missing == []


# --- configuration -----------------------------------------------------------


def test_pessimistic_source_round_trips():
    data = _config()["sources"]
    config = SourceConfig.from_dict(data)
    assert config.pv.pessimistic[0].entity.entity_id == "sensor.pv_low"
    assert config.to_dict() == data
    plain = _config(pessimistic=False)["sources"]
    assert "pessimistic" not in plain["pv"]


def test_pessimistic_source_needs_enabled_pv():
    config = SourceConfig.from_dict(_config()["sources"])
    validate_sources(config, set())
    disabled = replace(config, pv=replace(config.pv, enabled=False, arrays=()))
    with pytest.raises(InputError):
        validate_sources(disabled, set())


def test_pessimistic_source_rejects_feedback_loops():
    config = SourceConfig.from_dict(_config()["sources"])
    with pytest.raises(InputError):
        validate_sources(config, {"sensor.pv_low"})


# --- source management -------------------------------------------------------


def test_inventory_edit_and_remove_the_pessimistic_source():
    config = _config()
    ref = SourceRef("pv_pessimistic", "interval", binding_index=0)
    assert ref in [item for item, _ in source_inventory(config, _Registry(), "en")]
    original = selected_source(config, ref)
    replacement = _binding("sensor.other").to_dict()
    edited = replace_source(config, ref, original, replacement)
    assert edited["sources"]["pv"]["pessimistic"] == [replacement]
    removed = remove_source(config, ref, original)
    assert "pessimistic" not in removed["sources"]["pv"]
    SourceConfig.from_dict(removed["sources"])


class _Registry:
    def async_get(self, _):
        return None
