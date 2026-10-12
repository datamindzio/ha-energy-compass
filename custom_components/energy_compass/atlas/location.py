"""Home location as a municipality id, found inside Home Assistant (ADR-0045 §15, S-226).

The home's coordinates never leave Home Assistant. `GET /v1/public/geo` names the geometry
files (TopoJSON, public, the same for every home); `pl.topo.json` tells which voivodeship the
home lies in and that voivodeship's municipalities file which municipality. The only things
derived from the coordinates that leave are the voivodeship id in the file name of the
second request and, in the attributes, the municipality id.

`home_municipality` is one "attribute check": it keeps the municipality last found per
coordinates, reads the geometry version at most once per 24 h, and after a failure waits an
hour before trying again. State is kept per Atlas host. Given a `directory`, the result is
also written there (the coordinates, the geometry version, the municipality or null) and
`stored_municipality` reads it back after a restart, so a restart neither fetches the files
again nor loses the municipality while Atlas cannot be reached. Standard library plus Home
Assistant's own aiohttp session.
"""

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

GEO_READ_SECONDS = 24 * 3600
RETRY_SECONDS = 3600
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
_DATA_KEY = "energy_compass_atlas_location"
_VERSION = re.compile(r"^[0-9a-f]{12}$")
_VOIVODESHIP = re.compile(r"^[0-9]{2}$")
_MUNICIPALITY = re.compile(r"^[0-9]{7}$")
_KEPT_HOMES = 8
STORE_FILE = "municipality.json"


class _LookupError(Exception):
    """The lookup could not finish (unreachable, missing, malformed or wrongly shaped)."""


@dataclass
class _State:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    version: str | None = None
    files: dict | None = None
    geo_read_at: float = 0.0
    failed_at: float | None = None
    # coordinates -> (geometry version, municipality id or None), newest last
    found: dict = field(default_factory=dict)


def _valid(latitude, longitude) -> bool:
    for value in (latitude, longitude):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if not math.isfinite(value):
            return False
    return -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0


def _state(hass: HomeAssistant, base_url: str) -> _State:
    return hass.data.setdefault(_DATA_KEY, {}).setdefault(base_url, _State())


def _home(hass: HomeAssistant):
    latitude, longitude = hass.config.latitude, hass.config.longitude
    if not _valid(latitude, longitude):
        return None
    return float(latitude), float(longitude)


async def stored_municipality(
    hass: HomeAssistant, base_url: str, directory: Path
) -> str | None:
    """Municipality stored in `directory` for the home's current coordinates, or None.

    Reads one small local file, never the network and never waits for a running lookup. A missing, unreadable or malformed file,
    or one stored for other coordinates, is nothing found.
    """
    key = _home(hass)
    if key is None:
        return None
    known = await _adopt_stored(hass, _state(hass, base_url), key, directory)
    return known[1] if known else None


async def _adopt_stored(hass, state: _State, key, directory: Path) -> tuple | None:
    """What this home last found: in memory, else what `directory` holds for these coordinates."""
    known = state.found.get(key)
    if known is None:
        stored = await hass.async_add_executor_job(_load, directory)
        if stored is not None and stored[0] == key:
            known = state.found.setdefault(key, stored[1])
    return known


async def home_municipality(
    hass: HomeAssistant, base_url: str, directory: Path | None = None
) -> str | None:
    """Municipality id (7 digits) of Home Assistant's home, or None.

    None means no land municipality or nothing found yet. Never raises for bad
    coordinates or an Atlas that cannot be reached or read. With a `directory` a new
    result is stored there.
    """
    key = _home(hass)
    if key is None:
        return None
    state = _state(hass, base_url)
    async with state.lock:
        before = (
            state.found.get(key)
            if directory is None
            else await _adopt_stored(hass, state, key, directory)
        )
        try:
            found = await _check(hass, base_url, state, key)
        except _LookupError:
            state.failed_at = time.time()
            known = state.found.get(key)
            return known[1] if known else None
        after = state.found.get(key)
        if directory is not None and after is not None and after != before:
            await hass.async_add_executor_job(_save, directory, key, after)
        return found


def _load(directory: Path) -> tuple | None:
    """`((latitude, longitude), (version, municipality or None))` stored in `directory`."""
    try:
        record = json.loads((directory / STORE_FILE).read_bytes())
        latitude, longitude = record["latitude"], record["longitude"]
        version, municipality = record["version"], record["municipality"]
        if not (
            _valid(latitude, longitude)
            and isinstance(version, str)
            and _VERSION.match(version)
            and (
                municipality is None
                or (isinstance(municipality, str) and _MUNICIPALITY.match(municipality))
            )
        ):
            return None
    except OSError, ValueError, KeyError, TypeError:
        return None
    return (float(latitude), float(longitude)), (version, municipality)


def _save(directory: Path, key, found: tuple) -> None:
    """Write the result atomically; a directory that is gone or unwritable is skipped."""
    record = {
        "latitude": key[0],
        "longitude": key[1],
        "version": found[0],
        "municipality": found[1],
    }
    path = directory / STORE_FILE
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{STORE_FILE}.")
    except OSError:
        _LOGGER.debug("Energy Atlas municipality not stored: %s", directory)
        return
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(record, handle)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        _LOGGER.debug("Energy Atlas municipality not stored: %s", directory)


