"""Carried PV-follow commitment next to the legacy direction guard and flexible-load probes (T-038).

T-025 forbade HOLD on a chargeable PV surplus in a committed interval. Two
neighbours of that rule had no plan: a direction the legacy guard blocks was
read as open, and a flexible-load probe was held to the tightening without the
bridge reading. The pinned problems below were planned on 34d6e1f, the base of
T-025, and are planned again.
"""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.energy_compass.coordinator import EnergyCompassCoordinator
from custom_components.energy_compass.engine import dispatch_policy as policy
from custom_components.energy_compass.engine.models import (
    Battery,
    FlexibleLoadRequest,
    Problem,
    SiteLimits,
    Slot,
    SolveError,
)
from custom_components.energy_compass.engine.optimize import solve, solve_flexible_load

START = datetime(2026, 12, 18, 19, tzinfo=UTC)
Q = timedelta(minutes=15)


def committed(
    rows,
    battery,
    mode,
    minutes,
    since_min,
    legacy=None,
    legacy_min=0,
    power=0.1,
    start=START,
    **changes,
):
    slots = tuple(
        Slot(start + i * Q, start + (i + 1) * Q, buy, sell, pv, load)
        for i, (pv, load, buy, sell) in enumerate(rows)
    )
    return Problem(
        slots,
        SiteLimits(8, 8, 8, True),
        battery,
        changes.pop("terminal_mode", "value"),
        changes.pop("terminal_value_per_kwh", 0.0),
        changes.pop("remaining_daily_throughput_kwh", ()),
        "UTC",
        minimum_mode_minutes=minutes,
        minimum_mode_power_kw=power,
        initial_dispatch_mode=mode,
        initial_dispatch_mode_since=start - timedelta(minutes=since_min),
        initial_battery_mode=legacy,
        initial_battery_mode_since=(
            start - timedelta(minutes=legacy_min) if legacy else None
        ),
        **changes,
    )


def modes(plan):
    return [flow.dispatch_mode for flow in plan.flows]


# E: the guard blocks charging. SELF_CONSUME empties the pack in interval 0, interval 1
# has a surplus the pack could take but may not, so HOLD starts there on the depleted
# pack (T-025 review, round 2).
def repro_e():
    return committed(
        [
            (0, 0.1, 0.98, 0.75),
            (0.2, 0.025, 0.75, 0.52),
            (0.5, 0.05, 0.22, 0.25),
            (0.02, 0.03, 0.59, 0.57),
        ],
        Battery(5, 0, 1, 0.03, 0.5, 1, 0.95, 0.95, 0, True, True, 0.0),
        "CHARGE_PV",
        120,
        16,
        "discharge",
        15,
    )


# F: the guard blocks discharging. CHARGE_PV is forced on the surplus of intervals 0
# and 1, interval 2 has a net load but SELF_CONSUME cannot discharge.
def repro_f():
    problem = committed(
        [(0.1, 0.01, 0.46, 0.68), (0.5, 0.0, 0.95, 0.64), (0.0, 0.05, 1.05, 0.31)],
        Battery(5, 0, 1, 0.02, 0.5, 3, 0.95, 0.95, 0, False, True, 0.0),
        "SELF_CONSUME",
        60,
        6,
        "charge",
        5,
        power=0.2,
    )
    first = replace(
        problem.slots[0],
        start=START + timedelta(minutes=10),
        pv_kwh=0.03333,
        load_kwh=0.00333,
    )
    return replace(problem, slots=(first, *problem.slots[1:]))


def test_a_guard_that_blocks_charging_does_not_leave_charge_pv_open():
    problem = repro_e()
    first, second = problem.slots[:2]
    assert policy._guarded(problem, first) == "bc"
    assert not policy._takes_surplus(problem, second)
    free = replace(problem, initial_battery_mode=None, initial_battery_mode_since=None)
    assert policy._takes_surplus(free, second)
    plan = solve(problem)
    assert modes(plan) == ["SELF_CONSUME", "HOLD", "HOLD", "HOLD"]
    policy.validate_modes(problem, plan.flows, plan.carried_bridges)


def test_a_guard_that_blocks_discharging_does_not_leave_self_consume_open():
    problem = repro_f()
    last = problem.slots[2]
    assert policy._guarded(problem, last) == "bd"
    assert 2 in policy.idle_slots(problem)
    free = replace(problem, initial_battery_mode=None, initial_battery_mode_since=None)
    assert 2 not in policy.idle_slots(free)
    plan = solve(problem)
    assert modes(plan) == ["CHARGE_PV", "CHARGE_PV", "HOLD"]
    policy.validate_modes(problem, plan.flows, plan.carried_bridges)


