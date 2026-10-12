"""Lifecycle glue between one config entry and its `SinkThread` (ADR-0019 §9).

Imported only when `options["atlas"]["enabled"]` is true (ADR-0019 §3): `async_setup_entry`
does `from .atlas import AtlasBridge` inside the `if enabled` branch.
"""

import logging
from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from ..atlas_env import BASE_URLS
from ..atlas_sink.sink import SinkThread
from ..flow_schema import snapshot
from ..settings import DOMAIN
from . import location
from .attrs_builder import build_attrs
from .mapping import resolve_feed, tracked_entity_ids
from .solve_builder import build_solve_payload
from .storage import environment_dir, is_registered

_LOGGER = logging.getLogger(__name__)

# One attribute check: the lookup keeps its own 24 h / 1 h limits (`atlas.location`), so a
# check that has nothing to do makes no request.
MUNICIPALITY_CHECK_INTERVAL = timedelta(minutes=15)


class AtlasBridge:
    """Owns the `SinkThread` for one entry's selected environment."""

    def __init__(self, hass: HomeAssistant, entry, config: dict, atlas_settings: dict):
        self.hass = hass
        self.entry = entry
        self.config = config
        self._attrs: dict | None = None
        self._atlas_settings = atlas_settings
        self.municipality: str | None = None
        self._unsub_check = None
        self._check_task = None
        self.environment = atlas_settings["environment"]
        self.dir = environment_dir(hass, entry.entry_id, self.environment)
        self.sink: SinkThread | None = None
        self.solves_skipped = 0
        self._unsub_state = None
        self._unsub_stop = None

    def _build_attrs(self, config: dict) -> dict:
        """The one place the attributes are built: this bridge's settings and municipality."""
        return build_attrs(config, self._atlas_settings, self.municipality)

    async def async_start(self) -> None:
        if not is_registered(self.hass, self.entry.entry_id, self.environment):
            _LOGGER.warning(
                "Energy Atlas enabled but not registered for %s; open Options → Energy Atlas",
                self.environment,
            )
            return
        # Only a local file read precedes the sink: with the municipality found before the
        # restart the first attributes already carry it (no new attribute version).
        self.municipality = await location.stored_municipality(
            self.hass, BASE_URLS[self.environment], self.dir
        )
        self._attrs = self._build_attrs(self.config)
        self.sink = SinkThread(self.dir, BASE_URLS[self.environment], self._attrs)
        await self.hass.async_add_executor_job(self.sink.start)
        ids = tracked_entity_ids(self.config)
        if ids:
            self._unsub_state = async_track_state_change_event(
                self.hass, ids, self._state_changed
            )
        self._unsub_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._hass_stopping
        )
        states = snapshot(self.hass, self.config)
        feed = resolve_feed(self.config, states, dt_util.utcnow())
        if feed:
            self.sink.feed(dt_util.utcnow(), feed)
        # Telemetry never waits for the lookup: it runs in the background and the
        # attributes follow when it finds the municipality.
        self._unsub_check = async_track_time_interval(
            self.hass, self._municipality_tick, MUNICIPALITY_CHECK_INTERVAL
        )
        self._start_check()

    @callback
    def _municipality_tick(self, now) -> None:
        self._start_check()

    @callback
    def _start_check(self) -> None:
        if self._check_task is not None and not self._check_task.done():
            return
        self._check_task = self.hass.async_create_background_task(
            self._check_municipality(), f"{DOMAIN} atlas municipality"
        )

    async def _check_municipality(self) -> None:
        found = await location.home_municipality(
            self.hass, BASE_URLS[self.environment], self.dir
        )
        if self.sink is None or found == self.municipality:
            return
        self.municipality = found
        self.update(self.config)

    @callback
    def update(self, config: dict) -> None:
        """Follow a live configuration change without restarting the sink thread.

        The state subscription moves only when the tracked entities differ, and the
        sink is told about attributes only when they differ, so repeated calls
        (every source rebind) are free.
        """
        previous = tracked_entity_ids(self.config)
        self.config = config
        if self.sink is None:
            return
        ids = tracked_entity_ids(config)
        if ids != previous:
            if self._unsub_state is not None:
                self._unsub_state()
                self._unsub_state = None
            if ids:
                self._unsub_state = async_track_state_change_event(
                    self.hass, ids, self._state_changed
                )
        attrs = self._build_attrs(config)
        if attrs != self._attrs:
            self._attrs = attrs
            self.sink.set_attrs(attrs)

    @callback
    def _state_changed(self, event) -> None:
        states = snapshot(self.hass, self.config)
        feed = resolve_feed(self.config, states, dt_util.utcnow())
        if feed and self.sink is not None:
            self.sink.feed(dt_util.utcnow(), feed)

    async def _hass_stopping(self, event) -> None:
        # `async_listen_once` already removed this listener before calling us; drop
        # our own remover so a later `async_stop()` doesn't call it again (HA logs
        # "Unable to remove unknown job listener" otherwise).
        self._unsub_stop = None
        if self.sink is not None:
            await self.hass.async_add_executor_job(self.sink.stop, 5)

    async def async_stop(self) -> None:
        if self._unsub_check is not None:
            self._unsub_check()
            self._unsub_check = None
        if self._check_task is not None:
            self._check_task.cancel()
            self._check_task = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_stop is not None:
            self._unsub_stop()
            self._unsub_stop = None
        if self.sink is not None:
            await self.hass.async_add_executor_job(self.sink.stop, 10)
            # Detach so callers (add_solve/skip_solve/status, atlas_backfill's
            # "enabled + registered" check) see a stopped bridge as unregistered
            # instead of silently feeding a dead SinkThread queue (ADR-0019 §9).
            self.sink = None

    def solve_builder(
        self, problem, plan, analysis, values, config, now
    ) -> dict | None:
        """Passed to `runtime.compute()`; builds the payload only, no side effects.

        `compute()` can still fail or be discarded after this call (timeout,
        superseded epoch): only `add_solve`/`skip_solve`, called by the
        coordinator on its actual publish path, may touch the sink or
        `solves_skipped` (ADR-0019 §6).
        """
        return build_solve_payload(problem, plan, analysis, values, config, now)

    def add_solve(self, payload: dict | None) -> None:
        """Push a solve payload that the coordinator is about to publish.

        Not registered (no sink): nothing is sent or counted; diagnostics say
        `registered: false` instead (ADR-0019 §9).
        """
        if self.sink is None:
            return
        if payload is None:
            self.solves_skipped += 1
        else:
            self.sink.add_solve(payload)

    def skip_solve(self) -> None:
        """Count a solve the coordinator could not publish (failed/invalid solve)."""
        if self.sink is not None:
            self.solves_skipped += 1

    def status(self) -> dict:
        if self.sink is None:
            return {"registered": False}
        return self.sink.status()
