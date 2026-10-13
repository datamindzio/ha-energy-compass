"""The guides and the translations must describe the native Deye controller."""

import json
from pathlib import Path

import pytest

from custom_components.energy_compass import controller_hardware as hardware
from custom_components.energy_compass import controller_texts as texts
from custom_components.energy_compass.controller_setup import LIMIT_FIELDS

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/energy_compass"
GUIDES = [ROOT / "docs/guide.en.md", ROOT / "docs/guide.pl.md"]
BLUEPRINT = ROOT / "blueprints/automation/energy_compass/deye_solarman_controller.yaml"


@pytest.mark.parametrize("guide", GUIDES, ids=lambda path: path.name)
def test_every_reason_code_and_the_error_code_are_documented(guide):
    text = guide.read_text()
    for code in (*texts.REASONS, "error", "controller_unavailable"):
        assert f"`{code}`" in text, code


@pytest.mark.parametrize("guide", GUIDES, ids=lambda path: path.name)
def test_the_supported_hardware_table_names_the_verified_pair(guide):
    text = guide.read_text()
    for device_type, protocol in hardware.VERIFIED:
        assert device_type in text
        assert f"| {protocol} " in text or f"{protocol} (" in text
    assert "SG0*HP3" in text
    assert "48 V" in text


@pytest.mark.parametrize("guide", GUIDES, ids=lambda path: path.name)
def test_the_limits_are_documented(guide):
    text = guide.read_text()
    for field in LIMIT_FIELDS:
        assert f"`{field}`" in text, field


@pytest.mark.parametrize("guide", GUIDES, ids=lambda path: path.name)
def test_the_new_repair_issues_and_the_pause_switch_are_documented(guide):
    text = guide.read_text()
    assert "pause_inverter_writes" in text
    assert "blueprint_active" in text
    assert "energy_compass.controller_runtime" in text


def test_the_installation_guide_leads_with_the_native_path():
    text = (ROOT / "docs/installation.md").read_text()
    section = text[text.index("## Deye inverter controller") :]
    assert section.index("native") < section.index("### Blueprint (deprecated)")
    assert "Supported hardware" in section


def test_the_blueprint_says_it_is_deprecated():
    assert "Deprecated" in BLUEPRINT.read_text()


def test_english_translations_are_level_with_the_strings():
    strings = json.loads((COMPONENT / "strings.json").read_text())
    english = json.loads((COMPONENT / "translations/en.json").read_text())
    assert english == strings
