"""The aimed PV-follow sweep (T-070) runs by the command lines the docs name.

`docs/model.md` and ADR-0004 quote `curtail_sweep.py aimrun`/`aimroll`, read with `compare` and
`rollcompare`, `curtail_sweep.py rungs aimrun` and `benchmarks/aimed_breakdown.py`. The commands are
the contract (a tool that reads rows by position is run on real output, as in
`tests/test_spill_sweep_tool.py`), so they are run here on a few draws.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tool(script, *args):
    done = subprocess.run(
        [sys.executable, f"benchmarks/{script}", *map(str, args)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


def test_aimrun_rows_are_compared_by_the_tool_the_docs_name(tmp_path):
    path = tmp_path / "aimrun.json"
    printed = _tool("curtail_sweep.py", "aimrun", 101, 4, path)
    rows = json.loads(path.read_text())
    assert len(rows) == 4
    assert all(row[0] in ("ok", "err") for row in rows)
    assert printed.strip().endswith("ok of 4")

    compared = _tool("curtail_sweep.py", "compare", path, path)
    assert "'newly_infeasible': 0" in compared
    assert "'newly_planned': 0" in compared
    assert "'same': " in compared


def test_aimroll_rows_are_read_by_rollcompare(tmp_path):
    path = tmp_path / "aimroll.json"
    printed = _tool("curtail_sweep.py", "aimroll", 101, 3, path)
    rows = json.loads(path.read_text())
    assert len(rows) == 3
    assert "followed to the end, of 3" in printed
    for row in rows:
        # status, reached, then the trace and the changed boundaries `rollcompare` reads by position
        assert isinstance(row[3], list)
        assert isinstance(row[4], list)

    compared = _tool("curtail_sweep.py", "rollcompare", path, path)
    assert "'newly_infeasible_while_followed': 0" in compared
    assert "'different_trace': 0" in compared


def test_the_rung_counter_takes_the_aimed_kind():
    counts = ast.literal_eval(_tool("curtail_sweep.py", "rungs", "aimrun", 101, 4))
    assert sum(counts.values()) == 4
    assert set(counts) <= {"first", "reserve_hold", "uncommitted", "none"}


def test_the_breakdown_prints_the_three_named_measures():
    printed = _tool("aimed_breakdown.py", 6, 101)
    assert "draws 6 | rungs {" in printed
    assert "first mode of the rung 3 plans:" in printed
    assert "class on rung 1: no plan with the commitment" in printed
    assert "no plan on any rung:" in printed
    assert "forced surplus profile:" in printed