def test_the_guard_ends_with_its_minimum_duration():
    problem = repro_e()
    later = problem.slots[-1]
    # The guard started 15 minutes before and holds for the 120-minute minimum duration.
    assert policy._guarded(problem, later) == "bc"
    over = replace(problem, initial_battery_mode_since=START - timedelta(minutes=121))
    assert policy._guarded(over, later) is None
    assert policy._takes_surplus(over, over.slots[1])


# The four problems of the T-025 review (sweep seed 79, flexible loads without a
# guard): planned on 34d6e1f, `infeasible` on b31d09d.
def probe_problem(rows, battery, terminal, value, mode, since, power, **changes):
    return committed(
        rows,
        battery,
        mode,
        120,
        since,
        power=power,
        start=datetime(2026, 12, 18, 11, tzinfo=UTC),
        terminal_mode=terminal,
        terminal_value_per_kwh=value,
        **changes,
    )


def quarter(rows):
    return [(pv, load, buy, sell) for buy, sell, pv, load in rows]


PROBE_44 = probe_problem(
    quarter(
        [
            (1.08, 0.5, 0.5, 0.005),
            (0.67, 0.18, 0.005, 0.01),
            (0.5, 0.7, 0.1, 0.026),
            (0.73, 0.09, 0.026, 0.025),
            (1.17, 0.66, 0.1, 0.5),
            (1.41, 0.31, 0.03, 0.1),
        ]
    ),
    Battery(5, 0, 1, 0.03, 0.5, 3, 0.95, 0.95, 0, True, True, 0),
    "preserve_initial",
    0.3,
    "CHARGE_PV",
    1,
    0.2,
)
PROBE_62 = probe_problem(
    quarter(
        [
            (0.99, 0.42, 0.005, 0.024),
            (0.84, 0.53, 0.2, 0.01),
            (1.16, 0.58, 0.005, 0.03),
            (1.21, 0.63, 0.1, 0.0),
            (0.86, 0.37, 0.0, 0.03),
            (1.21, 0.8, 0.01, 0.0),
            (1.46, 0.65, 0.02, 0.03),
            (0.74, 0.8, 0.2, 0.01),
            (0.6, 0.27, 0.0, 0.03),
            (0.92, 0.64, 0.5, 0.1),
            (0.78, 0.19, 0.025, 0.024),
            (1.27, 0.78, 0.05, 0.1),
        ]
    ),
    Battery(5, 0, 1, 0.1, 0.5, 0.5, 0.95, 0.95, 0, False, False, 0),
    "value",
    0,
    "CHARGE_PV",
    16,
    0.2,
)
PROBE_917 = probe_problem(
    quarter(
        [
            (1.42, 0.16, 0.025, 0.0),
            (1.3, 0.57, 0.5, 0.024),
            (0.72, 0.11, 0.02, 0.005),
            (1.42, 0.15, 0.026, 0.026),
            (0.48, 0.13, 0.0, 0.01),
            (0.4, 0.68, 0.2, 0.02),
        ]
    ),
    Battery(5, 0, 1, 0.02, 0.08, 1, 0.95, 0.95, 0.01, False, True, 0.05),
    "value",
    0.3,
    "SELF_CONSUME",
    16,
    0.05,
    remaining_daily_throughput_kwh=(("2026-12-18", 0.02),),
)
PROBE_1424 = probe_problem(
    quarter(
        [
            (0.47, 0.69, 0.05, 0.005),
            (1.37, 0.3, 0.03, 0.0),
            (1.16, 0.42, 0.0, 0.1),
            (1.38, 0.21, 0.026, 0.005),
            (1.3, 0.51, 0.01, 0.025),
            (1.02, 0.54, 0.024, 0.0),
            (1.0, 0.69, 0.005, 0.01),
            (1.33, 0.44, 0.024, 0.05),
        ]
    ),
    Battery(5, 0.1, 1, 0.53, 1, 1, 0.95, 0.95, 0.01, True, False, 0),
    "value",
    0,
    "SELF_CONSUME",
    0,
    0.1,
)


@pytest.mark.parametrize(
    ("problem", "request_", "first_modes"),
    [
        (PROBE_62, FlexibleLoadRequest(0.1, 0.5), ["SELF_CONSUME", "CHARGE_PV"]),
        (PROBE_917, FlexibleLoadRequest(0.3, 0.5), ["HOLD", "HOLD"]),
        (PROBE_1424, FlexibleLoadRequest(1.0, 0.5), ["SELF_CONSUME", "HOLD"]),
    ],
    ids=("62", "917", "1424"),
)
def test_a_probe_that_ends_the_idle_interval_is_planned(problem, request_, first_modes):
    result = solve_flexible_load(problem, request_)
    assert modes(result.plan)[:2] == first_modes
    policy.validate_modes(
        replace(
            problem,
            slots=tuple(
                replace(slot, load_kwh=slot.load_kwh + extra)
                for slot, extra in zip(problem.slots, result.schedule_kwh, strict=True)
            ),
        ),
        result.plan.flows,
        result.plan.carried_bridges,
    )


