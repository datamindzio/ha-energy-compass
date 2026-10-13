"""Flow-derived operating states shared by planning and display."""

from .models import Flow, MachineState, Slot

# HiGHS accepts a binary within 1e-6 of 0/1 (mip_feasibility_tolerance) and scipy
# cannot tighten it, so a flow a mode indicator gates can carry that much noise.
_TOL = 1e-6


def noise_bound(upper: float) -> float:
    """The most a flow bounded by `upper` carries on indicators the solver left at noise.

    A `flow <= M * binary` row accepts the binary within 1e-6 of 0 and itself within
    1e-6, so a flow gated by two unselected indicators can carry 1e-6 * (M1 + M2 + 1),
    and no M exceeds the variable's own bound.
    """
    return _TOL * (2 * upper + 1)


def activity_tolerance(activity: float) -> float:
    """How far below its minimum activity a solved flow may sit and still count."""
    return min(_TOL, activity * 1e-4)


def machine_state(slot: Slot, flow: Flow) -> MachineState:
    """Choose one display state by curtail, charge, export, discharge precedence."""
    if flow.curtail_kwh > _TOL:
        return "CURTAIL"
    if flow.charge_kwh > max(slot.pv_kwh - slot.load_kwh, 0) + _TOL:
        return "CHARGE_GRID"
    if flow.charge_kwh > _TOL:
        return "CHARGE_PV"
    if flow.discharge_kwh > _TOL and flow.grid_export_kwh > _TOL:
        return "DISCHARGE_GRID"
    if flow.discharge_kwh > _TOL:
        return "SELF_CONSUME"
    return "HOLD"
