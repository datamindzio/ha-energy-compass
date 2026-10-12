from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.config_models import (
    PvSource,
    SourceConfig,
    validate_sources,
)
from custom_components.energy_compass.engine.balance import (
    REACH_TOL,
    candidate_windows,
    fallback_costs,
)
from custom_components.energy_compass.engine.models import (
    Battery,
    InputError,
    Problem,
    SiteLimits,
    Slot,
)
from custom_components.energy_compass.engine.optimize import _TOL, solve
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


def _balance_problem(rows, windows, *, miss=100.0, **extra):
    return Problem(
        _slots(rows),
        SiteLimits(8, 25, 8, False),
        _battery(24.0),
        "value",
        0.0,
        (),
        "UTC",
        minimum_mode_minutes=15,
        balance_windows=windows,
        balance_threshold_kwh=0.99 * CAP,
        balance_miss_cost=miss,
        **extra,
    )


def _pinned(rows, windows):
    """Plan of each window pinned on its own; start 0 cannot be pinned at 24.0."""
    return [
        solve(_balance_problem(rows, (w,), balance_fixed=True))
        for w in windows
        if w.slots[0] > 0
    ]


def _assert_pinned_tie(rows, windows):
    pinned = _pinned(rows, windows)
    # The plan objective prices the median series, where the PV surplus pays for
    # every window: identical, so only the ranking cost can tell them apart.
    assert max(p.objective for p in pinned) - min(p.objective for p in pinned) < 1e-6


VETOED = [(0.9, 3.0, 1.0), (0.2, 3.0, 1.0), (0.2, 3.0, 1.0), (0.9, 3.0, 1.0)]
# The only cheap slot is start 0, which cannot be picked at 24.0 kWh.
VETOED_REVERSED = [(0.2, 3.0, 1.0), (0.9, 3.0, 1.0), (0.9, 3.0, 1.0), (0.9, 3.0, 1.0)]


def test_vetoed_grid_windows_carry_the_pessimistic_shortfall_cost():
    windows = _windows(_slots(VETOED), (0.2,) * 4, initial=24.0)
    # Coverage veto: (load 1.0 - pessimistic 0.2) kWh at the slot buy price, plus
    # the 0.75 kWh the pack still lacks to reach 24.75 kWh, at the same price.
    assert [round(w.fallback_cost, 6) for w in windows] == [1.395, 0.31, 0.31, 1.395]
    assert [w.fallback_cost for w in _windows(_slots(VETOED), None, initial=24.0)] == [
        0.0
    ] * 4


def test_reach_vetoed_grid_windows_carry_the_missing_pack_energy_cost():
    # Pessimistic PV equals load: it covers every window but lifts the pack by
    # nothing, so only the 0.75 kWh the pack lacks at the start is priced.
    windows = _windows(_slots(VETOED), (1.0,) * 4, initial=24.0)
    assert [w.mode for w in windows] == ["CHARGE_GRID"] * 4
    assert [round(w.fallback_cost, 6) for w in windows] == [0.675, 0.15, 0.15, 0.675]


def test_missing_pack_energy_is_priced_per_charge_efficiency():
    slots = _slots(VETOED)
    battery = replace(_battery(24.0), eta_charge=0.5)
    windows = candidate_windows(
        slots,
        phase="eligible",
        now=T0,
        hold_minutes=60,
        remaining_minutes=0,
        grid_charge_ok=(True,) * 4,
        pessimistic_pv_kwh=(1.0,) * 4,
        battery=battery,
        threshold_kwh=0.99 * CAP,
    )
    # 0.75 kWh in the pack takes 1.5 kWh from the grid at 50 % efficiency.
    assert [round(w.fallback_cost, 6) for w in windows] == [1.35, 0.3, 0.3, 1.35]


def test_pv_windows_and_median_only_candidates_carry_no_shortfall_cost():
    slots = _slots(MEDIAN)
    assert {w.fallback_cost for w in _windows(slots, (3.0,) * 4, initial=25.0)} == {0.0}
    assert {w.fallback_cost for w in _windows(slots, None)} == {0.0}


def test_pinned_windows_tie_on_the_median_series_but_not_on_the_ranking():
    windows = _windows(_slots(VETOED), (0.2,) * 4, initial=24.0)
    assert [p.balance_start for p in _pinned(VETOED, windows)] == [1, 2, 3]
    _assert_pinned_tie(VETOED, windows)
    assert fallback_costs(windows, 100.0) == pytest.approx((0.05425, 0.0, 0.0, 0.05425))


