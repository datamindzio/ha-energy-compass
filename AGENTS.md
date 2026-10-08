# AGENTS.md — Energy Compass

## Documentation sweep (required for every change)

Any change to behaviour, entities, settings, defaults, strategies, modes, reason codes, blueprints,
the Deye controller (blueprint, controller entities, options and services), dashboard examples or the release version ships with a
**doc sweep** in the same commit or PR. The docs describe the
code; when they disagree, the code is right and the docs are fixed.

1. **Map the change** to every affected doc using the table below. Search the docs for each
   changed identifier (setting key, state, reason code, strategy, default value) with
   `grep -rn '<identifier>' README*.md docs/*.md docs/index.html` and include every hit.
2. **Update English and Polish together.** `docs/guide.en.md` ↔ `docs/guide.pl.md` and
   `README.md` ↔ `README.pl.md` are mirrors: same sections, same tables, same diagrams, same
   numbers. Polish UI labels come from `custom_components/energy_compass/translations/pl.json`.
3. **Verify each claim against the code** (`engine/`, `runtime.py`, `coordinator.py`,
   `settings.py`, `translations/`, `tools/deye_controller/build.py`, `tools/dashboards/build.py`),
   not against other docs.
4. **Update diagrams** whose logic changed, then **rebuild the Pages guides**:
   `pip install -e '.[docs]'` (or `pip install markdown==3.8`), then
   `python tools/build_guides.py`. It renders every Mermaid block to light/dark SVGs in
   `docs/assets/diagrams/` (Node.js/npx required) and regenerates `docs/guide.en.html` and
   `docs/guide.pl.html`. Commit the generated files with the Markdown; edit only the `.md`.
5. **Check links**: every relative link and `#anchor` in `README*.md` and `docs/guide.*.md`
   resolves (GitHub heading slugs).
6. **On release**, bump the version stated in the intro of both guides and in `docs/index.html`
   (meta description, sidebar, intro, footer), then rebuild the guides.

Done when every grep hit is current, both languages match, `tools/build_guides.py` runs clean
and its output is committed, no link is broken, and `pytest tests/test_deye_controller_docs.py`
passes. State in the PR which docs changed, or why none needed to.

### Change → docs map

