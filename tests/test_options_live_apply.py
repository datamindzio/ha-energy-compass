"""An options save that the running coordinator can adopt must not reload the entry."""

import ast
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass import coordinator as module
from custom_components.energy_compass import settings
from custom_components.energy_compass.settings import (
    ENTITY_SET_SETTINGS,
    ENTITY_SET_TOP_LEVEL,
    RUNTIME_READ_SETTINGS,
    default_configuration,
    options_require_reload,
)

PACKAGE = Path(module.__file__).parent


def _config():
    config = default_configuration("EUR", "UTC")
    config["name"] = "Options"
    config["settings"].update(
        horizon_hours=1, display_horizon_hours=1, reference_horizon_hours=1
    )
    return config


def _changed(**updates):
    config = _config()
    config["settings"].update(updates)
    return config


@pytest.mark.parametrize("key", sorted(ENTITY_SET_SETTINGS))
def test_entity_gating_flag_requires_reload(key):
    flipped = not _config()["settings"][key]
    assert options_require_reload(_config(), _changed(**{key: flipped}))


@pytest.mark.parametrize("key", ["name", "currency"])
def test_entry_identity_change_requires_reload(key):
    changed = _config()
    changed[key] = "PLN" if key == "currency" else "Renamed"
    assert key in ENTITY_SET_TOP_LEVEL
    assert options_require_reload(_config(), changed)


@pytest.mark.parametrize(
    "updates",
    [
        {"cost_precision": 3},
        {"strategy": "max_export"},
        {"capacity_kwh": 12.5},
        {"refresh_minutes": 30},
    ],
)
def test_other_settings_apply_live(updates):
    assert not options_require_reload(_config(), _changed(**updates))


def test_sources_and_helpers_apply_live():
    changed = _config()
    changed["helpers"]["grid_import_kw"] = {
        "entity": {"entity_id": "input_number.other"},
        "unit": "kW",
        "max_age_seconds": None,
    }
    changed["sources"]["buy"]["fixed"]["fixed"] = 0.4
    assert not options_require_reload(_config(), changed)


def test_absent_gating_key_compares_as_its_default():
    previous = _config()
    del previous["settings"]["flexible_load_enabled"]
    assert not options_require_reload(previous, _config())


def _settings_keys_read_by_entities():
    keys = set()
    for name in ("entity.py", "sensor.py", "binary_sensor.py", "select.py"):
        tree = ast.parse((PACKAGE / name).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and _is_settings(node.value)
            ):
                keys.add(node.slice.value)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and _is_settings(node.func.value)
            ):
                keys.add(node.args[0].value)
    return keys


def _is_settings(node):
    if isinstance(node, ast.Name):
        return node.id == "settings"
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "settings"
    )


def test_every_setting_read_by_an_entity_platform_is_classified():
    read = _settings_keys_read_by_entities()
    assert ENTITY_SET_SETTINGS <= read
    unclassified = read - ENTITY_SET_SETTINGS - RUNTIME_READ_SETTINGS
    assert not unclassified, (
        f"classify {sorted(unclassified)} in settings.ENTITY_SET_SETTINGS "
        "(changes which entities exist) or RUNTIME_READ_SETTINGS (read at publish time)"
    )


def test_classified_settings_exist():
    known = set(settings.NUMBERS) | set(settings.BOOLEANS) | set(settings.CHOICES)
    known |= set(default_configuration("EUR", "UTC")["settings"])
    assert (ENTITY_SET_SETTINGS | RUNTIME_READ_SETTINGS) <= known


async def _setup(hass, config):
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title=config["name"], version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def _walk(hass, entry, steps, final_form=None):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    for step in steps:
        await hass.config_entries.options.async_configure(fid, step)
    await hass.config_entries.options.async_configure(fid, {"next_step_id": "preview"})
    return fid


TARIFF_STEPS = (
    {"next_step_id": "tariffs"},
    {"next_step_id": "tariff_values"},
    {"buy_rate": 0.4},
    {"next_step_id": "menu"},
)


async def test_tariff_save_keeps_the_plan_published_while_recalculating(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-17T10:00:00+00:00")
    entry = await _setup(hass, _config())
    coordinator = entry.runtime_data
    before = hass.states.get("sensor.options_plan")
    assert before.state not in ("unavailable", "unknown")
    started, release = threading.Event(), threading.Event()
    original = module.compute

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    fid = await _walk(hass, entry, TARIFF_STEPS)
    freezer.tick(60)
    with patch.object(module, "compute", new=delayed):
        try:
            result = await hass.config_entries.options.async_configure(
                fid, {"confirm": True}
            )
            assert result["type"] == "create_entry"
            assert await hass.async_add_executor_job(started.wait, 2)
            assert entry.runtime_data is coordinator
            during = hass.states.get("sensor.options_plan")
            assert during.state == before.state
            assert during.attributes["plan_retained"] is True
            assert during.attributes["refreshing"] is True
            assert (
                hass.states.get("sensor.options_optimizer_status").state
                == "calculating"
            )
            assert hass.states.get("binary_sensor.options_forecast_valid").state == "on"
        finally:
            release.set()
            await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data is coordinator
    after = hass.states.get("sensor.options_plan")
    assert after.attributes["refreshing"] is False
    assert after.attributes["generated_at"] != before.attributes["generated_at"]
    assert float(
        hass.states.get("sensor.options_consumption_cost").state
    ) == pytest.approx(0.4)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_tariff_save_does_not_unload_or_set_up_again(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-17T10:00:00+00:00")
    entry = await _setup(hass, _config())
    fid = await _walk(hass, entry, TARIFF_STEPS)
    with (
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload,
    ):
        await hass.config_entries.options.async_configure(fid, {"confirm": True})
        await hass.async_block_till_done(wait_background_tasks=True)
    reload.assert_not_called()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_entity_gating_flag_save_still_reloads(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-17T10:00:00+00:00")
    entry = await _setup(hass, _config())
    before = entry.runtime_data
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    for step in (
        {"next_step_id": "show_expert"},
        {"next_step_id": "presentation"},
        {"expose_costs": False, "expose_windows": True},
        {"next_step_id": "preview"},
        {"confirm": True},
    ):
        await hass.config_entries.options.async_configure(fid, step)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data is not before
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_source_binding_change_moves_the_state_listener(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-17T10:00:00+00:00")
    config = _config()
    config["helpers"]["grid_import_kw"] = {
        "entity": {"entity_id": "input_number.old"},
        "unit": "kW",
        "max_age_seconds": None,
    }
    for name in ("old", "new"):
        hass.states.async_set(
            f"input_number.{name}", "5", {"unit_of_measurement": "kW"}
        )
    entry = await _setup(hass, config)
    coordinator = entry.runtime_data
    moved = deepcopy(config)
    moved["helpers"]["grid_import_kw"]["entity"]["entity_id"] = "input_number.new"
    await coordinator.async_apply_configuration(moved)
    await hass.async_block_till_done(wait_background_tasks=True)
    generation = coordinator._generation
    hass.states.async_set("input_number.old", "6", {"unit_of_measurement": "kW"})
    await hass.async_block_till_done()
    assert coordinator._generation == generation
    hass.states.async_set("input_number.new", "6", {"unit_of_measurement": "kW"})
    await hass.async_block_till_done()
    assert coordinator._generation > generation
    assert await hass.config_entries.async_unload(entry.entry_id)
