"""Atlas `Solve` payload builder (ADR-0019 §6), run inside the solve's own executor job.

Returns `None` (never sent, `solves_skipped` incremented by the caller) for a solve that
cannot be expressed as one uniform-step Atlas solve: no slots, non-uniform slots left after
dropping a short leading slot, or a `state`/`consumption_level` outside the contract enums.
"""

_STATE_ENUM = {
    "CHARGE_GRID",
    "CHARGE_PV",
    "SELF_CONSUME",
    "DISCHARGE_GRID",
    "HOLD",
    "CURTAIL",
}
_LEVEL_ENUM = {"BOOST", "CHEAP", "NORMAL", "LIMIT"}


def _consumption_level(opportunities, at) -> str | None:
    for item in opportunities:
        if item.start <= at < item.end:
            return "NORMAL" if item.level is None else item.level
    return None


def build_solve_payload(
    problem, plan, analysis, values: dict, config: dict, now
) -> dict | None:
    slots = problem.slots
    flows = plan.flows
    if not slots or len(flows) != len(slots):
        return None

    def duration_s(slot):
        return round((slot.end - slot.start).total_seconds())

    # The step is set by the second slot when there are at least two, so that at
    # most one leading short slot (a horizon starting mid-slot) is ever dropped;
    # anything else not matching it makes the run non-uniform (ADR-0019 §6).
    step_s = duration_s(slots[1]) if len(slots) > 1 else duration_s(slots[0])
    if step_s <= 0:
        return None
    start_index = 1 if len(slots) > 1 and duration_s(slots[0]) != step_s else 0
    kept_slots = slots[start_index:]
    kept_flows = flows[start_index:]
    if not kept_slots:
        return None
    if any(
        round((slot.end - slot.start).total_seconds()) != step_s for slot in kept_slots
    ):
        return None
    state = kept_flows[0].dispatch_mode
    consumption_level = _consumption_level(analysis.opportunities, kept_slots[0].start)
    if state not in _STATE_ENUM or consumption_level not in _LEVEL_ENUM:
        return None
    soc0_kwh = (
        (
            problem.battery.initial_kwh
            if start_index == 0
            else flows[start_index - 1].end_soc_kwh
        )
        if problem.battery
        else 0.0
    )
    params = {
        "strategy": problem.strategy,
        "terminal_mode": problem.terminal_mode,
        "terminal_value_per_kwh": problem.terminal_value_per_kwh,
        "boost_ceiling": values.get("boost_ceiling"),
        "limit_floor": values.get("limit_floor"),
        "cheap_percentile": values.get("cheap_percentile"),
        "limit_percentile": values.get("limit_percentile"),
        "minimum_mode_minutes": values.get("minimum_mode_minutes"),
        "minimum_mode_power_kw": values.get("minimum_mode_power_kw"),
    }
    if problem.battery:
        params.update(
            eta_charge=problem.battery.eta_charge,
            eta_discharge=problem.battery.eta_discharge,
            wear_per_kwh=problem.battery.wear_per_kwh,
        )
    return {
        "solve_ts": now.isoformat(),
        "horizon_start": kept_slots[0].start.isoformat(),
        "step_s": step_s,
        "state": state,
        "consumption_level": consumption_level,
        "solver_status": "time_limited" if plan.time_limited else "optimal",
        "plan": {
            "g_in": [flow.grid_import_kwh for flow in kept_flows],
            "g_out": [flow.grid_export_kwh for flow in kept_flows],
            "b_c": [flow.charge_kwh for flow in kept_flows],
            "b_d": [flow.discharge_kwh for flow in kept_flows],
            "curt": [flow.curtail_kwh for flow in kept_flows],
            "soc": [flow.end_soc_kwh for flow in kept_flows],
        },
        "inputs": {
            "buy": [slot.buy_per_kwh for slot in kept_slots],
            "sell": [slot.sell_per_kwh for slot in kept_slots],
            "pv": [slot.pv_kwh for slot in kept_slots],
            "load": [slot.load_kwh for slot in kept_slots],
            "soc0_kwh": soc0_kwh,
            "params": params,
        },
    }
