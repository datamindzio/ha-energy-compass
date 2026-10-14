"""Planning cost of a HOLD interval: the forecast miss it would buy at the grid price."""

from .charge_price import price_allows_grid_charge
from .models import Problem
from .settlement import planning_buy, planning_sell

# The share of an interval's forecast load a HOLD leaves uncovered by the pack. HOLD
# discharges nothing, so whatever the forecast under-states (load above it, PV below it)
# is imported at the interval's price; SELF_CONSUME and CHARGE_PV would have drawn it from
# the pack. Live 2026-10-14 08:15-11:00 the house drew 1.10-1.27 kW against a forecast of
# 0.76 kW, and the pack stood idle while 0.72-0.86 kW was imported at the peak price.
# Half the forecast load is that miss rounded down; it is a penalty on a tie, not a ban,
# so a HOLD that is worth more than it (a full pack, a reserve to keep) is still chosen.
# The engine prices nothing until a `Problem` carries it (`hold_miss_share`, default 0, so
# the pinned `cost_min` solver model of the golden snapshot is unchanged); the runtime
# sets this share on every plan it makes.
HOLD_FORECAST_MISS = 0.5


def hold_exposure(problem: Problem) -> tuple[float, ...]:
    """Per interval, what a HOLD is charged for the miss it would buy at the grid price.

    `hold_miss_share x load x (buy - worth of a pack kWh later)`, nothing where the buy price
    is not above that worth. A kWh kept in the pack is worth at most what the plan can
    do with it later: sell it (`eta_discharge x` the best later sell, with battery export
    on) or avoid an import in a later interval whose load is above its PV (`eta_discharge
    x` that buy price). It is worth no more than refilling it costs: the cheapest later
    grid-charge slot (`buy / eta_charge`) while the pack has room for a slot's charge
    below its ceiling. HOLD is then protecting energy the plan can rebuild cheaper, and
    the miss it leaves to the grid costs the difference. Only an interval with PV above
    its load is charged: there SELF_CONSUME is closed (no net load to cover) and HOLD
    competes with a PV-follow charge for a few watts of exported surplus. Where the load
    is above PV, HOLD already imports the whole deficit at the buy price in the model and
    the pack covers it unless it is worth more than that. With no later use to compare
    with (and no terminal value) nothing is charged. A model-only term: the published objective
    holds none of it.
    """
    battery = problem.battery
    slots = problem.slots
    if battery is None or problem.hold_miss_share <= 0:
        return (0.0,) * len(slots)
    ceiling = battery.capacity_kwh * battery.maximum_soc_fraction
    room = ceiling - battery.initial_kwh
    refill_open = battery.allow_grid_charge
    exposure = [0.0] * len(slots)
    # Walked from the end: the best use and the cheapest refill among later intervals. A
    # kWh left in the pack at the end is worth the terminal value when the plan has one.
    best_use = (
        problem.terminal_value_per_kwh if problem.terminal_mode == "value" else None
    )
    cheapest_refill = None
    for index in range(len(slots) - 1, -1, -1):
        slot = slots[index]
        if best_use is not None:
            worth = best_use
            if cheapest_refill is not None:
                worth = min(worth, cheapest_refill)
            excess = planning_buy(problem, slot) - worth
            if excess > 0 and slot.pv_kwh > slot.load_kwh:
                exposure[index] = (
                    problem.import_weight
                    * problem.hold_miss_share
                    * slot.load_kwh
                    * excess
                )
        uses = []
        if battery.allow_battery_export:
            uses.append(planning_sell(problem, slot))
        if slot.load_kwh > slot.pv_kwh:
            uses.append(planning_buy(problem, slot))
        if uses:
            use = battery.eta_discharge * max(uses)
            best_use = use if best_use is None else max(best_use, use)
        hours = (slot.end - slot.start).total_seconds() / 3600
        if (
            refill_open
            and price_allows_grid_charge(problem, slot)
            and room >= battery.charge_kw * hours - 1e-9
            and battery.charge_kw > 0
        ):
            refill = planning_buy(problem, slot) / battery.eta_charge
            cheapest_refill = (
                refill if cheapest_refill is None else min(cheapest_refill, refill)
            )
    return tuple(exposure)
