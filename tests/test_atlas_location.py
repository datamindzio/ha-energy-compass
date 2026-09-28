"""Home zone → H3 res-6 cell for Atlas attributes (T-500, ADR-0019 §6, ADR-0022)."""

import re

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.atlas import location
from custom_components.energy_compass.atlas.location import h3_res6
from custom_components.energy_compass.atlas import bridge as bridge_module
from custom_components.energy_compass.atlas.storage import environment_dir
from custom_components.energy_compass.settings import DOMAIN, default_configuration

CELL = re.compile(r"^86[0-9a-f]{13}$")
WARSAW = (52.2297, 21.0122)


def test_home_cell_is_res6_and_stable():
    cell = h3_res6(*WARSAW)
    assert CELL.match(cell)
    assert h3_res6(*WARSAW) == cell
    # nearby point in the same ~36 km² hexagon shares the cell: only the cell leaves HA
    assert h3_res6(52.2301, 21.0118) == cell


@pytest.mark.parametrize(
    "lat,lon",
    [
        (None, 21.0),
        (52.0, None),
        (float("nan"), 21.0),
        (52.0, float("inf")),
        (91.0, 21.0),
        (52.0, 181.0),
        ("52.2", "21.0"),
    ],
)
def test_invalid_coordinates_give_no_cell(lat, lon):
    assert h3_res6(lat, lon) is None


def test_missing_h3_gives_no_cell_and_no_coordinates_in_log(monkeypatch, caplog):
    monkeypatch.setitem(__import__("sys").modules, "h3", None)
    assert h3_res6(*WARSAW) is None
    assert "52.2297" not in caplog.text and "21.0122" not in caplog.text


async def test_async_home_cell_uses_ha_home_coordinates(hass):
    hass.config.latitude, hass.config.longitude = WARSAW
    assert await location.async_home_h3_res6(hass) == h3_res6(*WARSAW)


class _CapturingSink:
    attrs = None

    def __init__(self, dir, base_url, attrs):
        type(self).attrs = attrs

    def start(self):
        pass

    def feed(self, *a):
        pass

    def stop(self, timeout):
        pass


def _entry(hass, options):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=default_configuration("EUR", "UTC"),
        options=options,
        version=2,
    )
    entry.add_to_hass(hass)
    return entry


async def test_enabled_setup_sends_location_cell_only(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    _CapturingSink.attrs = None
    monkeypatch.setattr(bridge_module, "SinkThread", _CapturingSink)
    hass.config.latitude, hass.config.longitude = WARSAW
    entry = _entry(
        hass,
        {"atlas": {"enabled": True, "environment": "staging", "pv_kwp": 5.0}},
    )
    directory = environment_dir(hass, entry.entry_id, "staging")
    directory.mkdir(parents=True)
    (directory / "site.json").write_text('{"site_id": "site-1"}')

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    attrs = _CapturingSink.attrs
    assert attrs["location"] == {"h3_res6": h3_res6(*WARSAW)}
    assert "52.22" not in repr(attrs) and "21.01" not in repr(attrs)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_off_path_never_computes_the_location(
    recorder_mock, hass, enable_custom_integrations, monkeypatch
):
    calls = []
    monkeypatch.setattr(location, "h3_res6", lambda *a: calls.append(a))
    entry = _entry(hass, {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == []
    assert await hass.config_entries.async_unload(entry.entry_id)
