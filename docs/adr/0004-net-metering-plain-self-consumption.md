# ADR-0004: Net-metering — the planner stands down; plain self-consumption, hourly costs are an approximation

Status: accepted
Date: 2026-10-12

## Context

The two Polish net-metering setup profiles (`pl_net_metering_80`, `pl_net_metering_70`,
`setup_profiles.py:83-102`) pre-fill one value, `sell_multiplier` 0.8 or 0.7, so the planner sees
`sell = ratio × buy` in every interval. The strategy stays `cost_min`; `allow_grid_charge` and
`allow_battery_export` keep their shipped default, off.

Net-metering is not settled per hour. Ustawa o odnawialnych źródłach energii, consolidated text Dz.U. 2026
poz. 68, art. 4:

- ust. 1 — energy fed in is settled against energy drawn in the ratio 1 to 0.7 (above 10 kW) or 1 to 0.8;
- ust. 3 — the settlement adds the hourly balances of the whole settlement period, the fed-in ones multiplied
  by the ratio, and carries the unused remainder forward;
- ust. 4 pkt 2 — on energy settled this way the prosumer pays no charge that depends on the energy drawn;
- ust. 5 and ust. 11 pkt 1 — fed-in energy can be taken back for 12 months, after which the seller keeps it;
- ust. 5a–5e — the oldest energy first; in a multi-zone tariff the same zone first, a zone's surplus then
  goes to the other zones starting with the dearest.

So an exported kWh is worth `ratio` kWh of a later import, nothing once the year's credit covers the year's
import, and the fixed part of the bill steps with the annual draw (capacity-fee bands at 500, 1 200 and
2 800 kWh). None of this is a price per hour, and none of it is known inside a 24-48 h horizon.

The calculator (energy-compass ADR-0030 pkt 3, T-433) measured its TypeScript port of this planner on
net-metering against plain self-consumption and decided that its EMS variant must run plain self-consumption
there. It asked this repository to reproduce the result on the real engine and to decide (T-449): what the
planner does on the two profiles, how costs are valued there, and what a net-metering user is told.

### Measurement

One year of the planner as Home Assistant runs it: `engine/optimize.py` at `main` 7f7ce2b, re-solved every
hour on a 24 h horizon with the forecast known at that hour, the first interval executed on the realized PV
and load by `tools/replay.py` `realize`, the SOC carried through the year. The resulting hourly grid import
and export are valued with the calculator's annual bill (energy-compass 95f27d2: 2026 tariffs, annual
netting, fixed fees with their bands). `K` = annual bill + battery wear − the energy left in the battery on
the last day above the reference's (at most 9 PLN, at the mean buy price).

40 homes: 5 operators × G11 and G12 × (2 500 kWh / 4 kWp, 3 600 / 6, 2 500 / 10, 6 000 / 10), ratio 0.8,
battery 10 kWh, 0.5 C, round trip 0.9, floor 10 %, wear 0.09 PLN/kWh; load forecast a 14-day mean, PV
forecast the previous day. The reference is the same battery in plain self-consumption
(`realize("SELF_CONSUME")`): its `K` equals the calculator's own self-consumption run in 40 of 40 homes.
Harness, inputs and the per-home table are artifacts of T-449.

Difference to plain self-consumption, PLN per year, median (range):

| Plan | Homes | `K` gain (+ = cheaper) | Worse / better by > 10 | Bill (+ = dearer) | Annual import, kWh | As Home Assistant reports it |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `cost_min`, the profile as shipped | 40 | 0 (−93…+70), mean −1 | 9 / 12 | +6 (−10…+136) | +186 (+33…+704) | +74 (−7…+601) |
| — G11 | 20 | −1 (−93…+10), mean −3 | 1 / 0 | +5 (0…+101) | +39 | −2 (−7…−1) |
| — G12 | 20 | +21 (−87…+70), mean +2 | 8 / 12 | +39 (−10…+136) | +591 (+317…+704) | +343 (+149…+601) |
| `cost_min` + `allow_grid_charge` | 13 | 0 (−209…+139), mean −17 | 5 / 5 | +5 (−258…+153) | +897 (+41…+1 340) | +783 (−4…+1 657) |
| `self_sufficiency` | 40 | −1 (−94…+14), mean −12 | 10 / 4 | +14 (0…+102) | +61 (+48…+98) | +30 (−11…+228) |
| `self_sufficiency`, `autonomy_reserve` off | 4 | +2 (0…+3) | 0 / 0 | +4 (+2…+6) | +40 | +195 (−1…+211) |
| `cost_min`, sell 0, terminal value 0 | 4 | +3 (0…+5) | 0 / 0 | +1 (−1…+4) | +38 | +58 (0…+82) |

