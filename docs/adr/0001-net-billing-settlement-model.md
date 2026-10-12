# ADR-0001: Net-billing settlement model — energy/distribution split and deposit value factors

Status: proposed — becomes accepted when the owner approves gate T-004
Date: 2026-10-12

No implementation is part of this ADR. It fixes the model, the settings and the contract between
runtime and engine; tickets are planned after the gate.

## Context

### What the planner does today

The solver prices every imported kWh at the full buy price and every exported kWh at the full sell
price (`engine/optimize.py`, the `gin` and `gout` coefficients):

```text
gin  cost = import_weight × buy + import_kwh_weight
gout cost = -export_weight × (sell - pv_export_margin)
```

`Plan.grid_cost`, `Plan.objective` and `plan_monetary_cost()` use the same `buy` and `sell`.
The buy price is one number per slot (`Slot.buy_per_kwh`), built in `runtime.py` from
`buy_rate` / `buy_off_peak_rate` (or a forecast) × `buy_multiplier` × VAT + `buy_addition`. The
tariff `CATALOG` (`sources/tariffs.py`) holds band windows only, never prices, and setup profiles
are forbidden from assigning tariff prices (`TARIFF_PRICE_KEYS`).

### What Polish net-billing settles

Points 1, 3 and 4 were checked against the seller and ministry descriptions listed under
[Sources](#sources); the consolidated text of art. 4c of the RES Act was not read directly (open
question 1). Point 2 is taken from T-003 and was not confirmed by those sources (open question 3).

1. The value of exported energy (RCE × 1.23 for hourly settlement, already in the sell price series)
   is booked on the **prosumer deposit** in the following month.
2. The deposit pays only what the prosumer owes the **seller for energy**. Distribution charges are
   paid in cash whatever the deposit holds.
3. Deposit amounts are used **oldest first** and stay usable for 12 months from booking.
4. What is still unused after 12 months is refunded, but the refund for a month **cannot exceed
   ρ × the value of the energy exported in that month** — ρ = 30 % with hourly RCE settlement,
   20 % with monthly RCEm. The rest is void.

For one monthly vintage with exported value `G` and unused amount `U`:

```text
refund = min(U, ρ × G)        void = max(U - ρ × G, 0)
```

### What that means at the margin

| Deposit state | One more currency unit of export is worth | One more unit of energy-part purchase costs |
| --- | --- | --- |
| **Short** — the deposit runs out, the rest of the energy part is paid in cash | 1 (replaces cash) | 1 (cash) |
| **Within the cap** — some deposit expires, `U ≤ ρ × G` | 1 (refunded in full) | 1 (reduces the refund one for one) |
| **Cap binding** — `U > ρ × G` | ρ (only the cap grows) | **0** (paid from money that would be void) |

So the planner's full-price valuation is exact until the refund cap binds, and wrong on both sides
once it does: export is overvalued by 1/ρ and the energy part of import is priced although it costs
nothing in cash.

This differs from the premise of T-003 in two ways, and both matter:

- The edge is **not** at "yearly deposit = yearly energy-part purchases". It is where the unused share
  of the deposit reaches the refund cap, roughly `E < (1 - ρ) × D`.
