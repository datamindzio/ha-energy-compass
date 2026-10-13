"""`benchmarks/curtail_charge_rows.py` (T-076) runs by the command line ADR-0005 names.

The counts in the ADR (noise below the controller's threshold, charging rows that plan less than
`charge_kw x h`) come from this tool, and it reads the rows `machine_snapshot` publishes, so it is run
here on a few real draws.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

from custom_components.energy_compass.controller_profile import CURTAIL_CHARGE_MIN_KWH

ROOT = Path(__file__).resolve().parents[1]


def test_the_counts_the_adr_quotes_come_out_of_real_draws():
    done = subprocess.run(
        [sys.executable, "benchmarks/curtail_charge_rows.py", "6", "11"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    lines = done.stdout.splitlines()
    assert [line.split(":")[0] for line in lines[:5]] == [
        "run",
        "negrun",
        "deprun",
        "resrun",
        "fullrun",
    ]
    assert lines[5].startswith("all: curtail ")
    assert "partial" in lines[5] and "noise" in lines[5]
    largest = float(re.fullmatch(r"largest noise charge_kwh: (\S+)", lines[6]).group(1))
    assert largest < CURTAIL_CHARGE_MIN_KWH
