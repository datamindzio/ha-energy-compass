"""Resolve the net-billing settlement factors for one calculation (ADR-0001)."""

from collections.abc import Mapping

from .engine.settlement import factors, manual_pressure, refund_share


def resolve_settlement(
    values: Mapping[str, object], *, split_known: bool = True
) -> dict:
    """Return the diagnostics for `dispatch_policy.settlement`.

    `split_known` is False when no slot carries a distribution part: the whole buy
    price would then count as deposit-paid, so φe is forced to 1 (`split_missing`).
    """
    model = values["deposit_model"]
    refund_percent = values["deposit_refund_percent"]
    share = refund_share(refund_percent)
    pressure = 0.0
    manual_export = None
    reason = "ok"
    if model == "off":
        reason = "off"
    else:
        manual_export = values["export_value_factor"]
        pressure = manual_pressure(manual_export, share)
    export_factor, energy_factor = factors(pressure, share)
    if manual_export is not None:
        # φx is the setting itself; only π (and so φe) is clamped to [0, 1].
        export_factor = manual_export
    if pressure > 0 and not split_known:
        energy_factor, reason = 1.0, "split_missing"
    return {
        "model": model,
        "export_value_factor": export_factor,
        "import_energy_factor": energy_factor,
        "cap_pressure": pressure,
        "refund_percent": refund_percent,
        "distribution_mode": values["buy_distribution_mode"],
        "reason": reason,
    }
