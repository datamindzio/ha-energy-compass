"""The native executor's own contract: outcomes and persistence order.

Behaviour on the inverter is covered by the scenario tests (`test_deye_controller.py`)
and the parity tests; this file pins what only the executor promises.
"""

import deye_native_harness as native
from deye_oracle_v0137 import MODE, P

CHARGE_GRID = {
    "state": "CHARGE_GRID",
    "charge_kwh": 0.5,
    "pv_kwh": 0,
    "end_soc_kwh": 20,
}


class Recording(native.SimPort):
    def __init__(self, runner):
        super().__init__(runner)
        self.events = []

    async def persist(self, runtime=None, restore_pending=None, *, durable=False):
        self.events.append(("persist", durable, restore_pending))
        return await super().persist(runtime, restore_pending, durable=durable)

    async def write(self, command):
        self.events.append(("write", command.entity))
        await super().write(command)


def recorded_runner(h):
    runner = native.Runner(h)
    runner.executor.port = port = Recording(runner)
    return runner, port


def test_a_write_is_always_preceded_by_a_durable_save():
    h = native.Harness()
    h.data[P]["attributes"]["intervals"][0].update(CHARGE_GRID)
    runner, port = recorded_runner(h)
    runner.run()
    writes = [i for i, event in enumerate(port.events) if event[0] == "write"]
    assert writes
    for index in writes:
        assert port.events[index - 1][:2] == ("persist", True)


def test_the_restore_obligation_is_saved_durably_before_the_first_write():
    h = native.Harness()
    h.data[P]["attributes"]["intervals"][0].update(CHARGE_GRID)
    runner, port = recorded_runner(h)
    runner.run()
    first_write = next(i for i, e in enumerate(port.events) if e[0] == "write")
    obligations = [
        i for i, event in enumerate(port.events) if event == ("persist", True, True)
    ]
    assert obligations and obligations[0] < first_write


def test_outcome_codes_explain_why_a_run_ended():
    h = native.Harness()
    runner = native.Runner(h)
    h.data[MODE]["state"] = "Simulation"
    runner.run()
    h.data[MODE]["state"] = "Off"
    runner.run()
    h.data[MODE]["state"] = "Auto"
    runner.run()
    h.controller_missing = "absent"
    runner.run()
    assert runner.outcomes == [
        "simulation",
        "released",
        "run_finished",
        "controller_unavailable",
    ]
