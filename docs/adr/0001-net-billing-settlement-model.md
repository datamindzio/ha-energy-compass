# ADR-0001: Net-billing settlement model — energy/distribution split and deposit value factors

Status: proposed — becomes accepted when the owner approves gate T-004
Date: 2026-10-12 (revision 2, after the first gate review)

No implementation is part of this ADR. It fixes the model, the settings and the contract between
runtime and engine; tickets are planned after the gate.

Revision 2 changes, from the gate review:

- the split is now a **distribution component** (rates per zone, an optional distribution tariff and
  an `included`/`added` mode) instead of energy rates, so a dynamic-price contract — hourly energy
  price from a forecast plus a zoned distribution tariff — is a first-class case;
- what the deposit pays and the refund rule are no longer open questions: they were researched and
  are stated with their sources; everything that depends on the contract is a setting.

## Context

### What the planner does today

The solver prices every imported kWh at the full buy price and every exported kWh at the full sell
price (`engine/optimize.py`, the `gin` and `gout` coefficients):

```text
gin  cost = import_weight × buy + import_kwh_weight
gout cost = -export_weight × (sell - pv_export_margin)
```

`Plan.grid_cost`, `Plan.objective` and `plan_monetary_cost()` use the same `buy` and `sell`.
The buy price is one number per slot (`Slot.buy_per_kwh`), built in `runtime.py` from one buy source:

| Buy source | Price per slot today |
| --- | --- |
| `fixed` | `buy_rate × factor + buy_addition` |
| `schedule` (Polish G11/G12/G12w) | `buy_rate` or `buy_off_peak_rate` by band, `× factor + buy_addition` |
| `forecast` (any interval entity) | `value × factor + buy_addition` |

with `factor = buy_multiplier × (1 + VAT)` when `buy_apply_vat` is on. The tariff `CATALOG`
(`sources/tariffs.py`) holds band windows only, never prices, and setup profiles are forbidden from
assigning tariff prices (`TARIFF_PRICE_KEYS`).

A dynamic-price contract with a two-zone distribution tariff cannot be configured today: the forecast
source carries the hourly energy price, and distribution can only be the flat `buy_addition`.

### What Polish net-billing settles

Researched for this revision; sources are listed at the end. The consolidated statute text on ISAP
could not be opened from this environment, so the article numbers are quoted from tax rulings and
seller pages that cite them.

1. The value of exported energy is booked on the **prosumer deposit** in the following month. With
   hourly settlement it is RCE × 1.23, which the sell price series already contains.
2. The deposit pays **only the purchase of energy from the seller** (RES Act art. 4c sec. 2). Energa
   states both exclusions: the deposit cannot cover distribution charges, nor other charges such as
   the trade fee. Distribution and every fixed fee are paid in cash whatever the deposit holds.
3. Amounts are used **oldest first** (art. 4c sec. 7) and stay usable for 12 months from booking
   (sec. 5).
4. What is still unused after 12 months is refunded, but the refund **cannot exceed ρ × the value of
   the energy exported in the calendar month the refund concerns** (art. 4 sec. 11 item 2). The rest
   is cancelled (art. 4c sec. 6). Energa words the same base as "the monthly deposit".

For one monthly vintage with exported value `G` and unused amount `U`:

```text
refund = min(U, ρ × G)        void = max(U - ρ × G, 0)
```

### What depends on the contract

