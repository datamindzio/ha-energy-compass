"""Annual settlement oracle: a year of hourly data replayed with carried SOC and valued by the bill.

`tools/replay.py` treats days as independent. This tool replays one stretch of consecutive hours
(normally a year) the way the integration runs it: a receding horizon (`horizon_hours`, default 24)
re-planned every N hours on the forecast, the state of charge carried over midnight, the daily
PV-export counters reset at local midnight, every plan executed on the realized series with
`replay.realize`. The hourly grid import and export it produces are valued by two net-billing bills
(ADR-0001 revision 3, ADR-0031 of the calculator):

    annual   deposit D = 1.23 x V pays the liability E+ = sum of max(E_t, 0) of the monthly energy
             parts; refund min(D - min(D, E+), rho x V)
    ledger   monthly vintages, booked the following month, used oldest first for 12 months,
             refund min(unused, rho x V_month) at expiry; the steady state of a repeated year:
             D <= E+ -> applied = D, refund 0; D > E+ -> a warm-up year from a FULL account (every
             live vintage at its whole deposit), then the measured year

Per run: both bills, wear, the SOC drift (initial minus final SOC at the mean buy price),
K = annual bill + wear + SOC drift (KL the same with the ledger), D, V, E (signed), E+ (the liability
the deposit pays), the refund and the void (deposit that neither pays energy nor comes back) of each
bill.

    python tools/replay_year.py site.json --set capacity_kwh=10 --refresh-hours 6
    python tools/replay_year.py site.csv --timezone Europe/Warsaw --rho 0.2 --fixed 310 --self-use

Input: one JSON object, or a CSV with one row per hour.

    JSON  {"timezone": "Europe/Warsaw",          # default UTC
           "start": "2025-10-12T00:00:00",       # first hour (local to timezone, or with offset)
           "rho": 0.2,                           # refund share of the value V (0..1)
           "fixed": 310.0,                       # fixed part of the bill (see Limits)
           "initial_soc_percent": 10,            # default: operating_floor
           "settings": {"capacity_kwh": 10, ...},  # planner settings, as in replay.py
           "hours": [{"buy": .., "sell": .., "buy_energy": .., "pv_forecast": ..,
                      "load_forecast": .., "pv": .., "load": ..}, ...]}
    CSV   start,buy,sell,buy_energy,pv_forecast,load_forecast,pv,load   (settings from --set)

`hours` are consecutive hours (one row per hour, no gaps, in order); a JSON row may carry its own
`start`, which must then be the hour the position implies. `buy`/`sell` are currency/kWh, `buy_energy` the part of `buy` the deposit pays (the energy
part, with excise and VAT as invoiced; missing = 0), the four energies are kWh per hour as in
`tools/replay.py`; `pv_forecast`/`load_forecast` are what was known at plan time, `pv`/`load` what
happened. `--rho`, `--fixed` and `--initial-soc` override the file.

Timestamps are real instants, advanced and compared in UTC, so a year across the DST changes (23 and
25 hour local days) is read correctly; the local date and month (counter reset, ledger month) come
from `timezone`. A stamp with an offset (`+02:00`, `Z`) is exact. A naive stamp is local to
`timezone`: in the repeated autumn hour it is the first occurrence unless the row before is that
first occurrence (then it is the second), so a CSV of consecutive naive local stamps reads right; a
naive JSON `start` in that hour is the first occurrence.

Limits: the fixed part of the bill is one number given in the input (taken from the no-battery bill
of the home); it does not follow the flow, so a charge band that depends on the annual import is
not modelled. `buy_energy` is non-negative as in the engine (`normalize.py`), so a month with a
negative energy part reaches `bill()` only as a signed `energy` list through the Python API, not
through this input. The ledger bins hours by the local month, so a partial first and last month are one
month of the cycle, and a stretch shorter than a year is read as a year with empty months. Energy
is valued per hour: intervals other than one hour are rejected. No household data belongs in the
repository; `tests/test_replay_year.py` uses a synthetic series.
"""

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay as R

DEPOSIT_FACTOR = 1.23
_HOUR = timedelta(hours=1)
_MODES = ("plan", "self")


