# Bundled Polish tariff rates and the three-zone G13 groups — design

Date: 2026-10-20 · Status: proposed (owner decisions of 2026-10-20 recorded in §3.1) · Task: T-100
(re-run of T-098) · Base: `main` at 2606734 (release 0.1.43 + T-097, T-099) · Data: energy-compass
`main` at 6a0f8a7, `packages/data/src` (read only)

This spec extends `2026-10-04-tariff-sources-design.md` (the tariff-sources spec, "TS"). It
**reverses TS §3.1 "rates are never shipped"** (owner decision O-1, §3.1): choosing a tariff
group and a seller offer now pre-fills the existing rate settings, which stay editable.

---

## 0. Summary

A Polish household opens **Tariffs → Buy source → Polish tariff schedule**, picks the tariff group
(13 catalog keys, two of them new three-zone groups), then the operator (G11 only), the seller offer
and three charge answers (phases, billing period, yearly consumption band). The flow writes the
full net buy price per zone, the distribution rate per zone, VAT and the monthly charge into the
existing Tariffs settings. The user types nothing and can still edit every value in
**Tariffs → Values**.

The rates come from `custom_components/energy_compass/sources/tariff_rates.json`, a snapshot of
energy-compass `osd/*.json`, `offers/*.json` and `programs.json`. `tools/build_tariff_rates.py`
writes it from a local energy-compass checkout and checks it in CI without one. The runtime never
reads the snapshot. Rates stay in settings, and the optimizer behaves exactly as if the user had
typed them.

The result per zone, (energy + excise + distribution) × (1 + VAT), equals the energy-compass
calculator to the last digit (appendix A; tolerance 0.001 zł/kWh). No config-entry `VERSION`
bump is needed.

---

## 1. Design

### 1.1 Goal, scope, out of scope

**Goal.** Zero-typing buy price for a Polish household on a URE (default seller) or fixed-price
offer, for every group of the five large operators that energy-compass models.

**In scope.**
1. A third band, `mid_peak`, in the zone model, with two new catalog keys:
   - `tauron_g13` (Tauron Dystrybucja G13);
   - `enea_g13active` (Enea Operator G13active).
2. Two new settings for the third zone: `buy_mid_peak_rate` and `buy_distribution_mid_peak_rate`.
3. Runtime, Preview, source list and profiles (revision 4) handle three bands.
4. A bundled rate snapshot, its generator with `--check`, and a CI step.
5. New flow steps: operator (G11 only), offer and charges. Saving them writes the prefill.
6. Prefill provenance stored on the schedule; stale-rate notes in Preview.
7. The EN+PL doc sweep and the generated-files row in AGENTS.md.

**Out of scope (owner).**
- Dynamic offers (kind `dynamic`).
- Any change to the sell side.
- Online fetch.
- The current-price sensors (T-097).
- Any release or version bump.

**Ambiguities resolved here.**

- A1 — *Task text says "profiles revision 3".* Revision 3 already exists: `setup_profiles.py`
  `_REVISION_3` added `settled_sell_sources`. The new catalog keys therefore go into
  **revision 4** (DD-11).
- A2 — *"VAT as configured today".* VAT stays the shared `vat_percent` with the per-side
  `buy_apply_vat` switch (`runtime.py:434`). The prefill turns the buy switch on. It sets
  `vat_percent` to the snapshot rate only when the sell side cannot be affected (DD-8). Rates
  are written net, so the runtime applies VAT exactly once.
- A3 — *G13active has no URE seller price* (energy-compass `catalog.ts:47` offers a group only
  when its URE offer prices it). No fixed offer prices it either. The flow offers
  "distribution only" for it (DD-10).
- A4 — *Which clock?* energy-compass reads zone rules in `zoneClock: "standard"` (UTC+1 all
  year). HA reads them on the local clock unless `meter_winter_clock` is set (TS DD-4). Prefill
  writes rates per zone, and rates do not depend on the clock. The clock choice stays the user's
  and is unchanged. The cross-check test (DD-14) compares the zone hours on the CET clock, where
  both definitions must agree.
- A5 — *Transitional charge band.* Every operator has `[{maxKWh: null, pln: 0}]` today. The
  generator accepts only transitional band edges that are a subset of the capacity edges, so the
  consumption answer picks both bands unambiguously (DD-7).

### 1.2 Decisions

**DD-1 — Three bands, explicit.**
- `Band = Literal["peak", "mid_peak", "off_peak"]`, ordered by name (`BANDS`), not by price.
- `Window` gains `band: Band = "off_peak"` as its last field, so every existing window keeps its
  meaning.
- `TariffSpec` gains `default_band: Band = "peak"`, and its `group` Literal adds `"G13"` and
  `"G13active"`.
- `_band_on_clock` order:
  1. a free day (if `spec.free_days`) gives `off_peak`;
  2. else the first active window that matches gives its `band`;
  3. else `spec.default_band`.
- For every existing key the result is identical (existing goldens stay green).

**DD-2 — `tariff_bands(tariff) -> tuple[Band, ...]`.** It returns the bands a group can produce,
in `BANDS` order: the default band, every window band, and `off_peak` when `free_days`.
- G11 → `("peak",)`
- G12 and G12w → `("peak", "off_peak")`
- G13 and G13active → all three

Runtime, Preview, flow and prefill all read the band set from here. No other code decides how
many zones a group has.

**DD-3 — Two catalog keys, appended.** `CATALOG` order is append-only, and keys are never
renamed (TS DD-3).
- `tauron_g13`:
  - `Window(7, 13, band="mid_peak")`;
  - `Window(19, 22, months=_SUMMER, band="peak")`;
  - `Window(16, 21, months=_WINTER, band="peak")`;
  - `default_band="off_peak"`, `free_days=True` (Saturdays, Sundays and holidays are all
    `off_peak`);
  - reference `_TAURON_REF`.
- `enea_g13active`:
  - one `Window` per month and range, copied from energy-compass `osd/enea.json` (end hour 24
    is allowed; `Window.length` already handles it):
    - restricted → `peak`;
    - remaining → `mid_peak`;
  - `default_band="off_peak"` (recommended);
  - `free_days=False` (the zones apply on every day, holidays included);
  - reference `_ENEA_REF`, `winter_clause=True`, no `choices`.

Labels (EN = PL): "Tauron Dystrybucja G13" and "Enea Operator G13active". The monthly table is
in appendix B and is cross-checked by DD-14.

**DD-4 — Zone → band map.** It is explicit, fixed in the generator, and recorded per group in the
snapshot:

| Group | peak | mid_peak | off_peak |
| --- | --- | --- | --- |
| G11 | `all` | — | — |
| G12 | `day` | — | `night` |
| G12w | `peak` | — | `offpeak` |
| G13 (Tauron) | `afternoon` | `morning` | `offpeak` |
| G13active (Enea) | `restricted` | `remaining` | `recommended` |

The generator fails when:
- a group's zone ids are not exactly the mapped ones;
- the mapped band set differs from `tariff_bands(catalog key)`;
- the variable distribution rate of `peak` is below that of `off_peak`.

The last check is a sanity guard against a swapped map. Ranking by price is rejected (§2): a
zone's identity is its published name and hours, while prices are re-set every year and by every
seller, so a price order could silently swap the labels Preview shows.

**DD-5 — Third-zone settings.** Both are added to `NUMBERS` next to their peers and to
`TARIFF_PRICE_KEYS`:
- `buy_mid_peak_rate`: `("tariffs", 0, -1000, 1000, "currency/kWh")`;
- `buy_distribution_mid_peak_rate`: `("tariffs", 0, 0, 1000, "currency/kWh")`.

`validate_configuration` reads `values.get(key, default)` (`settings.py:330`), so stored entries
need no migration.

Runtime per band (`runtime.py` schedule branch, `_buy_composition`):
- buy keys: peak → `buy_rate`, mid_peak → `buy_mid_peak_rate`, off_peak → `buy_off_peak_rate`;
- distribution keys: `buy_distribution_rate`, `buy_distribution_mid_peak_rate`,
  `buy_distribution_off_peak_rate`;
- `schedule_rates` gets exactly the `tariff_bands` of the tariff, so a band that cannot occur
  needs no value. `sources/prices.py:80` already reads `schedule_rates[row.band]`.

The existing error messages stay verbatim for one- and two-band groups (tests assert them). The
three-band messages are new:
- *Buy:* `tariff schedule needs positive buy rates: set Buy rate, Mid-peak buy rate and Off-peak
  buy rate in Tariffs → Values`.
- *Distribution:* `distribution with three zones needs all three rates or none: set Distribution
  rate, Mid-peak distribution rate and Off-peak distribution rate in Tariffs → Values`.

The all-or-none rule applies across the bands of the zone tariff (`_buy_zones`), so a G13 key
used as a distribution tariff (TS DD-6) also works with three zones.

**DD-6 — The snapshot is data, not code.**
- File: `custom_components/energy_compass/sources/tariff_rates.json`, format 1 (§1.4).
- Content:
  - raw components (verbatim camelCase field names);
  - the zone rules (for the cross-check);
  - the zone → band map;
  - a whitelisted subset of each `ure`/`fixed` offer.
