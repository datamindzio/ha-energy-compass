"""S-226 acceptance: Energy Compass sends the home's municipality (T-893).

Source of the criteria: energy-atlas docs/PRD-adminmap.md S-226, ADR-0045 §15, contract
1.4.0-adminmap (`tests/atlas_contract/openapi.yaml`, `getPublicGeo`).

Interface these tests pin (ADR-0045 §15 names `home_municipality`; the Atlas host is the one
thing the lookup needs besides `hass`, so it is the second argument):

* `atlas.location.home_municipality(hass, base_url)` — coroutine; returns the 7-digit municipality
  id of Home Assistant's home, or `None`. Never raises for bad coordinates or an Atlas that cannot
  be reached or read. Every call is one "attribute check". It keeps its state (result, coordinates,
  geometry version, last geo read, last failure) per `hass`.
* `atlas.attrs_builder.build_attrs(config, atlas_settings, municipality)` — the third argument is
  the municipality id (or `None`) and replaces the H3 cell; `location` is `{"municipality": id}`.
* All HTTP goes through Home Assistant's aiohttp client session (`aioclient_mock` records it);
  decoding runs off the event loop; standard library only.

The geometry files and shared points under `tests/fixtures/` are verbatim copies from the Atlas
repo (geometry version d053123129d9, `api/testvectors/municipality_points.json`).
"""

import ast
import asyncio
import json
import math
import re
import sys
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import pytest
from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)
from test_atlas_contract import schema_errors

from custom_components.energy_compass.atlas import bridge as bridge_module
from custom_components.energy_compass.atlas import location
from custom_components.energy_compass.atlas.attrs_builder import build_attrs
from custom_components.energy_compass.atlas.storage import environment_dir
from custom_components.energy_compass.atlas_env import BASE_URLS
from custom_components.energy_compass.settings import DOMAIN, default_configuration

# Red until the dev ticket T-894 lands. `tests/conftest.py` marks every test of this module that is
# not listed here as strict xfail: once the behaviour exists it XPASSes and fails CI, so T-894 must
# delete the `S226` block in `pytest_collection_modifyitems`
# when it implements the story. These four are invariant guards, green today and after.
GREEN_GUARDS = {
    "test_s226_shared_points_cover_land_none_and_all_voivodeships",
    "test_s226_build_attrs_without_municipality_has_no_location",
    "test_s226_no_requirement_is_added_and_the_lookup_is_standard_library_only",
    "test_s226_readmes_match_and_do_not_mention_h3",
}

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "energy_compass"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
VERSION = "d053123129d9"
GEO_DIR = FIXTURES / "geo" / VERSION
POINTS = json.loads((FIXTURES / "municipality_points.json").read_text())["cases"]
BASE = BASE_URLS["staging"]
HOST = urlparse(BASE).netloc

GEO_PATHS = {
    "voivodeships": f"/geo/{VERSION}/pl.topo.json",
    "counties": f"/geo/{VERSION}/{{vv}}-counties.topo.json",
    "municipalities": f"/geo/{VERSION}/{{vv}}-municipalities.topo.json",
}

# Ids found in the municipalities files of the shared points (voivodeship = first two digits).
BOLESLAWIEC = next(c for c in POINTS if c["municipality"] == "0201011")
SOPOT = next(c for c in POINTS if c["municipality"] == "2264011")
MAZOWIECKIE = next(c for c in POINTS if c["municipality"] == "1414063")
BERLIN = next(c for c in POINTS if c["name"].startswith("Berlin"))
BALTIC = next(c for c in POINTS if c["name"].startswith("Open Baltic"))
OFFSHORE = next(c for c in POINTS if c["name"] == "inside the offshore unit")


# ---------------------------------------------------------------------------- Atlas stand-in


class Atlas:
    """Serves `getPublicGeo` and the geometry files from the fixtures on `aioclient_mock`."""

    def __init__(self, mock, base=BASE, version=VERSION):
        self.mock = mock
        self.base = base
        self.version = version
        self.install()

    def geo_body(self):
        return {
            "version": self.version,
            "files": {
                key: path.replace(VERSION, self.version)
                for key, path in GEO_PATHS.items()
            },
        }

    def install(self, **overrides):
        """(Re)register every route; `overrides` maps a route name to mock-response kwargs."""
        self.mock.clear_requests()
        routes = {
            "geo": (f"{self.base}/v1/public/geo", {"json": self.geo_body()}),
            "pl": (
                f"{self.base}/geo/{self.version}/pl.topo.json",
                {"content": (GEO_DIR / "pl.topo.json").read_bytes()},
            ),
        }
        for vv in sorted({p.name[:2] for p in GEO_DIR.glob("*-municipalities*")}):
            routes[vv] = (
                f"{self.base}/geo/{self.version}/{vv}-municipalities.topo.json",
                {"content": (GEO_DIR / f"{vv}-municipalities.topo.json").read_bytes()},
            )
        for name, (url, kwargs) in routes.items():
            kwargs = dict(overrides.get(name, kwargs))
            kwargs.setdefault("headers", {"Content-Type": "application/json"})
            self.mock.get(url, **kwargs)

    def fail(self, route, **kwargs):
        """Make one route (or `"all"`) fail; `kwargs` are mock-response kwargs."""
        names = ["geo", "pl", *[f"{n:02d}" for n in range(2, 34, 2)]]
        self.install(**{n: kwargs for n in names if route in (n, "all")})

    def requests(self):
        return [(m, str(u)) for m, u, _d, _h in self.mock.mock_calls]

    def paths(self):
        return [urlparse(u).path for _m, u in self.requests()]

    def reset_calls(self):
        self.mock.mock_calls.clear()


@pytest.fixture
def atlas(aioclient_mock):
    return Atlas(aioclient_mock)


def _home(hass, lat, lng):
    hass.config.latitude, hass.config.longitude = lat, lng


async def _lookup(hass, lat, lng, base=BASE):
    _home(hass, lat, lng)
    return await location.home_municipality(hass, base)


# ---------------------------------------------------------------------------- criterion 2


@pytest.mark.parametrize(
    "case", POINTS, ids=[f"{i:02d}-{c['name']}" for i, c in enumerate(POINTS)]
)
async def test_s226_shared_points_resolve_to_the_listed_id(hass, atlas, case):
    got = await _lookup(hass, case["lat"], case["lng"])
    assert got == case["municipality"], case["name"]


async def test_s226_shared_points_cover_land_none_and_all_voivodeships():
    assert len(POINTS) == 68
    assert {c["municipality"][:2] for c in POINTS if c["municipality"]} >= {
        f"{n:02d}" for n in range(2, 34, 2)
    }
    assert sum(c["municipality"] is None for c in POINTS) == 6


async def test_s226_all_points_in_one_session_reuse_the_cached_files(hass, atlas):
    """Looking up different coordinates later gives each its own id (cache is keyed on them)."""
    for case in POINTS[:20] + POINTS[-6:]:
        assert await _lookup(hass, case["lat"], case["lng"]) == case["municipality"], (
            case["name"]
        )


# ---------------------------------------------------------------------------- criterion 1


def _enabled_entry(hass, environment="staging"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=default_configuration("EUR", "UTC"),
        options={"atlas": {"enabled": True, "environment": environment, "pv_kwp": 5.0}},
        version=2,
    )
    entry.add_to_hass(hass)
    directory = environment_dir(hass, entry.entry_id, environment)
    directory.mkdir(parents=True)
    (directory / "site.json").write_text('{"site_id": "site-1"}')
    return entry


class _CapturingSink:
    """Stands in for the delivery thread; keeps the newest attributes it was given."""

    attrs = None
    constructed = 0

    def __init__(self, dir, base_url, attrs):
        type(self).attrs = attrs
        type(self).constructed += 1
        type(self).base_url = base_url

    def start(self):
        pass

    def feed(self, *a):
        pass

    def set_attrs(self, attrs):
        type(self).attrs = attrs

    def stop(self, timeout):
        pass


@pytest.fixture
def sink(monkeypatch):
    _CapturingSink.attrs = None
    _CapturingSink.constructed = 0
    monkeypatch.setattr(bridge_module, "SinkThread", _CapturingSink)
    return _CapturingSink


async def _setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_s226_enabled_setup_sends_municipality_only_valid_against_attrs_v1(
    recorder_mock, hass, enable_custom_integrations, atlas, sink
):
    _home(hass, SOPOT["lat"], SOPOT["lng"])
    entry = _enabled_entry(hass)
    await _setup(hass, entry)

    attrs = sink.attrs
    assert attrs["location"] == {"municipality": "2264011"}
    assert "h3_res6" not in json.dumps(attrs)
    assert str(SOPOT["lat"]) not in json.dumps(attrs)
    assert schema_errors("AttrsV1", attrs) == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_s226_contract_is_1_4_0_adminmap_and_accepts_municipality_location():
    contract = (ROOT / "tests" / "atlas_contract" / "openapi.yaml").read_text()
    assert re.search(r"^  version: 1\.4\.0-adminmap\s*$", contract, re.MULTILINE)
    base = build_attrs(default_configuration("EUR", "UTC"), {"pv_kwp": 5.0})
    assert schema_errors("AttrsV1", base) == []
    with_location = build_attrs(
        default_configuration("EUR", "UTC"), {"pv_kwp": 5.0}, "2264011"
    )
    assert with_location["location"] == {"municipality": "2264011"}
    assert schema_errors("AttrsV1", with_location) == []


# ---------------------------------------------------------------------------- criterion 3


def _assert_no_coordinates(lat, lng, text):
    for needle in (
        str(lat),
        str(lng),
        f"{lat:.2f}",
        f"{lng:.2f}",
        f"{lat:.1f}",
        f"{lng:.1f}",
    ):
        assert needle not in text, f"{needle} leaked in {text}"


@pytest.mark.parametrize(
    "case", [SOPOT, BOLESLAWIEC, MAZOWIECKIE], ids=lambda c: c["name"]
)
async def test_s226_requests_carry_no_coordinates_and_only_the_voivodeship_id(
    hass, aioclient_mock, case
):
    atlas = Atlas(aioclient_mock)
    assert await _lookup(hass, case["lat"], case["lng"]) == case["municipality"]

    calls = aioclient_mock.mock_calls
    assert calls, "the lookup made no request"
    vv = case["municipality"][:2]
    allowed = {
        "/v1/public/geo",
        f"/geo/{VERSION}/pl.topo.json",
        f"/geo/{VERSION}/{vv}-municipalities.topo.json",
    }
    for method, url, data, headers in calls:
        parsed = urlparse(str(url))
        assert method.upper() == "GET"
        assert parsed.scheme == "https" and parsed.netloc == HOST
        assert parsed.path in allowed, parsed.path
        assert parsed.query == "" and parsed.fragment == ""
        assert not data
        _assert_no_coordinates(
            case["lat"], case["lng"], repr((str(url), data, headers))
        )
    assert atlas.paths().count(f"/geo/{VERSION}/{vv}-municipalities.topo.json") == 1
    other = [
        p
        for p in atlas.paths()
        if p.endswith("-municipalities.topo.json") and vv not in p
    ]
    assert other == [], "only the home's voivodeship file may be fetched"


@pytest.mark.parametrize("environment", ["staging", "production"])
async def test_s226_setup_requests_go_to_the_configured_atlas_host_only(
    recorder_mock, hass, enable_custom_integrations, aioclient_mock, sink, environment
):
    base = BASE_URLS[environment]
    atlas = Atlas(aioclient_mock, base=base)
    _home(hass, SOPOT["lat"], SOPOT["lng"])
    entry = _enabled_entry(hass, environment)
    await _setup(hass, entry)

    assert sink.attrs["location"] == {"municipality": "2264011"}
    hosts = {urlparse(u).netloc for _m, u in atlas.requests()}
    assert hosts == {urlparse(base).netloc}
    for _m, url in atlas.requests():
        _assert_no_coordinates(SOPOT["lat"], SOPOT["lng"], url)
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------- criterion 4


@pytest.mark.parametrize("case", [BERLIN, BALTIC, OFFSHORE], ids=lambda c: c["name"])
async def test_s226_no_land_municipality_gives_none(hass, atlas, case):
    assert await _lookup(hass, case["lat"], case["lng"]) is None


@pytest.mark.parametrize(
    "lat,lng",
    [(None, 21.0), (52.0, None), (None, None), (float("nan"), 21.0), (52.0, math.inf)],
)
async def test_s226_missing_or_invalid_coordinates_give_none_without_error(
    hass, atlas, lat, lng
):
    assert await _lookup(hass, lat, lng) is None
    vv_files = [p for p in atlas.paths() if "-municipalities" in p]
    assert vv_files == []


async def test_s226_no_location_when_no_municipality(
    recorder_mock, hass, enable_custom_integrations, atlas, sink
):
    _home(hass, BALTIC["lat"], BALTIC["lng"])
    entry = _enabled_entry(hass)
    await _setup(hass, entry)
    assert "location" not in sink.attrs
    assert schema_errors("AttrsV1", sink.attrs) == []
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_s226_build_attrs_without_municipality_has_no_location():
    config = default_configuration("EUR", "UTC")
    assert "location" not in build_attrs(config, {"pv_kwp": 5.0})
    assert "location" not in build_attrs(config, {"pv_kwp": 5.0}, None)


