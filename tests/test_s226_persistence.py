"""S-226 review follow-up (T-894): the municipality found survives a restart (ADR-0045 §15).

Not part of the QA acceptance file. Covers what the review asked for beyond it: the result is
kept in the integration's storage with the coordinates and the geometry version it was found
for, a restart reads only the geometry version, state is kept per Atlas host, and a lookup
never follows a redirect to another host.
"""

import json
import os
import stat
from typing import ClassVar
from urllib.parse import urlparse

import aiohttp
import pytest
from test_s226_municipality_lookup import (
    BASE,
    BERLIN,
    MAZOWIECKIE,
    SOPOT,
    VERSION,
    Atlas,
    _enabled_entry,
    _home,
    _lookup,
    _setup,
)

from custom_components.energy_compass.atlas import bridge as bridge_module
from custom_components.energy_compass.atlas import location
from custom_components.energy_compass.atlas.storage import (
    environment_dir,
    forget_environment,
)
from custom_components.energy_compass.atlas_env import BASE_URLS

PRODUCTION = BASE_URLS["production"]


@pytest.fixture
def atlas(aioclient_mock):
    return Atlas(aioclient_mock)


@pytest.fixture
def directory(tmp_path):
    return tmp_path


def _restart(hass):
    """What a Home Assistant restart does to the lookup: everything in memory is gone."""
    hass.data.pop(location._DATA_KEY, None)


async def _lookup_stored(hass, directory, lat, lng, base=BASE):
    _home(hass, lat, lng)
    return await location.home_municipality(hass, base, directory)


def _record(directory):
    return json.loads((directory / location.STORE_FILE).read_text())


# ---------------------------------------------------------------------------- the store file


async def test_found_municipality_is_stored_with_coordinates_and_version(
    hass, atlas, directory
):
    assert (
        await _lookup_stored(hass, directory, SOPOT["lat"], SOPOT["lng"]) == "2264011"
    )
    assert _record(directory) == {
        "latitude": SOPOT["lat"],
        "longitude": SOPOT["lng"],
        "version": VERSION,
        "municipality": "2264011",
    }
    assert stat.S_IMODE(os.stat(directory / location.STORE_FILE).st_mode) == 0o600
    assert [p.name for p in directory.iterdir()] == [location.STORE_FILE]


async def test_no_land_municipality_is_stored_as_null(hass, atlas, directory):
    assert await _lookup_stored(hass, directory, BERLIN["lat"], BERLIN["lng"]) is None
    assert _record(directory)["municipality"] is None
    _restart(hass)
    atlas.reset_calls()
    assert await location.home_municipality(hass, BASE, directory) is None
    assert atlas.paths() == ["/v1/public/geo"]  # not looked up again


async def test_without_a_directory_nothing_is_written(hass, atlas, directory):
    assert await _lookup(hass, SOPOT["lat"], SOPOT["lng"]) == "2264011"
    assert list(directory.iterdir()) == []


async def test_unchanged_result_is_not_written_again(hass, atlas, directory):
    assert (
        await _lookup_stored(hass, directory, SOPOT["lat"], SOPOT["lng"]) == "2264011"
    )
    path = directory / location.STORE_FILE
    os.utime(path, (1, 1))
    assert await location.home_municipality(hass, BASE, directory) == "2264011"
    assert path.stat().st_mtime == 1


async def test_a_missing_directory_does_not_break_the_lookup(hass, atlas, tmp_path):
    gone = tmp_path / "forgotten"
    assert await _lookup_stored(hass, gone, SOPOT["lat"], SOPOT["lng"]) == "2264011"
    assert not gone.exists()


async def test_forgetting_the_environment_removes_the_stored_municipality(
    hass, atlas, tmp_path
):
    directory = environment_dir(hass, "entry-1", "staging")
    directory.mkdir(parents=True)
    assert (
        await _lookup_stored(hass, directory, SOPOT["lat"], SOPOT["lng"]) == "2264011"
    )
    assert (directory / location.STORE_FILE).exists()
    forget_environment(hass, "entry-1", "staging")
    assert not directory.exists()


# ---------------------------------------------------------------------------- after a restart