def test_a_bridge_row_stays_idle_under_the_realised_probe():
    problem = PROBE_62
    result = solve_flexible_load(problem, FlexibleLoadRequest(0.1, 0.5))
    idle = policy.idle_slots(problem)
    for index in result.plan.carried_bridges:
        slot = problem.slots[index]
        realised = replace(
            problem,
            slots=tuple(
                replace(s, load_kwh=s.load_kwh + e)
                for s, e in zip(problem.slots, result.schedule_kwh, strict=True)
            ),
        )
        assert index in idle
        assert index in policy.idle_slots(realised)
        assert result.schedule_kwh[index] <= policy.probe_room(problem, slot)


def test_the_pinned_problems_without_a_probe_keep_their_status():
    # 44 and 917 have no plan without a probe either, on the base of T-025 as well;
    # 62 and 1424 are planned.
    for problem, planned in (
        (PROBE_44, False),
        (PROBE_62, True),
        (PROBE_917, False),
        (PROBE_1424, True),
    ):
        if planned:
            solve(problem)
        else:
            with pytest.raises(SolveError) as error:
                solve(problem)
            assert error.value.reason == "infeasible"


def test_a_probe_cannot_make_a_plan_the_problem_itself_does_not_have():
    # Base 34d6e1f planned this probe by HOLD on a chargeable surplus, which the
    # problem without the probe cannot: its pack must end at its initial energy while
    # the commitment carries SELF_CONSUME through a net load of 0.4 kWh. It stays
    # infeasible until the commitment ends (docs/model.md).
    with pytest.raises(SolveError) as error:
        solve_flexible_load(PROBE_44, FlexibleLoadRequest(0.1, 0.5))
    assert error.value.reason == "infeasible"


def test_a_probe_that_takes_a_surplus_below_the_activity_lets_hold_start():
    # Interval 1 has a surplus of 0.03 kWh, above the 0.025 kWh activity; a probe of
    # more than 0.005 kWh leaves it nothing to charge, so the depleted pack may rest.
    rows = [(0, 0.2, 1.0, 0.5), (0.03, 0.0, 1.0, 0.5), (0, 0.0, 1.0, 0.5)]
    problem = committed(
        rows,
        Battery(5, 0, 1, 0.05, 1, 1, 0.95, 0.95, 0, True, True, 0.0),
        "SELF_CONSUME",
        60,
        0,
    )
    assert policy._takes_surplus(problem, problem.slots[1])
    # The pack is too small to take the probe's energy anywhere else.
    result = solve_flexible_load(problem, FlexibleLoadRequest(0.375, 0.5))
    assert result.schedule_kwh[1] > 0.005
    assert modes(result.plan)[1] == "HOLD"


def test_validate_modes_without_bridges_accepts_either_reading():
    # SELF_CONSUME empties the pack in interval 0, HOLD starts on it in interval 1 and
    # goes on through the idle interval 2: a HOLD run, not a bridge.
    problem = committed(
        [(0, 0.1, 1.0, 0.5), (0, 0.1, 1.0, 0.5), (0, 0, 1.0, 0.5)],
        Battery(5, 0, 1, 0.05, 1, 1, 0.95, 0.95, 0, True, True, 0.0),
        "SELF_CONSUME",
        60,
        0,
    )
    plan = solve(problem)
    assert modes(plan) == ["SELF_CONSUME", "HOLD", "HOLD"]
    assert plan.carried_bridges == ()
    assert policy.idle_slots(problem) == {2}
    policy.validate_modes(problem, plan.flows, plan.carried_bridges)
    policy.validate_modes(problem, plan.flows)
    # Read as a bridge, the row is past the end of the carried commitment.
    with pytest.raises(SolveError, match="minimum mode duration"):
        policy.validate_modes(problem, plan.flows, {2})


