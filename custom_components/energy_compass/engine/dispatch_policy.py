"""Physical operating modes and elapsed-time advice commitments."""

import math
from dataclasses import replace
from datetime import UTC, timedelta

import numpy as np

from .charge_price import price_allows_grid_charge
from .idle_drain import cumulative, drain_schedule
from .machine_state import _TOL as _CLASSIFIER_TOLERANCE
from .machine_state import activity_tolerance, machine_state
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
# The flows (by solver variable) each named mode leaves at zero: the rows
# `validate_modes` certifies, and the ones `mode_noise` zeroes when the solver leaves
# noise on them.
IDLE_FLOWS = {
    "CHARGE_GRID": ("bd", "curt"),
    "CHARGE_PV": ("bd", "curt"),
    "DISCHARGE_GRID": ("bc", "curt"),
    "SELF_CONSUME": ("bc", "gout", "curt"),
    "HOLD": ("bc", "bd", "curt"),
    "CURTAIL": ("bc", "bd"),
}
# Import this small is solver noise, not a plan.
_IMPORT_TOLERANCE = 1e-5
_EMPTY_TOLERANCE = 1e-4
# A bridge's realised probe stays this far below the activity that would open the slot.
_PROBE_MARGIN = 1e-5


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


def mode_run_start(rows, current, bridges=()) -> int:
    """Find the first row of the run of equal dispatch modes that holds a row.

    A bridge row (HOLD with nothing for a carried PV-follow commitment to follow)
    is not part of the HOLD run after it: that run's minimum duration starts at
    its own first row, and the bridge row stays part of the commitment.
    """
    start = current
    while (
        start
        and rows[start - 1].get("dispatch_mode") == rows[current].get("dispatch_mode")
        and rows[start - 1]["start"] not in bridges
    ):
        start -= 1
    return start


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


def _hours(slot) -> float:
    return (
        slot.end.astimezone(UTC) - slot.start.astimezone(UTC)
    ).total_seconds() / 3600


def _guarded(problem, slot) -> str | None:
    """Name the battery flow the legacy direction guard forbids in a slot, if any.

    "bc" while a stored `discharge` holds, "bd" while a stored `charge` does. A
    direction the guard blocks is not open: neither CHARGE_PV nor SELF_CONSUME
    exists there, whatever the slot offers.
    """
    if problem.initial_battery_mode is None:
        return None
    held_until = problem.initial_battery_mode_since.astimezone(UTC) + timedelta(
        minutes=problem.minimum_mode_minutes
    )
    if slot.start.astimezone(UTC) >= held_until:
        return None
    return "bd" if problem.initial_battery_mode == "charge" else "bc"


def _surplus_bar(problem, slot) -> float:
    """Return the probe load that takes a chargeable surplus below the minimum activity."""
    activity = problem.minimum_mode_power_kw * _hours(slot)
    return max(slot.pv_kwh - slot.load_kwh - activity, 0.0) + _PROBE_MARGIN


def _takes_surplus(problem, slot) -> bool:
    """Tell whether CHARGE_PV is open: the pack can take its activity from PV surplus."""
    hours = _hours(slot)
    activity = problem.minimum_mode_power_kw * hours
    return (
        activity > 2e-6
        and _guarded(problem, slot) != "bc"
        and min(problem.battery.charge_kw * hours, slot.pv_kwh - slot.load_kwh)
        >= activity
    )


def _follow_open(problem, slot) -> bool:
    """Tell whether SELF_CONSUME or CHARGE_PV can reach the minimum activity in a slot.

    The same bounds `constrain_modes` puts on the two modes, from the problem:
    SELF_CONSUME needs a net load the pack can cover, CHARGE_PV a surplus it can
    take. A direction the legacy guard blocks is not open.
    """
    battery, site = problem.battery, problem.site
    hours = _hours(slot)
    activity = problem.minimum_mode_power_kw * hours
    if activity <= 2e-6:
        return False
    guarded = _guarded(problem, slot)
    surplus = max(slot.pv_kwh - slot.load_kwh, 0)
    charge_max = (
        0
        if guarded == "bc"
        else min(
            battery.charge_kw * hours,
            slot.pv_kwh + site.inverter_kw * hours,
            surplus + site.grid_import_kw * hours,
        )
    )
    net_load = max(slot.load_kwh - slot.pv_kwh, 0)
    discharge_max = (
        0
        if guarded == "bd"
        else max(
            0,
            min(
                battery.discharge_kw * hours,
                site.inverter_kw * hours - slot.pv_kwh,
                slot.load_kwh - slot.pv_kwh + site.grid_export_kw * hours,
            ),
        )
    )
    return (
        min(charge_max, surplus) >= activity or min(discharge_max, net_load) >= activity
    )


def probe_room(problem, slot) -> float:
    """Tell how much flexible load an idle slot takes before SELF_CONSUME opens in it.

    `_follow_open` for a slot that is idle without a probe, as a function of the
    load the probe adds: the net load grows with it, so SELF_CONSUME opens once
    that reaches the minimum activity, unless the pack's own limits (guard,
    discharge power, inverter) keep it below. CHARGE_PV never opens, a probe only
    shrinks the surplus. Returns inf when no probe can open the slot.
    """
    battery, site = problem.battery, problem.site
    hours = _hours(slot)
    activity = problem.minimum_mode_power_kw * hours
    if (
        _guarded(problem, slot) == "bd"
        or min(battery.discharge_kw * hours, site.inverter_kw * hours - slot.pv_kwh)
        < activity
    ):
        return math.inf
    return max(activity - (slot.load_kwh - slot.pv_kwh), 0.0)


