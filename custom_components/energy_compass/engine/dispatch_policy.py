"""Physical operating modes and elapsed-time advice commitments."""

import math
from collections import Counter
from collections.abc import Sequence
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
    "CURTAIL": ("bd",),
}
# Import this small is solver noise, not a plan.
_IMPORT_TOLERANCE = 1e-5
# HiGHS accepts each binary within 1e-6 and each row within 1e-6: a follow row written
# with big-M `m` (`bc >= m * (x - full)`) leaves up to 1e-6 * (2m + 1) kWh short, and
# 1e-6 * (3m + 1) with a flexible load, whose rows add a power binary.
_ROW_TOLERANCE = 1e-6
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
    if mode == "SELF_CONSUME" and not problem.initial_dispatch_reserve_hold:
        # A PV-follow commitment whose pack is at the reserve is not interrupted: the
        # inverter stops at the minimum SOC, that is its usual empty state, and the
        # commitment is released into a HOLD with no clock of its own that may go back
        # to the surplus (ADR-0005 point 13). A HOLD with the commitment's clock would
        # curtail beside the empty pack or leave the plan infeasible (T-058); it is
        # the last resort of `reserve_fallback` when the released plan has no solution.
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


def reserve_fallback(problem: Problem) -> Problem | None:
    """Return the problem with the pack-at-the-reserve HOLD kept, or None if it changes nothing.

    The released HOLD of a PV-follow commitment at the reserve cannot always take the PV
    above the export cap (charge power; charge and curtail in one interval are not
    modelled), and the house must never be left without a plan. Solving this problem
    instead gives the previous plan: a HOLD with the commitment's clock, which may curtail.
    """
    if problem.initial_dispatch_reserve_hold:
        return None
    fallback = replace(problem, initial_dispatch_reserve_hold=True)
    return fallback if safety_exception(fallback) else None


def uncommitted_fallback(problem: Problem) -> Problem | None:
    """Return the problem without its carried PV-follow commitment, or None if it has none.

    The last resort of `optimize.solve_released`, after `reserve_fallback`: a carried
    CHARGE_PV or SELF_CONSUME inside its minimum duration can leave a state no plan
    reaches (the previous plan allowed what the commitment now forbids, e.g. the
    `preserve_initial` terminal rule beside a pack just above the reserve and PV below the
    load), and the house is never left without a plan. The plan is solved without the
    commitment's clock; the coordinator still holds the commitment, so a first interval
    that is still CHARGE_PV or SELF_CONSUME keeps it and its clock, and any other first
    mode starts a new one, stored as for any mode change.
    """
    if dwell_group(problem.initial_dispatch_mode) != "PV_FOLLOW":
        return None
    return replace(
        problem,
        initial_dispatch_mode=None,
        initial_dispatch_mode_since=None,
        initial_dispatch_released=False,
        initial_dispatch_reserve_hold=False,
    )


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


def _owed(problem, drains, held_until, terminal=True) -> tuple[float, ...]:
    """Energy each slot must end with so the standby loss of later slots stays payable.

    Best case for the pack: every later slot charges all it may, from PV surplus
    and, outside the carried commitment, from the grid where grid charging is
    open. Ending a slot below this value cannot stay above the relaxed reserve
    floor later, or meet the `preserve_initial` terminal rule, whatever the plan
    does, so it is a bound the energy rows already imply, not a new requirement.
    `terminal` False leaves the terminal rule out: the bound of a pack that is
    depleted in itself, which is all a HOLD released from a full pack may go back on.
    """
    battery = problem.battery
    reserve = min(
        battery.capacity_kwh * battery.minimum_soc_fraction, battery.initial_kwh
    )
    drained = cumulative(drains)
    owed = []
    later = (
        battery.initial_kwh - sum(drains)
        if terminal and problem.terminal_mode == "preserve_initial"
        else 0.0
    )
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


def curtail_need(problem, slot, extra_load=0.0) -> float:
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


def _beyond_the_pack(problem, slot, extra_load=0.0) -> float:
    """Tell how much of `_curtail_need` the pack cannot take by its charge power (T-064).

    The model charges and curtails in one interval (a CURTAIL row may charge), and
    CHARGE_PV, which curtails nothing, has no flow where this is positive. The need from
    the inverter rating takes no load's help (`_takeover_room`), so a probe `extra_load`
    only shrinks what the export cap needs.
    """
    battery = problem.battery
    return _curtail_need(problem, slot, extra_load) - battery.charge_kw * _hours(slot)


def _beyond_room(problem, slot) -> float:
    """Tell below which probe load a slot's PV stays beyond what the pack takes.

    The PV above the inverter rating that the pack cannot take is beyond it whatever a
    probe adds (inf); otherwise `_beyond_the_pack` falls by the probe load and is positive
    for the loads below what is returned (negative: none).
    """
    hours = _hours(slot)
    charge = problem.battery.charge_kw * hours
    if slot.pv_kwh - problem.site.inverter_kw * hours - charge > _ROW_TOLERANCE:
        return math.inf
    return (
        slot.pv_kwh - slot.load_kwh - problem.site.grid_export_kw * hours - charge
    )