@dataclass(frozen=True)
class Site:
    """A stretch of consecutive hours with its prices, forecast and realized series."""

    zone_name: str
    intervals: tuple[R.Interval, ...]
    settings: dict
    rho: float = 0.0
    fixed: float = 0.0
    initial_soc_percent: float | None = None


# --- bills -----------------------------------------------------------------------------------


def monthly_totals(
    months: list[int],
    export_value: list[float],
    import_kwh: list[float],
    energy: list[float],
) -> tuple[list[float], list[float]]:
    """Deposit D_m (1.23 x the value of the export) and deposit-payable energy E_m by month 1..12.

    Index 0 is unused. `months` is the local calendar month of every hour; `export_value` the
    hourly export value (kWh x max(sell, 0)), `import_kwh` x `energy` the hourly energy part.
    """
    deposit = [0.0] * 13
    paid = [0.0] * 13
    for month, value, kwh, part in zip(
        months, export_value, import_kwh, energy, strict=True
    ):
        deposit[month] += DEPOSIT_FACTOR * value
        paid[month] += kwh * part
    return deposit, paid


def ledger_year(
    deposit: list[float],
    energy: list[float],
    rho: float,
    *,
    full: bool,
    passes: int = 2,
) -> list[tuple[float, float, float]]:
    """Monthly vintage ledger from January; `(applied, refund, void)` of each of `passes` years.

    Month t: (a) the vintage of the previous month (December for January) is booked with its
    month's deposit; (b) the liability max(E_t, 0) is paid from the oldest vintage; (c) the vintage
    of month t, in use for 12 months, expires: min(balance, rho x V_t) comes back, the rest is void.
    A month with a negative energy part owes nothing and never feeds the account (art. 4c ust. 1,
    ADR-0031 Amendment T-471): the caller keeps it on the bill side at full value.
    `full` starts from a full account: the vintages of January..November hold their whole deposit.
    """
    queue: list[list[float]] = []  # [last step it is usable, balance, V of its month]
    if full:
        for back in range(-11, 0):
            month = (back - 1) % 12 + 1
            if deposit[month] > 0:
                queue.append(
                    [back + 11, deposit[month], deposit[month] / DEPOSIT_FACTOR]
                )
    out = [[0.0, 0.0, 0.0] for _ in range(passes)]
    for step in range(12 * passes):
        year = step // 12
        month = step % 12 + 1
        booked = (step - 1) % 12 + 1
        if (step > 0 or full) and deposit[booked] > 0:
            queue.append([step + 11, deposit[booked], deposit[booked] / DEPOSIT_FACTOR])
        due = max(energy[month], 0.0)
        for vintage in queue:
            used = min(vintage[1], due)
            vintage[1] -= used
            due -= used
            out[year][0] += used
            if due <= 0:
                break
        keep = []
        for vintage in queue:
            if vintage[0] <= step:
                back = min(vintage[1], rho * vintage[2])
                out[year][1] += back
                out[year][2] += vintage[1] - back
            else:
                keep.append(vintage)
        queue = keep
    return [tuple(row) for row in out]


def bill(
    months: list[int],
    buy: list[float],
    sell: list[float],
    energy: list[float],
    imp: list[float],
    exp: list[float],
    rho: float,
    fixed: float = 0.0,
) -> dict:
    """Both yearly bills of an hourly import/export flow (see the module docstring).

    `E` is the signed energy part, `E_owed` the liability E+ = sum of max(E_t, 0) the deposit pays.
    bill = gross - applied - refund with gross = fixed + sum(import x buy): the energy part of a
    month (its negative part too) is inside gross, so a negative month stays on the bill at full
    value, never refunded, capped or voided (ADR-0031 Amendment T-471).
    """
    gross = fixed + sum(i * b for i, b in zip(imp, buy, strict=True))
    deposit_m, energy_m = monthly_totals(
        months, [e * max(s, 0.0) for e, s in zip(exp, sell, strict=True)], imp, energy
    )
    deposit, paid = sum(deposit_m), sum(energy_m)
    # both bills pay the liability E+ = sum of max(E_t, 0); a negative month stays in `gross`
    owed = sum(max(part, 0.0) for part in energy_m[1:])
    value = deposit / DEPOSIT_FACTOR
    applied = min(deposit, owed)
    refund = min(deposit - applied, rho * value)
    if deposit <= owed:
        applied_l, refund_l, void_l = applied, 0.0, 0.0
    else:
        applied_l, refund_l, void_l = ledger_year(deposit_m, energy_m, rho, full=True)[
            -1
        ]
    return {
        "gross": gross,
        "D": deposit,
        "V": value,
        "E": paid,
        "E_owed": owed,
        "applied": applied,
        "refund": refund,
        "void": deposit - applied - refund,
        "annual": gross - applied - refund,
        "applied_ledger": applied_l,
        "refund_ledger": refund_l,
        "void_ledger": void_l,
        "ledger": gross - applied_l - refund_l,
    }


