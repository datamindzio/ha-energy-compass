"""Net-billing settlement model (ADR-0001): planning prices and cap pressure.

Only the planning objective sees these prices. Reported costs stay at invoice
prices (`Plan.grid_cost`, `plan_monetary_cost`).
"""

from .models import Problem, Slot

# Fraction of rho at which the unused deposit starts to press on the refund cap.
_RAMP_START = 0.5
# Smallest change of the held cap pressure that replaces it (hysteresis).
DEAD_BAND = 0.10


def planning_buy(problem: Problem, slot: Slot) -> float:
    """Buy price as the planner sees it: the deposit-payable part scaled by φe."""
    if problem.import_energy_factor == 1.0:
        return slot.buy_per_kwh
    return slot.buy_per_kwh - (1 - problem.import_energy_factor) * min(
        slot.buy_energy_per_kwh, max(slot.buy_per_kwh, 0.0)
    )


def planning_sell(problem: Problem, slot: Slot) -> float:
    """Sell price as the planner sees it: φx scales the positive part, a negative stays whole."""
    if problem.export_value_factor == 1.0:
        return slot.sell_per_kwh
    return problem.export_value_factor * max(slot.sell_per_kwh, 0.0) + min(
        slot.sell_per_kwh, 0.0
    )


def factors(cap_pressure: float, refund_fraction: float) -> tuple[float, float]:
    """(export_value_factor φx, import_energy_factor φe) for a cap pressure π."""
    return 1 - cap_pressure * (1 - refund_fraction), 1 - cap_pressure


def auto_pressure(
    yearly_deposit: float, yearly_energy_cost: float, refund_fraction: float
) -> tuple[float, float]:
    """(π, unused share s) from the 12-month deposit D and payable charges E."""
    unused = min(max(1 - yearly_energy_cost / yearly_deposit, 0.0), 1.0)
    if refund_fraction >= 1:
        return 0.0, unused
    if refund_fraction <= 0:
        return (1.0 if unused > 0 else 0.0), unused
    start = refund_fraction * _RAMP_START
    pressure = (unused - start) / (refund_fraction - start)
    return min(max(pressure, 0.0), 1.0), unused


def manual_pressure(export_value_factor: float, refund_fraction: float) -> float:
    """π that reproduces a user-stated export value factor (clamped to [ρ, 1])."""
    if refund_fraction >= 1:
        return 0.0
    return min(max((1 - export_value_factor) / (1 - refund_fraction), 0.0), 1.0)


def hold_pressure(held: float | None, candidate: float) -> float:
    """Dead band: keep the held π unless the candidate moves ≥ 0.10 or hits 0 / 1."""
    if held is None:
        return candidate
    if abs(candidate - held) >= DEAD_BAND - 1e-12:
        return candidate
    if candidate in (0.0, 1.0) and candidate != held:
        return candidate
    return held