- Nothing is precomputed.
- It is written as canonical JSON: `json.dumps(..., indent=2, ensure_ascii=False,
  sort_keys=False) + "\n"` with insertion order fixed by the generator. That way `--check` is a
  byte comparison and the diff of a yearly refresh is reviewable.
- The file has no commit field. The generator prints the energy-compass `git rev-parse HEAD`,
  and the maintainer puts it in the commit message.

**DD-7 — The generator and its CI check.** `tools/build_tariff_rates.py`:

| Invocation | Who | Does |
| --- | --- | --- |
| `--source <ec>/packages/data/src` | maintainer, yearly | reads the source files and writes the snapshot |
| `--check` | CI | validates the committed file only (no energy-compass checkout) |
| `--check --source <path>` | maintainer | regenerates in memory and fails on any byte difference |

`--check` (no source) validates the committed file:
1. The file is canonical (it re-serializes to the same bytes).
2. `validate_snapshot` passes (it lives in `sources/rates.py`, so the integration and the tool
   share one validator). It checks:
   - format 1;
   - VAT 0 < x < 1;
   - excise ≥ 0;
   - each operator has `tariffYear`, ISO dates and `validFrom ≤ validTo`;
   - every group's `catalog` key exists in `CATALOG` and has the snapshot's operator and group;
   - the band set equals `tariff_bands`;
   - every rate is finite and ≥ 0;
   - `subscriptionPerMonth["1"]` exists and keys are ⊆ {1, 2, 3, 6, 12};
   - capacity bands are ascending and end with `maxKWh: null`;
   - transitional band edges ⊆ capacity edges ∪ {null};
   - every offer has kind `ure` or `fixed`, a non-empty `osdScope` of known operators, and every
     priced group prices exactly that group's zones.

Writing (with `--source`) also refuses:
- a non-empty `priceCaps` in `programs.json` (the formula would no longer be exact; a future
  cap needs a design change, not a silent drop);
- an OSD with `zoneClock` other than `standard` (the cross-check assumes it).

Offers of kind `dynamic` are skipped by design. The CI step is a fifth line in
`.github/workflows/validate.yml` next to the four builders.

**DD-8 — What the prefill writes.** All values are net; VAT stays in `buy_apply_vat`. Here
`e[b]` is the seller energy price of the band's zone, `x` the excise and `d[b]` the distribution
total of the zone.

| Key | Value |
| --- | --- |
| `buy_rate`, `buy_mid_peak_rate`, `buy_off_peak_rate` | `e[b] + x` per band; `0` for a band outside `tariff_bands`; G11 writes `buy_off_peak_rate = 0` |
| `buy_distribution_rate`, `…_mid_peak_rate`, `…_off_peak_rate` | `d[b] = variablePerKWh[zone] + qualityPerKWh + ozePerKWh + cogenerationPerKWh`; `0` outside the band set |
| `buy_distribution_mode` | `added` |
| `buy_multiplier` / `buy_addition` | `1` / `0` |
| `buy_apply_vat` | `true` |
| `vat_percent` | `vatRate × 100` (23) when `sell_apply_vat` is off and `vat_percent` is not helper-bound; otherwise kept, with a Preview note when it differs |
| `monthly_charge` | `(fixedPerMonth[phase] + subscriptionPerMonth[months] + capacityPerMonth[band] + transitionalPerMonth[band] + tradeFeePerMonth) × (1 + vat_percent/100 if buy_apply_vat)`, rounded to 0.01 (gross, as on the invoice; it is reporting-only, `runtime.py:1411`) |

The excise belongs in the energy rate, not in distribution. The composition `energy = total −
distribution` (`runtime.py:476`) then gives the deposit energy + excise, as energy-compass
ADR-0031 bills it. The sell side and every other setting are never touched.

Proof of equality (per zone):
- HA buy = (`buy_rate` × 1 × (1 + v) + 0) + `d` × (1 + v) = (e + x + d) × (1 + v).
- energy-compass `costOf` bills the lines energy, excise and distribution, each × (1 + v).
- The two are equal. Appendix A lists the values the acceptance test hard-codes.

**DD-9 — Prefill provenance on the schedule.** `TariffSchedule` gains an optional
`prefill: Prefill | None`. `to_dict` emits it only when set, so every stored schedule without
it serializes byte-identically:

```text
Prefill
  operator          str        snapshot operator id (pge|tauron|enea|energa|stoen)
  offer             str|None   offer id; None = distribution only
  tariff_year       int        OSD tariffYear
  valid_from        str        ISO date, OSD validFrom
  valid_to          str        ISO date, OSD validTo
  as_of             str        ISO date, max(OSD asOf, offer asOf, excise asOf)
  phases            "one_phase" | "three_phase"
  billing_months    int        1|2|3|6|12
  consumption_max_kwh int|None capacity band edge; None = the top band
  values            {key: number}   the exact values written (DD-8 keys only)
```

Validation and storage:
- `validate_schedule` checks shape and types only, never snapshot membership: an offer that
  leaves the snapshot next year must not break a stored entry.
- `prefill` on a `distribution_schedule` is rejected (it is only ever written for the buy
  source).
- `from_dict` already ignores unknown keys, so an older build drops `prefill` silently.
- `values` lets Preview say "N values edited since" without reading the snapshot.

**DD-10 — G13active and any group without a priced offer: distribution only.**
- The offer step lists `distribution_only` (default when no offer exists) and `manual`.
- `distribution_only` writes the distribution rates, mode, VAT and monthly charge (without a
  trade fee), and sets the energy rates to `0`.
- With `0` energy rates the runtime fails closed with the positivity message of DD-5 (the
  three-band one for G13active) until the user types the seller's net energy price + 0.005
  excise per zone.
- The `tariff_values` description says exactly that, with the excise from the snapshot.
- It is also offered for groups that have offers: a household on an unlisted seller still gets
  the distribution side for free.

**DD-11 — Setup profiles revision 4.**
- `_REVISION_4 = ProfileRevision(axes=<revision 3 axes with buy_tariff + tauron_g13,
  enea_g13active>, raw_rce_presets=_REVISION_3.raw_rce_presets, raw_rce_sources=True,
  settled_sell_sources=True)`.
- `REVISIONS[4]`; `SETUP_PROFILES_REVISION` becomes 4. Revisions 1–3 stay frozen.
- Profiles still never prefill rates (TS rule; `test_profiles_never_touch_forbidden_keys`): the
  offer needs operator and charge answers a profile does not have.
- The provenance line changes to: `Buy source pre-selected by buy_tariff <label>: tariff
  schedule; pick the seller offer in Tariffs → Buy source to pre-fill the rates, or enter them
  in Tariffs → Values.`

**DD-12 — Key hygiene.** It extends TS DD-9. Rows marked † are new.

| Save path | `schedule` | `schedule.prefill` | rate keys (DD-8) | `helpers[key]` for a DD-8 key |
| --- | --- | --- | --- | --- |
| Schedule + offer or distribution only † | set | set (new) | written | popped (D3); popped keys listed in the `tariff_values` detail |
| Schedule + `manual` † | set | removed | kept | kept |
| Schedule, same tariff, re-saved with `manual` † | set | removed | kept | kept |
| Fixed / forecast / helper (from schedule) | removed (with prefill) | — | kept | TS DD-9 unchanged (`helpers["buy_rate"]` popped on fixed) |
| Setup profile (new entry) | default schedule, no prefill | — | untouched | untouched |
| Distribution tariff (TS DD-6) | unchanged | never set | untouched | untouched |
| Tariffs → Values edit | unchanged | kept (`values` shows the edits) | as typed | as bound |

Helper-bound key:
- Prefill pops the helper binding of every key it writes. A helper would otherwise silently
  override the value the user just chose.
- `vat_percent` is the exception (DD-8): it is skipped, never unbound.
- New-entry profile re-resolution (`_reresolve_profiles`) only touches profile-assigned keys and
  never touches a DD-8 key.

**DD-13 — Snapshot access is pure and loaded once.**
- `sources/rates.py` loads `tariff_rates.json` at module import. HA imports integrations in its
  executor, so there is no blocking I/O in the event loop.
- It exposes:
  - `SNAPSHOT`;
  - `validate_snapshot(data)`;
  - `operators()`;
  - `operator_for(tariff)`;
  - `offers_for(operator, group)`;
  - `charge_options(operator, group)`;
  - `compute_prefill(tariff, operator, offer, phases, billing_months, consumption_max_kwh, values) -> PrefillResult`;
  - `offer_label(offer, language)`;
  - `snapshot_text(operator, today, language)`.
- `offers_for` keeps offers with kind ∈ {ure, fixed}, the operator ∈ `osdScope`, and a price
  for every zone of the group.
