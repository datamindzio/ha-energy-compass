"""Exercise the documented tariff Template sensor with Home Assistant rendering."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from homeassistant.components.template import DATA_COORDINATORS
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)
from custom_components.energy_compass.sources.prices import price_series

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "tariff-helper.yaml"
ENTITY_ID = "sensor.energy_compass_tariff_price"


@pytest.fixture
def example():
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
async def warsaw_time_zone(hass):
    await hass.config.async_set_time_zone("Europe/Warsaw")


def _render(hass, freezer, example, instant, **settings):
    freezer.move_to(instant)
    sensor = example["template"][0]["sensor"][0]
    variables = dict(example["template"][0]["variables"])
    variables.update(settings)
    for name, value in variables.copy().items():
        if isinstance(value, str) and "{%" in value:
            variables[name] = Template(value, hass).async_render(
                variables, parse_result=True
            )
    available = Template(sensor["availability"], hass).async_render(
        variables, parse_result=True
    )
    if not available:
        return None, []
    state = Template(sensor["state"], hass).async_render(variables, parse_result=True)
    rows = Template(sensor["attributes"]["forecast"], hass).async_render(
        variables, parse_result=True
    )
    return float(state), rows


def _parsed(rows, state, instant):
    snapshot = {
        ENTITY_ID: {
            "state": str(state),
            "attributes": {
                "forecast": rows,
                "generated_at": instant.isoformat(),
            },
            "last_updated": instant.isoformat(),
        }
    }
    binding = IntervalBinding(
        entity=EntityBinding(ENTITY_ID, attribute="forecast"),
        value_path="price",
        start_path="start",
        end_path="end",
        unit="PLN/kWh",
        value_kind="price",
        published_path="attributes.generated_at",
        max_age_seconds=3600,
    )
    return price_series(snapshot, binding, instant, multiplier=1, addition_per_kwh=0)


@pytest.mark.parametrize(
    ("instant", "group", "expected"),
    [
        ("2026-09-18T10:22:34+00:00", "G11", 0.9),
        ("2026-09-18T10:22:34+00:00", "G12", 0.9),
        ("2026-09-18T11:22:34+00:00", "G12", 0.55),
        ("2026-09-18T13:22:34+00:00", "G12", 0.9),
        ("2026-09-18T20:22:34+00:00", "G12", 0.55),
        ("2026-09-19T10:22:34+00:00", "G12w", 0.55),
        ("2026-09-20T22:22:34+00:00", "G12w", 0.55),
        ("2026-09-21T04:22:34+00:00", "G12w", 0.9),
    ],
)
def test_actual_template_rates_and_consumer_contract(
    hass, freezer, example, instant, group, expected
):
    now = datetime.fromisoformat(instant)
    state, rows = _render(hass, freezer, example, instant, tariff_group=group)
    assert state == pytest.approx(expected)
    assert isinstance(rows, list)
    assert len(rows) == 49
    parsed = _parsed(rows, state, now)
    assert parsed[0].start <= now < parsed[0].end
    assert parsed[0].value == pytest.approx(state)
    assert parsed[-1].end - now >= timedelta(hours=48)
    assert all(right.start == left.end for left, right in pairwise(parsed))
    assert all(row.end - row.start == timedelta(hours=1) for row in parsed)


@pytest.mark.parametrize(
    ("instant", "window", "expected"),
    [
        # 13:22 local on 18 September: fixed 13-15 is off-peak, seasonal summer is not.
        ("2026-09-18T11:22:34+00:00", "fixed", 0.55),
        ("2026-09-18T11:22:34+00:00", "seasonal", 0.9),
        # 15:22 local in September: seasonal summer window 15-17.
        ("2026-09-18T13:22:34+00:00", "seasonal", 0.55),
        # 13:22 local in November: seasonal winter window 13-15.
        ("2026-11-18T12:22:34+00:00", "seasonal", 0.55),
        ("2026-11-18T14:22:34+00:00", "seasonal", 0.9),
    ],
)
def test_afternoon_window(hass, freezer, example, instant, window, expected):
    state, rows = _render(
        hass, freezer, example, instant, tariff_group="G12", afternoon_window=window
    )
    assert state == pytest.approx(expected)
    assert len(rows) == 49


def test_unknown_afternoon_window_cannot_publish_a_price(hass, freezer, example):
    state, rows = _render(
        hass, freezer, example, "2026-09-18T11:22:34+00:00", afternoon_window="summer"
    )
    assert state is None and rows == []


@pytest.mark.parametrize(
    ("instant", "clock", "expected"),
    [
        # 20:30 UTC in July is 22:30 CEST but 21:30 on a fixed winter-time meter.
        ("2026-07-01T20:30:00+00:00", "local", 0.55),
        ("2026-07-01T20:30:00+00:00", "winter", 0.9),
        ("2026-01-15T21:30:00+00:00", "local", 0.55),
        ("2026-01-15T21:30:00+00:00", "winter", 0.55),
    ],
)
def test_meter_clock_local_or_fixed_winter_time(
    hass, freezer, example, instant, clock, expected
):
    state, rows = _render(
        hass, freezer, example, instant, tariff_group="G12", meter_clock=clock
    )
    assert state == pytest.approx(expected)
    assert len(rows) == 49


def test_meter_clock_defaults_to_local(example):
    assert example["template"][0]["variables"]["meter_clock"] == "local"


def test_unknown_meter_clock_cannot_publish_a_price(hass, freezer, example):
    state, rows = _render(
        hass,
        freezer,
        example,
        "2026-09-18T11:22:34+00:00",
        meter_clock="summer",
    )
    assert state is None and rows == []


def test_explicit_date_is_off_peak_without_holiday_lookups(hass, freezer, example):
    instant = "2026-12-24T10:32:00+00:00"
    state, rows = _render(
        hass,
        freezer,
        example,
        instant,
        tariff_group="G12w",
        off_peak_dates=["2026-12-24"],
    )
    assert state == pytest.approx(0.55)
    assert _parsed(rows, state, datetime.fromisoformat(instant))[0].value == 0.55


@pytest.mark.parametrize(
    ("instant", "first_local", "second_local"),
    [
        (
            "2026-03-29T00:34:00+00:00",
            "2026-03-29T01:00:00+01:00",
            "2026-03-29T03:00:00+02:00",
        ),
        (
            "2026-10-25T00:34:00+00:00",
            "2026-10-25T02:00:00+02:00",
            "2026-10-25T02:00:00+01:00",
        ),
    ],
)
def test_dst_real_hours_cover_gap_and_fold(
    hass, freezer, example, instant, first_local, second_local
):
    state, rows = _render(hass, freezer, example, instant, tariff_group="G12w")
    parsed = _parsed(rows, state, datetime.fromisoformat(instant))
    zone = ZoneInfo("Europe/Warsaw")
    assert parsed[0].start.astimezone(zone).isoformat() == first_local
    assert parsed[1].start.astimezone(zone).isoformat() == second_local
    assert parsed[1].start - parsed[0].start == timedelta(hours=1)


@pytest.mark.parametrize(
    "settings",
    [
        {"tariff_group": "G13"},
        {"tariff_group": None},
        {"base_rate": None},
        {"base_rate": ""},
        {"base_rate": float("nan")},
        {"off_peak_rate": float("inf")},
        {"tariff_group": "G11", "off_peak_rate": float("nan")},
    ],
)
def test_invalid_configuration_cannot_publish_a_price(hass, freezer, example, settings):
    state, rows = _render(
        hass, freezer, example, "2026-09-18T10:22:34+00:00", **settings
    )
    assert state is None
    assert rows == []


def test_missing_base_rate_cannot_publish_a_price(hass, freezer, example):
    del example["template"][0]["variables"]["base_rate"]
    state, rows = _render(hass, freezer, example, "2026-09-18T10:22:34+00:00")
    assert state is None
    assert rows == []


@pytest.mark.freeze_time("2026-09-18T10:14:59.990000+00:00")
async def test_configured_entity_startup_and_quarter_hour_refresh(
    hass, freezer, example
):
    assert await async_setup_component(hass, "template", example)
    try:
        await hass.async_start()
        await hass.async_block_till_done()
        first = hass.states.get(ENTITY_ID)
        assert first is not None
        assert float(first.state) == pytest.approx(0.9)
        assert len(first.attributes["forecast"]) == 49
        assert _parsed(first.attributes["forecast"], first.state, datetime.now(UTC))

        freezer.move_to("2026-09-18T10:15:00.010000+00:00")
        async_fire_time_changed(hass, datetime.now(UTC))
        await hass.async_block_till_done()
        refreshed = hass.states.get(ENTITY_ID)
        assert refreshed is not None
        assert refreshed.attributes["generated_at"] != first.attributes["generated_at"]
        assert float(refreshed.state) == pytest.approx(0.9)
    finally:
        for coordinator in hass.data.get(DATA_COORDINATORS, []):
            await coordinator.async_shutdown()
        await hass.async_stop()
