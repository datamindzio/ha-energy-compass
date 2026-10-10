"""Home Assistant adapter for the Deye controller state: store, session and timers."""

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import controller as core
from .settings import DOMAIN, NUMBERS

_LOGGER = logging.getLogger(__name__)

PROCESS_SESSION = "controller_process_session"
ENTRY_SESSIONS = "controller_sessions"
AUTO_ENABLED = "controller_auto_enabled"
MODE_UNIQUE_SUFFIX = "deye_mode"
_TOU_DOMAINS = frozenset(domain for _, domain in core.TOU_FIELDS)
_PACKAGE_DOMAINS = {
    "mode": "input_select",
    "session": "input_boolean",
    "session_start": "input_datetime",
    "restore_pending": "input_boolean",
    "snapshot": "sensor",
    "runtime": "sensor",
}


def controller_session(hass: HomeAssistant, entry_id: str) -> float:
    """The session stamp of this Core process, shared by reloads of the entry."""
    data = hass.data.setdefault(DOMAIN, {})
    process = data.setdefault(
        PROCESS_SESSION, float(math.ceil(dt_util.utcnow().timestamp()))
    )
    return data.setdefault(ENTRY_SESSIONS, {}).setdefault(entry_id, process)


def solarman_devices(hass: HomeAssistant) -> set[str]:
    """Device ids that own at least one Solarman entity."""
    registry = er.async_get(hass)
    return {
        item.device_id
        for item in registry.entities.values()
        if item.platform == "solarman" and item.device_id
    }


def device_resolution(hass: HomeAssistant, device_id: str | None) -> core.TouResolution:
    """Resolve the six TOU programs of one device from the entity registry."""
    if not device_id:
        return core.TouResolution(None, (), None)
    registry = er.async_get(hass)
    return core.resolve_tou(
        core.TouFact(
            item.entity_id,
            item.domain,
            item.platform,
            item.translation_key,
            item.disabled,
        )
        for item in er.async_entries_for_device(
            registry, device_id, include_disabled_entities=True
        )
    )


def default_device(hass: HomeAssistant) -> str | None:
    """The only Solarman device that has TOU programs, if there is exactly one."""
    registry = er.async_get(hass)
    devices = {
        item.device_id
        for item in registry.entities.values()
        if item.platform == "solarman"
        and item.translation_key == "program_1_time"
        and item.device_id
    }
    return next(iter(devices)) if len(devices) == 1 else None


def device_in_use(hass: HomeAssistant, entry_id: str, device_id: str) -> bool:
    """Whether another entry already runs a controller on this device."""
    return any(
        (other.options.get("controller") or {}).get("enabled")
        and other.options["controller"].get("device_id") == device_id
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry_id
    )


