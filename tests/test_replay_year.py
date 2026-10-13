"""tools/replay_year.py: the two net-billing bills and the year replay on a synthetic series."""

import csv
import importlib.util
import json
import math
import random
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "replay_year", ROOT / "tools/replay_year.py"
)
year = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(year)
R = year.R

ZONE = "Europe/Warsaw"


def _bill(months, exp, imp, *, rho, fixed=0.0, sell=1.0, buy=1.0, en=0.5):
    n = len(months)
    return year.bill(months, [buy] * n, [sell] * n, [en] * n, imp, exp, rho, fixed)


# --- bills, hand computed: V = export value, D = 1.23 V, E = import kWh x 0.5 -------------------


def test_short_deposit_pays_energy_only_and_both_bills_agree():
    # V = 10, D = 12.3, E = 50: D <= E, the whole deposit is applied, nothing is refunded.
    out = _bill([1, 2], [10.0, 0.0], [0.0, 100.0], rho=0.3, fixed=5.0)
    assert (out["D"], out["V"], out["E"]) == pytest.approx((12.3, 10.0, 50.0))
    assert out["applied"] == pytest.approx(12.3)
    assert out["refund"] == out["void"] == 0.0
    assert out["annual"] == pytest.approx(5.0 + 100.0 - 12.3)
    assert out["ledger"] == pytest.approx(out["annual"])
    assert out["refund_ledger"] == out["void_ledger"] == 0.0


def test_refund_within_the_cap_returns_the_whole_unused_deposit():
    # V = 10, D = 12.3, E = 10: unused 2.3 <= rho x V = 3.
    out = _bill([3, 3], [10.0, 0.0], [20.0, 0.0], rho=0.3)
    assert out["E"] == pytest.approx(10.0)
    assert out["applied"] == pytest.approx(10.0)
    assert out["refund"] == pytest.approx(2.3)
    assert out["void"] == pytest.approx(0.0)
    assert out["annual"] == pytest.approx(20.0 - 10.0 - 2.3)
    # the same month: the vintage is booked in April and still usable in March of the next year
    assert out["ledger"] == pytest.approx(out["annual"])
    assert out["void_ledger"] == pytest.approx(0.0)


def test_refund_cap_binds_at_rho_times_value():
    # same flow, rho = 0.2: refund 2.0 of the unused 2.3, 0.3 is void.
    out = _bill([3, 3], [10.0, 0.0], [20.0, 0.0], rho=0.2)
    assert out["refund"] == pytest.approx(2.0)
    assert out["void"] == pytest.approx(0.3)
    assert out["annual"] == pytest.approx(20.0 - 10.0 - 2.0)
    assert out["refund_ledger"] == pytest.approx(2.0)
    assert out["void_ledger"] == pytest.approx(0.3)


def test_negative_sell_price_deposits_nothing():
    out = _bill([1], [10.0], [4.0], rho=0.2, sell=-0.5)
    assert out["D"] == out["V"] == 0.0
    assert out["annual"] == out["ledger"] == pytest.approx(4.0)


def test_ledger_voids_a_month_the_annual_formula_refunds():
    # March V = 10 (D 12.3), June V = 20 (D 24.6), September energy E = 12.3, rho = 0.2.
    # Annual: unused 24.6, cap 0.2 x 30 = 6, refund 6.
    # Ledger: September is paid from the oldest vintage, March, which ends at 0 and refunds nothing;
    # June keeps 24.6 and returns only 0.2 x 20 = 4, the other 20.6 is void.
    out = _bill([3, 6, 9], [10.0, 20.0, 0.0], [0.0, 0.0, 24.6], rho=0.2, fixed=2.0)
    assert (out["D"], out["E"]) == pytest.approx((36.9, 12.3))
    assert out["refund"] == pytest.approx(6.0)
    assert out["annual"] == pytest.approx(2.0 + 24.6 - 12.3 - 6.0)
    assert out["applied_ledger"] == pytest.approx(12.3)
    assert out["refund_ledger"] == pytest.approx(4.0)
    assert out["void_ledger"] == pytest.approx(20.6)
    assert out["ledger"] == pytest.approx(2.0 + 24.6 - 12.3 - 4.0)
    assert out["ledger"] > out["annual"]


