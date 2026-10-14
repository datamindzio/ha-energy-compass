"""`benchmarks/spill_sweep.py` rolls stay readable by `curtail_sweep.roll_compare`.

The forced-spill rule (T-041) is judged with `spill_sweep.py rollcompare`, which is
`curtail_sweep.roll_compare` reading the rows of `spill_sweep.followed` by position. T-057
gave item 4 of a roll row a meaning (the boundaries that contradict the plan before)
while `followed` kept the stop reason there, and the tool crashed on the first row that
stopped. The command line is the contract, so it is run as the docs name it.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tool(*args):
    done = subprocess.run(
        [sys.executable, "benchmarks/spill_sweep.py", *map(str, args)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


def test_a_roll_is_compared_and_summarised_by_the_tools_the_docs_name(tmp_path):
    path = tmp_path / "roll.json"
    _tool("roll", 22, 10, path)
    rows = json.loads(path.read_text())

    # The draw has a roll followed to the end, one that stops and one whose next
    # boundary changes: without them the comparison below reads nothing.
    assert {row[0] for row in rows} == {"ok", "err"}
    assert any(row[4] for row in rows)
    for row in rows:
        status, reached, _error, trace, changed, why, minutes = row
        assert isinstance(trace, list)
        assert all(isinstance(step, int) for step in changed)
        assert reached == len(trace)
        assert (why != "") == (status == "err")
        assert minutes in (15, 30, 60)

    compared = _tool("rollcompare", path, path)
    assert "'newly_infeasible_while_followed': 0" in compared
    assert "'newly_changed_next_boundary': 0" in compared
    assert "'different_trace': 0" in compared

    summary = _tool("summary", path)
    stopped = sum(row[0] == "err" for row in rows)
    assert summary.count("minimum_mode_minutes") == len({row[-1] for row in rows})
    # The stops by reason, e.g. `stops {'off only': 1, 'start': 2}`.
    assert sum(map(int, re.findall(r"'[^']+': (\d+)", summary))) == stopped


def test_the_switch_audit_counts_the_matrices_of_a_draw_by_the_command_the_docs_name():
    # T-066: `benchmarks/switch_audit.py` solves each matrix with the solver switches both
    # ways; the line it prints is what `docs/model.md` quotes.
    done = subprocess.run(
        [sys.executable, "benchmarks/switch_audit.py", "run", "11", "6"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": f"{ROOT}{os.pathsep}{ROOT / 'benchmarks'}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    found = re.search(
        r"run 11: (\d+) draws, (\d+) matrices, default dearer than another run on (\d+)",
        done.stdout,
    )
    assert found, done.stdout
    draws, matrices, dearer = map(int, found.groups())
    assert (draws, dearer) == (6, 0)
    assert matrices >= 1


def _benchmark(script, *args):
    done = subprocess.run(
        [sys.executable, f"benchmarks/{script}", *map(str, args)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": f"{ROOT}{os.pathsep}{ROOT / 'benchmarks'}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


def test_the_switch_check_site_is_solved_both_ways_by_the_command_the_adr_names():
    # T-089: `benchmarks/switch_site.py <slots> <on|off> <strategy> <probes> <export_kw>` is the
    # command behind the ADR-0007 table of the plan time with the check on and off.
    for check in ("off", "on"):
        out = _benchmark("switch_site.py", 12, check, "cost_min", 0, 1.0)
        found = re.search(
            rf"^{check} 12 cost_min export 1\.0 (\{{.*\}}) second runs (\d+) of (\d+)$",
            out,
            re.MULTILINE,
        )
        assert found, out
        plan = json.loads(found.group(1))
        assert plan["result"] == "optimal"
        assert plan["probe_requested"] == 0
        second, runs = int(found.group(2)), int(found.group(3))
        assert runs >= 1
        if check == "off":
            assert second == 0


def test_the_switch_timing_names_the_draw_by_the_index_spill_sweep_writes(tmp_path):
    # T-089: the ADR and the fixtures name draw 277 of seed 7 by its place in the list of
    # `spill_sweep.py probe 7`; `switch_timing.py` records the same 0-based index.
    path = tmp_path / "timing.json"
    out = _benchmark("switch_timing.py", "probe", 7, 3, path)
    assert re.search(r"^probe 7 3: checked \d+, default runs ", out, re.MULTILINE), out
    data = json.loads(path.read_text())
    assert set(data) == {"records", "dumps", "wall_s"}
    draws = {row["draw"] for row in data["records"]}
    assert draws <= {0, 1, 2}
    assert 0 in draws  # counted from 1 it would have no draw 0
    for row in data["records"]:
        assert {"first_s", "second_s", "taken"} <= set(row)