async def test_restart_with_atlas_failing_keeps_the_municipality_found_before(
    hass, atlas, directory
):
    assert (
        await _lookup_stored(hass, directory, SOPOT["lat"], SOPOT["lng"]) == "2264011"
    )
    _restart(hass)
    atlas.fail("all", exc=aiohttp.ClientConnectionError("down"))
    assert await location.stored_municipality(hass, BASE, directory) == "2264011"
    assert await location.home_municipality(hass, BASE, directory) == "2264011"
    assert atlas.paths() == ["/v1/public/geo"]  # tried once, then the stored result


async def test_restart_with_unchanged_version_reads_only_the_version(
    hass, atlas, directory
):
    assert (
        await _lookup_stored(hass, directory, SOPOT["lat"], SOPOT["lng"]) == "2264011"
    )
    _restart(hass)
    atlas.reset_calls()
    assert await location.home_municipality(hass, BASE, directory) == "2264011"
    assert atlas.paths() == ["/v1/public/geo"]


async def test_restart_with_a_new_version_looks_the_home_up_again(
    hass, aioclient_mock, directory
):
    Atlas(aioclient_mock)
    assert (
        await _lookup_stored(hass, directory, SOPOT["lat"], SOPOT["lng"]) == "2264011"
    )
    _restart(hass)
    newer = Atlas(aioclient_mock, version="0123456789ab")
    assert await location.home_municipality(hass, BASE, directory) == "2264011"
    assert "/geo/0123456789ab/22-municipalities.topo.json" in newer.paths()
    assert _record(directory)["version"] == "0123456789ab"


async def test_stored_result_for_other_coordinates_is_not_used(hass, atlas, directory):
    assert (
        await _lookup_stored(hass, directory, SOPOT["lat"], SOPOT["lng"]) == "2264011"
    )
    _restart(hass)
    _home(hass, MAZOWIECKIE["lat"], MAZOWIECKIE["lng"])
    assert await location.stored_municipality(hass, BASE, directory) is None
    atlas.fail("all", exc=aiohttp.ClientConnectionError("down"))
    assert await location.home_municipality(hass, BASE, directory) is None


CORRUPT = {
    "empty": "",
    "not_json": "{oops",
    "not_an_object": "[1, 2]",
    "missing_keys": '{"latitude": 54.4}',
    "text_coordinates": json.dumps(
        {"latitude": "54", "longitude": "18", "version": VERSION, "municipality": None}
    ),
    "bad_version": json.dumps(
        {
            "latitude": SOPOT["lat"],
            "longitude": SOPOT["lng"],
            "version": "../etc",
            "municipality": "2264011",
        }
    ),
    "bad_municipality": json.dumps(
        {
            "latitude": SOPOT["lat"],
            "longitude": SOPOT["lng"],
            "version": VERSION,
            "municipality": "gdansk",
        }
    ),
}


@pytest.mark.parametrize("content", CORRUPT.values(), ids=CORRUPT.keys())
async def test_corrupt_store_file_is_nothing_found(hass, atlas, directory, content):
    (directory / location.STORE_FILE).write_text(content)
    _home(hass, SOPOT["lat"], SOPOT["lng"])
    assert await location.stored_municipality(hass, BASE, directory) is None
    atlas.fail("all", exc=aiohttp.ClientConnectionError("down"))
    assert await location.home_municipality(hass, BASE, directory) is None
    atlas.install()
    _restart(hass)  # the failed check's back-off is gone with the memory
    assert await location.home_municipality(hass, BASE, directory) == "2264011"
    assert _record(directory)["municipality"] == "2264011"  # replaced by the new result


async def test_missing_store_file_is_nothing_found(hass, atlas, directory):
    _home(hass, SOPOT["lat"], SOPOT["lng"])
    assert await location.stored_municipality(hass, BASE, directory) is None
    assert await location.stored_municipality(hass, BASE, directory / "absent") is None


# ---------------------------------------------------------------------------- per Atlas host


async def test_state_is_kept_per_atlas_host(hass, aioclient_mock):
    Atlas(aioclient_mock)
    assert await _lookup(hass, SOPOT["lat"], SOPOT["lng"]) == "2264011"
    production = Atlas(aioclient_mock, base=PRODUCTION)
    assert await location.home_municipality(hass, PRODUCTION) == "2264011"
    paths = production.paths()
    assert "/v1/public/geo" in paths and f"/geo/{VERSION}/pl.topo.json" in paths