# --- year replay -----------------------------------------------------------------------------


def run_year(
    site: Site,
    values: dict,
    *,
    refresh_hours: int = 6,
    mode: str = "plan",
    on_solve=None,
) -> dict:
    """Replay the stretch; `mode` is `plan` (receding horizon) or `self` (self-consumption).

    Returns the hourly import/export, wear, SOC drift and the solve bookkeeping. A failed solve
    runs its steps as self-consumption (`failures`).
    """
    if mode not in _MODES:
        raise R.ReplayError(f"mode must be one of {_MODES}, got {mode!r}")
    if refresh_hours < 1:
        raise R.ReplayError("refresh_hours must be at least 1")
    horizon = int(values["horizon_hours"])
    if mode == "plan" and refresh_hours > horizon:
        raise R.ReplayError(
            f"refresh_hours ({refresh_hours}) must not exceed horizon_hours ({horizon}): "
            "the hours past the end of the plan would run unplanned"
        )
    rows = site.intervals
    count = len(rows)
    zone = R._zone(site.zone_name)
    days = [row.start.astimezone(zone).date() for row in rows]
    capacity = values["capacity_kwh"]
    initial = (
        values["operating_floor"]
        if site.initial_soc_percent is None
        else site.initial_soc_percent
    )
    soc0 = soc = capacity * initial / 100
    imp = [0.0] * count
    exp = [0.0] * count
    soc_end = [0.0] * count
    throughput_total = produced = exported = throughput = 0.0
    state = since = None
    failures = solves = 0
    solve_s = 0.0
    day = None
    index = 0
    while index < count:
        planned = ()
        problem = None
        if mode == "plan":
            window = min(horizon, count - index)
            slots = tuple(
                R.Slot(
                    rows[i].start.astimezone(UTC),
                    rows[i].start.astimezone(UTC) + _HOUR,
                    rows[i].buy,
                    rows[i].sell,
                    rows[i].pv_forecast,
                    rows[i].load_forecast,
                    rows[i].buy_energy,
                )
                for i in range(index, index + window)
            )
            # the daily counters as the solver sees them at this instant
            fresh = days[index] != day
            problem = R.build_problem(
                values,
                slots,
                soc,
                site.zone_name,
                used_throughput_kwh=0.0 if fresh else throughput,
                pv_generated_kwh=0.0 if fresh else produced,
                exported_kwh=0.0 if fresh else exported,
                mode=state if values["minimum_mode_minutes"] else None,
                mode_since=since if values["minimum_mode_minutes"] else None,
            )
            began = time.perf_counter()
            try:
                plan, problem = R.solve_released(
                    problem,
                    time_limit_s=values["solve_time_limit_s"],
                    accept_incumbent=True,
                    solver=R.solve,
                )
                planned = plan.flows
            except R.SolveError:
                failures += 1
            solve_s += time.perf_counter() - began
            solves += 1
            if on_solve:
                on_solve(index, count)
        for step in range(min(refresh_hours, count - index)):
            i = index + step
            row = rows[i]
            if days[i] != day:
                day = days[i]
                produced = exported = throughput = 0.0
            flow_plan = planned[step] if step < len(planned) else None
            planned_mode = (
                R.machine_state(problem.slots[step], flow_plan) if flow_plan else None
            )
            produced += row.pv
            flow, soc = R.realize(
                planned_mode or "SELF_CONSUME",
                flow_plan,
                row.pv,
                row.load,
                1.0,
                soc,
                values,
                produced - exported,
                (row.pv_forecast, row.load_forecast),
            )
            exported += flow.grid_export_kwh + flow.curtail_kwh
            throughput += flow.charge_kwh + flow.discharge_kwh
            throughput_total += flow.charge_kwh + flow.discharge_kwh
            imp[i], exp[i], soc_end[i] = flow.grid_import_kwh, flow.grid_export_kwh, soc
            if planned_mode and (
                state is None or R.dwell_group(planned_mode) != R.dwell_group(state)
            ):
                state, since = planned_mode, row.start
            elif planned_mode:
                state = planned_mode
        index += refresh_hours
    mean_buy = sum(row.buy for row in rows) / count
    return {
        "imp": imp,
        "exp": exp,
        "soc": soc_end,
        "wear": values["wear_per_kwh"] * throughput_total / 2,
        "soc_drift": (soc0 - soc) * mean_buy,
        "failures": failures,
        "solves": solves,
        "solve_s": solve_s,
    }


def measure(site: Site, values: dict, **kwargs) -> dict:
    """One run: the replay valued by both bills. K / KL = bill + wear + SOC drift."""
    run = run_year(site, values, **kwargs)
    rows = site.intervals
    zone = R._zone(site.zone_name)
    out = bill(
        [row.start.astimezone(zone).month for row in rows],
        [row.buy for row in rows],
        [row.sell for row in rows],
        [row.buy_energy for row in rows],
        run["imp"],
        run["exp"],
        site.rho,
        site.fixed,
    )
    out.update(
        wear=run["wear"],
        soc_drift=run["soc_drift"],
        K=out["annual"] + run["wear"] + run["soc_drift"],
        KL=out["ledger"] + run["wear"] + run["soc_drift"],
        import_kwh=sum(run["imp"]),
        export_kwh=sum(run["exp"]),
        failures=run["failures"],
        solves=run["solves"],
        solve_s=run["solve_s"],
    )
    return out


# --- input -----------------------------------------------------------------------------------


def _check_hours(rows: tuple[R.Interval, ...]) -> None:
    if len(rows) < 2:
        raise R.ReplayError("at least two hours are required")
    for number, (earlier, later) in enumerate(pairwise(rows), 1):
        if later.start.astimezone(UTC) - earlier.start.astimezone(UTC) != _HOUR:
            raise R.ReplayError(
                f"hour {number}: hours must be consecutive, one hour apart"
            )


def _site(zone_name, rows, settings, rho, fixed, soc) -> Site:
    rows = tuple(rows)
    _check_hours(rows)
    if not 0 <= rho <= 1:
        raise R.ReplayError(f"rho must be within 0..1, got {rho}")
    if soc is not None and not 0 <= soc <= 100:
        raise R.ReplayError("initial_soc_percent must be within 0..100")
    return Site(zone_name, rows, settings, rho, fixed, soc)


def _instant(text, zone: ZoneInfo, where: str, previous: datetime | None = None):
    """A timestamp as a UTC instant (see the module docstring for the naive stamps)."""
    value = R._aware(text, zone, where)
    first = value.astimezone(UTC)
    if isinstance(value.tzinfo, ZoneInfo):  # naive: local to the zone
        second = value.replace(fold=1).astimezone(UTC)
        if second == first + _HOUR and previous == first:
            return second
    return first


def load_json(path: Path) -> Site:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise R.ReplayError("the JSON root must be an object")
    zone_name = document.get("timezone", "UTC")
    zone = R._zone(zone_name)
    start = _instant(document.get("start"), zone, "file")
    hours = R._list(document.get("hours", []), "file", "hours")
    rows = []
    for i, row in enumerate(hours):
        where = f"hours[{i}]"
        instant = start + _HOUR * i  # UTC: no wall-clock arithmetic across a DST change
        given = row.get("start") if isinstance(row, dict) else None
        if given not in (None, "") and (
            _instant(given, zone, where, instant - _HOUR) != instant
        ):
            raise R.ReplayError(
                f"{where}: start {given!r} is not hour {i} after the first (gap or repeat)"
            )
        rows.append(R._interval(instant, row, where))
    soc = document.get("initial_soc_percent")
    return _site(
        zone_name,
        rows,
        R._object(document.get("settings", {}), "file", "settings"),
        R._number(document.get("rho", 0.0), "file", "rho"),
        R._number(document.get("fixed", 0.0), "file", "fixed"),
        None if soc is None else R._number(soc, "file", "initial_soc_percent"),
    )


def load_csv(path: Path, zone_name: str, rho: float, fixed: float, soc) -> Site:
    zone = R._zone(zone_name)
    rows = []
    with path.open(newline="", encoding="utf-8") as handle:
        for number, row in enumerate(csv.DictReader(handle), 2):
            where = f"line {number}"
            previous = rows[-1].start if rows else None
            rows.append(
                R._interval(
                    _instant(row.get("start"), zone, where, previous), row, where
                )
            )
    return _site(zone_name, rows, {}, rho, fixed, soc)


# --- command line ----------------------------------------------------------------------------


def format_result(result: dict, currency: str) -> str:
    lines = [
        ("bill annual", result["annual"]),
        ("bill ledger", result["ledger"]),
        ("wear", result["wear"]),
        ("SOC drift", result["soc_drift"]),
        ("K (annual)", result["K"]),
        ("KL (ledger)", result["KL"]),
        ("D deposit", result["D"]),
        ("V value", result["V"]),
        ("E energy part", result["E"]),
        ("E+ liability", result["E_owed"]),
        ("refund annual", result["refund"]),
        ("refund ledger", result["refund_ledger"]),
        ("void annual", result["void"]),
        ("void ledger", result["void_ledger"]),
    ]
    out = [f"{label:<14}{value:>12.2f} {currency}" for label, value in lines]
    out.append(f"{'import kWh':<14}{result['import_kwh']:>12.1f}")
    out.append(f"{'export kWh':<14}{result['export_kwh']:>12.1f}")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "input", type=Path, help="JSON or CSV file (see the module docstring)"
    )
    parser.add_argument(
        "--set", dest="overrides", action="append", default=[], metavar="key=value"
    )
    parser.add_argument(
        "--refresh-hours", type=int, default=6, help="re-plan every N hours"
    )
    parser.add_argument(
        "--self-use", action="store_true", help="plain self-consumption, no solve"
    )
    parser.add_argument(
        "--rho", type=float, help="refund share of V (overrides the file)"
    )
    parser.add_argument(
        "--fixed", type=float, help="fixed part of the bill (overrides the file)"
    )
    parser.add_argument(
        "--initial-soc", type=float, help="initial SOC %% (overrides the file)"
    )
    parser.add_argument("--timezone", default="UTC", help="CSV only (JSON has its own)")
    parser.add_argument("--currency", default="")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    try:
        if args.input.suffix.lower() == ".csv":
            site = load_csv(
                args.input,
                args.timezone,
                args.rho or 0.0,
                args.fixed or 0.0,
                args.initial_soc,
            )
        else:
            site = load_json(args.input)
            overrides = {
                key: value
                for key, value in (
                    ("rho", args.rho),
                    ("fixed", args.fixed),
                    ("initial_soc_percent", args.initial_soc),
                )
                if value is not None
            }
            site = _site(
                site.zone_name,
                site.intervals,
                site.settings,
                overrides.get("rho", site.rho),
                overrides.get("fixed", site.fixed),
                overrides.get("initial_soc_percent", site.initial_soc_percent),
            )
        values, _ = R.resolve_settings(site.settings, R.parse_overrides(args.overrides))
        result = measure(
            site,
            values,
            refresh_hours=args.refresh_hours,
            mode="self" if args.self_use else "plan",
        )
    except (
        R.ReplayError,
        R.InputError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        csv.Error,
    ) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if result["failures"]:
        print(
            f"{result['failures']} of {result['solves']} plan solves failed "
            "(those hours ran as self-consumption)",
            file=sys.stderr,
        )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_result(result, args.currency or "currency"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
