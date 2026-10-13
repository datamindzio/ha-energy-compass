"""HiGHS' default run against the run without presolve on the forced-spill rows (T-085).

Two matrices of `benchmarks/spill_sweep.py probe` (seed 5 draw 7, seed 22 draw 294, second
matrix of the draw) as `main` built them before T-066 split the spill row
(dd40906): the default run returns status 0, gap 0 and no fractional binary at a plan that
is dearer than one the same matrix has (rows to 1e-6). The fixture holds the model as the
solver saw it, so no benchmark is imported.
"""

import gzip
import json
from pathlib import Path

import numpy as np
import pytest
from scipy.sparse import coo_matrix

from custom_components.energy_compass.engine import optimize

FIXTURES = (
    Path(__file__).parent / "fixtures" / "solver" / "highs_dearer_default_run.json.gz"
)
# name: (cost of the default run, cost of the run without presolve)
MATRICES = {
    "probe_5_7_run_1": (-0.206387, -0.263997),
    "probe_22_294_run_1": (-1.174629, -1.226629),
}


def load(name, *, compare):
    raw = json.loads(gzip.decompress(FIXTURES.read_bytes()))[name]
    model = optimize._Model()
    model.exact_mip_gap = raw["exact_mip_gap"]
    model.cost = raw["cost"]
    model.lower = raw["lower"]
    model.upper = raw["upper"]
    model.integrality = raw["integrality"]
    model.rows = [
        (
            {column: value for column, value in terms},
            -np.inf if lower is None else lower,
            np.inf if upper is None else upper,
        )
        for terms, lower, upper in raw["rows"]
    ]
    model.second_attempt = True
    model.compare_switches = compare
    return model


def matrix_of(model):
    entries = [
        (index, column, value)
        for index, (terms, _, _) in enumerate(model.rows)
        for column, value in terms.items()
        if value
    ]
    return coo_matrix(
        (
            [e[2] for e in entries],
            ([e[0] for e in entries], [e[1] for e in entries]),
        ),
        shape=(len(model.rows), len(model.cost)),
    ).tocsc()


def cost(model, values):
    return float(np.dot(model.cost, values))


@pytest.mark.parametrize("name", MATRICES)
def test_pinned_limit_highs_default_run_calls_a_dearer_plan_optimal(name):
    # Red after a SciPy upgrade means HiGHS no longer does this: correct `docs/model.md`
    # (forced spill) and ADR-0007 (Amendment T-085) with what the new version does.
    model = load(name, compare=False)
    matrix = matrix_of(model)
    default_cost, other_cost = MATRICES[name]
    default = model._run(matrix, 30.0)
    assert default.status == 0
    assert default.fun == pytest.approx(default_cost, abs=1e-5)
    other = model._run(matrix, 30.0, presolve=False)
    assert other.status == 0
    assert other.fun == pytest.approx(other_cost, abs=1e-5)
    assert model._satisfies(matrix, other.x)


@pytest.mark.parametrize("name", MATRICES)
def test_a_model_with_forced_spill_binaries_keeps_the_cheaper_certified_plan(name):
    model = load(name, compare=True)
    values = model.solve(30.0)
    assert cost(model, values) == pytest.approx(MATRICES[name][1], abs=1e-5)
    assert model._satisfies(matrix_of(model), values)


@pytest.mark.parametrize("name", MATRICES)
def test_a_model_without_the_flag_is_solved_once(name, monkeypatch):
    model = load(name, compare=False)
    runs = []
    run = optimize._Model._run

    def counted(self, matrix, time_limit_s, **options):
        runs.append(options)
        return run(self, matrix, time_limit_s, **options)

    monkeypatch.setattr(optimize._Model, "_run", counted)
    model.solve(30.0)
    assert runs == [{}]


def test_a_cheaper_run_that_breaks_a_row_is_not_taken(monkeypatch):
    # The run without presolve is wrong in its own way (T-072): a lower cost counts only
    # where the point satisfies every row of the model.
    name = "probe_5_7_run_1"
    model = load(name, compare=True)
    run = optimize._Model._run

    def broken(self, matrix, time_limit_s, **options):
        result = run(self, matrix, time_limit_s, **options)
        if options.get("presolve") is False:
            x = np.array(result.x)
            # Release the first row's binary-free column to a value no row allows.
            x[np.argmax(np.asarray(self.cost) < 0)] += 50.0
            result.x = x
        return result

    monkeypatch.setattr(optimize._Model, "_run", broken)
    values = model.solve(30.0)
    assert cost(model, values) == pytest.approx(MATRICES[name][0], abs=1e-5)


def test_a_proven_default_run_is_kept_when_the_other_is_not_cheaper(monkeypatch):
    name = "probe_5_7_run_1"
    model = load(name, compare=True)
    run = optimize._Model._run

    def timed_out(self, matrix, time_limit_s, **options):
        result = run(self, matrix, time_limit_s, **options)
        if options.get("presolve") is False:
            result.status = 1
        return result

    monkeypatch.setattr(optimize._Model, "_run", timed_out)
    values = model.solve(30.0)
    assert cost(model, values) == pytest.approx(MATRICES[name][0], abs=1e-5)
