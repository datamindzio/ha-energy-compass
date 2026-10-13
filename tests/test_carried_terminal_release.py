"""A carried PV-follow commitment is released where the terminal rule cannot be met (T-065).

`preserve_initial` asks for the pack to end where it began. A carried CHARGE_PV
or SELF_CONSUME forbids HOLD while the pack can follow, and SELF_CONSUME draws the
house's net load from it: with no PV surplus later to put it back, the problem had
no plan, though the boundary before had planned the same slot from a longer horizon
(`benchmarks/curtail_sweep.py` deproll seed 11 draw 293, T-058). The lock floor now
counts the terminal requirement among what a slot owes (`dispatch_policy._owed`), so
the interval may be a HOLD released from the pack, as for a standby loss (ADR-0004).
"""

from datetime import UTC, datetime

import pytest
from test_carried_commitment_guard_probes import committed, modes

from custom_components.energy_compass.engine import dispatch_policy as policy
from custom_components.energy_compass.engine.models import Battery
from custom_components.energy_compass.engine.optimize import solve

START = datetime(2026, 12, 18, 12, 30, tzinfo=UTC)
# The T-065 pack: 10 kWh, reserve 1 kWh, charge 1 kW, no grid charge, 0.323 kWh above reserve.
PACK = Battery(10, 0.1, 1, 1.323, 1, 4, 0.95, 0.95, 0, False, False, 0)
# 0.3 kWh of PV against a 0.4 kWh load, then 0.1 kWh of surplus: the surplus puts back
# 0.095 kWh, the house draws 0.105 kWh from the pack.
ROWS = [(0.3, 0.4, 0.8, 0.2), (0.3, 0.2, 0.8, 0.2)]


def problem(rows=ROWS, battery=PACK, mode="CHARGE_PV", **changes):
    return committed(
        rows,
        battery,
        mode,
        120,
        90,
        start=START,
        terminal_mode=changes.pop("terminal_mode", "preserve_initial"),
        **changes,
    )


@pytest.mark.parametrize("mode", ["CHARGE_PV", "SELF_CONSUME"])
@pytest.mark.parametrize("slots", [1, 2])
def test_a_terminal_rule_the_house_would_break_releases_the_commitment(mode, slots):
    plan = solve(problem(ROWS[:slots], mode=mode))
    assert modes(plan)[0] == "HOLD"
    assert plan.flows[0].discharge_kwh == pytest.approx(0)
    assert plan.flows[-1].end_soc_kwh >= 1.323 - 1e-6


def test_the_release_is_a_valid_plan_for_the_validator():
    source = problem()
    plan = solve(source)
    policy.validate_modes(source, plan.flows, plan.carried_bridges)


def test_a_later_surplus_that_refills_the_pack_keeps_the_commitment():
    rows = [ROWS[0], (1.0, 0.2, 0.8, 0.2), (1.0, 0.2, 0.8, 0.2)]
    plan = solve(problem(rows))
    assert modes(plan)[0] == "SELF_CONSUME"
    assert plan.flows[-1].end_soc_kwh >= 1.323 - 1e-6


def test_a_terminal_value_asks_nothing_of_the_pack():
    plan = solve(problem(terminal_mode="value", terminal_value_per_kwh=0.0))
    assert modes(plan)[0] == "SELF_CONSUME"
