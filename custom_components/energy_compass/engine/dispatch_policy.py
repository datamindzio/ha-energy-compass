"""Physical operating modes and elapsed-time advice commitments."""

from datetime import UTC, timedelta

import numpy as np

from .charge_price import price_allows_grid_charge
from .idle_drain import drain_schedule
from .machine_state import machine_state
from .models import Problem, SolveError

MODES = (
    "CHARGE_GRID",
    "CHARGE_PV",
    "DISCHARGE_GRID",
    "SELF_CONSUME",
    "HOLD",
    "CURTAIL",
)
ACTIVE = frozenset(MODES[:4])
# Import this small is solver noise, not a plan.
_IMPORT_TOLERANCE = 1e-5
_EMPTY_TOLERANCE = 1e-4


# CHARGE_PV and SELF_CONSUME both let the inverter follow PV: surplus charges
# the battery and a deficit is drawn from it. A controller writes the same
# registers for both, so a flip between them is not a mode change to protect.
_DWELL_GROUPS = {"CHARGE_PV": "PV_FOLLOW", "SELF_CONSUME": "PV_FOLLOW"}


def dwell_group(mode):
    """Name the physical state whose minimum duration a mode shares."""
    return _DWELL_GROUPS.get(mode, mode)


GROUPS = tuple(dict.fromkeys(dwell_group(mode) for mode in MODES))


def _members(group):
    return tuple(mode for mode in MODES if dwell_group(mode) == group)


def safety_exception(problem: Problem) -> dict | None:
    """Pause an active lock at an observed SOC bound or conflicting price ceiling."""
    battery = problem.battery
    mode = problem.initial_dispatch_mode
    if not battery or not problem.minimum_mode_minutes or mode not in ACTIVE:
        return None
    deadline = problem.initial_dispatch_mode_since.astimezone(UTC) + timedelta(
        minutes=problem.minimum_mode_minutes
    )
    if problem.slots[0].start.astimezone(UTC) >= deadline:
        return None
    charging = mode in ("CHARGE_GRID", "CHARGE_PV")
    bound = battery.capacity_kwh * (
        battery.maximum_soc_fraction if charging else battery.minimum_soc_fraction
    )
    reason = None
    if (
        battery.initial_kwh >= bound - 1e-9
        if charging
        else battery.initial_kwh <= bound + 1e-9
    ):
        reason = "observed_soc_maximum" if charging else "observed_soc_minimum"
    elif mode == "CHARGE_GRID" and any(
        not price_allows_grid_charge(problem, slot)
        for slot in problem.slots
        if slot.start.astimezone(UTC) < deadline
    ):
        reason = "grid_charge_price_limit"
    if reason is None:
        return None
    return {
        "reason": reason,
        "interrupted_mode": mode,
        "deadline": deadline.isoformat(),
    }


def _initial(problem):
    exception = safety_exception(problem)
    return (
        "HOLD" if exception else problem.initial_dispatch_mode
    ), problem.initial_dispatch_mode_since