def _takeover_room(problem, slot) -> float:
    """Tell how much flexible load a slot takes while `curtail_need` stays positive.

    The inverter rating needs no help from the load, so PV above it is a need whatever
    a probe adds (inf). Otherwise the load takes from the export cap's need what it
    uses: the need is positive for a probe load below what is returned (negative: none).
    """
    hours = _hours(slot)
    if slot.pv_kwh - problem.site.inverter_kw * hours > 0:
        return math.inf
    return slot.pv_kwh - slot.load_kwh - problem.site.grid_export_kw * hours


def _ceiling_less(problem, drains, index, absorbed) -> float:
    """Energy above which a pack cannot take `absorbed` kWh more (before charge losses) in a slot."""
    battery = problem.battery
    return (
        battery.capacity_kwh
        * (1 if index in problem.balance_lift_slots else battery.maximum_soc_fraction)
        + drains[index]
        - absorbed * battery.eta_charge
    )


def _lock_ceiling(problem, drains, index, extra_load=0.0) -> float:
    """Energy above which the battery cannot keep CHARGE_PV active in a slot.

    CHARGE_PV is kept by its minimum activity, or by what the site cannot pass
    (`curtail_need`) when that is more: a pack with less room cannot charge the
    interval and stay below the export cap or the inverter rating.
    """
    slot = problem.slots[index]
    absorbed = max(
        problem.minimum_mode_power_kw * _hours(slot),
        curtail_need(problem, slot, extra_load),
    )
    return _ceiling_less(problem, drains, index, absorbed)


def curtail_limit(problem, slot, extra_load=0.0):
    """Largest curtailment (kWh) a slot may carry.

    A site that can curtail may curtail all its PV. One that cannot still spills
    what the grid export cap or the inverter rating leaves it no way to pass once
    the pack cannot take it (a forced spill, T-041): the inverter throttles by itself
    and the plan reads that as CURTAIL. With a minimum mode duration a CURTAIL
    interval carries at least the minimum activity, so a smaller need is offered as
    that much (the controller's inverter spills what it must; the label needs the activity).
    """
    if problem.site.allow_curtailment:
        return slot.pv_kwh
    if not problem.battery:
        return 0
    need = curtail_need(problem, slot, extra_load)
    if need <= 0:
        # An integer zero keeps the rows built from this bound identical to a site that
        # never spills (the golden model hash).
        return 0
    if problem.minimum_mode_minutes:
        need = max(need, problem.minimum_mode_power_kw * _hours(slot))
    return need


def spill_gate(problem, drains, index, extra_load=0.0) -> float:
    """Energy at the start of a slot from which the pack cannot take the slot's need.

    The ceiling (plus the standby loss the slot takes) less what the pack would have to
    absorb, `curtail_need` and, with a minimum mode duration, at least the minimum
    activity: from here CHARGE_PV is not open, and a forced spill is the only thing left
    for the PV the site cannot pass. `extra_load` is the flexible load the slot may take
    at most (the pack's gate rises with it: the less to absorb, the more room it needs).
    """
    slot = problem.slots[index]
    absorbed = curtail_need(problem, slot, extra_load)
    if problem.minimum_mode_minutes:
        absorbed = max(absorbed, problem.minimum_mode_power_kw * _hours(slot))
    return _ceiling_less(problem, drains, index, absorbed)


def carried_curtail(problem) -> bool:
    """Tell whether the plan starts inside a CURTAIL run (mode model).

    On a site that cannot curtail that run is a forced spill under way: it keeps the
    first interval's spill open the way a run inside the plan keeps the next one, so the
    plan made one interval into a spill is the one that planned it.
    """
    return bool(problem.minimum_mode_minutes) and _initial(problem)[0] == "CURTAIL"


def _curtail_dry(problem, slot) -> bool:
    """Tell whether a slot has nothing to curtail the minimum activity from.

    Too little PV for it, or, on a site that cannot curtail, no PV the site cannot pass:
    a forced spill is the inverter's own throttling, so a run of it goes on as HOLD
    while there is none.
    """
    activity = problem.minimum_mode_power_kw * _hours(slot)
    if activity <= 2e-6:
        return False
    if slot.pv_kwh < activity:
        return True
    return (
        not problem.site.allow_curtailment
        and problem.battery is not None
        and curtail_need(problem, slot) <= 0
    )


