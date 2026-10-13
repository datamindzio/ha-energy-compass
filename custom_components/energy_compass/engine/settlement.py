"""Net-billing settlement model (ADR-0001): planning prices and cap pressure.

Only the planning objective sees these prices. Reported costs stay at invoice
prices (`Plan.grid_cost`, `plan_monetary_cost`).
"""

from .models import Problem, Slot

# The deposit coefficient inside a sell price: the refund share is a share of the
# export value without it, so on that price the share is `deposit_refund_percent /
# 100 / DEPOSIT_COEFFICIENT` (ADR-0001 revision 3).
DEPOSIT_COEFFICIENT = 1.23


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


def refund_share(refund_percent: float) -> float:
    """ρp: the refund share expressed on a sell price that contains the 1.23 coefficient."""
    return refund_percent / 100 / DEPOSIT_COEFFICIENT


def factors(cap_pressure: float, refund_share: float) -> tuple[float, float]:
    """(export_value_factor φx, import_energy_factor φe) for a cap pressure π."""
    return 1 - cap_pressure * (1 - refund_share), 1 - cap_pressure


def manual_pressure(export_value_factor: float, refund_share: float) -> float:
    """π that reproduces a user-stated export value factor (clamped to [ρp, 1])."""
    # Guard for direct callers: the settings cap the share at 1 / 1.23.
    if refund_share >= 1:
        return 0.0
    return min(max((1 - export_value_factor) / (1 - refund_share), 0.0), 1.0)
