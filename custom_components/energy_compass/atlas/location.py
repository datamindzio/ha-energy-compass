"""Home location as a municipality id, found inside Home Assistant (ADR-0045 §15, S-226).

The home's coordinates never leave Home Assistant. `GET /v1/public/geo` names the geometry
files (TopoJSON, public, the same for every home); `pl.topo.json` tells which voivodeship the
home lies in and that voivodeship's municipalities file which municipality. The only things
derived from the coordinates that leave are the voivodeship id in the file name of the
second request and, in the attributes, the municipality id.

`home_municipality` is one "attribute check": it keeps the municipality last found per
coordinates, reads the geometry version at most once per 24 h, and after a failure waits an
hour before trying again. Standard library plus Home Assistant's own aiohttp session.
"""

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field

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


async def home_municipality(hass: HomeAssistant, base_url: str) -> str | None:
    """Municipality id (7 digits) of Home Assistant's home, or None.

    None means no land municipality or nothing found yet. Never raises for bad
    coordinates or an Atlas that cannot be reached or read.
    """
    latitude, longitude = hass.config.latitude, hass.config.longitude
    if not _valid(latitude, longitude):
        return None
    state = hass.data.setdefault(_DATA_KEY, _State())
    key = (float(latitude), float(longitude))
    async with state.lock:
        try:
            return await _check(hass, base_url, state, key)
        except _LookupError:
            state.failed_at = time.time()
            known = state.found.get(key)
            return known[1] if known else None


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
        async with session.get(url, timeout=REQUEST_TIMEOUT) as response:
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