def _follow_load_unmoded(model, problem, vectors, *, flexible_load) -> None:
    """Apply the load- and surplus-following rules without the mode model.

    With no minimum duration there are no mode binaries, but the flows are still
    labelled SELF_CONSUME or CHARGE_PV and a controller still holds the full
    discharge or charge cap, so a partial discharge next to import or a partial
    charge next to export stays unexecutable. A binary per interval stands for
    "the battery discharges" (`discharging`, added here) or "the battery charges"
    (`battery_mode`, which `optimize` already holds per interval and ties to `bc`
    and `bd`); it plays the role of the mode indicator, and the rule is the same.
    A charge above the PV surplus is CHARGE_GRID and already takes the whole
    surplus, so the rule does not touch it.
    """
    battery = problem.battery
    reserve = battery.capacity_kwh * battery.minimum_soc_fraction
    lowest = battery.initial_kwh
    highest = battery.initial_kwh
    for slot, v, drain in zip(
        problem.slots, vectors, drain_schedule(problem), strict=True
    ):
        hours = _hours(slot)
        flex_max = model.upper[v["flex_load"]] if flexible_load else 0
        discharge_max = model.upper[v["bd"]]
        lowest -= discharge_max / battery.eta_discharge + drain
        if discharge_max > 0:
            discharging = v["discharging"] = model.variable(binary=True)
            model.constrain({v["bd"]: 1, discharging: -discharge_max}, -np.inf, 0)
            model.constrain({discharging: 1, v["battery_mode"]: 1}, -np.inf, 1)
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
        charge_max = model.upper[v["bc"]]
        ceiling = model.upper[v["energy"]]
        highest = min(ceiling, highest + battery.eta_charge * charge_max - drain)
        surplus = max(slot.pv_kwh - slot.load_kwh, 0)
        if charge_max <= 0 or surplus <= 0:
            continue
        _follow_surplus(
            model,
            v,
            {"CHARGE_PV": v["battery_mode"]},
            surplus,
            flex_max,
            highest >= ceiling - 1e-9,
        )


def _fill_releases(model, problem, vectors, drains, starts, dwell, flexible_load):
    """Let a PV-follow run that filled the pack end in HOLD on a site that cannot curtail.

    There the PV the site cannot pass makes CHARGE_PV the only legal mode while the pack
    has room for it, so the run is not the plan's choice, and once the pack is full it
    has nothing to charge: where the need is over as well (no forced spill to hand over
    to), it ends in a HOLD that starts its own clock. Offered within a minimum duration
    of an interval with such PV, per interval the flag of the release; the pack is full
    by the reading of a carried commitment (`_lock_ceiling`). A carried CURTAIL is such
    an interval just before the horizon: the plan it comes from counted on the release.
    """
    filled = {}
    if problem.site.allow_curtailment:
        return filled
    needs = [curtail_need(problem, slot) > 0 for slot in problem.slots]
    spilled = starts[0] - (problem.slots[0].end - problem.slots[0].start)
    for i, v in enumerate(vectors):
        if not i or needs[i]:
            continue
        if not (
            any(needs[j] and starts[i] < starts[j] + dwell for j in range(i))
            or carried_curtail(problem)
            and starts[i] < spilled + dwell
        ):
            continue
        before = vectors[i - 1]
        ended = filled[i] = v["fill_release"] = model.variable(upper=1.0)
        model.constrain({ended: 1, v["mode_HOLD"]: -1}, -np.inf, 0)
        model.constrain(
            {
                ended: 1,
                **{before[f"mode_{mode}"]: -1 for mode in _members("PV_FOLLOW")},
            },
            -np.inf,
            0,
        )
        extra = model.upper[v["flex_load"]] if flexible_load else 0.0
        ceiling = _lock_ceiling(problem, drains, i, extra)
        if ceiling > model.lower[before["energy"]]:
            full = v["fill_full"] = model.variable(binary=True)
            model.constrain({before["energy"]: 1, full: -ceiling}, 0, np.inf)
            model.constrain({ended: 1, full: -1}, -np.inf, 0)
    return filled


def _released_start(problem: Problem) -> bool:
    """Tell whether the interval before the first one was a HOLD released from a depleted pack.

    A HOLD released from a PV-follow commitment because its pack is depleted starts no
    clock, may go back to the commitment and, past the commitment's deadline, goes on
    as long as it runs. The coordinator carries that state across a boundary
    (`initial_dispatch_released`) with the commitment and its clock, which stay.
    """
    previous, _ = _initial(problem)
    return bool(
        problem.initial_dispatch_released
        and problem.battery
        and problem.minimum_mode_minutes
        and dwell_group(previous) == "PV_FOLLOW"
    )


def _resume(model, v, members, released, depleted, emptied, physical=None) -> None:
    """Let a HOLD released from a depleted pack go back to the commitment (`back`).

    A released HOLD does not go back to the commitment, except where the pack it
    was released for is still depleted: the surplus is then what the pack takes, and
    a HOLD cannot pass the PV above the export cap. `emptied` is the HOLD run that was
    released from a depleted pack, offered in an interval whose PV the site cannot
    pass: that HOLD has no legal flow there, so it goes back whatever the pack holds
    (a HOLD released from a full pack does not: the coordinator carries it as a HOLD
    with a clock of its own). The return is the commitment going on, not a PV-follow
    run of its own: its deadline is the only one.

    `depleted` counts a pack the terminal rule has spent (the lock floor above the
    reserve's). Only a run the coordinator carries as released, one that began on a
    depleted pack, goes back on that; any other run needs the pack depleted in itself.
    `physical` is that bound, `(energy before, slack, run that began depleted)`, where
    the two floors differ.
    """
    back = v["back"] = model.variable(binary=True)
    followed = {v[f"mode_{mode}"]: 1 for mode in members}
    model.constrain({**followed, **released, back: -1}, -np.inf, 1)
    model.constrain(
        {back: 1, **{key: -value for key, value in followed.items()}}, -np.inf, 0
    )
    model.constrain(
        {back: 1, **{key: -value for key, value in released.items()}}, -np.inf, 0
    )
    model.constrain(
        {back: 1, **depleted, **{key: -value for key, value in emptied.items()}},
        -np.inf,
        0,
    )
    if physical:
        energy, slack, began = physical
        model.constrain(
            {
                energy: 1,
                back: slack,
                **{key: -slack * value for key, value in began.items()},
            },
            -np.inf,
            model.upper[energy],
        )