def test_ledger_keeps_the_balance_of_every_vintage():
    # applied + refund + void = D whichever bill, on a flow spread over the year
    months = list(range(1, 13)) * 2
    exp = [30.0, 5.0, 0.0, 12.0, 40.0, 60.0, 80.0, 50.0, 20.0, 5.0, 0.0, 10.0] + [
        0.0
    ] * 12
    imp = [0.0] * 12 + [4.0, 8.0, 10.0, 6.0, 3.0, 2.0, 1.0, 1.0, 5.0, 9.0, 12.0, 8.0]
    out = _bill(months, exp, imp, rho=0.25)
    assert out["D"] > out["E"]
    assert out["applied"] + out["refund"] + out["void"] == pytest.approx(out["D"])
    assert out["applied_ledger"] + out["refund_ledger"] + out[
        "void_ledger"
    ] == pytest.approx(out["D"])
    assert out["refund_ledger"] <= out["refund"] + 1e-9


# --- a month with a negative energy part (ADR-0031 Amendment T-471) ------------------------------


def _ledger_second_year(v, december, april, rho=0.3):
    """Export only in June, full-account start, second year: (applied, refund, void)."""
    deposit = [0.0] * 13
    energy = [0.0] * 13
    deposit[6] = year.DEPOSIT_FACTOR * v
    energy[12], energy[4] = december, april
    return year.ledger_year(deposit, energy, rho, full=True)[-1]


@pytest.mark.parametrize(
    ("v", "december", "applied", "refund", "void"),
    [
        (1000.0, 500.0, 500.0, 300.0, 430.0),
        (1000.0, 1100.0, 1100.0, 130.0, 0.0),
        (400.0, 500.0, 492.0, 0.0, 0.0),
    ],
)
def test_negative_april_never_enters_the_deposit_account(
    v, december, applied, refund, void
):
    out = _ledger_second_year(v, december, -100.0)
    assert out == pytest.approx((applied, refund, void))
    # the negative month changes nothing in the ledger
    assert out == pytest.approx(_ledger_second_year(v, december, 0.0))


def test_year_without_a_negative_month_keeps_its_ledger_tuple():
    # pinned on the code before T-473
    deposit = [0.0] * 13
    deposit[3], deposit[6], deposit[9] = 123.0, 246.0, 61.5
    energy = [0.0] + [10.0, 30, 50, 80, 5, 200, 40, 0, 90, 15, 60, 25]
    assert year.ledger_year(deposit, energy, 0.3, full=True) == pytest.approx(
        [(605.0, 30.0, 3.0), (576.0, 0.0, 0.0)]
    )
    assert year.ledger_year(deposit, energy, 0.3, full=False) == pytest.approx(
        [(353.0, 0.0, 0.0), (430.5, 0.0, 0.0)]
    )


def test_year_with_a_deposit_above_the_liability_keeps_its_ledger_tuple():
    # the regime `bill` sends to the ledger (D 430.5 > E 300), no negative month; pinned on the
    # code before T-473
    deposit = [0.0] * 13
    deposit[3], deposit[6], deposit[9] = 123.0, 246.0, 61.5
    energy = [0.0] + [10.0, 30, 20, 40, 5, 60, 25, 0, 30, 15, 40, 25]
    assert year.ledger_year(deposit, energy, 0.3, full=True) == pytest.approx(
        [(300.0, 96.5, 114.0), (300.0, 66.5, 64.0)]
    )
    assert year.ledger_year(deposit, energy, 0.3, full=False) == pytest.approx(
        [(240.0, 0.0, 0.0), (300.0, 0.0, 0.0)]
    )


def test_bill_keeps_the_signed_energy_part_and_pays_the_liability_only():
    # March V = 10 (D 12.3); January imports E = +20, April E = -8 (en = 0.5, imp = -16 is
    # not a flow, so the negative month is a negative energy price instead).
    months = [1, 3, 4]
    buy, energy = [1.0, 1.0, 1.0], [0.5, 0.5, -0.5]
    imp, exp = [40.0, 0.0, 16.0], [0.0, 10.0, 0.0]
    out = year.bill(months, buy, [1.0] * 3, energy, imp, exp, 0.3, 5.0)
    assert out["E"] == pytest.approx(20.0 - 8.0)
    assert out["E_owed"] == pytest.approx(20.0)
    assert out["applied"] == pytest.approx(12.3)
    assert out["refund"] == out["void"] == 0.0
    assert out["annual"] == pytest.approx(5.0 + 56.0 - 12.3)
    assert out["ledger"] == pytest.approx(out["annual"])


def test_negative_months_on_random_years_keep_the_invariants():
    rng = random.Random(473)
    regime = 0  # years in the ledger regime (D > E+)
    for _ in range(300):
        rho = rng.choice([0.0, 0.2, 0.3, 1.0])
        deposit = [0.0] + [rng.choice([0.0, rng.uniform(0, 400)]) for _ in range(12)]
        # about half the years carry a deposit above the liability (the ledger regime of `bill`)
        scale = rng.choice([1.0, 0.2])
        energy = [0.0] + [rng.uniform(-150, 500) * scale for _ in range(12)]
        owed = sum(max(e, 0.0) for e in energy)
        total = sum(deposit)
        value = sum(deposit) / year.DEPOSIT_FACTOR
        # annual bill through `bill`: one export hour per month, one import hour per month
        months = list(range(1, 13))
        sells = [1.0] * 12
        exp = [deposit[m] / year.DEPOSIT_FACTOR for m in months]
        imp = [1.0] * 12
        buy = [e + 1.0 for e in energy[1:]]  # the energy part is part of the buy price
        out = year.bill(months, buy, sells, energy[1:], imp, exp, rho, 7.0)
        assert out["D"] == pytest.approx(total)
        assert out["applied"] == pytest.approx(min(total, owed))
        assert -1e-9 <= out["refund"] <= min(total - out["applied"], rho * value) + 1e-9
        assert out["applied"] + out["refund"] + out["void"] == pytest.approx(total)
        assert out["applied_ledger"] + out["refund_ledger"] + out[
            "void_ledger"
        ] == pytest.approx(total)
        # the steady state pays the whole liability whenever the deposit covers it
        assert out["applied_ledger"] == pytest.approx(min(total, owed))
        assert out["void_ledger"] >= -1e-9
        assert (
            -1e-9
            <= out["refund_ledger"]
            <= min(total - out["applied_ledger"], rho * value) + 1e-9
        )
        regime += total > owed
        # a negative month made more negative by x lowers both bills by exactly x
        negative = [m for m in months if energy[m] < 0]
        if negative:
            x = rng.uniform(1, 50)
            lower = list(energy)
            lower[negative[0]] -= x
            cheaper = list(buy)
            cheaper[negative[0] - 1] -= x
            more = year.bill(months, cheaper, sells, lower[1:], imp, exp, rho, 7.0)
            assert more["annual"] == pytest.approx(out["annual"] - x)
            assert more["ledger"] == pytest.approx(out["ledger"] - x)
            for key in ("applied", "refund", "applied_ledger", "refund_ledger"):
                assert more[key] == pytest.approx(out[key])
    assert regime >= 100  # the ledger regime is exercised, not an edge case


# --- the year replay ---------------------------------------------------------------------------

_START = datetime(2026, 3, 1, tzinfo=ZoneInfo(ZONE))
_SETTINGS = {
    "capacity_kwh": 10,
    "operating_floor": 10,
    "charge_kw": 5,
    "discharge_kw": 5,
    "inverter_kw": 8,
    "grid_import_kw": 10,
    "grid_export_kw": 5,
    "eta_charge": 0.9,
    "eta_discharge": 0.9,
    "wear_per_kwh": 0.05,
    "terminal_mode": "value",
    "terminal_value_per_kwh": 0.6,
    "solve_time_limit_s": 5,
}


def _rows(days, start=_START, hours=None):
    """Hourly series from `start`, advanced in real time (UTC), `hours` or `days` x 24 of them."""
    rows = []
    first = start.astimezone(UTC)
    for index in range(24 * days if hours is None else hours):
        instant = first + timedelta(hours=index)
        hour = instant.astimezone(ZoneInfo(ZONE)).hour
        peak = 6 <= hour < 10 or 17 <= hour < 20
        buy = 0.3 if hour >= 20 or hour < 2 else 1.2 if peak else 0.8
        pv = (
            max(0.0, 3.0 * math.sin(math.pi * (hour - 6) / 12))
            if 6 <= hour < 18
            else 0.0
        )
        load = 1.5 if peak else 0.5
        rows.append(
            R.Interval(
                instant,
                buy,
                0.3,
                0.9 * pv,
                0.9 * load,
                pv,
                load,
                0.5 * buy,
            )
        )
    return tuple(rows)


def _site(days=3, **changes):
    values = {"rho": 0.2, "fixed": 3.0, "initial_soc_percent": 10.0}
    values.update(changes)
    return year.Site(ZONE, _rows(days), dict(_SETTINGS), **values)


def _values(site):
    values, _ = R.resolve_settings(site.settings, {})
    return values


@pytest.fixture
def solves(monkeypatch):
    """Record every plan solve: start of the problem, SOC, daily counters."""
    calls = []
    real = R.solve

    def spy(problem, **kwargs):
        calls.append(
            (
                problem.slots[0].start,
                problem.battery.initial_kwh,
                problem.pv_generated_today_kwh,
                len(problem.slots),
            )
        )
        return real(problem, **kwargs)

    monkeypatch.setattr(R, "solve", spy)
    return calls


def test_replan_interval_is_respected(solves):
    site = _site(days=2)
    run = year.run_year(site, _values(site), refresh_hours=5)
    starts = [(call[0] - _START) // timedelta(hours=1) for call in solves]
    assert starts == list(range(0, 48, 5))
    assert run["solves"] == len(starts) and run["failures"] == 0
    # a receding 24 h horizon, shortened only by the end of the data
    assert [call[3] for call in solves] == [min(24, 48 - s) for s in starts]


def test_default_interval_is_six_hours(solves):
    site = _site(days=1)
    year.measure(site, _values(site))
    assert [(call[0] - _START) // timedelta(hours=1) for call in solves] == [
        0,
        6,
        12,
        18,
    ]


def test_soc_is_carried_over_midnight_and_between_solves(solves):
    site = _site(days=3)
    run = year.run_year(site, _values(site), refresh_hours=5)
    soc = run["soc"]
    capacity = _SETTINGS["capacity_kwh"]
    floor = capacity * _SETTINGS["operating_floor"] / 100
    # every solve starts from the SOC the executed hours ended on (no daily reset)
    for start, initial, _, _ in solves[1:]:
        hours = (start - _START) // timedelta(hours=1)
        assert initial == pytest.approx(soc[hours - 1])
    # the battery holds energy across both midnights
    assert soc[23] > floor + 0.5 and soc[47] > floor + 0.5
    assert soc[24] == pytest.approx(soc[23], abs=_SETTINGS["charge_kw"] * 1.0)
    assert all(floor - 1e-6 <= value <= capacity + 1e-6 for value in soc)


def test_daily_export_counters_reset_at_local_midnight(solves):
    site = _site(days=2)
    year.run_year(site, _values(site), refresh_hours=6)
    produced = {(call[0] - _START) // timedelta(hours=1): call[2] for call in solves}
    assert produced[0] == produced[24] == 0.0
    assert produced[12] > 0 and produced[36] > 0
    assert produced[18] > produced[12]


def test_local_midnight_follows_the_zone_not_utc(solves):
    # 2026-03-01 00:00 Warsaw = 23:00 UTC the day before: the counter resets there
    site = _site(days=2)
    year.run_year(site, _values(site), refresh_hours=1)
    by_hour = {(call[0] - _START) // timedelta(hours=1): call[2] for call in solves}
    assert by_hour[24] == 0.0
    assert by_hour[23] > 0.0


def test_a_failed_solve_falls_back_to_self_consumption(monkeypatch):
    site = _site(days=2)
    values = _values(site)

    def fail(problem, **kwargs):
        raise R.SolveError("no plan")

    monkeypatch.setattr(R, "solve", fail)
    failed = year.run_year(site, values, refresh_hours=6)
    own = year.run_year(site, values, mode="self")
    assert failed["failures"] == failed["solves"] == 8
    assert failed["imp"] == pytest.approx(own["imp"])
    assert failed["exp"] == pytest.approx(own["exp"])
    assert failed["soc"] == pytest.approx(own["soc"])


def test_plan_beats_self_use_on_the_cheap_night_series():
    site = _site(days=3, rho=0.0)
    values = _values(site)
    plan = year.measure(site, values, refresh_hours=6)
    own = year.measure(site, values, mode="self")
    assert plan["failures"] == 0
    assert plan["K"] < own["K"]
    # K = bill + wear + SOC drift, KL with the ledger
    assert plan["K"] == pytest.approx(plan["annual"] + plan["wear"] + plan["soc_drift"])
    assert plan["KL"] == pytest.approx(
        plan["ledger"] + plan["wear"] + plan["soc_drift"]
    )


def test_measure_values_the_replay_by_the_bill():
    site = _site(days=2)
    values = _values(site)
    run = year.run_year(site, values, mode="self")
    out = year.measure(site, values, mode="self")
    rows = site.intervals
    assert out["import_kwh"] == pytest.approx(sum(run["imp"]))
    assert out["gross"] == pytest.approx(
        site.fixed + sum(i * r.buy for i, r in zip(run["imp"], rows, strict=True))
    )
    assert out["E"] == pytest.approx(
        sum(i * r.buy_energy for i, r in zip(run["imp"], rows, strict=True))
    )
    assert out["D"] == pytest.approx(
        year.DEPOSIT_FACTOR * sum(e * 0.3 for e in run["exp"])
    )


def test_initial_soc_defaults_to_the_operating_floor_and_drift_is_priced():
    site = _site(days=1, initial_soc_percent=None)
    out = year.measure(site, _values(site), mode="self")
    final = year.run_year(site, _values(site), mode="self")["soc"][-1]
    mean_buy = sum(r.buy for r in site.intervals) / 24
    assert out["soc_drift"] == pytest.approx((1.0 - final) * mean_buy)


# --- input and command line --------------------------------------------------------------------


def _document(days=2, start=_START, hours=None):
    return {
        "timezone": ZONE,
        "start": start.isoformat(),
        "rho": 0.2,
        "fixed": 3.0,
        "initial_soc_percent": 10,
        "settings": dict(_SETTINGS),
        "hours": [
            {
                "buy": r.buy,
                "sell": r.sell,
                "buy_energy": r.buy_energy,
                "pv_forecast": r.pv_forecast,
                "load_forecast": r.load_forecast,
                "pv": r.pv,
                "load": r.load,
            }
            for r in _rows(days, start, hours)
        ],
    }


def test_cli_json_and_csv_give_the_same_result(tmp_path, capsys):
    document = _document()
    path = tmp_path / "site.json"
    path.write_text(json.dumps(document))
    csv_path = tmp_path / "site.csv"
    with csv_path.open("w", newline="") as handle:
        names = [
            "start",
            "buy",
            "sell",
            "buy_energy",
            "pv_forecast",
            "load_forecast",
            "pv",
            "load",
        ]
        writer = csv.DictWriter(handle, names)
        writer.writeheader()
        for index, row in enumerate(document["hours"]):
            stamp = _START.astimezone(UTC) + timedelta(hours=index)
            writer.writerow(
                {"start": stamp.astimezone(ZoneInfo(ZONE)).isoformat(), **row}
            )
    base = ["--self-use", "--json"]
    for key, value in _SETTINGS.items():
        base += ["--set", f"{key}={value}"]
    assert year.main([str(path), *base]) == 0
    from_json = json.loads(capsys.readouterr().out)
    args = [
        str(csv_path),
        *base,
        "--timezone",
        ZONE,
        "--rho",
        "0.2",
        "--fixed",
        "3",
        "--initial-soc",
        "10",
    ]
    assert year.main(args) == 0
    from_csv = json.loads(capsys.readouterr().out)
    for key in ("annual", "ledger", "K", "KL", "D", "E", "wear", "soc_drift"):
        assert from_csv[key] == pytest.approx(from_json[key])


def test_cli_overrides_rho_and_fixed(tmp_path, capsys):
    path = tmp_path / "site.json"
    path.write_text(json.dumps(_document(days=1)))
    assert year.main([str(path), "--self-use", "--json"]) == 0
    base = json.loads(capsys.readouterr().out)
    assert year.main([str(path), "--self-use", "--json", "--fixed", "10"]) == 0
    moved = json.loads(capsys.readouterr().out)
    assert moved["annual"] - base["annual"] == pytest.approx(7.0)


def test_table_prints_both_bills(tmp_path, capsys):
    path = tmp_path / "site.json"
    path.write_text(json.dumps(_document(days=1)))
    assert year.main([str(path), "--self-use", "--currency", "PLN"]) == 0
    out = capsys.readouterr().out
    assert "bill annual" in out and "bill ledger" in out and "PLN" in out


def test_table_prints_the_liability_next_to_a_negative_energy_part():
    # a negative month (the hourly input forbids a negative `buy_energy`, so it comes from `bill`
    # directly): E is signed, the liability E+ the deposit pays is only the positive month
    result = year.bill(
        [1, 3, 4],
        [1.0, 1.0, 1.0],
        [1.0] * 3,
        [0.5, 0.5, -0.5],
        [40.0, 0.0, 16.0],
        [0.0, 10.0, 0.0],
        0.3,
        5.0,
    )
    result.update(
        wear=0.0, soc_drift=0.0, K=0.0, KL=0.0, import_kwh=56.0, export_kwh=10.0
    )
    printed = {
        line[:14].strip(): float(line[14:].split()[0])
        for line in year.format_result(result, "PLN").splitlines()
    }
    assert printed["E energy part"] == pytest.approx(12.0)
    assert printed["E+ liability"] == pytest.approx(20.0)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["hours"].pop(), None),  # still valid: 23 hours
        (lambda d: d.update(hours=d["hours"][:1]), "at least two hours"),
        (lambda d: d.update(rho=1.5), "rho must be within"),
        (lambda d: d.update(start="not a time"), "bad timestamp"),
        (lambda d: d.update(timezone="Mars/Base"), "unknown timezone"),
        (lambda d: d["hours"][3].pop("pv"), "hours[3]"),
        (lambda d: d.update(initial_soc_percent=120), "initial_soc_percent"),
    ],
)
def test_bad_input_names_the_place(tmp_path, capsys, change, message):
    document = _document(days=1)
    change(document)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(document))
    code = year.main([str(path), "--self-use", "--json"])
    err = capsys.readouterr().err
    if message is None:
        assert code == 0
    else:
        assert code == 2 and message in err and err.startswith("error:")


def test_hours_must_be_consecutive():
    rows = _rows(1)
    with pytest.raises(R.ReplayError, match="consecutive"):
        year._site(ZONE, rows[:5] + rows[6:], {}, 0.0, 0.0, None)


def test_json_row_start_is_checked_when_given(tmp_path):
    document = _document(days=1)
    document["hours"][4]["start"] = (_START + timedelta(hours=4)).isoformat()
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(document))
    assert len(year.load_json(path).intervals) == 24
    document["hours"][5]["start"] = (_START + timedelta(hours=6)).isoformat()  # a gap
    path.write_text(json.dumps(document))
    with pytest.raises(R.ReplayError, match=r"hours\[5\].*not hour 5"):
        year.load_json(path)


# --- daylight saving time: hours are real instants ------------------------------------------------

_ZONE = ZoneInfo(ZONE)
# local midnights: 26 Oct 2025 has 25 hours, 29 Mar 2026 has 23
_FALL = datetime(2025, 10, 25, tzinfo=_ZONE)
_SPRING = datetime(2026, 3, 28, tzinfo=_ZONE)
_NAMES = [
    "start",
    "buy",
    "sell",
    "buy_energy",
    "pv_forecast",
    "load_forecast",
    "pv",
    "load",
]


def _local_midnights(rows):
    return [i for i, row in enumerate(rows) if row.start.astimezone(_ZONE).hour == 0]


