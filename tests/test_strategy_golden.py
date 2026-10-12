"""Golden regression: cost_min solver coefficients and plans must not drift.

This file is the contract for cost_min. It must keep passing unmodified through
every later dispatch-strategies step; if a step needs to change
tests/golden/cost_min.json, that is a bug in that step, not a stale snapshot.

Plans are compared with a tight float tolerance (last-digit noise differs between
platforms), labels exactly. Where several plans cost the same, HiGHS builds may
pick another one (owner decision T-024 A): such a plan is accepted only if every
slot matching the golden also matches its labels, the differing slots keep the
SOC balance, and the horizon totals and final SOC equal the golden's.
"""

import hashlib
import json
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from custom_components.energy_compass.engine import optimize
from custom_components.energy_compass.engine.models import Battery, Flow

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


_COST_KEYS = (
    "objective",
    "grid_cost",
    "wear_cost",
    "terminal_credit",
    "new_export_episodes",
    "export_episode_reserve",
)
# Flow columns by name, so a reordered or inserted field cannot shift the checks.
_COLUMN = {field.name: index for index, field in enumerate(fields(Flow))}
_NUMERIC_COLUMNS = tuple(name for name in _COLUMN if name.endswith("_kwh"))
_LABEL_COLUMNS = tuple(name for name in _COLUMN if name not in _NUMERIC_COLUMNS)
# Summed over the horizon by the tied-optimum check; end_soc_kwh is a level, not a flow.
_ENERGY_COLUMNS = tuple(name for name in _NUMERIC_COLUMNS if name != "end_soc_kwh")


def _flow_rows(flows):
    return [[getattr(flow, field.name) for field in fields(flow)] for flow in flows]


def _numbers_match(actual_row, expected_row):
    return all(
        actual_row[_COLUMN[name]]
        == pytest.approx(expected_row[_COLUMN[name]], rel=_FLOAT_REL, abs=_FLOAT_ABS)
        for name in _NUMERIC_COLUMNS
    )


def _assert_soc_balance(row, previous_soc_kwh, battery, path):
    assert battery.idle_drain_kw == 0, f"{path}: extend the SOC balance for idle drain"
    step = (
        battery.eta_charge * row[_COLUMN["charge_kwh"]]
        - row[_COLUMN["discharge_kwh"]] / battery.eta_discharge
    )
    assert row[_COLUMN["end_soc_kwh"]] == pytest.approx(
        previous_soc_kwh + step, rel=_FLOAT_REL, abs=_FLOAT_ABS
    ), f"{path} SOC balance"


def _assert_tied_optimum(actual_rows, expected_rows, battery, path):
    """Accept an equally cheap plan that the solver picked on another platform.

    Where several plans cost the same, HiGHS builds break the tie differently
    (e.g. export 2.1 + charge 0.1 instead of export 2.2 in one slot). Only slots
    that differ numerically may carry different labels; they must still balance
    the battery. The cost components are checked by the caller.
    """
    assert len(actual_rows) == len(expected_rows), f"{path} length"
    previous_soc = battery.initial_kwh if battery else None
    for index, (actual, expected) in enumerate(zip(actual_rows, expected_rows)):
        row_path = f"{path}[{index}]"
        if _numbers_match(actual, expected):
            for name in _LABEL_COLUMNS:
                assert actual[_COLUMN[name]] == expected[_COLUMN[name]], (
                    f"{row_path} {name}"
                )
        elif battery:
            _assert_soc_balance(actual, previous_soc, battery, row_path)
        if battery:
            previous_soc = actual[_COLUMN["end_soc_kwh"]]
    for name in _ENERGY_COLUMNS:
        index = _COLUMN[name]
        _assert_matches(
            sum(row[index] for row in actual_rows),
            sum(row[index] for row in expected_rows),
            f"{path} total {name}",
        )
    soc = _COLUMN["end_soc_kwh"]
    _assert_matches(actual_rows[-1][soc], expected_rows[-1][soc], f"{path} final SOC")


def _assert_flows_match(actual_rows, expected_rows, battery, path):
    try:
        _assert_matches(actual_rows, expected_rows, path)
    except AssertionError:
        _assert_tied_optimum(actual_rows, expected_rows, battery, path)


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_golden_cost_min_plan_is_unchanged(monkeypatch, name):
    problem = golden_problems()[name]
    plan, _ = _solve_with_capture(monkeypatch, problem)
    expected = _SNAPSHOT[name]
    for key in _COST_KEYS:
        _assert_matches(getattr(plan, key), expected[key], f"{name}.{key}")
    _assert_flows_match(
        _flow_rows(plan.flows), expected["flows"], problem.battery, f"{name}.flows"
    )


