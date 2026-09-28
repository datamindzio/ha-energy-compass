"""Lifecycle glue between one config entry and its `SinkThread` (ADR-0019 §9).

Imported only when `options["atlas"]["enabled"]` is true (ADR-0019 §3): `async_setup_entry`
does `from .atlas import AtlasBridge` inside the `if enabled` branch.
"""

import logging

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from ..atlas_env import BASE_URLS
from ..atlas_sink.sink import SinkThread
from ..flow_schema import snapshot
from .mapping import resolve_feed, tracked_entity_ids
from .solve_builder import build_solve_payload
from .storage import environment_dir, is_registered

_LOGGER = logging.getLogger(__name__)


class AtlasBridge:
    """Owns the `SinkThread` for one entry's selected environment."""

    def __init__(self, hass: HomeAssistant, entry, config: dict, atlas_settings: dict):
        self.hass = hass
        self.entry = entry
        self.config = config
        self.environment = atlas_settings["environment"]
        self.dir = environment_dir(hass, entry.entry_id, self.environment)
        self.sink: SinkThread | None = None
        self.solves_skipped = 0
        self._unsub_state = None
        self._unsub_stop = None

    async def async_start(self, attrs: dict) -> None:
        if not is_registered(self.hass, self.entry.entry_id, self.environment):
            _LOGGER.warning(
                "Energy Atlas enabled but not registered for %s; open Options → Energy Atlas",
                self.environment,
            )
            return
        self.sink = SinkThread(self.dir, BASE_URLS[self.environment], attrs)
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
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_stop is not None:
            self._unsub_stop()
            self._unsub_stop = None
        if self.sink is not None:
            await self.hass.async_add_executor_job(self.sink.stop, 10)

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
        """Push a solve payload that the coordinator is about to publish."""
        if payload is None:
            self.solves_skipped += 1
        elif self.sink is not None:
            self.sink.add_solve(payload)

    def skip_solve(self) -> None:
        """Count a solve the coordinator could not publish (failed/invalid solve)."""
        self.solves_skipped += 1

    def status(self) -> dict:
        if self.sink is None:
            return {"registered": False}
        return self.sink.status()