def async_auto_enable(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Enable the controller for a lone installation that already ran the package."""
    if "controller" in entry.options:
        return False
    package_mode = core.PACKAGE_IDENTITIES["mode"]
    if (
        er.async_get(hass).async_get_entity_id("input_select", *package_mode) is None
        or len(hass.config_entries.async_entries(DOMAIN)) != 1
    ):
        return False
    usable = [
        device
        for device in solarman_devices(hass)
        if device_resolution(hass, device).problem is None
        and device_resolution(hass, device).prefix
    ]
    if len(usable) != 1:
        return False
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "controller": {"enabled": True, "device_id": usable[0]},
        },
    )
    hass.data.setdefault(DOMAIN, {}).setdefault(AUTO_ENABLED, set()).add(entry.entry_id)
    _LOGGER.debug("Enabled the Deye controller for an installation with the package")
    return True


class DeyeController:
    """Own the accepted plan, revocation, mode, runtime and timing of one entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.state = core.ControllerState()
        self.session = 0.0
        self.device_id: str | None = None
        self.resolution = core.TouResolution(None, (), None)
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.controller")
        self._tou_values: dict[str, str] = {}
        self._next_tou: datetime | None = None
        self._view: tuple[datetime | None, dict] = (None, {})
        self._listeners: list[Callable[[], None]] = []
        self._runtime_listeners: list[Callable[[], None]] = []
        self._unsub_coordinator: CALLBACK_TYPE | None = None
        self._unsub_tou: CALLBACK_TYPE | None = None
        self._unsub_registry: CALLBACK_TYPE | None = None
        self._timer: CALLBACK_TYPE | None = None
        self._stopped = False

    async def async_start(self) -> None:
        """Restore the store, resolve the TOU entities and begin listening."""
        raw = await self._store.async_load()
        self.session = controller_session(self.hass, self.entry.entry_id)
        now = dt_util.utcnow()
        self.state, valid = core.load_state(raw, session=self.session, now=now)
        if not valid:
            _LOGGER.warning(
                "Invalid stored Deye controller state; mode Off with restore pending"
            )
        elif any(event.kind == "dropped" for event in self.state.history[-1:]):
            _LOGGER.debug("Dropped the Deye controller plan of an earlier session")
        self.device_id = (self.entry.options.get("controller") or {}).get("device_id")
        self._resolve()
        self._unsub_registry = self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed
        )
        self._unsub_coordinator = self.coordinator.async_add_listener(
            self._handle_update
        )
        auto = self.hass.data.get(DOMAIN, {}).get(AUTO_ENABLED, set())
        if self.entry.entry_id in auto:
            auto.discard(self.entry.entry_id)
            self.state = replace(
                self.state,
                history=(
                    *self.state.history,
                    core.Event(now.isoformat(), "auto_enabled"),
                )[-core.HISTORY_LIMIT :],
            )
        self._save()
        self._evaluate()

    async def async_stop(self) -> None:
        """Detach everything and persist the state."""
        self._stopped = True
        for unsubscribe in (
            self._unsub_coordinator,
            self._unsub_tou,
            self._unsub_registry,
            self._timer,
        ):
            if unsubscribe:
                unsubscribe()
        self._unsub_coordinator = self._unsub_tou = None
        self._unsub_registry = self._timer = None
        await self._store.async_save(self.state.to_dict())

    async def async_remove_store(self) -> None:
        await self._store.async_remove()

    @callback
    def view(self) -> tuple[datetime | None, dict]:
        """Controller sensor state (next event) and attributes."""
        return self._view

    @callback
    def runtime_view(self) -> tuple[str, dict]:
        runtime = self.state.runtime
        return runtime.get("code") or "waiting", {
            **core.runtime_summary(runtime, self.state.restore_pending),
            "runtime": dict(runtime),
            "updated_at": self.state.runtime_written_at,
        }

    @callback
    def async_add_listener(
        self, update: Callable[[], None], *, runtime: bool = False
    ) -> CALLBACK_TYPE:
        listeners = self._runtime_listeners if runtime else self._listeners
        listeners.append(update)

        @callback
        def remove() -> None:
            if update in listeners:
                listeners.remove(update)

        return remove

    @callback
    def async_set_mode(self, mode: str) -> None:
        if mode not in core.MODES:
            raise ValueError(mode)
        if mode == self.state.mode:
            return
        event = core.Event(
            dt_util.utcnow().isoformat(),
            "mode",
            None,
            f"{self.state.mode} -> {mode}",
        )
        self._replace(
            replace(
                self.state,
                mode=mode,
                history=(*self.state.history, event)[-core.HISTORY_LIMIT :],
            )
        )
        self._evaluate()

    @callback
    def async_update_runtime(
        self, runtime: Mapping | None, restore_pending: bool | None
    ) -> dict:
        """Replace the runtime and/or restore flag; with neither, read them."""
        validated = None if runtime is None else core.validate_runtime(runtime)
        if validated is not None or restore_pending is not None:
            pending_changed = (
                restore_pending is not None
                and restore_pending != self.state.restore_pending
            )
            self._replace(
                replace(
                    self.state,
                    runtime=self.state.runtime if validated is None else validated,
                    restore_pending=self.state.restore_pending
                    if restore_pending is None
                    else restore_pending,
                    runtime_written_at=dt_util.utcnow().isoformat(),
                )
            )
            if pending_changed:
                self._publish()
            self._notify(self._runtime_listeners)
        return {
            "runtime": dict(self.state.runtime),
            "restore_pending": self.state.restore_pending,
            "session": self.session,
        }

    async def async_import_package(self, *, force: bool = False) -> dict:
        """One-time copy of the 0.1.36 package helpers into the controller state."""
        if not force and self.state.imported_at is not None:
            raise core.PackageError("package_import_refused", "runtime")
        now = dt_util.utcnow()
        result = core.import_package(
            self._package_facts(), battery=self._fallback_battery(), now=now
        )
        sessions = self.hass.data.setdefault(DOMAIN, {}).setdefault(ENTRY_SESSIONS, {})
        sessions[self.entry.entry_id] = self.session = result.session
        self._replace(
            replace(
                result.state,
                history=(*self.state.history, *result.state.history)[
                    -core.HISTORY_LIMIT :
                ],
            )
        )
        self._evaluate(now)
        self._notify(self._runtime_listeners)
        accepted, revoked = self.state.accepted, self.state.revoked
        return {
            "mode": self.state.mode,
            "session": self.session,
            "restore_pending": self.state.restore_pending,
            "accepted_generation": accepted.get("generated_at") if accepted else None,
            "revoked_generation": revoked.generation if revoked else None,
            "runtime_keys": sorted(self.state.runtime),
            "dropped_keys": list(result.dropped_keys),
            "snapshot_kept": result.snapshot_kept,
        }

    def _package_facts(self) -> core.PackageFacts:
        registry = er.async_get(self.hass)
        found = {}
        for role, (platform, unique_id) in core.PACKAGE_IDENTITIES.items():
            domain = _PACKAGE_DOMAINS[role]
            entity_id = registry.async_get_entity_id(domain, platform, unique_id)
            found[role] = self.hass.states.get(entity_id) if entity_id else None
        mode, session, start, pending, snapshot, runtime = (
            found[role] for role in core.PACKAGE_IDENTITIES
        )
        return core.PackageFacts(
            mode=mode.state if mode else None,
            session=session.state if session else None,
            session_start=start.attributes.get("timestamp") if start else None,
            restore_pending=pending.state if pending else None,
            snapshot=None
            if snapshot is None
            else (snapshot.attributes.get("snapshot") or {}),
            runtime=None
            if runtime is None
            else (runtime.attributes.get("runtime") or {}),
        )

    @callback
    def async_set_device(self, device_id: str | None) -> None:
        if device_id == self.device_id:
            return
        self.device_id = device_id
        now = dt_util.utcnow()
        event = core.Event(now.isoformat(), "device", None, "changed")
        self._replace(
            replace(
                self.state,
                history=(*self.state.history, event)[-core.HISTORY_LIMIT :],
            )
        )
        self._resolve()
        self._publish()

    @callback
    def async_refresh_view(self) -> None:
        """Recompute the view, e.g. once the mode select has a registry id."""
        self._publish()

    def diagnostics(self) -> dict:
        accepted = self.state.accepted
        revoked = self.state.revoked
        _, attrs = self._view
        return {
            "enabled": True,
            "device_id": self.device_id,
            "program_prefix": self.resolution.prefix,
            "tou_problem": self.resolution.problem,
            "session": self.session,
            "mode": self.state.mode,
            "restore_pending": self.state.restore_pending,
            "plan_reason": attrs.get("plan_reason"),
            "accepted": None
            if accepted is None
            else {
                "generated_at": accepted.get("generated_at"),
                "valid_until": accepted.get("valid_until"),
                "coverage_end": accepted.get("coverage_end"),
                "interval_count": len(accepted.get("intervals", [])),
                "battery": accepted.get("battery"),
            },
            "revoked": None
            if revoked is None
            else {
                "generation": revoked.generation,
                "at": revoked.at,
                "reason": revoked.reason,
            },
            "runtime": {
                "code": self.state.runtime.get("code"),
                "keys": sorted(self.state.runtime),
            },
            "imported_at": self.state.imported_at,
            "history": self.state.to_dict()["history"],
        }

    def _fallback_battery(self) -> dict[str, float]:
        settings = self.coordinator.configuration.get("settings", {})
        return {
            key: float(settings.get(key, NUMBERS[key][1])) for key in core.BATTERY_KEYS
        }

    def _save(self) -> None:
        self._store.async_delay_save(self.state.to_dict, 1)

    def _replace(self, state: core.ControllerState) -> None:
        if state is not self.state:
            self.state = state
            self._save()

    @callback
    def _handle_update(self) -> None:
        self._evaluate()

    def _evaluate(self, now: datetime | None = None) -> None:
        if self._stopped:
            return
        now = now or dt_util.utcnow()
        before = self.state
        self._replace(
            core.step(
                self.state,
                self.coordinator.data,
                session=self.session,
                now=now,
                fallback_battery=self._fallback_battery(),
            )
        )
        if self.state.revoked != before.revoked and self.state.revoked is not None:
            _LOGGER.debug("Deye controller plan revoked")
        self._publish(now)

    def _publish(self, now: datetime | None = None) -> None:
        if self._stopped:
            return
        now = now or dt_util.utcnow()
        prefix = self.resolution.prefix
        times = [
            self._tou_values.get(f"time.{prefix}{number}_time")
            for number in range(1, 7)
        ]
        self._next_tou = core.next_tou(times, dt_util.as_local(now)) if prefix else None
        event = core.next_event(self.state.accepted, self._next_tou, now)
        self._view = (
            event,
            core.controller_attributes(
                self.state,
                self.coordinator.data,
                session=self.session,
                now=now,
                mode_entity=self._mode_entity(),
                fallback_battery=self._fallback_battery(),
                tou=self._tou_values,
                device_id=self.device_id,
                program_prefix=prefix,
                tou_problem=self.resolution.problem,
                next_tou_at=self._next_tou,
            ),
        )
        if self._timer:
            self._timer()
            self._timer = None
        if event is not None:
            self._timer = async_track_point_in_utc_time(
                self.hass,
                self._advance,
                event + timedelta(seconds=core.ADVANCE_DELAY_SECONDS),
            )
        self._notify(self._listeners)

    @callback
    def _advance(self, now: datetime) -> None:
        self._timer = None
        self._evaluate()

    def _notify(self, listeners: list[Callable[[], None]]) -> None:
        for update in list(listeners):
            update()

    def _mode_entity(self) -> str | None:
        return er.async_get(self.hass).async_get_entity_id(
            "select", DOMAIN, f"{self.entry.entry_id}_{MODE_UNIQUE_SUFFIX}"
        )

    def _resolve(self) -> None:
        resolution = device_resolution(self.hass, self.device_id)
        changed = resolution.entities != self.resolution.entities
        self.resolution = resolution
        if changed or self._unsub_tou is None:
            if self._unsub_tou:
                self._unsub_tou()
                self._unsub_tou = None
            if resolution.entities:
                self._unsub_tou = async_track_state_change_event(
                    self.hass, list(resolution.entities), self._tou_changed
                )
        self._read_tou()

    def _read_tou(self) -> None:
        values: dict[str, Any] = {}
        for entity_id in self.resolution.entities:
            state = self.hass.states.get(entity_id)
            values[entity_id] = state.state if state else "unknown"
        self._tou_values = values

    @callback
    def _tou_changed(self, event) -> None:
        self._read_tou()
        self._publish()

    @callback
    def _registry_changed(self, event) -> None:
        domains = {
            str(event.data.get(key) or "").partition(".")[0]
            for key in ("entity_id", "old_entity_id")
        }
        if domains & (_TOU_DOMAINS | {"select"}):
            if self.device_id:
                self._resolve()
            self._publish()
