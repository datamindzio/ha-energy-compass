# ADR-0009: A HOLD or CURTAIL interval at a peak price is charged the forecast miss it buys

Status: accepted
Date: 2026-10-14

## Context

On 2026-10-14 (08:15-11:00, G12 buy 1.242, `cost_min`) the plan held the pack through the morning peak for a
modelled gain of about 0.08 kWh of exported PV per interval, at 1.08 instead of stored. The house drew
1.10-1.27 kW against a forecast of 0.76 kW and imported 0.72-0.86 kW at 1.242 with the battery at 0 W. A kWh of the
pack was worth at most `max later sell × eta_discharge` = 1.11 and refilling it at the 13:00 cheap slot cost
0.64, so covering the miss from the pack was worth 0.5-0.6 per kWh and HOLD only protected energy the plan could
rebuild cheaper (T-091).

HOLD and CURTAIL are the two modes that discharge nothing. The model priced neither for the miss they leave to
the grid.

## Decision

`engine/hold_exposure.py` gives each interval with PV above its load a planning-only cost on `mode_HOLD` and
`mode_CURTAIL`: `hold_miss_share × load × max(buy - worth, 0) × import_weight`, `worth` being the smaller of
the best later use of a pack kWh and the cheapest later refill the pack has room for (formula and limits in
[model.md](../model.md#hold-and-curtail-at-a-peak-price)). `hold_miss_share` is a `Problem` field, default 0; the
runtime sets 0.5 (`HOLD_FORECAST_MISS`) on every plan.

- It is a price, not a ban: a HOLD worth more than the charge is still planned.
- CURTAIL is priced with HOLD. With CURTAIL free the plan replaced a HOLD by a CURTAIL of the minimum activity
  (100 W) and was no better off.
- Only intervals with PV above the load are charged. With a deficit the model already imports all of it at the
  buy price in a HOLD.
- The charge is outside `Plan.objective`, `grid_cost` and every published cost, like the import penalty.
- The share is a constant, not a setting: no site-specific value is known, and 0.5 is the measured miss of the
  live case rounded down (0.76 kW forecast, 1.10-1.27 kW measured).
- The default 0 keeps the pinned `cost_min` solver model of `tests/test_strategy_golden.py` byte for byte; the
  golden file is unchanged.

## Consequences

- A PV surplus below about `share × load × (buy - worth) / (sell - worth)` per interval is stored instead of
  exported where HOLD would have exported it; the modelled cost of the plan rises by the difference between the
  export price and the refill cost on that energy (+1.03 on the 2026-10-14 test day, about 0.03 per interval at the
  live surplus).
- Solve time. At the default 96 slots nothing changes. At 192 slots (48 h) the benchmark's six strategies are all
  `time_limited` at 10 s (before: `cost_min`, `pv_swap`, `max_export`, `grid_friendly` proved in 0.5-0.9 s), with
  the plan cost +0.4 % for `cost_min`. Pricing HOLD alone proves `cost_min` in 3.8 s but leaves the CURTAIL alias.
  The base plan publishes its incumbent at the limit (ADR-0003); probes at 192 slots did not complete before
  (38 of 96) and do not now. A tighter formulation of the charge is open work; a shorter horizon or a lower
  share are the levers.
- A pack with no room to refill (full) is charged only the round trip, so HOLD stays available.

## Alternatives rejected

- A hard ban of HOLD in peak intervals: a full pack or a reserve floor may need it.
- A fixed epsilon on HOLD: the right size depends on the load and the price gap, and a constant either misses the
  live case or prices every HOLD.
- Fixing it in the controller: it must not second-guess the plan.