def idle_slots(problem) -> frozenset[int]:
    """List the committed intervals in which a carried PV-follow commitment has nothing to follow.

    Net load and PV surplus are both below the minimum activity, so neither
    SELF_CONSUME nor CHARGE_PV exists there and the flows are HOLD by
    classification. That is not leaving the commitment: it neither needs a
    release nor starts a HOLD minimum duration.
    """
    if not problem.battery or not problem.minimum_mode_minutes:
        return frozenset()
    previous, since = _initial(problem)
    if dwell_group(previous) != "PV_FOLLOW":
        return frozenset()
    held_until = since.astimezone(UTC) + timedelta(minutes=problem.minimum_mode_minutes)
    return frozenset(
        i
        for i, slot in enumerate(problem.slots)
        if slot.start.astimezone(UTC) < held_until and not _follow_open(problem, slot)
    )


def _owed(problem, drains, held_until) -> tuple[float, ...]:
    """Energy each slot must end with so the standby loss of later slots stays payable.

    Best case for the pack: every later slot charges all it may, from PV surplus
    and, outside the carried commitment, from the grid where grid charging is
    open. Ending a slot below this value cannot stay above the relaxed reserve
    floor later whatever the plan does, so it is a bound the energy rows already
    imply, not a new requirement.
    """
    battery = problem.battery
    reserve = min(
        battery.capacity_kwh * battery.minimum_soc_fraction, battery.initial_kwh
    )
    drained = cumulative(drains)
    owed = []
    later = 0.0
    for slot, drain, lost in zip(
        reversed(problem.slots), reversed(drains), reversed(drained), strict=True
    ):
        hours = _hours(slot)
        owed.append(max(reserve - lost, later, 0.0))
        charge = max(slot.pv_kwh - slot.load_kwh, 0)
        if (
            battery.allow_grid_charge
            and slot.start.astimezone(UTC) >= held_until
            and price_allows_grid_charge(problem, slot)
        ):
            charge += problem.site.grid_import_kw * hours
        later = (
            owed[-1]
            + drain
            - battery.eta_charge * min(battery.charge_kw * hours, charge)
        )
    return tuple(reversed(owed))


def _follow_surplus(model, v, x, surplus, flex_max, can_fill) -> None:
    """Make CHARGE_PV charge what PV leaves over, up to what the battery takes.

    The inverter charges with every watt the house does not use, so a partial
    charge next to grid export is not a state a controller can hold. Export needs
    the charge at its interval power limit or the battery filled to its ceiling.
    Written on the charge, not on the export, for the same tight relaxation as
    `_follow_load`.
    """
    limit = model.upper[v["bc"]]
    ceiling = model.upper[v["energy"]]
    floor = model.lower[v["energy"]]
    x_pv = x["CHARGE_PV"]
    # `relief` is the share of the obligation a filled battery is excused from.
    relief = {}
    if can_fill and ceiling > floor:
        full = v["charge_pv_full"] = model.variable(binary=True)
        # Any charge ends at or below the ceiling, so a battery that ends on
        # it has just been filled.
        model.constrain({v["energy"]: 1, full: floor - ceiling}, floor, np.inf)
        relief[full] = 1
    flexible = flex_max > 0
    # PV above the house, net of a flexible load, is what a charge is owed.
    flex = {v["flex_load"]: 1} if flexible else {}
    if limit >= surplus - 1e-9:
        # Surplus runs out before the power limit: charge all of it.
        model.constrain(
            {v["bc"]: 1, **flex, x_pv: -surplus, **{f: surplus for f in relief}},
            0,
            np.inf,
        )
    elif not flexible:
        # The limit binds: charge it all.
        model.constrain(
            {v["bc"]: 1, x_pv: -limit, **{f: limit for f in relief}}, 0, np.inf
        )
    else:
        # Either the limit binds or the (variable) surplus is taken.
        power = v["charge_pv_power"] = model.variable(binary=True)
        model.constrain(
            {v["bc"]: 1, power: -limit, **{f: limit for f in relief}}, 0, np.inf
        )
        model.constrain(
            {
                v["bc"]: 1,
                **flex,
                x_pv: -surplus,
                power: surplus,
                **{f: surplus for f in relief},
            },
            0,
            np.inf,
        )


def _lock_floor(problem, drains, owed, index) -> float:
    """Energy below which the battery cannot keep SELF_CONSUME active in a slot."""
    battery = problem.battery
    slot = problem.slots[index]
    hours = _hours(slot)
    keep = battery.capacity_kwh * battery.minimum_soc_fraction
    discharge = problem.minimum_mode_power_kw * hours
    if owed[index] > keep + 1e-9:
        # Later standby loss needs more than the reserve, so the pack cannot end
        # on it: SELF_CONSUME has to cover the house or reach its power limit.
        keep = owed[index]
        discharge = max(
            discharge,
            min(
                slot.load_kwh - slot.pv_kwh,
                battery.discharge_kw * hours,
                problem.site.inverter_kw * hours - slot.pv_kwh,
            ),
        )
    return keep + drains[index] + discharge / battery.eta_discharge


