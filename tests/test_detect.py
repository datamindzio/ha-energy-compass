import ast
import re
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from custom_components.energy_compass import detect
from custom_components.energy_compass.config_models import (
    SolarForecastBinding,
    SourceConfig,
    validate_sources,
)
from custom_components.energy_compass.detect import (
    Option,
    SolarForecastFacts,
    Target,
    apply_detection,
    detection_text,
    normalize_energy_prefs,
    option_label,
    row_label,
)
from custom_components.energy_compass.detect import (
    detect as run_detection,
)
from custom_components.energy_compass.settings import default_configuration

sys.path.insert(0, str(Path(__file__).parent / "golden"))

import detect_site as site

ZONE = site.ZONE


def snapshot():
    return site.site_snapshot()


def run(snap=None, **changes):
    return run_detection(snap or snapshot(), site.site_context(**changes))


def offer(detection, row):
    return next((item for item in detection.offers if item.row == row), None)


def signature(option):
    return (
        option.provider,
        option.kind,
        option.tier,
        tuple(target.entity_id for target in option.targets),
        option.counts,
        option.value,
        option.attribute,
        option.sign,
    )


def chosen(offer_item):
    return offer_item.options[offer_item.default]


def tweak(snap, entity_id, **fields):
    entities = dict(snap.entities)
    entities[entity_id] = replace(entities[entity_id], **fields)
    return replace(snap, entities=entities)


def attributes_of(snap, entity_id, **changes):
    merged = {**snap.entities[entity_id].attributes, **changes}
    return tweak(snap, entity_id, attributes=merged)


def drop(snap, *entity_ids):
    entities = {k: v for k, v in snap.entities.items() if k not in entity_ids}
    return replace(snap, entities=entities)


def add(snap, facts):
    return replace(snap, entities={**snap.entities, facts.entity_id: facts})


def prefs(snap, data):
    return replace(snap, energy=normalize_energy_prefs(data))


def edited_prefs(**edits):
    data = deepcopy(site.ENERGY_PREFS)
    for source in data["energy_sources"]:
        source.update(edits.get(source["type"], {}))
    return data


def fact(entity_id, **kwargs):
    base = {
        "platform": "fx_extra",
        "uid": f"uid-{entity_id}",
        "state": "50",
        "attributes": {"unit_of_measurement": "%"},
    }
    base.update(kwargs)
    return site._fact(entity_id, **base)


# --- purity and hygiene -------------------------------------------------------


def test_detect_module_is_pure():
    tree = ast.parse(Path(detect.__file__).read_text())
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        assert not any(name.split(".")[0] == "homeassistant" for name in names)


def test_fixture_is_synthetic():
    snap = snapshot()
    for entity_id in snap.entities:
        assert re.fullmatch(r"(sensor|number)\.fx_[a-z0-9_]+", entity_id), entity_id
    for facts in snap.entities.values():
        for value in (facts.config_entry_id, facts.device_id):
            assert value is None or re.fullmatch(r"fx[0-9a-f]{6}", value), value
        assert facts.registry_id is None or re.fullmatch(
            r"fx[0-9a-f]{6}", facts.registry_id
        )
    for device_id, device in snap.devices.items():
        assert re.fullmatch(r"fx[0-9a-f]{6}", device_id)
        assert all(re.fullmatch(r"fx[0-9a-f]{6}", e) for e in device.config_entry_ids)
    for entry_id in snap.solar_forecasts:
        assert re.fullmatch(r"fx[0-9a-f]{6}", entry_id)


# --- golden ---------------------------------------------------------------------