async def _check(hass, base_url, state: _State, key) -> str | None:
    now = time.time()
    known = state.found.get(key)
    fallback = known[1] if known else None
    geo_fresh = state.files is not None and now - state.geo_read_at < GEO_READ_SECONDS
    if known and geo_fresh and known[0] == state.version:
        return known[1]
    if state.failed_at is not None and now - state.failed_at < RETRY_SECONDS:
        return fallback
    if not geo_fresh:
        await _read_geo(hass, base_url, state)
        state.geo_read_at = now
        if known and known[0] == state.version:
            state.failed_at = None
            return known[1]
    found = await _resolve(hass, base_url, state, key)
    state.found.pop(key, None)
    state.found[key] = (state.version, found)
    while len(state.found) > _KEPT_HOMES:
        state.found.pop(next(iter(state.found)))
    state.failed_at = None
    return found


async def _read_geo(hass, base_url, state: _State) -> None:
    body = await _fetch(hass, base_url + "/v1/public/geo")
    geo = await hass.async_add_executor_job(_decode, body)
    try:
        version = geo["version"]
        files = geo["files"]
        voivodeships = files["voivodeships"]
        municipalities = files["municipalities"]
        if not (
            isinstance(version, str)
            and _VERSION.match(version)
            and isinstance(voivodeships, str)
            and isinstance(municipalities, str)
            and "{vv}" in municipalities
            and voivodeships.startswith("/")
            and municipalities.startswith("/")
            and not voivodeships.startswith("//")
            and not municipalities.startswith("//")
        ):
            raise _LookupError("geo shape")
    except (KeyError, TypeError) as err:
        raise _LookupError("geo shape") from err
    state.version = version
    state.files = {"voivodeships": voivodeships, "municipalities": municipalities}


async def _resolve(hass, base_url, state: _State, key) -> str | None:
    latitude, longitude = key
    files = state.files
    body = await _fetch(hass, base_url + files["voivodeships"])
    voivodeship = await hass.async_add_executor_job(
        _find, body, "voivodeships", latitude, longitude, _VOIVODESHIP
    )
    if voivodeship is None or voivodeship == "00":
        return None  # abroad, at sea or in the offshore unit
    path = files["municipalities"].replace("{vv}", voivodeship)
    body = await _fetch(hass, base_url + path)
    found = await hass.async_add_executor_job(
        _find, body, "municipalities", latitude, longitude, _MUNICIPALITY
    )
    if found is not None and not found.startswith(voivodeship):
        raise _LookupError("municipality outside its voivodeship file")
    return found


async def _fetch(hass: HomeAssistant, url: str) -> bytes:
    try:
        session = async_get_clientsession(hass)
        # A redirect could send the second request (it names the voivodeship) to another host.
        async with session.get(
            url, timeout=REQUEST_TIMEOUT, allow_redirects=False
        ) as response:
            if response.status != 200:
                raise _LookupError(f"HTTP {response.status}")
            return await response.read()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise _LookupError(type(err).__name__) from err


def _decode(body: bytes):
    try:
        return json.loads(body)
    except ValueError as err:  # includes UnicodeDecodeError and JSONDecodeError
        raise _LookupError("not JSON") from err


def _find(body: bytes, name: str, latitude: float, longitude: float, id_pattern):
    """Id of the geometry of `objects[name]` that contains the point, or None."""
    topology = _decode(body)
    try:
        geometries = topology["objects"][name]["geometries"]
        scale_x, scale_y = topology["transform"]["scale"]
        translate_x, translate_y = topology["transform"]["translate"]
        arcs = topology["arcs"]
        if not isinstance(arcs, list) or not isinstance(geometries, list):
            raise _LookupError("topology shape")
        # the point in the integer grid the arcs are quantised on
        x = (longitude - translate_x) / scale_x
        y = (latitude - translate_y) / scale_y
        decoded: dict[int, list] = {}

        def arc_points(index: int) -> list:
            forward = index if index >= 0 else ~index
            points = decoded.get(forward)
            if points is None:
                points = []
                px = py = 0
                for dx, dy in arcs[forward]:
                    px += dx
                    py += dy
                    points.append((px, py))
                decoded[forward] = points
            return points[::-1] if index < 0 else points

        def ring(indexes) -> list:
            points: list = []
            for position, index in enumerate(indexes):
                arc = arc_points(index)
                points.extend(arc if position == 0 else arc[1:])
            return points

        for geometry in geometries:
            kind = geometry["type"]
            if kind == "Polygon":
                polygons = [geometry["arcs"]]
            elif kind == "MultiPolygon":
                polygons = geometry["arcs"]
            else:
                continue
            for rings in polygons:
                inside = False
                for indexes in rings:
                    if _crossings_odd(ring(indexes), x, y):
                        inside = not inside
                if inside:
                    found = geometry["id"]
                    if not isinstance(found, str) or not id_pattern.match(found):
                        raise _LookupError("unit id")
                    return found
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError) as err:
        raise _LookupError("topology shape") from err
    return None


def _crossings_odd(points: list, x: float, y: float) -> bool:
    """Even-odd rule: does a ray from the point towards +x cross the ring an odd number of times."""
    inside = False
    previous_x, previous_y = points[-1]
    for current_x, current_y in points:
        if (current_y > y) != (previous_y > y) and x < (
            (previous_x - current_x) * (y - current_y) / (previous_y - current_y)
            + current_x
        ):
            inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside
