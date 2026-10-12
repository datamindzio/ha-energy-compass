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
        "2026-09-21": (2.5161, 3.9765, 2.0073),
    }
    for day in days:
        plan, self_use, hindsight = expected[day["date"]]
        assert day["plan"] == pytest.approx(plan, abs=1e-3)
        assert day["self_use"] == pytest.approx(self_use, abs=1e-3)
        assert day["hindsight"] == pytest.approx(hindsight, abs=1e-3)
        assert day["hindsight"] <= day["plan"] + 1e-6


def test_table_prints_the_three_costs_and_totals(capsys):
    assert replay.main([str(SAMPLE), "--currency", "PLN"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:4] == ["date", "plan", "self_use", "hindsight"]
    assert lines[-1].split()[:4] == ["total", "1.791", "4.707", "1.269"]


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