def _curtail_need(problem, slot, extra_load=0.0) -> float:
    """Energy a slot must send to the pack or curtail, because the site cannot pass it.

    PV above the house, a flexible load of at most `extra_load` and the export cap
    leaves `surplus - export cap`, and PV above the inverter rating needs the same
    from the pack or the curtailment. HOLD takes none of it, CHARGE_PV all of it.
    """
    hours = _hours(slot)
    return max(
        slot.pv_kwh - slot.load_kwh - extra_load - problem.site.grid_export_kw * hours,
        slot.pv_kwh - problem.site.inverter_kw * hours,
        0.0,
    )


def _lock_ceiling(problem, drains, index, extra_load=0.0) -> float:
    """Energy above which the battery cannot keep CHARGE_PV active in a slot.

    CHARGE_PV is kept by its minimum activity, or by what the site cannot pass
    (`_curtail_need`) when that is more: a pack with less room cannot charge the
    interval and stay below the export cap or the inverter rating.
    """
    battery = problem.battery
    slot = problem.slots[index]
    hours = _hours(slot)
    absorbed = max(
        problem.minimum_mode_power_kw * hours, _curtail_need(problem, slot, extra_load)
    )
    return (
        battery.capacity_kwh
        * (1 if index in problem.balance_lift_slots else battery.maximum_soc_fraction)
        + drains[index]
        - absorbed * battery.eta_charge
    )


def _curtail_dry(problem, slot) -> bool:
    """Tell whether a slot has too little PV to curtail the minimum activity."""
    activity = problem.minimum_mode_power_kw * _hours(slot)
    return activity > 2e-6 and slot.pv_kwh < activity


def _follow_load_unmoded(model, problem, vectors, *, flexible_load) -> None:
    """Apply the load-following rule without the mode model.

    With no minimum duration there are no mode binaries, but the flows are still
    labelled SELF_CONSUME and a controller still holds the full discharge cap,
    so a partial discharge next to import stays unexecutable. One binary per
    interval stands for "the battery discharges"; it plays the role of the
    SELF_CONSUME indicator, and the rule on the discharge is the same.
    """
    battery = problem.battery
    reserve = battery.capacity_kwh * battery.minimum_soc_fraction
    lowest = battery.initial_kwh
    for slot, v, drain in zip(
        problem.slots, vectors, drain_schedule(problem), strict=True
    ):
        hours = (
            slot.end.astimezone(UTC) - slot.start.astimezone(UTC)
        ).total_seconds() / 3600
        discharge_max = model.upper[v["bd"]]
        lowest -= discharge_max / battery.eta_discharge + drain
        if discharge_max <= 0:
            continue
        discharging = v["discharging"] = model.variable(binary=True)
        model.constrain({v["bd"]: 1, discharging: -discharge_max}, -np.inf, 0)
        flex_max = model.upper[v["flex_load"]] if flexible_load else 0
        _follow_load(
            model,
            problem,
            v,
            {"SELF_CONSUME": discharging},
            slot,
            hours,
            flex_max,
            lowest <= reserve,
        )


def _offer_curtail(model, v, seen_full, full, curtailing, released) -> None:
    """Let a committed interval curtail: the pack has been full, and is full now or was let go.

    `seen_full` is the `lock_full` of every earlier interval; `full`, `curtailing`
    and `released` are the previous interval's: a pack at its ceiling, a CURTAIL
    under way, the HOLD released from the commitment.
    """
    model.constrain({v["mode_CURTAIL"]: 1, **seen_full}, -np.inf, 0)
    model.constrain(
        {
            v["mode_CURTAIL"]: 1,
            **full,
            **curtailing,
            **{key: -1 for key in released},
        },
        -np.inf,
        0,
    )


