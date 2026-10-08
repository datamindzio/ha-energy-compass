import json
import re
from pathlib import Path

from custom_components.energy_compass import detect
from custom_components.energy_compass.settings import EXPERT_GROUPS

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "energy_compass"
GUIDE_EN = (ROOT / "docs" / "guide.en.md").read_text(encoding="utf-8")
GUIDE_PL = (ROOT / "docs" / "guide.pl.md").read_text(encoding="utf-8")
SECTIONS = {
    "en": {
        "detected": "### Detected sources (new installations)",
        "forecast": "### Energy dashboard solar forecast",
        "expert": "### Expert settings",
        "guide": GUIDE_EN,
    },
    "pl": {
        "detected": "### Wykryte źródła (nowe instalacje)",
        "forecast": "### Prognoza PV z panelu Energia",
        "expert": "### Ustawienia eksperckie",
        "guide": GUIDE_PL,
    },
}


def section(guide, heading):
    start = guide.index(heading)
    end = re.search(r"^#{2,3} ", guide[start + len(heading) :], re.MULTILINE)
    return guide[start : start + len(heading) + end.start()]


def translation_keys():
    keys = set()
    for signal in detect.SIGNALS:
        if signal.translation_key:
            keys.add(signal.translation_key)
    return keys | {"total_kwh_forecast_tomorrow", "rce_pse_tomorrow_price"}


def test_detected_sources_sections_name_the_catalogue():
    for language, spec in SECTIONS.items():
        text = section(spec["guide"], spec["detected"])
        for row in detect.ROWS:
            assert f"`{row}`" in text, (language, row)
        for platform in detect.PROVIDER_PLATFORMS:
            assert f"`{platform}`" in text, (language, platform)
        for key in translation_keys():
            assert f"`{key}`" in text, (language, key)
        assert "`solar_forecast`" in text
        assert "`BMS SOC`" in text
        for age in sorted({int(value) for value in detect.MAX_AGE.values()}):
            assert str(age) in text, (language, age)


def test_solar_forecast_sections_name_the_mode():
    for spec in SECTIONS.values():
        text = section(spec["guide"], spec["forecast"])
        assert "`solar_forecast`" in text
        assert "`wh_hours`" in text
        assert "`key_estimate`" in text
        assert "49" in text


def test_expert_sections_name_every_expert_label():
    labels = {
        "en": json.loads((COMPONENT / "strings.json").read_text()),
        "pl": json.loads((COMPONENT / "translations" / "pl.json").read_text()),
    }
    for language, spec in SECTIONS.items():
        text = re.sub(
            r"\s+", " ", section(spec["guide"], spec["expert"]).replace("**", "")
        )
        options = labels[language]["config"]["step"]["menu"]["menu_options"]
        for group in (*EXPERT_GROUPS, "helpers"):
            assert options[group] in text, (language, group)
        assert options["show_expert"] in text
        assert options["hide_expert"] in text
        assert "`show_expert`" in text


def test_setup_profiles_sections_state_revision_three():
    assert "revision 3 of the setup profiles" in GUIDE_EN
    assert "Revision 3 adds one rule" in GUIDE_EN
    assert "To wersja 3" in GUIDE_PL
    assert "Wersja 3 dodaje do tego jedną regułę" in GUIDE_PL


def test_installation_and_readmes_link_the_new_sections():
    installation = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    for anchor in (
        "guide.en.md#detected-sources-new-installations",
        "guide.en.md#energy-dashboard-solar-forecast",
        "guide.en.md#expert-settings",
    ):
        assert anchor in installation
    assert "**Back to the main menu**" in installation
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_pl = (ROOT / "README.pl.md").read_text(encoding="utf-8")
    assert "guide.en.md#detected-sources-new-installations" in readme
    assert "guide.pl.md#wykryte-źródła-nowe-instalacje" in readme_pl


def test_source_requirements_document_automatic_detection():
    text = (ROOT / "docs" / "source-requirements.md").read_text(encoding="utf-8")
    start = text.index("## Automatic detection")
    body = text[start:]
    for platform in detect.PROVIDER_PLATFORMS:
        assert f"`{platform}`" in body
    for age in sorted({int(value) for value in detect.MAX_AGE.values()}):
        assert str(age) in body
    assert "PV: Energy dashboard solar forecast" in text
    contracts = (ROOT / "docs" / "source-contracts.md").read_text(encoding="utf-8")
    for fact in ("`BMS SOC`", "`price_unit`", "`wh_hours`"):
        assert fact in contracts
    helper = (ROOT / "docs" / "tariff-helper.md").read_text(encoding="utf-8")
    assert "sell multiplier 1 automatically" in helper
