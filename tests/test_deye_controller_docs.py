"""Fail when the Deye controller or its dashboard examples drift from the docs.

The guides are mirrors (AGENTS.md, Documentation sweep): every blueprint input,
controller entity key and attribute, service, plan reason, runtime code and
dashboard example must be named in both.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "blueprints/automation/energy_compass/deye_solarman_controller.yaml"
CONTROLLER = ROOT / "custom_components/energy_compass/controller.py"
CONTROLLER_HA = ROOT / "custom_components/energy_compass/controller_ha.py"
TRANSLATIONS = ROOT / "custom_components/energy_compass/strings.json"
GENERATOR = ROOT / "tools/deye_controller/build.py"
NOTIFICATIONS = ROOT / "blueprints/automation/energy_compass/notifications.yaml"
COUNTER = ROOT / "blueprints/automation/energy_compass/export_value_counter.yaml"
COST_CARD = ROOT / "cards/cost/energy-compass-cost-card.js"
COST_DOC = ROOT / "docs/cost-card.md"
GUIDES = [ROOT / "docs/guide.en.md", ROOT / "docs/guide.pl.md"]
INSTALLATION = ROOT / "docs/installation.md"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!input", lambda loader, node: loader.construct_scalar(node))


def blueprint_inputs():
    doc = yaml.load(BLUEPRINT.read_text(), Loader=_Loader)
    return sorted(
        key
        for section in doc["blueprint"]["input"].values()
        for key in section["input"]
    )


def controller_surface():
    """Entity translation keys, attribute names, services and plan reasons."""
    import json

    from custom_components.energy_compass import controller

    strings = json.loads(TRANSLATIONS.read_text())
    keys = [
        key
        for platform in ("sensor", "select")
        for key in strings["entity"][platform]
        if key.startswith("deye_")
    ]
    services = [name for name in strings["services"] if name.startswith("controller_")]
    return sorted(
        {
            *keys,
            *controller.CONTROLLER_ATTRIBUTES,
            *controller.PLAN_REASONS,
            *(f"energy_compass.{name}" for name in services),
        }
    )


def runtime_codes():
    text = GENERATOR.read_text()
    codes = set(re.findall(r'or "([a-z_]+)"', CONTROLLER_HA.read_text()))
    for expression in re.findall(r"code=([^,]{0,120})", text):
        codes |= set(re.findall(r"'([a-z_]+)'", expression))
    return sorted(codes)


def examples():
    return sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "examples/dashboards").glob("*.yaml")
    )


def test_extraction_finds_the_known_surface():
    assert {"controller_entity", "max_power_w", "old_writers"} <= set(
        blueprint_inputs()
    )
    assert not {"plan_entity", "solarman_device", "capacity_kwh", "eta"} & set(
        blueprint_inputs()
    )
    surface = controller_surface()
    assert {"deye_controller", "deye_mode", "deye_runtime"} <= set(surface)
    assert {"controller_schema", "plan_reason", "restore_pending", "tou"} <= set(
        surface
    )
    assert {
        "energy_compass.controller_runtime",
        "energy_compass.controller_import_package",
    } <= set(surface)
    assert {"ok", "session", "revoked", "sources"} <= set(surface)
    assert {"ok", "blocked", "restored", "waiting", "write_failed"} <= set(
        runtime_codes()
    )
    assert "examples/dashboards/plan_chart.yaml" in examples()


@pytest.mark.parametrize("guide", GUIDES, ids=lambda path: path.name)
@pytest.mark.parametrize(
    "identifier",
    [*blueprint_inputs(), *controller_surface(), *runtime_codes()],
)
def test_guides_document_controller_surface(guide, identifier):
    assert f"`{identifier}`" in guide.read_text(), (
        f"{guide.name} does not mention `{identifier}`; follow AGENTS.md Documentation sweep"
    )


@pytest.mark.parametrize("example", examples())
def test_installation_links_every_dashboard_example(example):
    assert f"../{example}" in INSTALLATION.read_text()


def notification_inputs():
    doc = yaml.load(NOTIFICATIONS.read_text(), Loader=_Loader)
    return sorted(doc["blueprint"]["input"])


@pytest.mark.parametrize("guide", GUIDES, ids=lambda path: path.name)
@pytest.mark.parametrize("identifier", notification_inputs())
def test_guides_document_notification_inputs(guide, identifier):
    assert f"`{identifier}`" in guide.read_text(), (
        f"{guide.name} does not mention notification input `{identifier}`; "
        "follow AGENTS.md Documentation sweep"
    )


def cost_card_keys():
    text = COST_CARD.read_text()
    keys = set(re.findall(r"(?:cfg|this\.config|config\?)\.([a-z_]+)", text)) - {
        "time_zone"
    }
    return sorted(keys)


def counter_inputs():
    doc = yaml.load(COUNTER.read_text(), Loader=_Loader)
    return sorted(doc["blueprint"]["input"])


def test_cost_card_keys_are_found():
    assert {"cost_entity", "deposit_backfill", "tariff_label"} <= set(cost_card_keys())


@pytest.mark.parametrize("doc", [*GUIDES, COST_DOC], ids=lambda path: path.name)
@pytest.mark.parametrize("identifier", [*cost_card_keys(), *counter_inputs()])
def test_docs_document_cost_card_and_counter(doc, identifier):
    assert f"`{identifier}`" in doc.read_text(), (
        f"{doc.name} does not mention `{identifier}`; follow AGENTS.md Documentation sweep"
    )
