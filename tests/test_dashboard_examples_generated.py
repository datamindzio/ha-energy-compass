"""The generated dashboard examples: current, placeholder-only, valid templates."""

import importlib.util
import re
from pathlib import Path

import pytest
from homeassistant.helpers.template import Template
from homeassistant.util import yaml as yaml_util

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "dashboards_build", ROOT / "tools/dashboards/build.py"
)
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
EXAMPLES = sorted((ROOT / "examples/dashboards").glob("*.yaml"))


def test_committed_examples_match_generator():
    stale = [
        path.relative_to(ROOT)
        for path, text in build.outputs().items()
        if not path.exists() or path.read_text() != text
    ]
    assert not stale, "run python tools/dashboards/build.py"
    assert sorted(build.outputs()) == EXAMPLES


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
def test_examples_hold_no_installation_entities(path):
    text = path.read_text()
    for private in ["home_pilot", "inverter_deye", "pl-PL"]:
        assert private not in text


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
async def test_markdown_cards_render(hass, path):
    section = yaml_util.load_yaml(path)
    cards = []

    def collect(value):
        if isinstance(value, dict):
            if value.get("type") == "markdown":
                cards.append(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(section)
    for card in cards:
        assert Template(card["content"], hass).async_render(parse_result=False)


@pytest.mark.parametrize("lang", list(build.LABELS))
def test_every_section_builds_in_every_language(lang):
    for name, section in build.SECTIONS.items():
        assert section(build.ENTITIES, lang)["cards"], name


ROLE_IDS = {
    "deye_controller": "sensor.energy_compass_deye_controller",
    "deye_mode": "select.energy_compass_deye_mode",
    "deye_runtime": "sensor.energy_compass_deye_controller_runtime",
}
CONTROLLER_EXAMPLES = [
    ROOT / "examples/dashboards/controller_panel.yaml",
    ROOT / "examples/dashboards/controller_diagnostics.yaml",
]


def test_the_controller_roles_default_to_the_integration_entities():
    assert {role: build.ENTITIES[role] for role in ROLE_IDS} == ROLE_IDS


@pytest.mark.parametrize("path", CONTROLLER_EXAMPLES, ids=lambda path: path.name)
def test_controller_examples_use_only_the_three_roles(path):
    text = path.read_text()
    found = set(re.findall(r"[a-z_]+\.energy_compass_deye_[a-z_]+", text))
    assert found <= set(ROLE_IDS.values())
    for retired in (
        "input_select.",
        "input_boolean.energy_compass",
        "energy_compass_deye_tou_settings",
        "energy_compass_deye_next_tou",
        "energy_compass_deye_session",
    ):
        assert retired not in text


def test_controller_attributes_replace_the_package_helpers():
    panel = (ROOT / "examples/dashboards/controller_panel.yaml").read_text()
    diagnostics = (ROOT / "examples/dashboards/controller_diagnostics.yaml").read_text()
    assert "''program_prefix''" in panel
    assert "''session''" in diagnostics and "''restore_pending''" in diagnostics
    assert "attribute: next_tou" in diagnostics


def test_a_mode_entity_override_retargets_every_use():
    entities = build.ENTITIES | {"deye_mode": "select.x_tryb"}
    for name in ("panel", "diagnostics"):
        text = build.dump(build.SECTIONS[name](entities, "pl"))
        assert "select.x_tryb" in text or name == "diagnostics"
        assert "select.energy_compass_deye_mode" not in text
    panel = build.dump(build.SECTIONS["panel"](entities, "en"))
    assert panel.count("select.x_tryb") >= 5


def test_the_generators_hold_no_package_entity_ids():
    for path in (
        ROOT / "tools/dashboards/build.py",
        ROOT / "tools/build_builder.py",
    ):
        lines = [
            line
            for line in path.read_text().splitlines()
            if "energy_compass_deye_" in line
        ]
        assert all(
            any(role_id in line for role_id in ROLE_IDS.values()) for line in lines
        ), (path, lines)
        assert all(
            "input_" not in line and "tou_settings" not in line for line in lines
        )
