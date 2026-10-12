"""The native executor's own contract: outcomes and persistence order.

Behaviour on the inverter is covered by the scenario tests (`test_deye_controller.py`)
and the parity tests; this file pins what only the executor promises.
"""

import asyncio
import copy
import json

import deye_native_harness as native
import pytest
from deye_oracle_v0137 import MODE, RT, P
from homeassistant.exceptions import ServiceValidationError

from custom_components.energy_compass import controller_run as run_module
from custom_components.energy_compass import controller_texts as texts

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


class Failing(native.SimPort):
    """Raises from the service calls the way a failing Home Assistant call does."""

    def __init__(self, runner, *, write=None, read=None):
        super().__init__(runner)
        self.fail_write, self.fail_read = write, read
        self.calls = {"write": 0, "read": 0}

    async def write(self, command):
        self.calls["write"] += 1
        if self.fail_write:
            raise self.fail_write
        await super().write(command)

    async def read_registers(self, address, count):
        self.calls["read"] += 1
        if self.fail_read:
            raise self.fail_read
        return await super().read_registers(address, count)


def failing_runner(**failures):
    h = native.Harness()
    h.data[P]["attributes"]["intervals"][0].update(CHARGE_GRID)
    runner = native.Runner(h)
    runner.executor.port = port = Failing(runner, **failures)
    return h, runner, port


@pytest.mark.parametrize(
    "failures",
    [
        {"write": RuntimeError("service failed")},
        {"read": RuntimeError("no answer")},
        {"write": ServiceValidationError("x"), "read": TimeoutError()},
    ],
)
def test_failing_service_calls_are_unconfirmed_and_the_transactions_carry_on(failures):
    h, runner, port = failing_runner(**failures)
    runner.run()
    assert runner.outcomes == ["write_failed"]
    runtime = h.stored_runtime()
    assert runtime["code"] == "write_failed"
    assert runtime["confirmed_mode"] == "unconfirmed"
    assert h.restore_pending is True
    # Three transactions, each command tried three times: nothing gave up early.
    assert port.calls["write"] >= 3 * 3
    assert port.calls["read"] >= 3 * 3


def test_a_failed_read_leaves_no_observation_and_a_failed_write_still_reads():
    h, runner, port = failing_runner(read=RuntimeError("no answer"))
    runner.run()
    assert "confirmed" not in h.stored_runtime() or not any(
        h.stored_runtime()["confirmed"]
    )
    h, runner, port = failing_runner(write=RuntimeError("service failed"))
    runner.run()
    assert port.calls["read"] == port.calls["write"] > 0


@pytest.mark.parametrize("where", ["write", "read"])
def test_cancellation_is_never_taken_for_a_failed_call(where):
    h, runner, port = failing_runner(**{where: asyncio.CancelledError()})
    with pytest.raises(asyncio.CancelledError):
        runner.run()
    assert port.calls["write"] == 1
    assert h.stored_runtime()["uncertain"]
    assert h.restore_pending is True


def test_the_profile_reads_the_runtime_the_executor_stored(monkeypatch):
    h = native.Harness()
    h.data[P]["attributes"]["intervals"][0].update(CHARGE_GRID)
    runner = native.Runner(h)
    store = h.data[RT]["attributes"]
    h.stored_runtime = dict  # a port whose snapshot never sees the store
    seen = []

    def spy(function):
        def wrapped(snapshot, *args, **kwargs):
            seen.append((snapshot.runtime, copy.deepcopy(store.get("runtime", {}))))
            return function(snapshot, *args, **kwargs)

        return wrapped

    for name in ("diff", "guard", "cap_only_replan"):
        monkeypatch.setattr(
            run_module.profile, name, spy(getattr(run_module.profile, name))
        )
    runner.run()
    assert seen and any(actual for _, actual in seen)
    assert all(used == actual for used, actual in seen)


