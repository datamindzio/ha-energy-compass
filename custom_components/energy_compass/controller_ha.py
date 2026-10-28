"""Home Assistant adapter for the Deye controller state: store, session and timers."""

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import controller as core
from . import controller_clock as clock
from . import controller_grid as grid
from . import controller_hardware as hardware
from .controller_profile import PACK_VOLTAGE_RANGE, Limits
from .controller_run import ControllerUnavailable
from .controller_setup import (
    LIMIT_FIELDS,
    READ_SERVICE,
    ROLES,
    derive_limits,
    resolve,
)
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


BLUEPRINT_PATH = "energy_compass/deye_solarman_controller.yaml"
WRITTEN_ROLES = ("charge", "discharge", "grid", "operation")


def blueprint_automations(hass: HomeAssistant) -> list:
    """Automation entities that use the Deye blueprint, switched on or off."""
    from homeassistant.components import automation

    if automation.DOMAIN not in hass.data:
        return []
    return [
        entity
        for entity in hass.data[automation.DATA_COMPONENT].entities
        if entity.referenced_blueprint == BLUEPRINT_PATH
    ]


def active_blueprint_automations(hass: HomeAssistant) -> list[str]:
    """Entity ids of blueprint automations that are on; `off` ones cannot write."""
    return [
        entity.entity_id
        for entity in blueprint_automations(hass)
        if (state := hass.states.get(entity.entity_id)) is not None
        and state.state == "on"
    ]


def blueprint_prefill(hass: HomeAssistant, resolved, device_id: str) -> dict[str, dict]:
    """Limits and entity overrides of the one blueprint automation, for the options form.

    Only when the entities it writes through belong to this Solarman device: another
    inverter's automation has nothing to say about these limits. Entity inputs only
    where they differ from what the device resolves to. Nothing is applied until the
    user confirms the form.
    """
    found = blueprint_automations(hass)
    if len(found) != 1:
        return {}
    # The blueprint's first action copies every input, defaults included, into
    # variables: they are what this automation actually runs with.
    actions = (found[0].raw_config or {}).get("actions") or [{}]
    inputs = actions[0].get("variables") or {}
    owned = {
        item.entity_id
        for item in er.async_entries_for_device(
            er.async_get(hass), device_id, include_disabled_entities=True
        )
    }
    if any(inputs.get(f"{role}_entity") not in owned for role in WRITTEN_ROLES):
        return {}
    limits = {
        key: inputs[key]
        for key in LIMIT_FIELDS
        if inputs.get(key) not in (None, "", []) and inputs.get(key) != resolved.energy
    }
    entities = {
        role: inputs[f"{role}_entity"]
        for role in ROLES
        if inputs.get(f"{role}_entity")
        and inputs[f"{role}_entity"] != resolved.entities.get(role)
    }
    return {"limits": limits, "entities": entities}


def resolve_device(hass: HomeAssistant, device_id: str, overrides=None):
    """The native controller's entities on one device."""
    return resolve(
        (
            core.TouFact(
                item.entity_id,
                item.domain,
                item.platform,
                item.translation_key,
                item.disabled,
            )
            for item in er.async_entries_for_device(
                er.async_get(hass), device_id, include_disabled_entities=True
            )
        ),
        overrides,
    )


def native_problem(
    hass: HomeAssistant, device_id: str, overrides=None
) -> tuple[str, dict[str, str]] | None:
    """Why the native controller cannot run on this device: (error key, placeholders)."""
    resolved = resolve_device(hass, device_id, overrides)
    missing = list(resolved.missing)
    if not hass.services.has_service(*READ_SERVICE):
        missing.append(".".join(READ_SERVICE))
    if missing:
        return "controller_entity_missing", {"missing": ", ".join(missing)}
    voltage = hass.states.get(resolved.entities["voltage"])
    if voltage is None or not core.is_number(voltage.state):
        # Without a reading the pack cannot be told from a 48 V one.
        return "controller_voltage_unavailable", {
            "entity": resolved.entities["voltage"]
        }
    if float(voltage.state) < PACK_VOLTAGE_RANGE[0]:
        # Only high-voltage packs are supported (design DD-15).
        return "controller_low_voltage_pack", {"voltage": voltage.state}
    return None