# ---------------------------------------------------------------------------- criterion 5

FAILURES = {
    "atlas_unreachable": ("geo", {"exc": aiohttp.ClientConnectionError("down")}),
    "geo_503": ("geo", {"status": 503}),
    "geo_malformed": ("geo", {"text": "{not json"}),
    "geo_wrong_shape": ("geo", {"json": {"version": 1}}),
    "pl_missing": ("pl", {"status": 404}),
    "pl_malformed": ("pl", {"content": b'{"type":"Topology","objects":'}),
    "pl_wrong_shape": ("pl", {"json": {"type": "Topology", "objects": {}}}),
    "municipalities_missing": ("14", {"status": 404}),
    "municipalities_malformed": ("14", {"content": b"\x00\x01 garbage"}),
    "municipalities_wrong_shape": ("14", {"json": {"type": "Topology", "arcs": "x"}}),
    "all_unreachable": ("all", {"exc": aiohttp.ClientConnectionError("down")}),
}


@pytest.mark.parametrize("failure", FAILURES)
async def test_s226_failed_lookup_without_earlier_result_gives_none(
    hass, atlas, failure
):
    route, kwargs = FAILURES[failure]
    atlas.fail(route, **kwargs)
    assert await _lookup(hass, MAZOWIECKIE["lat"], MAZOWIECKIE["lng"]) is None


@pytest.mark.parametrize("failure", FAILURES)
async def test_s226_failed_lookup_keeps_the_municipality_last_found_for_the_same_home(
    hass, atlas, failure
):
    route, kwargs = FAILURES[failure]
    with freeze_time("2026-10-12T08:00:00+00:00") as clock:
        assert await _lookup(hass, MAZOWIECKIE["lat"], MAZOWIECKIE["lng"]) == "1414063"
        atlas.fail(route, **kwargs)
        # past every cache window (24 h geo read, hourly retry): a new lookup must be tried
        clock.tick(timedelta(hours=26))
        got = await location.home_municipality(hass, BASE)
        assert got == "1414063"
        clock.tick(timedelta(hours=2))
        assert await location.home_municipality(hass, BASE) == "1414063"


async def test_s226_failed_lookup_does_not_reuse_a_result_of_other_coordinates(
    hass, atlas
):
    with freeze_time("2026-10-12T08:00:00+00:00") as clock:
        assert await _lookup(hass, SOPOT["lat"], SOPOT["lng"]) == "2264011"
        atlas.fail("all", exc=aiohttp.ClientConnectionError("down"))
        clock.tick(timedelta(hours=2))
        assert await _lookup(hass, MAZOWIECKIE["lat"], MAZOWIECKIE["lng"]) is None
        # back at the first home the old result is the last one found for those coordinates
        clock.tick(timedelta(hours=2))
        assert await _lookup(hass, SOPOT["lat"], SOPOT["lng"]) == "2264011"


async def test_s226_failed_lookup_is_retried_at_most_once_per_hour(hass, atlas):
    atlas.fail("geo", exc=aiohttp.ClientConnectionError("down"))
    with freeze_time("2026-10-12T08:00:00+00:00") as clock:
        assert await _lookup(hass, SOPOT["lat"], SOPOT["lng"]) is None
        assert atlas.requests(), "first attribute check must try"
        atlas.reset_calls()

        for step in (10, 10, 9, 30):  # attribute checks at 10, 20, 29 and 59 minutes
            clock.tick(timedelta(minutes=step))
            assert await location.home_municipality(hass, BASE) is None
        assert atlas.requests() == [], "retried inside the hour"

        atlas.install()  # Atlas is back
        clock.tick(timedelta(minutes=2))  # 61 min after the failure
        assert await location.home_municipality(hass, BASE) == "2264011"
        assert atlas.requests(), "no retry after the hour"


async def test_s226_failed_retry_waits_another_hour(hass, atlas):
    atlas.fail("pl", status=404)
    with freeze_time("2026-10-12T08:00:00+00:00") as clock:
        assert await _lookup(hass, SOPOT["lat"], SOPOT["lng"]) is None
        clock.tick(timedelta(minutes=61))
        assert await location.home_municipality(hass, BASE) is None  # second try fails
        atlas.reset_calls()
        clock.tick(timedelta(minutes=30))
        assert await location.home_municipality(hass, BASE) is None
        assert atlas.requests() == []