def _follow_load(model, problem, v, x, slot, hours, flex_max, can_empty) -> None:
    """Make SELF_CONSUME discharge what the house draws, up to what the battery gives.

    The inverter discharges whatever the house draws, so a partial discharge
    next to grid import is not a state a controller can hold. Import needs the
    discharge at its interval power limit or the battery emptied to its reserve.
    Written on the discharge, not on the import: the bound is the (small) load,
    not the (large) grid connection, which keeps the relaxation tight.
    """
    battery = problem.battery
    power_limit = min(
        model.upper[v["bd"]], problem.site.inverter_kw * hours - slot.pv_kwh
    )
    # Signed: PV above the house lowers what a flexible load asks of the battery.
    demand_max = slot.load_kwh - slot.pv_kwh + flex_max
    reserve = battery.capacity_kwh * battery.minimum_soc_fraction
    spare = model.upper[v["energy"]] - reserve
    x_sc = x["SELF_CONSUME"]
    # `relief` is the share of the obligation an emptied battery is excused from.
    relief = {}
    if can_empty and spare > 0:
        empty = v["self_consume_empty"] = model.variable(binary=True)
        # Any discharge ends at or above the reserve, so a battery that ends on
        # it has just been emptied.
        model.constrain(
            {v["energy"]: 1, empty: spare}, -np.inf, model.upper[v["energy"]]
        )
        relief[empty] = 1
    flexible = flex_max > 0
    flex = {v["flex_load"]: -1} if flexible else {}
    if power_limit >= demand_max - 1e-9:
        # Discharge reaches the load before its own limit: cover all of it.
        model.constrain(
            {v["bd"]: 1, **flex, x_sc: -demand_max, **{e: demand_max for e in relief}},
            -flex_max,
            np.inf,
        )
        coupled = {**relief}
    elif not flexible:
        # The limit binds: discharge it all.
        model.constrain(
            {v["bd"]: 1, x_sc: -power_limit, **{e: power_limit for e in relief}},
            0,
            np.inf,
        )
        coupled = {**relief}
    else:
        # Either the limit binds or the (variable) load is covered.
        power = v["self_consume_power"] = model.variable(binary=True)
        model.constrain(
            {v["bd"]: 1, power: -power_limit, **{e: power_limit for e in relief}},
            0,
            np.inf,
        )
        model.constrain(
            {
                v["bd"]: 1,
                **flex,
                x_sc: -demand_max,
                power: demand_max,
                **{e: demand_max for e in relief},
            },
            -flex_max,
            np.inf,
        )
        coupled = {**relief, power: 1}
    if coupled:
        model.constrain({**coupled, x_sc: -1}, -np.inf, 0)


def constrain_modes(model, problem: Problem, vectors, *, flexible_load=False) -> None:
    """Bind one named state to material flows and forbid premature transitions."""
    if not problem.battery or not problem.minimum_mode_minutes:
        return
    battery = problem.battery
    dwell = timedelta(minutes=problem.minimum_mode_minutes)
    starts = [s.start.astimezone(UTC) for s in problem.slots]
    coverage_end = problem.slots[-1].end.astimezone(UTC)
    previous, since = _initial(problem)
    drains = drain_schedule(problem)
    lowest = battery.initial_kwh
    for slot, v, drain in zip(problem.slots, vectors, drains, strict=True):
        hours = (
            slot.end.astimezone(UTC) - slot.start.astimezone(UTC)
        ).total_seconds() / 3600
        activity = problem.minimum_mode_power_kw * hours
        surplus = max(slot.pv_kwh - slot.load_kwh, 0)
        extra_load_max = model.upper[v["flex_load"]] if flexible_load else 0
        charge_max = min(
            model.upper[v["bc"]],
            slot.pv_kwh + problem.site.inverter_kw * hours,
            surplus + model.upper[v["gin"]],
        )
        discharge_max = max(
            0,
            min(
                model.upper[v["bd"]],
                problem.site.inverter_kw * hours - slot.pv_kwh,
                slot.load_kwh + extra_load_max - slot.pv_kwh + model.upper[v["gout"]],
            ),
        )
        pv_max = min(charge_max, surplus)
        self_max = min(
            discharge_max,
            max(slot.load_kwh + extra_load_max - slot.pv_kwh, 0),
        )
        curt_max = model.upper[v["curt"]]
        available = {
            "CHARGE_GRID": battery.allow_grid_charge
            and price_allows_grid_charge(problem, slot)
            and charge_max >= surplus + activity,
            "CHARGE_PV": pv_max >= activity,
            "DISCHARGE_GRID": battery.allow_battery_export
            and discharge_max >= activity
            and model.upper[v["gout"]] >= activity,
            "SELF_CONSUME": self_max >= activity,
            "HOLD": True,
            "CURTAIL": curt_max >= activity,
        }
        for mode in MODES:
            key = f"mode_{mode}"
            v[key] = model.variable(binary=True)
            # Below classifier resolution a named material state cannot be certified.
            if not available[mode] or (mode != "HOLD" and activity <= 2e-6):
                model.upper[v[key]] = 0
        x = {mode: v[f"mode_{mode}"] for mode in MODES}
        model.constrain({index: 1 for index in x.values()}, 1, 1)
        # Named modes already determine flow direction. Tie the auxiliary flags
        # to them so fractional relaxations cannot invent grid arbitrage.
        model.constrain(
            {v["battery_mode"]: 1, x["CHARGE_GRID"]: -1, x["CHARGE_PV"]: -1},
            0,
            0,
        )
        grid_direction = {
            v["grid_mode"]: 1,
            x["CHARGE_GRID"]: -1,
            x["SELF_CONSUME"]: -1,
        }
        if slot.load_kwh >= slot.pv_kwh:
            grid_direction[x["HOLD"]] = -1
        # Curtailment may put net demand on either side of zero.
        if not flexible_load:
            model.constrain(grid_direction, 0, np.inf)
            model.constrain({**grid_direction, x["CURTAIL"]: -1}, -np.inf, 0)
        model.constrain(
            {v["bc"]: 1, x["CHARGE_GRID"]: -charge_max, x["CHARGE_PV"]: -pv_max},
            -np.inf,
            0,
        )
        if flexible_load:
            model.constrain(
                {
                    v["bc"]: 1,
                    x["CHARGE_GRID"]: -activity,
                    x["CHARGE_PV"]: -activity,
                },
                0,
                np.inf,
            )
            if surplus:
                model.constrain(
                    {
                        v["bc"]: 1,
                        v["flex_load"]: 1,
                        x["CHARGE_GRID"]: -(surplus + activity),
                    },
                    0,
                    np.inf,
                )
                charge_source_max = model.upper[v["bc"]] + model.upper[v["flex_load"]]
                model.constrain(
                    {
                        v["bc"]: 1,
                        v["flex_load"]: 1,
                        x["CHARGE_PV"]: charge_source_max,
                    },
                    -np.inf,
                    surplus + charge_source_max,
                )
        else:
            model.constrain(
                {
                    v["bc"]: 1,
                    x["CHARGE_GRID"]: -(surplus + activity),
                    x["CHARGE_PV"]: -activity,
                },
                0,
                np.inf,
            )
        model.constrain(
            {
                v["bd"]: 1,
                x["DISCHARGE_GRID"]: -discharge_max,
                x["SELF_CONSUME"]: -self_max,
            },
            -np.inf,
            0,
        )
        model.constrain(
            {v["bd"]: 1, x["DISCHARGE_GRID"]: -activity, x["SELF_CONSUME"]: -activity},
            0,
            np.inf,
        )
        model.constrain({v["curt"]: 1, x["CURTAIL"]: -curt_max}, -np.inf, 0)
        model.constrain({v["curt"]: 1, x["CURTAIL"]: -activity}, 0, np.inf)
        model.constrain({v["gout"]: 1, x["DISCHARGE_GRID"]: -activity}, 0, np.inf)
        model.constrain(
            {v["gout"]: 1, x["SELF_CONSUME"]: model.upper[v["gout"]]},
            -np.inf,
            model.upper[v["gout"]],
        )
        # Even discharging at full power from the start, the pack may not reach
        # its reserve yet; then it cannot be emptied in this interval.
        lowest -= model.upper[v["bd"]] / battery.eta_discharge + drain
        if available["SELF_CONSUME"]:
            can_empty = lowest <= battery.capacity_kwh * battery.minimum_soc_fraction
            _follow_load(model, problem, v, x, slot, hours, extra_load_max, can_empty)
    if problem.initial_battery_mode is not None:
        held_until = problem.initial_battery_mode_since.astimezone(UTC) + dwell
        for start, v in zip(starts, vectors, strict=True):
            if start < held_until:
                blocked = "bd" if problem.initial_battery_mode == "charge" else "bc"
                model.constrain({v[blocked]: 1}, 0, 0)
    if previous is not None:
        held_until = since.astimezone(UTC) + dwell
        for start, v in zip(starts, vectors, strict=True):
            if start < held_until:
                model.constrain(
                    {v[f"mode_{mode}"]: 1 for mode in _members(dwell_group(previous))},
                    1,
                    1,
                )
    for i, v in enumerate(vectors):
        for group in GROUPS:
            members = _members(group)
            # Modes are one-hot, so a group's member sum is its binary indicator.
            current = {v[f"mode_{mode}"]: 1 for mode in members}
            prior = (
                {vectors[i - 1][f"mode_{mode}"]: -1 for mode in members} if i else {}
            )
            initial = int(previous is not None and dwell_group(previous) == group)
            if ACTIVE.intersection(members) and coverage_end < starts[i] + dwell:
                model.constrain({**current, **prior}, -np.inf, initial if i == 0 else 0)
            for j in range(i + 1, len(vectors)):
                if starts[j] >= starts[i] + dwell:
                    break
                following = {vectors[j][f"mode_{mode}"]: -1 for mode in members}
                model.constrain(
                    {**current, **following, **prior},
                    -np.inf,
                    initial if i == 0 else 0,
                )


