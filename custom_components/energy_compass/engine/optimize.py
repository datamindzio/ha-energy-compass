"""Bounded advisory dispatch for grid, solar, and optional battery energy."""

from dataclasses import dataclass, replace
from datetime import UTC
from math import isfinite
from time import perf_counter
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from .balance import REACH_TOL, fallback_costs
from .charge_benefit import constrain_charge_benefit, validate_charge_benefit
from .charge_price import price_allows_grid_charge
from .daily_energy import cumulative_export_bounds, daily_export_rows, day_fractions
from .dispatch_policy import (
    MODES,
    carried_curtail,
    constrain_modes,
    curtail_limit,
    reserve_fallback,
    spill_gate,
    uncommitted_fallback,
    validate_modes,
)
from .export_benefit import constrain_export_benefit, validate_export_benefit
from .idle_drain import cumulative, drain_schedule
from .mode_noise import settle_mode_noise
from .models import (
    FlexibleLoadPlan,
    FlexibleLoadRequest,
    Flow,
    InputError,
    Plan,
    Problem,
    SolveError,
)
from .normalize import validate_problem
from .settlement import planning_buy, planning_sell

_TOL = REACH_TOL
_UTC = UTC


class _Model:
    def __init__(self) -> None:
        self.exact_mip_gap = False
        self.accept_incumbent = False
        self.time_limited = False
        # Set by the rows HiGHS is known to misjudge (`solve`).
        self.second_attempt = False
        self.mip_gap: float | None = 0.0
        self.cost: list[float] = []
        self.lower: list[float] = []
        self.upper: list[float] = []
        self.integrality: list[int] = []
        self.rows: list[tuple[dict[int, float], float, float]] = []

    def variable(
        self, *, upper: float = np.inf, cost: float = 0.0, binary: bool = False
    ) -> int:
        index = len(self.cost)
        self.cost.append(cost)
        self.lower.append(0.0)
        self.upper.append(1.0 if binary else upper)
        self.integrality.append(int(binary))
        return index

    def constrain(self, terms: dict[int, float], lower: float, upper: float) -> None:
        self.rows.append((terms, lower, upper))

    def _run(self, matrix, time_limit_s: float, **options):
        return milp(
            c=np.asarray(self.cost),
            integrality=np.asarray(self.integrality),
            bounds=Bounds(self.lower, self.upper),
            constraints=LinearConstraint(
                matrix,
                [row[1] for row in self.rows],
                [row[2] for row in self.rows],
            ),
            options={
                "time_limit": time_limit_s,
                **({"mip_rel_gap": 0} if self.exact_mip_gap else {}),
                **options,
            },
        )

    def _fractional(self, values) -> bool:
        """Tell whether a binary of a returned point is neither 0 nor 1."""
        binaries = np.asarray(values)[np.asarray(self.integrality, dtype=bool)]
        return bool(np.any(np.minimum(np.abs(binaries), np.abs(binaries - 1)) > _TOL))

    def solve(self, time_limit_s: float) -> np.ndarray:
        entries = [
            (row_index, column, coefficient)
            for row_index, (terms, _, _) in enumerate(self.rows)
            for column, coefficient in terms.items()
            if coefficient
        ]
        matrix = coo_matrix(
            (
                [item[2] for item in entries],
                ([item[0] for item in entries], [item[1] for item in entries]),
            ),
            shape=(len(self.rows), len(self.cost)),
        ).tocsc()
        started = perf_counter()
        result = self._run(matrix, time_limit_s)
        if self.second_attempt and (
            result.status == 2 or result.x is not None and self._fractional(result.x)
        ):
            # HiGHS is not sound on models with the forced-spill rows (T-041, T-072). With
            # presolve it handed back, as optimal, a point with CURTAIL at 0.16 (a need
            # below the minimum activity), and called a model infeasible whose plan the
            # same solver finds without presolve. Without presolve its root cuts have cut
            # the optimum off and it called a far worse plan optimal (HiGHS 1.8.1 to
            # 1.15.1). So a model with those rows that comes back infeasible, or with a
            # fractional binary, gets a second attempt without presolve, in what is left
            # of the time limit. Neither run is a proof: the plan is accepted by the
            # certificate (`_validate_solution`), its gap is only what HiGHS reports, and
            # an infeasible second run may still have a plan.
            result = self._run(
                matrix,
                max(time_limit_s - (perf_counter() - started), 1e-3),
                presolve=False,
            )
        # A time-limited run often holds the optimum long before HiGHS can
        # prove it. Only the base dispatch plan opts in; probes stay exact.
        if self.accept_incumbent and result.status == 1 and result.x is not None:
            self.time_limited = True
            gap = getattr(result, "mip_gap", None)
            self.mip_gap = float(gap) if gap is not None and isfinite(gap) else None
            return np.asarray(result.x)
        if result.status != 0 or result.x is None:
            reasons = {1: "timeout", 2: "infeasible", 3: "unbounded"}
            raise SolveError(
                reasons.get(result.status, "solver_failure"), result.message
            )
        # A proven solve stops at HiGHS's relative gap (default 1e-4 unless the
        # model asked for 0); report it instead of a constant 0.
        gap = getattr(result, "mip_gap", None)
        self.mip_gap = float(gap) if gap is not None and isfinite(gap) else 0.0
        return np.asarray(result.x)


def _check(condition: bool, detail: str) -> None:
    if not condition:
        raise SolveError("solver_failure", f"invalid solver result: {detail}")


def _close(actual: float, expected: float, detail: str) -> None:
    _check(abs(actual - expected) <= _TOL, detail)