async def test_s226_hanging_atlas_does_not_delay_telemetry_or_setup(
    recorder_mock, hass, enable_custom_integrations, aioclient_mock, sink
):
    atlas = Atlas(aioclient_mock)
    release = asyncio.Event()

    async def hang(method, url, data):
        await release.wait()
        return AiohttpClientMockResponse(method, url, status=503)

    atlas.install(geo={"side_effect": hang})
    _home(hass, SOPOT["lat"], SOPOT["lng"])
    entry = _enabled_entry(hass)
    try:
        await asyncio.wait_for(
            hass.config_entries.async_setup(entry.entry_id), timeout=10
        )
        assert sink.constructed == 1, "delivery did not start while the lookup hangs"
        assert "location" not in sink.attrs
    finally:
        release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------- criterion 6


async def test_s226_unchanged_home_and_version_fetch_nothing_again(hass, atlas):
    with freeze_time("2026-10-12T08:00:00+00:00") as clock:
        assert await _lookup(hass, SOPOT["lat"], SOPOT["lng"]) == "2264011"
        first = atlas.paths()
        assert first.count("/v1/public/geo") == 1
        assert f"/geo/{VERSION}/pl.topo.json" in first
        assert f"/geo/{VERSION}/22-municipalities.topo.json" in first
        atlas.reset_calls()

        for step in (1, 5, 6, 11):  # attribute checks at 1, 6, 12 and 23 hours
            clock.tick(timedelta(hours=step))
            assert await location.home_municipality(hass, BASE) == "2264011"
        assert atlas.requests() == []


async def test_s226_version_is_read_again_after_24_hours_and_files_stay_cached(
    hass, atlas
):
    with freeze_time("2026-10-12T08:00:00+00:00") as clock:
        assert await _lookup(hass, SOPOT["lat"], SOPOT["lng"]) == "2264011"
        atlas.reset_calls()
        clock.tick(timedelta(hours=25))
        assert await location.home_municipality(hass, BASE) == "2264011"
        assert atlas.paths() == ["/v1/public/geo"]
        atlas.reset_calls()
        assert await location.home_municipality(hass, BASE) == "2264011"
        assert atlas.requests() == []  # at most one geo read per 24 h


async def test_s226_new_geometry_version_looks_the_home_up_again(hass, aioclient_mock):
    atlas = Atlas(aioclient_mock)
    with freeze_time("2026-10-12T08:00:00+00:00") as clock:
        assert await _lookup(hass, SOPOT["lat"], SOPOT["lng"]) == "2264011"
        newer = Atlas(aioclient_mock, version="0123456789ab")
        clock.tick(timedelta(hours=25))
        assert await location.home_municipality(hass, BASE) == "2264011"
        paths = newer.paths()
        assert "/v1/public/geo" in paths
        assert "/geo/0123456789ab/pl.topo.json" in paths
        assert "/geo/0123456789ab/22-municipalities.topo.json" in paths
        assert not any(VERSION in p for p in paths)
    del atlas


async def test_s226_changed_home_coordinates_look_the_home_up_again(hass, atlas):
    assert await _lookup(hass, SOPOT["lat"], SOPOT["lng"]) == "2264011"
    atlas.reset_calls()
    assert await _lookup(hass, BOLESLAWIEC["lat"], BOLESLAWIEC["lng"]) == "0201011"
    assert "/v1/public/geo" not in atlas.paths(), (
        "version is read at start and per 24 h"
    )


async def test_s226_unchanged_municipality_builds_identical_attributes(hass, atlas):
    config = default_configuration("EUR", "UTC")
    first = await _lookup(hass, SOPOT["lat"], SOPOT["lng"])
    second = await location.home_municipality(hass, BASE)
    assert build_attrs(config, {"pv_kwp": 5.0}, first) == build_attrs(
        config, {"pv_kwp": 5.0}, second
    )


# ---------------------------------------------------------------------------- criterion 7


async def test_s226_largest_file_lookup_with_nothing_cached_is_within_one_second(
    hass, atlas
):
    _home(hass, MAZOWIECKIE["lat"], MAZOWIECKIE["lng"])
    started = time.perf_counter()
    got = await location.home_municipality(hass, BASE)
    elapsed = time.perf_counter() - started
    assert got == "1414063"
    assert elapsed <= 1.0, f"lookup took {elapsed:.3f} s"