# The comparison itself, on synthetic rows: what the golden test accepts and rejects.

_BATTERY = Battery(
    capacity_kwh=10.0,
    minimum_soc_fraction=0.1,
    maximum_soc_fraction=1.0,
    initial_kwh=5.0,
    charge_kw=4.0,
    discharge_kw=4.0,
    eta_charge=0.9,
    eta_discharge=0.9,
    wear_per_kwh=0.0,
    allow_grid_charge=True,
    allow_battery_export=True,
)


def _synthetic_rows(*slots):
    """(charge, discharge, export, battery_mode, dispatch_mode) -> golden-style rows."""
    soc = _BATTERY.initial_kwh
    rows = []
    for charge, discharge, export, battery_mode, dispatch_mode in slots:
        soc += _BATTERY.eta_charge * charge - discharge / _BATTERY.eta_discharge
        flow = Flow(
            0.0, export, charge, discharge, 0.0, soc, battery_mode, dispatch_mode
        )
        rows.append([getattr(flow, field.name) for field in fields(flow)])
    return rows


_GOLDEN_ROWS = _synthetic_rows(
    (0.0, 0.0, 0.0, "charge", "CHARGE_PV"),
    (0.0, 0.0, 2.2, "discharge", "HOLD"),
    (2.0, 0.0, 0.0, "charge", "CHARGE_PV"),
    (0.2, 0.0, 1.3, "charge", "CHARGE_PV"),
    (0.0, 0.0, 1.0, "discharge", "HOLD"),
)
# Same cost: 0.1 kWh of charging moved from slot 3 to slot 1, export follows.
_TIED_ROWS = _synthetic_rows(
    (0.0, 0.0, 0.0, "charge", "CHARGE_PV"),
    (0.1, 0.0, 2.1, "charge", "CHARGE_PV"),
    (2.0, 0.0, 0.0, "charge", "CHARGE_PV"),
    (0.1, 0.0, 1.4, "charge", "CHARGE_PV"),
    (0.0, 0.0, 1.0, "discharge", "HOLD"),
)


def _compare(actual_rows):
    _assert_flows_match(actual_rows, _GOLDEN_ROWS, _BATTERY, "synthetic")


def _mutated(rows, index, **changes):
    result = [list(row) for row in rows]
    for name, value in changes.items():
        result[index][_COLUMN[name]] = value
    return result


def test_flow_compare_accepts_last_digit_noise():
    noisy = [
        [v + 1e-15 if isinstance(v, float) else v for v in r] for r in _GOLDEN_ROWS
    ]
    _compare(noisy)


def test_flow_compare_accepts_tied_plan_with_equal_totals():
    _compare(_TIED_ROWS)


@pytest.mark.parametrize("name", _LABEL_COLUMNS)
@pytest.mark.parametrize("rows", ("golden", "tied"))
def test_flow_compare_rejects_label_only_change(name, rows):
    base = _GOLDEN_ROWS if rows == "golden" else _TIED_ROWS
    # Slot 4 is numerically identical to the golden in both plans.
    flipped = {"battery_mode": "charge", "dispatch_mode": "SELF_CONSUME"}[name]
    with pytest.raises(AssertionError, match=name):
        _compare(_mutated(base, 4, **{name: flipped}))


def test_flow_compare_rejects_cleared_labels():
    cleared = _GOLDEN_ROWS
    for index in range(len(cleared)):
        cleared = _mutated(cleared, index, battery_mode=None, dispatch_mode=None)
    with pytest.raises(AssertionError):
        _compare(cleared)


def test_flow_compare_rejects_flow_beyond_tolerance_with_changed_total():
    with pytest.raises(AssertionError, match="total grid_export_kwh"):
        _compare(_mutated(_GOLDEN_ROWS, 4, grid_export_kwh=1.0 + 1e-6))


def test_flow_compare_rejects_shifted_flow_with_kept_totals_but_broken_soc():
    broken = _mutated(
        _TIED_ROWS, 1, end_soc_kwh=_TIED_ROWS[1][_COLUMN["end_soc_kwh"]] + 1e-3
    )
    with pytest.raises(AssertionError, match="SOC balance"):
        _compare(broken)


def test_flow_compare_rejects_changed_midhorizon_soc():
    with pytest.raises(AssertionError, match="SOC balance"):
        _compare(_mutated(_GOLDEN_ROWS, 0, end_soc_kwh=0.0))


def test_flow_compare_rejects_changed_final_soc():
    with pytest.raises(AssertionError):
        _compare(
            _mutated(
                _GOLDEN_ROWS,
                4,
                end_soc_kwh=_GOLDEN_ROWS[4][_COLUMN["end_soc_kwh"]] + 1e-6,
            )
        )