def estimator_capacity_conflict(
    hass: HomeAssistant, controller: Mapping[str, Any] | None
) -> bool:
    """Whether the inverter is in Capacity mode under a running native controller.

    The counter SOC estimator cannot be chosen then: in Capacity mode the inverter
    compares its program SOC thresholds with the BMS SOC, so a plan target in the
    estimator's scale would be acted on in the wrong one. Voltage mode, or no
    native controller, is unaffected. While the estimator is the SOC source the
    controller also treats Capacity as not commissioned, so a later switch of the
    inverter to Capacity parks it in BASE instead of writing SOC thresholds.
    """
    controller = controller or {}
    if not controller.get("enabled") or core.controller_engine(controller) != "native":
        return False
    device_id = controller.get("device_id")
    if not device_id:
        return False
    entity = resolve_device(hass, device_id, controller.get("entities")).entities.get(
        "operation"
    )
    state = hass.states.get(entity) if entity else None
    return state is not None and state.state == "Capacity"


def _derived_limits(
    hass: HomeAssistant,
    device_id: str,
    battery: Mapping[str, float],
    limits: Mapping[str, Any] | None,
    overrides=None,
) -> Limits:
    resolved = resolve_device(hass, device_id, overrides)

    def maximum(role: str) -> Any:
        state = hass.states.get(resolved.entities.get(role, ""))
        return state.attributes.get("max") if state else None

    return derive_limits(
        limits,
        charge_kw=battery["charge_kw"],
        discharge_kw=battery["discharge_kw"],
        charge_max=maximum("charge"),
        grid_max=maximum("grid"),
        resolved=resolved,
    )


def limits_underivable(
    hass: HomeAssistant,
    device_id: str,
    battery: Mapping[str, float],
    limits: Mapping[str, Any] | None,
    overrides=None,
) -> bool:
    """Whether the power cap or a current limit would come out as zero.

    Zero limits would make every plan invalid, so the user has to enter them.
    """
    derived = _derived_limits(hass, device_id, battery, limits, overrides)
    return (
        min(
            derived.max_power_w,
            derived.max_current,
            derived.max_grid_current,
            derived.relinquish_current,
        )
        <= 0
    )


