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
- Solve time. At the default 96 slots nothing changes. At 192 slots (48 h) the charge as first merged (T-091) made all six benchmark strategies `time_limited` at 10 s (before: `cost_min`, `pv_swap`, `max_export`, `grid_friendly` proved in 0.5-0.9 s), the plan cost of `cost_min` +0.4 %. T-094 found the cause and removed it (Amendment below): with it `cost_min`, `pv_swap`, `max_export` and `grid_friendly` prove again in 1.4-2.7 s; `self_sufficiency` and `backup_ready` were `time_limited` without the charge too. A random corpus of 24-48 quarter-hours costs a quarter more solve time (92.6 s to 117.7 s) than the first version (94.9 s). Probes at 192 slots did not all complete before (38 of 96, and 14 of 16 without the charge in the T-094 benchmark) and complete 11 of 16 now.
- A pack with no room to refill (full) is charged only the round trip, so HOLD stays available.

## Alternatives rejected

- A hard ban of HOLD in peak intervals: a full pack or a reserve floor may need it.
- A fixed epsilon on HOLD: the right size depends on the load and the price gap, and a constant either misses the
  live case or prices every HOLD.
- Fixing it in the controller: it must not second-guess the plan.

## Amendment T-094: a HOLD that cannot be a charge is not charged

T-091's charge made every 192-slot plan `time_limited` (`cost_min` 0.84 s to the 10 s limit at a weighted gap of 5.5 %). Findings, on `benchmarks/optimizer.py --slots 192 --battery-export --accept-incumbent`:

- It is not the size of the charge: `cost_min` takes 1.9-2.7 s and gives the same plan for `--hold-miss-share` 0.1 to 1, and the time grows with the number of priced intervals (of the 34 priced ones, only the first 2, 4, 8 or 16 priced: 1.2, 1.2, 0.7, 1.0 s to prove; only the last 4, 12 or 24: 1.2, 2.0 s and a limit at a gap of 0.4 %; all 34: 28 s).
- It is not the CURTAIL alias, and it is not a tie between equal charges (a charge perturbed by 1-30 % per interval does not help). Leaving CURTAIL unpriced, or crediting a CURTAIL by its direction flag, proves in 3.8 s, but only because the plan then takes 15 CURTAIL intervals that the charge no longer prices: that is the alias this ADR prices.
- It is the charge on a HOLD the plan cannot avoid. Once the day's PV has filled the pack, the rest of the PV intervals can only be HOLD (or a battery export), each one charged `exposure`, and the relaxation avoids the sum with a fraction of a CHARGE_PV in each. The integer plan pays it, the bound does not: a weak bound, an unprovable gap.

Decision: where the pack has less room at the start of the interval than the PV-follow charge would store, the charge is given back (`model.md` § HOLD and CURTAIL at a peak price: `hold_full`, `hold_relief`). Priced HOLD and CURTAIL remain priced where the pack has room; nothing is added for an interval the pack cannot reach that level in (`highest`), so a site with a large pack gets no new variables.

Not changed:

- The room for a refill (`hold_exposure`) is read from `initial_kwh` only. A plan that empties the pack before the cheap slot would have room that the charge does not count, so the refill reads as impossible and the worth of a kWh is its later use (higher): the charge is smaller than a trajectory-aware one, never larger. A trajectory-aware room needs the plan's own energy in the price, a term in the objective that is not linear; it is open.
- The share 0.5 comes from one day (2026-10-14). On the benchmark day and on the T-091 test day the plan is the same for 0.1 to 1, so it is a tie-break of the right order, not a tuned value; a site with a different forecast error is where it would be wrong, and a setting for it is not warranted by one measurement.

Measured at `main` 3c12242 with T-091 merged in, three runs each (`docs/model.md` has the table): `cost_min` 1.73 s, `pv_swap` 1.37 s, `max_export` 2.29 s, `grid_friendly` 2.61 s optimal at 192 slots with the share 0.5, against 0.84, 0.53, 0.90, 0.83 s with the share 0. The corpus of 144 random problems of 24-48 quarter-hours has the same plan-cost distribution as before the change (66 unchanged, median 0, p90 0.26) and a quarter more solve time.
