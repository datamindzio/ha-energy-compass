"""Reason codes and texts of the native Deye controller."""

import re
import string

import pytest
from deye_oracle_v0137 import GOLDEN

from custom_components.energy_compass import controller as core
from custom_components.energy_compass import controller_texts as texts

# Texts of this release that the blueprint never had.
NEW = {"layout_mismatch", "paused", "plan_malformed"}
CODES = (*texts.REASONS, *texts.STOPS, *texts.WARNINGS)


def squash(value):
    return " ".join(value.split())


def pieces(template):
    """The literal parts of a text around its `{placeholder}`s."""
    return [part for part, *_ in string.Formatter().parse(template) if part]


def test_the_reason_codes_are_the_documented_ones():
    assert texts.REASONS == (
        "ok",
        "telemetry_stale",
        "tou_times",
        "session_wait",
        "plan_revoked",
        "plan_sources",
        "battery_params",
        "plan_expired",
        "operation_mode",
        "plan_malformed",
        "voltage_range",
        "mode_not_commissioned",
        "settings_range",
        "takeover_blocked",
        "layout_mismatch",
        "paused",
        "verification_required",
        "write_failed",
        "simulation_blocked",
    )


@pytest.mark.parametrize("language", ["pl", "en"])
def test_every_code_has_a_text_and_nothing_else_does(language):
    assert set(texts.TEXTS[language]) == set(CODES)
    assert len(set(CODES)) == len(CODES)


def test_both_languages_use_the_same_placeholders():
    for code in CODES:
        fields = {
            language: {
                name
                for _, name, *_ in string.Formatter().parse(texts.TEXTS[language][code])
                if name
            }
            for language in ("pl", "en")
        }
        assert fields["pl"] == fields["en"], code


@pytest.mark.parametrize("code", sorted(set(CODES) - NEW))
def test_polish_texts_are_the_blueprint_strings_byte_for_byte(code):
    blueprint = squash(GOLDEN.read_text())
    for piece in pieces(texts.PL[code]):
        assert squash(piece) in blueprint, (code, piece)


def test_no_blueprint_reason_is_missing_from_the_table():
    """Every reason literal the blueprint assigns is (a part of) a Polish text."""
    blueprint = squash(GOLDEN.read_text())
    found = set(re.findall(r"reason=''?([^'=]+?)''?[ ,)}]", blueprint))
    found |= set(re.findall(r"reason=''?([^'=]+?)''? %", blueprint))
    assert len(found) >= 8
    table = [squash(value) for value in texts.PL.values()]
    for reason in found - {"ok"}:
        assert any(squash(reason) in value for value in table), reason


def test_language_selection_and_arguments():
    args = {"operation": "Voltage", "modes": "Capacity, Voltage"}
    assert texts.text("mode_not_commissioned", "pl", args).startswith(
        "bateria: tryb Voltage niezatwierdzony; przywróć Capacity, Voltage lub"
    )
    assert texts.text("mode_not_commissioned", "en", args).startswith(
        "battery: mode Voltage not commissioned; restore Capacity, Voltage or"
    )
    assert texts.text("ok", "de") == "ok"
    assert texts.text("telemetry_stale", "de") == texts.EN["telemetry_stale"]
    assert texts.text("", "pl") == ""


def test_reason_code_is_a_runtime_key():
    assert "reason_code" in core.RUNTIME_KEYS
    runtime = {"code": "blocked", "reason_code": "tou_times", "reason": "x"}
    assert core.validate_runtime(runtime) == runtime
