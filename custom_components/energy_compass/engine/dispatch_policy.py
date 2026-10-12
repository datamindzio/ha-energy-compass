"""Physical operating modes and elapsed-time advice commitments."""

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


def _takes_surplus(problem, slot) -> bool:
    """Tell whether CHARGE_PV is open: the pack can take its activity from PV surplus."""
    hours = _hours(slot)
    activity = problem.minimum_mode_power_kw * hours
    return (
        activity > 2e-6
        and min(problem.battery.charge_kw * hours, slot.pv_kwh - slot.load_kwh)
        >= activity
    )


def _follow_open(problem, slot, extra_load=0.0) -> bool:
    """Tell whether SELF_CONSUME or CHARGE_PV can reach the minimum activity in a slot.

    The same bounds `constrain_modes` puts on the two modes, from the problem:
    SELF_CONSUME needs a net load the pack can cover, CHARGE_PV a surplus it can
    take. `extra_load` is the largest flexible-load probe the model may add.
    """
    battery, site = problem.battery, problem.site
    hours = _hours(slot)
    activity = problem.minimum_mode_power_kw * hours
    if activity <= 2e-6:
        return False
    surplus = max(slot.pv_kwh - slot.load_kwh, 0)
    charge_max = min(
        battery.charge_kw * hours,
        slot.pv_kwh + site.inverter_kw * hours,
        surplus + site.grid_import_kw * hours,
    )
    net_load = max(slot.load_kwh + extra_load - slot.pv_kwh, 0)
    discharge_max = max(
        0,
        min(
            battery.discharge_kw * hours,
            site.inverter_kw * hours - slot.pv_kwh,
            slot.load_kwh + extra_load - slot.pv_kwh + site.grid_export_kw * hours,
        ),
    )
    return (
        min(charge_max, surplus) >= activity or min(discharge_max, net_load) >= activity
    )


def idle_slots(problem, extra_loads=None) -> frozenset[int]:
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
        if slot.start.astimezone(UTC) < held_until
        and not _follow_open(problem, slot, extra_loads[i] if extra_loads else 0.0)
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


def _lock_ceiling(problem, drains, index) -> float:
    """Energy above which the battery cannot keep CHARGE_PV active in a slot."""
    battery = problem.battery
    hours = _hours(problem.slots[index])
    return (
        battery.capacity_kwh
        * (1 if index in problem.balance_lift_slots else battery.maximum_soc_fraction)
        + drains[index]
        - problem.minimum_mode_power_kw * hours * battery.eta_charge
    )


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
    idle = idle_slots(
        problem,
        [model.upper[v["flex_load"]] if flexible_load else 0.0 for v in vectors],
    )
    # Per idle committed interval, the binary that tells a real HOLD run from a bridge.
    runs = {}
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
        spent = (
            follows_pv
            and not _takes_surplus(problem, problem.slots[0])
            and battery.initial_kwh <= _lock_floor(problem, drains, owed, 0) + 1e-9
        )
        # What lets a committed interval be HOLD: a HOLD run that began earlier goes on
        # (`held`, the previous interval's HOLD with its sign flipped), the pack is too
        # empty to go on and HOLD starts where CHARGE_PV is not open (`depleted`; the
        # constant `spent` is the first interval's test), or it is too full (`full`).
        held = {}
        depleted = {}
        full = {}
        for i, (slot, start, v) in enumerate(
            zip(problem.slots, starts, vectors, strict=True)
        ):
            if start >= held_until:
                break
            chargeable = _takes_surplus(problem, slot)
            # The pack is known to be spent: HOLD starts, with nothing to test.
            known = spent and not i
            releases = {**full, **({} if chargeable else depleted)}
            if i in idle:
                # Nothing to follow: the flows are HOLD by classification. Either a HOLD
                # run is under way or starts here on a release, as in any committed
                # interval, or this row is a bridge and the commitment goes on.
                run = runs[i] = v["hold_run"] = model.variable(binary=True)
                model.constrain({run: 1, **held}, 0, np.inf)
                if not known:
                    model.constrain({run: 1, **held, **releases}, -np.inf, 0)
                held = {run: -1}
                model.constrain({v["mode_HOLD"]: 1}, 1, 1)
            else:
                # HOLD starts only on a release, and not on a PV surplus the pack
                # could take, but a run that began earlier goes on through it.
                kept = {v[f"mode_{mode}"]: 1 for mode in members}
                if known or held or releases:
                    if not known:
                        model.constrain(
                            {v["mode_HOLD"]: 1, **held, **releases}, -np.inf, 0
                        )
                    held = {v["mode_HOLD"]: -1}
                    kept[v["mode_HOLD"]] = 1
                model.constrain(kept, 1, 1)
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
                slack = _lock_ceiling(problem, drains, i + 1) - model.lower[v["energy"]]
                if slack > 0:
                    # `filled` may only be set once the pack reaches the next slot's
                    # ceiling less what its minimum activity would still add.
                    model.constrain(
                        {v["energy"]: 1, filled: -slack},
                        model.lower[v["energy"]],
                        np.inf,
                    )
                full[filled] = -1
    # A committed idle interval that is a bridge carries its neighbour's state: it
    # continues the PV-follow commitment and starts no HOLD clock.
    indicators = {group: [] for group in GROUPS}
    for i, v in enumerate(vectors):
        for group in GROUPS:
            if i in runs:
                indicators[group].append(
                    ({runs[i]: 1}, 0)
                    if group == "HOLD"
                    else ({runs[i]: -1}, 1)
                    if group == "PV_FOLLOW"
                    else ({}, 0)
                )
            else:
                indicators[group].append(
                    # Modes are one-hot, so a group's member sum is its binary indicator.
                    ({v[f"mode_{mode}"]: 1 for mode in _members(group)}, 0)
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
            if rise:
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
    both have the same flows). None reads every idle HOLD row as a bridge.
    """
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
    owed = _owed(problem, drains, since.astimezone(UTC) + dwell) if carried else ()
    idle = idle_slots(problem) if carried else frozenset()
    bridges = idle if bridges is None else frozenset(bridges)
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
        if index in bridges:
            if not (carried and mode == "HOLD" and index in idle):
                raise SolveError(
                    "solver_failure", "invalid solver result: minimum mode duration"
                )
            # Nothing to follow: HOLD by classification, the commitment goes on.
            left = flow.end_soc_kwh
            continue
        if dwell_group(mode) != dwell_group(previous):
            if (
                previous is not None
                and start < since.astimezone(UTC) + dwell
                # A carried PV-follow commitment ends once the battery cannot go on:
                # too empty to discharge, and not where CHARGE_PV could take a PV
                # surplus, or too full to charge.
                and not (
                    carried
                    and mode == "HOLD"
                    and (
                        not _takes_surplus(problem, slot)
                        and left
                        <= _lock_floor(problem, drains, owed, index) + _EMPTY_TOLERANCE
                        or index
                        and left
                        >= _lock_ceiling(problem, drains, index) - _EMPTY_TOLERANCE
                    )
                )
            ):
                raise SolveError(
                    "solver_failure", "invalid solver result: minimum mode duration"
                )
            if mode in ACTIVE and coverage_end < start + dwell:
                raise SolveError(
                    "solver_failure",
                    "invalid solver result: active mode beyond coverage",
                )
            since = start
            carried = False
        previous = mode
        left = flow.end_soc_kwh