def constrain_modes(model, problem: Problem, vectors, *, flexible_load=False) -> None:
    """Bind one named state to material flows and forbid premature transitions."""
    if not problem.battery:
        return
    if not problem.minimum_mode_minutes:
        _follow_load_unmoded(model, problem, vectors, flexible_load=flexible_load)
        return
    battery = problem.battery
    dwell = timedelta(minutes=problem.minimum_mode_minutes)
    starts = [s.start.astimezone(UTC) for s in problem.slots]
    coverage_end = problem.slots[-1].end.astimezone(UTC)
    previous, since = _initial(problem)
    drains = drain_schedule(problem)
    lowest = battery.initial_kwh
    highest = battery.initial_kwh
    for slot, v, drain in zip(problem.slots, vectors, drains, strict=True):
        hours = (
            slot.end.astimezone(UTC) - slot.start.astimezone(UTC)
        ).total_seconds() / 3600
        activity = problem.minimum_mode_power_kw * hours
        surplus = max(slot.pv_kwh - slot.load_kwh, 0)
        extra_load_max = model.upper[v["flex_load"]] if flexible_load else 0
        # A direction the legacy guard blocks is not open: no mode may count on it.
        if (guarded := _guarded(problem, slot)) is not None:
            model.upper[v[guarded]] = 0
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
            # HOLD is the remainder of the one-hot row below, so it is integral whenever
            # the other modes are; leaving it continuous spares the solver a branching
            # variable without changing the feasible set.
            v[key] = (
                model.variable(upper=1.0)
                if mode == "HOLD"
                else model.variable(binary=True)
            )
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
        highest = min(
            model.upper[v["energy"]],
            highest + battery.eta_charge * model.upper[v["bc"]] - drain,
        )
        if available["CHARGE_PV"]:
            can_fill = highest >= model.upper[v["energy"]] - 1e-9
            _follow_surplus(model, v, x, surplus, extra_load_max, can_fill)
    if problem.initial_battery_mode is not None:
        held_until = problem.initial_battery_mode_since.astimezone(UTC) + dwell
        for start, v in zip(starts, vectors, strict=True):
            if start < held_until:
                blocked = "bd" if problem.initial_battery_mode == "charge" else "bc"
                model.constrain({v[blocked]: 1}, 0, 0)
    idle = idle_slots(problem)
    # A CURTAIL run needs PV to curtail, and PV may drop inside its minimum duration.
    # A HOLD in an interval with too little PV for the minimum activity, inside the
    # duration of a CURTAIL run, is that run going on: the inverter has nothing to
    # curtail and the pack and the grid do what HOLD does. It counts for the CURTAIL
    # clock, starts no HOLD clock, and lasts only until the run's own duration is up.
    bridge = {}
    held_curtail = previous is not None and dwell_group(previous) == "CURTAIL"
    curtail_until = since.astimezone(UTC) + dwell if held_curtail else None

    def curtail_terms(j):
        terms = {vectors[j]["mode_CURTAIL"]: 1}
        if j in bridge:
            terms[bridge[j]] = 1
        return terms

    for i, (slot, v) in enumerate(zip(problem.slots, vectors, strict=True)):
        if not _curtail_dry(problem, slot):
            continue
        # Only where a CURTAIL run can be under way: carried in, or begun by an
        # interval whose PV the cap or the inverter cannot pass, within its minimum
        # duration (a CURTAIL chosen for its price can always be left out).
        carried_run = held_curtail and starts[i] < curtail_until
        if not carried_run and not any(
            starts[i] < starts[j] + dwell
            and _curtail_need(problem, problem.slots[j]) > 0
            for j in range(i)
        ):
            continue
        run = bridge[i] = v["curtail_bridge"] = model.variable(binary=True)
        model.constrain({run: 1, v["mode_HOLD"]: -1}, -np.inf, 0)
        if i:
            model.constrain(
                {run: 1, **{key: -1 for key in curtail_terms(i - 1)}}, -np.inf, 0
            )
        if held_curtail and starts[i] < curtail_until:
            continue
        # Only while the run that this continues began inside its duration: one of the
        # intervals that precede that start must not be CURTAIL or a bridge.
        window = [j for j in range(-1, i) if starts[i] < starts[j + 1] + dwell]
        terms = {}
        for j in window:
            for key in curtail_terms(j) if j >= 0 else {}:
                terms[key] = terms.get(key, 0) + 1
        model.constrain(
            {run: 1, **terms},
            -np.inf,
            len(window) - int(held_curtail and -1 in window),
        )
    # Per idle committed interval, the binary that tells a real HOLD run from a bridge.
    runs = {}
    # Intervals whose HOLD started from a carried PV-follow commitment: no clock of its own.
    free_hold = set()
    if previous is not None:
        held_until = since.astimezone(UTC) + dwell
        members = _members(dwell_group(previous))
        follows_pv = "SELF_CONSUME" in members
        owed = _owed(problem, drains, held_until) if follows_pv else ()
        # A battery too empty or too full to go on ends a carried PV-follow commitment
        # the way an observed SOC bound does: the inverter imports for the house or
        # exports what the pack cannot take, which is HOLD. A pack too empty is known
        # up front in the first slot; with PV surplus to charge from the inverter
        # still has something to follow.
        pack_spent = (
            follows_pv
            and battery.initial_kwh <= _lock_floor(problem, drains, owed, 0) + 1e-9
        )
        spent = pack_spent and not _takes_surplus(problem, problem.slots[0])
        # What lets a committed interval be HOLD: a HOLD run that began earlier goes on
        # (`held`, the previous interval's HOLD with its sign flipped), the pack is too
        # empty to go on and HOLD starts where CHARGE_PV is not open (`depleted`; the
        # constant `spent` is the first interval's test), or it is too full (`full`).
        held = {}
        depleted = {}
        full = {}
        # A full pack takes no more PV, and the inverter curtails what the site cannot
        # pass: an interval that starts at the ceiling may be CURTAIL as well (`full`).
        # So may the one after a released HOLD, in a commitment whose pack has been full
        # (`seen_full`, the `lock_full` of every earlier interval), and a CURTAIL under
        # way goes on (`curtailing`). A pack with room again does not curtail.
        seen_full = {}
        curtailing = {}
        # The HOLD or the HOLD run released from the commitment, in the previous interval.
        released = {}
        if "CHARGE_PV" in members and (flexible_load or 0 not in idle):
            # The observed pack may already be at the first interval's ceiling: there
            # is no earlier interval to leave it full, so the release is a constant.
            # An idle first interval needs none, its HOLD is a bridge (a probe may
            # still make it a real HOLD run, which does).
            extra = model.upper[vectors[0]["flex_load"]] if flexible_load else 0.0
            ceiling = _lock_ceiling(problem, drains, 0, extra)
            if battery.initial_kwh >= ceiling - _EMPTY_TOLERANCE:
                at_start = model.variable(upper=1.0)
                model.lower[at_start] = 1.0
                full[at_start] = -1
                seen_full[at_start] = -1
        for i, (slot, start, v) in enumerate(
            zip(problem.slots, starts, vectors, strict=True)
        ):
            if start >= held_until:
                break
            chargeable = _takes_surplus(problem, slot)
            # The pack is known to be spent: HOLD starts, with nothing to test.
            known = spent and not i
            releases = {**full, **({} if chargeable else depleted)}
            if flexible_load and chargeable:
                # The surplus is chargeable as realised: a probe may take it below the
                # minimum activity, which is what the plan is certified against.
                open_surplus = model.variable(binary=True)
                bar = _surplus_bar(problem, slot)
                model.constrain({v["flex_load"]: 1, open_surplus: bar}, bar, np.inf)
                releases = dict(full)
                if pack_spent if not i else depleted:
                    # Depleted and not chargeable as realised: HOLD may start.
                    release = model.variable(binary=True)
                    model.constrain({release: 1, open_surplus: 1}, -np.inf, 1)
                    if i:
                        model.constrain({release: 1, **depleted}, -np.inf, 0)
                    releases[release] = -1
            # An idle interval is CURTAIL on the terms of any committed interval: nothing
            # to follow does not make room in a full pack. A CURTAIL row is not a bridge.
            curtails = bool(seen_full) and model.upper[v["mode_CURTAIL"]] > 0
            if i in idle and (flexible_load or curtails):
                # Idle without a probe. A bridge needs it idle as realised too: the probe
                # may not put the slot's net load at the activity, or SELF_CONSUME would
                # be open. Any other row (`run` set) is a normal committed interval.
                run = runs[i] = v["hold_run"] = model.variable(binary=True)
                if i in bridge:
                    # A CURTAIL run going on is neither a HOLD run nor the commitment.
                    model.constrain({run: 1, bridge[i]: 1}, -np.inf, 1)
                top = model.upper[v["flex_load"]] if flexible_load else 0
                room = probe_room(problem, slot) - _PROBE_MARGIN
                if not _follow_open(
                    problem, replace(slot, load_kwh=slot.load_kwh + top)
                ):
                    # Idle with the largest probe too, by the reading the plan is
                    # certified against: a bridge whatever the probe, without a margin.
                    pass
                elif room < 0:
                    # Idle by less than solver noise: whether the realised load leaves
                    # it idle is not decided, so it cannot be a bridge.
                    model.constrain({run: 1}, 1, 1)
                elif room < top:
                    model.constrain(
                        {v["flex_load"]: 1, run: -(top - room)}, -np.inf, room
                    )
                model.constrain({run: 1, **held}, 0, np.inf)
                model.constrain({v["mode_HOLD"]: 1, run: 1}, 1, np.inf)
                # Without a probe the slot stays idle: no member of the commitment.
                kept = {
                    v[f"mode_{mode}"]: 1
                    for mode in (*(members if flexible_load else ()), "HOLD")
                }
                if curtails:
                    _offer_curtail(model, v, seen_full, full, curtailing, released)
                    kept[v["mode_CURTAIL"]] = 1
                model.constrain(kept, 1, 1)
                if not known:
                    model.constrain(
                        {v["mode_HOLD"]: 1, run: 1, **held, **releases}, -np.inf, 1
                    )
                # A real HOLD run is the HOLD row that is not a bridge.
                real = v["hold_real"] = model.variable(upper=1.0)
                model.constrain({real: 1, v["mode_HOLD"]: -1}, -np.inf, 0)
                model.constrain({real: 1, run: -1}, -np.inf, 0)
                model.constrain({real: 1, v["mode_HOLD"]: -1, run: -1}, -1, np.inf)
                held = {real: -1}
                if follows_pv:
                    # As in any committed interval: a released HOLD does not go back
                    # to the commitment and starts no clock of its own.
                    if released:
                        model.constrain(
                            {**{v[f"mode_{mode}"]: 1 for mode in members}, **released},
                            -np.inf,
                            1,
                        )
                    free_hold.add(i)
                    released = {real: 1}
                curtailing = {v["mode_CURTAIL"]: -1} if curtails else {}
                if i in bridge:
                    curtailing[bridge[i]] = -1
            elif i in idle:
                # Nothing to follow: the flows are HOLD by classification. Either a HOLD
                # run is under way or starts here on a release, as in any committed
                # interval, or this row is a bridge and the commitment goes on.
                run = runs[i] = v["hold_run"] = model.variable(binary=True)
                if i in bridge:
                    model.constrain({run: 1, bridge[i]: 1}, -np.inf, 1)
                model.constrain({run: 1, **held}, 0, np.inf)
                if not known:
                    model.constrain({run: 1, **held, **releases}, -np.inf, 0)
                held = {run: -1}
                model.constrain({v["mode_HOLD"]: 1}, 1, 1)
                if follows_pv:
                    free_hold.add(i)
                    released = {run: 1}
                curtailing = {bridge[i]: -1} if i in bridge else {}
            else:
                # HOLD starts only on a release, and not on a PV surplus the pack
                # could take, but a run that began earlier goes on through it.
                kept = {v[f"mode_{mode}"]: 1 for mode in members}
                # A HOLD that goes on with a CURTAIL run is not leaving the commitment:
                # it needs no release, and it is not a HOLD run for the next interval.
                going_on = {bridge[i]: 1} if i in bridge else {}
                if known or held or releases or going_on:
                    if not known:
                        model.constrain(
                            {
                                v["mode_HOLD"]: 1,
                                **held,
                                **releases,
                                **{key: -1 for key in going_on},
                            },
                            -np.inf,
                            0,
                        )
                    held = {v["mode_HOLD"]: -1, **going_on}
                    kept[v["mode_HOLD"]] = 1
                if seen_full:
                    _offer_curtail(model, v, seen_full, full, curtailing, released)
                    kept[v["mode_CURTAIL"]] = 1
                    curtailing = {v["mode_CURTAIL"]: -1, **{k: -1 for k in going_on}}
                model.constrain(kept, 1, 1)
                if follows_pv:
                    # A released HOLD does not go back to the commitment (a released
                    # CURTAIL is held by its own minimum duration), and it starts no
                    # clock of its own: the commitment's deadline is the only one, so it
                    # may end in CURTAIL at once.
                    if released:
                        model.constrain(
                            {**{v[f"mode_{mode}"]: 1 for mode in members}, **released},
                            -np.inf,
                            1,
                        )
                    free_hold.add(i)
                    released = {v["mode_HOLD"]: 1}
            depleted = {}
            full = {}
            if follows_pv and i + 1 < len(vectors):
                empty = v["lock_depleted"] = model.variable(binary=True)
                slack = model.upper[v["energy"]] - _lock_floor(
                    problem, drains, owed, i + 1
                )
                if slack > 0:
                    model.constrain(
                        {v["energy"]: 1, empty: slack},
                        -np.inf,
                        model.upper[v["energy"]],
                    )
                depleted[empty] = -1
            if "CHARGE_PV" in members and i + 1 < len(vectors):
                filled = v["lock_full"] = model.variable(binary=True)
                extra = (
                    model.upper[vectors[i + 1]["flex_load"]] if flexible_load else 0.0
                )
                slack = (
                    _lock_ceiling(problem, drains, i + 1, extra)
                    - model.lower[v["energy"]]
                )
                if slack > 0:
                    # `filled` may only be set once the pack reaches the next slot's
                    # ceiling less what its minimum activity, or the PV the site
                    # cannot pass, would still add.
                    model.constrain(
                        {v["energy"]: 1, filled: -slack},
                        model.lower[v["energy"]],
                        np.inf,
                    )
                full[filled] = -1
                seen_full[filled] = -1
    # A committed idle interval that is a bridge carries its neighbour's state: it
    # continues the PV-follow commitment and starts no HOLD clock.
    indicators = {group: [] for group in GROUPS}
    for i, v in enumerate(vectors):
        for group in GROUPS:
            # Modes are one-hot, so a group's member sum is its binary indicator.
            modes = {v[f"mode_{mode}"]: 1 for mode in _members(group)}
            if i not in runs:
                if i in bridge and group in ("HOLD", "CURTAIL"):
                    # HOLD with the CURTAIL run going on: CURTAIL's, not HOLD's.
                    modes = {**modes, bridge[i]: -1 if group == "HOLD" else 1}
                indicators[group].append((modes, 0))
            elif "hold_real" in v:
                # The row is a bridge when `run` is 0: HOLD with the commitment going on,
                # or, with `bridge` set, with a CURTAIL run going on (that run's, not
                # the commitment's).
                going = {bridge[i]: -1} if i in bridge else {}
                indicators[group].append(
                    ({**modes, runs[i]: 1, **going}, -1)
                    if group == "HOLD"
                    else ({**modes, runs[i]: -1, **going}, 1)
                    if group == "PV_FOLLOW"
                    else ({**modes, **{k: 1 for k in going}}, 0)
                    if group == "CURTAIL"
                    else (modes, 0)
                )
            else:
                going = {bridge[i]: -1} if i in bridge else {}
                indicators[group].append(
                    ({runs[i]: 1}, 0)
                    if group == "HOLD"
                    else ({runs[i]: -1, **going}, 1)
                    if group == "PV_FOLLOW"
                    else ({k: 1 for k in going}, 0)
                    if group == "CURTAIL"
                    else ({}, 0)
                )
    for group in GROUPS:
        members = _members(group)
        # A group's start is a continuous flag: it is at least the rise of the
        # group's indicator, and the indicator must cover every start still inside
        # its dwell. Summing the starts, not pairing each with every later slot,
        # gives the relaxation the convex hull of a minimum duration.
        starts_here = []
        for i, v in enumerate(vectors):
            current, current_const = indicators[group][i]
            prior, prior_const = (
                indicators[group][i - 1]
                if i
                else ({}, int(previous is not None and dwell_group(previous) == group))
            )
            rise = {
                key: current.get(key, 0) - prior.get(key, 0)
                for key in current.keys() | prior.keys()
            }
            rise = {key: value for key, value in rise.items() if value}
            bound = prior_const - current_const
            if (
                rise
                and ACTIVE.intersection(members)
                and coverage_end < starts[i] + dwell
            ):
                model.constrain(rise, -np.inf, bound)
            started = v[f"start_{group}"] = model.variable(upper=1.0)
            starts_here.append(started)
            if rise and not (group == "HOLD" and i in free_hold):
                model.constrain({started: -1, **rise}, -np.inf, bound)
        for i, v in enumerate(vectors):
            current, current_const = indicators[group][i]
            holding = {
                starts_here[j]: 1 for j in range(i + 1) if starts[i] < starts[j] + dwell
            }
            holding.update({key: -value for key, value in current.items()})
            model.constrain(holding, -np.inf, current_const)


