"""The offline shadow comparison of the native decision with the blueprint's own."""

import copy
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import deye_native_harness as native
import pytest
from deye_oracle_v0137 import CONTROLLER, INPUTS, MODE, RT, Harness, Runner

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/shadow_compare.py"
spec = importlib.util.spec_from_file_location("shadow_compare", SCRIPT)
shadow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shadow)


def run_blueprint(h):
    """One pass of the real (frozen) blueprint; Simulation, so nothing is written."""
    h.data[MODE]["state"] = "Simulation"
    Runner(h).run()


def dump_of(h, written):
    """What `GET /api/states` returns for the sample now."""
    h.sync()
    out = []
    for entity, item in copy.deepcopy(h.data).items():
        attributes = item.get("attributes", {})
        if entity == RT:
            attributes["updated_at"] = written
        out.append(
            {
                "entity_id": entity,
                "state": item.get("state", "unknown"),
                "attributes": attributes,
                "last_reported": item.get("last_reported"),
                "last_updated": item.get("last_reported"),
            }
        )
    return out


def setup_of(h):
    ctx = native.limits_of(h.ctx)
    return {
        "timezone": "Europe/Warsaw",
        "controller": CONTROLLER,
        "runtime": RT,
        "entities": {
            "charge": INPUTS["charge_entity"],
            "discharge": INPUTS["discharge_entity"],
            "grid": INPUTS["grid_entity"],
            "operation": INPUTS["operation_entity"],
            "soc": INPUTS["soc_entity"],
            "voltage": INPUTS["voltage_entity"],
        },
        "limits": {
            "max_power_w": ctx.max_power_w,
            "max_current": ctx.max_current,
            "max_grid_current": ctx.max_grid_current,
            "relinquish_current": ctx.relinquish_current,
            "telemetry_entities": list(ctx.telemetry_entities),
            "old_writers": list(ctx.old_writers),
        },
    }


def write(directory, h, minutes, written=None, shift=None, sidecar=True):
    """One dump per minute; `shift` edits the dump before it is written.

    The sidecar holds the in-memory `last_reported` of every entity, as the
    template API reports it, taken before `shift` ages the dump's own copy.
    """
    start = h.now
    for minute in range(minutes):
        h.now = start + timedelta(minutes=minute)
        for item in h.data.values():
            item["last_reported"] = h.now.isoformat()
        run_blueprint(h)
        dump = dump_of(h, written or h.now.isoformat())
        reported = {
            item["entity_id"]: item["last_reported"]
            for item in dump
            if item["last_reported"]
        }
        if shift:
            shift(minute, dump)
        (directory / f"{h.now.isoformat()}.json").write_text(json.dumps(dump))
        if sidecar:
            (directory / f"{h.now.isoformat()}.reported.json").write_text(
                json.dumps(reported)
            )
    return start


@pytest.fixture
def case(tmp_path):
    h = Harness()
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    setup = tmp_path / "setup.json"
    setup.write_text(json.dumps(setup_of(h)))
    return h, dumps, setup


def run(dumps, setup, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(dumps), "--setup", str(setup), *extra],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def test_the_native_decision_equals_the_blueprint_on_every_minute(case):
    h, dumps, setup = case
    write(dumps, h, 5)
    result = run(dumps, setup)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EQUAL 5" in result.stdout
    assert result.stdout.count("EQUAL") >= 5


def test_a_changed_blueprint_decision_is_a_real_difference(case):
    h, dumps, setup = case

    def tamper(minute, dump):
        if minute == 2:
            runtime = next(item for item in dump if item["entity_id"] == RT)
            desired = runtime["attributes"]["runtime"]["desired"]
            key = next(iter(desired))
            desired[key] = desired[key] + 1

    write(dumps, h, 4, shift=tamper)
    result = run(dumps, setup)
    assert result.returncode == 1
    assert "DIFF" in result.stdout and "EQUAL 3" in result.stdout
    assert "desired[" in result.stdout


def test_a_dump_taken_after_a_clock_dependent_change_is_timing_not_a_difference(case):
    h, dumps, setup = case
    written = h.now.isoformat()
    run_blueprint(h)
    first = dump_of(h, written)
    # The same states, a long time later: the blueprint did not run in between.
    for hours in (6, 12):
        later = h.now + timedelta(hours=hours)
        copy_ = copy.deepcopy(first)
        for item in copy_:
            item["last_reported"] = later.isoformat()
        (dumps / f"{later.isoformat()}.json").write_text(json.dumps(copy_))
    result = run(dumps, setup, "--stale-seconds", "100000")
    assert result.returncode == 2, result.stdout
    assert "TIMING 2" in result.stdout and "DIFF 0" in result.stdout


