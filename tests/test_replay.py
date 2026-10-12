"""tools/replay.py: offline replay of recorded days against the real engine."""

import csv
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("replay", ROOT / "tools/replay.py")
replay = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(replay)

SAMPLE = ROOT / "tools/replay_sample.json"


def run(capsys, *args):
    code = replay.main([str(SAMPLE), "--json", *args])
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out else None), out.err


def test_sample_prices_plan_between_self_use_and_hindsight(capsys):
    code, days, _ = run(capsys)

    assert code == 0
    assert [day["date"] for day in days] == ["2026-09-20", "2026-09-21"]
    expected = {
        "2026-09-20": (-0.7249, 0.7308, -0.7383),
        "2026-09-21": (2.7562, 3.9765, 2.0073),
    }
    for day in days:
        plan, self_use, hindsight = expected[day["date"]]
        assert day["plan"] == pytest.approx(plan, abs=1e-3)
        assert day["self_use"] == pytest.approx(self_use, abs=1e-3)
        assert day["hindsight"] == pytest.approx(hindsight, abs=1e-3)
        assert day["hindsight"] <= day["plan"] + 1e-6
        assert day["solve_failures"] == 0


def test_table_prints_the_three_costs_and_totals(capsys):
    assert replay.main([str(SAMPLE), "--currency", "PLN"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:4] == ["date", "plan", "self_use", "hindsight"]
    assert lines[-1].split()[:4] == ["total", "2.031", "4.707", "1.269"]


def test_set_overrides_a_setting_so_runs_compare(capsys):
    _, baseline, _ = run(capsys)
    code, restricted, _ = run(
        capsys,
        "--set",
        "allow_grid_charge=false",
        "--set",
        "allow_battery_export=false",
    )

    assert code == 0
    assert sum(d["plan"] for d in restricted) > sum(d["plan"] for d in baseline)
    for day in restricted:
        assert day["hindsight"] <= day["plan"] + 1e-6


@pytest.mark.parametrize(
    "override",
    [
        "no_such_setting=1",
        "capacity_kwh=abc",
        "capacity_kwh=0",
        "strategy=x",
        "novalue",
    ],
)
def test_bad_override_is_rejected_with_a_message(capsys, override):
    code, _, err = run(capsys, "--set", override)

    assert code == 2
    assert err.startswith("error:")


def test_reported_cost_does_not_follow_the_terminal_settings(capsys):
    """self_use sees no planner setting, so it must not move with the terminal value."""
    _, baseline, _ = run(capsys)
    for overrides in (
        ("--set", "terminal_mode=value"),
        ("--set", "terminal_mode=value", "--set", "terminal_value_per_kwh=0.2"),
        ("--set", "terminal_value_per_kwh=0.2"),
    ):
        _, days, _ = run(capsys, *overrides)
        assert [d["self_use"] for d in days] == [d["self_use"] for d in baseline]


def test_terminal_value_moves_the_plan_not_the_measuring_stick(capsys):
    _, cheap, _ = run(capsys, "--set", "terminal_mode=value")
    _, dear, _ = run(
        capsys,
        "--set",
        "terminal_mode=value",
        "--set",
        "terminal_value_per_kwh=0.5",
    )

    assert [d["self_use"] for d in cheap] == [d["self_use"] for d in dear]
    # Valuing stored energy at 0 drains the battery by day end, which the report
    # prices at the day's mean buy price; a realistic value does better.
    assert sum(d["plan"] for d in dear) < sum(d["plan"] for d in cheap)


def test_peak_reserve_override_changes_the_plan(capsys):
    ceiling = (
        "--set",
        "limit_grid_charge_price=true",
        "--set",
        "maximum_grid_charge_price=0.4",
    )
    _, without, _ = run(capsys, *ceiling)
    code, reserved, err = run(capsys, *ceiling, "--set", "peak_reserve_margin_kwh=3")

    assert code == 0
    assert "warning" not in err
    assert [d["plan"] for d in reserved] != pytest.approx([d["plan"] for d in without])


def test_peak_reserve_without_a_grid_charge_ceiling_warns(capsys):
    code, _, err = run(capsys, "--set", "peak_reserve_margin_kwh=3")

    assert code == 0
    assert "peak reserve needs limit_grid_charge_price" in err


def _document(**changes):
    document = json.loads(SAMPLE.read_text())
    document.update(changes)
    return document


def _write(tmp_path, document, name="bad.json"):
    path = tmp_path / name
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    return path


def _bad_interval(index, **row):
    document = _document()
    document["days"][1]["intervals"][index].update(row)
    return document


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        (_bad_interval(3, load=-0.2), "days[2][3]: load must not be negative"),
        (
            _bad_interval(4, pv_forecast=-1),
            "days[2][4]: pv_forecast must not be negative",
        ),
        (_bad_interval(5, buy=float("nan")), "days[2][5]: buy must be finite"),
        (_bad_interval(6, sell=None), "days[2][6]: sell must be a number"),
        (_bad_interval(7, pv="x"), "days[2][7]: pv must be a number"),
        (_document(interval_minutes="soon"), "interval_minutes must be a number"),
        (_document(interval_minutes=0), "interval_minutes must be positive"),
        (_document(timezone="Nowhere/Land"), "unknown timezone"),
        (_document(settings=[1]), "settings must be an object"),
        (_document(days={"a": 1}), "days must be a list"),
        (_document(days=[3]), "days[1]: the day must be an object"),
        ("[1, 2]", "the JSON root must be an object"),
        ("{not json", "error:"),
    ],
)
def test_bad_input_file_is_rejected_with_its_location(
    tmp_path, capsys, document, fragment
):
    code = replay.main([str(_write(tmp_path, document))])
    err = capsys.readouterr().err

    assert code == 2
    assert err.startswith("error:")
    assert fragment in err
    assert "Traceback" not in err


