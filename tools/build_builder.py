"""Build the static YAML builder page: docs/builder.html + docs/assets/builder/templates.js.

The dashboard generator stays the only source. This script renders its output
once per language with placeholder tokens (`__EC_PLAN__`, ...);
docs/assets/builder/builder.js only substitutes validated values for the tokens,
so the page holds no generator logic of its own.

Usage: python tools/build_builder.py [--check]
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
TEMPLATES = DOCS / "assets/builder/templates.js"
PAGE = DOCS / "builder.html"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dashboards = _load("dashboards_build", "tools/dashboards/build.py")

# Validation patterns shared with builder.js; values matching them are safe to
# splice into YAML plain scalars, JavaScript strings and Jinja literals.
PATTERNS = {
    "entity": r"^[a-z_]+\.[a-z0-9_]+$",
    "number": r"^[0-9]+(\.[0-9]+)?$",
    "signed": r"^-?[0-9]+(\.[0-9]+)?$",
    "tariff_group": r"^(G11|G12|G12w)$",
    "afternoon_window": r"^(fixed|seasonal)$",
    "meter_clock": r"^(winter|local)$",
}
# Choice kinds are shown as a select; values must match their pattern.
OPTIONS = {
    "tariff_group": ["G11", "G12", "G12w"],
    "afternoon_window": ["fixed", "seasonal"],
    "meter_clock": ["local", "winter"],
}
# Hand-written examples whose settings the builder fills: role -> exact line.
EXAMPLES = {
    "tariff": (
        "examples/tariff-helper.yaml",
        {
            "tariff_group": "      tariff_group: {}",
            "base_rate": "      base_rate: {}",
            "off_peak_rate": "      off_peak_rate: {}",
            "afternoon_window": "      afternoon_window: {}",
            "meter_clock": "      meter_clock: {}",
        },
    ),
    "rce": (
        "examples/rce-sell-price.yaml",
        {"multiplier": "      multiplier: {}", "floor": "      floor: {}"},
    ),
}
FIELDS = {
    "plan": ("entity", "sensor.energy_compass_plan", "Plan", "Plan"),
    "valid": (
        "entity",
        "binary_sensor.energy_compass_forecast_valid",
        "Forecast valid",
        "Poprawna prognoza",
    ),
    "consumer": (
        "entity",
        "sensor.energy_compass_consumer_compass",
        "Consumer compass",
        "Kompas zużycia",
    ),
    "soc": (
        "entity",
        "sensor.inverter_deye_battery",
        "Battery SOC (%)",
        "SOC baterii (%)",
    ),
    "load": (
        "entity",
        "sensor.inverter_deye_load_power",
        "Load power (W)",
        "Moc obciążenia (W)",
    ),
    "grid": (
        "entity",
        "sensor.inverter_deye_grid_power",
        "Grid power (W, + import)",
        "Moc sieci (W, + import)",
    ),
    "pv": ("entity", "sensor.inverter_deye_pv_power", "PV power (W)", "Moc PV (W)"),
    "alert": ("entity", "binary_sensor.energy_compass_alert", "Alert", "Alert"),
    "optimizer": (
        "entity",
        "sensor.energy_compass_optimizer_status",
        "Optimizer status",
        "Stan optymalizatora",
    ),
    "machine": (
        "entity",
        "sensor.energy_compass_energy_compass",
        "Energy compass (planned battery state)",
        "Kompas energii (planowany stan baterii)",
    ),
    "consumer_cost": (
        "entity",
        "sensor.energy_compass_consumption_cost",
        "Consumption cost",
        "Koszt zużycia",
    ),
    "next_change": (
        "entity",
        "sensor.energy_compass_next_change",
        "Next change",
        "Następna zmiana",
    ),
    "battery_power": (
        "entity",
        "sensor.inverter_deye_battery_power",
        "Battery power (W, + discharge)",
        "Moc baterii (W, + rozładowanie)",
    ),
    "battery_state": (
        "entity",
        "sensor.inverter_deye_battery_state",
        "Battery state (inverter)",
        "Stan baterii (falownik)",
    ),
    "charge_limit": (
        "entity",
        "number.inverter_deye_battery_max_charging_current",
        "Battery max charging current",
        "Maks. prąd ładowania baterii",
    ),
    "discharge_limit": (
        "entity",
        "number.inverter_deye_battery_max_discharging_current",
        "Battery max discharging current",
        "Maks. prąd rozładowania baterii",
    ),
    "grid_limit": (
        "entity",
        "number.inverter_deye_battery_grid_charging_current",
        "Battery grid charging current",
        "Prąd ładowania baterii z sieci",
    ),
    "tariff_group": ("tariff_group", "G12", "Tariff group", "Grupa taryfowa"),
    "base_rate": (
        "number",
        "1.25",
        "Peak (or G11) final price per kWh",
        "Cena szczytowa (lub G11) za kWh, brutto",
    ),
    "off_peak_rate": (
        "number",
        "0.61",
        "Off-peak final price per kWh",
        "Cena pozaszczytowa za kWh, brutto",
    ),
    "afternoon_window": (
        "afternoon_window",
        "fixed",
        "Afternoon off-peak window: fixed 13-15 or seasonal (15-17 Apr-Sep)",
        "Popołudniowe okno taniej strefy: stałe 13-15 lub sezonowe (15-17 IV-IX)",
    ),
    "meter_clock": (
        "meter_clock",
        "local",
        "Meter clock: local (wall clock) or winter (old meter fixed on winter time)",
        "Zegar licznika: lokalny (czas ścienny) lub zimowy (stary licznik na czasie zimowym)",
    ),
    "multiplier": (
        "number",
        "1.23",
        "Sell price multiplier (net-billing deposit 1.23)",
        "Mnożnik ceny sprzedaży (depozyt net-billing 1,23)",
    ),
    "floor": (
        "signed",
        "0",
        "Sell price floor in PLN/MWh (net-billing 0)",
        "Minimalna cena sprzedaży w PLN/MWh (net-billing 0)",
    ),
    "import_cost": (
        "entity",
        "sensor.inverter_total_energy_import_cost",
        "Grid import cost (Energy dashboard cost sensor)",
        "Koszt importu z sieci (sensor kosztu z panelu Energia)",
    ),
    "import_energy": (
        "entity",
        "sensor.inverter_total_energy_import",
        "Grid import energy (kWh, total)",
        "Energia pobrana z sieci (kWh, licznik)",
    ),
    "export_energy": (
        "entity",
        "sensor.inverter_total_energy_export",
        "Grid export energy (kWh, total)",
        "Energia oddana do sieci (kWh, licznik)",
    ),
    "import_price": (
        "entity",
        "sensor.energy_compass_tariff_price",
        "Current buy price (for gaps after a restart)",
        "Bieżąca cena zakupu (do luk po restarcie)",
    ),
    "export_prices": (
        "entity",
        "sensor.energy_compass_rce_export_forecast",
        "Export price forecast (prices attribute)",
        "Prognoza ceny sprzedaży (atrybut prices)",
    ),
    "deposit": (
        "entity",
        "sensor.export_value",
        "Export value sensor (from the counter)",
        "Sensor wartości eksportu (z licznika)",
    ),
    "capacity": (
        "number",
        "25",
        "Battery capacity used by Energy Compass (kWh)",
        "Pojemność baterii w Energy Compass (kWh)",
    ),
    "deye_controller": (
        "entity",
        "sensor.energy_compass_deye_controller",
        "Deye controller sensor (Options → Deye controller)",
        "Sensor sterownika Deye (Opcje → Sterownik Deye)",
    ),
    "deye_mode": (
        "entity",
        "select.energy_compass_deye_mode",
        "Deye mode select",
        "Wybór trybu sterownika Deye",
    ),
    "deye_runtime": (
        "entity",
        "sensor.energy_compass_deye_controller_runtime",
        "Deye controller runtime sensor",
        "Sensor stanu pracy sterownika Deye",
    ),
}
SECTIONS = {
    "plan": ("Plan chart (ApexCharts)", "Wykres planu (ApexCharts)"),
    "consumer": (
        "Consumer Compass timeline (ApexCharts)",
        "Oś czasu Consumer Compass (ApexCharts)",
    ),
    "panel": ("Deye controller panel", "Panel sterownika Deye"),
    "diagnostics": ("Deye controller diagnostics", "Diagnostyka sterownika Deye"),
    "cost": (
        "Cost card (purchase, deposit, balance)",
        "Karta kosztów (zakup, depozyt, bilans)",
    ),
    "tariff": ("Buy price: G11/G12/G12w tariff", "Cena zakupu: taryfa G11/G12/G12w"),
    "rce": ("Sell price: RCE (net-billing)", "Cena sprzedaży: RCE (net-billing)"),
}


def token(role):
    return f"__EC_{role.upper()}__"


def dump(value, dumper=yaml.SafeDumper):
    return yaml.dump(
        value, Dumper=dumper, sort_keys=False, allow_unicode=True, width=4096
    )


def section_text(name, lang, values):
    """The YAML for one section; `values` maps roles to IDs, numbers or tokens."""
    if name in EXAMPLES:
        path, lines = EXAMPLES[name]
        text = (ROOT / path).read_text(encoding="utf-8")
        for role, line in lines.items():
            default = re.search(
                "^" + re.escape(line.format("")) + "(.*)$", text, re.MULTILINE
            )
            assert default, f"{path}: missing {line!r}"
            text = text.replace(
                line.format(default.group(1)), line.format(values[role]), 1
            )
        return text
    entities = {role: values[role] for role in dashboards.ENTITIES}
    kwargs = {"capacity": values["capacity"]} if name in ("plan", "panel") else {}
    return dump(dashboards.SECTIONS[name](entities, lang, **kwargs))


def templates():
    tokens = {role: token(role) for role in FIELDS}
    out = {}
    for lang in dashboards.LABELS:
        for name in SECTIONS:
            text = section_text(name, lang, tokens)
            out.setdefault(name, {})[lang] = {
                "yaml": text,
                "fields": [role for role in FIELDS if token(role) in text],
            }
    return out


def data():
    return {
        "patterns": PATTERNS,
        "fields": {
            role: {
                "token": token(role),
                "kind": kind,
                "example": example,
                "label": {"en": en, "pl": pl},
            }
            | ({"options": OPTIONS[kind]} if kind in OPTIONS else {})
            for role, (kind, example, en, pl) in FIELDS.items()
        },
        "sections": {
            name: {"label": {"en": en, "pl": pl}} for name, (en, pl) in SECTIONS.items()
        },
        "templates": templates(),
    }


def fill(template, values):
    """Python twin of builder.js fill(); used by the tests."""
    for role, value in values.items():
        if not re.fullmatch(PATTERNS[FIELDS[role][0]], value):
            raise ValueError(f"{role}: {value!r}")
        template = template.replace(token(role), value)
    return template


PAGE_TEMPLATE = """<!doctype html>
<!-- Generated by tools/build_builder.py - do not edit; edit the generator and rebuild. -->
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Energy Compass YAML builder</title>
  <meta name="description" content="Build Energy Compass dashboard sections and the tariff and sell-price examples with your own entity IDs.">
  <style>__STYLE__
    .builder { max-width: 60rem; margin: 2rem auto; padding: 0 1.2rem; }
    .builder form { display: grid; gap: .8rem; margin: 1.2rem 0; }
    .builder #fields { display: grid; gap: .8rem; }
    .builder label { display: grid; gap: .25rem; font-weight: 600; }
    .builder input, .builder select { font: inherit; padding: .45rem .6rem; border: 1px solid var(--line);
      border-radius: .3rem; background: var(--surface); color: var(--ink); }
    .builder input[aria-invalid="true"] { border-color: #c62828; }
    .builder textarea { width: 100%; min-height: 24rem; font: .85rem/1.4 ui-monospace, monospace;
      padding: .8rem; border: 1px solid var(--line); border-radius: .35rem; background: var(--surface); color: var(--ink); }
    .builder .error { color: #c62828; min-height: 1.4em; }
    .builder button { font: inherit; padding: .5rem 1rem; border-radius: .3rem; border: 1px solid var(--line);
      background: var(--surface); color: var(--ink); cursor: pointer; }
  </style>
</head>
<body>
<main class="builder">
  <p><a href="index.html">Energy Compass</a> · <a href="guide.en.html">Guide</a> · <a href="guide.pl.html">Przewodnik</a></p>
  <h1>Energy Compass YAML builder</h1>
  <p id="intro"></p>
  <form id="builder" autocomplete="off">
    <label><span id="lang-label">Language</span>
      <select id="lang"><option value="en">English</option><option value="pl">Polski</option></select></label>
    <label><span id="section-label">Section</span><select id="section"></select></label>
    <div id="fields"></div>
  </form>
  <p class="error" id="error" role="alert"></p>
  <textarea id="output" readonly aria-label="YAML"></textarea>
  <p><button type="button" id="copy">Copy</button> <span id="copied"></span></p>
  <p id="note"></p>
</main>
<script src="assets/builder/templates.js"></script>
<script src="assets/builder/builder.js"></script>
</body>
</html>
"""


def page():
    # Same look as docs/index.html; build_guides.py needs `markdown`, CI does not have it.
    index = (DOCS / "index.html").read_text(encoding="utf-8")
    style = re.search(r"<style>(.*?)</style>", index, re.DOTALL).group(1).rstrip()
    return PAGE_TEMPLATE.replace("__STYLE__", style)


def outputs():
    js = (
        "// Generated by tools/build_builder.py - do not edit; edit the generators and rebuild.\n"
        "window.EC_BUILDER = "
        + json.dumps(data(), ensure_ascii=False, indent=1)
        + ";\n"
    )
    return {TEMPLATES: js, PAGE: page()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="fail if the committed files are stale"
    )
    args = parser.parse_args(argv)
    stale = [
        path
        for path, text in outputs().items()
        if not path.exists() or path.read_text() != text
    ]
    if args.check:
        for path in stale:
            print(
                f"stale: {path.relative_to(ROOT)} - run python tools/build_builder.py",
                file=sys.stderr,
            )
        return 1 if stale else 0
    for path, text in outputs().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
