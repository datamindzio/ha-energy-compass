"""Golden regression: cost_min solver coefficients and plans must not drift.

This file is the contract for cost_min. It must keep passing unmodified through
every later dispatch-strategies step; if a step needs to change
tests/golden/cost_min.json, that is a bug in that step, not a stale snapshot.
"""

import hashlib
import json
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from custom_components.energy_compass.engine import optimize

_GOLDEN_DIR = Path(__file__).parent / "golden"
sys.path.insert(0, str(_GOLDEN_DIR))

from fixtures import golden_problems

_SNAPSHOT = json.loads((_GOLDEN_DIR / "cost_min.json").read_text())
_FIXTURE_NAMES = tuple(golden_problems())


def _render_bound(value):
    if value == float("inf"):
        return "inf"
    if value == float("-inf"):
        return "-inf"
    return value


def _rows_sha256(rows):
    payload = [
        [sorted(terms.items()), _render_bound(lo), _render_bound(hi)]
        for terms, lo, hi in rows
    ]
    digest = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(digest).hexdigest()


def _solve_with_capture(monkeypatch, problem):
    captured = {}
    original_solve = optimize._Model.solve

    def spy(self, time_limit_s):
        captured["cost"] = list(self.cost)
        captured["lower"] = list(self.lower)
        captured["upper"] = list(self.upper)
        captured["integrality"] = list(self.integrality)
        captured["rows"] = list(self.rows)
        return original_solve(self, time_limit_s)

    monkeypatch.setattr(optimize._Model, "solve", spy)
    plan = optimize.solve(problem)
    return plan, captured


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_golden_cost_min_solver_model_is_unchanged(monkeypatch, name):
    problem = golden_problems()[name]
    _, captured = _solve_with_capture(monkeypatch, problem)
    expected = _SNAPSHOT[name]
    assert captured["cost"] == expected["cost"]
    assert captured["lower"] == expected["lower"]
    assert [_render_bound(v) for v in captured["upper"]] == expected["upper"]
    assert captured["integrality"] == expected["integrality"]
    assert _rows_sha256(captured["rows"]) == expected["rows_sha256"]


# Last-digit float noise differs between platforms (libm / HiGHS builds); anything
# beyond this tolerance, or any non-numeric difference, is a real plan change.
_FLOAT_REL = 1e-9
_FLOAT_ABS = 1e-9


def _assert_matches(actual, expected, path):
    if isinstance(expected, (list, tuple)):
        assert isinstance(actual, (list, tuple)), path
        assert len(actual) == len(expected), path
        for index, (a, e) in enumerate(zip(actual, expected)):
            _assert_matches(a, e, f"{path}[{index}]")
    elif isinstance(expected, (int, float)) and not isinstance(expected, bool):
        assert not isinstance(actual, bool), path
        assert actual == pytest.approx(expected, rel=_FLOAT_REL, abs=_FLOAT_ABS), path
    else:
        assert actual == expected, path


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_golden_cost_min_plan_is_unchanged(monkeypatch, name):
    problem = golden_problems()[name]
    plan, _ = _solve_with_capture(monkeypatch, problem)
    expected = _SNAPSHOT[name]
    _assert_matches(
        [[getattr(flow, field.name) for field in fields(flow)] for flow in plan.flows],
        expected["flows"],
        f"{name}.flows",
    )
    for key in (
        "objective",
        "grid_cost",
        "wear_cost",
        "terminal_credit",
        "new_export_episodes",
        "export_episode_reserve",
    ):
        _assert_matches(getattr(plan, key), expected[key], f"{name}.{key}")
