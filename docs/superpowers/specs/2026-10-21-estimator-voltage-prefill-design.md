# Estimator voltages suggested from the Deye inverter's battery settings — design

Date: 2026-10-21 · Status: proposed (owner decisions of 2026-10-21 recorded in §3.1) · Task: T-112 ·
Base: `main` at d571703 (release 0.1.44)

The counter SOC estimator needs two pack voltages: `empty_voltage` (the pack voltage that is 0 %) and
`full_min_voltage` (the pack voltage a charge reaches at full). Today both fields of the estimator
form start empty and have no default (`soc_estimator_flow.py:227-232`,
`docs/source-requirements.md:81-82`). A Deye inverter read through Solarman already knows both: its
battery shutdown voltage and its float / absorption voltages. This spec reads them when the form
opens and offers them as the suggested values, so a Deye owner types nothing.

---

## 0. Summary

When the **Battery SOC source** form (`async_step_soc_estimator`) renders and its four core sources
(`charge_counter`, `discharge_counter`, `voltage`, `current`) are the Solarman entities
`total_battery_charge`, `total_battery_discharge`, `battery_voltage` and `battery_current` of one
Deye config entry, the flow reads three Solarman `number` entities of that same entry, found by
translation key:

- `battery_shutdown_voltage` → 0 % voltage;
- `battery_float`, `battery_absorption` → full voltage = min(float, absorption).

The inverter keeps these settings in its 48 V scale even on an HV pack. The flow scales them by the
power of ten (1 or 10) that matches the pack voltage reading. On the live site (pack 519.6 V,
shutdown 49.5, float 55.3, absorption 56) this gives **495 V** and **553 V**. The owner's hand-tuned
values are 496 / 552 V.

The values are only suggestions. They fill the empty field and stay editable. A saved value always
wins, and nothing is ever written to the configuration unless the user submits the form. The form
names the origin ("From the inverter: shutdown 49.5 V × 10 = 495 V"), and Preview repeats it. Any
missing, disabled, unavailable or non-numeric setting, a scale that fits neither 1 nor 10, or a 0 %
voltage at or above the full one means no suggestion. The field then stays empty as today. This is
never an error.

There is no new polling, no runtime code, no config-entry `VERSION` bump and no new setting.

---

## 1. Design

### 1.1 Goal, scope, out of scope

**Goal.** A Deye owner whose estimator sources are the inverter's Solarman entities saves the
estimator settings without typing either voltage.

**In scope.**
1. A pure module `estimator_prefill.py`: find the three settings, scale them, build both values and
   the provenance text (EN, PL).
2. The estimator form (`soc_estimator_flow.py`): suggested values, a `{prefill}` description line.
3. A Preview line for the estimator voltages that names the origin (`config_flow.py`).
4. Translations: the form's description in `strings.json`, `translations/en.json` and
   `translations/pl.json` (config and options step), and the detection note in `detect.py`.
5. The EN+PL doc sweep (§1.7).

**Out of scope (owner).**
- Any change to the anchor rules or to how the estimator uses the voltages.
- Writing any inverter setting.
- The native controller.
- Any release or version bump.

**Ambiguities resolved here** (each has a default in §3.2).

- A1 — *"The power of ten nearest to `battery_voltage / battery_float`" with "ratio 3 → none".* Plain
  rounding in log space maps 3 to ×1 (log₁₀ 3 = 0.48 < 0.5). The required golden case therefore needs
  a closeness band around the chosen power. DD-3 sets it to a factor of 1.5 either side.
- A2 — *"Float missing → empty prefilled only".* The scale is defined against the float. With no float,
  the shutdown voltage is the reference instead (DD-3). The scale is a property of the inverter, the
  same for every one of its voltage settings.
- A3 — *"Full = min(float, absorption)" and "absorption alone is not used".* With float and no
  absorption, full = float. With absorption and no float, there is no full suggestion (DD-4).
- A4 — *"Empty ≥ full = no prefill for that value".* Both values come from the same inverter, so an
  inverted pair means neither can be trusted. There is no suggestion for either (DD-4). When only one
  value exists, there is nothing to compare. A saved value on the other field is not compared here:
  `cfg.validate` checks the pair on submit, as today.
