"""The protective stop parks the battery limits at the buffer, never at 0 A (DD-1).

While the grid is out the stop never touches the battery limits at all: islanded, the
battery is the only source of the house, so no write may lower a current. The frozen
blueprint keeps the 0 A stop; only the native engine is held to this.
"""

from dataclasses import replace

import pytest
from deye_harness import CACHE
from deye_native_harness import Harness, Runner
from deye_oracle import P
from deye_oracle_v0137 import INPUTS

from custom_components.energy_compass import controller_profile as profile

CAPS = INPUTS | {"commissioned_battery_modes": ["Capacity", "Voltage"]}


def _running(state="CHARGE_PV"):
    """A site that ran `state`: the inverter holds its profile."""
    h = Harness(CAPS)
    h.data[P]["attributes"]["intervals"][0].update(
        state=state, charge_kwh=0.1, discharge_kwh=0, pv_kwh=0, load_kwh=0.05
    )
    runner = Runner(h)
    runner.run()
    return h, runner


def _hold(h):
    h.data[CACHE]["attributes"]["snapshot"]["intervals"][0].update(
        state="HOLD", charge_kwh=0, end_soc_kwh=20
    )


def _plan(h, *, outage=False, base=False):
    snapshot = replace(h.snapshot(), grid_outage=outage)
    target = profile.decide(snapshot, h.limits(), base=base)
    return snapshot, target, profile.diff(snapshot, target)


def _battery(h, commands):
    return [
        c
        for c in commands
        if c.entity in (h.ctx["charge_entity"], h.ctx["discharge_entity"])
    ]


def test_the_protective_stop_writes_the_buffer_not_zero():
    h, _ = _running()
    _hold(h)
    _, target, commands = _plan(h)
    stops = [c for c in _battery(h, commands) if c.phase == "disable"]
    assert {c.entity for c in stops} == {
        h.ctx["charge_entity"],
        h.ctx["discharge_entity"],
    }
    assert all(c.value == target.buffer_current == 1 for c in stops)
    assert not any(
        c.value == 0
        for c in commands
        if c.entity in (h.ctx["charge_entity"], h.ctx["discharge_entity"])
    )
    grid = [c for c in commands if c.entity == h.ctx["grid_entity"]]
    assert all(c.value == 0 for c in grid)


def test_the_stop_is_skipped_for_a_limit_already_at_the_buffer():
    h, _ = _running()
    _hold(h)
    h.data[h.ctx["discharge_entity"]]["state"] = "1"
    _, _, commands = _plan(h)
    stops = [c.entity for c in _battery(h, commands) if c.phase == "disable"]
    assert h.ctx["discharge_entity"] not in stops
    assert h.ctx["charge_entity"] in stops


def test_the_zero_opt_out_stops_at_zero_again():
    h, _ = _running()
    _hold(h)
    snapshot = h.snapshot()
    limits = replace(h.limits(), min_buffer_current=0, hold_grid_current=0)
    target = profile.decide(snapshot, limits)
    stops = [
        c
        for c in profile.diff(snapshot, target)
        if c.phase == "disable"
        and c.entity in (h.ctx["charge_entity"], h.ctx["discharge_entity"])
    ]
    assert stops and all(c.value == 0 for c in stops)


@pytest.mark.parametrize("base", [False, True])
def test_an_outage_never_lowers_the_battery_limits(base):
    h, _ = _running()
    _hold(h)
    snapshot, target, commands = _plan(h, outage=True, base=base)
    assert target.grid_outage
    for command in _battery(h, commands):
        assert command.phase != "disable"
        # A plan row is never decided while islanded (BACKUP, T5); BASE relinquishes up.
        if base:
            assert command.value >= float(snapshot.state(command.entity))


def test_an_outage_still_stops_the_grid_current_and_the_selects():
    h, _ = _running("CHARGE_GRID")
    h.data[h.ctx["grid_entity"]]["state"] = "4"
    _hold(h)
    _, _, commands = _plan(h, outage=True)
    stops = {c.entity: c.value for c in commands if c.phase == "disable"}
    assert stops[h.ctx["grid_entity"]] == 0
    assert h.ctx["charge_entity"] not in stops
    assert h.ctx["discharge_entity"] not in stops


def test_an_outage_run_never_writes_zero_to_the_battery():
    h, runner = _running()
    _hold(h)
    h.grid_outage = True
    runner.writes.clear()
    runner.run()
    for entity, value in runner.writes:
        if entity in (h.ctx["charge_entity"], h.ctx["discharge_entity"]):
            assert value >= 1


def test_enabling_waits_for_the_buffer_to_be_confirmed():
    h, _ = _running()
    _hold(h)
    snapshot = h.snapshot()
    target = profile.decide(snapshot, h.limits())
    command = next(c for c in profile.diff(snapshot, target) if c.phase == "enable")
    confirmed = {
        entity: value
        for entity, value in target.desired.items()
        if value == 0
        or value == "Disabled"
        or entity.startswith("number.inverter_deye_program")
    }
    charge = h.ctx["charge_entity"]
    assert target.desired[charge] == target.buffer_current

    def allowed(confirmed):
        runtime = {**snapshot.runtime, "confirmed": confirmed}
        return profile.guard(
            replace(snapshot, runtime=runtime),
            command,
            target=target,
            fresh=target,
            baseline=target,
            cleanup=False,
            failed=False,
        )

    assert not allowed(confirmed)
    assert not allowed({**confirmed, charge: 0})
    assert allowed({**confirmed, charge: target.buffer_current})