GOLDEN = [
    (
        "pv",
        0,
        [
            ("energy_prefs", "solar_forecast", 1, (), (50,), None, None, 1.0),
            (
                "solcast",
                "entity_forecast",
                2,
                ("sensor.fx_sc_tomorrow", "sensor.fx_sc_today"),
                (48, 48),
                None,
                None,
                1.0,
            ),
        ],
    ),
    (
        "sell",
        0,
        [
            (
                "template",
                "template_sell",
                1,
                ("sensor.fx_ec_sell",),
                (192,),
                None,
                None,
                1.0,
            ),
            (
                "rce_pse",
                "rce",
                2,
                ("sensor.fx_rce_today", "sensor.fx_rce_tomorrow"),
                (96, None),
                None,
                None,
                1.0,
            ),
        ],
    ),
    (
        "load",
        0,
        [
            (
                "solarman",
                "statistic",
                1,
                ("sensor.fx_inv_load_total",),
                (),
                None,
                None,
                1.0,
            )
        ],
    ),
    (
        "soc",
        0,
        [
            ("template", "soc", 2, ("sensor.fx_ec_soc",), (), 23.2, None, 1.0),
            ("solarman", "soc", 3, ("sensor.fx_inv_battery",), (), 1.0, None, 1.0),
        ],
    ),
    (
        "bms_soc",
        None,
        [("solarman", "soc", 1, ("sensor.fx_inv_battery",), (), 12.0, "BMS SOC", 1.0)],
    ),
    (
        "battery_power",
        0,
        [
            (
                "energy_prefs",
                "measurement",
                1,
                ("sensor.fx_inv_battery_power",),
                (),
                -890.0,
                None,
                1.0,
            )
        ],
    ),
    (
        "pv_power",
        0,
        [
            (
                "energy_prefs",
                "measurement",
                1,
                ("sensor.fx_inv_pv_power",),
                (),
                1650.0,
                None,
                1.0,
            )
        ],
    ),
    (
        "grid_import_power",
        0,
        [
            (
                "energy_prefs",
                "measurement",
                1,
                ("sensor.fx_grid_total_power",),
                (),
                15.0,
                None,
                1.0,
            ),
            (
                "solarman",
                "measurement",
                2,
                ("sensor.fx_inv_grid_power",),
                (),
                15.0,
                None,
                1.0,
            ),
        ],
    ),
    (
        "grid_export_power",
        0,
        [
            (
                "energy_prefs",
                "measurement",
                1,
                ("sensor.fx_grid_total_power",),
                (),
                15.0,
                None,
                -1.0,
            ),
            (
                "solarman",
                "measurement",
                2,
                ("sensor.fx_inv_grid_power",),
                (),
                15.0,
                None,
                -1.0,
            ),
        ],
    ),
    (
        "pv_energy",
        0,
        [
            (
                "energy_prefs",
                "measurement",
                1,
                ("sensor.fx_inv_prod_total",),
                (),
                9420.8,
                None,
                1.0,
            )
        ],
    ),
    (
        "grid_import_energy",
        0,
        [
            (
                "energy_prefs",
                "measurement",
                1,
                ("sensor.fx_inv_import_total",),
                (),
                4729.7,
                None,
                1.0,
            ),
            (
                "energy_prefs",
                "measurement",
                1,
                ("sensor.fx_meter2_total",),
                (),
                123456.0,
                None,
                1.0,
            ),
        ],
    ),
    (
        "grid_export_energy",
        0,
        [
            (
                "energy_prefs",
                "measurement",
                1,
                ("sensor.fx_inv_export_total",),
                (),
                2863.0,
                None,
                1.0,
            )
        ],
    ),
    (
        "pv_energy_today",
        0,
        [
            (
                "solarman",
                "measurement",
                1,
                ("sensor.fx_inv_prod_today",),
                (),
                1.1,
                None,
                1.0,
            )
        ],
    ),
    (
        "grid_export_energy_today",
        0,
        [
            (
                "solarman",
                "measurement",
                1,
                ("sensor.fx_inv_export_today",),
                (),
                0.0,
                None,
                1.0,
            )
        ],
    ),
]


def test_live_site_detection_golden():
    detection = run()
    assert [item.row for item in detection.offers] == list(detect.ROWS)
    assert [
        (
            item.row,
            item.default,
            [signature(option) for option in item.options],
        )
        for item in detection.offers
    ] == GOLDEN
    assert detection.unusable == ()
    rce = offer(detection, "sell").options[1]
    assert [target.published for target in rce.targets] == [True, False]
    assert offer(detection, "pv").options[0].forecasts == (
        SolarForecastBinding(site.SOLCAST_ENTRY, "solcast_solar"),
    )
    assert offer(detection, "pv").options[0].titles == ("Fx Solcast",)
    assert offer(detection, "sell").options[0].settlement == (
        "RCE, floor 0, multiplier 1.23"
    )
    assert offer(detection, "load").options[0].source_unit == "kWh"
    assert offer(detection, "grid_import_energy").options[1].source_unit == "Wh"
    _, notes = detection_text(detection, "en")
    assert "BMS SOC is offered unticked." in notes


def numeric(snap, entity_id, unit, source_unit, multiplier, age, minimum=None):
    return {
        "fixed": None,
        "entity": {
            "entity_id": entity_id,
            "registry_id": snap.entities[entity_id].registry_id,
            "attribute": None,
            "record_value_path": None,
        },
        "unit": unit,
        "source_unit": source_unit,
        "multiplier": multiplier,
        "minimum": minimum,
        "maximum": None,
        "max_age_seconds": age,
    }