def test_pinned_reach_vetoed_windows_tie_on_the_median_series_but_not_on_the_ranking():
    windows = _windows(_slots(VETOED), (1.0,) * 4, initial=24.0)
    _assert_pinned_tie(VETOED, windows)
    assert fallback_costs(windows, 100.0) == pytest.approx((0.02625, 0.0, 0.0, 0.02625))


def test_solver_picks_cheap_grid_window_when_pessimistic_pv_is_insufficient():
    windows = _windows(_slots(VETOED), (0.2,) * 4, initial=24.0)
    assert all(w.mode == "CHARGE_GRID" for w in windows)
    plan = solve(_balance_problem(VETOED, windows))
    assert plan.balance_start in (1, 2)


def test_solver_picks_cheap_grid_window_when_only_the_pack_misses_the_threshold():
    # Pessimistic PV covers the load, so the veto is the threshold alone.
    windows = _windows(_slots(VETOED), (1.0,) * 4, initial=24.0)
    assert all(w.mode == "CHARGE_GRID" for w in windows)
    plan = solve(_balance_problem(VETOED, windows))
    assert plan.balance_start in (1, 2)


@pytest.mark.parametrize("cheap", [1, 2, 3])
@pytest.mark.parametrize("pessimistic", [0.2, 1.0])
def test_solver_picks_the_cheap_window_whichever_slot_is_cheap(cheap, pessimistic):
    rows = [(0.2 if i == cheap else 0.9, 3.0, 1.0) for i in range(4)]
    windows = _windows(_slots(rows), (pessimistic,) * 4, initial=24.0)
    assert all(w.mode == "CHARGE_GRID" for w in windows)
    assert solve(_balance_problem(rows, windows)).balance_start == cheap


def test_pessimistic_robust_pv_window_beats_vetoed_grid_windows():
    # Pessimistic PV 1.3 lifts the pack past 24.75 kWh by the last slot and
    # covers the load there: start 3 stays CHARGE_PV, starts 0-2 fall to the grid.
    windows = _windows(_slots(VETOED), (1.3,) * 4, initial=24.0)
    assert [w.mode for w in windows] == ["CHARGE_GRID"] * 3 + ["CHARGE_PV"]
    assert windows[3].fallback_cost == 0.0
    assert all(w.fallback_cost > 0 for w in windows[:3])
    # The grid window of start 1 or 2 is the cheapest of its kind, and the PV
    # window still wins although its slot is the expensive one.
    plan = solve(_balance_problem(VETOED, windows))
    assert plan.balance_start == 3


def test_ranking_is_not_part_of_the_published_objective():
    # Start 0 holds the cheapest slot but cannot be picked at 24.0 kWh, so every
    # window the solver can pick carries a positive ranking cost.
    windows = _windows(_slots(VETOED_REVERSED), (0.2,) * 4, initial=24.0)
    ranking = fallback_costs(windows, 100.0)
    plan = solve(_balance_problem(VETOED_REVERSED, windows))
    assert plan.balance_start in (1, 2, 3)
    assert ranking[plan.balance_start] > 0
    pinned = solve(
        _balance_problem(
            VETOED_REVERSED, (windows[plan.balance_start],), balance_fixed=True
        )
    )
    assert plan.objective == pytest.approx(pinned.objective)


def test_ranking_never_costs_more_than_a_share_of_the_miss_cost():
    windows = _windows(_slots(VETOED), (0.2,) * 4, initial=24.0)
    assert max(fallback_costs(windows, 100.0)) == pytest.approx(0.05425)
    # A cheap miss shrinks the weight so the dearest window costs half of it.
    assert max(fallback_costs(windows, 0.05)) == pytest.approx(0.025)
    assert fallback_costs(windows, 0.0) == (0.0,) * 4
    assert fallback_costs((), 1.0) == ()
    assert fallback_costs(_windows(_slots(MEDIAN), None), 1.0) == (0.0,) * 4