async def test_s226_lookup_does_not_block_the_event_loop(hass, atlas):
    _home(hass, MAZOWIECKIE["lat"], MAZOWIECKIE["lng"])
    gaps = []
    stop = asyncio.Event()

    async def ticker():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    gaps.clear()
    try:
        assert await location.home_municipality(hass, BASE) == "1414063"
    finally:
        stop.set()
        await task
    assert gaps, "the loop never ran during the lookup"
    assert max(gaps) < 0.1, f"event loop stalled for {max(gaps):.3f} s"


# ---------------------------------------------------------------------------- criterion 8


def test_s226_h3_port_and_its_tests_are_gone():
    assert not (PACKAGE / "atlas" / "h3_res6.py").exists()
    assert not (ROOT / "tests" / "test_atlas_h3_res6.py").exists()
    assert not hasattr(location, "h3_res6") and not hasattr(location, "home_h3_res6")


def test_s226_nothing_computes_or_sends_an_h3_index():
    pattern = re.compile(r"h3_res6|\bh3\b|\bH3\b|h3_index", re.IGNORECASE)
    offenders = []
    for path in PACKAGE.rglob("*"):
        if path.suffix in {".py", ".json", ".yaml"} and "brand" not in path.parts:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert offenders == []


def test_s226_no_requirement_is_added_and_the_lookup_is_standard_library_only():
    manifest = json.loads((PACKAGE / "manifest.json").read_text())
    assert manifest["requirements"] == ["scipy==1.18.1"]

    source = (PACKAGE / "atlas" / "location.py").read_text()
    allowed_extra = {"homeassistant", "aiohttp", "custom_components"}
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        for name in names:
            assert name in sys.stdlib_module_names or name in allowed_extra, name


async def test_s226_off_path_makes_no_request(
    recorder_mock, hass, enable_custom_integrations, aioclient_mock, monkeypatch
):
    calls = []

    async def spy(*a, **k):
        calls.append(a)

    monkeypatch.setattr(location, "home_municipality", spy)
    entry = MockConfigEntry(
        domain=DOMAIN, data=default_configuration("EUR", "UTC"), options={}, version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == []
    assert aioclient_mock.mock_calls == []
    assert await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------- criterion 9


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\s*$", text, re.MULTILINE)
    assert match, f"heading {heading!r} missing"
    rest = text[match.end() :]
    following = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: following.start()] if following else rest


def test_s226_guide_en_explains_the_shared_municipality():
    section = _section(
        (ROOT / "docs/guide.en.md").read_text(), "Energy Atlas (optional)"
    )
    flat = " ".join(section.split())
    assert "municipality" in flat and "gmina" in flat
    assert "by name" in flat
    assert re.search(r"about 50 m|within 50 m|approximately 50 m|~50 m", flat)
    assert re.search(r"neighbou?ring municipality", flat)
    assert re.search(r"coordinates .{0,60}(stay|remain|never leave)", flat)
    assert not re.search(r"\bH3\b|hexagon", flat)


def test_s226_guide_pl_explains_the_shared_municipality():
    section = _section(
        (ROOT / "docs/guide.pl.md").read_text(), "Energy Atlas (opcjonalnie)"
    )
    flat = " ".join(section.split())
    assert "gmin" in flat
    assert "nazw" in flat
    assert re.search(r"(ok\.|około|do) 50 m", flat)
    assert re.search(r"sąsiedni", flat)
    assert re.search(r"współrzędn", flat)
    assert not re.search(r"\bH3\b|sześciokąt", flat)


def test_s226_readmes_match_and_do_not_mention_h3():
    en = (ROOT / "README.md").read_text()
    pl = (ROOT / "README.pl.md").read_text()
    for text in (en, pl):
        assert not re.search(r"\bH3\b|h3_res6", text)
    # where either README describes what is shared, both name the municipality
    en_shared = bool(re.search(r"municipalit", en, re.IGNORECASE))
    pl_shared = bool(re.search(r"gmin", pl, re.IGNORECASE))
    assert en_shared == pl_shared


def test_s226_docs_never_describe_the_h3_cell_as_shared():
    for name in (
        "docs/guide.en.md",
        "docs/guide.pl.md",
        "docs/index.html",
        "docs/installation.md",
    ):
        text = (ROOT / name).read_text()
        assert not re.search(
            r"H3 (resolution|rozdzielczo)|komórk\w+ H3|h3_res6", text
        ), name
