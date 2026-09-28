"""Atlas `Solve` payload builder (ADR-0019 §6): uniform/non-uniform slots, drop, enums."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.energy_compass.atlas.solve_builder import build_solve_payload
from custom_components.energy_compass.engine.models import (
    Battery,
    Flow,
    Opportunity,
    Plan,
    Problem,
    SiteLimits,
    Slot,
)

START = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
VALUES = {
    "boost_ceiling": 0.01,
    "limit_floor": 0.8,
    "cheap_percentile": 25,
    "limit_percentile": 75,
    "minimum_mode_minutes": 60,
    "minimum_mode_power_kw": 0.1,
}
CONFIG = {}


def _slot(start, hours, buy=0.2, sell=0.1, pv=1.0, load=0.5):
    return Slot(start, start + timedelta(hours=hours), buy, sell, pv, load)


def _flow(dispatch_mode="SELF_CONSUME", end_soc_kwh=5.0):
    return Flow(0.1, 0.0, 0.0, 0.2, 0.0, end_soc_kwh, dispatch_mode=dispatch_mode)


def _battery(initial_kwh=4.0):
    return Battery(10.0, 0.1, 1.0, initial_kwh, 3.0, 3.0, 0.95, 0.95, 0.01, True, True)


def _problem(slots, battery=None):
    return Problem(
        slots,
        SiteLimits(5.0, 5.0, 5.0, False),
        battery,
        "preserve_initial",
        0.0,
        (),
        "UTC",
    )


def _analysis(level="CHEAP", start=START, hours=1):
    return SimpleNamespace(
        opportunities=(Opportunity(start, start + timedelta(hours=hours), 0.1, level),)
    )


def test_uniform_slots_produce_a_valid_payload():
    slots = tuple(_slot(START + timedelta(hours=i), 1) for i in range(3))
    flows = tuple(_flow() for _ in slots)
    problem = _problem(slots, _battery())
    plan = Plan(flows, 1.0, 1.0, 0.0, 0.0)
    payload = build_solve_payload(
        problem, plan, _analysis(hours=3), VALUES, CONFIG, START
    )
    assert payload is not None
    assert payload["horizon_start"] == slots[0].start.isoformat()
    assert payload["step_s"] == 3600
    assert payload["state"] == "SELF_CONSUME"
    assert payload["consumption_level"] == "CHEAP"
    assert payload["plan"]["g_in"] == [0.1, 0.1, 0.1]
    assert payload["inputs"]["soc0_kwh"] == 4.0
    assert payload["inputs"]["params"]["strategy"] == "cost_min"


def test_leading_short_slot_is_dropped():
    short = _slot(START, 0.25)  # 15 minutes
    rest = [_slot(START + timedelta(hours=i, minutes=15), 1) for i in range(2)]
    slots = (short, *rest)
    flows = (_flow(end_soc_kwh=3.0), _flow(end_soc_kwh=5.0), _flow(end_soc_kwh=6.0))
    problem = _problem(slots, _battery(initial_kwh=1.0))
    plan = Plan(flows, 1.0, 1.0, 0.0, 0.0)
    payload = build_solve_payload(
        problem, plan, _analysis(start=rest[0].start, hours=2), VALUES, CONFIG, START
    )
    assert payload is not None
    assert payload["horizon_start"] == rest[0].start.isoformat()
    assert payload["step_s"] == 3600
    assert len(payload["plan"]["g_in"]) == 2
    # soc0_kwh at horizon_start is the dropped slot's own end SOC, not the problem's.
    assert payload["inputs"]["soc0_kwh"] == 3.0


def test_non_uniform_slots_after_drop_are_not_sent():
    slots = (
        _slot(START, 0.25),
        _slot(START + timedelta(minutes=15), 1),
        _slot(START + timedelta(hours=1, minutes=15), 2),
    )
    flows = tuple(_flow() for _ in slots)
    problem = _problem(slots, _battery())
    plan = Plan(flows, 1.0, 1.0, 0.0, 0.0)
    payload = build_solve_payload(
        problem, plan, _analysis(hours=3), VALUES, CONFIG, START
    )
    assert payload is None


def test_null_dispatch_mode_is_not_sent():
    slots = (_slot(START, 1),)
    flows = (_flow(dispatch_mode=None),)
    problem = _problem(slots, _battery())
    plan = Plan(flows, 1.0, 1.0, 0.0, 0.0)
    payload = build_solve_payload(problem, plan, _analysis(), VALUES, CONFIG, START)
    assert payload is None


def test_no_covering_opportunity_is_not_sent():
    slots = (_slot(START, 1),)
    flows = (_flow(),)
    problem = _problem(slots, _battery())
    plan = Plan(flows, 1.0, 1.0, 0.0, 0.0)
    far_away = _analysis(start=START + timedelta(days=1), hours=1)
    payload = build_solve_payload(problem, plan, far_away, VALUES, CONFIG, START)
    assert payload is None


def test_normal_level_maps_from_none():
    slots = (_slot(START, 1),)
    flows = (_flow(),)
    problem = _problem(slots, _battery())
    plan = Plan(flows, 1.0, 1.0, 0.0, 0.0)
    payload = build_solve_payload(
        problem, plan, _analysis(level=None), VALUES, CONFIG, START
    )
    assert payload["consumption_level"] == "NORMAL"


def test_no_battery_uses_zero_soc0():
    slots = (_slot(START, 1),)
    flows = (Flow(0.5, 0.0, 0.0, 0.0, 0.0, 0.0, dispatch_mode="HOLD"),)
    problem = _problem(slots, battery=None)
    plan = Plan(flows, 1.0, 1.0, 0.0, 0.0)
    payload = build_solve_payload(problem, plan, _analysis(), VALUES, CONFIG, START)
    assert payload["inputs"]["soc0_kwh"] == 0.0
    assert "eta_charge" not in payload["inputs"]["params"]
