import json
from pathlib import Path

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.settings import (
    EXPERT_GROUPS,
    GROUPS,
    default_configuration,
)

COMPONENT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "energy_compass"
)
HIDDEN = [
    "installation",
    "sources",
    "battery",
    "hardware",
    "tariffs",
    "forecast",
    "notifications",
    "show_expert",
    "preview",
]
SHOWN = [
    "installation",
    "sources",
    "battery",
    "hardware",
    "tariffs",
    "forecast",
    "planning",
    "compass",
    "performance",
    "presentation",
    "notifications",
    "helpers",
    "hide_expert",
    "preview",
]


def _payload():
    return {
        "name": "Fresh",
        "currency": "EUR",
        "timezone": "UTC",
        "preset": "generic",
        "settlement": "generic",
        "inverter": "generic",
        "pv_enabled": False,
        "battery_enabled": False,
    }


async def start(hass, kind):
    if kind == "config":
        result = await hass.config_entries.flow.async_init(
            "energy_compass", context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _payload()
        )
        return hass.config_entries.flow, result
    entry = MockConfigEntry(
        domain="energy_compass", data=default_configuration("EUR", "UTC"), version=3
    )
    entry.add_to_hass(hass)
    if kind == "options":
        result = await hass.config_entries.options.async_init(entry.entry_id)
        return hass.config_entries.options, result
    result = await hass.config_entries.flow.async_init(
        "energy_compass",
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    return hass.config_entries.flow, result


def test_expert_groups_are_a_subset_of_the_groups():
    assert set(EXPERT_GROUPS) < set(GROUPS)
    assert EXPERT_GROUPS == ("planning", "compass", "performance", "presentation")


@pytest.mark.parametrize("kind", ["config", "options", "reconfigure"])
async def test_every_flow_starts_hidden_and_round_trips(
    recorder_mock, hass, enable_custom_integrations, kind
):
    manager, menu = await start(hass, kind)
    fid = menu["flow_id"]
    flow = manager._progress[fid]
    assert flow.show_advanced_options is True
    atlas = ["energy_atlas"] if kind == "options" else []
    hidden = [*HIDDEN[:-2], *atlas, "show_expert", "preview"]
    shown = [*SHOWN[:-2], *atlas, "hide_expert", "preview"]
    assert menu["menu_options"] == hidden
    menu = await manager.async_configure(fid, {"next_step_id": "show_expert"})
    assert menu["step_id"] == "menu"
    assert menu["menu_options"] == shown
    menu = await manager.async_configure(fid, {"next_step_id": "hide_expert"})
    assert menu["menu_options"] == hidden


@pytest.mark.parametrize("kind", ["config", "options"])
async def test_hidden_groups_are_refused_until_shown(
    recorder_mock, hass, enable_custom_integrations, kind
):
    manager, menu = await start(hass, kind)
    fid = menu["flow_id"]
    for step in ("planning", "compass", "performance", "presentation", "helpers"):
        with pytest.raises(InvalidData):
            await manager.async_configure(fid, {"next_step_id": step})
    await manager.async_configure(fid, {"next_step_id": "show_expert"})
    form = await manager.async_configure(fid, {"next_step_id": "planning"})
    assert form["step_id"] == "planning"


async def test_values_survive_the_toggle(
    recorder_mock, hass, enable_custom_integrations
):
    manager, menu = await start(hass, "config")
    fid = menu["flow_id"]
    await manager.async_configure(fid, {"next_step_id": "show_expert"})
    await manager.async_configure(fid, {"next_step_id": "planning"})
    await manager.async_configure(fid, {"terminal_value_per_kwh": 0.5})
    await manager.async_configure(fid, {"next_step_id": "hide_expert"})
    await manager.async_configure(fid, {"next_step_id": "show_expert"})
    assert manager._progress[fid]._draft["settings"]["terminal_value_per_kwh"] == 0.5


def test_expert_strings_are_exact():
    expected = {
        "strings.json": ("Show expert settings", "Hide expert settings"),
        "translations/en.json": ("Show expert settings", "Hide expert settings"),
        "translations/pl.json": (
            "Pokaż ustawienia eksperckie",
            "Ukryj ustawienia eksperckie",
        ),
    }
    for name, (show, hide) in expected.items():
        data = json.loads((COMPONENT / name).read_text())
        for flow in ("config", "options"):
            options = data[flow]["step"]["menu"]["menu_options"]
            assert options["show_expert"] == show
            assert options["hide_expert"] == hide
