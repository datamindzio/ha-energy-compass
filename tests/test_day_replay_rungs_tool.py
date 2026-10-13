"""The followed-days rung count (T-081) runs by the command lines ADR-0004 names.

ADR-0004 follow-up T-081 decides on `benchmarks/day_replay_rungs.py` (which rung of `solve_released`
plans each boundary of followed days under `preserve_initial`) and its `compare`. The commands are
the contract, as in `tests/test_aimed_sweep_tool.py`, so they are run here on one day; and the count
is only worth something if it sees rung 3 where there is one, so it is read against the aimed draws.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONPATH": f"{ROOT}{os.pathsep}{ROOT / 'benchmarks'}"}


def _run(*args):
    done = subprocess.run(
        [sys.executable, *map(str, args)],
        cwd=ROOT,
        env=ENV,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


def _tool(*args):
    return _run("benchmarks/day_replay_rungs.py", *args)


def _answer(code):
    # HiGHS writes to the same stream, sometimes without a newline: read what follows the mark
    return json.loads(_run("-c", code).rsplit("ANSWER ", 1)[1])


def test_days_ending_at_midnight_are_counted_and_compared(tmp_path):
    path = tmp_path / "day.json"
    printed = _tool(1, 11, "--seasons", "gloom", "--coverage", "day", "--json", path)
    rows = json.loads(path.read_text())
    # minimum duration 60 and 120, standby loss 0 and 0.13 kW
    assert [row["stretch"] for row in rows] == [
        [11, "gloom", minimum, drain, "day"]
        for minimum in (60, 120)
        for drain in (0, 0.13)
    ]
    for row in rows:
        assert row["boundaries"] == 96
        assert sum(row["rungs"].values()) == 96
        assert set(row["rungs"]) <= {"first", "reserve_hold", "uncommitted", "none"}
        assert sum(row["rung3_first"].values()) == row["rungs"].get("uncommitted", 0)
    assert "'boundaries': 384" in printed.strip().splitlines()[-1]

    compared = _tool("compare", path, path)
    assert "'stretches': 4, 'different': 0" in compared


def test_a_rolling_stretch_follows_its_days_and_not_the_look_ahead():
    row = _answer(
        "import json, day_replay_rungs as tool\n"
        "row = tool.stretch((11, 'gloom', 120, 0.13, 1, 'rolling'))\n"
        "print('ANSWER ' + json.dumps(row))"
    )
    assert row["stretch"] == [11, "gloom", 120, 0.13, "rolling"]
    assert sum(row["rungs"].values()) == row["boundaries"] == 96
    assert row["inside_pv_follow_dwell"] <= 96


AIMED = """
import json, random
import curtail_sweep as sweep, day_replay_rungs as tool
from custom_components.energy_compass.engine import optimize
from custom_components.energy_compass.engine.models import SolveError

rng = random.Random(101)
pairs = []
for _ in range(30):
    problem = sweep.make_aimed(rng)
    try:
        _plan, solved = optimize.solve_released(problem)
    except SolveError:
        continue
    pairs.append([tool.rung_of(problem, solved), sweep.rung_of(problem)])
print("ANSWER " + json.dumps(pairs))
"""


def test_the_count_names_rung_3_as_the_aimed_sweep_does():
    pairs = _answer(AIMED)
    assert all(mine == theirs for mine, theirs in pairs)
    # the aimed draws do reach rung 3; a count of zero on followed days is not the tool's blindness
    assert "uncommitted" in {mine for mine, _ in pairs}
