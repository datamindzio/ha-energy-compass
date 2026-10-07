"""The docs YAML builder: current, and filled output equals the generators' output."""

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "build_builder", ROOT / "tools/build_builder.py"
)
builder = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(builder)

VALUES = {
    "plan": "sensor.home_plan",
    "valid": "binary_sensor.home_forecast_valid",
    "consumer": "sensor.home_consumer_compass",
    "soc": "sensor.battery_soc",
    "load": "sensor.load_power",
    "grid": "sensor.grid_power",
    "pv": "sensor.pv_power",
    "alert": "binary_sensor.home_alert",
    "optimizer": "sensor.home_optimizer_status",
    "machine": "sensor.home_energy_compass",
    "consumer_cost": "sensor.home_consumption_cost",
    "next_change": "sensor.home_next_change",
    "battery_power": "sensor.battery_power",
    "battery_state": "sensor.battery_state",
    "charge_limit": "number.battery_max_charging_current",
    "discharge_limit": "number.battery_max_discharging_current",
    "grid_limit": "number.battery_grid_charging_current",
    "tariff_group": "G12",
    "base_rate": "1.25",
    "off_peak_rate": "0.61",
    "afternoon_window": "seasonal",
    "meter_clock": "winter",
    "multiplier": "1.23",
    "floor": "-10",
    "import_cost": "sensor.import_cost",
    "import_energy": "sensor.import_energy",
    "export_energy": "sensor.export_energy",
    "import_price": "sensor.buy_price",
    "export_prices": "sensor.sell_prices",
    "deposit": "sensor.export_value",
    "capacity": "12.5",
    "prefix": "inverter_2_program_",
}


def test_committed_builder_matches_generator():
    stale = [
        path.relative_to(ROOT)
        for path, text in builder.outputs().items()
        if not path.exists() or path.read_text() != text
    ]
    assert not stale, "run python tools/build_builder.py"


@pytest.mark.parametrize("lang", ["en", "pl"])
@pytest.mark.parametrize("section", list(builder.SECTIONS))
def test_filled_template_equals_generator_output(section, lang):
    template = builder.templates()[section][lang]
    values = {role: VALUES[role] for role in template["fields"]}
    filled = builder.fill(template["yaml"], values)
    assert not re.search(r"__EC_[A-Z]+__", filled)
    expected = builder.section_text(section, lang, VALUES | {"capacity": 12.5})
    assert yaml.safe_load(filled) == yaml.safe_load(expected)


@pytest.mark.parametrize(
    ("role", "value"),
    [
        ("plan", "sensor.x'); alert(1); ('"),
        ("plan", "Sensor.Plan"),
        ("capacity", "25; x"),
        ("capacity", "-1"),
        ("prefix", "inverter deye"),
        ("tariff_group", "G13"),
        ("afternoon_window", "summer"),
        ("meter_clock", "summer"),
        ("base_rate", "1,25"),
        ("floor", "0; x"),
    ],
)
def test_fill_rejects_unsafe_values(role, value):
    with pytest.raises(ValueError):
        builder.fill("", {role: value})


def test_docs_link_the_builder():
    for doc in ["docs/installation.md", "docs/guide.en.md", "docs/guide.pl.md"]:
        assert "builder.html" in (ROOT / doc).read_text(), doc


def test_tariff_and_rce_templates_keep_the_examples_otherwise_unchanged():
    for name, (path, lines) in builder.EXAMPLES.items():
        original = (builder.ROOT / path).read_text(encoding="utf-8")
        template = builder.templates()[name]["en"]["yaml"]
        changed = [
            (a, b)
            for a, b in zip(original.splitlines(), template.splitlines(), strict=True)
            if a != b
        ]
        assert len(changed) == len(lines)