"As Home Assistant reports it" is the same pair of flows priced the way `runtime.py:1232-1247`, the cost
card and `tools/replay.py` price them: import × buy − export × sell per hour, plus wear. Bill and import are
as metered.

What the numbers say:

1. **The profile as shipped does not lower the bill.** It never lowers the annual bill by more than 10 PLN
   (best −10) and raises it by more than 10 PLN in 15 of 40 homes. Every plan imports more than plain
   self-consumption, and 9 homes cross a fee band for it (+101 PLN each). With grid charging on the bill
   falls in 5 of 13 homes and rises in 4; the added cycling (wear +44 PLN, median) and the band steps (6 of
   13 homes) take it back.
2. **What `cost_min` saves on G12 is wear, not money on the bill.** It keeps the battery out of the cheap
   zone: the battery charges 55 % (49-66 %) of what self-consumption charges, which is 30-68 PLN of wear at
   0.09 PLN/kWh. That is the whole of the `K` gain in the 12 homes where it is ahead; in the 8 where it is
   behind, the band step is larger. The mean is +2 PLN per year on G12 and −1 over all 40 homes. On G11 the
   plan is self-consumption within 5 PLN, one band step aside.
3. **The reported gain is not there.** At hourly prices the same G12 plan shows +343 PLN per year
   (+149…+601) where the bill moves by −10…+136 the other way. With grid charging on it shows +783
   (up to +1 657) against a `K` of −209…+139.
4. **`self_sufficiency` is not plain self-consumption.** It is behind by more than 10 PLN in 10 of 40 homes
   and ahead in 4 (mean −12): the autonomy reserve keeps energy in the battery (the year ends at 72-100 %
   SOC) and the grid flow differs in about 2 000 hours.
5. The calculator's port (one plan per day) is behind by a median of 10 PLN with grid charging off and 26
   with it on, on the same homes; the hourly re-planning of the real engine is closer to self-consumption
   than the port, and the direction is the same: no reliable gain, spread of about ±100 PLN set by the fee
   bands.

Limits: one battery size (10 kWh), the operators' standard load profile, one year of prices, hourly
intervals (the integration plans on 15 minutes), wear 0.09 PLN/kWh where the shipped `wear_per_kwh` is 0,
grid charging on 13 homes (Enea, Energa, one PGE) and the last two rows on 4. The bill nets the zones of a
G12 tariff as one mean rate, where ust. 5b–5d net the same zone first; this affects the G12 rows in both
directions and is why no G12 sign is used in the decision below — only that the bill is never lower and the
import is always higher.

## Decision

1. **On net-metering the right plan is plain self-consumption, and the planner stands down.** A net-metering
   installation runs the battery in the inverter's own self-use mode (load first, no grid charging, no battery
   export); the plan is not sent to the inverter. Energy Compass does not offer price-driven dispatch as a
   saving under net-metering. This is the "plain self-consumption" outcome of energy-compass ADR-0030 pkt 3:
   the calculator models net-metering as the battery without EMS.
2. **No engine change, no new strategy, no profile revision.** The two profiles keep revision 3 and their one
   assignment (`sell_multiplier`), the strategy stays `cost_min`, and `allow_grid_charge` and
   `allow_battery_export` stay off by default. Where a user lets a controller follow the default plan anyway,
   the measured difference to self-consumption is the accepted limit: `K` mean −1, median 0, −93…+70 PLN per
   year; bill median +6, at most +136.
3. **Costs stay hourly and are labelled an approximation.** `expected_net_cost`, `consumption_cost`, the cost
   card and `tools/replay.py` keep pricing export at the interval's sell price. Under net-metering that is not
   the settlement: the kWh are exact, the money overstates exporting in dear hours and every shift between
   hours (point 3 above). Home Assistant gets no annual-netting bill: the yearly import, the yearly credit per
   zone and the fee bands are outside the planner's state and horizon. `tools/replay.py` is unchanged; on a
   net-metering file its `plan − self_use` difference is not a saving, and the guide says so.