| Change | Update |
| --- | --- |
| Entity added/removed/renamed, state value, attribute, availability rule | `guide.*` (entity overview, state tables, availability diagram), `docs/index.html` (entity index + article), `README*.md` entity paragraph, `translations/*.json` + `strings.json` |
| Optimizer status, Alert code, `calculating`/`invalid_input` reason, plan retention | `guide.*` (optimizer status, Alert, retention diagrams + tables), `docs/model.md` § Plan retention, `docs/index.html` |
| Operating mode, minimum duration, safety exception, Sell only PV, grid-charge ceiling, episode hurdles, import penalty, standby loss, terminal rule | `guide.*` § Operating modes + policy table, `docs/model.md`, `README*.md` policy paragraphs |
| Strategy added/changed, strategy flag or weight, strategy default | `guide.*` § Dispatch strategies (summary table, per-strategy section, formula), `docs/model.md` § Dispatch strategies, `README*.md` § Dispatch strategy, `docs/index.html` Strategy article |
| Consumption levels, probes, percentiles, windows, flexible depth | `guide.*` § Consumption levels / Windows, `docs/model.md` § Incremental consumption, `docs/index.html` |
| Recalculation cadence, refresh, rate limit, SOC trigger, time budgets | `guide.*` § Recalculation cadence, `docs/model.md` § Recalculation cadence, `README*.md` last paragraph |
| Any setting key or default value | every doc citing it (grep the key and the old value) |
| `strategy_switch` blueprint | `guide.*` § Automatic strategy switching, `docs/installation.md` |
| Notification blueprint: input, gate, message, helper format | `guide.*` § Window notifications (diagram, messages table, inputs table), `docs/installation.md` § Opt-in notifications, `README*.md` notification paragraph |
| Deye controller: input, default, profile, plan acceptance, write/confirm rule, runtime code | `guide.*` § Deye inverter controller (inputs table, profiles table, runtime codes, diagram), `docs/installation.md` § Deye inverter controller |
| Deye controller entities, options step, services, store, `plan_reason`, session or migration | `guide.*` § Deye inverter controller → Enabling, Controller entities, Services, Plan acceptance, Migrating from the package; `docs/installation.md` § Deye inverter controller step 1; `docs/index.html` entity index and articles; `translations/*.json` + `strings.json` |
| Plan attribute or state consumed by the controller (`intervals`, `dispatch_policy`, `balance_hold`, `generated_at`, `valid_until`, `refreshing`, `plan_retained`) | the controller generator and its tests first, then `guide.*` § Deye inverter controller |
| Cost card (`cards/cost/`): config key, view, notice, string | `docs/cost-card.md` (configuration table), `guide.*` § Cost card (keys table); every user-visible string lives in the card's `TEXT.en` and `TEXT.pl` — change both; `node --test test-*.mjs` in `cards/cost/` |
| Export value counter blueprint or `tools/export_value_backfill.py` | `docs/cost-card.md` § Export value, `guide.*` § Cost card |
| Dashboard example added/changed | `docs/installation.md` § Dashboard examples, `guide.*` § Deye inverter controller → Dashboard examples; new section or input role → `FIELDS`/`SECTIONS` in `tools/build_builder.py` |
| Source requirements, units, tariffs; `examples/tariff-helper.yaml`, `examples/rce-sell-price.yaml` | `docs/source-requirements.md`, `docs/source-contracts.md`, `docs/tariff-helper.md`; a changed setting line in either example → `EXAMPLES` in `tools/build_builder.py`, then rebuild the builder |
| Home Assistant / SciPy / runtime requirement | `README*.md` § Install, `docs/runtime-validation.md` |

## Generated files (never edit by hand)

| Generated file | Source | Rebuild |
| --- | --- | --- |
| `blueprints/automation/energy_compass/deye_solarman_controller.yaml` | `tools/deye_controller/build.py` | `python tools/deye_controller/build.py` |
| `examples/dashboards/*.yaml` | `tools/dashboards/build.py` (+ `state_bands.js`) | `python tools/dashboards/build.py` |
| `docs/guide.*.html`, `docs/assets/diagrams/*.svg` | `docs/guide.*.md` | `python tools/build_guides.py` |
| `docs/builder.html`, `docs/assets/builder/templates.js` | `tools/build_builder.py` (renders both generators with `__EC_*__` tokens; style from `docs/index.html`) | `python tools/build_builder.py` |

Edit the source, rebuild, and commit source and output together. A change to either generator or to
`docs/index.html` also requires `python tools/build_builder.py`. `docs/assets/builder/builder.js` is
hand-written and must only validate and substitute tokens — never re-implement generator logic in it.
CI runs all three builders with
`--check` and fails on any difference. `tests/test_deye_controller_docs.py` fails when a Deye
controller input, controller entity key or attribute, service, `plan_reason` or runtime code, a notification blueprint input, a cost-card config
key or an export value counter input is missing from either guide (card keys and counter inputs also
from `docs/cost-card.md`), or a dashboard example is not linked from `docs/installation.md` — fix the
docs, never weaken the test. CI also runs the cost card's Node tests (`cards/cost/test-*.mjs`).

The controller behaviour is covered by `tests/test_deye_controller.py` (templates and action tree
against a sanitized state sample, with `tests/deye_harness.py` playing Energy Compass),
`tests/test_deye_controller_blueprint.py` (the blueprint loaded by Home Assistant), `tests/test_controller_*.py`
(the integration-owned state) and `tests/test_controller_parity.py` (acceptance against the frozen v0.1.35
blueprint in `tests/golden/`, which must never change). A controller change adds or updates a test there. Keep
household-specific values (device IDs, entity names of one installation) out of the repository:
they belong in blueprint inputs, not in the generator.
