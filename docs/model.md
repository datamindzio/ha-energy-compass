# Advisory dispatch model

`solve(problem, time_limit_s=10.0)` minimizes forecast physical energy cost plus the configured additional-export-period decision reserve for contiguous slots. Each slot has grid import and export, battery charge and discharge, solar curtailment, and end-of-slot stored energy, all measured in kWh. A battery-free problem keeps only grid and solar variables. The caller supplies finite grid connection limits even without a battery.

The energy balance is `PV - curtailment + grid import + battery discharge = load + grid export + battery charge`. Battery energy advances by `charge efficiency × charge - discharge ÷ discharge efficiency`. Every slot observes the operating reserve and maximum stored energy, energy limits from charge and discharge power multiplied by elapsed hours, and the import and export connection limits. Binary direction choices prevent simultaneous grid import/export and battery charge/discharge. The inverter limits the magnitude of `PV - curtailment + battery discharge - battery charge`; it is separate from the whole-house grid connection limits. Curtailment is fixed to zero if the site cannot curtail.

An observed initial SOC below the operating reserve, including zero, is valid. The measured energy is never clamped upward. While below reserve, the plan can wait or charge gradually without a forced first-slot refill; it cannot discharge. Once charged above reserve, discharge may use only the excess and must finish at or above the full reserve. Partial recovery below reserve cannot be spent later. The independent result validator checks each interval against the lesser of its starting energy and the configured reserve. Negative, nonfinite, and above-ceiling observations remain invalid; source freshness, jump and BMS consistency checks are unchanged.

Nonnegative source allocations split solar among load, battery and export; grid import among load and battery; and battery discharge among load and export. The `Flow` reports grid import/export, charge/discharge, curtailment, final stored energy, a verified `dispatch_mode`, and the legacy `battery_mode` direction diagnostic. A display can infer charging, discharging, grid exchange and curtailment from those aggregate values. Charging beyond `max(PV - load, 0)` identifies grid-fed charging. When grid charging is disabled, its allocation is zero and charge cannot exceed that solar surplus. When battery export is disabled, its export allocation is zero and discharge cannot exceed `max(load - PV, 0)`. These conservative bounds use forecast input values and prevent source relabeling from bypassing capability restrictions.