async def test_a_failure_on_one_host_does_not_hold_back_the_other(hass, aioclient_mock):
    staging = Atlas(aioclient_mock)
    staging.fail("geo", exc=aiohttp.ClientConnectionError("down"))
    assert await _lookup(hass, SOPOT["lat"], SOPOT["lng"]) is None
    Atlas(aioclient_mock, base=PRODUCTION)
    assert await location.home_municipality(hass, PRODUCTION) == "2264011"


# ---------------------------------------------------------------------------- no redirects


class _RecordingSession:
    def __init__(self, session, calls):
        self._session = session
        self._calls = calls

    def get(self, url, **kwargs):
        self._calls.append((url, kwargs))
        return self._session.get(url, **kwargs)


@pytest.fixture
def get_calls(monkeypatch):
    calls = []
    real = location.async_get_clientsession
    monkeypatch.setattr(
        location,
        "async_get_clientsession",
        lambda hass: _RecordingSession(real(hass), calls),
    )
    return calls


async def test_no_request_follows_a_redirect(hass, atlas, get_calls):
    assert await _lookup(hass, SOPOT["lat"], SOPOT["lng"]) == "2264011"
    assert len(get_calls) == 3
    assert all(kwargs["allow_redirects"] is False for _url, kwargs in get_calls)


@pytest.mark.parametrize("route", ["geo", "pl", "22"])
async def test_a_redirect_to_another_host_ends_the_lookup(hass, atlas, route):
    atlas.fail(
        route,
        status=302,
        headers={"Location": "https://elsewhere.example/geo/x.topo.json"},
    )
    assert await _lookup(hass, SOPOT["lat"], SOPOT["lng"]) is None
    assert {urlparse(u).netloc for _m, u in atlas.requests()} == {urlparse(BASE).netloc}


# ---------------------------------------------------------------------------- Home Assistant restart


class _RecordingSink:
    """Stands in for the delivery thread; keeps every attribute set it was given."""

    instances: ClassVar[list] = []

    def __init__(self, dir, base_url, attrs):
        self.history = [attrs]
        type(self).instances.append(self)

    def start(self):
        pass

    def feed(self, *a):
        pass

    def set_attrs(self, attrs):
        self.history.append(attrs)

    def stop(self, timeout):
        pass


@pytest.fixture
def sink(monkeypatch):
    _RecordingSink.instances = []
    monkeypatch.setattr(bridge_module, "SinkThread", _RecordingSink)
    return _RecordingSink


async def test_restart_starts_the_sink_with_the_stored_municipality(
    recorder_mock, hass, enable_custom_integrations, atlas, sink
):
    _home(hass, SOPOT["lat"], SOPOT["lng"])
    entry = _enabled_entry(hass)
    await _setup(hass, entry)
    first = sink.instances[-1]
    assert first.history[-1]["location"] == {"municipality": "2264011"}
    assert (
        environment_dir(hass, entry.entry_id, "staging") / location.STORE_FILE
    ).exists()
    assert await hass.config_entries.async_unload(entry.entry_id)

    _restart(hass)
    atlas.fail("all", exc=aiohttp.ClientConnectionError("down"))
    await _setup(hass, entry)
    second = sink.instances[-1]
    assert second is not first
    assert second.history[0]["location"] == {"municipality": "2264011"}
    assert len(second.history) == 1, "attributes changed after the sink started"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_restart_with_unchanged_version_sends_no_new_attribute_version(
    recorder_mock, hass, enable_custom_integrations, atlas, sink
):
    _home(hass, SOPOT["lat"], SOPOT["lng"])
    entry = _enabled_entry(hass)
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)

    _restart(hass)
    atlas.reset_calls()
    await _setup(hass, entry)
    assert atlas.paths() == ["/v1/public/geo"]
    restarted = sink.instances[-1]
    assert len(restarted.history) == 1
    assert restarted.history[0]["location"] == {"municipality": "2264011"}
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_restart_with_a_corrupt_store_file_starts_without_location(
    recorder_mock, hass, enable_custom_integrations, atlas, sink
):
    _home(hass, SOPOT["lat"], SOPOT["lng"])
    entry = _enabled_entry(hass)
    (environment_dir(hass, entry.entry_id, "staging") / location.STORE_FILE).write_text(
        "{oops"
    )
    atlas.fail("all", exc=aiohttp.ClientConnectionError("down"))
    await _setup(hass, entry)
    assert "location" not in sink.instances[-1].history[0]
    assert await hass.config_entries.async_unload(entry.entry_id)
