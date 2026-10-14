"""`benchmarks/load_forecast_backtest.py` runs as the docs name it and reads recorder data."""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tool(*args):
    done = subprocess.run(
        [sys.executable, "benchmarks/load_forecast_backtest.py", *map(str, args)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        check=False,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_level_change_scenario_shows_recency_weighting_cutting_the_morning_error():
    rows = json.loads(_tool("--scenario", "level-change", "--json"))
    plain = rows["plain"]["morning 06-13"]
    weighted = rows["half-life 5 d"]["morning 06-13"]
    assert weighted["mae"] < plain["mae"]
    assert abs(weighted["bias"]) < abs(plain["bias"])
    # The ticket's guard: evening MAE does not get worse by more than 5 %.
    for scenario in ("level-change", "stable"):
        evening = json.loads(_tool("--scenario", scenario, "--json"))
        assert (
            evening["half-life 5 d"]["evening 15-22"]["mae"]
            <= evening["plain"]["evening 15-22"]["mae"] * 1.05
        )


def test_recorder_database_hours_are_differenced_and_gaps_dropped(tmp_path):
    db = tmp_path / "recorder.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE statistics_meta (id INTEGER PRIMARY KEY, statistic_id TEXT)"
    )
    con.execute(
        "CREATE TABLE statistics (metadata_id INTEGER, start_ts REAL, sum REAL)"
    )
    con.execute("INSERT INTO statistics_meta VALUES (1, 'sensor.house_energy')")
    first = datetime(2026, 8, 1, tzinfo=UTC)
    total = 0.0
    for hour in range(24 * 40):
        if hour == 100:  # a missing hour: the next delta spans two hours and is dropped
            continue
        total += 1.0
        con.execute(
            "INSERT INTO statistics VALUES (1, ?, ?)",
            ((first + timedelta(hours=hour)).timestamp(), total),
        )
    con.commit()
    con.close()
    from benchmarks.load_forecast_backtest import read_recorder

    hours = read_recorder(str(db), "sensor.house_energy")
    assert len(hours) == 24 * 40 - 3  # first row, the gap and the row after it
    assert {energy for _, energy in hours} == {1.0}
    out = json.loads(
        _tool(
            "--recorder-db",
            db,
            "--statistic-id",
            "sensor.house_energy",
            "--since",
            "2026-09-05",
            "--until",
            "2026-09-08",
            "--json",
        )
    )
    assert out["plain"]["morning 06-13"]["mae"] < 1e-9
