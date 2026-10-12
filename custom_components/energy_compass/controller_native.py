"""Home Assistant side of the native Deye controller: port, triggers and run discipline.

`HassPort` is the `ExecutorPort` over states, services and the controller store.
`NativeHost` owns the executor of one entry: it turns the blueprint's triggers into run
requests and serialises the runs (one running, one re-run queued, further requests
dropped), which is the blueprint's `mode: queued, max: 2, max_exceeded: silent`.
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from . import controller_profile as profile
from .controller import TouFact
from .controller_run import ControllerUnavailable, DeyeExecutor
from .controller_setup import derive_limits, entities_of, resolve

if TYPE_CHECKING:
    from .controller_ha import DeyeController

_LOGGER = logging.getLogger(__name__)

READ_SERVICE = ("solarman", "read_holding_registers")
ATTRIBUTES = ("min", "max", "step", "options", "current")
NO_ATTRIBUTES: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class NativeSetup:
    """What one run works with: the resolved entities and the limits in force."""

    entities: profile.Entities
    limits: profile.Limits
    tracked: tuple[str, ...]


def native_setup(hass: HomeAssistant, controller: DeyeController) -> NativeSetup | str:
    """The entities and limits for this run, or the name of what is missing."""
    prefix = controller.resolution.prefix
    if not controller.device_id or not prefix:
        return "tou"
    options = controller.entry.options.get("controller") or {}
    registry = er.async_get(hass)
    resolved = resolve(
        (
            TouFact(
                item.entity_id,
                item.domain,
                item.platform,
                item.translation_key,
                item.disabled,
            )
            for item in er.async_entries_for_device(registry, controller.device_id)
        ),
        options.get("entities"),
    )
    if resolved.missing:
        return ",".join(resolved.missing)
    entities = entities_of(resolved, prefix)
    battery = controller.fallback_battery()

    def attribute(entity_id: str, name: str) -> Any:
        state = hass.states.get(entity_id)
        return state.attributes.get(name) if state else None

    limits = derive_limits(
        options.get("limits"),
        charge_kw=battery["charge_kw"],
        discharge_kw=battery["discharge_kw"],
        charge_max=attribute(entities.charge, "max"),
        grid_max=attribute(entities.grid, "max"),
        resolved=resolved,
    )
    tracked = dict.fromkeys(
        [
            *resolved.entities.values(),
            *controller.resolution.entities,
            *limits.telemetry_entities,
            *limits.old_writers,
            limits.discharge_energy_entity,
        ]
    )
    tracked.pop("", None)
    return NativeSetup(entities, limits, tuple(tracked))


class HassPort:
    """`ExecutorPort` over Home Assistant."""

    def __init__(self, hass: HomeAssistant, controller: DeyeController) -> None:
        self.hass = hass
        self.controller = controller
        self.setup: NativeSetup | None = None

    def now(self) -> datetime:
        return dt_util.now()

    def snapshot(self, stage: str) -> profile.Snapshot:
        if stage == "start":
            found = native_setup(self.hass, self.controller)
            self.setup = found if isinstance(found, NativeSetup) else None
        setup = self.setup
        facts = {}
        if setup is not None:
            for entity_id in setup.tracked:
                if (state := self.hass.states.get(entity_id)) is not None:
                    facts[entity_id] = profile.Fact(
                        state.state,
                        MappingProxyType(
                            {
                                key: state.attributes[key]
                                for key in ATTRIBUTES
                                if key in state.attributes
                            }
                        ),
                        state.last_reported.timestamp(),
                    )
        view = self.controller.view()[1]
        return profile.Snapshot(
            now=self.now(),
            mode=self.controller.state.mode,
            entities=setup.entities if setup else _NO_ENTITIES,
            states=facts,
            controller=view if view and setup is not None else None,
            paused=self.controller.state.paused,
        )

    async def persist(
        self,
        runtime: Mapping[str, Any] | None = None,
        restore_pending: bool | None = None,
        *,
        durable: bool = False,
    ) -> tuple[dict, bool]:
        return await self.controller.async_persist(
            runtime, restore_pending, durable=durable
        )

    async def select_mode(self, mode: str) -> None:
        self.controller.async_set_mode(mode)

    async def write(self, command: profile.Command) -> None:
        domain = command.entity.partition(".")[0]
        if domain == "select":
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": command.entity, "option": command.value},
                blocking=True,
            )
        else:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": command.entity, "value": command.value},
                blocking=True,
            )

    async def read_registers(self, address: int, count: int) -> Any:
        if not self.hass.services.has_service(*READ_SERVICE):
            return None
        return await self.hass.services.async_call(
            *READ_SERVICE,
            {"device": self.controller.device_id, "address": address, "count": count},
            blocking=True,
            return_response=True,
        )


_NO_ENTITIES = profile.Entities("", "", "", "", "", "", "")
_EDGE = (STATE_UNKNOWN, STATE_UNAVAILABLE)


class NativeHost:
    """Triggers, run lock and lifecycle of the executor of one entry."""

    def __init__(self, hass: HomeAssistant, controller: DeyeController) -> None:
        self.hass = hass
        self.controller = controller
        self.port = HassPort(hass, controller)
        self.executor = DeyeExecutor(
            self.port,
            self._limits,
            language=hass.config.language,
        )
        self.runs = 0
        self._task: asyncio.Task | None = None
        self._rerun = False
        self._stopped = False
        self._unsubscribe: list[CALLBACK_TYPE] = []
        self._watched: tuple[str, ...] = ()
        self._unwatch: CALLBACK_TYPE | None = None

    def _limits(self) -> profile.Limits:
        setup = self.port.setup
        if setup is None:
            raise ControllerUnavailable("setup")
        return setup.limits

    async def async_start(self) -> None:
        self._unsubscribe = [
            self.controller.async_add_listener(self._controller_changed),
            async_track_time_change(self.hass, self._minute, second=0),
        ]
        if self.hass.state is CoreState.running:
            self.request("start")
        else:
            self._unsubscribe.append(
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, self._started
                )
            )

    async def async_stop(self) -> None:
        """Stop listening and cancel a run in flight.

        The store already holds `uncertain` and `restore_pending` from before the
        write, so a cancelled transaction is resolved by the next run.
        """
        self._stopped = True
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        self._watch(())
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task])
        self._task = None

    @callback
    def request(self, trigger: str) -> None:
        """Ask for a run: at most one runs and one more waits."""
        if self._stopped:
            return
        if self._task is not None and not self._task.done():
            self._rerun = True
            return
        self._task = self.hass.async_create_background_task(
            self._loop(trigger),
            f"energy_compass_deye_run_{self.controller.entry.entry_id}",
        )

    async def async_wait_idle(self) -> None:
        """Wait until no run is in flight or queued (for tests and shutdown)."""
        while self._task is not None and not self._task.done():
            await asyncio.wait([self._task])

    async def _loop(self, trigger: str) -> None:
        while True:
            self._rerun = False
            self.executor.language = self.hass.config.language
            try:
                outcome = await self.executor.run()
            except ControllerUnavailable:
                outcome = "controller_unavailable"
            except Exception:
                _LOGGER.exception("Deye controller run failed")
                outcome = "error"
            self.runs += 1
            self._sync_watch()
            _LOGGER.debug("Deye controller run (%s): %s", trigger, outcome)
            if not self._rerun or self._stopped:
                return
            trigger = "queued"

    @callback
    def _started(self, _event: Event) -> None:
        self.request("start")

    @callback
    def _minute(self, _now: datetime) -> None:
        self.request("minute")

    @callback
    def _controller_changed(self) -> None:
        self.request("controller")

    def _sync_watch(self) -> None:
        setup = self.port.setup
        if setup is None:
            return
        e = setup.entities
        self._watch(
            (
                e.operation,
                e.soc,
                e.charge,
                e.discharge,
                e.grid,
                *setup.limits.old_writers,
                *setup.limits.telemetry_entities,
            )
        )

    def _watch(self, entities: tuple[str, ...]) -> None:
        entities = tuple(dict.fromkeys(entities))
        if entities == self._watched:
            return
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None
        self._watched = entities
        if entities:
            self._unwatch = async_track_state_change_event(
                self.hass, list(entities), self._state_changed
            )

    @callback
    def _state_changed(self, event: Event) -> None:
        setup = self.port.setup
        old, new = event.data.get("old_state"), event.data.get("new_state")
        entity = event.data["entity_id"]
        if setup is None:
            return
        e = setup.entities
        limits = setup.limits
        edge = (old is not None and old.state in _EDGE) != (
            new is not None and new.state in _EDGE
        )
        if entity in (e.soc, e.charge, e.discharge, e.grid):
            # The blueprint reacts to state changes of these, not to attributes.
            if old is not None and new is not None and old.state == new.state:
                return
        elif (
            entity in limits.telemetry_entities
            and entity not in (e.operation, *limits.old_writers)
            and not edge
        ):
            return
        self.request("change")