| Contract fact | Variants found | Where it goes |
| --- | --- | --- |
| Settlement price of export, and with it ρ | **RCEm** (monthly): prosumers who started before 1 July 2024 and did not switch → ρ = 20 %. **RCE** (hourly): everyone who started from 1 July 2024, and older prosumers after a declaration to the seller that cannot be taken back → ρ = 30 % (in force since 27 December 2024; Energa applies it from 1 February 2025) | `deposit_refund_percent`; the sell source already selects RCE or RCEm |
| Energy price | **G-tariff price list**: one rate per zone (G11, G12, G12w), fixed for the price-list period. **Dynamic-price contract**: hourly TGE day-ahead price (RDN Fixing I) plus the seller's per-kWh add-on (Tauron: 0.1097 PLN/kWh gross; market range in 2024: 0.10–0.15) plus a monthly fee | the buy source: `schedule`/`fixed` for a price list, `forecast` for a dynamic price |
| Distribution price | billed by the distributor's tariff group whatever the energy contract is: a dynamic-price contract keeps its G11 or G12 distribution zones. Rates differ per distributor and group and change with every approved tariff | new distribution rates and distribution tariff (Decision 1) |
| Offers beyond the statute | one seller is reported to apply the deposit to the whole invoice with no expiry (a single trade blog, not verified) | not modelled: `deposit_model: off` or ρ = 100 % |

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
share `1 - E/D` = 2.9 %: the site is **within the cap** at ρ = 30 % and at ρ = 20 %. Its exports are
worth full price today; the valuation starts to move above `D` ≈ 2530 PLN (ρ = 30 %) or ≈ 2390 PLN
(ρ = 20 %) at unchanged purchases.

## Decision

### 1. Split of the buy price: a distribution component

The buy price is the sum of an energy part and a distribution part. The existing buy source and
settings keep their meaning; the distribution part is described by three settings and one source
option:

| Setting | Group | Default | Range | Meaning |
| --- | --- | --- | --- | --- |
| `buy_distribution_mode` | tariffs | `included` | `included` · `added` | is distribution already inside the buy source price, or added on top of it |
| `buy_distribution_rate` | tariffs | 0 | 0–1000 currency/kWh | distribution per kWh, peak zone or single zone |
| `buy_distribution_off_peak_rate` | tariffs | 0 | 0–1000 currency/kWh | distribution per kWh, off-peak zone |

Source option, in **Tariffs → Buy source**: **Distribution tariff** — none (single zone) or one entry
of the existing tariff `CATALOG`, with the same options as the schedule source (afternoon window,
meter clock, extra off-peak dates). It is stored as `PriceSource.distribution_schedule`
(`TariffSchedule | None`, buy only, serialized only when set).

Rules:

- **Rates.** A distribution rate is the sum of every per-kWh position the distributor bills (variable
  network rate, quality fee, RES fee, cogeneration fee). VAT applies when `buy_apply_vat` is on, as
  for `buy_rate`; `buy_multiplier` and `buy_addition` never apply. Monthly positions stay in
  `monthly_charge`.
- **Zones.** With the `schedule` buy source the distribution zones are that schedule's bands and no
  separate distribution tariff can be picked. With `fixed` and `forecast` the distribution tariff
  decides; without one, and for `g11`, `buy_distribution_rate` applies to every slot. Band edges of
  the distribution tariff become slot boundaries like those of the buy schedule.
- **Two-zone check.** With a two-zone schedule, exactly one of the two rates being 0 is an
  `InputError`, like the existing "tariff schedule needs positive buy rates".
- **Per slot**, with `source_t` the price the buy source gives today and `dist_t` the distribution
  rate of the slot's zone:

  ```text
  included:  buy_t = source_t
  added:     buy_t = source_t + dist_t
  energy_t = clamp(buy_t - dist_t, 0, max(buy_t, 0))        # the part the deposit can pay
  ```

- **Defaults keep today's behaviour.** `included` with both rates 0 gives `buy_t = source_t`; nothing
  else reads `energy_t` while the deposit model is off.
- **`added` changes the buy price itself**, for the planner and for reported cost alike, and works
  with the deposit model off. It is how a dynamic-price contract is configured: forecast source = the
  hourly energy price, `buy_addition` = the seller's per-kWh add-on, distribution tariff = the group
  on the distribution contract, two distribution rates.
- **No price catalog.** Both rates join `TARIFF_PRICE_KEYS`: a setup profile never assigns them and
  the `CATALOG` keeps holding windows only (see alternative 9).
- `Slot` gains `buy_energy_per_kwh: float = 0.0`, filled in `runtime.py` next to the two tariffs.
- The buy-source summary in the config flow and Preview show both parts per zone.

How each contract maps:

| Contract | Buy source | Mode | Settings to fill |
| --- | --- | --- | --- |
| G11/G12/G12w price list, all-in rates already typed (today's installations) | `schedule` | `included` | the two distribution rates |
| Same, typed as on the invoice | `schedule` | `added` | `buy_rate`/`buy_off_peak_rate` = energy, the two distribution rates |
| Dynamic price, G11 distribution | `forecast` | `added` | `buy_addition` = seller add-on, `buy_distribution_rate` |
| Dynamic price, G12/G12w distribution | `forecast` + distribution tariff | `added` | as above plus `buy_distribution_off_peak_rate` |
| All-in price from a helper sensor (tariff helper, a seller integration) | `forecast` + distribution tariff | `included` | the two distribution rates |

Known limit: the `CATALOG` has two bands. Groups with three zones or a time-varying distribution rate
(G13, G12n, a dynamic distribution tariff) get a single distribution rate in stage 1, so their split
is an average (open question 2).

### 2. Deposit inputs

| Setting | Group | Default | Range | Meaning |
| --- | --- | --- | --- | --- |
| `deposit_model` | tariffs | `off` | `off` · `manual` · `auto` | how the factors are obtained |
| `deposit_refund_percent` | tariffs | 30 | 0–100 % | ρ from the contract: 30 with hourly RCE, 20 with monthly RCEm, 100 if nothing is ever cancelled |
| `export_value_factor` | tariffs | 1.0 | 0–1 | `manual` only: what one more unit of export is worth |
| `deposit_yearly_value` | tariffs | 0 | 0–1 000 000 currency | `auto`: expected deposit booked over 12 months (`D`) |
| `deposit_yearly_energy_cost` | tariffs | 0 | 0–1 000 000 currency | `auto`: expected energy charges the deposit can pay over 12 months (`E`) |

- `D` is the export value at the settlement price, including the 1.23 coefficient. `E` is the per-kWh
  energy charge of 12 months of import, gross. The seller's trade fee and all distribution are not in
  `E`: the deposit does not pay them.
- **Stage 1** takes `D` and `E` from the two settings. 0 means unknown.
- **Stage 2** (separate tickets, only if stage 1 proves useful) adds two optional source bindings read
  once a day from long-term statistics: a cumulative export value sensor (the export value counter
  already produces one) for `D`, and a cumulative energy-part cost sensor (a sibling counter that
  integrates import × `energy_t`) for `E`. A binding with at least 365 days of statistics replaces its
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
cap binds at about half the yearly share. Between the two points π is the expected weight of the
cap-binding state under forecast uncertainty. The constants `ρ/2` and `ρ` are fixed in code, not
settings.

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
| π > 0 and both distribution rates are 0 | `φe` is forced to 1: export is devalued, import keeps its full price; `settlement.reason: split_missing`. Without the guard the whole buy price would count as deposit-paid and import would look free |
| Negative sell price | the factor applies to `max(sell, 0)` only; a negative price is a real charge and stays whole |
| Negative energy price in a dynamic contract | `energy_t` is clamped at 0, so the slot keeps its real price |

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
| `maximum_grid_charge_price` comparison | it is a ceiling the user types against the all-in price they see |
| `peak_reserve`, autonomy margin, `self_sufficiency` import weight | they rank or bound by the buy price; `buy_plan ≤ buy`, so the dominance they rely on still holds |
| Atlas solve payload (`inputs.buy`, `inputs.sell`) | vendored contract; the factors are not sent |
| Deye controller, cost card, export value counter | they consume intervals and invoice prices only |

"Invoice price" means the all-in `buy_t`. In `added` mode that price now contains distribution, which
is the point of the mode and not an effect of the deposit model.

New diagnostics, under `dispatch_policy.settlement` in the plan attributes: `model`,
`export_value_factor`, `import_energy_factor`, `cap_pressure`, `unused_share`, `refund_percent`,
`yearly_deposit`, `yearly_energy_cost`, `distribution_mode`, `evaluated_on`, `reason` (`ok`, `off`,
`inputs_missing`, `split_missing`, `held`). No new entity in stage 1.

### 5. Interactions

| Existing piece | Rule |
| --- | --- |
| `import_penalty_per_kwh` | unchanged and still additive on top of `buy_plan`. It covers forecast error inside an interval, not settlement. The 0.20 backtested on the reference site was tuned with full-price export; re-check it only if π leaves 0 |
| `minimum_export_episode_benefit`, `minimum_grid_charge_episode_benefit` | unchanged amounts, not scaled. The hurdle is compared in the planning objective, so an export period must now earn the hurdle in deposit-adjusted value; with φx < 1 fewer periods pass |
| `limit_export_to_pv`, `export_limit_scope` | unchanged hard constraints. They matter more when π = 1: the energy part of a night re-buy is free, so selling battery energy stays attractive (see the example) and only the daily PV budget bounds it |
| `terminal_value_per_kwh` | unchanged. With π = 1 a night charge at `buy_plan` 0.14 against a 0.60 terminal value fills the battery every night; that matches the cash economics (night charge 0.21 per delivered kWh against 0.54 avoided by day) |
| Strategies | the factors apply under every strategy, before the strategy weights, like the import penalty. They are not strategy-owned keys and no bundle overrides them. `pv_swap` subtracts its margin from `sell_plan`; `self_sufficiency` keeps `max abs buy + 0.50`, which still exceeds every `buy_plan` |
| `buy_multiplier`, `buy_addition`, forecast `floor_per_kwh` | apply to the buy source only, before the distribution part is added or split off |
| LFP balance, standby loss, mode duration | untouched |

### Worked example

Indicative PGE G12 2026 gross prices (from T-003; comparison sites, not the price list): day
0.7018 energy + 0.540 distribution = 1.2418; night 0.4635 + 0.143 = 0.6065 PLN/kWh. Battery round trip
0.89, wear 0.05 PLN per kWh of average throughput.

Selling 1 kWh from the battery and buying it back at night takes 1/0.89 = 1.1236 kWh of import and
(1.1236 + 1)/2 × 0.05 = 0.0531 of wear. T-003 used a flat 0.05, hence its 0.73 and 1.22.

**Factors for yearly positions** (`E` = 2150 PLN):

| Yearly deposit `D` | Unused share `s` | ρ = 30 %: π → φx, φe | ρ = 20 %: π → φx, φe |
| --- | --- | --- | --- |
| 2214 (reference site) | 2.9 % | 0 → 1.00, 1.00 | 0 → 1.00, 1.00 |
| 2500 | 14.0 % | 0 → 1.00, 1.00 | 0.40 → 0.68, 0.60 |
| 2800 | 23.2 % | 0.55 → 0.62, 0.45 | 1 → 0.20, 0.00 |
| 3300 | 34.8 % | 1 → 0.30, 0.00 | 1 → 0.20, 0.00 |

**G12 price list: planning prices and break-evens** (PLN/kWh):

| | Short or within cap (π = 0) | Cap binding, ρ = 30 % | Cap binding, ρ = 20 % |
| --- | --- | --- | --- |
| `buy_plan` day | 1.2418 | 0.540 | 0.540 |
| `buy_plan` night | 0.6065 | 0.143 | 0.143 |
| value of 1 kWh exported at 0.40 | 0.40 | 0.12 | 0.08 |
| battery sale, re-bought at night: sell price needed | 1.1236 × 0.6065 + 0.0531 = **0.73** | (1.1236 × 0.143 + 0.0531) / 0.30 = **0.71** | (…) / 0.20 = **1.07** |
| battery sale instead of covering night load: sell price needed | 0.61 | 0.143 / 0.30 = 0.48 | 0.72 |
| battery sale instead of covering day load: sell price needed | 1.24 | 0.540 / 0.30 = 1.80 | 2.70 |
| night grid charge to cover day load: cost per delivered kWh → gain | 0.73 → 0.51 | 0.21 → 0.33 | 0.21 → 0.33 |

**Dynamic-price contract with G12 distribution** (`added` mode; three illustrative hours, energy
price = market price + seller add-on, gross):

| Hour | Energy | Distribution | `buy` | `buy_plan` at π = 0 | `buy_plan` at π = 1 |
| --- | --- | --- | --- | --- | --- |
| sunny midday (day zone) | 0.25 | 0.540 | 0.79 | 0.79 | 0.540 |
| evening peak (day zone) | 0.95 | 0.540 | 1.49 | 1.49 | 0.540 |
| night (night zone) | 0.45 | 0.143 | 0.593 | 0.593 | 0.143 |

| Charging for the evening peak, per delivered kWh | π = 0 | π = 1 |
| --- | --- | --- |
| from the grid at night | 0.593 / 0.89 + 0.053 = 0.72 → gain 0.77 | 0.143 / 0.89 + 0.053 = 0.21 → gain 0.33 |
| from the grid at midday | 0.79 / 0.89 + 0.053 = 0.94 → gain 0.55 | 0.540 / 0.89 + 0.053 = 0.66 → loss 0.12 |

Reading it:

- At ρ = 30 % the sale-and-re-buy break-even barely moves (0.73 → 0.71), because the cheaper re-buy
  offsets the lower export value. A plan that sells from the battery between 0.73 and 1.22 PLN/kWh is
  right in both states. At ρ = 20 % it rises to 1.07 — the contract decides, which is why ρ is a
  setting.
- What does change when the cap binds: surplus PV is worth 0.12 (or 0.08) instead of 0.40, so it goes
  to the battery and to flexible load first; battery energy is kept for day load up to a sell price of
  1.80 (2.70) instead of 1.24; and night grid charging stays profitable.
- With a dynamic price and a binding cap, import is ranked by the distribution zone alone: the hourly
  energy price is paid from money that would be void. Cheap midday hours stop being a reason to
  charge; the night zone remains one.
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
   states at ρ = 30 %, so a ban gives up the evening price peaks, and it does nothing about the larger
   error, the valuation of PV export against storage. The existing switch stays for users who want it.
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
8. **Energy rates as the split settings** (`buy_energy_rate`, `buy_off_peak_energy_rate`; revision 1 of
   this ADR) — rejected because a dynamic-price contract has no energy rate to type: its energy part
   is the hourly forecast, and revision 1 could only give it a flat share. Distribution is the part
   that is a fixed number per zone under every contract.
9. **Catalog of energy or distribution prices per distributor and group** — rejected because five
   distributors × several groups change rates with every approved tariff, nobody would own the updates,
   and the public figures disagree (one comparison page gives PGE G12 2026 distribution as both
   0.49/0.09 and 0.55/0.15 PLN/kWh). The user reads two numbers off an invoice.
10. **A second forecast entity for distribution** — rejected for stage 1 because every group in the
    `CATALOG` is a two-zone schedule the integration already computes; an entity would move that work
    into user YAML. Revisit with three-zone groups (open question 2).
11. **A setting for what the deposit pays** (energy only, or the whole invoice) — rejected because the
    statute and the owner's decision say energy only; the one offer reported to differ also drops the
    expiry, which makes the whole model unnecessary for it.

## Consequences

- With the defaults nothing changes for any installation: no migration, no profile revision, golden
  tests untouched.
- Dynamic-price contracts with a zoned distribution tariff become configurable (`added` mode), with or
  without the deposit model. This is new buy-source behaviour and needs its own tests and docs.
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
  above ~2530 PLN (ρ = 30 %).
- Work to plan after the gate (each line is at least one ticket, in this order; T-005 as written
  covers lines 1–4 and should be split along them):
  1. buy price composition: the three distribution settings, `PriceSource.distribution_schedule` and
     its step in the source flow, `Slot.buy_energy_per_kwh`, slot boundaries, Preview;
  2. engine: the two `Problem` factors, the coefficients, tests for both deposit states and for
     byte-identity at 1.0;
  3. runtime and settings: the five deposit settings, `manual` and `auto` with fixed inputs, the
     settlement Store, the dead band, the `split_missing` guard, `dispatch_policy.settlement`;
  4. config flow, `strings.json`, `translations/en.json` and `pl.json`; doc sweep per `AGENTS.md`:
     `docs/model.md` (objective, new section), `guide.en.md` / `guide.pl.md` (Tariffs settings, buy
     source, plan attributes), `docs/source-requirements.md`, `docs/tariff-helper.md`, `README*.md`
     policy paragraphs, `docs/index.html`, rebuilt guides and builder;
  5. stage 2, only on request: the two statistics bindings and the energy-part cost counter.

## Open questions for the owner

Approving the gate accepts the recommended answer of each.

1. **Stage 2 (inputs from history).** Recommended: not now. Stage 1 takes the two yearly sums from
   settings; history bindings follow only if a site reaches the ramp.
2. **Groups outside the catalog** (G13, G12n, a dynamic distribution tariff). Recommended: stage 1
   gives them one distribution rate; a third zone is a separate task if someone needs it.
3. **Statute text.** The rules above come from seller pages and tax rulings that quote the RES Act;
   ISAP itself could not be opened here. Recommended: accept; the two numbers that matter (ρ and the
   distribution rates) are settings, so a difference in one contract is a settings change, not a
   model change.

Answered in revision 2, no longer open: the refund rule (cap of ρ × the month's export value), ρ per
contract (setting), what the deposit pays (energy only), the buy source of a dynamic-price site
(forecast + distribution tariff).

## Sources

- Energa, net-billing page: the deposit settles "the purchase of electricity from the seller", cannot
  cover distribution charges or other charges such as the trade fee; oldest amounts first; 12 months;
  refund up to 20 % (RCEm) or, from 1 February 2025, 30 % (RCE) of the month's deposit; the switch to
  RCE cannot be taken back — <https://www.energa.pl/dom/strefa-prosumenta/net-billing>
- Tauron, prosumer zone: refund cap as a share of the value of energy exported in the calendar month
  the refund concerns; the rest is cancelled; the switch to hourly prices is irreversible —
  <https://www.tauron.pl/dla-domu/obsluga-i-pomoc/strefa-prosumenta>
- PGE, prosumer guide (12 March 2025): 12-month use, refund limit of 20 % (RCEm) or 30 % (RCE) of the
  value of energy exported in the month —
  <https://www.gkpge.pl/dla-domu/strefa-klienta/przewodnik-prosumenta/pytania-i-odpowiedzi>
- Ministry of Climate and Environment (15 June 2025): prosumers from before 1 July 2024 may stay on
  RCEm or switch by declaration; deposit raised by 23 %; refund 20 % → 30 % in the hourly model —
  <https://www.gov.pl/web/klimat/korzystne-zmiany-dla-prosumentow>
- RES Act art. 4c sec. 2, 5, 6, 7 and art. 4 sec. 11 item 2, as quoted in tax rulings on net-billing,
  for example the one of 4 June 2025 (0114-KDIP4-1.4012.121.2025.2.MK); read through search excerpts,
  not in full —
  <https://www.inforlex.pl/dok/tresc,FOB0000000000006973607,Opodatkowanie-wprowadzania-energii-elektrycznej-do-sieci-w-systemie-net-billing-przez-prosumenta-Interpretacja-indywidualna-z-dnia-4-czerwca-2025-r-Dyrektor-Krajowej-Informacji-Skarbowej-sygn-0114.html>
- Tauron, dynamic prices: TGE RDN Fixing I plus 0.1097 PLN/kWh gross; distribution charges by the
  operator's tariff — <https://www.tauron.pl/dla-domu/prad/prad-z-usluga/ceny-dynamiczne>
- Add-on range and monthly fees of dynamic-price offers in 2024 —
  <https://www.gramwzielone.pl/trendy/20318870/male-zainteresowanie-umowami-z-cena-dynamiczna-wsrod-odbiorcow-energii-w-polsce>
- Offer applying the deposit to the whole invoice (single blog, not verified) —
  <https://laczynasnapiecie.pl/blog/net-billing-pstryk-depozyt-oplaty-dystrybucyjne-ceny-dynamiczne>