def test_a_publish_derives_the_bridge_clock_from_the_plan():
    start = datetime(2026, 12, 18, 19, tzinfo=UTC)
    now = start + timedelta(minutes=35)

    def row(index, mode):
        return {
            "start": (start + index * Q).isoformat(),
            "end": (start + (index + 1) * Q).isoformat(),
            "dispatch_mode": mode,
            "discharge_kwh": 0.0,
            "grid_export_kwh": 0.0,
        }

    rows = [row(0, "SELF_CONSUME"), row(1, "HOLD"), row(2, "HOLD"), row(3, "HOLD")]
    result = {
        "intervals": rows,
        "outlook": [],
        "valid_until": (start + 4 * Q).isoformat(),
        "generated_at": start.isoformat(),
        "plan_retained": True,
        "dispatch_policy": {
            "enabled": True,
            "carried_idle_starts": [rows[1]["start"], rows[2]["start"]],
        },
    }
    committed_rows = []
    coordinator = SimpleNamespace(
        _inputs_valid_until=None,
        configuration={},
        last_successful_plan_at=None,
        _commit_battery_direction=lambda res, carried_idle=False: committed_rows.append(
            (res["intervals"][0]["start"], res["generated_at"], carried_idle)
        ),
        _commit_current_export=MagicMock(),
        _commit_current_grid_charge=MagicMock(),
        _record_pv_forecast=MagicMock(),
        _anchor_windows=MagicMock(),
        async_set_updated_data=MagicMock(),
    )
    # Interval 2 is a bridge: the HOLD run it neighbours began at interval 3 at the
    # earliest, so the clock starts after the bridge rows (`mode_run_start`).
    EnergyCompassCoordinator._publish_current(coordinator, result, None, {}, now)
    first = (start + 2 * Q).isoformat()
    assert committed_rows == [(first, first, True)] or committed_rows == [
        (rows[2]["start"], rows[2]["start"], True)
    ]
    # Interval 3 is a HOLD run, not a bridge: it is not carried and starts its own clock.
    committed_rows.clear()
    EnergyCompassCoordinator._publish_current(
        coordinator, result, None, {}, start + timedelta(minutes=50)
    )
    assert committed_rows[0][2] is False
    assert committed_rows[0][1] == rows[3]["start"]


def test_the_guard_and_probe_corpus_has_no_rejected_plan():
    # The solver certifies every plan with `validate_modes`: a `solver_failure` is the
    # model and the validator disagreeing.
    rng = random.Random(38)
    for index in range(60):
        rows = [
            (
                rng.choice((0, 0.005, 0.02, 0.026, 0.1, 0.5)),
                rng.choice((0, 0.005, 0.024, 0.03, 0.1, 0.5)),
                round(rng.uniform(0.2, 1.5), 2),
                round(rng.uniform(0, 0.8), 2),
            )
            for _ in range(rng.randint(2, 10))
        ]
        battery = Battery(
            5,
            0,
            1,
            rng.choice((0.02, 0.1, 1.0, 4.9)),
            rng.choice((0.5, 1, 3)),
            rng.choice((0.5, 1, 3)),
            0.95,
            0.95,
            0,
            rng.random() < 0.5,
            rng.random() < 0.5,
            0.0,
        )
        problem = committed(
            rows,
            battery,
            rng.choice(("SELF_CONSUME", "CHARGE_PV")),
            rng.choice((60, 120)),
            rng.choice((0, 16)),
            rng.choice((None, "charge", "discharge")),
            rng.choice((0, 15)),
        )
        for attempt in (
            lambda problem=problem: solve(problem),
            lambda problem=problem, rng=rng: solve_flexible_load(
                problem,
                FlexibleLoadRequest(rng.choice((0.1, 0.3)), rng.choice((0.5, 2.0))),
            ),
        ):
            try:
                attempt()
            except SolveError as error:
                assert error.reason == "infeasible", (index, str(error))


def test_a_throughput_budget_that_cannot_pay_the_commitment_has_no_plan():
    # Interval 0 spends 0.06 kWh of the 0.1 kWh daily budget on SELF_CONSUME; interval 1
    # has a surplus the pack could take, so the commitment asks for CHARGE_PV, which the
    # rest of the budget cannot pay. Base 34d6e1f saved it by HOLD on that surplus. With
    # the commitment over, or with no budget, there is a plan (docs/model.md).
    rows = [(0.008, 0.06667, 1.27, 0.13), (0.5, 0.025, 0.2, 0.18)]
    battery = Battery(5, 0, 1, 0.1, 1, 1, 0.95, 0.95, 0.01, True, True, 0.0)
    problem = committed(
        rows,
        battery,
        "SELF_CONSUME",
        120,
        16,
        remaining_daily_throughput_kwh=(("2026-12-18", 0.1),),
    )
    with pytest.raises(SolveError) as error:
        solve(problem)
    assert error.value.reason == "infeasible"
    solve(replace(problem, remaining_daily_throughput_kwh=()))
    solve(replace(problem, initial_dispatch_mode_since=START - timedelta(minutes=300)))