4. **The user is told in the Preview and in both guides** (dev task T-455, tests first; the doc sweep of
   `AGENTS.md` applies):
   - `settlement_notes` (`setup_profiles.py:456-551`), for an entry whose settlement profile has a
     `sell_ratio` and currency PLN, after the existing ratio note, always:
     `Note: net-metering is settled on yearly kWh (an exported kWh takes back {ratio:g} kWh within 12 months), not on hourly prices: price-driven dispatch does not lower the yearly bill and the costs shown are an hourly approximation. Run the battery in the inverter's own self-use mode and do not let a controller follow this plan.`
   - the same entry with `allow_grid_charge` or `allow_battery_export` on, one more note naming what is on
     (`Allow grid charge is on` / `Allow battery export is on` / `Allow grid charge and Allow battery export are on`):
     `Note: {…}: under net-metering energy sent through the grid comes back at {ratio:g} and raises the yearly import; turn it off.`
   - `docs/guide.en.md` and `docs/guide.pl.md`: § Setup profiles (what a net-metering user sets today:
     inverter self-use, grid charging off, battery export off, controller not enabled; the numbers of the
     table above; the list of Preview notes), § Offline replay tool → Reading the result (hourly costs are not
     the net-metering settlement), § Deye inverter controller → Enabling (not on a net-metering installation).
5. **Return to this** when users ask for the controller to hold self-use by itself (alternative 2), or when
   an annual bill model exists in this repository (alternative 5).

## Alternatives rejected

1. **Profile preset `strategy: self_sufficiency` (a revision 4 of the two profiles)** — rejected because it is
   measurably not plain self-consumption and no better: behind by more than 10 PLN in 10 of 40 homes, ahead in
   4 (median −1, mean −12, to −94). Without the autonomy reserve it matches self-consumption in money on the
   4 homes measured (0…+3), but `autonomy_reserve` is a strategy-owned
   setting a profile may not assign (`FORBIDDEN_KEYS`), the grid flow still differs in 1 550-1 920 hours, and
   the reported gain (+195) is as unreal as that of `cost_min`.
2. **A dedicated strategy (`self_consumption`: every interval `SELF_CONSUME`, pre-filled by a revision 4)** —
   rejected because it buys what leaving the controller off already gives, at the price of a seventh strategy:
   a plan path that bypasses the MILP (none exists today), probes and consumption levels on that path, the
   controller's plan acceptance and parity tests, the `strategy_switch` blueprint, the select, three
   translation files and both guides. The stake against today's default is a mean of 1 PLN per year.
3. **Keep recommending the shipped `cost_min` plan on net-metering (no note)** — rejected because the plan
   does not lower the bill, pushes 9 of 40 homes over a fee band and reports a gain of 343 PLN per year on G12
   that the settlement does not pay. Its only real effect is less battery cycling, which is not what the user
   is shown.
4. **A settlement-aware objective (export valued at 0 or at `ratio` × a mean price, the yearly credit as a
   constraint)** — rejected because the quantities that decide the value of an export (yearly import, yearly
   credit per zone, fee bands) are not in a 24-48 h horizon, the calculator measured such a plan at a median
   of −9 PLN (−130…+260, ADR-0030 alternative 4), and the run with export valued at 0 lands on
   self-consumption (0…+5 PLN, 4 homes): there is nothing to gain by building it. It would also need a second
   price series, one for the objective and one for the reports.
5. **An annual net-metering bill in Home Assistant (sensors, cost card, replay)** — rejected because it needs
   a 12-month kWh account per tariff zone, the fee bands and the fixed fees of the user's tariff, none of
   which the integration holds, for a settlement closed to installations connected after 31 March 2022 (art. 4
   ust. 1a pkt 2). The calculator computes that bill; here the limit is stated instead.

## Consequences

- energy-compass T-450, T-451 and T-452 proceed as written: on net-metering the EMS variant is the battery
  without EMS. The engine did not change, so the `cost_min` golden fixtures of the calculator stay valid at
  any `haSha` from 7f7ce2b on.
- One dev task in this repository (T-455) adds the two Preview notes and the guide sections of point 4;
  until it lands, a net-metering user is not warned in the product.
- A net-metering user who enables the Deye controller on the default plan gets the limit of point 2, and
  with `allow_grid_charge` on the wider one (−209…+139 PLN per year on 13 homes, import +897 kWh).
- The reported costs of a net-metering installation remain an hourly approximation; comparing two strategies
  by `expected_net_cost` or by the replay there is meaningless, and the guide says so.
- Risk: the measurement uses one battery size, a standard load profile and a bill that nets G12 zones as a
  mean. A home whose shape differs may see a larger difference in either direction; the decision rests on
  the two findings that do not depend on that — the import is always higher and the hourly valuation is not
  the settlement.
