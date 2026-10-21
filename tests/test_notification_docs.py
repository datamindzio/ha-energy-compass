"""Fail when the native window notifications drift from the docs.

The guides are mirrors (AGENTS.md, Documentation sweep): the native sender's
settings, test action, repair issues and the migration from the deprecated
blueprint must be named in both, and no document may still say that the
integration sends nothing.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE_EN = ROOT / "docs/guide.en.md"
GUIDE_PL = ROOT / "docs/guide.pl.md"
GUIDES = [GUIDE_EN, GUIDE_PL]
INSTALLATION = ROOT / "docs/installation.md"
INDEX = ROOT / "docs/index.html"
READMES = [ROOT / "README.md", ROOT / "README.pl.md"]

TERMS = [
    "notify_services",
    "notify_entities",
    "notify_language",
    "send_test_notification",
    "notify_sender",
    "notify_send_failed",
    "notify_blueprint_deprecated",
    "notify_blueprint_suppressed",
]
CLAIMS = [
    "sends no messages",
    "never sends",
    "Sam nie wysyła",
    "niczego nie wysyła",
    "Sama nie wysyła",
    "does not execute actions",
    "Integracja sama nie wykonuje akcji",
    "Energy Compass itself never sends",
]


def _slugs(text: str) -> set[str]:
    """GitHub heading anchors of a Markdown file (fences skipped)."""
    slugs: set[str] = set()
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        match = None if fenced else re.match(r"#{1,6}\s+(.*)", line)
        if match:
            title = re.sub(r"[`*_]", "", match.group(1)).strip().lower()
            title = re.sub(r"[^\w\- ]", "", title).replace(" ", "-")
            slugs.add(title)
    return slugs


@pytest.mark.parametrize("guide", GUIDES, ids=lambda path: path.name)
@pytest.mark.parametrize("term", TERMS)
def test_native_notification_terms_in_both_guides(guide, term):
    assert term in guide.read_text(), f"{guide.name} does not mention `{term}`"


@pytest.mark.parametrize(
    ("guide", "anchor", "once_more"),
    [
        (GUIDE_EN, "migrating-from-the-blueprint", "announced once more"),
        (GUIDE_PL, "migracja-z-blueprintu", "ogłoszone jeszcze raz"),
    ],
    ids=["en", "pl"],
)
def test_migration_section_in_both_guides(guide, anchor, once_more):
    text = guide.read_text()
    assert anchor in _slugs(text), f"{guide.name} has no section #{anchor}"
    title = (
        "Migrating from the blueprint" if guide == GUIDE_EN else "Migracja z blueprintu"
    )
    section = text.split(f"\n### {title}\n")[1]
    section = section.split("\n## ")[0].split("\n### ")[0]
    assert once_more in section, "the migration must say a running window repeats once"
    assert "send_test_notification" in section


@pytest.mark.parametrize("guide", GUIDES, ids=lambda path: path.name)
def test_blueprint_diagram_and_inputs_stay_in_a_deprecated_subsection(guide):
    text = guide.read_text()
    heading = (
        "### Blueprint (deprecated)"
        if guide == GUIDE_EN
        else "### Blueprint (wycofywany)"
    )
    assert heading in text
    deprecated = text.split(heading)[1].split("\n## ")[0]
    assert "flowchart TD" in deprecated
    assert "`use_blueprint_overrides`" in deprecated


def test_no_sends_no_messages_claim():
    paths = [*READMES, *GUIDES, INSTALLATION, INDEX]
    for path in paths:
        text = path.read_text()
        for claim in CLAIMS:
            assert claim not in text, f"{path.name} still says {claim!r}"


@pytest.mark.parametrize("path", [*READMES, *GUIDES, INDEX], ids=lambda p: p.name)
def test_turn_them_on_statement(path):
    text = path.read_text()
    polish = path.name in ("README.pl.md", "guide.pl.md")
    wording = "tylko po ich włączeniu" if polish else "only when you turn them on"
    assert wording in text, f"{path.name} lacks {wording!r}"


def test_installation_names_the_test_action():
    text = INSTALLATION.read_text()
    section = text.split("## Opt-in notifications")[1].split("\n## ")[0]
    assert "energy_compass.send_test_notification" in section
    assert "Blueprint (deprecated)" in section
    assert "migrating-from-the-blueprint" in section


def test_preferences_table_documents_sender_and_the_new_enabled_meaning():
    for guide in GUIDES:
        text = guide.read_text()
        assert "`notification_preferences`" in text
        assert '"integration"' in text and '"blueprint"' in text and '"off"' in text
    assert "notify_sender" in INDEX.read_text()