def test_live_site_apply_golden():
    snap = snapshot()
    detection = run(snap)
    base = default_configuration("PLN", ZONE)
    base["sources"]["pv"]["enabled"] = True
    base["sources"]["battery_enabled"] = True
    selected = [
        item.options[item.default]
        for item in detection.offers
        if item.default is not None
    ]
    before = deepcopy(base)
    result = apply_detection(base, selected)
    assert base == before
    ids = snap.entities
    sources = result["sources"]
    assert sources["pv"] == {
        "enabled": True,
        "arrays": [],
        "solar_forecasts": [
            {"config_entry_id": site.SOLCAST_ENTRY, "domain": "solcast_solar"}
        ],
    }
    assert sources["sell"] == {
        **base["sources"]["sell"],
        "mode": "forecast",
        "fixed": None,
        "forecast": [
            {
                "entity": {
                    "entity_id": "sensor.fx_ec_sell",
                    "registry_id": ids["sensor.fx_ec_sell"].registry_id,
                    "attribute": "prices",
                    "record_value_path": None,
                },
                "value_path": "price",
                "start_path": "start",
                "end_path": "end",
                "duration_path": None,
                "interval_minutes": 15,
                "unit": "PLN/kWh",
                "unit_path": None,
                "value_kind": "price",
                "value_sign": 1.0,
                "source_timezone": "UTC",
                "published_path": "attributes.published_at",
                "max_age_seconds": 4500,
            }
        ],
    }
    assert sources["load"] == {
        "mode": "recorder",
        "forecast": None,
        "statistic_id": "sensor.fx_inv_load_total",
        "power": None,
        "daily_estimate": None,
        "history_unit": "kWh",
        "history_sign": 1,
        "power_max_gap_minutes": 60.0,
    }
    assert sources["soc"] == {
        "entity_id": "sensor.fx_ec_soc",
        "registry_id": ids["sensor.fx_ec_soc"].registry_id,
        "attribute": None,
        "record_value_path": None,
    }
    assert sources["bms_soc"] is None
    assert result["soc_options"] == {**base["soc_options"], "sign": 1}
    assert result["measurements"] == {
        "battery_power": numeric(
            snap, "sensor.fx_inv_battery_power", "kW", "W", 0.001, 3600
        ),
        "pv_power": numeric(snap, "sensor.fx_inv_pv_power", "kW", "W", 0.001, 86400),
        "grid_import_power": numeric(
            snap, "sensor.fx_grid_total_power", "kW", "W", 0.001, 3600
        ),
        "grid_export_power": numeric(
            snap, "sensor.fx_grid_total_power", "kW", "W", -0.001, 3600
        ),
        "pv_energy": numeric(snap, "sensor.fx_inv_prod_total", "kWh", "kWh", 1, 86400),
        "grid_import_energy": numeric(
            snap, "sensor.fx_inv_import_total", "kWh", "kWh", 1, 86400
        ),
        "grid_export_energy": numeric(
            snap, "sensor.fx_inv_export_total", "kWh", "kWh", 1, 86400
        ),
        "pv_energy_today": numeric(
            snap, "sensor.fx_inv_prod_today", "kWh", "kWh", 1, 86400, minimum=0
        ),
        "grid_export_energy_today": numeric(
            snap, "sensor.fx_inv_export_today", "kWh", "kWh", 1, 86400, minimum=0
        ),
    }
    assert sources["buy"] == base["sources"]["buy"]
    for key in ("settings", "preset", "helpers", "timezone", "currency"):
        assert result[key] == base[key]
    assert "setup_profiles" not in result
    validate_sources(SourceConfig.from_dict(result["sources"]), set())


def test_apply_with_bms_and_rce_choices():
    snap = snapshot()
    detection = run(snap)
    base = default_configuration("PLN", ZONE)
    result = apply_detection(
        base,
        [
            offer(detection, "bms_soc").options[0],
            offer(detection, "sell").options[1],
            offer(detection, "pv").options[1],
        ],
    )
    assert result["sources"]["bms_soc"]["attribute"] == "BMS SOC"
    assert result["soc_options"]["bms_unit"] == "%"
    sell = result["sources"]["sell"]
    assert sell["floor_per_kwh"] == 0.0
    assert [item["entity"]["entity_id"] for item in sell["forecast"]] == [
        "sensor.fx_rce_today",
        "sensor.fx_rce_tomorrow",
    ]
    assert sell["forecast"][0]["unit"] == "PLN/MWh"
    groups = result["sources"]["pv"]["arrays"]
    assert [item["entity"]["entity_id"] for item in groups[0]] == [
        "sensor.fx_sc_tomorrow",
        "sensor.fx_sc_today",
    ]
    assert "solar_forecasts" not in result["sources"]["pv"]


def test_never_detected_rows_are_refused():
    assert detect.NEVER_DETECTED == {
        "buy",
        "throughput_today",
        "battery_energy",
        "battery_charge_power",
        "battery_discharge_power",
    }
    option = Option(
        "throughput_today",
        "solarman",
        "measurement",
        1,
        (Target("sensor.fx_x", None),),
        source_unit="kWh",
    )
    with pytest.raises(ValueError):
        apply_detection(default_configuration("PLN", ZONE), [option])
    assert not set(detect.ROWS) & detect.NEVER_DETECTED


# --- identity -------------------------------------------------------------------


def rename_snapshot(snap, mapping):
    entities = {}
    for entity_id, facts in snap.entities.items():
        entities[mapping[entity_id]] = replace(facts, entity_id=mapping[entity_id])
    return replace(
        snap,
        entities=entities,
        energy=normalize_energy_prefs(rename_value(site.ENERGY_PREFS, mapping)),
    )