def _floor_windows(window_ids: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    """Group slot indices by the window id the autonomy module assigned them."""
    windows: list[list[int]] = []
    for index, window_id in enumerate(window_ids):
        if windows and window_id == window_ids[index - 1]:
            windows[-1].append(index)
        else:
            windows.append([index])
    return tuple(tuple(window) for window in windows)


_SOFT_FLOORS = (
    ("soc_slack", "soc_target_kwh", "soc_target_window", "soc_target_weight"),
    ("peak_slack", "peak_reserve_kwh", "peak_reserve_window", "peak_reserve_weight"),
)


def _active_floor_windows(
    problem: Problem, targets_name: str, window_name: str, weight_name: str
) -> tuple[tuple[int, ...], ...]:
    """Windows of one soft floor; window id -1 marks slots without a floor."""
    targets = getattr(problem, targets_name)
    window_ids = getattr(problem, window_name)
    if not (problem.battery and targets and getattr(problem, weight_name) > 0):
        return ()
    return tuple(
        window for window in _floor_windows(window_ids) if window_ids[window[0]] != -1
    )


def _constrain_balance(
    model: _Model, problem: Problem, vectors: list[dict[str, int]]
) -> tuple[list[int], int | None]:
    """At most one hold window: SOC at/above threshold and no discharge inside."""
    battery = problem.battery
    if not battery or not problem.balance_windows:
        return [], None
    threshold = problem.balance_threshold_kwh
    choices: list[int] = []
    ranking = fallback_costs(
        problem.balance_windows,
        problem.balance_miss_cost,
        initial_kwh=battery.initial_kwh,
        threshold_kwh=threshold,
    )
    for number, window in enumerate(problem.balance_windows):
        chosen = model.variable(binary=True, cost=ranking[number])
        # Registered in the slot vectors so the variable-count check covers it.
        vectors[0][f"balance_choice_{number}"] = chosen
        if problem.balance_fixed:
            model.lower[chosen] = 1.0
        first = window.slots[0]
        if first == 0:
            if battery.initial_kwh < threshold - _TOL:
                model.upper[chosen] = 0.0
        else:
            model.constrain(
                {vectors[first - 1]["energy"]: 1, chosen: -threshold}, 0, np.inf
            )
        for t in window.slots:
            v = vectors[t]
            model.constrain({v["energy"]: 1, chosen: -threshold}, 0, np.inf)
            cap = model.upper[v["bd"]]
            model.constrain({v["bd"]: 1, chosen: cap}, -np.inf, cap)
        choices.append(chosen)
    model.constrain(dict.fromkeys(choices, 1), -np.inf, 1)
    miss = None
    if not problem.balance_fixed:
        miss = model.variable(upper=1.0, cost=problem.balance_miss_cost)
        vectors[0]["balance_miss"] = miss
        model.constrain({**dict.fromkeys(choices, 1), miss: 1}, 1, np.inf)
    return choices, miss


def _validate_balance(
    problem: Problem,
    vectors: list[dict[str, int]],
    values: np.ndarray,
    choices: list[int],
    miss: int | None,
) -> int | None:
    """Independently certify the chosen hold window; return its first slot."""
    if not choices:
        return None
    for index in choices:
        value = float(values[index])
        _check(min(abs(value), abs(value - 1)) <= _TOL, "fractional balance choice")
    picked = [c for c, index in enumerate(choices) if values[index] > 0.5]
    _check(len(picked) <= 1, "balance windows")
    if problem.balance_fixed:
        _check(len(picked) == 1, "fixed balance window")
    else:
        _check(len(picked) + float(values[miss]) >= 1 - _TOL, "balance coverage")
    if not picked:
        return None
    window = problem.balance_windows[picked[0]]
    threshold = problem.balance_threshold_kwh
    first = window.slots[0]
    start = (
        problem.battery.initial_kwh
        if first == 0
        else float(values[vectors[first - 1]["energy"]])
    )
    _check(start >= threshold - _TOL, "balance start energy")
    for t in window.slots:
        _check(
            float(values[vectors[t]["energy"]]) >= threshold - _TOL, "balance energy"
        )
        _check(float(values[vectors[t]["bd"]]) <= _TOL, "balance discharge")
    return first


def _validate_settled(problem: Problem, settled: list[dict[str, float]]) -> None:
    """The published rows keep one direction per slot, whatever noise the solver left."""
    for flow in settled:
        # Cannot fire after `settle_mode_noise` (it nets a grid import beside an export
        # last); it holds that netting to account.
        _check(min(flow["gin"], flow["gout"]) <= _TOL, "simultaneous grid directions")
        if problem.battery:
            _check(
                min(flow["bc"], flow["bd"]) <= _TOL, "simultaneous battery directions"
            )


def _validate_solution(
    problem: Problem,
    vectors: list[dict[str, int]],
    values: np.ndarray,
    daily_fractions: list[dict[str, float]],
    budgets: dict[str, float],
) -> None:
    flexible_load = "flex_load" in vectors[0]
    _check(
        len(values) == max(max(vector.values()) for vector in vectors) + 1,
        "variable count",
    )
    _check(bool(np.all(np.isfinite(values))), "nonfinite variables")
    validate_export_benefit(problem, vectors, values)
    validate_charge_benefit(problem, vectors, values)
    battery = problem.battery
    previous_energy = battery.initial_kwh if battery else 0.0
    drains = drain_schedule(problem)
    lifted = frozenset(problem.balance_lift_slots)
    relayed = carried_curtail(problem)
    spent = {day: 0.0 for day in budgets}
    floor_slacks = [
        (
            getattr(problem, targets_name),
            {
                t: vectors[window[0]][slack_key]
                for window in _active_floor_windows(
                    problem, targets_name, window_name, weight_name
                )
                for t in window
            },
            "autonomy floor slack"
            if slack_key == "soc_slack"
            else "peak reserve slack",
        )
        for slack_key, targets_name, window_name, weight_name in _SOFT_FLOORS
    ]
    peak_index = vectors[0].get("peak_import")

    for index, (slot, variables, fractions) in enumerate(
        zip(problem.slots, vectors, daily_fractions, strict=True)
    ):

        def value(name, variables=variables):
            return float(values[variables[name]])

        duration = (
            slot.end.astimezone(_UTC) - slot.start.astimezone(_UTC)
        ).total_seconds() / 3600
        for name in variables:
            _check(value(name) >= -_TOL, f"negative {name}")
        for name in (
            "grid_mode",
            "battery_mode",
            "reserve_discharge",
            "load_above_solar",
            # HOLD is continuous: the one-hot row below makes it integral.
            *(f"mode_{mode}" for mode in MODES if mode != "HOLD"),
        ):
            if name in variables:
                _check(
                    min(abs(value(name)), abs(value(name) - 1)) <= _TOL,
                    f"fractional {name}",
                )
        if "mode_HOLD" in variables:
            _close(
                sum(value(f"mode_{mode}") for mode in MODES), 1, "one operating mode"
            )
        gin, gout, curt = value("gin"), value("gout"), value("curt")
        for targets, slack_by_slot, detail in floor_slacks:
            if index in slack_by_slot and targets[index] > 0:
                slack = float(values[slack_by_slot[index]])
                _check(slack >= targets[index] - value("energy") - _TOL, detail)
        if peak_index is not None:
            _check(float(values[peak_index]) >= gin / duration - _TOL, "peak import")
        if "cap_import" in variables:
            cap = problem.soft_import_cap_kw * duration
            _check(gin - value("cap_import") <= cap + _TOL, "soft import cap")
        if "cap_export" in variables:
            cap = problem.soft_export_cap_kw * duration
            _check(gout - value("cap_export") <= cap + _TOL, "soft export cap")
        extra_load = value("flex_load") if flexible_load else 0.0
        charge = value("bc") if battery else 0.0
        discharge = value("bd") if battery else 0.0
        _check(
            curt <= curtail_limit(problem, slot, extra_load) + _TOL,
            "curtailment limit",
        )
        if battery and not problem.site.allow_curtailment:
            # Forced spill: the pack cannot take the PV the site cannot pass, or a CURTAIL
            # run that began at that gate goes on (the pack only drifted by its standby
            # loss, CURTAIL moving nothing in or out).
            open_gate = (
                relayed
                or previous_energy
                >= spill_gate(problem, drains, index, extra_load) - _FULL_TOL
            )
            _check(
                curt <= _TOL or open_gate,
                "forced spill with room in the pack",
            )
            relayed = (
                open_gate
                and "mode_CURTAIL" in variables
                and value("mode_CURTAIL") > 0.5
                and curtail_limit(problem, slot) > 0
            )
        _check(
            gin <= problem.site.grid_import_kw * duration + _TOL, "grid import limit"
        )
        _check(
            gout <= problem.site.grid_export_kw * duration + _TOL, "grid export limit"
        )
        # One direction per slot is the pair of rows below with `grid_mode` checked to
        # be within 1e-6 of 0 or 1, up to the noise that leaves on the other side
        # (1e-6 * (M + 1)): `_validate_settled` holds the published rows to it.
        _check(
            gin <= problem.site.grid_import_kw * duration * value("grid_mode") + _TOL,
            "grid import direction",
        )
        _check(
            gout
            <= problem.site.grid_export_kw * duration * (1 - value("grid_mode")) + _TOL,
            "grid export direction",
        )
        _check(
            abs(slot.pv_kwh - curt + discharge - charge)
            <= problem.site.inverter_kw * duration + _TOL,
            "inverter limit",
        )
        _close(
            slot.pv_kwh - curt + gin + discharge,
            slot.load_kwh + extra_load + gout + charge,
            "site balance",
        )
        _close(
            slot.pv_kwh - curt,
            value("pv_load")
            + value("pv_grid")
            + (value("pv_battery") if battery else 0),
            "solar allocation",
        )
        _close(
            gin,
            value("grid_load") + (value("grid_battery") if battery else 0),
            "grid allocation",
        )
        _close(
            slot.load_kwh + extra_load,
            value("pv_load")
            + value("grid_load")
            + (value("battery_load") if battery else 0),
            "load allocation",
        )
        _close(
            gout,
            value("pv_grid") + (value("battery_grid") if battery else 0),
            "export allocation",
        )

        if battery:
            _close(
                discharge,
                value("battery_load") + value("battery_grid"),
                "discharge allocation",
            )
            _close(
                charge, value("pv_battery") + value("grid_battery"), "charge allocation"
            )
            _check(
                charge <= battery.charge_kw * duration * value("battery_mode") + _TOL,
                "charge direction",
            )
            _check(
                discharge
                <= battery.discharge_kw * duration * (1 - value("battery_mode")) + _TOL,
                "discharge direction",
            )
            # Likewise `battery_mode` for the battery: noise on the idle side is settled.
            if not battery.allow_grid_charge or not price_allows_grid_charge(
                problem, slot
            ):
                _check(value("grid_battery") <= _TOL, "grid charging capability")
                _check(
                    charge <= max(slot.pv_kwh - slot.load_kwh - extra_load, 0) + _TOL,
                    "solar surplus charging",
                )
            if not price_allows_grid_charge(problem, slot):
                _check(
                    charge
                    <= max(
                        slot.pv_kwh - curt - slot.load_kwh - extra_load,
                        0,
                    )
                    + _TOL,
                    "price ceiling after curtailment",
                )
            if not battery.allow_battery_export:
                _check(value("battery_grid") <= _TOL, "battery export capability")
                _check(
                    discharge
                    <= max(slot.load_kwh + extra_load - slot.pv_kwh, 0) + _TOL,
                    "residual load discharge",
                )
            discharge_floor = min(
                battery.capacity_kwh * battery.minimum_soc_fraction, previous_energy
            )
            previous_energy += (
                battery.eta_charge * charge
                - discharge / battery.eta_discharge
                - drains[index]
            )
            discharge_floor = max(0.0, discharge_floor - drains[index])
            _close(value("energy"), previous_energy, "battery energy")
            _check(
                discharge_floor - _TOL
                <= previous_energy
                <= (
                    battery.capacity_kwh
                    if index in lifted
                    else battery.capacity_kwh * battery.maximum_soc_fraction
                )
                + _TOL,
                "battery SOC",
            )
            for day, fraction in fractions.items():
                if day in spent:
                    spent[day] += fraction * (charge + discharge)
    if problem.limit_export_to_pv and problem.export_limit_scope == "produced":
        cumulative_export: dict[str, float] = {}
        for entry in cumulative_export_bounds(problem):
            fraction = entry["fraction"]
            if not fraction:
                continue
            v = vectors[entry["slot_index"]]
            day = entry["date"]
            cumulative_export[day] = cumulative_export.get(day, 0.0) + fraction * (
                float(values[v["gout"]]) + float(values[v["curt"]])
            )
            _check(
                cumulative_export[day] <= entry["export_ceiling_kwh"] + _TOL,
                "export exceeds PV generated so far: "
                f"{day} {problem.slots[entry['slot_index']].start.isoformat()}",
            )
    elif problem.limit_export_to_pv:
        for row in daily_export_rows(problem):
            planned = sum(
                fraction.get(row["date"], 0)
                * (float(values[v["gout"]]) + float(values[v["curt"]]))
                for v, fraction in zip(vectors, daily_fractions, strict=True)
            )
            _check(
                planned <= max(0.0, row["remaining_export_kwh"]) + _TOL,
                f"daily export exceeds PV generation: {row['date']}",
            )
    if battery:
        if problem.terminal_mode == "preserve_initial":
            # Standby loss is a tax, not a decision, so "leave it as you found
            # it" is measured against what the plan actually controls. Holding
            # the raw initial SOC here would be infeasible for an install that
            # can neither grid-charge nor see PV inside the horizon.
            terminal = battery.initial_kwh - sum(drains)
            _check(previous_energy >= terminal - _TOL, "terminal SOC")
        for day, total in spent.items():
            _check(total <= budgets[day] + _TOL, f"daily throughput {day}")
    if flexible_load:
        _close(
            sum(float(values[v["flex_load"]]) for v in vectors),
            float(values[vectors[0]["flex_target"]]),
            "flexible load energy",
        )


def solve(
    problem: Problem, *, time_limit_s: float = 10.0, accept_incumbent: bool = False
) -> Plan:
    """Find a bounded least-cost advisory plan or raise a typed solver error.

    `accept_incumbent` returns the best validated solution found before the
    time limit instead of raising `timeout`; the plan is then marked
    `time_limited` with the solver's final relative MIP gap.
    """
    return _solve(
        problem,
        flexible_load=None,
        time_limit_s=time_limit_s,
        accept_incumbent=accept_incumbent,
    ).plan


def solve_released(
    problem: Problem,
    *,
    time_limit_s: float = 10.0,
    budget_s: float | None = None,
    accept_incumbent: bool = False,
    solver=None,
) -> tuple[Plan, Problem]:
    """Solve a carried PV-follow commitment's plan; the house is never left without one.

    The problem as it is first. Where it has no plan (`infeasible`) the ladder of
    ADR-0005 point 24 follows, each rung only where it applies: a commitment with the pack
    at the reserve is released first (point 13), and where that release has no plan either
    the commitment's own HOLD is planned (`reserve_fallback`); last, the plan is made
    without the commitment (`uncommitted_fallback`). Returns the plan and the problem it
    was solved for. Only `infeasible` takes a rung; a timeout or any other solver error
    propagates. `budget_s` bounds all solves together: each later one gets what is left
    of it, at most `time_limit_s`. `solver` is the single-solve function (default `solve`),
    a seam for callers that count or replace the solve.
    """
    solver = solver or solve
    started = perf_counter()
    limit = time_limit_s
    failure = None
    for rung in (None, reserve_fallback, uncommitted_fallback):
        candidate = problem if rung is None else rung(problem)
        if candidate is None:
            continue
        if budget_s is not None:
            limit = min(time_limit_s, budget_s - (perf_counter() - started))
            if limit <= 0:
                raise SolveError("timeout") from failure
        try:
            return (
                solver(
                    candidate, time_limit_s=limit, accept_incumbent=accept_incumbent
                ),
                candidate,
            )
        except SolveError as err:
            if err.reason != "infeasible":
                raise
            failure = failure or err
    raise failure


def later_rungs(problem: Problem) -> tuple[Problem, ...]:
    """The problems of the rungs after the one `problem` was solved on, in the order of the ladder.

    The ladder of `solve_released` for a flexible-load probe: a level that has no solution on
    the problem the plan was solved for (the model bounds what a probe may take from a pack
    that is just full by its largest draw, so a plan on that edge has no probe) is tried on
    these. Each is a different problem, so a level solved on one is priced against a plan
    solved on that same problem, never against the plan of another rung.
    """
    return tuple(
        rung
        for rung in (reserve_fallback(problem), uncommitted_fallback(problem))
        if rung is not None
    )


def solve_flexible_load(
    problem: Problem,
    request: FlexibleLoadRequest,
    *,
    time_limit_s: float = 10.0,
) -> FlexibleLoadPlan:
    """Find least-cost dispatch and schedule for one flexible energy target."""
    try:
        energy_kwh = float(request.energy_kwh)
        max_power_kw = float(request.max_power_kw)
    except (AttributeError, TypeError, ValueError) as err:
        raise InputError("flexible load values must be finite and positive") from err
    if (
        not isfinite(energy_kwh)
        or energy_kwh <= 0
        or not isfinite(max_power_kw)
        or max_power_kw <= 0
    ):
        raise InputError("flexible load values must be finite and positive")
    return _solve(
        problem,
        flexible_load=FlexibleLoadRequest(energy_kwh, max_power_kw),
        time_limit_s=time_limit_s,
    )


# A forced spill's gate is certified this far below its energy (kWh, validator).
_FULL_TOL = 1e-3


@dataclass
class _Spill:
    """One slot's forced spill: its binary, its gate row and the gate the row is certified against."""

    gate: float
    binary: int | None = None
    row: dict[int, float] | None = None


def _gate_forced_spill(
    model, problem, v, index, drains, previous_energy_index, flexible_load
) -> _Spill | None:
    """Let a site that cannot curtail spill only the PV it cannot pass, into a pack that cannot take it.

    `spill_open` opens the slot's forced spill: the pack cannot take the slot's need at
    its start (`spill_gate`). With minimum mode durations a CURTAIL run under way also
    keeps the next slot open (`_relay_forced_spill`): CURTAIL charges nothing, so the pack
    drifts below the gate by its standby loss while the inverter throttles. The gate is
    the strictest the flexible load allows, so what the model plans the certificate
    (written with the load realised) accepts.
    Returns the slot's gate for the relay, or None where the slot has no need or is shut.
    """
    if problem.site.allow_curtailment or model.upper[v["curt"]] <= 0:
        return None
    battery = problem.battery
    upper = model.upper[v["curt"]]
    flex_upper = model.upper[v["flex_load"]] if flexible_load else 0.0
    gate = spill_gate(problem, drains, index, flex_upper)
    # A CURTAIL run carried into the plan keeps its first interval open, as a run inside
    # the plan keeps the next one (`_relay_forced_spill`).
    if (
        previous_energy_index is None
        and battery.initial_kwh < gate - 1e-6
        and not carried_curtail(problem)
    ):
        model.upper[v["curt"]] = 0
        return None
    slot = problem.slots[index]
    hours = (slot.end - slot.start).total_seconds() / 3600
    spill = _Spill(gate)
    # Nothing to gate where even the emptiest pack cannot take the need.
    gated = (
        previous_energy_index is not None and gate > model.lower[previous_energy_index]
    )
    if gated or flexible_load:
        model.second_attempt = True
        spill.binary = spill_open = v["spill_open"] = model.variable(binary=True)
        model.constrain({v["curt"]: 1, spill_open: -upper}, -np.inf, 0)
    if gated:
        spill.row = {previous_energy_index: 1, spill.binary: -gate}
        model.constrain(spill.row, 0, np.inf)
    if not flexible_load:
        return spill
    # A flexible load takes from what the export cap leaves to spill, not from what the
    # inverter rating does: the need is `max(export part - load, inverter part)`.
    flex_upper = model.upper[v["flex_load"]]
    inverter_part = max(slot.pv_kwh - problem.site.inverter_kw * hours, 0.0)
    if inverter_part <= 0:
        # curt + flex <= upper where it spills; no limit on the load where it does not.
        model.constrain(
            {v["curt"]: 1, v["flex_load"]: 1, spill.binary: flex_upper},
            -np.inf,
            upper + flex_upper,
        )
    elif upper > inverter_part:
        export_part = model.variable(binary=True)
        v["spill_export_part"] = export_part
        model.constrain(
            {v["curt"]: 1, export_part: -(upper - inverter_part)},
            -np.inf,
            inverter_part,
        )
        reach = upper + flex_upper
        model.constrain(
            {v["curt"]: 1, v["flex_load"]: 1, export_part: reach},
            -np.inf,
            upper + reach,
        )
    return spill


def _relay_forced_spill(model, vectors, spills) -> None:
    """Keep a CURTAIL run's spill open while the run lasts (minimum mode duration).

    CURTAIL charges and discharges nothing, so the pack of a run that began at the gate
    only drifts by its standby loss: the next slot's gate row takes the run's mode as the
    reason to stay open, and CURTAIL itself is allowed only where the gate is open. A
    chain rooted that way ends at a slot whose pack was at the gate, which is what the
    certificate reads (`_validate_solution`). Without minimum durations the pack charges
    its standby loss back, so there is nothing to relay.
    """
    for index, spill in enumerate(spills):
        if spill is None or "mode_CURTAIL" not in vectors[index]:
            continue
        curtail = vectors[index]["mode_CURTAIL"]
        if model.upper[curtail] <= 0:
            continue
        if spill.binary is not None:
            model.constrain({curtail: 1, spill.binary: -1}, -np.inf, 0)
        following = spills[index + 1] if index + 1 < len(spills) else None
        if following is not None and following.row is not None:
            following.row[curtail] = following.gate


def _solve(
    problem: Problem,
    *,
    flexible_load: FlexibleLoadRequest | None,
    time_limit_s: float,
    accept_incumbent: bool = False,
) -> FlexibleLoadPlan:
    validate_problem(problem)
    try:
        time_limit = float(time_limit_s)
    except (TypeError, ValueError) as err:
        raise InputError("time limit must be finite and positive") from err
    if not isfinite(time_limit) or time_limit <= 0:
        raise InputError("time limit must be finite and positive")

    budgets = dict(problem.remaining_daily_throughput_kwh)
    if len(budgets) != len(problem.remaining_daily_throughput_kwh):
        raise InputError("duplicate daily throughput date")
    zone = ZoneInfo(problem.timezone)
    model = _Model()
    model.accept_incumbent = accept_incumbent
    vectors: list[dict[str, int]] = []
    daily_fractions: list[dict[str, float]] = []
    previous_energy_index: int | None = None
    spills: list[_Spill | None] = []
    battery = problem.battery
    drains = drain_schedule(problem)
    drained = cumulative(drains)
    lifted = frozenset(problem.balance_lift_slots)

    for index, slot in enumerate(problem.slots):
        duration = (
            slot.end.astimezone(_UTC) - slot.start.astimezone(_UTC)
        ).total_seconds() / 3600
        variables = {
            "gin": model.variable(
                upper=problem.site.grid_import_kw * duration,
                cost=problem.import_weight * planning_buy(problem, slot)
                + problem.import_kwh_weight,
            ),
            "gout": model.variable(
                upper=problem.site.grid_export_kw * duration,
                cost=-problem.export_weight
                * (planning_sell(problem, slot) - problem.pv_export_margin),
            ),
            "curt": model.variable(upper=curtail_limit(problem, slot)),
            "pv_load": model.variable(upper=slot.pv_kwh),
            "pv_grid": model.variable(upper=slot.pv_kwh),
            "grid_load": model.variable(upper=problem.site.grid_import_kw * duration),
            "grid_mode": model.variable(binary=True),
        }
        if flexible_load:
            variables["flex_load"] = model.variable(
                upper=flexible_load.max_power_kw * duration
            )
        if battery:
            variables.update(
                {
                    "bc": model.variable(
                        upper=battery.charge_kw * duration,
                        cost=battery.wear_per_kwh / 2,
                    ),
                    "bd": model.variable(
                        upper=battery.discharge_kw * duration,
                        cost=battery.wear_per_kwh / 2,
                    ),
                    "energy": model.variable(
                        upper=battery.capacity_kwh
                        if index in lifted
                        else battery.capacity_kwh * battery.maximum_soc_fraction,
                        cost=-problem.terminal_value_per_kwh
                        if problem.terminal_mode == "value"
                        and slot is problem.slots[-1]
                        else 0,
                    ),
                    "pv_battery": model.variable(upper=slot.pv_kwh),
                    "grid_battery": model.variable(
                        upper=problem.site.grid_import_kw * duration
                        if battery.allow_grid_charge
                        and price_allows_grid_charge(problem, slot)
                        else 0
                    ),
                    "battery_load": model.variable(
                        upper=battery.discharge_kw * duration
                    ),
                    "battery_grid": model.variable(
                        upper=battery.discharge_kw * duration
                        if battery.allow_battery_export
                        else 0,
                        cost=problem.battery_export_penalty_per_kwh,
                    ),
                    "battery_mode": model.variable(binary=True),
                }
            )
            reserve = battery.capacity_kwh * battery.minimum_soc_fraction
            floor = max(0.0, min(reserve, battery.initial_kwh) - drained[index])
            model.lower[variables["energy"]] = floor
            if floor < reserve:
                # Below reserve, waiting or gradual charging remains feasible.
                # Any discharge must finish at/above the full reserve; a fixed
                # lowered energy bound alone would allow spending partial
                # recovery, or let standby loss open the floor to discharge.
                gate = variables["reserve_discharge"] = model.variable(binary=True)
                model.constrain(
                    {variables["bd"]: 1, gate: -battery.discharge_kw * duration},
                    -np.inf,
                    0,
                )
                model.constrain({variables["energy"]: 1, gate: -reserve}, 0, np.inf)
        v = variables
        model.constrain(
            {v["gin"]: 1, v["grid_mode"]: -problem.site.grid_import_kw * duration},
            -np.inf,
            0,
        )
        model.constrain(
            {v["gout"]: 1, v["grid_mode"]: problem.site.grid_export_kw * duration},
            -np.inf,
            problem.site.grid_export_kw * duration,
        )
        if problem.soft_import_cap_kw is not None and problem.cap_violation_weight > 0:
            cap = problem.soft_import_cap_kw * duration
            slack = max(0.0, problem.site.grid_import_kw * duration - cap)
            u = v["cap_import"] = model.variable(
                upper=slack, cost=problem.cap_violation_weight
            )
            model.constrain({v["gin"]: 1, u: -1}, -np.inf, cap)
        if problem.soft_export_cap_kw is not None and problem.cap_violation_weight > 0:
            cap = problem.soft_export_cap_kw * duration
            slack = max(0.0, problem.site.grid_export_kw * duration - cap)
            u = v["cap_export"] = model.variable(
                upper=slack, cost=problem.cap_violation_weight
            )
            model.constrain({v["gout"]: 1, u: -1}, -np.inf, cap)
        model.constrain(
            {v["curt"]: -1, **({v["bd"]: 1, v["bc"]: -1} if battery else {})},
            -problem.site.inverter_kw * duration - slot.pv_kwh,
            problem.site.inverter_kw * duration - slot.pv_kwh,
        )
        solar = {v["curt"]: 1, v["pv_load"]: 1, v["pv_grid"]: 1}
        if battery:
            solar[v["pv_battery"]] = 1
        model.constrain(solar, slot.pv_kwh, slot.pv_kwh)
        grid = {v["gin"]: 1, v["grid_load"]: -1}
        load = {v["pv_load"]: 1, v["grid_load"]: 1}
        if flexible_load:
            load[v["flex_load"]] = -1
        export = {v["gout"]: 1, v["pv_grid"]: -1}
        if battery:
            grid[v["grid_battery"]] = -1
            load[v["battery_load"]] = 1
            export[v["battery_grid"]] = -1
            model.constrain(
                {v["bd"]: 1, v["battery_load"]: -1, v["battery_grid"]: -1}, 0, 0
            )
            model.constrain(
                {v["bc"]: 1, v["pv_battery"]: -1, v["grid_battery"]: -1}, 0, 0
            )
            model.constrain(
                {v["bc"]: 1, v["battery_mode"]: -battery.charge_kw * duration},
                -np.inf,
                0,
            )
            model.constrain(
                {v["bd"]: 1, v["battery_mode"]: battery.discharge_kw * duration},
                -np.inf,
                battery.discharge_kw * duration,
            )
            spills.append(
                _gate_forced_spill(
                    model,
                    problem,
                    v,
                    index,
                    drains,
                    previous_energy_index,
                    flexible_load,
                )
            )
            energy = {
                v["energy"]: 1,
                v["bc"]: -battery.eta_charge,
                v["bd"]: 1 / battery.eta_discharge,
            }
            if previous_energy_index is not None:
                energy[previous_energy_index] = -1
            initial = battery.initial_kwh if previous_energy_index is None else 0
            # Leave the row untouched when standby loss is off, so a disabled
            # setting keeps the solver model byte-identical to 0.1.18.
            balance = initial - drains[index] if drains[index] else initial
            model.constrain(energy, balance, balance)
            previous_energy_index = v["energy"]
            solar_surplus = slot.pv_kwh - slot.load_kwh
            load_above_solar = None
            if (
                flexible_load
                and solar_surplus > 0
                and (
                    not battery.allow_grid_charge
                    or not price_allows_grid_charge(problem, slot)
                    or not battery.allow_battery_export
                )
            ):
                load_above_solar = model.variable(binary=True)
                v["load_above_solar"] = load_above_solar
                model.constrain(
                    {
                        v["flex_load"]: 1,
                        load_above_solar: -model.upper[v["flex_load"]],
                    },
                    -np.inf,
                    solar_surplus,
                )
                model.constrain(
                    {v["flex_load"]: 1, load_above_solar: -solar_surplus},
                    0,
                    np.inf,
                )
            if not battery.allow_grid_charge or not price_allows_grid_charge(
                problem, slot
            ):
                if not flexible_load or solar_surplus <= 0:
                    model.constrain({v["bc"]: 1}, -np.inf, max(solar_surplus, 0))
                else:
                    charge_cap = model.upper[v["bc"]]
                    model.constrain(
                        {
                            v["bc"]: 1,
                            v["flex_load"]: 1,
                            load_above_solar: -model.upper[v["flex_load"]],
                        },
                        -np.inf,
                        solar_surplus,
                    )
                    model.constrain(
                        {v["bc"]: 1, load_above_solar: charge_cap},
                        -np.inf,
                        charge_cap,
                    )
            if (
                not price_allows_grid_charge(problem, slot)
                and problem.site.allow_curtailment
            ):
                # Curtailing solar cannot create an apparent surplus for charging.
                # With no charge, the idle/discharge direction permits curtailment.
                model.constrain(
                    {v["bc"]: 1, v["curt"]: 1, v["battery_mode"]: slot.pv_kwh},
                    -np.inf,
                    max(slot.pv_kwh - slot.load_kwh, 0) + slot.pv_kwh,
                )
            if not battery.allow_battery_export:
                if not flexible_load:
                    model.constrain(
                        {v["bd"]: 1},
                        -np.inf,
                        max(slot.load_kwh - slot.pv_kwh, 0),
                    )
                elif solar_surplus <= 0:
                    model.constrain(
                        {v["bd"]: 1, v["flex_load"]: -1},
                        -np.inf,
                        -solar_surplus,
                    )
                else:
                    discharge_cap = model.upper[v["bd"]]
                    model.constrain(
                        {v["bd"]: 1, load_above_solar: -discharge_cap},
                        -np.inf,
                        0,
                    )
                    model.constrain(
                        {
                            v["bd"]: 1,
                            v["flex_load"]: -1,
                            load_above_solar: discharge_cap + solar_surplus,
                        },
                        -np.inf,
                        discharge_cap,
                    )
        model.constrain(grid, 0, 0)
        model.constrain(load, slot.load_kwh, slot.load_kwh)
        model.constrain(export, 0, 0)
        vectors.append(variables)
        daily_fractions.append(day_fractions(slot.start, slot.end, zone))

    for slack_key, targets_name, window_name, weight_name in _SOFT_FLOORS:
        targets = getattr(problem, targets_name)
        for window in _active_floor_windows(
            problem, targets_name, window_name, weight_name
        ):
            slack = model.variable(
                upper=max(targets[t] for t in window),
                cost=getattr(problem, weight_name),
            )
            vectors[window[0]][slack_key] = slack
            for t in window:
                if targets[t] > 0:
                    model.constrain(
                        {vectors[t]["energy"]: 1, slack: 1}, targets[t], np.inf
                    )
    balance_choices, balance_miss = _constrain_balance(model, problem, vectors)

    if problem.peak_import_weight > 0:
        peak = model.variable(
            upper=problem.site.grid_import_kw, cost=problem.peak_import_weight
        )
        vectors[0]["peak_import"] = peak
        for slot, v in zip(problem.slots, vectors, strict=True):
            duration = (
                slot.end.astimezone(_UTC) - slot.start.astimezone(_UTC)
            ).total_seconds() / 3600
            model.constrain({v["gin"]: 1 / duration, peak: -1}, -np.inf, 0)

    if flexible_load:
        target = model.variable(upper=flexible_load.energy_kwh)
        model.lower[target] = flexible_load.energy_kwh
        vectors[0]["flex_target"] = target
        model.constrain(
            {
                **{variables["flex_load"]: 1 for variables in vectors},
                target: -1,
            },
            0,
            0,
        )

    if problem.limit_export_to_pv and problem.export_limit_scope == "produced":
        # One running total per local day: each slot's export (gout + curt)
        # feeds a cumulative variable bounded by the PV accrued since that
        # day's midnight, so export can never get ahead of production. A
        # running variable keeps this O(slots) instead of O(slots^2) dense
        # prefix rows.
        day_cumulative: dict[str, int] = {}
        for entry in cumulative_export_bounds(problem):
            fraction = entry["fraction"]
            if not fraction:
                continue
            v = vectors[entry["slot_index"]]
            running = model.variable()
            v[f"export_cum_{entry['date']}"] = running
            terms = {running: 1, v["gout"]: -fraction, v["curt"]: -fraction}
            previous = day_cumulative.get(entry["date"])
            if previous is not None:
                terms[previous] = -1
            model.constrain(terms, 0, 0)
            model.constrain({running: 1}, -np.inf, entry["export_ceiling_kwh"])
            day_cumulative[entry["date"]] = running
    elif problem.limit_export_to_pv:
        for row in daily_export_rows(problem):
            model.constrain(
                {
                    v[name]: fraction[row["date"]]
                    for v, fraction in zip(vectors, daily_fractions, strict=True)
                    if row["date"] in fraction
                    for name in ("gout", "curt")
                },
                -np.inf,
                # Export already above today's PV cannot be undone; it only
                # forbids more. A negative bound would make every plan infeasible.
                max(0.0, row["remaining_export_kwh"]),
            )
    if battery:
        constrain_modes(
            model, problem, vectors, flexible_load=flexible_load is not None
        )
        _relay_forced_spill(model, vectors, spills)
        if problem.terminal_mode == "preserve_initial":
            model.constrain(
                {previous_energy_index: 1},
                battery.initial_kwh - sum(drains),
                np.inf,
            )
        for day, budget in budgets.items():
            terms = {}
            for variables, fractions in zip(vectors, daily_fractions, strict=True):
                fraction = fractions.get(day, 0)
                if fraction:
                    terms[variables["bc"]] = fraction
                    terms[variables["bd"]] = fraction
            if terms:
                model.constrain(terms, -np.inf, budget)

    constrain_export_benefit(model, problem, vectors)
    constrain_charge_benefit(model, problem, vectors)
    values = model.solve(time_limit)
    _validate_solution(problem, vectors, values, daily_fractions, budgets)
    balance_start = _validate_balance(
        problem, vectors, values, balance_choices, balance_miss
    )
    settled = settle_mode_noise(problem, vectors, values, model.upper)
    _validate_settled(problem, settled)
    flows = tuple(
        Flow(
            f["gin"],
            f["gout"],
            f["bc"],
            f["bd"],
            f["curt"],
            f["energy"],
            ("charge" if values[v["battery_mode"]] > 0.5 else "discharge")
            if battery
            else None,
            next(
                (
                    mode
                    for mode in MODES
                    if f"mode_{mode}" in v and values[v[f"mode_{mode}"]] > 0.5
                ),
                None,
            ),
        )
        for v, f in zip(vectors, settled, strict=True)
    )
    # A probe is a load: solver noise below zero (-3e-17) is none. Left in, it would
    # lift a slot that sits exactly at the minimum activity from idle (the base
    # reading) to open as realised, and the mode certificate would fail.
    probe = (
        tuple(max(float(values[v["flex_load"]]), 0.0) for v in vectors)
        if flexible_load
        else ()
    )
    mode_problem = (
        replace(
            problem,
            slots=tuple(
                replace(slot, load_kwh=slot.load_kwh + extra)
                for slot, extra in zip(problem.slots, probe, strict=True)
            ),
        )
        if flexible_load
        else problem
    )
    bridges = tuple(
        i
        for i, v in enumerate(vectors)
        if "hold_run" in v and values[v["hold_run"]] < 0.5
    )
    released = validate_modes(
        mode_problem,
        flows,
        bridges,
        tuple(model.upper[v["flex_load"]] for v in vectors) if flexible_load else (),
    )
    # A HOLD that goes on with a CURTAIL run is a bridge for the commitment as well; an
    # idle row has both readings, and is listed once.
    bridges = tuple(
        sorted(
            {
                *bridges,
                *(
                    i
                    for i, v in enumerate(vectors)
                    if "curtail_bridge" in v and values[v["curtail_bridge"]] > 0.5
                ),
            }
        )
    )
    grid_cost = sum(
        slot.buy_per_kwh * flow.grid_import_kwh
        - slot.sell_per_kwh * flow.grid_export_kwh
        for slot, flow in zip(problem.slots, flows, strict=True)
    )
    wear_cost = (
        sum(
            battery.wear_per_kwh * (flow.charge_kwh + flow.discharge_kwh) / 2
            for flow in flows
        )
        if battery
        else 0.0
    )
    terminal_credit = (
        problem.terminal_value_per_kwh * flows[-1].end_soc_kwh
        if battery and problem.terminal_mode == "value"
        else 0.0
    )
    objective = grid_cost + wear_cost - terminal_credit
    _check(isfinite(objective), "nonfinite objective")
    autonomy_shortfall_kwh = sum(
        float(values[v["soc_slack"]]) for v in vectors if "soc_slack" in v
    )
    peak_reserve_shortfall_kwh = sum(
        float(values[v["peak_slack"]]) for v in vectors if "peak_slack" in v
    )
    cap_violation_kwh = sum(
        float(values[v[name]])
        for v in vectors
        for name in ("cap_import", "cap_export")
        if name in v
    )
    peak_import_kw = (
        float(values[vectors[0]["peak_import"]]) if "peak_import" in vectors[0] else 0.0
    )
    episodes, reserve = validate_export_benefit(problem, vectors, values, settled)
    charge_episodes, charge_reserve = validate_charge_benefit(problem, vectors, values)
    return FlexibleLoadPlan(
        Plan(
            flows,
            objective,
            grid_cost,
            wear_cost,
            terminal_credit,
            episodes,
            reserve,
            charge_episodes,
            charge_reserve,
            autonomy_shortfall_kwh,
            cap_violation_kwh,
            peak_import_kw,
            balance_start=balance_start,
            balance_missed=bool(problem.balance_windows) and balance_start is None,
            peak_reserve_shortfall_kwh=peak_reserve_shortfall_kwh,
            time_limited=model.time_limited,
            mip_gap=model.mip_gap,
            carried_bridges=bridges,
            released_holds=tuple(sorted(released)),
        ),
        probe,
    )