- In the cap-binding state the energy part of import is worth **0**, not ρ. The premise ("only ~0.3
  of it once the deposit is in surplus", applied to both export and the energy part of import)
  describes a proportional refund `ρ × U`, which is not the rule in point 4.

Reference site (12 months): deposit `D` ≈ 2214 PLN, energy-part purchases `E` ≈ 2150 PLN. Unused
share `1 - E/D` = 2.9 %, cap 30 %: the site is **within the cap**, not on the edge. Its exports are
worth full price today; the valuation starts to move only above `D` ≈ 2530 PLN at unchanged
purchases.

## Decision

### 1. Split of the buy price

The buy price stays one series. Two new settings state how much of it the deposit can pay:

| Setting | Group | Default | Range | Meaning |
| --- | --- | --- | --- | --- |
| `buy_energy_rate` | tariffs | 0 | 0–1000 currency/kWh | energy part of `buy_rate` |
| `buy_off_peak_energy_rate` | tariffs | 0 | 0–1000 currency/kWh | energy part of `buy_off_peak_rate` |

- They are written the way `buy_rate` is: `buy_multiplier` and VAT (when `buy_apply_vat` is on)
  apply to them; `buy_addition` belongs entirely to the non-energy part.
- Per slot: `energy_t = min(energy rate of the slot's band × factor, max(buy_t, 0))` and
  `distribution_t = buy_t - energy_t`. Bands come from the existing `band_rows()` when the buy source
  is a Polish tariff schedule; a `g11` schedule, a fixed price and a forecast have one band and use
  `buy_energy_rate` for every slot.
- Default 0 means "no part of the buy price is deposit-payable": import keeps its full price in
  every deposit state.
- Both keys join `TARIFF_PRICE_KEYS`: a setup profile never assigns them, and the tariff `CATALOG`
  keeps holding windows only. Prices change yearly and per seller; a catalog of rates would be wrong
  within months and nobody would own its updates.
- `Slot` gains `buy_energy_per_kwh: float = 0.0`, filled in `runtime.py` next to the two tariffs.

Known limit: with a forecast buy source the energy part is flat. A G12 site that feeds its buy price
through the tariff helper sensor gets one energy rate for both zones; the schedule buy source is the
supported way to split per zone (open question 4).

### 2. Deposit inputs

| Setting | Group | Default | Range | Meaning |
| --- | --- | --- | --- | --- |
| `deposit_model` | tariffs | `off` | `off` · `manual` · `auto` | how the factors are obtained |
| `deposit_refund_percent` | tariffs | 30 | 0–100 % | ρ: refund cap as a share of a month's export value |
| `export_value_factor` | tariffs | 1.0 | 0–1 | `manual` only: what one more unit of export is worth |
| `deposit_yearly_value` | tariffs | 0 | 0–1 000 000 currency | `auto`: expected deposit booked over 12 months (`D`) |
| `deposit_yearly_energy_cost` | tariffs | 0 | 0–1 000 000 currency | `auto`: expected charges the deposit can pay over 12 months (`E`) |

- `D` is the export value at the settlement price, including the 1.23 coefficient. `E` is everything
  the deposit can pay in a year — the per-kWh energy part and any fixed seller fee it covers.
- **Stage 1** takes `D` and `E` from the two settings. 0 means unknown.
- **Stage 2** (separate tickets, only if stage 1 proves useful) adds two optional source bindings read
  once a day from long-term statistics: a cumulative export value sensor (the export value counter
  already produces one) for `D`, and a cumulative grid import energy sensor for
  `E = Σ hourly import × energy_t`. A binding with at least 365 days of statistics replaces its
  setting; a shorter history is ignored, never extrapolated, because both sums are strongly seasonal.
- **The deposit balance is not an input.** A balance read on one day says nothing without the booking
  dates behind it: a site that is short every year still holds its largest balance in October. With
  oldest-first use, the long-run state is decided by the 12-month sums `D` and `E`; the balance only
  describes the path between them. No entity and no setting for it.
- No setup profile changes. `pl_net_billing` keeps its three assignments; every existing and new
  installation starts with `deposit_model: off`.

### 3. Factors, hysteresis and missing inputs

Two numbers enter the objective, both derived from one **cap pressure** `π ∈ [0, 1]` — the weight given
to the cap-binding state:

```text
export_value_factor  φx = 1 - π × (1 - ρ)        # 1 → ρ
import_energy_factor φe = 1 - π                  # 1 → 0
```

`auto`:

```text
s = clamp(1 - E / D, 0, 1)                       # unused share of the yearly deposit
π = clamp((s - ρ/2) / (ρ/2), 0, 1)               # 0 below ρ/2, 1 from ρ
```

The ramp starts at ρ/2 rather than ρ because expiry is not spread over the year: with oldest-first use
the winter vintages are consumed and the unused amount lands on the high-export months. Assuming
those months hold about half of the yearly deposit (an assumption, not measured on any site), their
cap binds at about half the yearly share. Between the two points π is the expected weight of the cap-binding state under forecast uncertainty. The constants
`ρ/2` and `ρ` are fixed in code, not settings.

`manual`: `φx` is the setting; `π = clamp((1 - φx) / (1 - ρ), 0, 1)` and `φe = 1 - π`. With ρ = 100 %
both factors are 1.

Hysteresis (`auto` only):

- π is evaluated **once per local day**, with the first accepted calculation after local midnight, and
  held for the whole day. The held value and its inputs live in a new HA Store
  `energy_compass.<entry_id>.settlement`, so reloads and restarts do not re-evaluate.
- A new value replaces the held one only if it differs by **at least 0.10**, or if it is exactly 0 or 1
  and the held one is not. The ramp is continuous, so a small input change gives a small factor change;
  the dead band removes the remaining day-to-day movement.
- Changing any setting in the tables above re-evaluates at once, without the dead band.

Missing inputs:

| Situation | Result |
| --- | --- |
| `deposit_model: off` | π = 0; the solver model is byte-identical to today |
| `auto`, `D` or `E` is 0 or unknown | π = 0; `settlement.reason: inputs_missing`; no Alert, the plan stays valid |
| `auto`, stage 2 statistics unreadable today | the held π is kept; a held value older than 35 days is dropped (π = 0) |
| `buy_energy_rate` = 0 with π > 0 | export is devalued, import keeps its full price |
| Negative sell price | the factor applies to `max(sell, 0)` only; a negative price is a real charge and stays whole |

### 4. Where it enters the objective, and what stays out

Planning prices, computed per slot inside the solver's coefficient builder:

```text
buy_plan  = buy - (1 - φe) × energy
sell_plan = φx × max(sell, 0) + min(sell, 0)

gin  cost = import_weight × buy_plan + import_kwh_weight
gout cost = -export_weight × (sell_plan - pv_export_margin)
```

`Problem` gains `export_value_factor: float = 1.0` and `import_energy_factor: float = 1.0`. With both
at 1.0 the two expressions reduce to today's coefficients exactly (`buy - 0 × energy`, `1 × sell`), so
`cost_min` with the model off stays byte-identical; the strategy golden tests must pass unchanged.

Unchanged, on purpose:

| Stays at invoice prices | Why |
| --- | --- |
| `Plan.grid_cost`, `Plan.objective`, `plan_monetary_cost()` | reported cost must reconcile with the invoice and the cost card; a forecast-based factor would make past values unverifiable |
| net cost and wear in the plan attributes, cost sensors | same |
| consumption probes' reported cost (`plan_monetary_cost` differences) and the levels built on it | same convention as strategy weights: the probe *solves* with the planning objective and *reports* real currency |
| `maximum_grid_charge_price` comparison | it is a ceiling the user types against the price they see |
| `peak_reserve`, autonomy margin, `self_sufficiency` import weight | they rank or bound by the buy price; `buy_plan ≤ buy`, so the dominance they rely on still holds |
| Atlas solve payload (`inputs.buy`, `inputs.sell`) | vendored contract; the factors are not sent |
| Deye controller, cost card, export value counter | they consume intervals and invoice prices only |

New diagnostics, under `dispatch_policy.settlement` in the plan attributes: `model`,
`export_value_factor`, `import_energy_factor`, `cap_pressure`, `unused_share`, `refund_percent`,
`yearly_deposit`, `yearly_energy_cost`, `evaluated_on`, `reason` (`ok`, `off`, `inputs_missing`,
`held`). No new entity in stage 1.

### 5. Interactions

| Existing piece | Rule |
| --- | --- |
| `import_penalty_per_kwh` | unchanged and still additive on top of `buy_plan`. It covers forecast error inside an interval, not settlement. The 0.20 backtested on the reference site was tuned with full-price export; re-check it only if π leaves 0 |
| `minimum_export_episode_benefit`, `minimum_grid_charge_episode_benefit` | unchanged amounts, not scaled. The hurdle is compared in the planning objective, so an export period must now earn the hurdle in deposit-adjusted value; with φx < 1 fewer periods pass |
| `limit_export_to_pv`, `export_limit_scope` | unchanged hard constraints. They matter more when π = 1: the energy part of a night re-buy is free, so selling battery energy stays attractive (see the example) and only the daily PV budget bounds it |
| `terminal_value_per_kwh` | unchanged. With π = 1 a night charge at `buy_plan` 0.14 against a 0.60 terminal value fills the battery every night; that matches the cash economics (night charge 0.21 per delivered kWh against 0.54 avoided by day) |
| Strategies | the factors apply under every strategy, before the strategy weights, like the import penalty. They are not strategy-owned keys and no bundle overrides them. `pv_swap` subtracts its margin from `sell_plan`; `self_sufficiency` keeps `max abs buy + 0.50`, which still exceeds every `buy_plan` |
| LFP balance, standby loss, mode duration | untouched |

### Worked example

Indicative PGE G12 2026 gross prices (from T-003; comparison sites, not the price list): day
0.7018 energy + 0.540 distribution = 1.2418; night 0.4635 + 0.143 = 0.6065 PLN/kWh. Battery round trip
0.89, wear 0.05 PLN per kWh of average throughput, ρ = 30 %.

Selling 1 kWh from the battery and buying it back at night takes 1/0.89 = 1.1236 kWh of import and
(1.1236 + 1)/2 × 0.05 = 0.0531 of wear. T-003 used a flat 0.05, hence its 0.73 and 1.22.

**Factors for three yearly positions** (`E` = 2150):

| Yearly deposit `D` | Unused share `s` | π | φx | φe |
| --- | --- | --- | --- | --- |
| 2214 (reference site) | 2.9 % | 0 | 1.00 | 1.00 |
| 2800 | 23.2 % | 0.55 | 0.62 | 0.45 |
| 3300 | 34.8 % | 1 | 0.30 | 0.00 |

**Planning prices and break-evens** (PLN/kWh):

| | Deposit short or within cap (π = 0) | Cap binding (π = 1) |
| --- | --- | --- |
| `buy_plan` day | 1.2418 | 0.540 |
| `buy_plan` night | 0.6065 | 0.143 |
| value of 1 kWh exported at 0.40 | 0.40 | 0.12 |
| battery sale, re-bought at night: sell price needed | 1.1236 × 0.6065 + 0.0531 = **0.73** | (1.1236 × 0.143 + 0.0531) / 0.30 = **0.71** |
| battery sale instead of covering night load: sell price needed | 0.61 | 0.143 / 0.30 = 0.48 |
| battery sale instead of covering day load: sell price needed | 1.24 | 0.540 / 0.30 = 1.80 |
| night grid charge to cover day load: cost per delivered kWh → gain | 0.73 → 0.51 | 0.21 → 0.33 |

Reading it:

- The sale-and-re-buy break-even barely moves (0.73 → 0.71), because the cheaper re-buy offsets the
  lower export value. A plan that sells from the battery between 0.73 and 1.22 PLN/kWh is right in
  both states.
- What does change when the cap binds: surplus PV is worth 0.12 instead of 0.40, so it goes to the
  battery and to flexible load first; battery energy is kept for day load up to a sell price of 1.80
  instead of 1.24; and night grid charging stays profitable.
- Under the T-003 premise (φx = φe = 0.30) the same sale would need (1.1236 × 0.282 + 0.0531) / 0.30 =
  1.23 PLN/kWh, and the reference site would already sit on the edge. That model would block sales
  that pay in cash.

## Alternatives rejected

1. **Fixed sell multiplier** (lower `sell_multiplier`, or one constant factor on the sell price) —
   rejected because the sell series is also the settlement price: it would change reported cost, the
   cost sensors, probe costs, the cost card and the export value counter; it cannot touch the energy
   part of import, which is half of the effect; and it has no state, so the user would edit it by hand
   as the year goes. `deposit_model: manual` keeps the legitimate use — a user who knows their state —
   without those side effects.
2. **Banning battery export** (forcing `allow_battery_export` off, or a hard constraint while in
   surplus) — rejected because the example shows battery export above ~0.71–0.73 PLN/kWh pays in both
   states, so a ban gives up the evening price peaks, and it does nothing about the larger error, the
   valuation of PV export against storage. The existing switch stays for users who want it.
3. **Proportional refund, one factor on both sides** (the T-003 premise: factor ρ on export and on the
   energy part of import once `D > E`) — rejected because the refund is capped by the month's export
   value, not proportional to the unused amount; see the last bullet of the example.
4. **Exact vintage ledger** (twelve monthly vintages, oldest-first, monthly forecasts of export value
   and purchases) — rejected for now because it needs monthly history and a 12-month seasonal forecast
   for both sides, sellers invoice with a lag of up to half a year so the user cannot check it, and the
   yearly share captures the first-order effect. Revisit if a site stays inside the ramp for months.
5. **Deposit balance as the state input** — rejected, see Decision 2.
6. **Emulating it with `import_penalty_per_kwh`, `pv_swap_margin_per_kwh` or a new strategy** —
   rejected because additive constants cannot express a multiplicative change that differs per tariff
   band, and a strategy bundle would make the settlement model exclusive with the other five.
7. **Deposit-adjusted reported cost** — rejected, see the table in Decision 4.

## Consequences

- With the defaults nothing changes for any installation: no migration, no profile revision, golden
  tests untouched.
- The model is a yearly steady-state approximation. It ignores which month a kWh is exported in (a
  winter export at a cap-bound site can be worth less than ρ), the 13-month delay of the refund, and
  seller-specific invoice rules.
- From stage 2 on, `D` and `E` are measured from a year the planner itself shaped. A lower factor
  reduces export, which lowers `D` and raises the factor again. The ramp and the dead band damp this;
  it is a slow yearly movement, not an oscillation within a season, but it has to be watched on the
  reference site.
- Under π > 0 the consumption windows still report invoice prices, so they understate how cheap
  surplus PV really is. Accepted for stage 1; revisit with real data.
- The reference site gets factor 1.0 today. The feature pays off for sites whose PV is large against
  their consumption, and for the reference site only if battery arbitrage pushes the yearly deposit
  above ~2530 PLN.
- Work to plan after the gate (each line is at least one ticket, in this order):
  1. engine: `Slot.buy_energy_per_kwh`, the two `Problem` factors, the coefficients, tests for both
     states and for byte-identity at 1.0;
  2. runtime and settings: the seven settings, band resolution, `manual` and `auto` with fixed inputs,
     the settlement Store, the dead band, `dispatch_policy.settlement`;
  3. config flow, `strings.json`, `translations/en.json` and `pl.json`;
  4. doc sweep per `AGENTS.md`: `docs/model.md` (objective, new section), `guide.en.md` /
     `guide.pl.md` (Tariffs settings, plan attributes), `README*.md` policy paragraphs,
     `docs/index.html`, rebuilt guides;
  5. stage 2, only on request: the two statistics bindings.

## Open questions for the owner

1. **Refund rule.** This ADR uses "refund ≤ ρ × the month's export value". Recommended: confirm it on
   one seller settlement of an expired month, or in the PGE prosumer guide, before any ticket is
   planned. If the seller really refunds ρ × the unused amount, alternative 3 becomes the model: one
   factor on both sides and the edge at `D = E`.
2. **ρ on the reference site.** 30 % (hourly RCE) or 20 % (RCEm)? Default assumed: 30.
3. **What the deposit pays on the invoice.** Gross energy charge including excise and VAT? The
   seller's fixed trade fee? This sets `buy_energy_rate` and what counts into `E`.
4. **Buy source of the reference site.** Polish tariff schedule or a forecast sensor? Only the schedule
   splits the energy part per zone in stage 1.
5. **Build it now?** With question 1 answered as assumed, the reference site's plan does not change.
   Recommended: build stage 1 (default off, small) for sites with large PV, and leave stage 2 until a
   site reaches the ramp.

## Sources

- PGE, prosumer guide and net-billing invoice leaflet (12 March 2025): 12-month use, refund limit of
  20 % (RCEm) or 30 % (RCE) of the value of energy exported in the month —
  <https://www.gkpge.pl/dla-domu/strefa-klienta/przewodnik-prosumenta/pytania-i-odpowiedzi>
- Ministry of Climate and Environment, changes in force from 27 December 2024 (1.23 coefficient, 30 %
  refund with RCE) — <https://www.gov.pl/web/klimat/korzystne-zmiany-dla-prosumentow-wchodza-w-zycie>
- Enea, prosumer page: the refund limit refers to the deposit booked 12 months earlier —
  <https://www.enea.pl/male-firmy/prosument-energii-odnawialnej>
- Oldest-first use and voiding after 12 months —
  <https://sun-pro.pl/blog/depozyt-prosumencki-zasady-zwrotu-nadplaty-po-12-miesiacach-wniosek-terminy-podatki>
