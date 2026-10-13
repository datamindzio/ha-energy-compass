"""The controller dashboards show what the native engine adds."""

from pathlib import Path

import pytest
from homeassistant.helpers.template import Template
from homeassistant.util import yaml as yaml_util

DASHBOARDS = Path(__file__).resolve().parents[1] / "examples/dashboards"
CONTROLLER = "sensor.energy_compass_deye_controller"
RUNTIME = "sensor.energy_compass_deye_controller_runtime"
PAUSE = "switch.energy_compass_pause_inverter_writes"


def cards(name):
    return yaml_util.load_yaml(DASHBOARDS / name)["cards"]


async def render(hass, state_hardware, last_run):
    await hass.config.async_set_time_zone("Europe/Warsaw")
    hass.states.async_set(
        CONTROLLER, "2026-10-12T10:00:00+00:00", {"hardware": state_hardware}
    )
    hass.states.async_set(
        RUNTIME,
        "ok",
        {
            "runtime": {"code": "ok", "reason": "ok", "reason_code": "ok"},
            "last_run": last_run,
        },
    )
    markdown = next(
        card for card in cards("controller_diagnostics.yaml") if "content" in card
    )
    return Template(markdown["content"], hass).async_render(parse_result=False)


async def test_diagnostics_show_reason_code_hardware_and_last_run(hass):
    text = await render(
        hass,
        {"device_type": "Some Inverter", "protocol": "1.4", "verified": True},
        {
            "at": "2026-10-12T10:00:05+00:00",
            "trigger": "minute",
            "outcome": "run_finished",
            "seconds": 1.25,
        },
    )
    assert "| Reason code | ok |" in text
    assert "| Inverter | Some Inverter, protocol 1.4 · verified |" in text
    assert "| Last run | 12.10 12:00:05 · minute · run_finished · 1.25 s |" in text


async def test_diagnostics_without_the_native_attributes_show_dashes(hass):
    text = await render(hass, None, None)
    assert "| Inverter | — |" in text
    assert "| Last run | — |" in text


async def test_an_unverified_inverter_says_so(hass):
    text = await render(
        hass, {"device_type": "Other", "protocol": "2.0", "verified": False}, None
    )
    assert "| Inverter | Other, protocol 2.0 · not verified |" in text


def tiles(card):
    if card.get("type") == "conditional":
        yield from tiles(card["card"])
    yield card
    for child in card.get("cards", []):
        yield from tiles(child)


@pytest.mark.parametrize("name", ["controller_panel.yaml"])
def test_the_panel_offers_the_pause_switch_only_when_it_exists(name):
    found = [
        card
        for top in cards(name)
        for card in tiles(top)
        if card.get("type") == "conditional"
        and card.get("card", {}).get("entity") == PAUSE
    ]
    assert len(found) == 1
    assert found[0]["conditions"][0]["state"] == ["on", "off"]
    assert found[0]["card"]["features"] == [{"type": "toggle"}]