def test_bad_initial_soc_is_rejected(tmp_path, capsys):
    document = _document()
    document["days"][0]["initial_soc_percent"] = "half"

    code = replay.main([str(_write(tmp_path, document))])

    assert code == 2
    assert "days[1]: initial_soc_percent must be a number" in capsys.readouterr().err


def test_csv_bad_values_name_the_line(tmp_path, capsys):
    path = tmp_path / "bad.csv"
    path.write_text(
        "start,buy,sell,pv_forecast,load_forecast,pv,load,initial_soc_percent\n"
        "2026-09-20T00:00:00,0.3,0.1,0,1,0,1,40\n"
        "2026-09-20T01:00:00,0.3,0.1,0,1,0,-1,\n"
    )

    assert replay.main([str(path)]) == 2
    assert "line 3: load must not be negative" in capsys.readouterr().err

    path.write_text(
        "start,buy,sell,pv_forecast,load_forecast,pv,load,initial_soc_percent\n"
        "2026-09-20T00:00:00,0.3,0.1,0,1,0,1,lots\n"
        "2026-09-20T01:00:00,0.3,0.1,0,1,0,1,\n"
    )

    assert replay.main([str(path)]) == 2
    assert "line 2: initial_soc_percent must be a number" in capsys.readouterr().err


def test_a_failing_hindsight_solve_names_the_day(tmp_path, capsys, monkeypatch):
    def fail(problem, **kwargs):
        raise replay.SolveError("infeasible")

    monkeypatch.setattr(replay, "solve", fail)

    code = replay.main([str(SAMPLE)])
    err = capsys.readouterr().err

    assert code == 2
    assert err.startswith("error: 2026-09-20: ") and "infeasible" in err


def test_failed_plan_solves_are_counted_per_day_in_json(capsys, monkeypatch):
    real = replay.solve

    def solve(problem, **kwargs):
        if kwargs.get("accept_incumbent"):
            raise replay.SolveError("time limit")
        return real(problem, **kwargs)

    monkeypatch.setattr(replay, "solve", solve)

    code, days, _ = run(capsys)

    assert code == 0
    assert [d["solve_failures"] for d in days] == [24, 24]
    assert [d["plan"] for d in days] == pytest.approx([d["self_use"] for d in days])


def test_csv_gives_the_same_result_as_json(tmp_path, capsys):
    document = json.loads(SAMPLE.read_text())
    path = tmp_path / "days.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["start", "buy", "sell", "pv_forecast", "load_forecast", "pv", "load"]
            + ["initial_soc_percent"]
        )
        for day in document["days"]:
            for index, row in enumerate(day["intervals"]):
                writer.writerow(
                    [f"{day['date']}T{index:02d}:00:00+02:00"]
                    + [
                        row[k]
                        for k in (
                            "buy",
                            "sell",
                            "pv_forecast",
                            "load_forecast",
                            "pv",
                            "load",
                        )
                    ]
                    + [day["initial_soc_percent"] if index == 0 else ""]
                )
    overrides = [
        arg
        for key, value in document["settings"].items()
        for arg in ("--set", f"{key}={value}")
    ]

    code = replay.main([str(path), "--json", "--timezone", "Europe/Warsaw", *overrides])
    from_csv = json.loads(capsys.readouterr().out)
    _, from_json, _ = run(capsys)

    assert code == 0
    assert from_csv == from_json


def _values(**overrides):
    values, _ = replay.resolve_settings(
        {
            "capacity_kwh": 10,
            "operating_floor": 10,
            "charge_kw": 3,
            "discharge_kw": 3,
            "inverter_kw": 5,
            "grid_import_kw": 10,
            "grid_export_kw": 5,
            "eta_charge": 0.9,
            "eta_discharge": 0.9,
        },
        overrides,
    )
    return values