def _nonzero(terms) -> dict:
    return {key: value for key, value in terms.items() if value}


def _emptied_hold(model, v, hold, origin, held, emptied) -> dict:
    """Tell the HOLD run `hold` began on a depleted pack (`emptied_hold`, 1 where it did).

    `origin` is what released it from a depleted pack (the terms of `depleted`, signs
    flipped as in `releases`), `held` the HOLD run going on from the interval before
    (signed as `held`), `emptied` that interval's own result. A run goes on as it began,
    so a run released from a full pack stays one when the pack empties under it.
    """
    row = v["emptied_hold"] = model.variable(upper=1.0)
    model.constrain({row: 1, hold: -1}, -np.inf, 0)
    # Released from a depleted pack here, or the run went on with its release.
    began = Counter(origin)
    began.update({key: -value for key, value in emptied.items()})
    model.constrain({row: 1, **_nonzero(began)}, -np.inf, 0)
    # A run going on is the one before it: only a run that starts here has an origin.
    going = Counter({key: -value for key, value in held.items()})
    going.update({key: -value for key, value in emptied.items()})
    model.constrain({row: 1, **_nonzero(going)}, -np.inf, 1)
    return {row: 1}


def _open_beyond(model, v, room, top):
    """Tell whether a committed interval's PV is beyond what the pack takes (T-064).

    `room` is `_beyond_the_pack` for a load of nothing and `top` the largest probe load:
    the PV is beyond the pack for the loads below `room`. Returns None where it never is,
    True where it is whatever a probe adds, and otherwise the term of a binary that is 1
    only for a probe load below `room` (the term `_offer_curtail` takes).
    """
    limit = room - _ROW_TOLERANCE - (_PROBE_MARGIN if top else 0.0)
    if limit <= 0:
        return None
    if limit >= top:
        return True
    opened = v["beyond_open"] = model.variable(binary=True)
    model.constrain({v["flex_load"]: 1, opened: top - limit}, -np.inf, top)
    return {opened: -1}


