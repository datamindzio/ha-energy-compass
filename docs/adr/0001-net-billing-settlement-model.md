# ADR-0001: Net-billing settlement model — energy/distribution split and deposit value factors

Status: proposed — becomes accepted when the owner approves it at the gate
Date: 2026-10-12 (revision 3, after the annual-bill measurement of T-448)
Amended: 2026-10-13, T-474 — the offline tools read `buy_energy` with its sign; the engine contract
is unchanged ([Amendment T-474](#amendment-t-474-2026-10-13-the-oracle-reads-the-energy-part-with-its-sign))

This ADR fixes the model, the settings and the contract between runtime and engine. Revision 2 was
implemented on `main` by T-005 (`engine/settlement.py`, `settlement.py`) and is in no release yet
(v0.1.37 does not contain it).

Revision 3 changes, from a one-year replay of the real planner valued by the bill
([Revision 3](#revision-3-the-model-measured-on-the-annual-bill)):

- `deposit_model: off` stays the default and is the **only recommended setting**, also for a site
  whose deposit is forfeited. No configuration of the model lowered the yearly bill by more than
  44 PLN, and the factor at the cap (π = 1) raised it in 40 of 40 hourly-RCE homes.
- `auto` is **withdrawn** with its two inputs, the daily evaluation and the dead band. `manual` stays
  as an **experimental** setting.
- The refund share applies to the export value **without** the 1.23 coefficient, so the floor of the
  export value factor on a sell price that contains 1.23 is ρ / 1.23 (0.244 with hourly RCE, 0.163
  with RCEm), not ρ.
- The profile heuristics (export episode hurdle 1 PLN, import penalty 0.20 PLN/kWh) stay as they are,
  and are not scaled with the planning prices.
- Decisions 2, 3 and 5 below are kept as the record of revision 2; where revision 3 changes them the
  text says so.

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
| Settlement price of export, and with it ρ | **RCEm** (monthly): prosumers who started before 1 July 2024 and did not switch → ρ = 20 %. **RCE** (hourly): everyone who started from 1 July 2024, and older prosumers after a declaration to the seller that cannot be taken back → ρ = 30 % (in force since 27 December 2024; Energa applies it from 1 February 2025) | `deposit_refund_percent`; the sell source already selects RCE or RCEm. The share is of the **value of the exported energy**, which the statute names separately from the deposit it raises by 1.23 (art. 4 sec. 11a); one seller page words it as a share "of the month's deposit" (revision 3, point 2) |
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

Revision 3 withdraws `auto`, `deposit_yearly_value`, `deposit_yearly_energy_cost` and stage 2, and
marks `manual` experimental. The table and the bullets are the revision 2 record.

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

Revision 3 replaces ρ in these two formulas and in `manual` below by `ρp = ρ / 1.23`, the refund share
expressed on a sell price that contains the 1.23 coefficient (point 2 of revision 3), and withdraws
`auto` together with its hysteresis and the `inputs_missing` and `held` reasons.

`auto` (withdrawn in revision 3):

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

Revision 3 measured the first, second and fourth row: with the amounts unscaled, π = 1 raises the
yearly bill; scaling them removes most of that and still does not beat `off` (points 3 and 4 of
revision 3). The rules stay as written.

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

## Revision 3: the model measured on the annual bill

Revision 2 was accepted on a worked example. Nothing valued a year of its plans by the bill. The
calculator repository did that for its TypeScript port of this planner (energy-compass ADR-0030,
T-433): the factors raised the yearly bill, by up to 1 672 PLN at π = 1, with the note "indicative
for Home Assistant — reproduce before deciding". T-448 reproduced it on the planner itself.

### Method

- **Planner**: `engine/optimize.py::solve` of `main` at 7f7ce2b, problems built by
  `tools/replay.py::build_problem`, plans executed on the realized series by
  `tools/replay.py::realize`. Horizon 24 h in 1 h slots, the state of charge carried through the
  year, the daily PV-export budget reset at local midnight, mode duration 60 min.
- **Homes**: the 80 net-billing homes of T-433 — five distributors × G11/G12 × four sizes
  (2 500 kWh with 4 kWp, 3 600 with 6, 2 500 with 10, 6 000 with 10), each settled with hourly RCE and
  with RCEm, on the offer the calculator picks for a planner. 8 760 h from 12 October 2025, the
  calculator's price series, load and PV from the distributor's standard profiles. `n` in each
  table is the number of homes the configuration ran on.
- **Site**: 10 kWh, floor 10 %, 0.5 C, round trip 0.9, wear 0.0916 PLN/kWh, grid 14 kW, the
  `pl_net_billing` profile (import penalty 0.20, terminal value 0.60, export episode hurdle 1,
  `limit_export_to_pv` on) with `allow_grid_charge` and `allow_battery_export` **on**. Both are off
  by default; the measurement describes a site that switched them on.
- **Forecast**: yesterday's PV and the 14-day mean load of the hour, never the realized series.
- **Cadence**: re-plan every 6 h, the first 6 h of each plan executed. Checked against 1 h and
  against plan-once-a-day on six homes (table below).
- **Valuation**, two bills for the same hourly import and export:
  - *annual* — the calculator's formula: the deposit `D = 1.23 × V` pays energy and excise `E`, the
    refund is `min(D − min(D, E), ρ × V)`;
  - *ledger* — monthly vintages booked the following month, used oldest first for 12 months, the
    refund of each vintage capped at ρ × that month's export value, steady state of a repeated year.
  `K` = bill + battery wear + the change of stored energy at the mean buy price. ρ = 30 % on the RCE
  homes, 20 % on the RCEm homes.
- **Checks**: the bill function returns the calculator's bill for the same flow (2 589.78 PLN on
  both sides without a battery; 503.01 against 502.99 on the port's 10 kWh flow). `K` of `off` is
  within 1 PLN of the port's `K` at the median (n = 66, −8 … +103). 76 of 763 580 solves ended
  without a plan; those intervals ran as self-consumption.

Both valuations are kept because they disagree where it matters. The annual formula pools the year,
so winter purchases absorb summer deposit and the refund cap is one yearly number. The ledger is
closer to the statute (the cap is per month) and gives a bill higher by 125 PLN at the median and by
up to 353 PLN on the RCE homes with `off`. Under `off`, 34 of 40 RCE homes have `D > E`, the yearly
cap binds in 21 and the ledger forfeits deposit in 30 (median 402, up to 2 349 PLN a year); 6 of 26
RCEm homes forfeit.

### Results

Difference to `off` in PLN per year, positive = dearer. "worse" and "better" count homes beyond
10 PLN.

Hourly RCE, 10 kWh:

| Configuration | n | annual: median (min … max) | worse / better | ledger: median (min … max) | worse / better |
| --- | --- | --- | --- | --- | --- |
| `manual`, π = 0.25 | 24 | +18 (+3 … +50) | 14 / 0 | −1 (−21 … +57) | 9 / 4 |
| `manual`, π = 0.5 | 40 | +58 (+6 … +143) | 36 / 0 | +4 (−27 … +102) | 18 / 11 |
| `manual`, π = 0.75 | 24 | +111 (+19 … +215) | 24 / 0 | +41 (−44 … +152) | 17 / 2 |
| `manual`, π = 1 (φx = 0.30) | 40 | +292 (+68 … +431) | 40 / 0 | +146 (+21 … +306) | 40 / 0 |
| π = 1 with ρ / 1.23 (φx = 0.244) | 16 | +348 (+110 … +516) | 16 / 0 | +147 (+54 … +332) | 16 / 0 |
| π = 1, heuristics scaled | 16 | +2 (−33 … +57) | 5 / 4 | 0 (−25 … +150) | 6 / 3 |
| `auto`, `D` and `E` of the year without a battery | 24 | 0 (0 … +122) | 6 / 0 | 0 (0 … +106) | 6 / 0 |
| `auto`, `D` and `E` of the `off` year | 24 | +78 (0 … +333) | 15 / 0 | +13 (−8 … +151) | 12 / 0 |
| `allow_battery_export` off | 16 | +423 (+154 … +662) | 16 / 0 | +244 (+102 … +493) | 16 / 0 |
| export episode hurdle 0 | 24 | −15 (−94 … −1) | 0 / 14 | +10 (−94 … +39) | 12 / 7 |
| import penalty 0 | 24 | −11 (−36 … 0) | 0 / 13 | +20 (−22 … +75) | 16 / 4 |
| hurdle 0 and import penalty 0 | 27 | +14 (−37 … +75) | 16 / 7 | +62 (−37 … +220) | 24 / 1 |

- The best π of {0, 0.25, 0.5, 0.75, 1} chosen per home with hindsight (n = 24): on the annual bill
  it is 0 in all 24; on the ledger it gains 6 PLN at the median and 44 PLN at most (π = 0 in 10
  homes, 0.25 in 3, 0.5 in 8, 0.75 in 3, 1 in none).
- Where the yearly cap binds under `off` (21 homes) π = 1 is still dearer in all 21: +160 at the
  median (+68 … +421) on the annual bill, +63 (+21 … +182) on the ledger.
- `auto` from the year without a battery leaves 18 of 24 homes at π = 0 and sets π = 1 in 6; from the
  `off` year it sets π > 0 in 16. No home is cheaper by more than 10 PLN on either bill.
- Other capacities (5, 15, 20 kWh; 15 rows): π = 1 is dearer in all, +128 at the median
  (+62 … +314) on the annual bill, +81 (+38 … +153) on the ledger.
- Monthly RCEm, 10 kWh (n = 24–26): π = 1, hurdle 0, import penalty 0 and both at 0 all stay within
  −9 … +6 PLN on both bills. The price is flat within a month, so a factor on it moves no decision.
- "Heuristics scaled" is π = 1 with the hurdle at ρ × 1 PLN and the import penalty and the terminal
  value multiplied by mean `buy_plan` / mean `buy`. It is cheaper than plain π = 1 by 252 PLN at the
  median on the annual bill and by 56 PLN on the ledger.

Cadence, π = 1 against `off` at 10 kWh, re-plan every 1 h / 6 h / 24 h:

| Home | annual | ledger | `K` of `off` |
| --- | --- | --- | --- |
| Energa G12, 3 600 kWh, 6 kWp, RCE | +392 / +377 / +778 | +298 / +270 / +643 | 1 125 / 1 107 / 1 133 |
| PGE G12, 2 500 kWh, 10 kWp, RCE | +92 / +90 / +198 | +41 / +32 / +154 | 10 / 6 / 18 |
| PGE G12, 3 600 kWh, 6 kWp, RCE | +228 / +183 / +661 | +91 / +52 / +402 | 794 / 795 / 807 |
| Stoen G11, 6 000 kWh, 10 kWp, RCE | +200 / +198 / +910 | +200 / +198 / +910 | 2 586 / 2 575 / 2 553 |
| Tauron G11, 2 500 kWh, 10 kWp, RCE | +97 / +95 / +160 | +53 / +43 / +250 | −57 / −66 / −54 |
| PGE G12, 3 600 kWh, 6 kWp, RCEm | +3 / −1 / +190 | +3 / −1 / +190 | 1 587 / 1 593 / 1 599 |

The 6 h cadence reproduces the 1 h cadence and slightly understates the loss. Plan-once-a-day, with
hours 7–24 of a plan executed, multiplies it by 2 to 4 and creates a loss on RCEm that the planner
does not have. That is the mode of the calculator's port: its π = 1 loss on the same 40 RCE homes is
+758 PLN at the median against +292 here, and +224 against 0 on RCEm. The sign of T-433 holds for
Home Assistant; its size does not.

### Decisions

1. **`off` is the only recommended setting; `auto` is withdrawn; `manual` is experimental.**
   - `auto` is removed before the first release that would carry it: the `auto` option, the settings
     `deposit_yearly_value` and `deposit_yearly_energy_cost`, the daily evaluation, the settlement
     Store, the dead band, the reasons `inputs_missing` and `held`, and stage 2. A stored `auto`
     (development installs only) is read as `off`. It raised π in 6 to 16 of 24 homes and none got
     cheaper.
   - `manual` stays, labelled experimental in the selector and the guides, with the measured result
     next to it. It is the handle a later model is tested through, and it costs one option and one
     number.
   - A deposit setting is recommended, or `auto` returns, only when a one-year replay of the planner
     at a re-plan interval of 6 h or less shows, against `off` and on **both** bills, a lower median
     and no home dearer by more than 10 PLN a year. The numbers go into this ADR.
2. **Refund share base and ρ per price kind.** The statute caps the refund at 20 % or 30 % of the
   *value of the energy exported in the month* (art. 4 sec. 11a) and raises the *deposit* by 1.23
   elsewhere, so the cap is ρ × V while the sell price the planner sees is 1.23 × the settlement
   price. One more unit of export under a binding cap is therefore worth `ρp = ρ / 1.23` of the sell
   price: 0.244 with hourly RCE, 0.163 with RCEm. `engine/settlement.py` uses ρ (0.30, 0.20).
   - The engine divides `deposit_refund_percent` by 1.23 in `factors` and in the `manual` derivation
     of π. 1.23 is a constant of the model, like `D = 1.23 × V`; a sell price without it already
     misstates the deposit and Preview says so.
   - `deposit_refund_percent` keeps the statutory meaning and the default 30 (hourly RCE, the
     settlement of everyone who started from 1 July 2024). 20 for RCEm stays a documented value: the
     integration does not know the settlement kind, and on RCEm the factor changes the bill by less
     than 10 PLN a year either way. A seller that refunds a share of the deposit is entered as
     36.9 or 24.6.
   - With the corrected base π = 1 is dearer than with the coded one by 76 PLN at the median on the
     annual bill (+23 … +156) and by 57 PLN on the ledger (+21 … +133). The correction is made
     because the number is wrong, not because it helps.
3. **Heuristics under planning prices: not scaled.** Scaling the hurdle, the import penalty and the
   terminal value takes π = 1 on its 16 homes from +274 at the median to about `off` (+2 annual,
   0 ledger; 5 or 6 of 16 homes dearer, 4 or 3 cheaper). A model that needs three more rules to reach the result of doing nothing
   is not worth them. If π > 0 is ever proposed as a recommended setting again, scaled heuristics
   are part of the proposal and of its measurement.
4. **Profile heuristics: unchanged.** Hurdle 1 PLN and import penalty 0.20 PLN/kWh stay.
   - Hurdle 0: −15 PLN at the median on the annual bill, +10 on the ledger (12 homes dearer, 7
     cheaper). Import penalty 0: −11 annual, +20 ledger (16 dearer, 4 cheaper). Both at 0: +14 and
     +62. On RCEm: within 9 PLN.
   - The two bills disagree on the sign, the effects are 1–2 % of `K` (806 PLN annual, 992 ledger at
     the median) and of the size of the cadence error above. Neither amount is shown to be wrong,
     and both also limit mode changes and the exposure to forecast error, which no bill here prices.
   - Where the yearly cap does not bind (11 homes) hurdle 0 is cheaper in all 11 on the annual bill
     (−57 at the median) and in 5 on the ledger (3 dearer). Not enough for a change; it is the first
     thing to re-measure once a replay with a measured forecast exists.
5. **What a user whose deposit is forfeited sets today: nothing.** `deposit_model` stays `off`.
   Battery export, where the user allows it, stays allowed: switching it off cost 154 to 662 PLN a
   year on the annual bill and 102 to 493 PLN on the ledger, in all 16 homes, and 411 / 202 PLN at
   the median in the homes whose yearly cap binds. An evening export at a high price raises the
   month's export value and with it the refund cap, also when the rest of the deposit is void.

### Why the factors lose

Measured: on the 16 homes that ran both, scaling the fixed amounts removes 97 % of the summed loss
of π = 1 on the annual bill and 82 % on the ledger (decision 3). With φx = 0.30 an export period must earn 1 PLN of a value cut to 30 %,
and an import is charged a 0.20 penalty on a price cut to its distribution part.

Not measured, from the settlement rules:

- One yearly π devalues every export of the year. On the ledger the forfeited amount sits in the
  summer vintages; the October–March exports are used in full and are worth their full price.
- A uniform factor does not change which hour is the best to export in. It only moves the
  comparison with wear, with the hurdle and with imports, and there it stops exports that still pay
  (decision 5).

### Limits

- Load and PV are distributor standard profiles and a PV model, as the meter sees them: `pv` is the
  surplus after self-consumption, so the `limit_export_to_pv` budget is tighter than on a site that
  reports gross PV.
- One year, one price history, a simple forecast, no standby loss, 10 kWh in the matrix (other
  capacities for π = 1 only), a 6 h cadence calibrated on six homes.
- The measurement code of T-448 was a scratch harness around `engine/` and `tools/replay.py`, stored
  with its results as artifacts of T-448. It is now the tool `tools/replay_year.py` (T-460, tested on
  a synthetic series; see the guide, "Annual settlement oracle"). The tool books the ledger by the
  exact two-year rule of the calculator's ADR-0031 (a warm-up year from a full account), not the
  three passes from an empty account behind the `KL` numbers of T-448, and takes the fixed part of
  the bill as an input, so a charge band that depends on the annual import is not modelled.
- The tool's `buy_energy` input was the planner's clamped `energy_t` until
  [Amendment T-474](#amendment-t-474-2026-10-13-the-oracle-reads-the-energy-part-with-its-sign); the
  80 homes of T-448 have no hour with a negative energy part (lowest +0.42 PLN/kWh), so no number of
  this revision depends on it.

## Amendment T-474 (2026-10-13): the oracle reads the energy part with its sign

Scope: the input of `tools/replay.py` and `tools/replay_year.py`. The engine, the runtime, the
settings and every plan stay as they are.

### Context

- The oracle's bills pay the liability `E⁺ = Σ max(E_t, 0)` over the months (T-473, the calculator's
  ADR-0031 Amendment T-471): art. 4c sec. 3 of the RES Act sums import × price over the hours of
  **one settlement period**, so hours at a negative price lower the month's liability, and a month
  that ends below zero owes nothing.
- The tool's hourly input cannot say so. `tools/replay.py::_interval` rejects a negative
  `buy_energy`, copying `engine/normalize.py` (`buy_energy_per_kwh` in `[0, max(buy, 0)]`). That
  bound belongs to the planner: decision 1 clamps `energy_t` at 0 so that a slot with a negative
  energy price keeps its real price in `buy_plan` (decision 3, missing inputs). T-460 used the same
  field as the base of the bill.
- With the clamp the month's energy part is `Σ import × max(e_h, 0)`: too high by the money of the
  negative hours, in **every** month with a draw at a negative energy price, not only in a month
  that ends below zero. `gross = fixed + Σ import × buy` is right (the `buy` column is signed), so
  the error is in `applied` and the refund that follows from it: the bill comes out too low where
  the deposit exceeds the liability and the refund cap binds.
- Size, an **estimate** (`artifacts/T-474/clamp_error.py`, `.txt`; the 40 PV homes of the
  calculator's T-471 on its one offer without an index floor, EMS plan of the calculator's port, one
  price year; hourly flows are not stored, so the money of the negative hours is the kWh drawn below
  zero per month × the unweighted mean price of those hours). The estimate is low: it falls below
  `N = Σ max(−E_t, 0)` in 16 of 40 homes at 10 kWh, which the true value cannot. Bill with the
  clamped input minus bill with the signed one, PLN a year, never above zero; "homes" are those
  with a difference over 0.005:

  | Capacity (realistic forecast) | Homes drawing below zero | Homes with a month below zero | Homes with `D > E⁺` | Annual bill too low: homes, median of those, max | Ledger bill too low: homes, median of those, max |
  | --- | --- | --- | --- | --- | --- |
  | 5 kWh | 38 of 40 | 4 | 10 | 8, 0.0, 0.1 | 8, 0.0, 0.1 |
  | 10 kWh | 40 | 26 | 15 | 5, 5.4, 5.8 | 7, 5.4, 5.8 |
  | 15 kWh | 40 | 38 | 20 | 10, 8.4, 20.0 | 14, 8.3, 19.8 |
  | 20 kWh | 40 | 39 | 24 | 13, 9.0, 24.3 | 18, 9.1, 37.0 |

  With a perfect forecast the maxima are 0.2, 7.8, 21.7 and 29.4 PLN on the annual bill and 0.2,
  7.8, 19.7 and 29.4 on the ledger. Decision 1 of revision 3 admits a deposit setting only if no
  home is dearer by more than 10 PLN a year: the estimated error is about half of that threshold at
  10 kWh and two to four times it at 15 and 20 kWh.

### Decision

1. **`buy_energy` in the tool input is the energy part as invoiced, with its sign** (energy price,
   excise and VAT; currency/kWh). Rules of `_interval`, for JSON and CSV, in both tools:
   - finite; missing or empty = 0 (unchanged);
   - `buy_energy ≤ max(buy, 0)` (unchanged, tolerance 1e-9);
   - below zero it must also satisfy `buy_energy ≤ buy`: the distribution part `buy − buy_energy` is
     never negative. `buy = −0.5, buy_energy = 0` stays valid (a file written with the clamp).
2. **The planner gets the engine's value.** Every `Slot` the tools build carries
   `min(max(buy_energy, 0), max(buy, 0))`, the `energy_t` of decision 1, from one helper in
   `tools/replay.py` used by `replay._slots` and `replay_year.run_year`. `validate_problem` is not
   changed and never sees a negative value; a file without a negative `buy_energy` gives the same
   plans and the same bills as before.
3. **The bill gets the signed value.** `replay_year.measure` passes `Interval.buy_energy` to `bill()`
   as today. `E_t` is then the signed sum of the month, `E⁺` and the ledger follow T-473.
4. **The engine contract stays**: `Slot.buy_energy_per_kwh` in `[0, max(buy_per_kwh, 0)]`,
   `runtime._buy_composition` and `planning_buy` untouched. The integration has no bill, and with
   `deposit_model: off` nothing reads the field.

### Alternatives rejected

1. **Leave the tool, document the limit** (option A of T-474): rejected. The limit is wider than
   "a negative month reaches `bill()` only through the Python API": every month with a draw below
   zero is overstated, the bill moves by an amount of the order of the 10 PLN threshold (estimate
   above), and on the one kind of offer where the calculator found the case (26 of 40 homes with a
   month below zero at 10 kWh) the rule of T-473 would stay unreachable from the command line. The
   change is one validation rule and one helper in the tools.
2. **Signed `buy_energy_per_kwh` in the engine**, `buy_plan = buy − (1 − φe) × energy` with a
   negative `energy`: rejected. It changes plans only with π > 0, which revision 3 does not
   recommend. It is right only in a month that ends above zero with a deposit in excess (the
   negative hour then lowers a liability the deposit would have paid); in a month that ends below
   zero, or with a deposit that is short, the hour is worth its full price, and a 24 h horizon does
   not know which month it is in. The clamp plans such an hour as `off` does. A change to planning
   prices also needs the year replay of decision 1 of revision 3, and this repository has no corpus
   with a negative energy part.
3. **A second input column** for the bill next to a clamped `buy_energy`: rejected. One invoice
   number would be written twice, and the clamp is a formula the tool applies itself.
4. **Clamp the hour in the bill and correct the month afterwards**: rejected, it is the signed sum
   computed in two steps. The calculator rejected the hourly floor in the bill for the same reason
   (ADR-0031 Amendment T-471, alternative 4).

### Consequences

- T-476 (dev, after T-473): `_interval`, the helper, tests, tool docstrings, both guides ("Offline
  replay tool" and "Annual settlement oracle": the `buy_energy` paragraphs and Limits) and the
  rebuilt guides. `custom_components/` has no diff.
- Limits that stay, to be written in the guides: the settlement period is the calendar month (a
  seller billing over a longer period nets across its months; the calculator measured 0 to +67.6 PLN
  a year, ADR-0031 Amendment T-471 point 8), and energy is valued per hour, so a negative quarter
  inside a positive hour is not seen.
- No stored number changes: T-448's homes have no negative energy hour and T-460's tests use a
  synthetic series without one.
- Risk: the size above is an estimate on standard profiles, one offer and one price year. The rule
  does not depend on it.

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

Added in revision 3:

12. **Keep `auto` and label it experimental** — rejected because a label does not change what it
    does: it switched the factors on in 6 to 16 of 24 homes, made up to 15 of them dearer (up to
    333 PLN a year) and none cheaper. It is in no release, so removing it needs no migration; a
    released option would need one.
13. **Withdraw the whole model, `manual` and the distribution component included** — rejected
    because the distribution component is buy-source behaviour of its own (a dynamic energy price
    plus a zoned distribution tariff) and `manual` is the only way to test a later model on the
    replay. Removing both is a larger change than removing `auto` and buys nothing the label and the
    default do not give.
14. **Repair the model instead: scaled heuristics, a monthly π, the corrected base** — rejected for
    now. Scaled heuristics alone bring π = 1 back to `off`, not below it, and the best π per home
    chosen with hindsight gains 44 PLN a year at most on one bill and nothing on the other. A
    monthly π is alternative 4 again, with the same need for a 12-month forecast of both sides.
15. **Leave the refund base as coded** (ρ on a price that contains 1.23) — rejected because the
    documented meaning of `export_value_factor` at the cap would stay wrong by a factor of 1.23,
    although the coded value loses less (decision 2 of revision 3).
16. **Lower the export episode hurdle or the import penalty in `pl_net_billing`** — rejected: 10 to
    20 PLN a year at the median with opposite signs on the two bills, and nothing on RCEm
    (decision 4 of revision 3).
17. **Adopt the calculator's numbers without a replay of this planner** — rejected because the port
    plans once a day and executes the tail of the plan, which this planner never does; the loss it
    reports is 2 to 4 times the loss measured here and exists on RCEm only there.

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
- The reference site gets factor 1.0 today. Revision 2 expected the feature to pay off for sites
  whose PV is large against their consumption. Revision 3 measured those sites (10 kWp against
  2 500 kWh): it does not.
- Revision 3: with `auto` gone, five settings become three (`deposit_model` with `off` and `manual`,
  `deposit_refund_percent`, `export_value_factor`), `dispatch_policy.settlement` loses
  `unused_share`, `yearly_deposit`, `yearly_energy_cost` and `evaluated_on`, and the reasons are
  `ok`, `off` and `split_missing`. The distribution component is untouched.
- Revision 3: the guides, the READMEs and the setting descriptions say that `off` is the setting for
  every site, the forfeited-deposit site included, and carry the measured numbers for `manual`.
- Revision 3: the yearly bill of a plan is now the test of a change to the planning prices or to
  the profile amounts. Until the harness is a tool in `tools/`, the artifacts of T-448 are the only
  copy.
- Work after revision 3:
  1. T-459 — withdraw `auto`, label `manual` experimental, the refund base, strings and the doc
     sweep with the text for the forfeited-deposit site;
  2. T-460 — the yearly replay and the two bills as a tool with tests;
  3. T-461 (calculator repository) — the yearly refund formula against the monthly ledger, and the
     size of the π loss quoted in energy-compass ADR-0030.
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

1. **Stage 2 (inputs from history).** Closed by revision 3: `auto` is withdrawn, so there is nothing
   to feed.
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

- Revision 3: RES Act amendment of 27 November 2024 (Dz. U. 2024 item 1847), art. 4 sec. 11a — the
  refund may not exceed 20 % (art. 4b sec. 1, RCEm) or 30 % (art. 4b sec. 2, hourly RCE) of "the
  value of the electricity fed into the grid in the calendar month the refund concerns"; the 1.23
  coefficient raises the deposit of the month, not that value. Read through search excerpts of the
  act, not on ISAP — <https://eli.gov.pl/eli/DU/2024/1847/ogl/pol/pdf>
- Revision 3: energy-compass `docs/adr/0030-cel-planu-ems-a-rozliczenie-prosumenta.md` (T-433), the
  calculator's measurement on its port of this planner; its yearly formula is
  `packages/calc/src/cost/settlement.ts`.
- Revision 3: artifacts of T-448 (`README-t448.md`, `harness.py`, `runner.py`, `results.jsonl`,
  `tables.txt`) — the replay, the two bills and every number of the revision 3 tables.
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
