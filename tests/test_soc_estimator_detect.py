"""Detection offers the counter SOC estimator next to the BMS row."""

import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from custom_components.energy_compass.config_models import (
    SourceConfig,
    validate_sources,
)
from custom_components.energy_compass.detect import (
    apply_detection,
    detect,
    detection_text,
    option_label,
    row_label,
)
from custom_components.energy_compass.settings import default_configuration

sys.path.insert(0, str(Path(__file__).parent / "golden"))

import detect_site as site

ESTIMATOR = {
    "sensor.fx_inv_battery_voltage": ("battery_voltage", "520.0", "V"),
    "sensor.fx_inv_battery_current": ("battery_current", "1.2", "A"),
}
COUNTERS = [
    "sensor.fx_inv_batt_charge_total",
    "sensor.fx_inv_batt_discharge_total",
]


def snapshot(**changes):
    snap = site.site_snapshot()
    entities = dict(snap.entities)
    for entity_id, (key, state, unit) in ESTIMATOR.items():
        entities[entity_id] = site._solarman(entity_id, key, state, unit)
    entities.update(changes)
    return replace(snap, entities=entities)


def offer(detection, row):
    return next((item for item in detection.offers if item.row == row), None)


def test_the_estimator_is_offered_unticked_with_all_four_sources():
    detection = detect(snapshot(), site.site_context())
    found = offer(detection, "soc_estimator")
    assert found is not None
    assert found.default is None
    assert [t.entity_id for t in found.options[0].targets[:4]] == [
        *COUNTERS,
        *ESTIMATOR,
    ]


def test_a_missing_source_withholds_the_offer():
    snap = snapshot()
    entities = {
        k: v for k, v in snap.entities.items() if k != "sensor.fx_inv_battery_current"
    }
    detection = detect(replace(snap, entities=entities), site.site_context())
    assert offer(detection, "soc_estimator") is None


def test_an_unusable_unit_is_reported_not_offered():
    snap = snapshot()
    bad = site._solarman(
        "sensor.fx_inv_battery_voltage", "battery_voltage", "520", "mV"
    )
    detection = detect(
        replace(snap, entities={**snap.entities, bad.entity_id: bad}),
        site.site_context(),
    )
    assert offer(detection, "soc_estimator") is None
    assert any(note.row == "soc_estimator" for note in detection.unusable)


def test_no_offer_without_a_battery():
    detection = detect(snapshot(), site.site_context(battery_enabled=False))
    assert offer(detection, "soc_estimator") is None


def test_choosing_it_replaces_the_soc_sources_and_keeps_the_bms_for_comparison():
    detection = detect(snapshot(), site.site_context())
    chosen = [
        offer(detection, "soc").options[offer(detection, "soc").default],
        offer(detection, "bms_soc").options[0],
        offer(detection, "soc_estimator").options[0],
    ]
    draft = default_configuration("PLN", "Europe/Warsaw")
    draft["sources"]["battery_enabled"] = True
    result = apply_detection(draft, chosen)
    assert result["sources"]["soc"] is None
    assert result["sources"]["bms_soc"] is None
    assert result["sources"]["soc_mode"] == "counter_estimator"
    bindings = result["soc_estimator"]["bindings"]
    assert bindings["charge_counter"]["entity_id"] == COUNTERS[0]
    assert bindings["bms_soc"]["attribute"] == "BMS SOC"
    validate_sources(SourceConfig.from_dict(result["sources"]), set())


def test_the_texts_name_the_offer_in_both_languages():
    detection = detect(snapshot(), site.site_context())
    option = offer(detection, "soc_estimator").options[0]
    assert row_label("soc_estimator", "en").startswith("Counter estimator")
    assert row_label("soc_estimator", "pl").startswith("Estymator z liczników")
    assert "sensor.fx_inv_battery_voltage" in option_label(option, "en")
    assert "napięcie" in option_label(option, "pl")
    _, notes = detection_text(detection, "en")
    assert "pack voltage that is 0 %" in notes
    assert "BMS SOC is offered unticked." in notes


def test_the_default_configuration_is_untouched_by_the_new_mode():
    config = default_configuration("PLN", "UTC")
    assert "soc_mode" not in config["sources"]
    assert (
        deepcopy(config)["sources"]
        == SourceConfig.from_dict(config["sources"]).to_dict()
    )


@pytest.mark.parametrize("mode", ["entity", "counter_estimator"])
def test_the_mode_round_trips_through_the_source_config(mode):
    sources = default_configuration("PLN", "UTC")["sources"]
    sources["soc_mode"] = mode
    assert SourceConfig.from_dict(sources).soc_mode == mode