@pytest.mark.parametrize(
    "mode", ["SELF_CONSUME", "HOLD", "CHARGE_GRID", "CHARGE_PV", "DISCHARGE_GRID"]
)
@pytest.mark.parametrize(
    ("pv", "load"), [(0.0, 2.0), (4.0, 1.0), (6.0, 0.2), (1.0, 1.0)]
)
@pytest.mark.parametrize("soc", [1.0, 5.0, 10.0])
def test_realized_interval_balances_and_stays_inside_limits(mode, pv, load, soc):
    values = _values()
    planned = replay.Flow(0.0, 0.0, 2.0, 2.0, 0.5, soc)

    flow, end = replay.realize(mode, planned, pv, load, 1.0, soc, values, 100.0)

    assert (
        pv - flow.curtail_kwh + flow.grid_import_kwh + flow.discharge_kwh
        == pytest.approx(load + flow.grid_export_kwh + flow.charge_kwh)
    )
    assert min(flow.grid_import_kwh, flow.grid_export_kwh) == 0
    assert flow.grid_export_kwh <= 5.0 + 1e-9
    assert flow.charge_kwh <= 3.0 + 1e-9 and flow.discharge_kwh <= 3.0 + 1e-9
    assert (
        abs(pv - flow.curtail_kwh + flow.discharge_kwh - flow.charge_kwh) <= 5.0 + 1e-9
    )
    assert 1.0 - 1e-9 <= end <= 10.0 + 1e-9 or end == pytest.approx(soc)


def _flow(charge=0.0, discharge=0.0, curtail=0.0):
    return replay.Flow(0.0, 0.0, charge, discharge, curtail, 5.0)


def test_charge_pv_follows_the_load_like_self_consumption():
    values = _values()
    planned = _flow(charge=1.0)

    surplus, _ = replay.realize("CHARGE_PV", planned, 3.0, 1.0, 1.0, 5.0, values, 100.0)
    deficit, _ = replay.realize("CHARGE_PV", planned, 0.0, 1.0, 1.0, 5.0, values, 100.0)

    # All of the surplus charges, not just the planned energy; a deficit is
    # covered from the battery instead of the grid (build.py: both caps).
    assert surplus.charge_kwh == pytest.approx(2.0)
    assert surplus.grid_export_kwh == 0
    assert deficit.discharge_kwh == pytest.approx(1.0)
    assert deficit.grid_import_kwh == 0


def _grid_flow(start_soc=5.0, charge=2.0):
    # end SOC as planned: the charge through the 0.9 charging efficiency, no drain
    return replay.Flow(0.0, 0.0, charge, 0.0, 0.0, start_soc + charge * 0.9)


@pytest.mark.parametrize(
    ("pv", "load", "charge", "grid_import", "end"),
    [
        # forecast 1.0 PV / 0.5 load: surplus 0.5, so the grid share of 2.0 is 1.5
        (1.0, 0.5, 2.0, 1.5, 6.8),  # as forecast
        # PV above forecast: the grid stops at the planned SOC, PV charges on top
        (1.5, 0.5, 2.0, 1.0, 6.8),
        (2.5, 0.5, 2.0, 0.0, 6.8),  # PV alone reaches the target: no grid at all
        (3.5, 0.5, 3.0, 0.0, 7.7),  # PV beyond the target, up to the power limit
        # PV below forecast: no shortfall bought from the grid, no catch-up either
        (0.2, 0.5, 1.5, 1.8, 6.35),
    ],
)
def test_charge_grid_stops_the_grid_share_at_the_planned_soc(
    pv, load, charge, grid_import, end
):
    flow, end_soc = replay.realize(
        "CHARGE_GRID",
        _grid_flow(),
        pv,
        load,
        1.0,
        5.0,
        _values(),
        100.0,
        forecast=(1.0, 0.5),
    )

    assert flow.charge_kwh == pytest.approx(charge)
    assert flow.grid_import_kwh == pytest.approx(grid_import)
    assert flow.grid_export_kwh == 0
    assert end_soc == pytest.approx(end)


def test_charge_grid_below_plan_start_is_not_caught_up():
    # Start 1.0 kWh under the 5.0 the plan assumed: only the planned 1.5 grid
    # share is bought, so the SOC ends short of the planned end.
    flow, end_soc = replay.realize(
        "CHARGE_GRID",
        _grid_flow(),
        1.0,
        0.5,
        1.0,
        4.0,
        _values(),
        100.0,
        forecast=(1.0, 0.5),
    )

    assert flow.grid_import_kwh == pytest.approx(1.5)
    assert end_soc == pytest.approx(4.0 + 2.0 * 0.9)