def test_a_malformed_row_is_an_invalid_plan_and_nothing_is_forced():
    h = native.Harness()
    h.data[P]["attributes"]["intervals"][0].update(CHARGE_GRID)
    # Plan acceptance rejects such rows; this is the second line of defence.
    h.accept()["intervals"][0]["charge_kwh"] = "n/a"
    decision = h.decision()
    assert decision["valid"] is False
    assert decision["state"] == "BASE"
    assert "przedział" in decision["reason"]
    runner = native.Runner(h)
    runner.run()
    assert not [value for _, value in runner.writes if value in ("Grid", "Sell")]
    assert h.stored_runtime()["state"] == "BASE"


def malformed(state, key, value):
    h = native.Harness()
    h.data[P]["attributes"]["intervals"][0].update(
        {"state": state, "charge_kwh": 0.5, "discharge_kwh": 0.5, "end_soc_kwh": 20}
    )
    row = h.accept()["intervals"][0]
    if value == "missing":
        row.pop(key, None)
    else:
        row[key] = value
    return h.decision()


@pytest.mark.parametrize(
    ("state", "key", "value"),
    [
        ("CHARGE_GRID", "charge_kwh", "missing"),
        ("CHARGE_GRID", "pv_kwh", "x"),
        ("CHARGE_GRID", "load_kwh", None),
        ("CHARGE_GRID", "end_soc_kwh", "missing"),
        ("CHARGE_GRID", "start", "missing"),
        ("DISCHARGE_GRID", "discharge_kwh", None),
        ("DISCHARGE_GRID", "start", "yesterday"),
        ("HOLD", "end_soc_kwh", "missing"),
        ("CURTAIL", "end_soc_kwh", "n/a"),
    ],
)
def test_rows_the_blueprint_cannot_process_are_invalid(state, key, value):
    decision = malformed(state, key, value)
    assert decision["valid"] is False
    assert decision["state"] == "BASE"
    assert decision["reason"] == texts.PL["plan_malformed"]


@pytest.mark.parametrize(
    ("state", "key", "value"),
    [
        ("CHARGE_GRID", "discharge_kwh", "missing"),
        ("DISCHARGE_GRID", "charge_kwh", "x"),
        ("CHARGE_PV", "end_soc_kwh", "missing"),
        ("SELF_CONSUME", "charge_kwh", None),
    ],
)
def test_fields_the_blueprint_never_reads_do_not_invalidate_a_row(state, key, value):
    assert malformed(state, key, value)["valid"] is True


def pause_runner():
    h = native.Harness()
    h.data[P]["attributes"]["intervals"][0].update(CHARGE_GRID)
    return h, native.Runner(h)


def test_paused_computes_and_publishes_but_writes_nothing():
    h, runner = pause_runner()
    h.paused = True
    runner.run()
    assert runner.outcomes == ["paused"]
    assert runner.writes == [] and runner.reads == []
    runtime = h.stored_runtime()
    assert runtime["code"] == "paused"
    assert runtime["reason_code"] == "paused"
    assert runtime["state"] == "CHARGE_GRID"
    assert h.restore_pending is False


def test_pausing_leaves_the_restore_obligation_and_uncertain_registers_alone():
    h, runner = pause_runner()
    h.data[RT]["attributes"]["runtime"] = {
        "uncertain": ["number.x"],
        "owned_session": 1,
    }
    h.restore_pending = True
    h.paused = True
    runner.run()
    assert h.restore_pending is True
    assert h.stored_runtime()["uncertain"] == ["number.x"]
    assert runner.writes == []


def test_a_run_in_flight_stops_before_its_next_write_and_resumes_later():
    h, runner = pause_runner()

    def pause_after_two(r, entity, value):
        if len(r.writes) == 2:
            h.paused = True

    runner.hook = pause_after_two
    runner.run()
    assert len(runner.writes) == 2
    assert runner.outcomes == ["paused"]
    runtime = h.stored_runtime()
    assert runtime["code"] == "paused"
    assert h.restore_pending is True
    h.paused = False
    runner.hook = None
    runner.run()
    assert runner.outcomes[-1] == "run_finished"
    assert h.stored_runtime()["confirmed_mode"] == "CHARGE_GRID"