- A5 — *"Same Deye config entry … the estimator binds".* The binding the form shows (its `bound(role)`:
  the user's input, else the saved binding, else the detected one) decides. All four core roles must
  point at that entry's Solarman sensors with the four estimator translation keys (DD-2).

### 1.2 Decisions

**DD-1 — Flow-time read of the detection snapshot, no new polling.**
- The read happens only while the estimator form renders. It uses the same `DetectionSnapshot`
  (`detect_ha.async_detection_snapshot`) that `_detected_roles` already builds for the form's
  detected sources. The snapshot holds every `solarman` platform entity (`detect_ha.py:97-99`): its
  registry facts (translation key, config entry, device, disabled) and its current state and
  attributes.
- The flow builds the snapshot once per flow and keeps it in `self._estimator_snapshot`, beside
  `self._estimator_detected`. `_detected_roles` reads the same cached snapshot, so the step still
  calls `async_detection_snapshot` once per flow, as today.
- The values are Solarman's last reported states. Nothing subscribes, polls or writes. The
  coordinator, the runtime and the estimator never call this code.
- A failure building the snapshot is caught, as `_detected_roles` does today
  (`soc_estimator_flow.py:85-87`). The result is no suggestion.

**DD-2 — The Deye entry is the one all four core sources are bound to.**
`bound_entry(snapshot, bound)` takes `bound: Mapping[str, str | None]` (core role → entity id, from
the form's `bound(role)`). It returns the config entry id when every check below passes, else `None`:
1. each of the four core roles has an entity id that is in `snapshot.entities`;
2. each entity has platform `solarman` and the translation key that `ESTIMATOR_KEYS` gives its role
   (`charge_counter` → `total_battery_charge`, `discharge_counter` → `total_battery_discharge`,
   `voltage` → `battery_voltage`, `current` → `battery_current`);
3. all four have the same `config_entry_id`;
4. that entry is a Deye entry (`detect._deye_entries`: a device whose manufacturer is `deye`,
   case-insensitive).

Entity ids and names are never matched. The optional `power` role and the expert roles play no part.

**DD-3 — Scale rule: ×1 or ×10, from the pack voltage reading.**
- **Reference setting.** `battery_float` when its value is usable (DD-5), else
  `battery_shutdown_voltage`. `battery_absorption` is never the reference.
- **Ratio.** r = V / reference, where V is the numeric state of the bound `voltage` entity
  (`battery_voltage`, unit `V`, finite, > 0). No usable V or no usable reference means no scale.
- **Factor.** f = 10^round(log₁₀ r). It is accepted only when f ∈ {1, 10} **and**
  1/1.5 ≤ r / f ≤ 1.5 (`SCALE_TOLERANCE = 1.5`). Anything else means no scale, and both suggestions
  are withheld with reason `scale`.
- **Why 1.5.** Over a working pack, V/float runs from shutdown/float to about absorption/float
  (LFP 0.82–1.05, lead-acid down to about 0.73). V/shutdown runs from 1 to about 1.4. A band of 1.5
  holds all of these. It still refuses every ratio from 1.5 to 6.7, where neither scale is plausible.
- **Examples** (the golden cases of Task 1):

| Site | V | shutdown | float | absorption | reference | r | f | empty | full |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| HV pack (live site) | 519.6 | 49.5 | 55.3 | 56 | float | 9.40 | 10 | 495 | 553 |
| 48 V pack | 52.1 | 46.0 | 54.0 | 55.2 | float | 0.965 | 1 | 46 | 54 |
| 48 V pack, absorption below float | 52.1 | 46.0 | 55.3 | 54.8 | float | 0.942 | 1 | 46 | 54.8 |
| Mismatch | 165.9 | 49.5 | 55.3 | 56 | float | 3.0 | — | none (`scale`) | none (`scale`) |
| Float missing | 519.6 | 49.5 | — | 56 | shutdown | 10.50 | 10 | 495 | none (`missing`) |
| Inverted pair | 519.6 | 56.0 | 55.3 | 56 | float | 9.40 | 10 | none (`order`) | none (`order`) |
| Pack reads 0 V | 0 | 49.5 | 55.3 | 56 | float | — | — | none (`scale`) | none (`scale`) |

**DD-4 — Values.**
- empty = round(shutdown × f, 1).
- full = round(min(float, absorption) × f, 1) when both are usable, round(float × f, 1) when only
  float is usable, and none when float is not usable.
- When both values exist and empty ≥ full, neither is suggested (reason `order`).
- A value outside the form's range [1, 10000] V is not suggested (reason `range`). This cannot happen
  with an accepted scale, but it guards the selector.
- `battery_empty` and `battery_low_voltage` are never read (owner, §3.1 item 5).

**DD-5 — A usable setting.** A setting is usable when exactly one snapshot entity exists with:
- platform `solarman`, the bound entry's `config_entry_id` and the translation key;
- an entity id starting with `number.`;
- not disabled in the registry;
- a state that parses as a finite float > 0, with unit `V`.

Two or more such entities with one key are ambiguous, and the setting is not usable (the detection
rule for counters, `detect.py:577`). An unusable setting gives reason `missing` (no single entity,
or disabled) or `unreadable` (unavailable, unknown, non-numeric, ≤ 0, or another unit) for the value
it feeds.

**DD-6 — Suggestion only: precedence and persistence.**
- The form's `suggested(key)` becomes, for `empty_voltage` and `full_min_voltage` only: the value
  the user just submitted (a re-rendered form), else the saved block value, else the prefill, else
  nothing. The saved value always wins, so Configure / Reconfigure never replaces a saved voltage.
  Nothing is computed or written outside the form.
- The prefill is never inserted into the candidate on the server side. `_estimator_candidate` reads
  only `user_input`, as today. A form submitted without a voltage still fails with `invalid_input`
  (existing `test_the_pack_voltage_that_is_zero_is_required` stays green).
- As with every `suggested_value` in Home Assistant, the frontend puts the suggestion in the field.
  A submit sends it, so it is saved like a typed value, exactly as the measured round-trip
  efficiency is today (`eta_charge`, `soc_estimator_flow.py:242-252`). From then on it is a saved
  value, and DD-6's first point applies.
- The flow keeps the last computed `VoltagePrefill` in `self._estimator_prefill` for Preview (DD-8).
  It is never stored in the config entry, the options or the estimator store.

**DD-7 — Provenance in the form.**
- New description placeholder `{prefill}` in the `soc_estimator` step (config and options). It is the
  empty string when no Deye entry is bound (DD-2). The form then reads exactly as today, apart from
  the reworded description sentence (§1.6).
- With a Deye entry bound, it is one line per voltage field that has no saved or submitted value:
  - the value with its origin; or
  - why there is no suggestion.
- A field with a saved value gets no line: the saved value is the user's, and repeating the
  inverter's number beside a hand-tuned one (496 vs 495) is noise.
- Texts (formatting `:g`, the same as `_seed_text`):

| Case | EN | PL |
| --- | --- | --- |
| empty suggested | `From the inverter: 0 % = shutdown {shutdown:g} V × {f} = {empty:g} V.` | `Z falownika: 0 % = napięcie wyłączenia {shutdown:g} V × {f} = {empty:g} V.` |
| full, both settings | `From the inverter: full = lower of float {float:g} V and absorption {absorption:g} V × {f} = {full:g} V.` | `Z falownika: pełny = niższe z napięć float {float:g} V i absorpcji {absorption:g} V × {f} = {full:g} V.` |
| full, float only | `From the inverter: full = float {float:g} V × {f} = {full:g} V.` | `Z falownika: pełny = napięcie float {float:g} V × {f} = {full:g} V.` |
| none, `missing` | `No suggestion from the inverter for {field}: its {setting} setting is not available.` | `Brak podpowiedzi z falownika dla pola {field}: ustawienie {setting} jest niedostępne.` |
| none, `unreadable` | `No suggestion from the inverter for {field}: its {setting} setting has no readable voltage.` | `Brak podpowiedzi z falownika dla pola {field}: ustawienie {setting} nie ma czytelnego napięcia.` |
| none, `scale` | `No suggestion from the inverter: its battery settings fit neither ×1 nor ×10 of the pack voltage ({voltage:g} V).` | `Brak podpowiedzi z falownika: jego ustawienia baterii nie pasują ani ×1, ani ×10 do napięcia pakietu ({voltage:g} V).` |
| none, `order` | `No suggestion from the inverter: its shutdown voltage is not below its full voltage.` | `Brak podpowiedzi z falownika: jego napięcie wyłączenia nie jest niższe od napięcia pełnego.` |
| none, `range` | `No suggestion from the inverter for {field}: {value:g} V is outside 1–10000 V.` | `Brak podpowiedzi z falownika dla pola {field}: {value:g} V wykracza poza 1–10000 V.` |

- Notes on the placeholders:
  - `{field}` is the field label of the language: EN `Pack voltage that is 0 %` /
    `Pack voltage a charge reaches at full`; PL `Napięcie pakietu odpowiadające 0 %` /
    `Napięcie pakietu, które ładowanie osiąga przy pełnym`. These are the labels from
    `translations/*.json`, without the trailing " (V)".
  - `{setting}` is the translation key (`battery_float`). It is what the user finds in the Solarman
    device page and in the entity registry.
  - `{f}` is `1` or `10`.
- `scale` and `order` concern both fields, so they produce one line, not two.
- Language follows `hass.config.language.startswith("pl")`, as `_seed_text` does.

**DD-8 — Provenance in Preview.**
- Preview gains one line when the estimator is active (`cfg.estimator_active(candidate)`):
  - EN: `SOC estimator voltages: 0 % at {empty:g} V{origin}, full at {full:g} V{origin}.`
  - PL: `Napięcia estymatora SOC: 0 % przy {empty:g} V{origin}, pełny przy {full:g} V{origin}.`
- `{origin}` is ` (from the inverter: shutdown 49.5 V × 10)` / ` (z falownika: napięcie wyłączenia
  49.5 V × 10)` for empty, and ` (from the inverter: float 55.3 V × 10)` / ` (z falownika: napięcie
  float 55.3 V × 10)` for full. For full, the named setting is whichever of float and absorption
  gave the minimum (`absorption` / `absorpcji`). An absent value is shown as `—`.
- `{origin}` is added only when `self._estimator_prefill` holds a value for that key and the draft
  value equals it (|Δ| < 0.05 V). The draft does not record whether a value was typed or accepted.
  Equality is the honest test: the number is the inverter's.
- Otherwise there is no origin. A saved 496 V reads `0 % at 496 V`.
- The line goes after the `Battery:` line of the Preview text. Preview has no other estimator line
  today.

### 1.3 Data model (code)

```python
# custom_components/energy_compass/estimator_prefill.py (new, pure: no homeassistant import)
SHUTDOWN_KEY = "battery_shutdown_voltage"
FLOAT_KEY = "battery_float"
ABSORPTION_KEY = "battery_absorption"
SCALE_FACTORS = (1, 10)
SCALE_TOLERANCE = 1.5
CORE_ROLES = ("charge_counter", "discharge_counter", "voltage", "current")

Reason = Literal["missing", "unreadable", "scale", "order", "range"]


@dataclass(frozen=True)
class Setting:
    key: str
    entity_id: str | None
    value: float | None  # volts, the inverter's own scale
    reason: Reason | None  # None when value is usable


@dataclass(frozen=True)
class VoltagePrefill:
    entry_id: str
    pack_voltage: float | None
    factor: int | None
    shutdown: Setting
    float_: Setting
    absorption: Setting
    empty: float | None
    full: float | None
    full_from: str | None  # FLOAT_KEY or ABSORPTION_KEY, the minimum's key
    empty_reason: Reason | None
    full_reason: Reason | None


def bound_entry(
    snapshot: DetectionSnapshot, bound: Mapping[str, str | None]
) -> str | None: ...


def scale_factor(pack_voltage: float | None, reference: float | None) -> int | None: ...


def voltage_prefill(
    snapshot: DetectionSnapshot, bound: Mapping[str, str | None]
) -> VoltagePrefill | None:  # None: no Deye entry bound (DD-2)
    ...


def prefill_text(
    prefill: VoltagePrefill | None, language: str, *, skip: frozenset[str]
) -> str: ...  # skip: the keys with a saved or submitted value (DD-7)


def preview_line(
    prefill: VoltagePrefill | None,
    empty: float | None,
    full: float | None,
    language: str,
) -> str: ...
```

`detect.py` keeps owning the Deye identity. `estimator_prefill.py` imports `ESTIMATOR_KEYS`,
`DetectionSnapshot` and `deye_entries` from it (`_deye_entries` renamed; its two callers,
`detect.py:525` and `detect.py:907`, follow; no other module uses it on d571703).

### 1.4 Data flow

```text
async_step_soc_estimator (render)
  ├─ snapshot = self._estimator_snapshot  (async_detection_snapshot once per flow; DD-1)
  ├─ detected = _detected_roles()         (reads the same snapshot)
  ├─ bound(role) for the 4 core roles     (user input > saved binding > detected)
  ├─ prefill = voltage_prefill(snapshot, bound)            ─┐ pure, estimator_prefill.py
  │     bound_entry → 3 settings (DD-5) → scale (DD-3) → values (DD-4)
  ├─ self._estimator_prefill = prefill
  ├─ suggested("empty_voltage")  = submitted > saved > prefill.empty   (DD-6)
  ├─ suggested("full_min_voltage") = submitted > saved > prefill.full
  └─ placeholder {prefill} = prefill_text(prefill, language, skip=saved|submitted)  (DD-7)

user submits → _estimator_candidate(user_input)  (unchanged: only user_input is stored)

async_step_preview
  └─ if estimator_active(candidate): preview_line(self._estimator_prefill, block values)  (DD-8)
```

### 1.5 Flow changes (`soc_estimator_flow.py`)

- Add `_estimator_snapshot: DetectionSnapshot | None = None` and
  `_estimator_prefill: VoltagePrefill | None = None` class attributes.
- Add `async def _snapshot(self)`, which builds and caches the snapshot. On an exception it caches
  `None` and logs at debug. `_detected_roles` calls it, and on `None` returns `{}` as today.
- In `async_step_soc_estimator`, after `bound` is defined, call
  `voltage_prefill(snapshot, {role: bound(role) for role in CORE_ROLES})` when the snapshot is not
  `None`. Store the result in `self._estimator_prefill`.
- `suggested("empty_voltage", prefill.empty if prefill else None)`, and the same for full. The
  existing `suggested(key, fallback)` already gives submitted > saved > fallback.
- `description_placeholders["prefill"] = prefill_text(...)`, with `skip` = the voltage keys present in
  `values` or in `block`.

### 1.6 Translations and texts

- `strings.json`, `translations/en.json` (equal to `strings.json`) and `translations/pl.json`,
  `config.step.soc_estimator.description` and `options.step.soc_estimator.description`:
  - EN: replace "both voltages are your pack’s own values and have no default." with "both voltages
    are your pack’s own values. With a Deye inverter read through Solarman, they are suggested from
    its battery settings (shutdown voltage for 0 %, the lower of float and absorption for full,
    scaled to the pack voltage); check them against your pack. Otherwise there is no default."
  - PL: the matching sentence of `pl.json` gets the same meaning.
  - Insert `{prefill}` before `{seed}`: `…\n\n{prefill}\n\n{seed}\n\n{detail}`.
- `detect.py` `_TEXT["en"]["estimator_note"]` / `["pl"]`: add one sentence. EN: "On a Deye inverter
  they are suggested from its battery settings." PL: "Na falowniku Deye podpowiada je ustawienie
  baterii falownika." `test_soc_estimator_detect_flow.py:52` keeps matching
  `"pack voltage that is 0 %"`.

### 1.7 Doc sweep (AGENTS.md)

| File | Where | Change |
| --- | --- | --- |
| `docs/guide.en.md` | § Counter SOC estimator, "Zero is a voltage" (~668-669) | "there is no default" → suggested from a Deye inverter's `battery_shutdown_voltage` × 1 or × 10 (scale rule in one sentence, the live example 49.5 V × 10 = 495 V); otherwise no default; a saved value is never replaced |
| `docs/guide.en.md` | "A full pack is a voltage too" (~675-677) | same: lower of `battery_float` and `battery_absorption` × the same factor; float alone when absorption is missing; none without float |
| `docs/guide.en.md` | settings table rows `empty_voltage`, `full_min_voltage` (~788-789) | "required, none" → "required; suggested from a Deye inverter's settings, else none" |
| `docs/guide.en.md` | "Setup and detection" (~813) | the settings step opens with both voltages suggested from the inverter |
| `docs/guide.pl.md` | the four mirror places (~684-693, ~804-805, ~829) | same meaning, PL labels from `pl.json` |
| `docs/source-requirements.md` | Counter SOC estimator sources table, rows 81-82 | "No default" → "Suggested from the Deye inverter's `battery_shutdown_voltage` / `battery_float`, `battery_absorption` (Solarman `number` entities of the same config entry), scaled ×1 or ×10; no default otherwise" + one paragraph under the table with the scale rule and the reasons for no suggestion |
| `docs/model.md` | § counter estimator (~281) | "(`full_min_voltage`, required, no default)" → "required; suggested from the inverter's settings on a Deye" |
| `README.md`, `README.pl.md`, `docs/index.html`, `docs/installation.md` | — | grep finds no voltage default claim on d571703: no change; the PR says so |

- Guides: `python tools/build_guides.py`, then commit the HTML. No Mermaid diagram changes.
- Before writing, re-run the grep from AGENTS.md step 1 for `empty_voltage`, `full_min_voltage`,
  `no default`, `pack voltage that is 0`, `Napięcie pakietu odpowiadające 0`.

---

## 2. Rejected alternatives

- **Write the prefill into the configuration at detection time** (`set_counter_estimator` stores
  495 / 553). It saves values the user never saw and re-applies them whenever detection runs. That
  breaks the owner's "suggestion only, never overwrites" (§3.1 item 3).
- **A live link: the estimator reads the inverter settings at runtime.** The voltages would follow
  every change of the inverter. But the anchor thresholds would then change under a running
  estimator, and that needs a runtime path, availability handling and an anchor-rule review, all
  out of scope. A one-time suggestion keeps the estimator's settings the user's own.
- **Scale from the entity's `unit_of_measurement` or the device model** (an HV model list). Solarman
  reports `V` for both the 48 V-scale settings and the HV pack, and a model list goes stale. The
  pack reading on the same entry is the only evidence that is always there.
- **Plain nearest power of ten with no band** (owner wording read literally). It maps ratio 3 to ×1
  and suggests a 0 % voltage of 49.5 V for a 165 V pack, which is exactly what the owner's "ratio 3 →
  none" case forbids (A1).
- **Any power of ten** (×100 and up). No Deye pack sits at 100× its setting scale. Accepting it
  would only widen the error class. The owner fixed {1, 10}.
- **`battery_empty` or `battery_low_voltage` for 0 %.** These are alarm and low-SOC points, not where
  the inverter stops (owner, §3.1 item 5).
- **Absorption alone for full.** On the live site the BMS alarms above 55.2 V, so 56 V is never
  reached and a full anchor would never fire (owner, §3.1 item 5).
- **Show the inverter's values next to saved ones** ("inverter now: 495 V"). That is noise on every
  hand-tuned site, and it invites overwriting a deliberate value. A user who wants the suggestion
  clears the field and reopens the step.
- **Match the settings by entity id or name.** Names are localized and user-editable. Detection rule:
  integration identity and translation key only (`detect.py:3-5`).

---

## 3. Decisions

### 3.1 Human-confirmed (owner, 2026-10-21)

1. Source = Solarman `number` entities of the same Deye config entry whose `total_battery_charge`,
   `total_battery_discharge`, `battery_voltage` and `battery_current` the estimator binds, found by
   translation key: `battery_shutdown_voltage` → empty; `battery_float` and `battery_absorption` →
   full = min(float, absorption).
2. The settings are in the inverter's 48 V scale even on HV packs (live site: `battery_voltage`
   519.6 V, `battery_float` 55.3, `battery_absorption` 56, `battery_shutdown_voltage` 49.5,
   `battery_empty` 48, `battery_low_voltage` 50). Scale factor = the power of ten nearest to
   `battery_voltage / battery_float`, only 1 or 10 accepted, else no prefill. Live result 495 / 553 V
   (hand-tuned 496 / 552 V).
3. A suggestion only: shown as the suggested value in the estimator form, editable, never overwrites
   a saved value, never re-applied by Configure/Reconfigure on its own. The form and Preview name the
   origin. It applies to Detected sources (the `soc_estimator` row) and to Battery SOC source →
   Counter estimator when a Deye Solarman entry is bound.
4. A missing, disabled, unavailable or non-numeric entity, a failed scale, or empty ≥ full means no
   prefill for that value, never an error.
5. `battery_empty` and `battery_low_voltage` are not used. Absorption alone is not used.

### 3.2 Open decisions (defaults applied in this spec)

| # | Question | Default (applied) | Alternative and consequence |
| --- | --- | --- | --- |
| Q1 | Closeness band around ×1 / ×10 (A1, DD-3) | 1.5 either side: r/f in [0.67, 1.5] | 1.25: refuses a 48 V lead-acid pack read near its shutdown voltage (r ≈ 0.75); 2.0: accepts r = 2 as ×1 and r = 5 as ×10 |
| Q2 | Scale reference when float is missing (A2) | shutdown voltage | no scale without float: then "float missing → empty only" (owner's acceptance case) cannot hold |
| Q3 | Unit of the `number` entities | exactly `V`; another or no unit → `unreadable` | accept a missing unit: a profile with an unexpected unit (mV) would be read as volts |
| Q4 | Inverted pair (A4) | neither value suggested | suggest the one that is plausible: there is no evidence for which of the two is wrong |
| Q5 | Suggestion while **SOC source = Entity** (shadow off) | the fields still show it; a submit saves it, like `eta_charge` today (DD-6) | hide it unless the counter estimator is chosen: the form is one schema, and HA cannot change it when the dropdown changes, so this would need a two-step form |

None of these blocks the plan. Each is a constant or one branch in Task 1.

---

## 4. Implementation plan

> **For agentic workers:** one task = one session = one commit on the task branch. TDD: write the
> failing tests named in the task, run them red, implement, run them green, then the full gate.
> Do not edit generated files by hand (AGENTS.md). Never weaken an existing test; the assertions
> this plan changes are listed per task. A ```python block added to any `.md` file is checked with
> `ruff format --check` (LESSONS.md 2026-10-20).

**Goal.** §0. **Architecture.**
- `estimator_prefill.py` (pure: identity, scale, values, texts);
- `soc_estimator_flow.py` (snapshot cache, suggestions, placeholder);
- `config_flow.py` (Preview line);
- translations and docs.

**Tech stack.** Python 3.13, the HA test image (pytest, ruff). No new dependency.

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
docker run --rm -v "$PWD:/workspace:ro" ha-energy-compass-test:2026.9.1 tools/build_tariff_rates.py --check
```

Narrow runs use the same image with `-m pytest -p no:cacheprovider -q tests/<file>.py`.

**Step 0 (every task).** Read the task log, `LESSONS.md` and this spec's DD rows for the task.
Build the image once. `git merge origin/main` is the last step before `shq done` (LESSONS.md
2026-10-13).

**Order and dependencies.** The three tasks touch the same flow and the same guide paragraphs, so
they are chained (LESSONS.md 2026-10-13 evidence-pinned-to-a-moving-main):

```text
T1 (pure module) ── T2 (form + texts + doc sweep) ── T3 (Preview line + its doc lines)
```

### Task 1 — Pure prefill: identity, scale, values, texts

**Implements:** DD-2, DD-3, DD-4, DD-5, DD-7 texts, DD-8 line text; §1.3. **Deps:** none.
**No behaviour change** (nothing calls the module yet), so no doc sweep. The PR says so.

**Files:**
- `custom_components/energy_compass/estimator_prefill.py` (new).
- `custom_components/energy_compass/detect.py`: `_deye_entries` → `deye_entries` (public; update
  its two callers, lines 525 and 907).
- `tests/test_estimator_prefill.py` (new). Build a `DetectionSnapshot` by hand: `EntityFacts` for
  the four core sensors and the three `number` settings on entry `deye1`, plus a `DeviceFacts` with
  manufacturer `Deye`. Keep one helper `site(**overrides)`.

**Steps:**
- [ ] Step 1: Write the failing tests in `tests/test_estimator_prefill.py`:
  - golden cases, each asserting `empty`, `full`, `factor` and both reasons, one per DD-3 table row:
    - `test_hv_pack_scales_by_ten_live_site` (519.6 / 49.5 / 55.3 / 56 → f 10, 495.0, 553.0,
      `full_from == "battery_float"`);
    - `test_48_volt_pack_scales_by_one` (52.1 / 46.0 / 54.0 / 55.2 → 1, 46.0, 54.0);
    - `test_absorption_below_float_gives_full` (→ 54.8, `full_from == "battery_absorption"`);
    - `test_ratio_three_gives_no_suggestion` (165.9 → both `None`, both reasons `scale`);
    - `test_float_missing_prefills_empty_only` (→ 495.0, full `None` reason `missing`, factor 10
      from the shutdown reference);
    - `test_empty_not_below_full_gives_none` (shutdown 56.0 → both `None`, reason `order`);
    - `test_pack_voltage_zero_or_unavailable_gives_none` (parametrized "0", "unavailable" → `scale`);
  - `scale_factor` table test `test_scale_factor_band` (parametrized: (9.4, 10), (0.94, 1),
    (1.49, 1), (1.51, None), (6.6, None), (6.7, 10), (15.0, 10), (15.1, None), (3.0, None),
    (100.0, None), (0.66, None)). The pairs are (V, reference=1.0) → f;
  - identity, each returning `None` from `voltage_prefill`:
    - `test_no_prefill_without_all_four_core_sources_on_one_entry` (voltage on `deye2`);
    - `test_no_prefill_for_a_non_deye_solarman_entry` (manufacturer `Sofar`);
    - `test_no_prefill_when_a_core_source_is_not_solarman` (voltage `sensor.pack_v`, platform
      `template`);
    - `test_no_prefill_when_a_core_source_has_another_translation_key` (voltage bound to
      `battery_1_voltage`);
  - settings, per value:
    - `test_disabled_setting_is_missing` (float disabled → like float missing);
    - `test_unavailable_or_non_numeric_setting_is_unreadable` (parametrized "unavailable",
      "unknown", "abc", "nan", "-1" on shutdown → empty `None` reason `unreadable`, full 553.0);
    - `test_setting_with_another_unit_is_unreadable` (float unit `mV`);
    - `test_setting_of_another_entry_is_ignored` (float only on `deye2` → `missing`);
    - `test_two_settings_with_one_key_are_ambiguous` (two `battery_float` numbers → `missing`);
    - `test_sensor_with_the_setting_key_is_not_used` (`sensor.x` with `battery_float` → `missing`);
    - `test_battery_empty_and_low_voltage_are_never_read` (no shutdown entity, `battery_empty`
      and `battery_low_voltage` present → empty `None`, reason `missing`);
  - texts:
    - `test_prefill_text_live_site_en_pl` (exact strings of DD-7 for both rows, EN and PL);
    - `test_prefill_text_skips_saved_fields` (`skip={"empty_voltage"}` → only the full line);
    - `test_prefill_text_reasons` (one exact line per reason, EN and PL; `scale`/`order` give one
      line);
    - `test_prefill_text_empty_without_a_deye_entry` (`None` → `""`);
    - `test_preview_line_names_the_origin_only_on_equal_values` (495/553 → both origins; 496/552 →
      no origin; 495/552 → origin on empty only; EN and PL exact).
- [ ] Step 2: Run; expect FAIL: `shq test 'docker run --rm -v "$PWD:/workspace:ro" ha-energy-compass-test:2026.9.1 -m pytest -p no:cacheprovider -q tests/test_estimator_prefill.py'`.
- [ ] Step 3: Implement §1.3. `scale_factor`: `None` when either input is `None` or ≤ 0, or not
  finite; `f = 10 ** round(math.log10(r))`; return `f` if `f in SCALE_FACTORS and 1 / SCALE_TOLERANCE
  <= r / f <= SCALE_TOLERANCE` else `None`.
- [ ] Step 4: Narrow run PASS, plus `tests/test_detect.py tests/test_soc_estimator_detect.py`, then
  the full gate.
- [ ] Step 5: Commit `feat(T-xxx): suggest estimator voltages from the Deye battery settings (pure)`.

### Task 2 — Form suggestions, provenance line, translations, doc sweep

**Implements:** DD-1, DD-6, DD-7 (form), §1.5, §1.6, §1.7 (all rows but Preview). **Deps:** T1.

**Files:**
- `custom_components/energy_compass/soc_estimator_flow.py`.
- `custom_components/energy_compass/strings.json`, `translations/en.json`, `translations/pl.json`
  (config and options `soc_estimator.description`).
- `custom_components/energy_compass/detect.py` (`estimator_note` EN and PL).
- `docs/guide.en.md`, `docs/guide.pl.md`, `docs/source-requirements.md`, `docs/model.md`, and the
  regenerated `docs/guide.en.html` and `docs/guide.pl.html`.
- `tests/test_estimator_prefill_flow.py` (new). Reuse `site` / `rich_site` from
  `tests/test_detect_ha.py` / `tests/test_detect_flow.py` and `_estimator_entities` from
  `tests/test_soc_estimator_detect_flow.py`. Add a helper `_deye_settings(hass, site, shutdown="49.5",
  float_="55.3", absorption="56")` that registers `number.*` Solarman entities with those
  translation keys on `site["solarman"]` and `site["inverter"]`, unit `V`. The pack voltage state is
  `519.6`.

**Steps:**
- [ ] Step 1: Write the failing tests in `tests/test_estimator_prefill_flow.py`. Read suggested values
  from `key.description["suggested_value"]` of the form schema:
  - `test_detected_sources_open_the_form_with_the_inverter_voltages`: tick the `soc_estimator` row →
    form `soc_estimator` suggests 495.0 / 553.0; `description_placeholders["prefill"]` contains
    `"shutdown 49.5 V × 10 = 495 V"` and `"float 55.3 V and absorption 56 V × 10 = 553 V"`;
  - `test_options_counter_estimator_suggests_the_inverter_voltages`: entry whose estimator bindings
    are the four Solarman sensors and no voltages saved → options → `soc_estimator` → same
    suggestions;
  - `test_a_saved_value_is_not_overwritten`: saved 496 / 552 → suggested 496 / 552, `prefill`
    placeholder is `""`; submit the form with 496 / 552 → saved config still 496 / 552; open the
    step again → still 496 / 552;
  - `test_one_saved_value_keeps_the_other_suggested`: saved empty 496 only → suggested 496 / 553.0;
    the placeholder has the full line only;
  - `test_the_server_never_inserts_the_suggestion`: submit the form with both voltages omitted →
    `invalid_input`, as today, and the saved configuration has no voltage;
  - `test_an_unreadable_setting_leaves_the_field_empty_without_an_error`: float `unavailable` → no
    errors, full suggested `None`, empty 495.0, the placeholder names `battery_float`;
  - `test_a_mismatched_scale_suggests_nothing`: pack state `165.9` → both `None`; one `scale` line;
  - `test_no_deye_entry_keeps_todays_form`: the existing `_entry` site of
    `tests/test_soc_estimator_flow.py` (`sensor.pack_v`) → both `None`, `prefill == ""`;
  - `test_polish_texts`: `hass.config.language = "pl"` → the placeholder starts with
    `"Z falownika: 0 % = napięcie wyłączenia 49.5 V × 10 = 495 V."`;
  - `test_the_snapshot_is_built_once_per_flow`: patch `async_detection_snapshot` with a counter;
    render twice (an error re-render) → called once;
  - in `tests/test_soc_estimator_flow.py`: no assertion changes. The existing tests must stay
    green.
- [ ] Step 2: Run; expect FAIL: `shq test 'docker run --rm -v "$PWD:/workspace:ro" ha-energy-compass-test:2026.9.1 -m pytest -p no:cacheprovider -q tests/test_estimator_prefill_flow.py'`.
- [ ] Step 3: Implement §1.5 and §1.6. Then the doc sweep §1.7 (all rows except anything about
  Preview). Rebuild the guides with `python tools/build_guides.py` (markdown 3.8).
- [ ] Step 4: Narrow run PASS with `tests/test_estimator_prefill_flow.py tests/test_soc_estimator_flow.py
  tests/test_soc_estimator_detect_flow.py tests/test_detect_flow.py`, plus the translation checks
  (`grep -ln "strings.json" tests/*.py` names them), then the full gate.
- [ ] Step 5: Commit `feat(T-xxx): pre-fill the estimator voltages from the Deye inverter settings`.

### Task 3 — Preview names the origin

**Implements:** DD-8. **Deps:** T2.

**Files:**
- `custom_components/energy_compass/config_flow.py` (`async_step_preview`: one line after the
  `Battery:` line).
- `docs/guide.en.md`, `docs/guide.pl.md`: one sentence in § Counter SOC estimator ("Preview repeats the
  voltages and names those that came from the inverter"), and the regenerated HTML.
- `tests/test_estimator_prefill_flow.py` (extend).

**Steps:**
- [ ] Step 1: Write the failing tests:
  - `test_preview_names_the_inverter_origin`: detected path, submit the suggested 495 / 553 → the
    Preview description contains `"SOC estimator voltages: 0 % at 495 V (from the inverter: shutdown
    49.5 V × 10), full at 553 V (from the inverter: float 55.3 V × 10)."`;
  - `test_preview_shows_typed_values_without_origin`: submit 496 / 552 →
    `"0 % at 496 V, full at 552 V."`;
  - `test_preview_has_no_estimator_line_with_the_entity_source`: `soc_source` `entity`, shadow off →
    no `"SOC estimator voltages"` text;
  - `test_preview_line_polish`: `pl` → `"Napięcia estymatora SOC: 0 % przy 495 V (z falownika:
    napięcie wyłączenia 49.5 V × 10)"`.
- [ ] Step 2: Run; expect FAIL (narrow, as in Task 2).
- [ ] Step 3: Implement. Call `preview_line(self._estimator_prefill, block.get("empty_voltage"),
  block.get("full_min_voltage"), language)` from `estimator_prefill`, appended only when
  `cfg.estimator_active(candidate)`.
- [ ] Step 4: Narrow PASS, plus every test that asserts on the Preview text
  (`grep -ln "Source inputs: validated" tests/*.py`), then the full gate.
- [ ] Step 5: Commit `feat(T-xxx): name the inverter origin of the estimator voltages in Preview`.

---

## 5. Risks

| Risk | Effect | Mitigation |
| --- | --- | --- |
| A Solarman profile names the settings with other translation keys | No suggestion (today's empty field) | DD-5 never guesses. The PR notes the keys checked on the live site |
| A Deye HV model stores its settings in pack scale | r ≈ 1 → ×1, and the values are correct | The ratio decides, not the model |
| The inverter's settings are wrong for the pack (shutdown above the BMS cutoff) | A plausible but poor suggestion | The text names its origin. The owner check is the same as for a typed value. The guide says to check against the pack |
| A stale Solarman state (inverter offline) | Last known setting | Settings rarely change. `unavailable` gives no suggestion |
| The HA frontend keeps a suggestion after the user clears it on a re-render | The value comes back | Documented HA behaviour, the same as `eta_charge` |

## 6. Verification matrix

| Requirement (task body) | Covered by |
| --- | --- |
| Flow-time read, no new polling | DD-1; `test_the_snapshot_is_built_once_per_flow`; no runtime file in any task |
| Scale rule ×1 / ×10 / none | DD-3; `test_scale_factor_band`, the golden cases of Task 1 |
| HV ×10 live numbers → 495 / 553 | `test_hv_pack_scales_by_ten_live_site`, `test_detected_sources_open_the_form_with_the_inverter_voltages` |
| 48 V pack ×1 | `test_48_volt_pack_scales_by_one` |
| Ratio 3 → none | `test_ratio_three_gives_no_suggestion`, `test_a_mismatched_scale_suggests_nothing` |
| Float missing → empty only | `test_float_missing_prefills_empty_only` |
| Empty ≥ full → none | `test_empty_not_below_full_gives_none` |
| Saved value not overwritten | `test_a_saved_value_is_not_overwritten`, `test_one_saved_value_keeps_the_other_suggested` |
| Missing / disabled / unavailable / non-numeric → no prefill, no error | Task 1 settings tests, `test_an_unreadable_setting_leaves_the_field_empty_without_an_error` |
| Both owner paths (Detected sources, Battery SOC source → Counter estimator) | Task 2 first two tests |
| Provenance EN + PL, form and Preview | DD-7, DD-8; `test_prefill_text_live_site_en_pl`, `test_polish_texts`, Task 3 |
| Doc sweep EN + PL | §1.7; Task 2 and Task 3 files; `tools/build_guides.py --check` |
| Full suite + ruff + builders `--check` in `ha-energy-compass-test:2026.9.1` | the full gate of every task |