def test_ranking_does_not_make_the_solver_skip_the_balance():
    # Prices scaled so the uncapped ranking (5.4) dwarfs the default eligible miss
    # cost (0.5); the cheap window is start 0, which cannot be picked.
    rows = [(20.0, 3.0, 1.0)] + [(90.0, 3.0, 1.0)] * 3
    windows = _windows(_slots(rows), (0.2,) * 4, initial=24.0)
    assert max(fallback_costs(windows, 0.5)) <= 0.25 + 1e-9
    plan = solve(_balance_problem(rows, windows, miss=0.5))
    assert plan.balance_start in (1, 2, 3)


def test_unpickable_start_window_does_not_shift_the_ranking_towards_a_miss():
    # Start 0 is the cheap slot but cannot be picked at 24.0 of 25 kWh. Taken as
    # the floor it gave every pickable window a ranking of half the miss cost,
    # and the solver skipped the balance at a miss cost of 0.12.
    rows = [(20.0, 3.0, 1.0)] + [(90.0, 3.0, 1.0)] * 3
    windows = _windows(_slots(rows), (0.2,) * 4, initial=24.0)
    threshold = 0.99 * CAP
    ranking = fallback_costs(windows, 0.12, initial_kwh=24.0, threshold_kwh=threshold)
    assert ranking[0] == 0.0
    assert ranking[1:] == (0.0,) * 3
    assert max(fallback_costs(windows, 0.12)) > 0
    plan = solve(_balance_problem(rows, windows, miss=0.12))
    assert plan.balance_start in (1, 2, 3)


def test_unpickable_start_window_leaves_the_ranking_of_unequal_pickable_windows():
    # Start 0 (buy 20) cannot be picked at 24.0 of 25 kWh. Windows 1-3 differ in
    # cost (fallback 139.5 / 77.5 / 139.5), so the ranking is the spread over
    # the pickable ones: floor at start 2, dearest at half the miss cost.
    rows = [(20.0, 3.0, 1.0), (90.0, 3.0, 1.0), (50.0, 3.0, 1.0), (90.0, 3.0, 1.0)]
    windows = _windows(_slots(rows), (0.2,) * 4, initial=24.0)
    assert [w.fallback_cost for w in windows] == pytest.approx([31, 139.5, 77.5, 139.5])
    threshold = 0.99 * CAP
    ranking = fallback_costs(windows, 0.12, initial_kwh=24.0, threshold_kwh=threshold)
    assert ranking == pytest.approx((0.0, 0.06, 0.0, 0.06))
    # A dear miss leaves the weight at its 0.05 cap; the spread stays proportional.
    ranking = fallback_costs(windows, 100.0, initial_kwh=24.0, threshold_kwh=threshold)
    assert ranking == pytest.approx((0.0, 3.1, 0.0, 3.1))
    assert solve(_balance_problem(rows, windows, miss=0.12)).balance_start == 2


def test_reach_tolerance_is_the_one_the_solver_uses_for_start_zero():
    assert REACH_TOL == _TOL


@pytest.mark.parametrize(
    ("shortfall", "mode"),
    [
        (0.0, "CHARGE_PV"),
        (REACH_TOL / 2, "CHARGE_PV"),
        (REACH_TOL * 2, "CHARGE_GRID"),
    ],
)
def test_charge_pv_reach_tolerance_boundary(shortfall, mode):
    slots = _slots(MEDIAN)
    windows = _windows(slots, (3.0,) * 4, initial=0.99 * CAP - shortfall)
    assert windows[0].mode == mode


def test_start_zero_window_stays_in_the_floor_when_the_pack_is_at_the_threshold():
    windows = _windows(_slots(VETOED_REVERSED), (0.2,) * 4, initial=24.9)
    ranking = fallback_costs(windows, 100.0, initial_kwh=24.9, threshold_kwh=24.75)
    assert ranking[0] == 0.0
    assert min(ranking[1:]) > 0


def test_solver_takes_grid_windows_from_the_median_series_when_pv_is_short():
    # Median PV 0.5 < load 1.0: CHARGE_GRID without any pessimistic series.
    rows = [(0.9, 0.5, 1.0), (0.2, 0.5, 1.0), (0.2, 0.5, 1.0), (0.9, 0.5, 1.0)]
    windows = _windows(_slots(rows), None, initial=24.0)
    assert [w.mode for w in windows] == ["CHARGE_GRID"] * 4
    plan = solve(_balance_problem(rows, windows))
    assert plan.balance_start in (1, 2)


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