def test_an_old_runtime_is_stale_not_a_difference(case):
    h, dumps, setup = case
    write(dumps, h, 1)
    path = next(p for p in dumps.iterdir() if not p.name.endswith(".reported.json"))
    dump = json.loads(path.read_text())
    runtime = next(item for item in dump if item["entity_id"] == RT)
    runtime["attributes"]["runtime"]["desired"] = {}
    runtime["attributes"]["updated_at"] = (h.now - timedelta(hours=3)).isoformat()
    path.write_text(json.dumps(dump))
    result = run(dumps, setup)
    assert result.returncode == 1
    assert "STALE" in result.stdout
    allowed = run(dumps, setup, "--allow-stale")
    assert allowed.returncode == 2
    assert "STALE" in allowed.stdout


def test_a_dump_without_a_blueprint_decision_is_reported_not_compared(case):
    h, dumps, setup = case
    write(dumps, h, 1)
    path = next(p for p in dumps.iterdir() if not p.name.endswith(".reported.json"))
    dump = [item for item in json.loads(path.read_text()) if item["entity_id"] != RT]
    path.write_text(json.dumps(dump))
    result = run(dumps, setup)
    assert result.returncode == 3
    assert "NOREF" in result.stdout


def test_an_empty_directory_and_a_bad_setup_fail_clearly(tmp_path):
    (tmp_path / "d").mkdir()
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert run(tmp_path / "d", bad).returncode != 0
    good = tmp_path / "setup.json"
    good.write_text(json.dumps(setup_of(Harness())))
    assert run(tmp_path / "d", good).returncode == 3


def stale_rest_copy(minute, dump):
    """The REST cache: `last_reported` equals an older `last_updated`."""
    for item in dump:
        item["last_reported"] = item["last_updated"] = "2026-01-01T00:00:00+00:00"


def test_the_sidecar_replaces_the_cached_last_reported(case):
    h, dumps, setup = case
    write(dumps, h, 3, shift=stale_rest_copy)
    result = run(dumps, setup)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EQUAL 3" in result.stdout
    assert "without .reported.json" not in result.stderr


def test_a_sidecar_value_may_be_a_unix_timestamp(case):
    h, dumps, setup = case
    write(dumps, h, 1, shift=stale_rest_copy)
    path = next(dumps.glob("*.reported.json"))
    reported = {
        entity: datetime.fromisoformat(value).timestamp()
        for entity, value in json.loads(path.read_text()).items()
    }
    path.write_text(json.dumps(reported))
    assert run(dumps, setup).returncode == 0


def test_a_dump_without_a_sidecar_is_a_report_difference_with_a_warning(case):
    h, dumps, setup = case
    write(dumps, h, 3, shift=stale_rest_copy, sidecar=False)
    result = run(dumps, setup)
    assert result.returncode == 1, result.stdout
    assert "REPORT 3" in result.stdout and "DIFF 0" in result.stdout
    assert "3 dumps without .reported.json" in result.stderr
    assert "telemetry-age differences are not reliable" in result.stderr
    allowed = run(dumps, setup, "--allow-rest-reported")
    assert allowed.returncode == 2
    assert "REPORT 3" in allowed.stdout


def test_a_dump_without_a_sidecar_still_compares_when_the_reports_are_fresh(case):
    h, dumps, setup = case
    write(dumps, h, 2, sidecar=False)
    result = run(dumps, setup)
    assert result.returncode == 0, result.stdout
    assert "EQUAL 2" in result.stdout
    assert "2 dumps without .reported.json" in result.stderr


def test_a_refreshing_plan_is_a_refresh_not_a_difference(case):
    h, dumps, setup = case

    def refreshing(minute, dump):
        if minute == 1:
            runtime = next(item for item in dump if item["entity_id"] == RT)
            body = runtime["attributes"]["runtime"]
            body["retained"] = not body["retained"]
            body["slot_energy"] = {"row": 1.0}

    write(dumps, h, 3, shift=refreshing)
    result = run(dumps, setup)
    assert result.returncode == 2, result.stdout
    assert "REFRESH 1" in result.stdout and "DIFF 0" in result.stdout
    assert "EQUAL 2" in result.stdout


def test_a_difference_beyond_retained_is_still_a_difference(case):
    h, dumps, setup = case

    def tamper(minute, dump):
        runtime = next(item for item in dump if item["entity_id"] == RT)
        body = runtime["attributes"]["runtime"]
        body["retained"] = not body["retained"]
        body["state"] = "other"

    write(dumps, h, 1, shift=tamper)
    result = run(dumps, setup)
    assert result.returncode == 1
    assert "DIFF 1" in result.stdout and "REFRESH 0" in result.stdout


def test_sidecars_are_not_taken_for_dumps(case):
    h, dumps, setup = case
    write(dumps, h, 2)
    assert len(list(dumps.glob("*.reported.json"))) == 2
    result = run(dumps, setup)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EQUAL 2" in result.stdout
    assert "reported.json" not in result.stdout