def _offer_curtail(
    model, v, seen_full, full, curtailing, released, *, beyond_the_pack=None
) -> None:
    """Let a committed interval curtail: the pack has been full, and is full now or was let go.

    `seen_full` is the `lock_full` of every earlier interval; `full`, `curtailing`
    and `released` are the previous interval's: a pack at its ceiling, a CURTAIL
    under way, the HOLD released from the commitment.

    `beyond_the_pack` (`_open_beyond`) is an interval whose PV above what the site passes
    the pack cannot take by its charge power: CHARGE_PV has no flow there and the
    inverter curtails beside the charging pack whatever the pack held before (T-064), so
    there is nothing to wait for.
    """
    if beyond_the_pack is True:
        return
    beyond_the_pack = beyond_the_pack or {}
    model.constrain({v["mode_CURTAIL"]: 1, **seen_full, **beyond_the_pack}, -np.inf, 0)
    model.constrain(
        {
            v["mode_CURTAIL"]: 1,
            **full,
            **curtailing,
            **{key: -1 for key in released},
            **beyond_the_pack,
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
        # A CURTAIL interval may charge from the PV it cannot pass (T-064): the direction
        # flag is then free, the solver sets it where it charges. Where CURTAIL cannot be
        # chosen, or the pack takes all the PV the site cannot pass by its charge power,
        # the rows are the ones every other interval has.
        can_curtail = (
            model.upper[x["CURTAIL"]] > 0
            and _beyond_room(problem, slot) > _ROW_TOLERANCE
        )
        if can_curtail:
            model.constrain(
                {v["battery_mode"]: 1, x["CHARGE_GRID"]: -1, x["CHARGE_PV"]: -1},
                0,
                1,
            )
            model.constrain(
                {
                    v["battery_mode"]: 1,
                    x["CHARGE_GRID"]: -1,
                    x["CHARGE_PV"]: -1,
                    x["CURTAIL"]: -1,
                },
                -np.inf,
                0,
            )
        else:
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
        # A CURTAIL interval charges from the PV it leaves, after the house and a probe's
        # load: with a probe, only where it fits (`curtail_charge`, a CURTAIL that charges).
        curtail_charge = x["CURTAIL"]
        if flexible_load and can_curtail:
            curtail_charge = v["curtail_charge"] = model.variable(binary=True)
            model.constrain({curtail_charge: 1, x["CURTAIL"]: -1}, -np.inf, 0)
        charge_rows = {
            v["bc"]: 1,
            x["CHARGE_GRID"]: -charge_max,
            x["CHARGE_PV"]: -pv_max,
        }
        if can_curtail:
            charge_rows[curtail_charge] = -pv_max
        model.constrain(charge_rows, -np.inf, 0)
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
                if can_curtail:
                    model.constrain(
                        {
                            v["bc"]: 1,
                            v["flex_load"]: 1,
                            curtail_charge: charge_source_max,
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
            and curtail_need(problem, problem.slots[j]) > 0
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
    # A HOLD takes no curtailment: in an interval whose PV the site cannot pass
    # (`curtail_need`) a HOLD run inside its minimum duration has no legal mode left
    # but CURTAIL, which the inverter does by itself. That CURTAIL takes the HOLD over
    # (`takeover`): the HOLD's remaining duration is the CURTAIL run's from then on
    # (it lasts longer), and a HOLD after the run starts its own clock. Only where the
    # site cannot pass the PV, so it is no label to leave the duration with. A probe's
    # load is part of what the site passes: the takeover is offered for the probe
    # loads that leave the need as realised (`_takeover_room`), as the validator reads it.
    takes = {}
    taken = {}
    margin = _PROBE_MARGIN if flexible_load else 0.0
    rooms = [_takeover_room(problem, slot) - margin for slot in problem.slots]
    # PV the site cannot pass whatever the probe adds: a HOLD has no legal flow there.
    unpassable = [
        curtail_need(
            problem, slot, model.upper[v["flex_load"]] if flexible_load else 0.0
        )
        - margin
        > 0
        for slot, v in zip(problem.slots, vectors, strict=True)
    ]
    # PV above what the site passes that the pack's charge power cannot take (T-064).
    beyond_rooms = [_beyond_room(problem, slot) for slot in problem.slots]
    if any(
        model.upper[v["mode_CURTAIL"]] > 0 and room > 0
        for room, v in zip(rooms, vectors, strict=True)
    ):
        for i, (room, v) in enumerate(zip(rooms, vectors, strict=True)):
            takeover = v["takeover"] = model.variable(upper=1.0)
            taken[i] = takeover
            # The CURTAIL under way (a bridge row is that run going on).
            model.constrain(
                {takeover: 1, **{k: -c for k, c in curtail_terms(i).items()}},
                -np.inf,
                0,
            )
            continues = {takeover: 1}
            if i:
                continues[taken[i - 1]] = -1
            if (
                model.upper[v["mode_CURTAIL"]] > 0
                and room > 0
                and (i or previous == "HOLD")
            ):
                # The run begins here, from the HOLD of the interval before.
                begins = takes[i] = v["takeover_start"] = model.variable(upper=1.0)
                model.constrain({begins: 1, v["mode_CURTAIL"]: -1}, -np.inf, 0)
                continues[begins] = -1
                top = model.upper[v["flex_load"]] if flexible_load else 0
                if room < top:
                    # Only for the probe loads that leave the PV a need.
                    model.constrain(
                        {v["flex_load"]: 1, begins: top - room}, -np.inf, top
                    )
            model.constrain(continues, -np.inf, 0)
    released_start = _released_start(problem)
    # Per idle committed interval, the binary that tells a real HOLD run from a bridge.
    runs = {}
    # Intervals whose HOLD started from a carried PV-follow commitment: no clock of its own.
    free_hold = set()
    if previous is not None:
        held_until = since.astimezone(UTC) + dwell
        members = _members(dwell_group(previous))
        follows_pv = "SELF_CONSUME" in members
        follows_hold = dwell_group(previous) == "HOLD"
        owed = _owed(problem, drains, held_until) if follows_pv else ()
        # The same without the terminal rule: where the two floors differ, a pack above
        # this one is spent only by the terminal rule, which a HOLD released from a
        # full pack does not go back on (the coordinator does not carry it as released).
        owed_physical = (
            _owed(problem, drains, held_until, terminal=False)
            if follows_pv and problem.terminal_mode == "preserve_initial"
            else owed
        )
        raised = [
            owed_physical is not owed
            and _lock_floor(problem, drains, owed, j)
            > _lock_floor(problem, drains, owed_physical, j) + 1e-9
            for j in range(len(problem.slots))
        ]
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
        # That HOLD run, when it was released from a depleted pack (`_emptied_hold`): in
        # an interval whose PV the site cannot pass it goes back to the commitment even
        # if the pack has since been charged (the coordinator carries it, a full-pack
        # release it does not).
        emptied = {}
        if released_start:
            # The interval before the first was a HOLD released from the depleted pack:
            # that HOLD goes on, or goes back to the commitment (`_resume`).
            fixed = model.variable(upper=1.0)
            model.lower[fixed] = 1.0
            held = {fixed: -1}
            released = {fixed: 1}
            emptied = {fixed: 1}
            depleted = {fixed: -1}
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
            # A released HOLD run of a depleted pack goes back to the commitment where
            # the site cannot pass the PV (`_resume`); the run before is carried over.
            resumable = emptied if unpassable[i] else {}
            held_before, emptied_before, emptied = held, emptied, {}
            track = follows_pv and (any(unpassable[i + 1 :]) or any(raised[i + 1 :]))
            # The bound a HOLD run not carried as released goes back on, where the lock
            # floor is above what the pack itself needs.
            physical = None
            if i and raised[i]:
                energy = vectors[i - 1]["energy"]
                slack = model.upper[energy] - _lock_floor(
                    problem, drains, owed_physical, i
                )
                if slack > 0:
                    physical = (energy, slack, emptied_before)
            # The pack is known to be spent: HOLD starts, with nothing to test.
            known = spent and not i
            # What starts a HOLD run from a depleted pack, and the releases of both kinds.
            origin = {} if chargeable else depleted
            releases = {**full, **origin}
            if known and track:
                # The first interval's pack is observed spent: a constant.
                sure = model.variable(upper=1.0)
                model.lower[sure] = 1.0
                origin = {sure: -1}
            if flexible_load and chargeable:
                # The surplus is chargeable as realised: a probe may take it below the
                # minimum activity, which is what the plan is certified against.
                open_surplus = model.variable(binary=True)
                bar = _surplus_bar(problem, slot)
                model.constrain({v["flex_load"]: 1, open_surplus: bar}, bar, np.inf)
                releases = dict(full)
                origin = {}
                if pack_spent if not i else depleted:
                    # Depleted and not chargeable as realised: HOLD may start.
                    release = model.variable(binary=True)
                    model.constrain({release: 1, open_surplus: 1}, -np.inf, 1)
                    if i:
                        model.constrain({release: 1, **depleted}, -np.inf, 0)
                    releases[release] = -1
                    origin = {release: -1}
            # An idle interval is CURTAIL on the terms of any committed interval: nothing
            # to follow does not make room in a full pack. A CURTAIL row is not a bridge.
            beyond = (
                _open_beyond(
                    model,
                    v,
                    beyond_rooms[i],
                    model.upper[v["flex_load"]] if flexible_load else 0.0,
                )
                if model.upper[v["mode_CURTAIL"]] > 0 and "CHARGE_PV" in members
                else None
            )
            curtails = (bool(seen_full) or beyond is not None) and model.upper[
                v["mode_CURTAIL"]
            ] > 0
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
                    _offer_curtail(
                        model,
                        v,
                        seen_full,
                        full,
                        curtailing,
                        released,
                        beyond_the_pack=beyond,
                    )
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
                    # to the commitment (unless the pack is depleted) and starts no
                    # clock of its own.
                    if released:
                        _resume(
                            model, v, members, released, depleted, resumable, physical
                        )
                    if track:
                        emptied = _emptied_hold(
                            model, v, real, origin, held_before, emptied_before
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
                    if track:
                        emptied = _emptied_hold(
                            model, v, run, origin, held_before, emptied_before
                        )
                    free_hold.add(i)
                    released = {run: 1}
                curtailing = {bridge[i]: -1} if i in bridge else {}
            elif follows_hold:
                # A HOLD that must last: nothing else is legal, but a CURTAIL that takes
                # it over where the site cannot pass the PV (and goes on, with its own
                # clock, as a CURTAIL run does).
                kept = {v["mode_HOLD"]: 1}
                if i in taken:
                    model.constrain({v["mode_CURTAIL"]: 1, taken[i]: -1}, -np.inf, 0)
                    kept[v["mode_CURTAIL"]] = 1
                model.constrain(kept, 1, 1)
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
                if seen_full or beyond is not None:
                    _offer_curtail(
                        model,
                        v,
                        seen_full,
                        full,
                        curtailing,
                        released,
                        beyond_the_pack=beyond,
                    )
                    kept[v["mode_CURTAIL"]] = 1
                    curtailing = {v["mode_CURTAIL"]: -1, **{k: -1 for k in going_on}}
                model.constrain(kept, 1, 1)
                if follows_pv:
                    # A released HOLD does not go back to the commitment (a released
                    # CURTAIL is held by its own minimum duration), except while the
                    # pack is depleted: the surplus it takes is what the inverter does
                    # there, and a HOLD cannot pass the PV above the export cap. It
                    # starts no clock of its own: the commitment's deadline is the only
                    # one, so it may end in CURTAIL at once.
                    if released:
                        _resume(
                            model, v, members, released, depleted, resumable, physical
                        )
                    if track:
                        emptied = _emptied_hold(
                            model,
                            v,
                            v["mode_HOLD"],
                            origin,
                            held_before,
                            emptied_before,
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
    for i, begins in takes.items():
        # The HOLD run that a CURTAIL takes over is under way in the interval before.
        if i:
            current, current_const = indicators["HOLD"][i - 1]
            model.constrain(
                {begins: 1, **{k: -v for k, v in current.items()}},
                -np.inf,
                current_const,
            )
    filled = _fill_releases(
        model, problem, vectors, drains, starts, dwell, flexible_load
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
                else (
                    {},
                    int(
                        (
                            previous is not None
                            and dwell_group(previous) == group
                            and not (released_start and group == "PV_FOLLOW")
                        )
                        # The released HOLD goes on: a HOLD run with no clock.
                        or (released_start and group == "HOLD")
                    ),
                )
            )
            rise = {
                key: current.get(key, 0) - prior.get(key, 0)
                for key in current.keys() | prior.keys()
            }
            rise = {key: value for key, value in rise.items() if value}
            bound = prior_const - current_const
            # A return to the commitment (`back`) is that commitment going on, not a run.
            if group == "PV_FOLLOW" and "back" in v:
                rise = {**rise, v["back"]: -1}
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
            if group == "HOLD" and i in taken:
                # A CURTAIL that took the HOLD over is the HOLD going on.
                holding[taken[i]] = -1
            if group == "PV_FOLLOW" and not problem.site.allow_curtailment:
                # A site that cannot curtail has CURTAIL only as a forced spill, into a
                # pack that cannot take the PV: a PV-follow run that fills the pack
                # ends there (its own minimum duration is not the pack's to wait out).
                # The spill goes on as a CURTAIL run does (a bridge where the need is
                # over), and where there is no need the run ends in a HOLD (`filled`).
                if model.upper[v["mode_CURTAIL"]] > 0 or i in bridge:
                    for key in curtail_terms(i):
                        holding[key] = -1
                for j, ended in filled.items():
                    if j <= i and starts[i] < starts[j] + dwell:
                        holding[ended] = -1
            model.constrain(holding, -np.inf, current_const)


def _follow_tolerance(big_m: float, binaries: int = 2) -> float:
    """Import or export the noise of a follow row of bound `big_m` kWh may leave.

    `binaries` is how many of the row's binaries (mode, relief, and with a flexible
    load the power one) carry the bound: each leaves 1e-6 of it.
    """
    return max(_IMPORT_TOLERANCE, _ROW_TOLERANCE * (binaries * big_m + 1))


def _check_load_following(
    problem: Problem, slot, flow, mode, flex_max: float = 0.0
) -> None:
    """Reject import beside a partial discharge in a SELF_CONSUME interval.

    `flex_max` is the flexible load's largest draw in the interval (kWh): its rows
    carry a third binary and a bound that includes it, while `slot` holds the load
    the plan realised (the base load plus the probe it drew).
    """
    if mode != "SELF_CONSUME":
        return
    hours = (
        slot.end.astimezone(UTC) - slot.start.astimezone(UTC)
    ).total_seconds() / 3600
    power_limit = min(
        problem.battery.discharge_kw * hours,
        problem.site.inverter_kw * hours - slot.pv_kwh,
    )
    # Bound of every row's M (docs/model.md): the power limit, or the net load plus
    # the flexible load's largest draw (the base load is at most the realised one).
    tolerance = _follow_tolerance(
        max(power_limit, slot.load_kwh + flex_max), 3 if flex_max > 0 else 2
    )
    if flow.grid_import_kwh <= tolerance:
        return
    reserve = problem.battery.capacity_kwh * problem.battery.minimum_soc_fraction
    if (
        flow.discharge_kwh < power_limit - tolerance
        and flow.end_soc_kwh > reserve + _EMPTY_TOLERANCE
    ):
        raise SolveError(
            "solver_failure",
            "invalid solver result: self-consume imports below its limit",
        )


def _check_surplus_following(
    problem: Problem, slot, flow, mode, index, flex_max: float = 0.0
) -> None:
    """Reject export beside a partial charge in a CHARGE_PV interval.

    `flex_max` is as for `_check_load_following`: a third binary on the surplus row.
    """
    if mode != "CHARGE_PV":
        return
    hours = _hours(slot)
    # Bound of every row's M (docs/model.md): the charge limit, or the surplus below the slot's PV.
    tolerance = _follow_tolerance(
        max(problem.battery.charge_kw * hours, slot.pv_kwh), 3 if flex_max > 0 else 2
    )
    if flow.grid_export_kwh <= tolerance:
        return
    ceiling = problem.battery.capacity_kwh * (
        1
        if index in problem.balance_lift_slots
        else problem.battery.maximum_soc_fraction
    )
    if (
        flow.charge_kwh < problem.battery.charge_kw * hours - tolerance
        and flow.end_soc_kwh < ceiling - _EMPTY_TOLERANCE
    ):
        raise SolveError(
            "solver_failure",
            "invalid solver result: charge-from-PV exports below its limit",
        )


def validate_modes(
    problem: Problem, flows, bridges=None, flex_max: Sequence[float] = ()
) -> frozenset[int]:
    """Independently certify labels, activity, coverage and carried commitments.

    Returns the rows of a HOLD released from a PV-follow commitment whose pack was
    depleted, for as long as that HOLD runs: the coordinator carries that release
    (`initial_dispatch_released`).

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

    `flex_max` gives, per interval, the largest draw (kWh) of the flexible load the
    plan was solved with; `problem` then holds the load the plan realised. It widens
    the follow checks to the bound of the rows a flexible load adds. Empty: none.
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
                return _certify_modes(problem, flows, reading, flex_max)
            except SolveError as error:
                failure = failure or error
        raise failure
    return _certify_modes(problem, flows, frozenset(bridges or ()), flex_max)


def _certify_modes(
    problem: Problem, flows, bridges: frozenset[int], flex_max: Sequence[float] = ()
) -> frozenset[int]:
    """Certify the flows of a plan that reads the committed idle rows in `bridges` as bridges."""
    if not problem.battery:
        return frozenset()
    if not problem.minimum_mode_minutes:
        for index, (slot, flow) in enumerate(zip(problem.slots, flows, strict=True)):
            mode = machine_state(slot, flow)
            flexible = flex_max[index] if flex_max else 0.0
            _check_load_following(problem, slot, flow, mode, flexible)
            _check_surplus_following(problem, slot, flow, mode, index, flexible)
        return frozenset()
    previous, since = _initial(problem)
    dwell = timedelta(minutes=problem.minimum_mode_minutes)
    coverage_end = problem.slots[-1].end.astimezone(UTC)
    drains = drain_schedule(problem)
    carried = dwell_group(previous) == "PV_FOLLOW"
    released = full_seen = False
    owed = _owed(problem, drains, since.astimezone(UTC) + dwell) if carried else ()
    # Without the terminal rule: the floor a HOLD released from a full pack goes back on.
    physical = (
        _owed(problem, drains, since.astimezone(UTC) + dwell, terminal=False)
        if carried
        else ()
    )
    idle = idle_slots(problem) if carried else frozenset()
    # The HOLD released from a depleted pack before the first interval goes on: it starts
    # no clock, and the commitment's deadline stays the only one.
    emptied = resumed = _released_start(problem)
    if emptied:
        previous, carried, released = "HOLD", False, True
    rows = []
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
            and abs(discharge) <= noise
            and -noise <= charge <= surplus + noise,
        }
        if flow.dispatch_mode != mode or not valid[mode]:
            raise SolveError(
                "solver_failure", "invalid solver result: physical dispatch mode"
            )
        flexible = flex_max[index] if flex_max else 0.0
        _check_load_following(problem, slot, flow, mode, flexible)
        _check_surplus_following(problem, slot, flow, mode, index, flexible)
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
        ends = spent = False
        if dwell_group(mode) != dwell_group(previous):
            if previous is not None and start < since.astimezone(UTC) + dwell:
                # A carried PV-follow commitment ends once the battery cannot go on:
                # too empty to discharge, and not where CHARGE_PV could take a PV
                # surplus, or too full to charge. It ends in HOLD; a full pack may also
                # curtail what the site cannot pass, and so may the HOLD released from a
                # commitment whose pack has been full.
                full = left >= _lock_ceiling(problem, drains, index) - _EMPTY_TOLERANCE
                spent = (
                    carried
                    and mode == "HOLD"
                    and not _takes_surplus(problem, slot)
                    and left
                    <= _lock_floor(problem, drains, owed, index) + _EMPTY_TOLERANCE
                )
                ends = spent or (carried and mode == "HOLD" and full)
                curtails = mode == "CURTAIL" and (
                    carried
                    and full
                    or released
                    and full_seen
                    or _beyond_the_pack(problem, slot) > _ROW_TOLERANCE
                )
                # A CURTAIL where the site cannot pass the PV takes a HOLD with a clock of
                # its own over (not one released from a commitment, which has none): the
                # HOLD has no legal flow there, and the run's own clock goes on.
                takes_over = (
                    previous == "HOLD"
                    and not released
                    and mode == "CURTAIL"
                    and curtail_need(problem, slot) > 0
                )
                # A site that cannot curtail spills only into a pack that cannot take the
                # PV (the plan's gate, certified with the flows): a PV-follow run that
                # fills the pack hands over to that spill.
                hands_over = (
                    mode == "CURTAIL"
                    and dwell_group(previous) == "PV_FOLLOW"
                    and not problem.site.allow_curtailment
                )
                # Where there is no such PV left the run that filled the pack ends in a
                # HOLD with its own clock (a carried one is released above).
                fills = (
                    mode == "HOLD"
                    and not carried
                    and dwell_group(previous) == "PV_FOLLOW"
                    and not problem.site.allow_curtailment
                    and full
                )
                # A HOLD released from a depleted pack goes back to the commitment: the
                # HOLD has no legal flow for PV the site cannot pass, and the pack is
                # what takes it. A HOLD released from a full pack does not (the pack
                # takes it where it has room again, and the coordinator carries that
                # HOLD with a clock of its own).
                resumes = (
                    released
                    and dwell_group(mode) == "PV_FOLLOW"
                    and (
                        left
                        <= _lock_floor(problem, drains, physical, index)
                        + _EMPTY_TOLERANCE
                        # Or, for the run released from a depleted pack (the coordinator
                        # carries it): a pack the terminal rule has spent, or PV the site
                        # cannot pass, the pack may have been charged since.
                        or emptied
                        and (
                            left
                            <= _lock_floor(problem, drains, owed, index)
                            + _EMPTY_TOLERANCE
                            or curtail_need(problem, slot) > 0
                        )
                        # The release the coordinator carried: its pack was depleted.
                        or resumed
                        and not index
                    )
                )
                if not (
                    ends or curtails or takes_over or hands_over or fills or resumes
                ):
                    raise SolveError(
                        "solver_failure", "invalid solver result: minimum mode duration"
                    )
                if resumes:
                    carried, released, emptied = True, False, False
                    previous = mode
                    left = flow.end_soc_kwh
                    continue
            if mode in ACTIVE and coverage_end < start + dwell:
                raise SolveError(
                    "solver_failure",
                    "invalid solver result: active mode beyond coverage",
                )
            # A HOLD released from the commitment starts no clock of its own: the
            # commitment's deadline stays the only one.
            carried, released, emptied = False, ends, ends and spent
            if not ends:
                since = start
        previous = mode
        left = flow.end_soc_kwh
        if emptied and mode == "HOLD":
            rows.append(index)
    return frozenset(rows)