def rename_value(value, mapping):
    if isinstance(value, dict):
        return {k: rename_value(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [rename_value(v, mapping) for v in value]
    if isinstance(value, str):
        return mapping.get(value, value)
    return value


def canonical(detection, inverse):
    def sig(option):
        return (
            option.provider,
            option.kind,
            option.tier,
            tuple(inverse[target.entity_id] for target in option.targets),
            option.counts,
            option.value,
            option.attribute,
            option.sign,
        )

    return [
        (
            item.row,
            None if item.default is None else sig(item.options[item.default]),
            sorted((sig(option) for option in item.options), key=repr),
        )
        for item in detection.offers
    ]


def test_detection_ignores_entity_ids():
    snap = snapshot()
    names = sorted(snap.entities)
    mapping = {
        entity_id: f"{entity_id.split('.')[0]}.zq_{index:03d}_{entity_id[::-1][:6]}"
        for index, entity_id in enumerate(reversed(names))
    }
    inverse = {value: key for key, value in mapping.items()}
    renamed = run(rename_snapshot(snap, mapping))
    original = run(snap)
    assert canonical(renamed, inverse) == canonical(original, {k: k for k in names})


def test_distractors_are_never_offered():
    detection = run()
    seen = {
        target.entity_id
        for item in detection.offers
        for option in item.options
        for target in option.targets
    }
    assert not seen & site.DISTRACTORS
    assert "sensor.fx_ec_buy" in site.DISTRACTORS


# --- Q4: Energy dashboard first ----------------------------------------------------


def test_prefs_grid_power_differs_from_solarman_so_both_are_offered():
    item = offer(run(), "grid_import_power")
    assert [option.provider for option in item.options] == [
        "energy_prefs",
        "solarman",
    ]
    assert item.default == 0


def test_prefs_equal_to_solarman_is_one_prefs_option():
    item = offer(run(), "battery_power")
    assert len(item.options) == 1
    assert item.options[0].provider == "energy_prefs"
    text, _ = detection_text(run(), "en")
    assert "- Battery power: Energy dashboard — sensor.fx_inv_battery_power" in text


def test_unusable_prefs_candidate_falls_back_and_is_noted():
    snap = tweak(snapshot(), "sensor.fx_grid_total_power", state="unavailable")
    detection = run(snap)
    item = offer(detection, "grid_import_power")
    assert [option.provider for option in item.options] == ["solarman"]
    assert item.default == 0
    assert any(
        note.entity_ids == ("sensor.fx_grid_total_power",) and note.row == row
        for note in detection.unusable
        for row in ("grid_import_power", "grid_export_power")
    )
    _, notes = detection_text(detection, "en")
    assert "sensor.fx_grid_total_power" in notes
    assert "is not usable now" in notes


def test_prefs_power_from_power_config_only_is_used():
    data = edited_prefs()
    for source in data["energy_sources"]:
        source.pop("stat_rate", None)
    item = offer(run(prefs(snapshot(), data)), "grid_import_power")
    assert item.options[0].targets[0].entity_id == "sensor.fx_grid_total_power"


def test_prefs_two_sensor_power_without_stat_rate_is_skipped():
    data = edited_prefs(
        grid={
            "stat_rate": None,
            "power_config": {
                "stat_rate_from": "sensor.fx_grid_total_power",
                "stat_rate_to": "sensor.fx_inv_grid_power",
            },
        }
    )
    for source in data["energy_sources"]:
        if source["type"] == "grid":
            source.pop("stat_rate")
    item = offer(run(prefs(snapshot(), data)), "grid_import_power")
    assert [option.provider for option in item.options] == ["solarman"]


def test_disabled_prefs_entity_is_dropped_silently():
    snap = tweak(snapshot(), "sensor.fx_meter2_total", disabled=True, state=None)
    detection = run(snap)
    item = offer(detection, "grid_import_energy")
    assert len(item.options) == 1
    assert not any(
        note.entity_ids == ("sensor.fx_meter2_total",) for note in detection.unusable
    )


def test_missing_prefs_entity_is_noted():
    snap = drop(snapshot(), "sensor.fx_meter2_total")
    detection = run(snap)
    assert len(offer(detection, "grid_import_energy").options) == 1
    assert [
        (note.row, note.entity_ids, note.detail)
        for note in detection.unusable
        if note.entity_ids == ("sensor.fx_meter2_total",)
    ] == [("grid_import_energy", ("sensor.fx_meter2_total",), "missing entity")]


# --- Q5: SOC -----------------------------------------------------------------------


def with_prefs_soc(snap, unit="%"):
    snap = add(
        snap,
        fact(
            "sensor.fx_prefs_soc",
            attributes={"unit_of_measurement": unit, "device_class": "battery"},
        ),
    )
    data = edited_prefs(battery={"stat_soc": "sensor.fx_prefs_soc"})
    return prefs(snap, data)


def test_prefs_soc_is_the_default_when_present():
    item = offer(run(with_prefs_soc(snapshot())), "soc")
    assert [option.tier for option in item.options] == [1, 2, 3]
    assert item.default == 0
    assert item.options[0].targets[0].entity_id == "sensor.fx_prefs_soc"


def test_soc_with_kwh_unit_is_unusable():
    detection = run(with_prefs_soc(snapshot(), "kWh"))
    item = offer(detection, "soc")
    assert [option.tier for option in item.options] == [2, 3]
    assert any(
        note.entity_ids == ("sensor.fx_prefs_soc",) for note in detection.unusable
    )


def test_two_external_soc_templates_have_no_default():
    snap = add(
        snapshot(),
        fact(
            "sensor.fx_ec_soc_two",
            platform="template",
            attributes={"unit_of_measurement": "%", "device_class": "battery"},
        ),
    )
    item = offer(run(snap), "soc")
    assert [option.tier for option in item.options] == [2, 2, 3]
    assert item.default is None


def test_bms_soc_is_never_preselected():
    pack = site._solarman(
        "sensor.fx_pack_1", "battery_1", "55", "%", "measurement", device="fxd00002"
    )
    snap = add(snapshot(), pack)
    item = offer(run(snap), "bms_soc")
    assert [option.tier for option in item.options] == [1, 2]
    assert item.default is None


def test_battery_pack_option_needs_exactly_one_enabled_pack():
    packs = [
        site._solarman(
            f"sensor.fx_pack_{n}",
            f"battery_{n}",
            "55",
            "%",
            "measurement",
            device="fxd00002",
        )
        for n in (1, 2)
    ]
    snap = add(add(snapshot(), packs[0]), packs[1])
    assert [option.tier for option in offer(run(snap), "bms_soc").options] == [1]


def test_bms_attribute_out_of_range_is_unusable():
    snap = attributes_of(snapshot(), "sensor.fx_inv_battery", **{"BMS SOC": 120})
    detection = run(snap)
    assert offer(detection, "bms_soc") is None
    assert any(note.row == "bms_soc" for note in detection.unusable)


# --- Q7: solar forecast ------------------------------------------------------------


def test_no_prefs_forecast_leaves_the_solcast_checkbox():
    data = edited_prefs(solar={"config_entry_solar_forecast": []})
    item = offer(run(prefs(snapshot(), data)), "pv")
    assert len(item.options) == 1
    assert item.options[0].provider == "solcast"
    assert item.default == 0


def test_a_failing_prefs_forecast_entry_makes_the_option_unusable():
    snap = snapshot()
    data = edited_prefs(
        solar={
            "config_entry_solar_forecast": [site.SOLCAST_ENTRY, "fxe00007"],
        }
    )
    snap = prefs(snap, data)
    snap = replace(
        snap,
        solar_forecasts={
            **snap.solar_forecasts,
            "fxe00007": SolarForecastFacts(
                "fxe00007",
                "fx_forecast",
                "Fx Other",
                None,
                "Energy solar forecast not loaded: fx_forecast",
            ),
        },
    )
    detection = run(snap)
    item = offer(detection, "pv")
    assert [option.provider for option in item.options] == ["solcast"]
    assert item.default == 0
    assert [
        (note.row, note.provider, note.entity_ids, note.detail)
        for note in detection.unusable
    ] == [
        (
            "pv",
            "energy_prefs",
            ("solcast_solar", "fx_forecast"),
            "Energy solar forecast not loaded: fx_forecast",
        )
    ]
    _, notes = detection_text(detection, "en")
    assert "Energy solar forecast not loaded: fx_forecast" in notes


def test_two_prefs_forecast_entries_form_one_option():
    snap = snapshot()
    data = edited_prefs(
        solar={"config_entry_solar_forecast": [site.SOLCAST_ENTRY, "fxe00007"]}
    )
    snap = prefs(snap, data)
    snap = replace(
        snap,
        solar_forecasts={
            **snap.solar_forecasts,
            "fxe00007": SolarForecastFacts(
                "fxe00007",
                "fx_forecast",
                "Fx Other",
                dict(site.solcast_wh_hours()),
                None,
            ),
        },
    )
    option = offer(run(snap), "pv").options[0]
    assert option.kind == "solar_forecast"
    assert [item.config_entry_id for item in option.forecasts] == [
        site.SOLCAST_ENTRY,
        "fxe00007",
    ]
    assert option.titles == ("Fx Solcast", "Fx Other")
    assert "Fx Solcast (solcast_solar) + Fx Other (fx_forecast)" in option_label(
        option, "en"
    )


def test_forecast_without_future_values_is_unusable():
    snap = snapshot()
    stale = {"2026-10-01T12:00:00+02:00": 5}
    snap = replace(
        snap,
        solar_forecasts={
            site.SOLCAST_ENTRY: SolarForecastFacts(
                site.SOLCAST_ENTRY, "solcast_solar", "Fx Solcast", stale, None
            )
        },
    )
    detection = run(snap)
    assert offer(detection, "pv").options[0].provider == "solcast"
    assert detection.unusable[0].detail == (
        "Energy solar forecast has no current or future values"
    )


# --- Q9: sell ----------------------------------------------------------------------


def test_rce_with_kwh_unit_is_unusable():
    snap = attributes_of(
        snapshot(), "sensor.fx_rce_today", unit_of_measurement="PLN/kWh"
    )
    detection = run(snap)
    assert [option.provider for option in offer(detection, "sell").options] == [
        "template"
    ]
    assert any(
        note.detail == "RCE sensor must report PLN/MWh" for note in detection.unusable
    )


def test_stale_template_falls_back_to_rce():
    snap = attributes_of(
        snapshot(), "sensor.fx_ec_sell", published_at="2026-10-07T00:00:00+00:00"
    )
    item = offer(run(snap), "sell")
    assert [option.provider for option in item.options] == ["rce_pse"]
    assert item.default == 0


def test_template_without_settlement_multiplier_is_not_a_sell_source():
    snap = attributes_of(snapshot(), "sensor.fx_ec_sell", settlement="gross")
    assert [option.provider for option in offer(run(snap), "sell").options] == [
        "rce_pse"
    ]


def test_published_tomorrow_rce_counts_its_intervals():
    snap = attributes_of(
        snapshot(), "sensor.fx_rce_tomorrow", prices=site._rce_records()
    )
    snap = tweak(snap, "sensor.fx_rce_tomorrow", state="500")
    rce = offer(run(snap), "sell").options[1]
    assert rce.counts == (96, 96)
    assert [target.published for target in rce.targets] == [True, True]


# --- Solarman grouping ---------------------------------------------------------------


def test_two_deye_entries_leave_every_solarman_row_without_a_default():
    snap = snapshot()
    second = site._solarman(
        "sensor.fx_inv2_load_total",
        "total_load_consumption",
        "100",
        "kWh",
        "total_increasing",
        device="fxd00007",
    )
    second = replace(second, config_entry_id="fxe00008")
    today = replace(
        site._solarman(
            "sensor.fx_inv2_prod_today",
            "today_production",
            "1",
            "kWh",
            "total_increasing",
            device="fxd00007",
        ),
        config_entry_id="fxe00008",
    )
    snap = add(add(snap, second), today)
    snap = replace(
        snap,
        devices={
            **snap.devices,
            "fxd00007": detect.DeviceFacts(
                "fxd00007", "Deye", None, frozenset({"fxe00008"})
            ),
        },
    )
    detection = run(snap)
    for row in ("load", "pv_energy_today"):
        item = offer(detection, row)
        assert len(item.options) == 2
        assert item.default is None
    assert offer(detection, "grid_import_energy").default is None
    assert offer(detection, "battery_power").default == 0


def test_a_non_deye_solarman_entry_has_no_solarman_candidates():
    snap = snapshot()
    devices = {
        key: replace(device, manufacturer="Solarman")
        if key in ("fxd00001", "fxd00002")
        else device
        for key, device in snap.devices.items()
    }
    detection = run(replace(snap, devices=devices))
    assert offer(detection, "load") is None
    assert offer(detection, "pv_energy_today") is None
    assert [o.provider for o in offer(detection, "grid_import_power").options] == [
        "energy_prefs"
    ]


# --- gates ---------------------------------------------------------------------------


def test_eur_has_no_sell_offer_and_no_note():
    detection = run(currency="EUR")
    assert offer(detection, "sell") is None
    assert not any(note.row == "sell" for note in detection.unusable)


def test_pv_off_drops_the_pv_rows():
    detection = run(pv_enabled=False)
    rows = {item.row for item in detection.offers}
    assert not rows & {"pv", "pv_power", "pv_energy"}
    assert "pv_energy_today" in rows


def test_battery_off_drops_the_battery_rows():
    detection = run(battery_enabled=False)
    rows = {item.row for item in detection.offers}
    assert not rows & {"soc", "bms_soc", "battery_power"}
    assert "grid_export_energy_today" in rows


# --- load ---------------------------------------------------------------------------


def test_load_needs_a_cumulative_energy_state():
    snap = attributes_of(
        snapshot(), "sensor.fx_inv_load_total", state_class="measurement"
    )
    detection = run(snap)
    assert offer(detection, "load") is None
    assert detection.unusable[0].row == "load"
    snap = attributes_of(
        snapshot(), "sensor.fx_inv_load_total", unit_of_measurement="MWh"
    )
    assert offer(run(snap), "load") is None


# --- normalize ------------------------------------------------------------------------


def test_normalize_live_layout():
    facts = normalize_energy_prefs(site.ENERGY_PREFS)
    assert facts == detect.EnergyPrefsFacts(
        solar_energy=("sensor.fx_inv_prod_total",),
        solar_power=("sensor.fx_inv_pv_power",),
        solar_forecast_entries=(site.SOLCAST_ENTRY,),
        grid_import=("sensor.fx_inv_import_total", "sensor.fx_meter2_total"),
        grid_export=("sensor.fx_inv_export_total",),
        grid_power=("sensor.fx_grid_total_power",),
        battery_power=("sensor.fx_inv_battery_power",),
        battery_soc=(),
    )


@pytest.mark.parametrize("value", [None, {}, [], "x"])
def test_normalize_without_prefs_is_none(value):
    assert normalize_energy_prefs(value) is None


def test_normalize_generated_stat_rate_variants_and_hygiene():
    data = {
        "energy_sources": [
            "junk",
            {
                "type": "grid",
                "stat_energy_from": "sensor.fx_a",
                "stat_energy_to": "sensor.fx_a",
                "stat_rate": "sensor.fx_generated_power",
                "power_config": {"stat_rate_inverted": "sensor.fx_raw"},
            },
            {
                "type": "grid",
                "stat_energy_from": "external:fx_stat",
                "power_config": {"stat_rate_inverted": "sensor.fx_raw"},
            },
            {
                "type": "battery",
                "power_config": {"stat_rate": "sensor.fx_batt"},
                "stat_soc": "sensor.fx_soc",
            },
            {"type": "solar"},
            {"type": "gas", "stat_energy_from": "sensor.fx_gas"},
        ]
    }
    facts = normalize_energy_prefs(data)
    assert facts.grid_import == ("sensor.fx_a",)
    assert facts.grid_export == ("sensor.fx_a",)
    assert facts.grid_power == ("sensor.fx_generated_power",)
    assert facts.battery_power == ("sensor.fx_batt",)
    assert facts.battery_soc == ("sensor.fx_soc",)
    assert facts.solar_energy == ()
    assert normalize_energy_prefs({"energy_sources": []}) == detect.EnergyPrefsFacts(
        (), (), (), (), (), (), (), ()
    )
    assert normalize_energy_prefs({"x": 1}) == detect.EnergyPrefsFacts(
        (), (), (), (), (), (), (), ()
    )


# --- texts -----------------------------------------------------------------------------

EN_DETECTED = """- PV forecast: Energy dashboard — Fx Solcast (solcast_solar): 50 hourly values ahead (preselected); also: Solcast — sensor.fx_sc_tomorrow (today: 48 intervals) + sensor.fx_sc_today (tomorrow: 48 intervals)
- Sell price: Template — sensor.fx_ec_sell (192 intervals; settlement RCE, floor 0, multiplier 1.23; sell multiplier set to 1) (preselected); also: RCE PSE — sensor.fx_rce_today (today: 96 intervals) + sensor.fx_rce_tomorrow (tomorrow: not yet published)
- Household load history: Solarman (Deye) — sensor.fx_inv_load_total (recorder statistic, kWh)
- Battery SOC: Template — sensor.fx_ec_soc (now 23.2 %) (preselected); also: Solarman (Deye) — sensor.fx_inv_battery (now 1 %)
- BMS SOC (agreement check): Solarman (Deye) — sensor.fx_inv_battery attribute BMS SOC (now 12 %) — not ticked, see the note below
- Battery power: Energy dashboard — sensor.fx_inv_battery_power (now -890 W)
- PV power: Energy dashboard — sensor.fx_inv_pv_power (now 1650 W)
- Grid import power: Energy dashboard — sensor.fx_grid_total_power (now 15 W) (preselected); also: Solarman (Deye) — sensor.fx_inv_grid_power (now 15 W)
- Grid export power: Energy dashboard — sensor.fx_grid_total_power (now 15 W; sign −1) (preselected); also: Solarman (Deye) — sensor.fx_inv_grid_power (now 15 W; sign −1)
- PV energy: Energy dashboard — sensor.fx_inv_prod_total (now 9420.8 kWh)
- Grid import energy: Energy dashboard — sensor.fx_inv_import_total (now 4729.7 kWh) (preselected); also: Energy dashboard — sensor.fx_meter2_total (now 123456 Wh)
- Grid export energy: Energy dashboard — sensor.fx_inv_export_total (now 2863 kWh)
- PV energy today: Solarman (Deye) — sensor.fx_inv_prod_today (now 1.1 kWh)
- Grid export energy today: Solarman (Deye) — sensor.fx_inv_export_today (now 0 kWh)"""
EN_NOTES = (
    "- BMS SOC is offered unticked. With imbalanced battery cells the BMS SOC and "
    "the battery SOC can differ by more than Battery → Soc disagreement percent "
    "(default 5 %), and then every plan is blocked. Tick it only if both agree "
    "over a full day."
)
PL_DETECTED = """- Prognoza PV: Panel Energia — Fx Solcast (solcast_solar): 50 wartości godzinowych naprzód (wybrane); także: Solcast — sensor.fx_sc_tomorrow (dziś: 48 przedziałów) + sensor.fx_sc_today (jutro: 48 przedziałów)
- Cena sprzedaży: Szablon — sensor.fx_ec_sell (192 przedziałów; rozliczenie RCE, floor 0, multiplier 1.23; mnożnik sprzedaży ustawiony na 1) (wybrane); także: RCE PSE — sensor.fx_rce_today (dziś: 96 przedziałów) + sensor.fx_rce_tomorrow (jutro: jeszcze nieopublikowane)
- Historia zużycia domu: Solarman (Deye) — sensor.fx_inv_load_total (statystyka rejestratora, kWh)
- SOC baterii: Szablon — sensor.fx_ec_soc (teraz 23.2 %) (wybrane); także: Solarman (Deye) — sensor.fx_inv_battery (teraz 1 %)
- SOC z BMS (kontrola zgodności): Solarman (Deye) — sensor.fx_inv_battery atrybut BMS SOC (teraz 12 %) — niezaznaczone, zobacz uwagę poniżej
- Moc baterii: Panel Energia — sensor.fx_inv_battery_power (teraz -890 W)
- Moc PV: Panel Energia — sensor.fx_inv_pv_power (teraz 1650 W)
- Moc importu z sieci: Panel Energia — sensor.fx_grid_total_power (teraz 15 W) (wybrane); także: Solarman (Deye) — sensor.fx_inv_grid_power (teraz 15 W)
- Moc eksportu do sieci: Panel Energia — sensor.fx_grid_total_power (teraz 15 W; znak −1) (wybrane); także: Solarman (Deye) — sensor.fx_inv_grid_power (teraz 15 W; znak −1)
- Energia PV: Panel Energia — sensor.fx_inv_prod_total (teraz 9420.8 kWh)
- Energia importu z sieci: Panel Energia — sensor.fx_inv_import_total (teraz 4729.7 kWh) (wybrane); także: Panel Energia — sensor.fx_meter2_total (teraz 123456 Wh)
- Energia eksportu do sieci: Panel Energia — sensor.fx_inv_export_total (teraz 2863 kWh)
- Dzienna energia PV: Solarman (Deye) — sensor.fx_inv_prod_today (teraz 1.1 kWh)
- Dzienna energia eksportu do sieci: Solarman (Deye) — sensor.fx_inv_export_today (teraz 0 kWh)"""
PL_NOTES = (
    "- SOC z BMS jest proponowany bez zaznaczenia. Przy niezbalansowanych "
    "ogniwach SOC z BMS i SOC baterii mogą się różnić o więcej niż Bateria → "
    "Tolerancja różnicy BMS (domyślnie 5 %), a wtedy każdy plan jest blokowany. "
    "Zaznacz tylko, jeśli oba są zgodne przez całą dobę."
)


def test_detection_text_exact():
    detection = run()
    assert detection_text(detection, "en") == (EN_DETECTED, EN_NOTES)
    assert detection_text(detection, None) == (EN_DETECTED, EN_NOTES)
    assert detection_text(detection, "pl") == (PL_DETECTED, PL_NOTES)


def test_no_default_select_row_and_unusable_row_texts():
    snap = add(
        snapshot(),
        fact(
            "sensor.fx_ec_soc_two",
            platform="template",
            attributes={"unit_of_measurement": "%", "device_class": "battery"},
        ),
    )
    snap = tweak(snap, "sensor.fx_grid_total_power", state="unavailable")
    detection = run(snap)
    detected, notes = detection_text(detection, "en")
    assert (
        "- Battery SOC: several candidates, none preselected: Template — "
        "sensor.fx_ec_soc (now 23.2 %); Template — sensor.fx_ec_soc_two (now 50 %); "
        "Solarman (Deye) — sensor.fx_inv_battery (now 1 %)"
    ) in detected
    assert (
        "- Grid import power: Solarman (Deye) — sensor.fx_inv_grid_power (now 15 W)"
        in detected
    )
    assert (
        "- Grid import power: Energy dashboard sensor.fx_grid_total_power is not "
        "usable now (source state unavailable); set it in Sources if needed."
    ) in notes
    detected_pl, notes_pl = detection_text(detection, "pl")
    assert (
        "- SOC baterii: kilku kandydatów, żaden nie jest wybrany: Szablon — "
        "sensor.fx_ec_soc (teraz 23.2 %); Szablon — sensor.fx_ec_soc_two (teraz "
        "50 %); Solarman (Deye) — sensor.fx_inv_battery (teraz 1 %)"
    ) in detected_pl
    assert (
        "- Moc importu z sieci: Panel Energia sensor.fx_grid_total_power jest teraz "
        "nieużywalne (Stan źródła jest niedostępny.); w razie potrzeby ustaw w "
        "Źródłach danych."
    ) in notes_pl


def test_row_labels():
    assert row_label("pv", "en") == "PV forecast"
    assert row_label("bms_soc", "pl") == "SOC z BMS (kontrola zgodności)"
    assert row_label("battery_power", "en") == "Battery power"
    assert row_label("grid_export_energy_today", "pl") == (
        "Dzienna energia eksportu do sieci"
    )
    assert detect.SKIP == {"en": "Do not bind", "pl": "Nie wiąż"}