The reported physical objective is `sum(buy × import - sell × export + wear × (charge + discharge) ÷ 2) - terminal credit`. Wear is charged per kWh of average charge/discharge throughput, so a complete 1 kWh charge/discharge cycle incurs one wear unit. The default `preserve_initial` mode requires final stored energy at least the initial energy and has no terminal credit. The explicit `value` mode credits final stored energy at `terminal_value_per_kwh`; its `Plan.terminal_credit` and `Plan.objective` expose this choice to diagnostics. A battery-free plan has no terminal credit or throughput charge. For a new installation, the PL net-billing setup profile pre-fills `terminal_mode` `value` and `terminal_value_per_kwh` 0.60, backtested on one site; see [Setup profiles](guide.en.md#setup-profiles-new-installations).

`remaining_daily_throughput_kwh` contains raw remaining charge-plus-discharge energy budgets by local date, keyed by canonical `YYYY-MM-DD` strings. A limit of `2 × capacity` kWh corresponds to one equivalent full cycle, including throughput already observed before planning. The caller subtracts observed throughput before supplying the remaining budget. Slot order and duration use UTC instants, so a repeated local hour remains valid. Slots crossing local midnight contribute to each date in proportion to actual elapsed overlap; a daylight-saving day can have 23 or 25 hours. Dates absent from the mapping have no solver throughput bound.

The model uses AC-equivalent PV and battery flows. This approximation must be checked against measured energy, battery energy changes, and inverter behavior before any control work. The solver is advisory: it never writes device controls. SciPy HiGHS normally reports a proven optimum within the time limit. For the base plan only, a time limit reached with a feasible incumbent publishes that incumbent after the same independent validation, marked `Plan.time_limited` with the solver's final relative gap in `Plan.mip_gap` (both exposed as plan attributes); the incumbent is often already optimal and only the proof is unfinished. A time limit without an incumbent, and every probe, flexible-load and configuration-check solve, still raise `SolveError("timeout")`; infeasibility, unboundedness and solver failure raise `SolveError` as before. The returned variables are independently checked against every interval balance, capability, direction and bound within an absolute 1e-6 kWh tolerance.

## Operating-mode duration and PV export budget

The Planning setting `minimum_mode_minutes` defaults to 60; zero disables mode duration and mode minimum-active-power restrictions; the separate export-benefit detector below may still enforce material battery export. `minimum_mode_power_kw` defaults to 0.1 kW (100 W), accepts finite values from 0.001 to 1000 kW, and permits variable power above that floor. Restrictions apply only with a battery. Native source and settlement slots remain unchanged; all timing uses elapsed UTC time, including partial first slots and DST.

For each interval of `h` hours, `activity = minimum_mode_power_kw × h` and `surplus = max(PV - load, 0)`. Exactly one physical mode is selected:

| Mode | Actual required flows |
| --- | --- |
| CHARGE_GRID | Charge at least surplus + activity; discharge and curtailment zero |
| CHARGE_PV | Charge between activity and surplus; discharge and curtailment zero |
| DISCHARGE_GRID | Discharge and grid export each at least activity; charge and curtailment zero |
| SELF_CONSUME | Discharge at least activity; charge, grid export and curtailment zero |
| HOLD | Charge, discharge and curtailment zero |
| CURTAIL | Curtailment at least activity; charge and discharge zero |

The CURTAIL restriction is deliberately conservative: curtailment cannot mask a battery mode transition. Duration zero retains the previous simultaneous-curtailment behavior. Tiny intervals whose activity is at or below 2e-6 kWh cannot carry a material mode because the classifier cannot reliably resolve it; only truthful HOLD is available, or the plan is infeasible.

Each newly started mode lasts at least the configured duration. CHARGE_PV and SELF_CONSUME form one PV-follow duration group: in both the inverter charges from PV surplus and covers a PV deficit from the battery, and a controller writes the same registers for both, so a flip between them is not a mode change. Their labels stay flow-derived and truthful; only the clock is shared. A carried CHARGE_PV commitment can therefore continue as SELF_CONSUME when PV drops below load, instead of making the plan or a consumption probe infeasible. Any other source or destination change starts a distinct clock; idle HOLD cannot fund an active run. New active modes must have a full duration of forecast coverage remaining. Final passive HOLD/CURTAIL runs and the remaining part of a carried commitment may be clipped by coverage. The solver never invents future flows.

With duration enabled, auxiliary battery and grid direction flags are tied to the named modes. This strengthens the continuous relaxation without changing feasible physical schedules or the economic objective. CURTAIL retains either grid direction because curtailed production may lie above or below household demand.

The coordinator persists the first accepted, flow-verified named mode and its timezone-aware start. Recalculation of the same mode, or a change within the PV-follow group, preserves its clock; a changed normal mode starts a new clock. Superseded and failed calculations do not modify it. Invalid or future stored clocks are discarded. Legacy `charge`/`discharge` records preserve their remaining direction guard but donate no age: the first named publication starts a fresh clock. The `battery_mode` field is only a legacy diagnostic, not the public duration policy.

Hardware and SoC limits stay hard. An observed-bound safety exception applies when the current observed SoC is already at the upper bound, or at/below the lower reserve, during an unexpired carried active commitment. A conflicting grid-charge price ceiling can also interrupt a carried grid-charge commitment, as described below. Rows then truthfully say HOLD, while persistence retains the interrupted mode and original expiry. No other mode can start before that deadline. `dispatch_policy.safety_exception` exposes the reason, interrupted mode and deadline. A forecast burst to a future bound cannot use this exception; future active runs must sustain their duration or the plan is infeasible. This remains advisory output, not inverter control.

The physical classifier and validator independently check the chosen states, floor, transitions and coverage. `dispatch_policy` reports `mode_scope: actual_operating_mode`, `enabled`, minimum duration and minimum power. All existing balance, capacity, tariff, wear, terminal and daily-throughput constraints remain in force.

**Sell only PV** (`limit_export_to_pv`) is available under **Planning** and defaults to on. The rule applies separately to every **local calendar day in the installation timezone**:

`observed export since midnight + forecast export <= observed PV since midnight + forecast PV - forecast curtailment`.

Direct solar and battery export share that day's budget. Household consumption is not subtracted from generation. Unused credit cannot carry to another date, and tomorrow's generation cannot fund today's export. Intervals crossing midnight are divided by UTC elapsed overlap, including 23- and 25-hour daylight-saving days. Only available forecast coverage counts; missing future PV is never invented. An already observed deficit is retained, not clamped to zero; if remaining generation cannot cover it, the strict daily constraint is infeasible.

When enabled with nonzero grid export capacity, **Sources** must include daily energy counters `pv_energy_today` and `grid_export_energy_today`. Select entity measurements in kWh or Wh that reset at midnight in the same timezone. These are required planning inputs, not lifetime totals or optional diagnostics. Each calculation reads them again, so refresh and restart never reset the used budget. Missing, unavailable, negative, stale or previous-day observations block a new calculation and raise Alert; an already published plan continues within its coverage. Fresh native `last_reported` timestamps support unchanged totals; the configurable age bound defaults to 86400 seconds for these daily counters. New calculations after local midnight wait for current-day reports. A retained plan uses its original day-specific budgets until replacement or the end of its coverage. Disabling Sell only PV, or setting grid export capacity to zero, removes this counter requirement.

Battery provenance is not tracked. Initial SOC and grid-charged energy may be exported within the current day's generation budget, including before forecast production later that same day under the default scope below. This remains an aggregate energy rule and advisory output, not an inverter control or a guarantee about exported energy's origin. Existing battery export capability, SOC, power, wear and operating-mode constraints remain unchanged.

`export_limit_scope` chooses how tightly that daily budget is checked, and only matters while Sell only PV is on:

- `local_day` (default): the whole-day formula above. Grid-charged or carried-over battery energy may be exported before that day's PV is actually produced, as long as the day's total export stays within the day's total PV.
- `produced`: the same budget, checked as a running total instead of a single end-of-day sum. For every planned interval and every local day it overlaps, `observed export today + planned export up to and including this interval <= observed PV today + planned PV up to and including this interval`, floored at zero the same way as the whole-day rule. Export can never run ahead of PV generated so far that day; selling overnight grid charge before sunrise, financed by PV the day has not produced yet, is no longer possible. Credit still never carries between days, and export already above PV from an earlier observation is never retroactively undone, only prevented from growing further. At a day's last interval this running check implies the `local_day` total, so the two scopes agree by end of day; `produced` only tightens *when* the budget may be spent.

The plan's `dispatch_policy` exposes `export_limit_scope` (`local_day` or `produced`, mirroring the setting), `timezone` and `daily_balances`. Each date reports `observed_pv_kwh`, `observed_export_kwh`, `forecast_pv_kwh` (after curtailment), `forecast_export_kwh`, and the unused `remaining_export_kwh`; these whole-day figures are reported the same way under both scopes. Interval rows expose verified `dispatch_mode` alongside the legacy direction diagnostic. Every extra-consumption probe uses the same observed totals and daily constraints.

## Maximum grid-charging price

`limit_grid_charge_price` defaults to false. When enabled, `maximum_grid_charge_price` is a finite amount from −1000 to 1000 in installation currency/kWh. A zero ceiling permits free or negative prices; disabling the switch removes the ceiling. Engine callers use `Problem.maximum_grid_charge_price=None` to disable it. The comparison uses the final `Slot.buy_per_kwh` after source/tariff adjustments, with 1e-9 tolerance solely for floating-point price rounding. Equality is allowed.

Above the ceiling, the optimizer forces grid-to-battery allocation to zero and limits total battery charging to `max(PV − curtailment − load, 0)`. Thus assigning PV to the battery while buying energy for the same household load cannot evade the ceiling. Household import remains available; other physical, terminal, daily-budget and export-benefit constraints continue to apply. Independent solution validation checks both allocation and physical surplus charging.

New CHARGE_GRID periods must meet the ceiling throughout their minimum duration. When a carried CHARGE_GRID commitment has any over-ceiling slot before its original deadline, the existing interruption path emits HOLD immediately through that deadline, with reason `grid_charge_price_limit`. The accepted HOLD keeps the original commitment clock across recalculation and reload. This gives the price ceiling priority over continuing grid charge, even if the first conflicting price is later in the remaining commitment. PV-charge and other mode commitments are unaffected. The price ceiling applies independently of the duration setting; it sends no inverter-control commands.

## Minimum additional export benefit

`minimum_export_episode_benefit` is a finite amount from 0 to 1000 in the installation currency, default 1. Zero disables this policy. A physical battery-export period is consecutive intervals with both battery discharge and grid export. It does not track battery energy provenance, recharge cycles or realized profit.

Let `C = grid_cost + wear_cost - terminal_credit`, `P = minimum_export_episode_benefit`, and `N` be new physical export-period starts. The solver minimizes `C + P × N`. For the chosen plan, any feasible alternative with `k` fewer starts must worsen physical `C` by at least `P × k`, within solver numerical tolerance. This is a full-horizon comparison that includes prices, conversion losses, wear, opportunity costs and optional terminal value. It does not impose a profit per kWh or a separate cash test on an arbitrarily paired recharge cycle. Real export above the activity floor may economically bridge a price valley without recharge, forming one continuous period.

With duration enabled, the export indicator reuses the verified `mode_DISCHARGE_GRID`. With duration zero and positive benefit, the solver adds only the physical export detector. For each slot, `a = minimum_mode_power_kw × elapsed_UTC_hours`; `Bd` and `Go` are the finite discharge and export energy bounds; `e` and `z` are binary:

```text
bd <= Bd × (e + z)
gout <= Go × (e + 1 - z)
bd >= a × e
gout >= a × e
```

Thus `e=0` requires discharge or export to be zero, while `e=1` requires both to meet the material power floor. The indicator is unavailable when the floor is at or below the existing classifier resolution of 2e-6 kWh or exceeds physical bounds. The material floor is a deliberate interaction with duration zero: it restricts simultaneous discharge/export only. It does not enable all six modes, restrict standalone household discharge or PV-only export, or prohibit simultaneous curtailment and charging. Curtailment and arbitrary hidden direction/allocation labels cannot hide physical export. Native settlement boundaries and UTC elapsed durations are retained.

The exact start identity is `s_i = max(0, e_i - e_previous)`, enforced by all three bounds `s >= e - previous`, `s <= e`, `s <= 1 - previous`. The first previous indicator is 1 only for a valid current export continuity record. The reserve is `P × sum(s)`. Independent validation checks binary integrality, physical activity and start identities before counting starts and computing the reserve. The existing physical balance/bounds tolerances apply; activity checks use at most 1e-7 kWh. With this policy active, HiGHS uses zero relative MIP gap; floating-point feasibility and absolute objective tolerances still apply. A time-limited incumbent is never published as optimal: a base plan accepted at the time limit carries `time_limited` and its gap, and probes never accept one. Existing base, probe and total deadlines remain unchanged; incomplete probes remain unknown.

`Plan.objective` remains physical `C`. `Plan.new_export_episodes` and `Plan.export_episode_reserve` are separate diagnostics, both zero when the benefit policy is disabled or no battery exists. Runtime exposes them with `minimum_export_episode_benefit` and `export_benefit_scope: additional_battery_export_period` under `dispatch_policy`. The reserve covers the solved horizon and is a decision preference, not an electricity charge; it is excluded from expected grid/wear costs and consumption-probe cost differences.

A separate HA Store, `energy_compass.<entry_id>.export`, contains only `{generated_at, until}` for a published current export period. `until` is the contiguous current period's end, not the first settlement boundary. A record is usable only with aware timestamps and `generated_at <= now < until`, with duration greater than zero and at most 48 hours. Recalculation and restart within that interval grant continuity; expiry, future or corrupt records do not. The record never grants dwell age or waives physical constraints. Legacy mode/direction records alone do not establish export continuity. A preview without the live record starts fresh.

Only accepted, published results update this record; failed and superseded results do not. Continuity uses the physical discharge/export values with a 1e-6 kWh tolerance, not the display label (which may give curtailment precedence). Current HOLD or PV-only export clears the record, and a future planned export period creates no current record. Unload flushes the record and restart restores it. Continuity is also maintained while the hurdle is zero, so enabling it during a current period does not fabricate a new start. Each accepted result records its own contiguous current period; without a replacement it expires at the previously scheduled end. No forecasts or household measurements are persisted in this store.

## Minimum grid-charge episode benefit

`minimum_grid_charge_episode_benefit` mirrors `minimum_export_episode_benefit` exactly, but for grid-fed battery charging: it is a finite amount from 0 to 1000 in the installation currency, default **0 (disabled)**, so existing installs see no behaviour change until it is raised. A physical grid-charge period is consecutive intervals where `Problem.minimum_mode_power_kw × elapsed_UTC_hours` or more of `grid_battery` (grid import allocated to the battery) is present. Unlike export, this is a single physical flow, so the exact binary indicator needs only one big-M pair (`grid_battery <= charge_max × e`, `grid_battery >= a × e`) rather than the two-flow `e`/`z` pair export uses. When duration constraints are active, the indicator reuses the verified `mode_CHARGE_GRID` binary directly.

Let `C = grid_cost + wear_cost - terminal_credit`, `P = minimum_grid_charge_episode_benefit`, and `N` be new physical grid-charge-period starts. The solver minimizes `C + P × N` (added to the internal objective the same way the export reserve is, alongside the `episodes` term). Any feasible alternative with `k` fewer starts must worsen physical `C` by at least `P × k`, within solver numerical tolerance. This directly targets the alternating CHARGE_PV/CHARGE_GRID behaviour observed at the activity floor: without the hurdle, the solver is indifferent between many small grid-charge intervals and few consolidated ones, so it may import exactly the 0.025 kWh floor every other slot. A positive hurdle makes each additional start costly, consolidating grid charging into fewer, deliberate episodes.

`Plan.objective` remains physical `C`. `Plan.new_grid_charge_episodes` and `Plan.grid_charge_episode_reserve` are separate diagnostics, both zero when the hurdle is disabled or no battery exists — the reserve is a decision preference, not an electricity charge, and is excluded from `grid_cost`, `wear_cost`, `terminal_credit`, `plan_monetary_cost()` and consumption-probe cost differences, exactly as the export reserve is excluded. Runtime exposes them with `minimum_grid_charge_episode_benefit`, `new_grid_charge_episodes`, `grid_charge_episode_reserve` and `grid_charge_benefit_scope: grid_fed_battery_charge_period` under `dispatch_policy`.

A separate HA Store, `energy_compass.<entry_id>.grid_charge`, persists only `{generated_at, until}` for a published current grid-charge period, with the same continuity, expiry and restart semantics as the export store — without it, an hourly refresh mid-episode would see no prior episode and re-charge the hurdle, biasing against continuing an in-progress charge.

## Inverter standby loss

A hybrid inverter keeps its DC side alive from the battery whenever the pack is connected. The draw is not routed through the inverter's discharge-current limit and it does not serve site load, so a plan that treats a parked battery as a flat SOC is wrong: on one measured night the pack fell from 12 % to 1 % while the grid covered the whole house and the commanded discharge current was zero.

The battery setting `idle_drain_kw` is a finite power from 0 to 10 kW, default **0 (disabled)**, so existing installs see no behaviour change until it is raised; with it at zero the solver model is byte-identical to 0.1.18. Set it to the measured standby draw — battery power while the plan commands neither charge nor discharge and PV is zero. For a new installation, the Deye hybrid setup profile pre-fills 0.13 kW; measure yours rather than trusting that figure for a different inverter or battery class.

Battery energy then advances by `charge efficiency × charge - discharge ÷ discharge efficiency - standby loss`, where the per-interval loss is `idle_drain_kw × elapsed UTC hours`. The loss is a constant, not a decision variable, so the program stays linear. It is capped by the "do nothing" SOC trajectory — the energy the pack would still hold if the plan never charged or discharged — which both keeps the leak from driving SOC below zero and makes that trajectory a feasible solution in every problem, so a positive setting can never make the program infeasible.

Two bounds move with the loss. The per-interval operating-reserve floor relaxes to `max(0, min(reserve, initial SOC) - cumulative loss)`, because standby draw can carry a pack below reserve with no discharge commanded; the reserve-discharge gate is armed in that case, so the relaxed floor still cannot be spent — any interval that discharges must finish at or above the full reserve. The `preserve_initial` terminal requirement becomes `initial SOC - total loss`: standby draw is a tax rather than a decision, and holding the raw initial SOC would be infeasible for an install that can neither grid-charge nor see PV inside the horizon. Charging to cover the loss remains available and is taken whenever prices or the autonomy reserve justify it.

The independent result validator applies the same per-interval loss and the same relaxed floor, so a solution that ignores the leak is rejected.

## LFP balance

`lfp_balance` adds one optional binary hold window to the dispatch problem so a lithium iron
phosphate pack can periodically sit at full SOC long enough for its BMS to re-anchor cell voltages.
The tracker (`balance_tracker.py`) runs unconditionally from observed SOC, independent of whether
`lfp_balance` is on; the setting only controls whether `runtime.py` turns the tracker's phase into
candidate windows for the solver.

`engine/balance.candidate_windows` proposes hour-aligned windows `c` of consecutive slots long
enough to cover `balance_hold_minutes`. While the phase is `due`, only starts within the next 24 h
are offered; an `eligible` balance may take a start anywhere in the horizon. A window is
`CHARGE_PV` if PV covers load in every one of its slots, else `CHARGE_GRID` if grid charging is
allowed and passes the price ceiling in every slot; otherwise the start is dropped.

For each candidate window `c` the solver adds one binary `y_c` (`_constrain_balance` in
`engine/optimize.py`) and, unless the dispatch plan already pinned the choice for a consumption
probe, one continuous `miss ∈ [0, 1]`:

```text
Σ_c y_c ≤ 1                     -- at most one hold window is chosen
Σ_c y_c + miss ≥ 1              -- choosing none forces miss to 1
E_{start(c)} ≥ θ · y_c          -- energy the slot before the window starts is already at threshold
E_t ≥ θ · y_c        for t ∈ c  -- energy stays at/above threshold θ = balance_soc_threshold × capacity for every slot in the window
bd_t ≤ bd̄_t · (1 − y_c)  for t ∈ c  -- no discharge inside a chosen window (bd̄_t is the slot's normal discharge cap)
```

For the window whose first slot is slot 0, `E_{start(c)}` is the battery's known initial energy
instead of a decision variable, so `y_c` is simply forced to 0 when the initial energy is already
below threshold. The solver's internal objective carries `+ m · miss`, where `m =
balance_miss_cost` is `10% × balance_value` while eligible, `balance_value × (1 + days overdue)`
once due, and `10 × balance_value` during an active hold (`engine/balance.miss_cost`); a hold
that starts while the balance is still `ok` is tracked but offers no window and no miss cost —
this term
steers the solver away from skipping a due or in-progress balance but is **not** part of the
returned `Plan.objective`, which stays exactly `grid_cost + wear_cost - terminal_credit`; balancing
is priced as a soft planning preference, not a physical cost.

From the slot before the earliest candidate window's first slot to the horizon end
(`engine/balance.lift_slots`), the per-slot energy upper bound switches from the configured
`soc_ceiling` fraction of capacity to full `battery.capacity_kwh`. After a hold reaches the
threshold, energy can only fall back under the ceiling at load pace, so capping the following
slots at the ceiling would make the chosen window infeasible; the lift only changes anything when
`soc_ceiling < 100 %`. If the battery's initial energy is already above the ceiling, every slot is
lifted instead.

The independent validator (`_validate_balance`) re-checks the LP relaxation is integral for every
`y_c`, that at most one window is picked (exactly one if the choice was pinned), that miss covers
any unpicked case, and independently re-derives the window's start energy and per-slot energy/
discharge bounds from the solved flows — the same checks `_constrain_balance` encodes, run again
against the returned solution.

A consumption probe must not be allowed to re-decide the balance choice — that would price the
probe's extra load against the balance instead of the load alone. `engine/balance.pin_balance`
freezes the dispatch plan's choice before probes run: if the baseline plan picked a window, the
probe problem keeps only that one window with `balance_fixed=True` and `balance_miss_cost=0`; if
the baseline missed the balance entirely, probes see no balance windows at all. Either way the lift
slots are kept so the probe's energy bounds match the pinned choice.

## Dispatch strategies

The `strategy` Planning setting selects one of six bundles. `cost_min` is the exact objective and
constraints above, unchanged; every other strategy adds weighted terms to the solver's internal
coefficients only — the reported physical objective (`Plan.grid_cost + Plan.wear_cost -
Plan.terminal_credit`, exposed as `plan_monetary_cost()`) stays real currency under every strategy,
never carrying a strategy weight or penalty. The solver minimizes

```
grid = sum(import_weight × buy × import + import_kwh_weight × import
           - export_weight × (sell - pv_export_margin) × export)
     + battery_export_penalty_per_kwh × battery_export
autonomy = soc_target_weight × sum(window shortfall slacks)
peak = peak_import_weight × max(import ÷ duration)
caps = cap_violation_weight × sum(import/export cap slacks)
objective = grid + wear + episodes + autonomy + peak + caps - terminal_credit
```

| strategy | changes | when |
| --- | --- | --- |
| `cost_min` | none — every weight at its default, autonomy reserve off | default; byte-identical to the plain objective above |
| `self_sufficiency` | `import_kwh_weight` dominates price (`max(self_sufficiency_import_price_per_kwh, max abs buy price + 0.50, import_penalty_per_kwh)`), a `self_sufficiency_export_penalty_per_kwh` battery-export penalty; autonomy reserve on | minimize grid kWh, not currency; robust to zero or negative prices |
| `backup_ready` | autonomy reserve on, its floor raised to `backup_target_soc_percent` of capacity | storm warning, planned outage, winter |
| `pv_swap` | `pv_export_margin` (`pv_swap_margin_per_kwh`) subtracted from the sell price; `limit_export_to_pv` forced on and the grid-charge price ceiling forced off, so night charging is not blocked; autonomy reserve on | small winter PV: buy cheap at night, sell the real PV production later that day |
| `max_export` | the export-episode penalty zeroed, `limit_export_to_pv` and the grid-charge ceiling forced off; autonomy reserve off | pure arbitrage, high sell-tariff windows |
| `grid_friendly` | a `peak_import_price_per_kw` peak-import term and `cap_violation_price_per_kwh` soft import/export caps (`grid_friendly_import_cap_kw` / `grid_friendly_export_cap_kw`, `0` = use the site's `SiteLimits` connection limit); autonomy reserve off | capacity tariffs, a weak connection |

`import_penalty_per_kwh` (Planning, currency/kWh, default **0**, range 0–1000) sets
`import_kwh_weight` under every strategy; `self_sufficiency` takes the larger of it and its own
import price. It is a shadow price: a held battery must now beat covering the house by this
margin, so a marginally better later price no longer leaves the house on the grid, and the plan
keeps room for forecast error inside an interval, where import at the buy price and export at the
sell price do not net out. With the default 0, `cost_min` stays byte-identical to the plain
objective. The value is published under `dispatch_policy` and never enters reported costs. For a
new installation, the PL net-billing setup profile pre-fills 0.20 PLN/kWh, backtested on one site;
see [Setup profiles](guide.en.md#setup-profiles-new-installations).

Every numeric field a strategy did not itself set is still open to the user: settings the entry marks
as explicitly set (`explicit_strategy_fields`) are never overridden by the bundle, so changing a
strategy-owned flag by hand sticks across a later strategy switch.

### Autonomy-reserve SOC floor

Enabled by the bundles above whenever `battery` is present. Windows partition the horizon by the
*cumulative* net surplus, not by per-slot sign, so the partition is invariant under interval
subdivision: a window starts at the horizon start or immediately after the previous window ends, and
ends at the first slot where the running sum of `pv - load` since the window's own start reaches zero
and stays non-negative through the end of that local day (or the horizon end). Window ids are assigned
from this partition alone and are carried on `Problem.soc_target_window`; reserve or capacity clamping
of the targets never merges or splits a window. The floor itself,

```
soc_target[t] = min(usable_capacity, reserve + (1/eta_discharge) *
                     sum(max(0, load[tau] - pv[tau]) for tau in (t, end_of_window(t)]))
```

is billed **once per window**, on the deepest shortfall of that window — a ten-slot dip below the
floor costs one weight, not ten — at `soc_target_weight`, the expected night rebuy price plus
`autonomy_margin_per_kwh` (median buy price over the night hours, or over every slot when none fall at
night). `backup_ready` raises the target to `backup_floor_kwh` (`backup_target_soc_percent` of
capacity) and takes the larger of the two weights.

The floor sees a separate **48-hour** PV/load horizon, independent of the priced horizon: before
tomorrow's day-ahead prices publish (typically ~14:00), the solver still only plans over priced slots,
but the floor's own targets already account for tomorrow's forecast. Native PV/load edges inside the
tail are preserved (a brief surplus dip survives); a tail longer than 192 intervals coarsens to hourly
edges (`autonomy_tail_coarsened`); a tail source `InputError` degrades to the priced horizon alone
(`autonomy_tail_unavailable`). No PV source configured skips the floor entirely
(`autonomy_floor_requires_pv`) — without PV every window would run to the horizon end and pin the
battery at usable capacity, which is not a floor. If the grid-charge price ceiling sits below
`soc_target_weight - autonomy_margin_per_kwh`, a warning (`grid_charge_ceiling_below_autonomy_weight`)
fires — the solver could otherwise never refill what the floor made it sell — but the ceiling is never
mutated automatically.

### Peak-period reserve

Independent of strategy and of the autonomy floor, the Planning setting `peak_reserve_margin_kwh`
(0–100 kWh, default **0 = off**; at 0 the solver model is byte-identical to 0.1.25) adds a second
soft SOC floor built by `engine/peak_reserve.py`. It requires `limit_grid_charge_price`; with the
ceiling off the floor is skipped with the warning `peak_reserve_requires_grid_charge_ceiling` (a
horizon-minimum threshold would turn almost every slot of a dynamic tariff into one run). A run is a
maximal sequence of slots with buy price above `maximum_grid_charge_price` (the exact complement of the
grid-charge ceiling test, so the battery cannot be refilled from the grid inside it). For a run with a
positive deficit that starts after slot 0, only the cheap slot right before it gets

```
peak_target = min(usable_capacity, reserve + peak_reserve_margin_kwh +
                  (1/eta_discharge) * sum(max(0, load[tau] - pv[tau]) for tau in run))
```

and every other slot carries window id `-1` (no floor). Each entry target is its own window with one
slack variable, billed at `peak_reserve_weight` = median buy price of the expensive slots minus the
ceiling. The floor only sees the priced horizon (no 48-hour tail), so a run cut by the horizon end is
sized on its priced part.

Why entry only: the plan is re-solved inside the run. With a floor on every run slot, the shortfall
after overconsumption is deepest at slot 0, and importing there instead of discharging lowers the
billed slack by `weight / eta` per kWh at a cost of about `weight` — so the solver would buy at the
expensive price to rebuild the buffer the house had just used. For the same reason a run already in
progress at slot 0 gets no floor. Inside the run the buffer is ordinary stored energy that the plan may
still export when that pays.

Why a separate floor: the autonomy floor's windows run until PV recovers — evening, night and the next
morning form one window with one slack — so an evening export that already paid for the window's
deepest shortfall leaves the morning unprotected, and the floor ignores cheap night grid energy. With no
upcoming run with a deficit, or a zero weight, the floor is skipped with `peak_reserve_inactive`. The
slack total is `Plan.peak_reserve_shortfall_kwh`, exposed as the plan attribute
`peak_reserve_shortfall_kwh`; it is planning-only and never part of `plan_monetary_cost`, reported costs
or consumption probes.

### Soft caps

`grid_friendly`'s import/export power caps and every strategy's autonomy floor are **soft**: a
violation becomes a penalized slack variable rather than an infeasible solve, so a strategy always
returns a plan even under conditions its author did not foresee (a full battery with PV surplus and
curtailment disabled, load above the configured import cap). The total slack is reported as
`Plan.cap_violation_kwh` (and `Plan.autonomy_shortfall_kwh` / `Plan.peak_reserve_shortfall_kwh` for the floors), all exposed as plan
sensor attributes — the deployer sees the strategy is fighting its own constraints instead of the plan
silently failing to solve.

### Strategy-switch release semantics

Writing a new `strategy` (through the `select.<name>_strategy` entity or an options/reconfigure
flow) stamps `strategy_changed_at` on the configuration. The **next** `build_problem` call drops the
carried mode commitment entirely for that one generation — `initial_dispatch_mode`,
`initial_dispatch_mode_since`, `initial_battery_mode` and `initial_battery_mode_since` are all `None`,
and the new plan starts unlocked, with `Problem.strategy_changed=True` recorded on it. This is
different from a `safety_exception`: an exception is a *pause* — the locked mode is temporarily
substituted with `HOLD` but the commitment and its `since` survive underneath, ready to resume; a
strategy change is a *reset* — the commitment itself is gone, and whatever mode the new plan opens
with becomes the fresh baseline. `initial_export_active` is deliberately untouched by a release: it
marks an export episode already in progress, and clearing it would double-charge the episode penalty
mid-episode.

The `strategy_changed_at` token is a **one-shot**, consumed only after a plan generated under it is
successfully published (`Plan.strategy_released` mirrors the flag the token produced) — never on
persist, and never speculatively. If HA restarts, the compute fails, or the result is superseded by a
newer generation between the write and the publication, the token survives untouched and the first
successful plan after recovery performs the release. A generation superseded mid-solve (by another
configuration write, including a rapid second strategy switch) is discarded rather than published; the
select's `current_option` always reads the cached configuration the user set, while the plan's
`strategy` attribute is the label of the generation that actually produced it — the two differ only
for the moment a recalculation is in flight.

## Incremental consumption guidance

`analyze_consumption(problem, plan, settings, budget_s=50.0)` recomputes the same optimization with an extra load probe in each display and percentile-reference interval. The starting stored energy, terminal rule and value, prices, and equipment limits stay fixed. A probe adds the configured energy, 1 kWh by default, across underlying slots in proportion to UTC elapsed overlap. Display and reference bins are anchored at the actual snapshot start. The final covered bin is clipped to source coverage and probed at its actual duration; the configured probe energy is unchanged, so a short tail implies higher average power and can genuinely fail when equipment limits bind. Later display bins remain unknown. The incremental cost is the physical `Plan.objective` difference divided by the energy actually added; both plans use the same export-benefit decision policy, but their decision reserves are excluded from this difference. Infeasible and expired probes have unknown costs and levels. The optimizer calls are serial and stop when the shared probe budget expires.

Intervals advance by configured elapsed durations from the snapshot timestamp, including its microseconds. Household window boundaries therefore follow the snapshot rather than wall-clock hours; native tariff settlement slots are unchanged, and probes are distributed across those slots by overlap. A repeated local hour occupies distinct elapsed intervals. The display horizon can be shorter than the percentile reference horizon; every available reference probe still runs. Reference percentiles use linear interpolation between successful costs in the available contiguous reference horizon. Shorter source coverage remains in `percentile` mode. If its clipped final probe fails while at least one reference probe succeeds, that tail remains unknown and percentiles use the successful costs with `coverage_reason=reference_probe_failed`. A failed full reference interval instead uses the configured `absolute_fallback` or `unavailable` policy. `reference_complete` remains false whenever the requested horizon or any reference result is missing.

Classification checks BOOST strictly below `boost_ceiling`, then CHEAP at or below either the low percentile or the minimum purchase tariff in the available reference horizon, then LIMIT strictly above both `limit_floor` and the high percentile, then NORMAL. A tiny numeric tolerance applies only to CHEAP equality so solver residue cannot hide a mathematically equal value. The standalone defaults are 0.01 and 0.80 per kWh. Under `absolute_fallback`, a failed full reference probe removes percentile comparisons but retains the independent minimum-tariff CHEAP rule. `consumption_outlook` returns just the display opportunities for callers that do not need diagnostics.

`merge_windows` joins adjacent requested levels and retains each level represented within a window. `next_window` returns an active or upcoming window, keeping its original start even when called after it began. `next_transition` returns the next different known level while coverage is contiguous; it stops at an unknown interval. A subsequent coordinator may persist an observed active start across recalculation or restart. `serialize_opportunity` writes timezone-aware ISO timestamps with offsets.

The machine display has one primary state by precedence: curtailment, grid-fed charge when charge exceeds available solar surplus, solar-fed charge, discharge with grid export, discharge serving load, then hold. `machine_snapshot` includes the actual PV, load, import, export, charge, discharge, curtailment, and end-of-slot battery energy. Aggregate AC flows cannot uniquely attribute every simultaneous solar and battery exchange, so the single state is a display summary of those numeric flows.

Supported settings are 1–48 hour display and reference horizons, 15/30/60 minute display intervals, and at most 96 distinct full-duration probes, including for snapshots between clock boundaries. The probe is greater than zero and at most 5 kWh, ordered percentile settings lie in [0, 100], and absolute BOOST must be below absolute LIMIT. A per-probe time limit must be in (0, 30] seconds and the outlook budget in [0, 300] seconds. The default pipeline allowance is 10 seconds for the base solve, 2 seconds per probe, and 60 seconds overall. Runtime subtracts actual base-solve elapsed time and reserves one second for finishing analysis before passing the nonnegative remainder to `budget_s`. Zero remaining optional budget skips probes and leaves costs unknown. Budget expiry produces unknown intervals and a coverage reason; the certified base plan remains usable when optional work finishes within the overall deadline. The hard overall deadline still applies. A base plan accepted at `solve_time_limit_s` is marked `time_limited`; probes then difference against that validated incumbent, so their costs carry at most its suboptimality.

Under **Performance**, the base-plan solve limit can be set from 0.1 to 30 seconds for slower hosts or harder forecasts; its default remains 10 seconds. It must be strictly below the configured overall limit, which is configurable up to 300 seconds. More time spent proving the base optimum leaves less time for optional consumption probes, so more of those costs may remain unknown.


## Plan retention and alerts

`forecast_valid` means that a published baseline covers the current time. Its `current_guidance_valid` attribute describes the current consumption probe. Unknown probes and absent/ended windows stay unavailable; known later windows and baseline flows remain usable.

A queued calculation, source-validation failure, timeout, infeasible replacement or unexpected calculation error preserves the last published plan while it has a current interval. `plan_retained: true` identifies this earlier snapshot. Native interval boundaries continue to update current advice and commitment clocks, even while a worker runs or inputs remain invalid. `generated_at` and forecast values keep their original meaning; consumed rows are removed as time advances. The retained plan's `valid_until` is its actual coverage end. This does not extend the forecast or update its assumptions with unvalidated inputs. No plan is retained across an integration reload or restart. Saving the integration options is not a reload: the coordinator adopts the new configuration live and the published plan stays retained (`refreshing: true`) until the recalculation publishes. Only a change to which entities exist (`lfp_balance`, `flexible_load_enabled`, `expose_costs`, `expose_windows`), the installation name or currency, or a running Energy Atlas reloads the entry.

The diagnostic **Alert** binary sensor turns on for input and calculation failures. Its attributes expose `code`, `reason`, `since`, `plan_retained` and `last_successful_plan_at`. Repeated identical failures preserve `since`. Starting a retry does not clear an existing alert; a successful replacement does. `optimizer_status` continues to describe the latest calculation state independently of whether the old plan is available. Without a previous plan, or after its coverage ends, recommendations are unavailable and Alert stays available.

A downward SOC jump raises `soc_measurement_jump` without deleting the plan. An upward jump (typically a BMS recalibration near full charge) first reports `calculating` with reason `soc_rebase_pending`, keeps the retained plan and raises no alert, because that plan only underestimates stored energy; if the new level is not confirmed within 300 seconds it raises `soc_measurement_jump` like a downward jump. To recover from a corrected battery reading, the coordinator requires fresh reports spanning at least 60 seconds that are physically plausible both against the first recovery reading and the preceding reading. A new implausible jump restarts stabilization. Unavailable, stale, out-of-range or disagreeing BMS readings are not accepted as a recovery baseline. Re-reading the same timestamp cannot complete stabilization. Once accepted, the corrected SOC becomes the new reference and triggers a replacement calculation. The alert clears only when that calculation publishes successfully; this detects a stable correction, not proof of a particular BMS calibration event.

## Recalculation cadence

`refresh_minutes` controls periodic full optimization, aligned to its wall-clock boundary (up to 60 minutes). Significant source changes and manual refresh still request an earlier solve. Timer checks at native settlement boundaries and input freshness deadlines only advance the existing plan and validate sources. Current advice, consumption costs, mode commitments and export continuity follow the active interval without resetting the plan generation time. Expired input data raise Alert and retain the previous plan within its coverage. A newly generated plan has a fixed `valid_until` equal to its generation time plus twice the resolved `refresh_minutes`, capped by the end of actual forecast coverage. Normal republication does not shorten that deadline to the next solve or extend it when unchanged inputs report again. Input freshness is tracked separately by `inputs_valid_until`; fresh reports update this health deadline, including during an in-flight calculation. Expired inputs still raise Alert at their configured age limits. Consumers controlling hardware must enforce both their accepted plan lifetime and the input-error/Alert gates; a retained display is not a newly validated generation.

`minimum_replan_seconds` (Performance, default 900, 0 disables) is the shortest interval between two calculations started by an input change, measured from the previous start. Each solve may use its whole time budget, so without the limit inputs that change faster than a solve kept the optimizer permanently `calculating` and `ready` lasted milliseconds. While the limit holds, the published plan stays available; changes are collected and solved once when it expires. The limit applies whenever a valid plan is retained, including while a solve is running and after one ended in timeout or error, and lifts as soon as no valid plan remains. Periodic refreshes, configuration and registry changes and invalid inputs are not delayed.

`soc_trigger_percent` (Performance, default 5) is the SOC dead band: a battery reading is treated as unchanged until it moves that far from the value that last started a calculation. Together the two settings bound input-driven solves; `refresh_minutes` still sets the periodic floor. The Optimizer status sensor reports `last_calculation_started_at` and `calculations_since_load`; diagnostics add `calculations_last_hour` and `calculations_last_24h`.

When a long computation finishes after its first interval has ended, publication selects the interval active at completion. The configured overall budget covers base dispatch and optional consumption probes, with the existing one-second finish reserve. Increasing it to 300 seconds may improve cost coverage but does not guarantee every probe completes; unknown results remain unknown. Defaults stay at 15-minute refresh, 10-second base solve, 2-second probe and 60-second total budget.