def _check_load_following(problem: Problem, slot, flow, mode) -> None:
    """Reject import beside a partial discharge in a SELF_CONSUME interval."""
    if mode != "SELF_CONSUME" or flow.grid_import_kwh <= _IMPORT_TOLERANCE:
        return
    hours = (
        slot.end.astimezone(UTC) - slot.start.astimezone(UTC)
    ).total_seconds() / 3600
    power_limit = min(
        problem.battery.discharge_kw * hours,
        problem.site.inverter_kw * hours - slot.pv_kwh,
    )
    reserve = problem.battery.capacity_kwh * problem.battery.minimum_soc_fraction
    if (
        flow.discharge_kwh < power_limit - _IMPORT_TOLERANCE
        and flow.end_soc_kwh > reserve + _EMPTY_TOLERANCE
    ):
        raise SolveError(
            "solver_failure",
            "invalid solver result: self-consume imports below its limit",
        )


def _check_surplus_following(problem: Problem, slot, flow, mode, index) -> None:
    """Reject export beside a partial charge in a CHARGE_PV interval."""
    if mode != "CHARGE_PV" or flow.grid_export_kwh <= _IMPORT_TOLERANCE:
        return
    hours = _hours(slot)
    ceiling = problem.battery.capacity_kwh * (
        1
        if index in problem.balance_lift_slots
        else problem.battery.maximum_soc_fraction
    )
    if (
        flow.charge_kwh < problem.battery.charge_kw * hours - _IMPORT_TOLERANCE
        and flow.end_soc_kwh < ceiling - _EMPTY_TOLERANCE
    ):
        raise SolveError(
            "solver_failure",
            "invalid solver result: charge-from-PV exports below its limit",
        )