def _write_csv(path, stamps, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, _NAMES)
        writer.writeheader()
        for stamp, row in zip(stamps, rows, strict=True):
            writer.writerow(
                {
                    "start": stamp,
                    "buy": row.buy,
                    "sell": row.sell,
                    "buy_energy": row.buy_energy,
                    "pv_forecast": row.pv_forecast,
                    "load_forecast": row.load_forecast,
                    "pv": row.pv,
                    "load": row.load,
                }
            )


@pytest.mark.parametrize(
    ("start", "text", "midnights"),
    [
        (_FALL, "2025-10-25T00:00:00", [0, 24, 49, 73]),
        (_SPRING, "2026-03-28T00:00:00", [0, 24, 47, 71, 95]),
    ],
)
def test_json_naive_local_start_is_read_in_real_time(tmp_path, start, text, midnights):
    document = _document(hours=96, start=start)
    document["start"] = text  # naive: local to the timezone
    path = tmp_path / "site.json"
    path.write_text(json.dumps(document))
    rows = year.load_json(path).intervals
    assert all(
        later.start.astimezone(UTC) - earlier.start.astimezone(UTC)
        == timedelta(hours=1)
        for earlier, later in pairwise(rows)
    )
    assert _local_midnights(rows) == midnights


@pytest.mark.parametrize(
    ("start", "text", "resets"),
    [
        (_FALL, "2025-10-25T00:00:00", [24, 49, 73]),
        (_SPRING, "2026-03-28T00:00:00", [24, 47, 71, 95]),
    ],
)
def test_daily_counters_reset_at_the_local_midnight_across_dst(
    tmp_path, monkeypatch, start, text, resets
):
    document = _document(hours=96, start=start)
    document["start"] = text
    path = tmp_path / "site.json"
    path.write_text(json.dumps(document))
    site = year.load_json(path)
    seen = []

    def no_plan(problem, **kwargs):
        seen.append(problem.pv_generated_today_kwh)
        raise R.SolveError("recorded")

    monkeypatch.setattr(R, "solve", no_plan)
    run = year.run_year(site, _values(site), refresh_hours=1)
    # a problem per hour was built, none rejected
    assert run["solves"] == 96 == len(seen)
    assert [i for i in range(1, 96) if seen[i] < seen[i - 1]] == resets


