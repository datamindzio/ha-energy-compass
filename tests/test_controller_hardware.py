"""The inverter facts, the verified list and the register layout probe."""

import deye_native_harness as native
import pytest
from deye_oracle_v0137 import MODE, P

from custom_components.energy_compass import controller_hardware as hardware
from custom_components.energy_compass import controller_profile as profile

DEVICE = "HV 3-Phase Hybrid Inverter"
ATTRIBUTES = {
    "Protocol Version": "1.4",
    "Control Board Firmware Version": "3002-1095-11408",
    "Communication Board Firmware Version": "2001-C048-0000",
    "Rated Power": 8000,
    "Serial Number": "SN-1234",
}


def test_the_tested_unit_is_the_one_verified_pair():
    assert hardware.VERIFIED == [(DEVICE, "1.4")]
    facts = hardware.facts_from(DEVICE, ATTRIBUTES, "SG0*HP3")
    assert facts.verified and facts.known
    assert facts.control_board == "3002-1095-11408"
    assert facts.rated_power == "8000"


@pytest.mark.parametrize(
    ("state", "protocol", "verified"),
    [
        (DEVICE, "1.4", True),
        (DEVICE, 1.4, True),
        (DEVICE, "1.5", False),
        ("LV Single-Phase Hybrid Inverter", "1.4", False),
        (DEVICE, None, False),
        ("unavailable", "1.4", False),
    ],
)
def test_classify_matches_device_type_and_protocol_only(state, protocol, verified):
    attributes = {**ATTRIBUTES, "Protocol Version": protocol}
    assert hardware.facts_from(state, attributes, None).verified is verified


def test_firmware_is_recorded_not_matched():
    attributes = {**ATTRIBUTES, "Control Board Firmware Version": "9999-0000-00000"}
    assert hardware.facts_from(DEVICE, attributes, None).verified


def test_the_published_facts_never_carry_a_serial_number():
    published = hardware.facts_from(DEVICE, ATTRIBUTES, "SG0*HP3").as_attribute()
    assert "SN-1234" not in str(published)
    assert set(published) == {
        "device_type",
        "protocol",
        "control_board",
        "communication_board",
        "rated_power",
        "model",
        "verified",
    }


def test_missing_facts_are_unknown_not_unverified():
    facts = hardware.facts_from(None, None, None)
    assert not facts.known


def sample():
    h = native.Harness()
    runner = native.Runner(h, probe_layout=True)
    registers = profile.register_map(native.entities_of(h.ctx, h.prefix))
    states = {entity: h.data[entity]["state"] for entity in registers}
    return h, runner, registers, states


def test_the_probe_accepts_the_matching_bank_and_lists_what_differs():
    _, runner, registers, states = sample()
    assert hardware.probe(runner.raw, registers, states) == []
    shifted = {address + 1: raw for address, raw in runner.raw.items()}
    assert len(hardware.probe(shifted, registers, states)) > 10
    first = next(iter(registers))
    changed = {**runner.raw, registers[first].address: 7777}
    assert hardware.probe(changed, registers, states) == [first]


@pytest.mark.parametrize("answer", [None, "bad", [1, 2]])
def test_an_unreadable_answer_never_passes(answer):
    _, _, registers, states = sample()
    assert hardware.probe(answer, registers, states) == [
        "solarman.read_holding_registers"
    ]


def test_the_listing_is_shortened():
    names = [f"number.x{i}" for i in range(10)]
    assert hardware.listed(names[:2]) == "number.x0, number.x1"
    assert hardware.listed(names).endswith("(+4)")


def shifted_runner():
    h, runner, *_ = sample()
    h.data[P]["attributes"]["intervals"][0].update(
        state="CHARGE_GRID", charge_kwh=0.5, pv_kwh=0, end_soc_kwh=20
    )
    good = dict(runner.raw)
    runner.raw = {address + 1: raw for address, raw in good.items()}
    return h, runner, good


def test_a_shifted_layout_blocks_every_write_in_auto():
    h, runner, _ = shifted_runner()
    runner.run()
    assert runner.outcomes == ["layout_mismatch"]
    assert runner.writes == []
    runtime = h.stored_runtime()
    assert runtime["code"] == "blocked"
    assert runtime["reason_code"] == "layout_mismatch"
    assert "number.inverter_deye_battery_max_charging_current" in runtime["reason"]
    assert h.restore_pending is False


def test_the_probe_also_runs_in_simulation():
    h, runner, _ = shifted_runner()
    h.data[MODE]["state"] = "Simulation"
    runner.run()
    assert runner.outcomes == ["layout_mismatch"]
    assert h.stored_runtime()["code"] == "blocked"


def test_a_failed_probe_is_retried_and_a_passed_one_is_not_repeated():
    _, runner, good = shifted_runner()
    runner.run()
    assert len(runner.reads) == 1
    runner.raw = good
    runner.run()
    assert runner.outcomes[-1] == "run_finished"
    assert runner.writes
    reads = len(runner.reads)
    runner.run()
    # Nothing left to write: a proven layout is not read again within the session.
    assert runner.outcomes[-1] == "run_finished"
    assert len(runner.reads) == reads