def validate_modes(problem: Problem, flows, bridges=None) -> None:
    """Independently certify labels, activity, coverage and carried commitments.

    `bridges` lists the committed idle rows the plan reads as a bridge, the rest
    of the HOLD rows start or continue a HOLD run (the solver says which, since
    both have the same flows). None accepts any reading of the idle HOLD rows: every
    one a bridge, or only those before some row, from which a HOLD run goes on. A HOLD
    row that goes on with a CURTAIL run for lack of PV needs no listing: inside the
    run's duration it can be nothing else (the solver lists the idle ones too).

    The None readings are looser than the model: the model gates a bridge on the rows
    where a commitment (or a CURTAIL run) can be under way, and the validator accepts
    any idle HOLD row as one while the carried commitment lasts. The solver passes the
    model's own bridges; the readings are for callers that have none.
    """
    if bridges is None and problem.battery and problem.minimum_mode_minutes:
        previous, _ = _initial(problem)
        idle = idle_slots(problem) if dwell_group(previous) == "PV_FOLLOW" else ()
        resting = [
            i
            for i, (slot, flow) in enumerate(zip(problem.slots, flows, strict=False))
            if i in idle and machine_state(slot, flow) == "HOLD"
        ]
        # The first row that is not a bridge ends the commitment, so the bridges are the
        # idle HOLD rows before it.
        readings = dict.fromkeys(
            (
                frozenset(idle),
                *(frozenset(resting[:count]) for count in range(len(resting), -1, -1)),
            )
        )
        failure = None
        for reading in readings:
            try:
                return _certify_modes(problem, flows, reading)
            except SolveError as error:
                failure = failure or error
        raise failure
    _certify_modes(problem, flows, frozenset(bridges or ()))


