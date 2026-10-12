"""Resolve the net-billing settlement factors for one calculation (ADR-0001).

`resolve_settlement` is pure: the coordinator persists the returned `state` in the
`settlement` Store and hands it back on the next calculation.
"""

from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from .engine.settlement import (
    auto_pressure,
    factors,
    hold_pressure,
    manual_pressure,
)

# Keys whose change re-evaluates at once, without the dead band.
_INPUT_KEYS = (
    ("model", "deposit_model"),
    ("refund_percent", "deposit_refund_percent"),
    ("manual_factor", "export_value_factor"),
    ("yearly_deposit", "deposit_yearly_value"),
    ("yearly_energy_cost", "deposit_yearly_energy_cost"),
)


def _inputs(values: Mapping[str, object]) -> dict:
    return {name: values[key] for name, key in _INPUT_KEYS}


def resolve_settlement(
    values: Mapping[str, object],
    held: Mapping[str, object] | None,
    now: datetime,
    timezone: str,
    *,
    split_known: bool = True,
) -> tuple[dict, dict | None]:
    """Return (diagnostics for `dispatch_policy.settlement`, state to persist or None).

    `split_known` is False when no slot carries a distribution part: the whole buy
    price would then count as deposit-paid, so φe is forced to 1 (`split_missing`).
    """
    today = now.astimezone(ZoneInfo(timezone)).date().isoformat()
    inputs = _inputs(values)
    rho = inputs["refund_percent"] / 100
    deposit, energy = inputs["yearly_deposit"], inputs["yearly_energy_cost"]
    unused = None
    pressure = 0.0
    manual_export = None
    reason = "ok"
    if inputs["model"] == "off":
        reason = "off"
    elif inputs["model"] == "manual":
        pressure = manual_pressure(inputs["manual_factor"], rho)
        manual_export = inputs["manual_factor"] if rho < 1 else 1.0
    elif deposit <= 0 or energy <= 0:
        reason = "inputs_missing"
    else:
        candidate, unused = auto_pressure(deposit, energy, rho)
        kept = (
            held is not None
            and held.get("inputs") == inputs
            and isinstance(held.get("cap_pressure"), int | float)
            and not isinstance(held.get("cap_pressure"), bool)
            and isinstance(held.get("evaluated_on"), str)
        )
        if kept and held["evaluated_on"] == today:
            pressure, reason = float(held["cap_pressure"]), "held"
            unused = held.get("unused_share", unused)
        elif kept:
            pressure = hold_pressure(float(held["cap_pressure"]), candidate)
        else:
            pressure = candidate
    export_factor, energy_factor = factors(pressure, rho)
    if manual_export is not None:
        # φx is the setting itself; only π (and so φe) is clamped to [0, 1].
        export_factor = manual_export
    evaluated_on = held["evaluated_on"] if reason == "held" else today
    if pressure > 0 and not split_known:
        energy_factor, reason = 1.0, "split_missing"
    result = {
        "model": inputs["model"],
        "export_value_factor": export_factor,
        "import_energy_factor": energy_factor,
        "cap_pressure": pressure,
        "unused_share": unused,
        "refund_percent": inputs["refund_percent"],
        "yearly_deposit": deposit,
        "yearly_energy_cost": energy,
        "distribution_mode": values["buy_distribution_mode"],
        "evaluated_on": evaluated_on,
        "reason": reason,
    }
    if inputs["model"] != "auto" or reason == "inputs_missing":
        return result, None
    return result, {
        "inputs": inputs,
        "cap_pressure": pressure,
        "unused_share": unused,
        "evaluated_on": evaluated_on,
    }