def buffer_current_invalid(
    hass: HomeAssistant,
    device_id: str,
    battery: Mapping[str, float],
    limits: Mapping[str, Any] | None,
    overrides=None,
) -> bool:
    """Whether the minimum buffer current is negative, above the maximum current, or
    above the relinquish current (BASE would then write a lower limit than any state)."""
    derived = _derived_limits(hass, device_id, battery, limits, overrides)
    return not (
        0
        <= derived.min_buffer_current
        <= min(derived.max_current, derived.relinquish_current)
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
        self.engine = core.controller_engine(entry.options.get("controller"))
        self.host = None
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
        self._grid_key: tuple | None = None
        self._timer: CALLBACK_TYPE | None = None
        self.advances = 0
        self._stopped = False
        self._device_sensor: str | None = None
        self._clock_entity: str | None = None
        self._grid_presence: str | None = None
        self._grid_voltages: tuple[str, ...] = ()
        self._grid_timer: CALLBACK_TYPE | None = None
        self.hardware = hardware.HardwareFacts()
        self.clock = clock.ClockWatch()

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
        if self.engine != "native" and self.state.paused:
            # The pause belongs to the native executor; a stale flag must not come
            # back if the engine is switched again later.
            self.state = replace(self.state, paused=False)
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
        if self.engine == "native":
            from .controller_native import NativeHost

            self.host = NativeHost(self.hass, self)
            await self.host.async_start()

    async def async_stop(self) -> None:
        """Detach everything and persist the state."""
        if self.host is not None:
            await self.host.async_stop()
            self.host = None
        self._stopped = True
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        ir.async_delete_issue(self.hass, DOMAIN, self._clock_issue_id)
        for unsubscribe in (
            self._unsub_coordinator,
            self._unsub_tou,
            self._unsub_registry,
            self._timer,
            self._grid_timer,
        ):
            if unsubscribe:
                unsubscribe()
        self._unsub_coordinator = self._unsub_tou = None
        self._unsub_registry = None
        self._timer = self._grid_timer = None
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
        history = self.host.history if self.host is not None else []
        return runtime.get("code") or "waiting", {
            **core.runtime_summary(runtime, self.state.restore_pending),
            "runtime": dict(runtime),
            "updated_at": self.state.runtime_written_at,
            "last_run": history[-1] if history else None,
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
        # Two writers on one inverter would fight; a switched-off automation cannot
        # write, so only an enabled one blocks.
        if (
            self.engine == "native"
            and mode != "Off"
            and (active := active_blueprint_automations(self.hass))
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="controller_blueprint_active",
                translation_placeholders={"automation": ", ".join(active)},
            )
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
    def async_runtime_changed(self) -> None:
        """Let the runtime sensor republish, e.g. after a run only changed `last_run`."""
        self._notify(self._runtime_listeners)

    @callback
    def async_set_paused(self, paused: bool) -> None:
        """Hold (or release) every inverter write; takes effect before the next write."""
        if paused == self.state.paused:
            return
        event = core.Event(
            dt_util.utcnow().isoformat(), "mode", None, f"paused {paused}"
        )
        self._replace(
            replace(
                self.state,
                paused=paused,
                history=(*self.state.history, event)[-core.HISTORY_LIMIT :],
            )
        )
        self._publish()

    async def async_save_now(self) -> None:
        """Persist the state at once, e.g. a pause that must survive a crash."""
        await self._store.async_save(self.state.to_dict())

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

    async def async_persist(
        self,
        runtime: Mapping | None,
        restore_pending: bool | None,
        *,
        durable: bool = False,
    ) -> tuple[dict, bool]:
        """`async_update_runtime` for the native executor.

        `durable` saves at once instead of after the one-second delay: the write that
        follows must not outlive the restore obligation it depends on.
        """
        if self._stopped:
            raise ControllerUnavailable("stopped")
        if self._unchanged(runtime, restore_pending):
            result = self.async_update_runtime(None, None)
        else:
            result = self.async_update_runtime(runtime, restore_pending)
        if durable and (runtime is not None or restore_pending is not None):
            await self._store.async_save(self.state.to_dict())
        return result["runtime"], result["restore_pending"]

    def _unchanged(self, runtime: Mapping | None, restore_pending: bool | None) -> bool:
        """Whether storing this would leave the runtime and the flag as they are.

        Storing it anyway would move `runtime_written_at`, rewrite the store and wake
        the listeners for nothing.
        """
        return (
            runtime is None or core.validate_runtime(runtime) == self.state.runtime
        ) and (restore_pending is None or restore_pending == self.state.restore_pending)

    def fallback_battery(self) -> dict[str, float]:
        return self._fallback_battery()

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
            "tou_problem": self._tou_problem(),
            "clock": self._clock_attribute(),
            "grid": self._grid_diagnostics(),
            "engine": self.engine,
            "hardware": self.hardware.as_attribute(),
            "run_history": self.host.history if self.host is not None else [],
            "run_history_notable": self.host.notable if self.host is not None else [],
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
        self._read_hardware()
        self._read_clock(now)
        if self._grid_option_key() != self._grid_key:
            # A saved option may name other grid sources; the form does not reload.
            self._resolve()
        self._read_grid(now)
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
                tou_problem=self._tou_problem(),
                next_tou_at=self._next_tou,
                hardware=self.hardware.as_attribute() if self.hardware.known else None,
                clock=self._clock_attribute(),
                grid=self._grid_attribute(),
                # The blueprint engine keeps its own behaviour (and reads plan_reason).
                outage=self.engine == "native" and self.state.grid.active,
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
        self.advances += 1
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
        sensor = self._find_device_sensor()
        clock_entity = self._find_clock_entity()
        presence, voltages = self._find_grid_sources()
        self._grid_key = self._grid_option_key()
        changed = (
            resolution.entities != self.resolution.entities
            or sensor != self._device_sensor
            or clock_entity != self._clock_entity
            or presence != self._grid_presence
            or voltages != self._grid_voltages
        )
        self.resolution = resolution
        self._device_sensor = sensor
        self._clock_entity = clock_entity
        self._grid_presence = presence
        self._grid_voltages = voltages
        if changed or self._unsub_tou is None:
            if self._unsub_tou:
                self._unsub_tou()
                self._unsub_tou = None
            watched = [
                *resolution.entities,
                *([sensor] if sensor else []),
                *([clock_entity] if clock_entity else []),
                *([presence] if presence else []),
                *voltages,
            ]
            if watched:
                self._unsub_tou = async_track_state_change_event(
                    self.hass, watched, self._tou_changed
                )
        self._read_tou()

    def _find_device_sensor(self) -> str | None:
        if not self.device_id:
            return None
        return next(
            (
                item.entity_id
                for item in er.async_entries_for_device(
                    er.async_get(self.hass), self.device_id
                )
                if item.platform == "solarman"
                and item.domain == "sensor"
                and item.translation_key == hardware.DEVICE_KEY
            ),
            None,
        )

    def _find_clock_entity(self) -> str | None:
        """The Solarman date and time entity of the device, None when it has none."""
        if not self.device_id:
            return None
        return next(
            (
                item.entity_id
                for item in er.async_entries_for_device(
                    er.async_get(self.hass), self.device_id
                )
                if item.platform == "solarman"
                and item.domain == "datetime"
                and (
                    item.translation_key == clock.ENTITY_KEY
                    or item.entity_id.endswith(f"_{clock.ENTITY_KEY}")
                )
            ),
            None,
        )

    def _grid_option_key(self) -> tuple:
        options = self._options()
        voltages = options.get("grid_voltage_entities")
        return (
            options.get("grid_presence_entity"),
            tuple(voltages) if isinstance(voltages, list | tuple) else (),
        )

    def _find_grid_sources(self) -> tuple[str | None, tuple[str, ...]]:
        """The grid presence entity and the phase voltages: options first, then the device."""
        options = self._options()
        presence = options.get("grid_presence_entity") or None
        configured = options.get("grid_voltage_entities")
        voltages: tuple[str, ...] = (
            tuple(configured) if isinstance(configured, list | tuple) else ()
        )
        if self.device_id and (presence is None or not voltages):
            registry = er.async_get(self.hass)
            entries = [
                item
                for item in er.async_entries_for_device(registry, self.device_id)
                if item.platform == "solarman"
            ]
            if presence is None:
                presence = next(
                    (
                        item.entity_id
                        for item in entries
                        if item.domain == "binary_sensor"
                        and (
                            item.translation_key == grid.ENTITY_KEY
                            or item.entity_id.endswith(f"_{grid.ENTITY_KEY}")
                        )
                    ),
                    None,
                )
            if not voltages:
                voltages = tuple(
                    item.entity_id
                    for key in grid.VOLTAGE_KEYS
                    for item in entries
                    if item.domain == "sensor"
                    and (
                        item.translation_key == key
                        or item.entity_id.endswith(f"_{key}")
                    )
                )
        return presence, voltages

    @property
    def _issue_id(self) -> str:
        return f"deye_hardware_unverified_{self.entry.entry_id}"

    @property
    def _clock_issue_id(self) -> str:
        return f"deye_clock_skew_{self.entry.entry_id}"

    @property
    def clock_watched(self) -> bool:
        return self._clock_entity is not None

    def _tou_problem(self) -> str | None:
        return self.resolution.problem or clock.problem(self.clock)

    def _clock_attribute(self) -> dict | None:
        return self.clock.as_attribute() if self._clock_entity else None

    def _read_clock(self, now: datetime) -> None:
        """Judge the inverter clock; raise or clear the Repairs issue. Writes nothing."""
        if not self._clock_entity:
            self.clock = clock.ClockWatch()
        else:
            state = self.hass.states.get(self._clock_entity)
            self.clock = clock.observe(
                self.clock,
                {"state": state.state, "reported": state.last_reported}
                if state is not None
                else {},
                now,
                dt_util.get_default_time_zone(),
            )
        if not self.clock.active:
            ir.async_delete_issue(self.hass, DOMAIN, self._clock_issue_id)
            return
        reading = dt_util.as_local(datetime.fromisoformat(self.clock.inverter_clock))
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._clock_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="deye_clock_skew",
            translation_placeholders={
                "skew": f"{self.clock.skew_minutes:+d}",
                "inverter_clock": reading.strftime("%Y-%m-%d %H:%M"),
                "ha_time": dt_util.as_local(now).strftime("%Y-%m-%d %H:%M"),
            },
        )

    @property
    def grid_watched(self) -> bool:
        """Whether a presence entity or phase voltage is being read."""
        return self._grid_presence is not None or bool(self._grid_voltages)

    def _grid_attribute(self) -> dict | None:
        if not self.grid_watched:
            return None
        return self.state.grid.as_attribute()

    def _grid_diagnostics(self) -> dict:
        return {
            "presence_entity": self._grid_presence,
            "voltage_entities": list(self._grid_voltages),
            "enter_seconds": grid.enter_seconds_of(self._options()),
            "stable_seconds": grid.stable_seconds_of(self._options()),
            "watch": self.state.grid.as_attribute(),
            "edges": [
                {"at": event.at, "detail": event.detail}
                for event in self.state.history
                if event.kind == "grid"
            ][-grid.HISTORY_LIMIT :],
        }

    def _options(self) -> Mapping[str, Any]:
        """The expert limits of the controller: the grid sources and debounce live there."""
        return (self.entry.options.get("controller") or {}).get("limits") or {}

    def _grid_reading(self) -> grid.GridReading | None:
        if self._grid_presence is None and not self._grid_voltages:
            return None

        def state_of(entity_id: str | None) -> str | None:
            state = self.hass.states.get(entity_id) if entity_id else None
            return state.state if state else None

        return grid.read(
            state_of(self._grid_presence),
            [state_of(entity_id) for entity_id in self._grid_voltages],
        )

    def _read_grid(self, now: datetime) -> None:
        """Judge the grid presence; an edge is persisted and wakes the run and the planner."""
        options = self._options()
        before = self.state.grid
        watch = grid.observe(
            before,
            self._grid_reading(),
            now,
            enter_seconds=grid.enter_seconds_of(options),
            stable_seconds=grid.stable_seconds_of(options),
        )
        if watch != before:
            history = self.state.history
            if watch.active != before.active:
                detail = "started" if watch.active else "ended"
                history = core.remember(
                    history, core.Event(now.isoformat(), "grid", detail=detail)
                )
            self._replace(replace(self.state, grid=watch, history=history))
        if self._grid_timer:
            self._grid_timer()
            self._grid_timer = None
        deadline = grid.next_deadline(
            watch,
            enter_seconds=grid.enter_seconds_of(options),
            stable_seconds=grid.stable_seconds_of(options),
        )
        if deadline is not None:
            self._grid_timer = async_track_point_in_utc_time(
                self.hass, self._grid_due, max(deadline, now)
            )
        if watch.active != before.active:
            _LOGGER.warning("Grid outage %s", "started" if watch.active else "ended")
            self.coordinator.async_grid_changed(watch.active)

    @callback
    def _grid_due(self, now: datetime) -> None:
        self._grid_timer = None
        self._publish(now)

    def _read_hardware(self) -> None:
        state = (
            self.hass.states.get(self._device_sensor) if self._device_sensor else None
        )
        device = (
            dr.async_get(self.hass).async_get(self.device_id)
            if self.device_id
            else None
        )
        self.hardware = hardware.facts_from(
            state.state if state else None,
            state.attributes if state else None,
            device.model if device else None,
        )
        facts = self.hardware
        if self.engine == "native" and facts.known and not facts.verified:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="deye_hardware_unverified",
                translation_placeholders={
                    "device_type": facts.device_type or "?",
                    "protocol": facts.protocol or "?",
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)

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
        if domains & (_TOU_DOMAINS | {"select", "datetime", "binary_sensor", "sensor"}):
            if self.device_id:
                self._resolve()
            self._publish()
