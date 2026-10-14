"""Home Assistant side of the native Deye controller: port, triggers and run discipline.

`HassPort` is the `ExecutorPort` over states, services and the controller store.
`NativeHost` owns the executor of one entry: it turns the blueprint's triggers into run
requests and serialises the runs (one running, one re-run queued, further requests
dropped), which is the blueprint's `mode: queued, max: 2, max_exceeded: silent`.
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
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
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from . import controller_profile as profile
from . import controller_texts as texts
from .controller import TouFact
from .controller_ha import active_blueprint_automations
from .controller_run import ControllerUnavailable, DeyeExecutor
from .controller_setup import READ_SERVICE, derive_limits, entities_of, resolve
from .settings import DOMAIN
from .soc_estimator_config import counter_mode

if TYPE_CHECKING:
    from .controller_ha import DeyeController

_LOGGER = logging.getLogger(__name__)

FAILED_RUNS_FOR_ISSUE = 3
ATTRIBUTES = ("min", "max", "step", "options", "current")
NO_ATTRIBUTES: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class NativeSetup:
    """What one run works with: the resolved entities and the limits in force."""

    entities: profile.Entities
    limits: profile.Limits
    tracked: tuple[str, ...]


def estimator_in_charge(controller: DeyeController) -> bool:
    """Whether the planner's SOC is the counter estimator, not an entity."""
    configuration = getattr(controller.coordinator, "configuration", None)
    return bool(configuration) and counter_mode(configuration)


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
    if estimator_in_charge(controller):
        # The inverter compares its program SOC thresholds with its own BMS SOC,
        # which the estimator does not replace: Capacity mode would act on a
        # SOC target in the wrong scale. Voltage mode has voltage thresholds only.
        limits = replace(
            limits,
            commissioned_battery_modes=tuple(
                mode for mode in limits.commissioned_battery_modes if mode != "Capacity"
            ),
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

    def resolve_setup(self) -> NativeSetup | None:
        found = native_setup(self.hass, self.controller)
        self.setup = found if isinstance(found, NativeSetup) else None
        return self.setup

    def snapshot(self, stage: str) -> profile.Snapshot:
        if stage == "start":
            self.resolve_setup()
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
        if setup is not None and estimator_in_charge(self.controller):
            facts[setup.entities.soc] = self._estimator_fact()
        view = self.controller.view()[1]
        return profile.Snapshot(
            now=self.now(),
            mode=self.controller.state.mode,
            entities=setup.entities if setup else _NO_ENTITIES,
            states=facts,
            controller=view if view and setup is not None else None,
            paused=self.controller.state.paused,
            blueprint_active=tuple(active_blueprint_automations(self.hass)),
        )

    def _estimator_fact(self) -> profile.Fact:
        """The estimator in place of the BMS SOC entity: gate, full-pack rule, targets."""
        estimate = self.controller.coordinator.soc_estimator.view()
        if estimate is None:
            return profile.Fact(STATE_UNAVAILABLE)
        return profile.Fact(
            f"{estimate['fraction'] * 100:.4f}",
            NO_ATTRIBUTES,
            estimate["reported_at"].timestamp(),
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
FAILED_OUTCOMES = ("error", "controller_unavailable")


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
        self._running = False
        self._rerun = False
        self._stopped = False
        self._started_up = False
        self._unsubscribe: list[CALLBACK_TYPE] = []
        self._watched: tuple[str, ...] = ()
        self._unwatch: CALLBACK_TYPE | None = None
        self._logged: tuple[Any, Any] | None = None
        self._unsub_started: CALLBACK_TYPE | None = None
        self._failures = 0
        self._seen: tuple[Any, ...] | None = None

    @property
    def _entry_id(self) -> str:
        return self.controller.entry.entry_id

    def _limits(self) -> profile.Limits:
        setup = self.port.setup
        if setup is None:
            raise ControllerUnavailable("setup")
        return setup.limits

    @property
    def history(self) -> list[dict[str, Any]]:
        """The last runs, oldest first."""
        return list(self.executor.history)

    @property
    def notable(self) -> list[dict[str, Any]]:
        """The last runs that sent a command or ended with a code other than `ok`."""
        return list(self.executor.notable)

    async def async_start(self) -> None:
        runtime = self.controller.state.runtime
        self._logged = (runtime.get("confirmed_mode"), runtime.get("code"))
        self._seen = self._inputs()
        self._unsubscribe = [
            self.controller.async_add_listener(self._controller_changed),
            async_track_time_change(self.hass, self._minute, second=0),
        ]
        # Listen from the start whenever the setup resolves; requests made before
        # Home Assistant has started are ignored by `request`.
        self.port.resolve_setup()
        self._sync_watch()
        if self.hass.state is CoreState.running:
            self._started_up = True
            self.request("start")
        else:
            self._unsub_started = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._started
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
        if self._unsub_started is not None:
            self._unsub_started()
            self._unsub_started = None
        self._watch(())
        for issue in ("deye_blueprint_active", "deye_run_failed"):
            ir.async_delete_issue(self.hass, DOMAIN, f"{issue}_{self._entry_id}")
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task])
        self._task = None
        self._running = False

    @callback
    def request(self, trigger: str) -> None:
        """Ask for a run: at most one runs and one more waits."""
        if self._stopped or not self._started_up:
            return
        if self._running:
            self._rerun = True
            return
        # The flag, not the task, is the lock: Home Assistant starts tasks eagerly, so
        # the run's first step executes before `_task` is assigned, and a request made
        # from inside it (the controller listener fires on the run's own persists)
        # would otherwise start a second, nested run.
        self._running = True
        self._task = self.hass.async_create_background_task(
            self._loop(trigger),
            f"energy_compass_deye_run_{self.controller.entry.entry_id}",
            eager_start=False,
        )

    async def async_wait_idle(self) -> None:
        """Wait until no run is in flight or queued (for tests and shutdown)."""
        while self._task is not None and not self._task.done():
            await asyncio.wait([self._task])

    async def _loop(self, trigger: str) -> None:
        try:
            await self._runs(trigger)
        finally:
            self._running = False

    async def _runs(self, trigger: str) -> None:
        while True:
            self._rerun = False
            self.executor.language = self.hass.config.language
            try:
                outcome = await self.executor.run(trigger)
            except ControllerUnavailable:
                outcome = "controller_unavailable"
            except Exception:
                _LOGGER.exception("Deye controller run failed")
                outcome = "error"
            self.runs += 1
            await self._after_run(outcome)
            _LOGGER.debug("Deye controller run (%s): %s", trigger, outcome)
            if not self._rerun or self._stopped:
                return
            trigger = "queued"

    async def _after_run(self, outcome: str) -> None:
        """Everything that follows a run; none of it may end the task or lose a re-run."""
        failed = outcome in FAILED_OUTCOMES
        if failed and not self._stopped:
            await self._report_failure("run_failed" if outcome == "error" else outcome)
        for step in (
            lambda: self._track_failures(failed),
            self._check_writers,
            self._sync_watch,
            self._log_change,
            self.controller.async_runtime_changed,
        ):
            try:
                step()
            except Exception:
                _LOGGER.exception("Deye controller post-run step failed")

    async def _report_failure(self, code: str) -> None:
        """Show on the runtime sensor that the run failed; best effort."""
        try:
            await self.controller.async_persist(
                {
                    **self.controller.state.runtime,
                    "code": "error",
                    "reason_code": code,
                    "reason": texts.text(code, self.hass.config.language),
                },
                None,
            )
        except Exception:
            _LOGGER.exception("Deye controller could not publish its failure")

    def _track_failures(self, failed: bool) -> None:
        issue = f"deye_run_failed_{self._entry_id}"
        self._failures = self._failures + 1 if failed else 0
        if self._failures >= FAILED_RUNS_FOR_ISSUE:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="deye_run_failed",
            )
        elif not failed:
            ir.async_delete_issue(self.hass, DOMAIN, issue)

    def _check_writers(self) -> None:
        issue = f"deye_blueprint_active_{self._entry_id}"
        if active := active_blueprint_automations(self.hass):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="deye_blueprint_active",
                translation_placeholders={"automation": ", ".join(active)},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue)

    def _log_change(self) -> None:
        """One logbook entry whenever the confirmed mode or the runtime code changes."""
        runtime = self.controller.state.runtime
        now = (runtime.get("confirmed_mode"), runtime.get("code"))
        before, self._logged = self._logged, now
        if before == now:
            return
        from homeassistant.components.logbook import async_log_entry

        registry = er.async_get(self.hass)
        async_log_entry(
            self.hass,
            "Deye controller",
            f"{before[1]} → {now[1]}; confirmed {before[0]} → {now[0]}; "
            f"{runtime.get('reason', '')}",
            domain="energy_compass",
            entity_id=registry.async_get_entity_id(
                "sensor",
                "energy_compass",
                f"{self.controller.entry.entry_id}_deye_runtime",
            ),
        )

    @callback
    def _started(self, _event: Event) -> None:
        self._unsub_started = None
        self._started_up = True
        self.request("start")

    @callback
    def _minute(self, _now: datetime) -> None:
        self.request("minute")

    @callback
    def _controller_changed(self) -> None:
        """Run when something the executor reads from the controller changed.

        The controller notifies on every coordinator update, about every few seconds
        on a busy site, whether or not its view moved; the sensor entities rely on
        that, a run must not.
        """
        inputs = self._inputs()
        if inputs == self._seen:
            return
        self._seen = inputs
        self.request("controller")

    def _inputs(self) -> tuple[Any, ...]:
        """The controller facts a run depends on.

        The view is what the snapshot hands the profile; `advances` counts the
        next-event timer, which moves `now` across a row or TOU boundary without
        changing the view. Pause and the options (limits, entity overrides) are not
        part of the view.
        """
        controller = self.controller
        return (
            controller.view()[1],
            controller.advances,
            controller.state.mode,
            controller.state.paused,
            controller.entry.options.get("controller"),
        )

    def _sync_watch(self) -> None:
        setup = self.port.setup
        if setup is None:
            self._watch(())
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
        edge = (
            old is not None
            and new is not None
            and old.state != new.state
            and (old.state in _EDGE or new.state in _EDGE)
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
