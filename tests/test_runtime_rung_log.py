"""`runtime.compute` logs the rung of `solve_released` that planned the interval (T-086).

Rung 1 says nothing; rung 2 (the carried commitment's HOLD) and rung 3 (the plan without the
commitment) write one debug line a site can quote, so ADR-0004's reopening condition can be met
by a log and not only by a benchmark.
"""

import logging
import random
from datetime import UTC, datetime

import pytest

from benchmarks.curtail_sweep import make_aimed, rung_of
from custom_components.energy_compass import runtime
from custom_components.energy_compass.settings import default_configuration

RUNG_1_DRAW = 0
RUNG_3_DRAW = 5  # random.Random(101) + make_aimed on main dd40906


def _draw(index):
    rng = random.Random(101)
    for _ in range(index):
        make_aimed(rng)
    return make_aimed(rng)


def _compute(monkeypatch, problem):
    real = runtime.build_problem

    def build(*args, **kwargs):
        _, values, quality = real(*args, **kwargs)
        return problem, values, quality

    monkeypatch.setattr(runtime, "build_problem", build)
    config = default_configuration("PLN", "UTC")
    return runtime.compute(config, {}, datetime(2026, 12, 18, 11, 0, tzinfo=UTC))


def _lines(caplog):
    return [r for r in caplog.records if "solve_released planned on rung" in r.message]


def test_rung_3_writes_one_debug_line(monkeypatch, caplog):
    problem = _draw(RUNG_3_DRAW)
    assert rung_of(problem) == "uncommitted"
    caplog.set_level(logging.DEBUG, logger=runtime.__name__)
    _compute(monkeypatch, problem)
    (line,) = _lines(caplog)
    assert line.levelno == logging.DEBUG
    first = problem.slots[0]
    assert "rung 3" in line.message
    assert f"carried mode {problem.initial_dispatch_mode} since " in line.message
    assert str(problem.initial_dispatch_mode_since) in line.message
    assert "first planned mode CURTAIL" in line.message
    assert f"PV {first.pv_kwh:.3f} kWh" in line.message
    assert f"load {first.load_kwh:.3f} kWh" in line.message
    pack = problem.battery
    assert f"pack at {100 * pack.initial_kwh / pack.capacity_kwh:.1f}%" in line.message


def test_rung_1_writes_nothing(monkeypatch, caplog):
    problem = _draw(RUNG_1_DRAW)
    assert rung_of(problem) == "first"
    caplog.set_level(logging.DEBUG, logger=runtime.__name__)
    _compute(monkeypatch, problem)
    assert not _lines(caplog)


def test_rung_2_names_the_rung(caplog):
    read = _draw(RUNG_1_DRAW)
    solved = type(read)(**{**read.__dict__, "initial_dispatch_reserve_hold": True})
    plan = runtime.solve(read)
    caplog.set_level(logging.DEBUG, logger=runtime.__name__)
    runtime._log_released_rung(read, plan, solved)
    (line,) = _lines(caplog)
    assert "rung 2" in line.message


@pytest.mark.parametrize("draw", [RUNG_1_DRAW, RUNG_3_DRAW])
def test_nothing_is_formatted_below_debug(monkeypatch, caplog, draw):
    caplog.set_level(logging.INFO, logger=runtime.__name__)
    _compute(monkeypatch, _draw(draw))
    assert not _lines(caplog)