def test_the_history_records_what_a_run_decided_sent_and_how_it_ended():
    _, runner = pause_runner()
    runner.run()
    (record,) = runner.executor.history
    assert record["outcome"] == "run_finished"
    assert record["code"] == "ok"
    assert record["decision"]["state"] == "CHARGE_GRID"
    assert record["decision"]["reason_code"] == "ok"
    assert record["transactions"][0]["commands"]
    sent = [(w["entity"], w["value"]) for w in record["writes"]]
    assert sent == runner.writes
    assert all(w["confirmed"] and not w["write_error"] for w in record["writes"])
    assert record["seconds"] >= 0
    json.dumps(record)


def test_failed_and_blocked_writes_are_visible_in_the_history():
    h, runner, _ = failing_runner(write=RuntimeError("service failed"))
    runner.run()
    (record,) = runner.executor.history
    assert record["outcome"] == "write_failed"
    sent = [w for w in record["writes"] if "attempt" in w]
    assert sent
    assert all(w["write_error"] and not w["confirmed"] for w in sent)
    h, runner = pause_runner()

    def pause_after_one(r, entity, value):
        h.paused = True

    runner.hook = pause_after_one
    runner.run()
    (record,) = runner.executor.history
    assert record["outcome"] == "paused"
    assert any(w.get("blocked") for w in record["writes"])


def test_the_history_keeps_the_last_fifty_runs_only():
    _, runner = pause_runner()
    for number in range(55):
        runner.executor.history.append({"n": number})
    runner.run()
    assert len(runner.executor.history) == 50
    assert runner.executor.history[0] == {"n": 6}
    assert runner.executor.history[-1]["outcome"] == "run_finished"


def test_a_cancelled_run_is_recorded_as_cancelled():
    _, runner, _ = failing_runner(write=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        runner.run()
    assert runner.executor.history[-1]["outcome"] == "cancelled"


def test_the_record_stays_small_for_a_state_attribute():
    _, runner = pause_runner()
    runner.executor.history.clear()
    runner.run()
    assert len(json.dumps(runner.executor.history[-1])) < 16000


def test_off_without_a_restore_obligation_is_released_not_paused():
    h = native.Harness()
    runner = native.Runner(h)
    h.paused = True
    h.data[MODE]["state"] = "Off"
    runner.run()
    assert runner.outcomes == ["released"]
    assert h.stored_runtime()["code"] != "paused"


def test_off_with_a_restore_obligation_stays_paused_and_writes_nothing():
    h = native.Harness()
    h.data[P]["attributes"]["intervals"][0].update(CHARGE_GRID)
    runner = native.Runner(h)
    runner.run()
    assert h.restore_pending
    written = len(runner.writes)
    h.paused = True
    h.data[MODE]["state"] = "Off"
    runner.run()
    assert runner.outcomes[-1] == "paused"
    assert len(runner.writes) == written
    assert h.restore_pending
    assert h.stored_runtime()["reason_code"] == "paused"


def test_a_running_blueprint_automation_blocks_every_write_with_its_own_reason():
    h = native.Harness()
    h.data[P]["attributes"]["intervals"][0].update(CHARGE_GRID)
    h.blueprint_active = ("automation.old_deye",)
    runner = native.Runner(h)
    runner.run()
    assert runner.outcomes == ["takeover_blocked"]
    assert runner.writes == []
    runtime = h.stored_runtime()
    assert runtime["code"] == "blocked"
    assert runtime["reason_code"] == "blueprint_active"
    assert runtime["takeover_blocked"] is True
    assert "automation.old_deye" in runtime["reason"]
    assert not h.restore_pending


def test_a_blueprint_automation_starting_mid_run_stops_the_next_write():
    h = native.Harness()
    h.data[P]["attributes"]["intervals"][0].update(CHARGE_GRID)
    runner = native.Runner(h)

    def start_automation(runner, entity, value):
        h.blueprint_active = ("automation.old_deye",)

    runner.hook = start_automation
    runner.run()
    assert len(runner.writes) == 1
    assert h.restore_pending
    assert h.stored_runtime()["code"] != "ok"