def validate_modes(problem: Problem, flows) -> None:
    """Independently certify labels, activity, coverage and carried commitments."""
    if not problem.battery or not problem.minimum_mode_minutes:
        return
    previous, since = _initial(problem)
    dwell = timedelta(minutes=problem.minimum_mode_minutes)
    coverage_end = problem.slots[-1].end.astimezone(UTC)
    for slot, flow in zip(problem.slots, flows, strict=True):
        start = slot.start.astimezone(UTC)
        activity = (
            problem.minimum_mode_power_kw
            * (slot.end.astimezone(UTC) - start).total_seconds()
            / 3600
        )
        mode = machine_state(slot, flow)
        tolerance = min(1e-7, activity * 1e-4)
        charge, discharge, curt, export = (
            flow.charge_kwh,
            flow.discharge_kwh,
            flow.curtail_kwh,
            flow.grid_export_kwh,
        )
        surplus = max(slot.pv_kwh - slot.load_kwh, 0)
        valid = {
            "CHARGE_GRID": charge >= surplus + activity - tolerance
            and max(abs(discharge), abs(curt)) <= tolerance,
            "CHARGE_PV": activity - tolerance <= charge <= surplus + tolerance
            and max(abs(discharge), abs(curt)) <= tolerance,
            "DISCHARGE_GRID": min(discharge, export) >= activity - tolerance
            and max(abs(charge), abs(curt)) <= tolerance,
            "SELF_CONSUME": discharge >= activity - tolerance
            and max(abs(charge), abs(export), abs(curt)) <= tolerance,
            "HOLD": max(abs(charge), abs(discharge), abs(curt)) <= tolerance,
            "CURTAIL": curt >= activity - tolerance
            and max(abs(charge), abs(discharge)) <= tolerance,
        }
        if flow.dispatch_mode != mode or not valid[mode]:
            raise SolveError(
                "solver_failure", "invalid solver result: physical dispatch mode"
            )
        if mode == "SELF_CONSUME" and flow.grid_import_kwh > _IMPORT_TOLERANCE:
            hours = (slot.end.astimezone(UTC) - start).total_seconds() / 3600
            power_limit = min(
                problem.battery.discharge_kw * hours,
                problem.site.inverter_kw * hours - slot.pv_kwh,
            )
            reserve = (
                problem.battery.capacity_kwh * problem.battery.minimum_soc_fraction
            )
            if (
                discharge < power_limit - _IMPORT_TOLERANCE
                and flow.end_soc_kwh > reserve + _EMPTY_TOLERANCE
            ):
                raise SolveError(
                    "solver_failure",
                    "invalid solver result: self-consume imports below its limit",
                )
        if (
            problem.initial_battery_mode is not None
            and start < problem.initial_battery_mode_since.astimezone(UTC) + dwell
        ):
            blocked = discharge if problem.initial_battery_mode == "charge" else charge
            if abs(blocked) > tolerance:
                raise SolveError(
                    "solver_failure", "invalid solver result: legacy direction guard"
                )
        if dwell_group(mode) != dwell_group(previous):
            if previous is not None and start < since.astimezone(UTC) + dwell:
                raise SolveError(
                    "solver_failure", "invalid solver result: minimum mode duration"
                )
            if mode in ACTIVE and coverage_end < start + dwell:
                raise SolveError(
                    "solver_failure",
                    "invalid solver result: active mode beyond coverage",
                )
            since = start
        previous = mode