def _certify_modes(problem: Problem, flows, bridges: frozenset[int]) -> None:
    """Certify the flows of a plan that reads the committed idle rows in `bridges` as bridges."""
    if not problem.battery:
        return
    if not problem.minimum_mode_minutes:
        for slot, flow in zip(problem.slots, flows, strict=True):
            _check_load_following(problem, slot, flow, machine_state(slot, flow))
        return
    previous, since = _initial(problem)
    dwell = timedelta(minutes=problem.minimum_mode_minutes)
    coverage_end = problem.slots[-1].end.astimezone(UTC)
    drains = drain_schedule(problem)
    carried = dwell_group(previous) == "PV_FOLLOW"
    released = full_seen = False
    owed = _owed(problem, drains, since.astimezone(UTC) + dwell) if carried else ()
    idle = idle_slots(problem) if carried else frozenset()
    left = problem.battery.initial_kwh
    for index, (slot, flow) in enumerate(zip(problem.slots, flows, strict=True)):
        start = slot.start.astimezone(UTC)
        activity = (
            problem.minimum_mode_power_kw
            * (slot.end.astimezone(UTC) - start).total_seconds()
            / 3600
        )
        mode = machine_state(slot, flow)
        tolerance = activity_tolerance(activity)
        # A flow the classifier reads as zero is zero: solver noise up to its
        # threshold must not fail a slot it already labelled HOLD or an idle flow.
        noise = max(tolerance, _CLASSIFIER_TOLERANCE)
        charge, discharge, curt, export = (
            flow.charge_kwh,
            flow.discharge_kwh,
            flow.curtail_kwh,
            flow.grid_export_kwh,
        )
        surplus = max(slot.pv_kwh - slot.load_kwh, 0)
        valid = {
            "CHARGE_GRID": charge >= surplus + activity - tolerance
            and max(abs(discharge), abs(curt)) <= noise,
            "CHARGE_PV": activity - tolerance <= charge <= surplus + noise
            and max(abs(discharge), abs(curt)) <= noise,
            "DISCHARGE_GRID": min(discharge, export) >= activity - tolerance
            and max(abs(charge), abs(curt)) <= noise,
            "SELF_CONSUME": discharge >= activity - tolerance
            and max(abs(charge), abs(export), abs(curt)) <= noise,
            "HOLD": max(abs(charge), abs(discharge), abs(curt)) <= noise,
            "CURTAIL": curt >= activity - tolerance
            and max(abs(charge), abs(discharge)) <= noise,
        }
        if flow.dispatch_mode != mode or not valid[mode]:
            raise SolveError(
                "solver_failure", "invalid solver result: physical dispatch mode"
            )
        _check_load_following(problem, slot, flow, mode)
        _check_surplus_following(problem, slot, flow, mode, index)
        if (
            problem.initial_battery_mode is not None
            and start < problem.initial_battery_mode_since.astimezone(UTC) + dwell
        ):
            blocked = discharge if problem.initial_battery_mode == "charge" else charge
            if abs(blocked) > noise:
                raise SolveError(
                    "solver_failure", "invalid solver result: legacy direction guard"
                )
        # `problem` is the one the plan was realised on: a probe's load is in its slots
        # already, so the ceilings here take no `extra_load`, which the model, planning
        # the probe, bounds by its upper limit.
        if (carried or released) and not full_seen:
            full_seen = left >= _lock_ceiling(problem, drains, index) - _EMPTY_TOLERANCE
        if index in bridges:
            # Nothing to follow: HOLD by classification, the commitment goes on, or so
            # does the CURTAIL run that a dry interval has nothing left to curtail for.
            curtail_run = (
                previous == "CURTAIL"
                and _curtail_dry(problem, slot)
                and start < since.astimezone(UTC) + dwell
            )
            if not ((carried or curtail_run) and mode == "HOLD" and index in idle):
                raise SolveError(
                    "solver_failure", "invalid solver result: minimum mode duration"
                )
            left = flow.end_soc_kwh
            continue
        if (
            mode == "HOLD"
            and previous == "CURTAIL"
            and _curtail_dry(problem, slot)
            and start < since.astimezone(UTC) + dwell
        ):
            # Too little PV to curtail: the CURTAIL run goes on and its clock with it.
            left = flow.end_soc_kwh
            continue
        ends = False
        if dwell_group(mode) != dwell_group(previous):
            if previous is not None and start < since.astimezone(UTC) + dwell:
                # A carried PV-follow commitment ends once the battery cannot go on:
                # too empty to discharge, and not where CHARGE_PV could take a PV
                # surplus, or too full to charge. It ends in HOLD; a full pack may also
                # curtail what the site cannot pass, and so may the HOLD released from a
                # commitment whose pack has been full.
                full = left >= _lock_ceiling(problem, drains, index) - _EMPTY_TOLERANCE
                ends = (
                    carried
                    and mode == "HOLD"
                    and (
                        not _takes_surplus(problem, slot)
                        and left
                        <= _lock_floor(problem, drains, owed, index) + _EMPTY_TOLERANCE
                        or full
                    )
                )
                curtails = mode == "CURTAIL" and (
                    carried and full or released and full_seen
                )
                if not (ends or curtails):
                    raise SolveError(
                        "solver_failure", "invalid solver result: minimum mode duration"
                    )
            if mode in ACTIVE and coverage_end < start + dwell:
                raise SolveError(
                    "solver_failure",
                    "invalid solver result: active mode beyond coverage",
                )
            # A HOLD released from the commitment starts no clock of its own: the
            # commitment's deadline stays the only one.
            carried, released = False, ends
            if not ends:
                since = start
        previous = mode
        left = flow.end_soc_kwh