@pytest.mark.parametrize("start", [_FALL, _SPRING])
def test_plan_mode_runs_across_a_dst_change(tmp_path, capsys, start):
    document = _document(hours=96, start=start)
    document["start"] = start.strftime("%Y-%m-%dT%H:%M:%S")
    path = tmp_path / "site.json"
    path.write_text(json.dumps(document))
    assert year.main([str(path), "--refresh-hours", "12", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["solves"] == 8 and result["failures"] == 0


def test_ledger_month_comes_from_the_local_date(tmp_path, monkeypatch):
    # 25 Oct 00:00 CEST + 7 d 1 h (the repeated hour) = 1 Nov 00:00 CET, hour 169
    document = _document(hours=24 * 8, start=_FALL)
    document["start"] = "2025-10-25T00:00:00"
    path = tmp_path / "site.json"
    path.write_text(json.dumps(document))
    site = year.load_json(path)
    seen = {}
    real = year.bill

    def spy(months, *args):
        seen["months"] = months
        return real(months, *args)

    monkeypatch.setattr(year, "bill", spy)
    year.measure(site, _values(site), mode="self")
    assert seen["months"][168] == 10 and seen["months"][169] == 11


@pytest.mark.parametrize("start", [_FALL, _SPRING])
def test_csv_with_offsets_is_accepted_across_dst(tmp_path, capsys, start):
    rows = _rows(1, start, 96)
    stamps = [row.start.astimezone(_ZONE).isoformat() for row in rows]
    assert len({stamp[-6:] for stamp in stamps}) == 2  # both offsets are in the file
    path = tmp_path / "site.csv"
    _write_csv(path, stamps, rows)
    site = year.load_csv(path, ZONE, 0.0, 0.0, None)
    assert [row.start for row in site.intervals] == [row.start for row in rows]
    assert year.main([str(path), "--timezone", ZONE, "--self-use", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["import_kwh"] > 0


def test_csv_naive_local_stamps_are_resolved_by_their_order(tmp_path):
    # the repeated 02:00 of 26 Oct 2025 appears twice: first occurrence, then the second
    rows = _rows(1, _FALL, 24 * 2 + 1)
    stamps = [
        row.start.astimezone(_ZONE).replace(tzinfo=None).isoformat() for row in rows
    ]
    assert stamps.count("2025-10-26T02:00:00") == 2
    path = tmp_path / "site.csv"
    _write_csv(path, stamps, rows)
    site = year.load_csv(path, ZONE, 0.0, 0.0, None)
    assert [row.start for row in site.intervals] == [row.start for row in rows]


def test_csv_naive_local_stamps_across_spring_forward(tmp_path):
    rows = _rows(1, _SPRING, 24 * 3)  # 02:00 of 29 Mar 2026 does not exist
    stamps = [
        row.start.astimezone(_ZONE).replace(tzinfo=None).isoformat() for row in rows
    ]
    assert "2026-03-29T02:00:00" not in stamps
    path = tmp_path / "site.csv"
    _write_csv(path, stamps, rows)
    site = year.load_csv(path, ZONE, 0.0, 0.0, None)
    assert [row.start for row in site.intervals] == [row.start for row in rows]


def test_csv_naive_stamp_repeated_three_times_is_rejected(tmp_path):
    rows = _rows(1, _FALL, 24 * 2 + 2)
    stamps = [
        row.start.astimezone(_ZONE).replace(tzinfo=None).isoformat() for row in rows
    ]
    index = stamps.index("2025-10-26T02:00:00")
    stamps.insert(index + 2, "2025-10-26T02:00:00")  # a third 02:00
    path = tmp_path / "site.csv"
    _write_csv(path, stamps[: len(rows)], rows)
    with pytest.raises(R.ReplayError, match="consecutive"):
        year.load_csv(path, ZONE, 0.0, 0.0, None)


# --- refusals and the run report -------------------------------------------------------------------


def test_refresh_longer_than_the_horizon_is_rejected(tmp_path, capsys):
    site = _site(days=2)
    values = _values(site)
    assert values["horizon_hours"] == 24
    with pytest.raises(R.ReplayError, match="horizon_hours"):
        year.run_year(site, values, refresh_hours=25)
    year.run_year(site, values, refresh_hours=24)  # the horizon itself is fine
    path = tmp_path / "site.json"
    path.write_text(json.dumps(_document(days=2)))
    assert year.main([str(path), "--refresh-hours", "48"]) == 2
    assert "horizon_hours" in capsys.readouterr().err


def test_unknown_mode_is_rejected():
    site = _site(days=1)
    with pytest.raises(R.ReplayError, match="mode must be one of"):
        year.run_year(site, _values(site), mode="plna")


@pytest.mark.parametrize("flag", [["--json"], []])
def test_failed_solves_are_reported_on_stderr_in_both_formats(
    tmp_path, capsys, monkeypatch, flag
):
    def fail(problem, **kwargs):
        raise R.SolveError("no plan")

    monkeypatch.setattr(R, "solve", fail)
    path = tmp_path / "site.json"
    path.write_text(json.dumps(_document(days=1)))
    assert year.main([str(path), *flag]) == 0
    captured = capsys.readouterr()
    assert "4 of 4 plan solves failed" in captured.err
    assert "plan solves failed" not in captured.out
    if flag:
        assert json.loads(captured.out)["failures"] == 4