- Order: `ure` first, then `fixed` by name.
- The label shows the seller and name, the kind (EN "default seller tariff (URE)" / PL "taryfa
  sprzedawcy z urzędu (URE)"; "fixed price N months" / "cena stała N mies."), the trade fee, and
  "sign by <availableUntil>" / "do podpisania do <availableUntil>".
- An offer past `availableUntil` stays listed with "(no longer offered)" / "(już niedostępna)".
  Existing contracts keep their price (D8).
- The runtime, coordinator and Preview of a running entry never call `compute_prefill`.

**DD-14 — The zone hours stay hand-written in `tariffs.py`, cross-checked by data.** For every
snapshot group, `tests/test_tariff_rates_snapshot.py` evaluates the snapshot rules with a
port of energy-compass `zoneOfHour` (standard clock, Polish holidays from `tariffs.pl_holidays`).
It maps the zone through the snapshot band map, and compares the result with
`band_at(TariffSchedule.default(key) with meter_winter_clock=True, t, "Europe/Warsaw")` for
every hour of the tariff year.
- A mismatch fails CI and means a real disagreement between the two projects. The dev escalates
  it; the test must not be weakened.
- Enea G12 uses the default params (night 22, afternoon 13), which is the energy-compass
  `zonesAssumption`.

**DD-15 — Stale data never changes values.** The offer step description shows the tariff year,
validity, `asOf` and, when today (HA local date) > `validTo`, a warning. Preview, for an entry
whose `prefill` exists:
- prints `Rates pre-filled from <seller> <offer> (<operator> tariff <year>, valid <from>–<to>, as of <as_of>)`,
  plus `; N values edited since` when `values` differ from settings;
- prints the Note below when today > `prefill.valid_to`;
- adds `newer rates are bundled (tariff <Y>): pick the offer again in Tariffs → Buy source` when
  the snapshot year of that operator is newer.

The Note reads: `Note: the pre-filled rates are out of date (the <year> tariff ended <valid_to>);
values are unchanged — pick the offer again or edit Tariffs → Values.`

Values stay as they are in every case (O-8). Preview is a pure function of the configuration and
the module-level snapshot, so the stale check runs in tests with a fixed `now`.

**DD-16 — No config-entry migration.**
- `VERSION` stays 3.
- New settings default to 0 through `values.get`.
- New schedule keys are optional, and new catalog keys only appear when chosen.
- Downgrade: an older build ignores the two new setting keys (it iterates its own `NUMBERS`;
  `values = dict(config["settings"])` keeps strays) and drops `prefill`. It reports
  `unknown tariff; reconfigure the buy source` for a G13 key (the existing `_UNKNOWN_TARIFF`),
  which fails closed. This is documented, not engineered around.

### 1.3 Data model (code)

```python
# sources/tariffs.py (additions)
Band = Literal["peak", "mid_peak", "off_peak"]
BANDS: tuple[Band, ...] = ("peak", "mid_peak", "off_peak")

@dataclass(frozen=True)
class Window:
    start: int
    end: int
    months: frozenset[int] | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    param: str | None = None
    band: Band = "off_peak"

@dataclass(frozen=True)
class TariffSpec:
    group: Literal["G11", "G12", "G12w", "G13", "G13active"]
    operator: str | None
    windows: tuple[Window, ...]
    free_days: bool
    reference: str
    labels: Mapping[str, str]
    choices: Mapping[str, tuple[int, ...]] = MappingProxyType({})
    winter_clause: bool = True
    default_band: Band = "peak"

@dataclass(frozen=True)
class Prefill:
    operator: str
    offer: str | None
    tariff_year: int
    valid_from: str
    valid_to: str
    as_of: str
    phases: Literal["one_phase", "three_phase"]
    billing_months: int
    consumption_max_kwh: int | None
    values: Mapping[str, float]

@dataclass(frozen=True)
class TariffSchedule:
    tariff: str
    meter_winter_clock: bool = False
    params: Mapping[str, int] = field(default_factory=dict)
    prefill: Prefill | None = None       # emitted by to_dict only when set

BAND_RATE_KEYS = {"peak": "buy_rate", "mid_peak": "buy_mid_peak_rate",
                  "off_peak": "buy_off_peak_rate"}
BAND_DISTRIBUTION_KEYS = {"peak": "buy_distribution_rate",
                          "mid_peak": "buy_distribution_mid_peak_rate",
                          "off_peak": "buy_distribution_off_peak_rate"}
def tariff_bands(tariff: str) -> tuple[Band, ...]: ...
def local_band(schedule, day, timezone_name, band) -> str: ...   # "none" | "all day" | "HH:MM–HH:MM, …"
# local_off_peak(schedule, day, tz) == local_band(schedule, day, tz, "off_peak") (kept)

# sources/rates.py (new)
@dataclass(frozen=True)
class PrefillResult:
    values: Mapping[str, float | bool | str]   # DD-8 keys, ready for settings
    prefill: Prefill
    unbound: tuple[str, ...]                   # helper keys popped
    notes: tuple[str, ...]                     # e.g. VAT kept at 8 %
```

`BAND_RATE_KEYS` and `BAND_DISTRIBUTION_KEYS` live in `tariffs.py`, so the runtime, flow and
prefill share one map. The two new keys are appended to `setup_profiles.TARIFF_PRICE_KEYS` by hand,
since the profile data stays literal.

### 1.4 Snapshot format (format 1)

The excerpt shows one operator, one group and one offer. Field names inside `charges`, `zones`
and offers are verbatim from energy-compass.

```json
{
  "format": 1,
  "vatRate": 0.23,
  "excise": {"netPerKWh": 0.005, "asOf": "2026-10-06", "source": "Ustawa … o podatku akcyzowym, art. 89 ust. 3"},
  "operators": {
    "tauron": {
      "name": "Tauron Dystrybucja", "tariffYear": 2026,
      "validFrom": "2026-01-01", "validTo": "2026-12-31", "asOf": "2026-10-06",
      "source": "Taryfa Tauron Dystrybucja na 2026 r. — decyzja Prezesa URE DRE.WRE.4211.54.8.2025.ASa1 z 17.12.2025 …",
      "zoneClock": "standard",
      "groups": {
        "G13": {
          "catalog": "tauron_g13",
          "bands": {"peak": "afternoon", "mid_peak": "morning", "off_peak": "offpeak"},
          "zones": [ … verbatim zones with rules … ],
          "charges": {
            "variablePerKWh": {"morning": 0.2203, "afternoon": 0.3898, "offpeak": 0.0392},
            "qualityPerKWh": 0.0332, "ozePerKWh": 0.0073, "cogenerationPerKWh": 0.003,
            "fixedPerMonth": {"onePhase": 7.38, "threePhase": 10.86},
            "transitionalPerMonth": [{"maxKWh": null, "pln": 0}],
            "subscriptionPerMonth": {"1": 4.56, "2": 2.28, "6": 0.76, "12": 0.38},
            "capacityPerMonth": [{"maxKWh": 500, "pln": 4.29}, … , {"maxKWh": null, "pln": 24.05}]
          }
        }
      }
    }
  },
  "offers": {
    "tauron-ure-2026": {
      "seller": "…", "name": "…", "kind": "ure", "osdScope": ["tauron"],
      "groups": {"G13": {"morning": 0.4668, "afternoon": 0.778, "offpeak": 0.421}, "…": {}},
      "tradeFeePerMonth": 0, "termMonths": null, "availableUntil": null,
      "asOf": "2026-10-06", "source": "…"
    }
  }
}
```

Generator details:
- Rules for `catalog`:
  - G11 → `g11` for every operator;
  - otherwise `<operator>_<group lower>`, i.e. `pge_g12`, `tauron_g13`, `enea_g13active`, …
- Offer `groups` flattens `groups[G].energyPricePerKWh` to `groups[G]`. `seller` is
  `seller.name`, and `termMonths` comes from `contract.termMonths`.
- Fields not used here (audience, oneOffFees, checklist, area, order, meterExchangeNote,
  zonesAssumption) are dropped. The Enea G12 assumption is already TS's `enea_text`.

### 1.5 Flow

The flow below is reached from Tariffs → Buy source → Polish tariff schedule, in new entry,
Options and Reconfigure alike:

```text
tariff_schedule (group + winter clock)            existing step
  └─ [tariff_schedule_hours]  Enea G12/G12w params existing step
      └─ tariff_operator      only for g11: pge|tauron|enea|energa|stoen   NEW
          └─ tariff_offer     offers_for(op, group) + distribution_only + manual   NEW
              ├─ manual  → _save_schedule_source(schedule)              (today's behaviour)
              └─ offer | distribution_only
                  └─ tariff_charges  phases · billing period · consumption band   NEW
                      └─ _save_schedule_source(schedule, prefill_result)
                          └─ tariff_values form, {detail} = prefill summary + unbound helpers
```

Step details:
- **tariff_operator:**
  - a select of the five operators by snapshot `name`;
  - default: `prefill.operator`, else the first in snapshot order.
- **tariff_offer:**
  - a select;
  - description placeholders: `{tariff}`, `{year}`, `{valid}` ("2026-01-01 – 2026-12-31"),
    `{as_of}`, and `{note}` (stale warning or "no seller price for this group" or empty), built
    by `rates.snapshot_text(..., hass.config.language)`;
  - default:
    1. `prefill.offer` if still listed; else
    2. `manual` when the stored schedule has rates but no `prefill` (an existing user is never
       surprised); else
    3. the `ure` offer; else
    4. `distribution_only`.
- **tariff_charges:**
  - phases (`one_phase` / `three_phase`; shown even when both cost the same);
  - billing period (only keys present in that group's `subscriptionPerMonth`, labelled
    "monthly / every 2 months / …" — PL "co miesiąc / co 2 miesiące / …");
  - consumption band (labels from the operator's capacity edges: "up to 500 kWh/year", …, "above
    2800 kWh/year"; Enea and Energa show 499);
  - defaults: from `prefill`, else one phase, `1`, the ≤2800 band (D5).
- **Save:**
  - `_save_schedule_source` gets an optional `PrefillResult`. It first does everything it does
    today (TS DD-9), then applies the DD-8 values, sets `schedule.prefill` and pops the helpers
    (DD-12).
  - Validation (`assert_selected`) runs on the result, so a prefill that leaves the problem
    unsolvable is reported like any other save error.
  - The flow then shows `tariff_values` with the values in place, so "editable afterwards" is
    the very next screen.

Strings for all three steps: `strings.json` = `en.json`, `pl.json`, config and options copies.

### 1.6 Preview and the source list

- `_schedule_preview` prints one line per band of `tariff_bands`:
  - "peak today …", "mid-peak today …", "off-peak today …" (via `local_band`);
  - the rate line becomes "peak X, mid-peak Y, off-peak Z {currency}/kWh before multiplier";
  - holidays are shown when `spec.free_days` (today that means G12w) instead of a hard-coded
    G12w test.
- `_distribution_preview` gets the same generalisation.
- The DD-15 provenance line, edit count and stale notes come after the rate line.
- When the prefill kept `vat_percent` (DD-8), a note follows:
  `Note: VAT is 8 % (shared with the sell side); the offer assumes 23 %.`
- `source_management._distribution_label` and the schedule row add the label "mid-peak" (EN) /
  "strefa pośrednia" (PL), and the source-mode label becomes "Polish tariff schedule
  (G11/G12/G12w/G13)" (PL: "Taryfa OSD (G11/G12/G12w/G13)").

### 1.7 Translations of the new settings

| Key | EN | PL |
| --- | --- | --- |
| `buy_mid_peak_rate` | Mid-peak buy rate (G13 tariffs) | Cena zakupu w strefie pośredniej (taryfa OSD G13) |
| `buy_distribution_mid_peak_rate` | Mid-peak distribution rate | Stawka dystrybucji w strefie pośredniej |

Existing labels are kept. The PL `buy_off_peak_rate` label "(taryfa OSD G12/G12w)" becomes
"(taryfa OSD G12/G12w/G13)".

---

## 2. Rejected alternatives

| Alternative | Why not |
| --- | --- |
| Keep "rates are never shipped" (TS §3.1) | Reversed by the owner (O-1). |
| Operator-specific G11 catalog keys (`pge_g11`, …) | G11 has no zones to differ. Five new keys would mean a profiles revision and Preview text for no schedule change. An operator step only for `g11` is enough. |
| Precomputed per-zone prices in the snapshot | Loses traceability to the source fields, and the charges answers (phase, period, band) multiply the variants. Raw components keep the yearly diff readable. |
| Snapshot as a generated Python module | Ruff format fights the literals, a 600-line diff is unreadable, and data should be data. |
| Online fetch from energycompass.pl | Owner non-goal. It adds a network dependency in a flow step. |
| Bake VAT into the rates | Doubles VAT whenever `buy_apply_vat` is on, and hides the net price the invoice shows. |
| `included` mode with full rates in `buy_rate` | Loses the energy/distribution split the deposit model needs (ADR-0001), so the deposit would pay distribution. |
| Infer G13 bands by price ranking | Not stable: prices change yearly and per seller, while the zone identity (name, hours) does not. A swap would mislabel Preview. An explicit map plus a sanity check is safer. |
| Hide G13active because it has no URE price | O-7 requires the group. The distribution side is still worth pre-filling (3 rates + monthly charge). |
| Prefill provenance as a top-level `price` key | The provenance describes the schedule it was computed for. On the schedule it disappears with it under the existing hygiene (TS DD-9) without new code. |
| Config-entry `VERSION` bump with a migration | Nothing to migrate: every addition is optional with a safe default. A bump only buys a harder downgrade. |
| Generate `tariffs.py` windows from the snapshot | Couples the runtime zone model to energy-compass's schema and yearly cadence. The hand catalog plus the hour-by-hour cross-check (DD-14) gives the same guarantee with no runtime dependency. |
| Prefill through setup profiles | A profile has no operator, offer or charge answers. Profiles stay rate-free (TS rule and test). |
| Keep helper bindings on prefilled keys | The helper silently overrides the value just chosen, and the Preview would contradict the flow. |
| Store `monthly_charge` net | The invoice shows the gross fee. The field is reporting-only, and users compare it with the invoice. |

---

## 3. Decisions

### 3.1 Human-confirmed (owner, 2026-10-20)

- O-1 **Reversal of TS §3.1.** Choosing a tariff and an offer pre-fills the rate settings. They
  stay editable. The `tariffs.py` module docstring ("Rates are never shipped; …") is rewritten
  in Task 4.
- O-2 **Source of rates.** Rates come from a bundled snapshot generated from energy-compass
  `packages/data/src`, refreshed yearly. No online fetch. The generator reads a path, and CI
  `--check` validates only the committed file.
- O-3 **Formula.** Buy per zone = seller energy + distribution (variable + quality + OZE +
  cogeneration) + excise, with VAT as configured. It equals the energy-compass calculator within
  0.001 zł/kWh.
- O-4 **Offers.** Only kinds `ure` and `fixed`.
- O-5 **Sell side.** Unchanged.
- O-6 **monthly_charge.** Computed from phases, billing period and consumption band: fixed +
  subscription + capacity (+ transitional) + trade fee.
- O-7 **Groups.**
  - G11, G12 and G12w for PGE, Tauron, Enea, Energa and Stoen;
  - Tauron G13 and Enea G13active with 3 zones;
  - G13active has no URE price, which DD-10 handles.
- O-8 **Stale data.** The flow shows the year and asOf. After `validTo`, Preview warns, and
  values stay unchanged.

### 3.2 Open decisions (defaults applied in this spec)

| Id | Question | Default | Cost to flip |
| --- | --- | --- | --- |
| D1 | `monthly_charge` gross or net? | Gross (× VAT factor of the buy side), as on the invoice | One line in `compute_prefill` + goldens |
| D2 | `vat_percent` when the sell side applies VAT at another rate | Keep it and show a Preview note; write 23 only when `sell_apply_vat` is off and the key is not helper-bound | A rule in `compute_prefill` + 2 tests |
| D3 | Helper bound to a prefilled key | Pop the binding and list it in the detail | Skip the key instead; hygiene table + 1 test |
| D4 | G13active (no seller price) | Distribution only, energy rates 0, fails closed until typed | Hide the key; catalog + profiles |
| D5 | Charge defaults | One phase, monthly billing, ≤2800 kWh/year band | Constants in `rates.py` |
| D6 | Name of the third band | `mid_peak` (EN "mid-peak", PL "strefa pośrednia") | Rename before Task 1 only; afterwards it is persisted nowhere but in code and docs |
| D7 | `buy_multiplier` / `buy_addition` on prefill | Reset to 1 / 0 (otherwise the result ≠ calculator) | Keep them and add a Preview note |
| D8 | Offers past `availableUntil` | Still listed, marked "no longer offered" | Filter in `offers_for` |
| D9 | Excise when the user picks `distribution_only` | Not written; the description says "add 0.005 excise to the seller's net price" | Write excise-only energy rates (but then they are positive and do not fail closed) |

---

## 4. Implementation plan

> **For agentic workers:** one task = one session = one commit on the task branch. TDD: write the
> failing tests named in the task, run them red, implement, run them green, then the full gate.
> Do not edit generated files by hand (AGENTS.md). Never weaken an existing test; the assertions
> this plan changes are listed per task.

**Goal.** §0. **Architecture.**
- `tariffs.py` (zones, 3 bands) and `settings.py` (keys);
- `runtime.py` (bands → rates);
- `rates.py` + `tariff_rates.json` (snapshot, prefill);
- `source_flow.py` (steps, save) and `config_flow.py` (Preview);
- `setup_profiles.py` (rev 4);
- `tools/build_tariff_rates.py` (generator).

**Tech stack.** Python 3.13 and the HA test image (pytest, ruff). No new dependency: the
snapshot is plain JSON, and `jsonschema` is not needed.

Every command runs through `shq test '<command>'`. The full gate:

```bash
docker build --file Dockerfile.test --tag ha-energy-compass-test:2026.9.1 .
docker run --rm -v "$PWD:/workspace:ro" ha-energy-compass-test:2026.9.1 -m pytest -p no:cacheprovider -q
docker run --rm -v "$PWD:/workspace:ro" ha-energy-compass-test:2026.9.1 -m ruff check --no-cache .
docker run --rm -v "$PWD:/workspace:ro" ha-energy-compass-test:2026.9.1 -m ruff format --check --no-cache .
docker run --rm -v "$PWD:/workspace:ro" ha-energy-compass-test:2026.9.1 tools/deye_controller/build.py --check
docker run --rm -v "$PWD:/workspace:ro" ha-energy-compass-test:2026.9.1 tools/dashboards/build.py --check
docker run --rm -v "$PWD:/workspace:ro" ha-energy-compass-test:2026.9.1 tools/build_builder.py --check
docker run --rm -v "$PWD:/workspace:ro" ha-energy-compass-test:2026.9.1 tools/build_guides.py --check
# from Task 4 on:
docker run --rm -v "$PWD:/workspace:ro" ha-energy-compass-test:2026.9.1 tools/build_tariff_rates.py --check
```

Narrow runs use the same image with `-m pytest -p no:cacheprovider -q tests/<file>.py`.

**Step 0 (every task).** Read the task log, `LESSONS.md` and this spec's DD rows for the task.
Build the image once.

**Order and dependencies.**

```text
T1 ─┬─ T2 ─────────────┐
    ├─ T3 ─────────────┤
    └─ T4 ── T5 ── T6 ─┴─ T7 ── T8 ── T9
```

### Task 1 — Three-band zone model, catalog keys, settings, runtime

**Implements:** DD-1, DD-2, DD-3, DD-5; appendix B. **Deps:** none.

**Files:**
- `custom_components/energy_compass/sources/tariffs.py`: `Band`, `BANDS`, `Window.band`,
  `TariffSpec.default_band`, group Literal, `tariff_bands`, `local_band`, `BAND_RATE_KEYS`,
  `BAND_DISTRIBUTION_KEYS`, and the `tauron_g13` / `enea_g13active` entries appended to
  `CATALOG`.
- `custom_components/energy_compass/settings.py`: two `NUMBERS` rows.
- `custom_components/energy_compass/setup_profiles.py`: append both keys to `TARIFF_PRICE_KEYS`
  only (no revision here).
- `custom_components/energy_compass/runtime.py`: schedule branch and `_buy_composition` by
  `tariff_bands`.
- `strings.json`, `translations/en.json`, `translations/pl.json`: the two labels (§1.7) in
  config and options `tariff_values` (`test_tariff_flow.py:435` checks every file).
- Tests: `tests/test_tariffs.py`, `tests/test_tariff_runtime.py`.

**Steps:**
- [ ] Step 1: Write the failing tests:
  - in `tests/test_tariffs.py`:
    - `test_catalog_keys_and_order` (updated: 13 keys, two new ones last);
    - `test_tariff_bands_per_group`;
    - `test_existing_keys_unchanged_by_band_field` (every pre-existing key: `band_rows` over
      2026 equals the frozen golden);
    - `test_tauron_g13_weekday_summer` (2026-07-15 Wed local: 07:00 mid_peak, 13:00 off_peak,
      19:00 peak, 21:45 peak, 22:00 off_peak, 16:00 off_peak);
    - `test_tauron_g13_weekday_winter` (2026-01-14 Wed: 16:00 peak, 20:45 peak, 21:00 off_peak,
      19:00 peak);
    - `test_tauron_g13_saturday_and_holiday_off_peak` (2026-07-18 Sat and 2026-05-01 Fri
      holiday: every quarter off_peak);
    - `test_tauron_g13_winter_clock` (meter_winter_clock: summer peak 19–22 CET = 20–23 local);
    - `test_enea_g13active_months` (2026-01-12, 2026-03-10, 2026-06-10, 2026-11-10 at every hour
      vs appendix B with `meter_winter_clock=True`, plus 2026-06-10 on the local clock, where every
      boundary is one hour later);
    - `test_enea_g13active_holiday_and_saturday_follow_zones` (2026-05-01 and 2026-06-13 equal a
      weekday of the same month);
    - `test_enea_g13active_dst_days` (2026-03-29 and 2026-10-25: 92/100 quarters, bands by local
      hour);
    - `test_local_band_text`;
  - in `tests/test_tariff_runtime.py`:
    - `test_g13_three_band_transform`;
    - `test_g13_missing_mid_peak_rate_fails_closed` (exact new message);
    - `test_g13_distribution_all_or_none` (exact new message);
    - `test_g12_messages_unchanged`.
- [ ] Step 2: Run; expect FAIL: `shq test 'docker run --rm -v "$PWD:/workspace:ro" ha-energy-compass-test:2026.9.1 -m pytest -p no:cacheprovider -q tests/test_tariffs.py tests/test_tariff_runtime.py'`.
- [ ] Step 3: Implement. Existing `local_off_peak` delegates to `local_band`.
- [ ] Step 4: Narrow run PASS, then the full gate.
- [ ] Step 5: Commit `feat(T-xxx): three-band tariff zones with Tauron G13 and Enea G13active`.

### Task 2 — Setup profiles revision 4

**Implements:** DD-11 (axis only; the provenance wording is in Task 7). **Deps:** T1.

**Files:** `custom_components/energy_compass/setup_profiles.py`, `tests/test_setup_profiles.py`.

**Steps:**
- [ ] Step 1: Write the failing tests:
  - `test_revision_4_adds_g13_buy_tariffs`: the axis equals revision 3's plus the two keys,
    with labels;
  - `test_revision_3_is_frozen` is kept as it is;
  - new `EXPECTED_REVISION_4_BUY_TARIFF` + `test_revision_4_is_frozen`;
  - `test_buy_tariff_profiles_assign_nothing_and_name_a_catalog_key` (unchanged, now
    covers 13);
  - update the two `SETUP_PROFILES_REVISION == 3` assertions (`:159`, `:1418`) to `== 4`. This is
    an intended change, not a weakening.
- [ ] Step 2: Run; expect FAIL: `… -m pytest -p no:cacheprovider -q tests/test_setup_profiles.py`.
- [ ] Step 3: Implement `_REVISION_4` and `REVISIONS[4]`.
- [ ] Step 4: Narrow run PASS, then the full gate.
- [ ] Step 5: Commit `feat(T-xxx): setup profiles revision 4 with G13 buy tariffs`.

### Task 3 — Preview and source list for three bands

**Implements:** §1.6 without the prefill lines. **Deps:** T1.

**Files:** `config_flow.py` (`_schedule_preview`, `_distribution_preview`),
`source_management.py` (labels EN/PL, distribution label, mode label), `tests/test_tariff_flow.py`,
`tests/test_sources.py`.

**Steps:**
- [ ] Step 1: Write the failing tests:
  - `test_preview_tauron_g13_three_lines` (fixed `now` on a summer Wednesday: exact lines for
    peak/mid-peak/off-peak today and the three-rate line);
  - `test_preview_enea_g13active_saturday`;
  - `test_distribution_preview_three_zones`;
  - `test_source_list_g13_labels_en_pl`.
  
  Keep `test_preview_pge_g12_local_clock` and `test_preview_g11_is_a_single_rate_line` green and
  unchanged.
- [ ] Step 2: Run; expect FAIL (`tests/test_tariff_flow.py tests/test_sources.py`).
- [ ] Step 3: Implement.
- [ ] Step 4: Narrow run PASS, then the full gate.
- [ ] Step 5: Commit `feat(T-xxx): preview and source list show three tariff zones`.

### Task 4 — Snapshot, generator, validator, CI step

**Implements:** DD-4, DD-6, DD-7, DD-13 (loading + `validate_snapshot` only), DD-14, O-1
docstring. **Deps:** T1.

**Files:**
- `tools/build_tariff_rates.py` (new);
- `custom_components/energy_compass/sources/rates.py` (new: load, `validate_snapshot`,
  `operators`, `operator_for`);
- `custom_components/energy_compass/sources/tariff_rates.json` (generated);
- `custom_components/energy_compass/sources/tariffs.py` (module docstring: "Zone schedules of
  Polish tariff groups. Rates are bundled separately in `tariff_rates.json` and only pre-fill
  editable settings (2026-10-20 reversal, T-100 spec).");
- `.github/workflows/validate.yml` (5th builder line);
- `AGENTS.md` (generated-files row: `custom_components/energy_compass/sources/tariff_rates.json` |
  `tools/build_tariff_rates.py` reading energy-compass `packages/data/src` | `python
  tools/build_tariff_rates.py --source <energy-compass>/packages/data/src`; plus one sentence
  that CI `--check` validates the committed file only);
- tests `tests/test_tariff_rates_snapshot.py`, `tests/test_build_tariff_rates.py` (new).

**Steps:**
- [ ] Step 1: Write the failing tests:
  - in `test_build_tariff_rates.py`, against a tiny fixture tree under
    `tests/fixtures/tariff_rates_src/` (two OSDs, one ure, one fixed, one dynamic offer,
    programs.json):
    - `test_writes_canonical_snapshot`;
    - `test_check_passes_on_committed_file`;
    - `test_check_fails_on_non_canonical_bytes`;
    - `test_refuses_price_caps`;
    - `test_refuses_unmapped_zone`;
    - `test_refuses_swapped_band_map`;
    - `test_skips_dynamic_offers`;
    - `test_refuses_transitional_edge_not_in_capacity`;
    - `test_check_with_source_detects_drift`;
  - in `test_tariff_rates_snapshot.py`:
    - `test_committed_snapshot_is_valid`;
    - `test_snapshot_covers_catalog` (every catalog key other than `g11` maps to exactly one
      operator group; `g11` maps to the G11 group of all five);
    - `test_snapshot_zones_match_catalog` (DD-14, every hour of each operator's tariff year);
    - `test_snapshot_vat_and_excise` (0.23, 0.005).
- [ ] Step 2: Run; expect FAIL: `… -m pytest -p no:cacheprovider -q tests/test_build_tariff_rates.py tests/test_tariff_rates_snapshot.py`.
- [ ] Step 3: Implement. Generate the real snapshot from the local energy-compass checkout:

  ```bash
  docker run --rm -v "$PWD:/workspace" -v "<energy-compass>/packages/data/src:/ec:ro" \
    ha-energy-compass-test:2026.9.1 tools/build_tariff_rates.py --source /ec
  ```

  Put the printed energy-compass commit in the commit body. If `test_snapshot_zones_match_catalog`
  fails, stop and escalate: one of the two projects has wrong hours.
- [ ] Step 4: Narrow run PASS, `tools/build_tariff_rates.py --check` exit 0, then the full gate.
- [ ] Step 5: Commit `feat(T-xxx): bundled tariff rate snapshot and generator`.

### Task 5 — Prefill computation

**Implements:** DD-8, DD-10, DD-13 (offers, labels, `compute_prefill`), D1–D9 defaults. **Deps:**
T4.

**Files:** `sources/rates.py`, tests `tests/test_tariff_rates_prefill.py` (new).

**Steps:**
- [ ] Step 1: Write the failing tests:
  - `test_golden_prices` is parametrised over every row of appendix A. It hard-codes, per row:
    - the operator, catalog key and offer;
    - the expected net `buy_*` and `buy_distribution_*` values;
    - the gross per-zone price `(rate + distribution) × 1.23`, asserted with
      `abs(...) < 0.001` *and* equal to the Decimal value to 1e-9;
    - the gross `monthly_charge`, one phase, monthly billing, ≤2800 kWh band.
    
    The expected numbers are literals copied from appendix A, never computed from the snapshot
    inside the test.
  - `test_monthly_charge_variants` covers three cases, with literals hand-computed from the
    source JSON:
    - Tauron G13 three-phase, 12-month billing, above 2800: (10.86 + 0.38 + 24.05 + 0 + 0) × 1.23
      = 43.41;
    - Stoen G11 e.on, 6-month billing, ≤500 kWh;
    - the Enea ≤499 band edge;
  - `test_offers_for_scope_and_order` (Stoen: e.on URE first, then both Energa fixed; PGE: pge-ure,
    then fixed by name; no dynamic offer anywhere; Tauron G13 → only tauron-ure);
  - `test_enea_g13active_distribution_only` (energy rates 0, distribution 0.3467/0.2891/0.1165,
    monthly charge without a trade fee);
  - `test_vat_rule` (sell off → 23 written; sell on with 8 → kept + note; helper-bound VAT → kept);
  - `test_resets_multiplier_and_addition`;
  - `test_g11_off_peak_zeroed`;
  - `test_offer_labels_en_pl`;
  - `test_expired_offer_marked` (`today=2027-01-02`);
  - `test_snapshot_text_stale_en_pl`;
  - `test_billing_choices_per_operator` (PGE 1/2/6; Tauron 1/2/6/12; Energa 1/2; Stoen 1/6/12;
    Enea 1/2/6/12).
- [ ] Step 2: Run; expect FAIL: `… -m pytest -p no:cacheprovider -q tests/test_tariff_rates_prefill.py`.
- [ ] Step 3: Implement with `Decimal` from the JSON text (`parse_float=Decimal`). Convert to
  `float` only in `PrefillResult.values`, rounded to 6 decimals for rates and 2 for
  `monthly_charge`.
- [ ] Step 4: Narrow run PASS, then the full gate.
- [ ] Step 5: Commit `feat(T-xxx): compute tariff rate prefill from the bundled snapshot`.

### Task 6 — Persisted provenance and the prefill save path

**Implements:** DD-9, DD-12, DD-16. **Deps:** T5.

**Files:** `sources/tariffs.py` (`Prefill`, `TariffSchedule.prefill`, `to_dict`/`from_dict`,
`validate_schedule`), `config_models.py` (`validate_sources`: prefill rejected on
`distribution_schedule`), `source_flow.py` (`_save_schedule_source(schedule, result=None)`),
tests `tests/test_tariffs.py`, `tests/test_tariff_flow.py`, `tests/test_sources.py`.

**Steps:**
- [ ] Step 1: Write the failing tests:
  - `test_schedule_without_prefill_round_trips_byte_identical` (a stored legacy dict →
    `from_dict` → `to_dict` is equal);
  - `test_prefill_round_trip`;
  - `test_validate_prefill_shape` (bad phases, months 5, negative kWh, a bool as int, missing
    field);
  - `test_prefill_on_distribution_schedule_rejected`;
  - `test_prefill_save_writes_values_and_provenance`;
  - `test_prefill_save_pops_helpers_of_written_keys`;
  - `test_manual_save_drops_prefill_keeps_rates`;
  - `test_fixed_after_prefill_drops_schedule_and_prefill`;
  - `test_older_reader_ignores_prefill` (`from_dict` of a dict with prefill and an unknown
    future key);
  
  Keep `test_schedule_save_follows_the_key_hygiene_row` and
  `test_helper_bound_fixed_to_schedule_drops_only_the_rate_helper` green and unchanged.
- [ ] Step 2: Run; expect FAIL (`tests/test_tariffs.py tests/test_tariff_flow.py tests/test_sources.py`).
- [ ] Step 3: Implement.
- [ ] Step 4: Narrow run PASS, then the full gate.
- [ ] Step 5: Commit `feat(T-xxx): store tariff prefill provenance and apply it on save`.

### Task 7 — Flow steps operator, offer, charges

**Implements:** §1.5, DD-10, DD-11 provenance line, DD-15 (offer-step note). **Deps:** T6, T2, T3.

**Files:** `source_flow.py` (`async_step_tariff_operator`, `async_step_tariff_offer`,
`async_step_tariff_charges`; routing from `tariff_schedule` / `_hours`; `tariff_values` detail),
`setup_profiles.py` (provenance text), `strings.json`, `translations/en.json`,
`translations/pl.json` (config + options), tests `tests/test_tariff_flow.py`,
`tests/test_setup_profiles.py`.

**Steps:**
- [ ] Step 1: Write the failing tests, each through the real flow manager (new entry, Options,
  Reconfigure):
  - `test_flow_pge_g12_ure_prefills_and_shows_values` (asserts the settings equal appendix A
    and the next step is `tariff_values` with the detail);
  - `test_flow_g11_asks_operator`;
  - `test_flow_tauron_g13_three_rates`;
  - `test_flow_enea_g13active_distribution_only_fails_closed_until_typed`;
  - `test_flow_manual_keeps_today_behaviour`;
  - `test_flow_defaults_from_stored_prefill`;
  - `test_flow_existing_schedule_without_prefill_defaults_to_manual`;
  - `test_flow_enea_g12_hours_then_offer`;
  - `test_offer_step_shows_year_and_stale_note` (fixed `now` 2027-01-05);
  - `test_billing_options_follow_operator`;
  - `test_new_strings_in_every_translation_file` (extends `:435`);
  - `test_profile_provenance_points_to_offer` (exact new line).
- [ ] Step 2: Run; expect FAIL (`tests/test_tariff_flow.py tests/test_setup_profiles.py`).
- [ ] Step 3: Implement. Option labels come from `rates.offer_label(..., hass.config.language)`.
- [ ] Step 4: Narrow run PASS, then the full gate.
- [ ] Step 5: Commit `feat(T-xxx): pick operator, offer and charges to pre-fill tariff rates`.

### Task 8 — Preview provenance, edits and stale notes

**Implements:** DD-15, the D2 VAT note. **Deps:** T7.

**Files:** `config_flow.py` (`_schedule_preview` / `_price_preview`), `tests/test_tariff_flow.py`.

**Steps:**
- [ ] Step 1: Write the failing tests:
  - `test_preview_prefill_line`;
  - `test_preview_counts_edited_values`;
  - `test_preview_stale_after_valid_to` (`now` 2027-01-01T00:30 local → Note; values unchanged);
  - `test_preview_newer_snapshot_note` (monkeypatched snapshot year 2027);
  - `test_preview_vat_kept_note`;
  - `test_preview_without_prefill_unchanged` (golden of the existing Preview).
- [ ] Step 2: Run; expect FAIL (`tests/test_tariff_flow.py`).
- [ ] Step 3: Implement.
- [ ] Step 4: Narrow run PASS, then the full gate.
- [ ] Step 5: Commit `feat(T-xxx): preview shows tariff prefill provenance and stale rates`.

### Task 9 — EN+PL doc sweep

**Implements:** AGENTS.md doc sweep for every change of T1–T8. **Deps:** T8.

**Files:**
- `docs/guide.en.md` ↔ `docs/guide.pl.md`:
  - § Built-in Polish tariffs / Wbudowane taryfy OSD: catalog table + G13 rows, a new
    subsection "Pre-filled rates" (flow, formula, monthly charge, stale data, distribution only,
    downgrade note);
  - § Setup profiles (revision 4, provenance line);
  - the deposit/settings table rows for `buy_*rate` and `buy_distribution_*` (three zones, new
    keys);
  - any Mermaid diagram of the tariff flow;
- `docs/model.md` (tariff schedule § bands, prefill = settings, runtime unchanged);
- `docs/index.html` (:170, :182, :184, :309, :410: G13 and pre-filled rates);
- `README.md` ↔ `README.pl.md` (tariff paragraphs :22, :61 / :22, :52);
- `docs/installation.md` (buy source step);
- `docs/source-requirements.md` (:12);
- `docs/tariff-helper.md` (when a helper is still useful, and that prefill unbinds it);
- regenerated `docs/guide.*.html`, `docs/assets/diagrams/*.svg`, `docs/builder.html` (if
  `index.html` changed).

**Steps:**
- [ ] Step 1: Run `grep -rn 'never shipped\|G11/G12/G12w\|buy_off_peak_rate\|buy_distribution_off_peak_rate\|monthly_charge\|revision 3' README*.md docs/*.md docs/index.html`
  and fix every hit. Polish labels are taken from `translations/pl.json`.
- [ ] Step 2: `python tools/build_guides.py` (Node for Mermaid), `python tools/build_builder.py`.
- [ ] Step 3: The full gate, plus `… -m pytest -p no:cacheprovider -q tests/test_deye_controller_docs.py tests/test_build_guides_check.py`.
- [ ] Step 4: Check the links (relative + anchors) in `README*.md` and `docs/guide.*.md`.
- [ ] Step 5: Commit `docs(T-xxx): pre-filled Polish tariff rates and G13 zones in EN and PL docs`.

---

## 5. Risks

| Id | Risk | Mitigation |
| --- | --- | --- |
| R1 | Yearly rates go stale (new URE decision every December, offers end 2026-12-31) | The DD-15 note after `validTo`, and the "newer rates bundled" note. Values never change silently. The refresh is one generator run + review of a JSON diff, owned by the release maintainer. |
| R2 | The third band ripples through runtime, flow, profiles and Preview | One source of truth (`tariff_bands`, `BAND_*_KEYS`). T1 lands the core with goldens of every existing key unchanged before any UI work. Two-band messages and tests stay verbatim. |
| R3 | Cross-repo drift (energy-compass changes schema or hours) | The generator validates schema and mapping when writing. CI `--check` validates the committed file without the other repo. DD-14 compares hours hour by hour. `--check --source` detects drift locally. |
| R4 | A wrong G13 zone → band map | Explicit map, the generator's peak ≥ off-peak guard, the DD-14 cross-check, and the golden test per zone. |
| R5 | Blocking I/O in the event loop | The snapshot is read once at module import (executor). Flow steps only use memory. |
| R6 | Downgrade to an older build | New keys ignored, `prefill` dropped, G13 fails closed with the existing message. Documented in the guide. |
| R7 | Shared VAT changes the sell side | The prefill never writes `vat_percent` when `sell_apply_vat` is on; it adds a note instead (D2). |
| R8 | Helper overrides the prefilled value | Unbound and listed (D3). |
| R9 | Enea G12 hours are an assumption in energy-compass | Rates do not depend on hours. The HA params step stays, and the cross-check uses the default params. |
| R10 | Clock semantics differ (energy-compass standard vs HA local) | Rates are clock-independent. The cross-check runs on CET, and the user's clock choice is unchanged. |
| R11 | Float drift against the calculator | Decimal arithmetic in `compute_prefill`, values rounded to 6 dp. The test asserts both < 0.001 and the exact literal. |

---

## 6. Verification matrix

| Requirement (task text) | Where | Test |
| --- | --- | --- |
| Pick operator + group + offer, zero typing, editable | §1.5, T7 | `test_flow_pge_g12_ure_prefills_and_shows_values` |
| Reversal recorded, docstring updated | §0, O-1, T4 | review + `tariffs.py` docstring |
| Snapshot + generator + `--check` | DD-6, DD-7, T4 | `test_build_tariff_rates.py`, CI step |
| Full price = energy + distribution + excise, VAT; = calculator ±0.001 | DD-8, T5 | `test_golden_prices` (appendix A) |
| Only ure + fixed | DD-7, DD-13 | `test_skips_dynamic_offers`, `test_offers_for_scope_and_order` |
| Sell unchanged | DD-8 | `test_vat_rule`, `test_prefill_save_writes_values_and_provenance` (sell keys equal) |
| monthly_charge computed from 3 answers | DD-8, §1.5 | `test_monthly_charge_variants`, golden |
| Groups G11/G12/G12w × 5, Tauron G13, Enea G13active | DD-3, T1, T4 | `test_snapshot_covers_catalog`, band tests |
| G13active without a URE price | DD-10 | `test_enea_g13active_distribution_only`, flow test |
| Stale: year + asOf shown, Preview warns, values unchanged | DD-15 | `test_offer_step_shows_year_and_stale_note`, `test_preview_stale_after_valid_to` |
| Band tests: weekday, Saturday, holiday, Tauron summer/winter | T1 | `test_tauron_g13_*`, `test_enea_g13active_*` |
| Profiles revision | DD-11, T2 | `test_revision_4_*` |
| Key hygiene + helper-bound | DD-12, T6 | `test_prefill_save_pops_helpers_of_written_keys`, `test_manual_save_drops_prefill_keeps_rates` |
| No VERSION bump, back-compat | DD-16, T6 | `test_schedule_without_prefill_round_trips_byte_identical`, `test_older_reader_ignores_prefill` |
| EN+PL doc sweep | T9 | `test_deye_controller_docs.py`, `build_guides.py --check` |
| Full suite + ruff + builders in the image | every task | the full gate (§4) |

---

## Appendix A — Golden prices (2026 snapshot)

All values are computed with Python `Decimal` from energy-compass `packages/data/src` at 6a0f8a7:
`vatRate` 0.23 and excise 0.005 zł/kWh.

Columns:
- `e` = seller net energy + excise, which goes to `buy_*rate`;
- `d` = variable + quality (0.0332) + OZE (0.0073) + cogeneration (0.003), which goes to
  `buy_distribution_*`;
- `gross` = (e + d) × 1.23, the per-zone price both HA and the calculator produce;
- monthly = fixed (one phase) + subscription (monthly) + capacity (≤2800 band) + transitional
  (0) + trade fee, shown as net / gross.

Band order: peak; mid_peak; off_peak.

| Operator | Group | Offer | Per band: e · d · gross | Monthly net / gross |
| --- | --- | --- | --- | --- |
| pge | G11 | energa-podstawowa-2-lata | peak 0.5000 · 0.3904 · 1.095192 | 44.17 / 54.33 |
| pge | G11 | energa-podstawowa-3-lata | peak 0.4992 · 0.3904 · 1.094208 | 45.80 / 56.33 |
| pge | G11 | pge-prad-z-gwarancjami | peak 0.4950 · 0.3904 · 1.089042 | 48.18 / 59.26 |
| pge | G11 | pge-ure-2026 | peak 0.5032 · 0.3904 · 1.099128 | 27.18 / 33.43 |
| pge | G12 | energa-podstawowa-2-lata | peak 0.6080 · 0.4449 · 1.295067; off_peak 0.4037 · 0.1200 · 0.644151 | 47.17 / 58.02 |
| pge | G12 | energa-podstawowa-3-lata | peak 0.6071 · 0.4449 · 1.293960; off_peak 0.4031 · 0.1200 · 0.643413 | 48.80 / 60.02 |
| pge | G12 | pge-prad-z-gwarancjami | peak 0.56184 · 0.4449 · 1.238290; off_peak 0.36966 · 0.1200 · 0.602282 | 51.18 / 62.95 |
| pge | G12 | pge-ure-2026 | peak 0.5706 · 0.4449 · 1.249065; off_peak 0.3768 · 0.1200 · 0.611064 | 30.18 / 37.12 |
| pge | G12w | energa-podstawowa-2-lata | peak 0.6080 · 0.4711 · 1.327293; off_peak 0.4037 · 0.1280 · 0.653991 | 47.82 / 58.82 |
| pge | G12w | energa-podstawowa-3-lata | peak 0.6071 · 0.4711 · 1.326186; off_peak 0.4031 · 0.1280 · 0.653253 | 49.45 / 60.82 |
| pge | G12w | pge-prad-z-gwarancjami | peak 0.5782 · 0.4711 · 1.290639; off_peak 0.42093 · 0.1280 · 0.675184 | 51.83 / 63.75 |
| pge | G12w | pge-ure-2026 | peak 0.5871 · 0.4711 · 1.301586; off_peak 0.4285 · 0.1280 · 0.684495 | 30.83 / 37.92 |
| tauron | G11 | energa-podstawowa-2-lata | peak 0.5000 · 0.2899 · 0.971577 | 46.11 / 56.72 |
| tauron | G11 | energa-podstawowa-3-lata | peak 0.4992 · 0.2899 · 0.970593 | 47.74 / 58.72 |
| tauron | G11 | tauron-ure-2026 | peak 0.5020 · 0.2899 · 0.974037 | 29.12 / 35.82 |
| tauron | G12 | energa-podstawowa-2-lata | peak 0.6080 · 0.3276 · 1.150788; off_peak 0.4037 · 0.0993 · 0.618690 | 46.11 / 56.72 |
| tauron | G12 | energa-podstawowa-3-lata | peak 0.6071 · 0.3276 · 1.149681; off_peak 0.4031 · 0.0993 · 0.617952 | 47.74 / 58.72 |
| tauron | G12 | tauron-ure-2026 | peak 0.5480 · 0.3276 · 1.076988; off_peak 0.4180 · 0.0993 · 0.636279 | 29.12 / 35.82 |
| tauron | G12w | energa-podstawowa-2-lata | peak 0.6080 · 0.3733 · 1.206999; off_peak 0.4037 · 0.0947 · 0.613032 | 46.11 / 56.72 |
| tauron | G12w | energa-podstawowa-3-lata | peak 0.6071 · 0.3733 · 1.205892; off_peak 0.4031 · 0.0947 · 0.612294 | 47.74 / 58.72 |
| tauron | G12w | tauron-ure-2026 | peak 0.6270 · 0.3733 · 1.230369; off_peak 0.4180 · 0.0947 · 0.630621 | 29.12 / 35.82 |
| tauron | G13 | tauron-ure-2026 | peak 0.7830 · 0.4333 · 1.496049; mid_peak 0.4718 · 0.2638 · 0.904788; off_peak 0.4260 · 0.0827 · 0.625701 | 29.12 / 35.82 |
| enea | G11 | enea-ure-2026 | peak 0.5030 · 0.2891 · 0.974283 | 28.47 / 35.02 |
| enea | G11 | energa-podstawowa-2-lata | peak 0.5000 · 0.2891 · 0.970593 | 45.46 / 55.92 |
| enea | G11 | energa-podstawowa-3-lata | peak 0.4992 · 0.2891 · 0.969609 | 47.09 / 57.92 |
| enea | G12 | enea-ure-2026 | peak 0.5829 · 0.3214 · 1.112289; off_peak 0.3419 · 0.1348 · 0.586341 | 30.61 / 37.65 |
| enea | G12 | energa-podstawowa-2-lata | peak 0.6080 · 0.3214 · 1.143162; off_peak 0.4037 · 0.1348 · 0.662355 | 47.60 / 58.55 |
| enea | G12 | energa-podstawowa-3-lata | peak 0.6071 · 0.3214 · 1.142055; off_peak 0.4031 · 0.1348 · 0.661617 | 49.23 / 60.55 |
| enea | G12w | enea-ure-2026 | peak 0.6568 · 0.3137 · 1.193715; off_peak 0.3515 · 0.1248 · 0.585849 | 37.87 / 46.58 |
| enea | G12w | energa-podstawowa-2-lata | peak 0.6080 · 0.3137 · 1.133691; off_peak 0.4037 · 0.1248 · 0.650055 | 54.86 / 67.48 |
| enea | G12w | energa-podstawowa-3-lata | peak 0.6071 · 0.3137 · 1.132584; off_peak 0.4031 · 0.1248 · 0.649317 | 56.49 / 69.48 |
| enea | G13active | distribution only | peak — · 0.3467 · —; mid_peak — · 0.2891 · —; off_peak — · 0.1165 · — | 30.61 / 37.65 |
| energa | G11 | energa-podstawowa-2-lata | peak 0.5000 · 0.3920 · 1.097160 | 46.64 / 57.37 |
| energa | G11 | energa-podstawowa-3-lata | peak 0.4992 · 0.3920 · 1.096176 | 48.27 / 59.37 |
| energa | G11 | energa-ure-2026 | peak 0.5018 · 0.3920 · 1.099374 | 29.65 / 36.47 |
| energa | G12 | energa-podstawowa-2-lata | peak 0.6080 · 0.4279 · 1.274157; off_peak 0.4037 · 0.1262 · 0.651777 | 53.16 / 65.39 |
| energa | G12 | energa-podstawowa-3-lata | peak 0.6071 · 0.4279 · 1.273050; off_peak 0.4031 · 0.1262 · 0.651039 | 54.79 / 67.39 |
| energa | G12 | energa-ure-2026 | peak 0.5839 · 0.4279 · 1.244514; off_peak 0.3803 · 0.1262 · 0.622995 | 36.17 / 44.49 |
| energa | G12w | energa-podstawowa-2-lata | peak 0.6080 · 0.4452 · 1.295436; off_peak 0.4037 · 0.1286 · 0.654729 | 53.16 / 65.39 |
| energa | G12w | energa-podstawowa-3-lata | peak 0.6071 · 0.4452 · 1.294329; off_peak 0.4031 · 0.1286 · 0.653991 | 54.79 / 67.39 |
| energa | G12w | energa-ure-2026 | peak 0.6107 · 0.4452 · 1.298757; off_peak 0.3990 · 0.1286 · 0.648948 | 36.17 / 44.49 |
| stoen | G11 | energa-podstawowa-2-lata | peak 0.5000 · 0.2777 · 0.956571 | 48.51 / 59.67 |
| stoen | G11 | energa-podstawowa-3-lata | peak 0.4992 · 0.2777 · 0.955587 | 50.14 / 61.67 |
| stoen | G11 | eon-taryfa-2026 | peak 0.5050 · 0.2777 · 0.962721 | 44.75 / 55.04 |
| stoen | G12 | energa-podstawowa-2-lata | peak 0.6080 · 0.2980 · 1.114380; off_peak 0.4037 · 0.0990 · 0.618321 | 48.51 / 59.67 |
| stoen | G12 | energa-podstawowa-3-lata | peak 0.6071 · 0.2980 · 1.113273; off_peak 0.4031 · 0.0990 · 0.617583 | 50.14 / 61.67 |
| stoen | G12 | eon-taryfa-2026 | peak 0.5394 · 0.2980 · 1.030002; off_peak 0.4295 · 0.0990 · 0.650055 | 44.75 / 55.04 |
| stoen | G12w | energa-podstawowa-2-lata | peak 0.6080 · 0.3005 · 1.117455; off_peak 0.4037 · 0.1514 · 0.682773 | 48.51 / 59.67 |
| stoen | G12w | energa-podstawowa-3-lata | peak 0.6071 · 0.3005 · 1.116348; off_peak 0.4031 · 0.1514 · 0.682035 | 50.14 / 61.67 |
| stoen | G12w | eon-taryfa-2026 | peak 0.5294 · 0.3005 · 1.020777; off_peak 0.4445 · 0.1514 · 0.732957 | 44.75 / 55.04 |

Note: the gross is not rounded, matching `costOf`. The monthly gross is rounded to 0.01 for
`monthly_charge`. Task 5 re-derives every literal once by hand from the source JSON and stops if
one differs (a typo here is a spec bug, not a code bug).

## Appendix B — Enea G13active hours (standard clock, every day incl. holidays)

| Month | peak (restricted) | mid_peak (remaining) | off_peak |
| --- | --- | --- | --- |
| Jan | 7–10, 15–20 | 6–7, 10–15, 20–23 | rest |
| Feb | 7–9, 16–21 | 6–7, 9–16, 21–23 | rest |
| Mar | 6–9, 16–23 | 9–10, 23–24, 0–6 | rest |
| Apr | 6–9, 18–23 | 9–10, 16–18, 23–24, 0–6 | rest |
| May–Aug | 6–9, 18–23 | 17–18, 23–24, 0–6 | rest |
| Sep | 6–9, 17–23 | 9–10, 16–17, 23–24, 0–6 | rest |
| Oct | 7–9, 16–23 | 9–10, 23–24, 0–7 | rest |
| Nov | 7–9, 14–21 | 6–7, 9–14, 21–23 | rest |
| Dec | 7–10, 13–20 | 6–7, 10–13, 20–23 | rest |

Tauron G13 (standard clock, Mon–Fri except holidays; weekends and holidays are entirely off_peak):
- mid_peak (morning) 7–13;
- peak (afternoon) 19–22 in April–September and 16–21 in October–March;
- off_peak at every other hour.
