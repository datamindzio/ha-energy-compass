"""Home Assistant shell of the window notifications: triggers, store, notify calls, issue."""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import (
    CALLBACK_TYPE,
    HomeAssistant,
    ServiceCall,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import notifications as core
from .notifications import FAILED_ATTEMPTS_FOR_ISSUE, SEND_TIMEOUT_S
from .settings import DOMAIN, native_notifications_active

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "{domain}.{entry_id}.notifications"
ISSUE_FAILED = "notify_send_failed_{entry_id}"
ISSUE_DEPRECATED = "notify_blueprint_deprecated_{entry_id}"
ISSUE_SUPPRESSED = "notify_blueprint_suppressed_{entry_id}"
SERVICE_TEST = "send_test_notification"
BLUEPRINT_PATH = "energy_compass/notifications.yaml"
BLUEPRINT_FILE = "/notifications.yaml"
BLUEPRINT_SOURCE_SUFFIX = "ha-energy-compass/blob/main/blueprints/automation/energy_compass/notifications.yaml"
TEST_SCHEMA = vol.Schema({vol.Optional("config_entry_id"): cv.string})
ERROR_LIMIT = 200
_EXPECTED = (ServiceNotFound, HomeAssistantError, vol.Invalid, TimeoutError)
UNKNOWN_ERROR = "—"


def _cut(value: object) -> str:
    return str(value)[:ERROR_LIMIT]


def _pairs(settings: Mapping) -> list[tuple[str, str, bool]]:
    """(target name, service or entity id, is entity) in call order: services first."""
    pairs = [
        (f"notify.{name}", name, False)
        for name in settings.get("notify_services") or []
    ]
    pairs += [
        (entity_id, entity_id, True)
        for entity_id in settings.get("notify_entities") or []
    ]
    return pairs


def targets(settings: Mapping) -> tuple[str, ...]:
    """Every configured target as a printable name."""
    return tuple(name for name, _, _ in _pairs(settings))


async def _call(
    hass: HomeAssistant, name: str, ref: str, entity: bool, title: str, message: str
) -> dict:
    payload = {"title": title, "message": message}
    try:
        async with asyncio.timeout(SEND_TIMEOUT_S):
            if entity:
                await hass.services.async_call(
                    "notify",
                    "send_message",
                    {**payload, "entity_id": ref},
                    blocking=True,
                )
            else:
                await hass.services.async_call("notify", ref, payload, blocking=True)
    except _EXPECTED as err:
        error = "Timed out" if isinstance(err, TimeoutError) else _cut(err) or "failed"
        if isinstance(err, ServiceNotFound):
            error = "Service not found"
        _LOGGER.warning("Window notification to %s failed: %s", name, error)
        return {"target": name, "ok": False, "error": error}
    except Exception as err:
        _LOGGER.warning(
            "Window notification to %s failed: %s", name, err, exc_info=True
        )
        return {"target": name, "ok": False, "error": _cut(err) or type(err).__name__}
    return {"target": name, "ok": True}


async def async_send(
    hass: HomeAssistant, settings: Mapping, title: str, message: str
) -> list[dict]:
    """One concurrent call per target; every result is returned, nothing is raised."""
    return list(
        await asyncio.gather(
            *(
                _call(hass, name, ref, entity, title, message)
                for name, ref, entity in _pairs(settings)
            )
        )
    )


async def notification_blueprint_automations(hass: HomeAssistant) -> list[str]:
    """Entity ids of the automations that are on and use the notification blueprint.

    Two forms of `referenced_blueprint` count (DD-11): the path of a copy installed by
    hand or HACS, and any `.../notifications.yaml` whose loaded metadata names this
    repository as its source, which is how a my.home-assistant import is stored.
    """
    from homeassistant.components import automation
    from homeassistant.components.automation.helpers import async_get_blueprints

    if automation.DATA_COMPONENT not in hass.data:
        return []
    found: list[str] = []
    for entity in hass.data[automation.DATA_COMPONENT].entities:
        path = entity.referenced_blueprint
        if (
            not path
            or not (state := hass.states.get(entity.entity_id))
            or state.state != "on"
        ):
            continue
        if path == BLUEPRINT_PATH:
            found.append(entity.entity_id)
        elif path.endswith(BLUEPRINT_FILE):
            try:
                blueprint = await async_get_blueprints(hass).async_get_blueprint(path)
            except HomeAssistantError:
                continue
            source = str(blueprint.metadata.get("source_url") or "")
            if source.endswith(BLUEPRINT_SOURCE_SUFFIX):
                found.append(entity.entity_id)
    return found


def _loaded_notifier(hass: HomeAssistant, call: ServiceCall) -> WindowNotifier:
    """The notifier of the one entry the call is for (DD-10)."""
    requested = call.data.get("config_entry_id")
    if requested:
        entry = hass.config_entries.async_get_entry(requested)
        entries = [entry] if entry is not None and entry.domain == DOMAIN else []
    else:
        entries = hass.config_entries.async_entries(DOMAIN)
    entries = [item for item in entries if item.state is ConfigEntryState.LOADED]
    if not entries and requested:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="notify_entry_not_found",
            translation_placeholders={"entry": requested},
        )
    if len(entries) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="notify_entry_required"
            if entries
            else "notify_entry_not_found",
            translation_placeholders={"entry": "-"},
        )
    notifier = getattr(entries[0].runtime_data, "notifier", None)
    if notifier is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="notify_entry_not_found",
            translation_placeholders={"entry": entries[0].entry_id},
        )
    return notifier


async def _async_send_test(call: ServiceCall) -> dict:
    return await _loaded_notifier(call.hass, call).async_send_test()


def async_register_services(hass: HomeAssistant) -> None:
    """Register the test action once, domain-wide."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_TEST,
        _async_send_test,
        schema=TEST_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


class WindowNotifier:
    """Evaluate on every coordinator update and each minute; save, re-check, then send."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, coordinator: Any
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.state = core.SenderState()
        self._store = Store(
            hass, 1, STORE_KEY.format(domain=DOMAIN, entry_id=entry.entry_id)
        )
        self._unsub_update: CALLBACK_TYPE | None = None
        self._unsub_tick: CALLBACK_TYPE | None = None
        self._running = False
        self._checking = False
        self._stopped = False
        self._seen: tuple[str, ...] = ()
        self._failed_targets: list[str] = []
        self._last_error = ""
        self.last_code: str | None = None
        self.last_at: str | None = None

    @property
    def _issue_id(self) -> str:
        return ISSUE_FAILED.format(entry_id=self.entry.entry_id)

    @property
    def _settings(self) -> Mapping:
        return self.coordinator.configuration.get("settings", {})

    async def async_start(self) -> None:
        """Load the store before any listener is attached."""
        raw = await self._store.async_load()
        self.state, valid = core.load_state(raw)
        if raw is not None and not valid:
            _LOGGER.warning("Invalid stored notification state; starting fresh")
        self._seen = targets(self._settings)
        if self.state.failures >= FAILED_ATTEMPTS_FOR_ISSUE:
            self._failed_targets = list(self._seen)
            # The errors of the failed attempts are not stored; the issue is shown in the
            # user's language, so the placeholder carries no words.
            self._last_error = UNKNOWN_ERROR
            self._raise_issue()
        self._unsub_update = self.coordinator.async_add_listener(self._schedule)
        self._unsub_tick = async_track_time_change(self.hass, self._tick, second=0)

    async def async_stop(self) -> None:
        self._stopped = True
        for unsubscribe in (self._unsub_update, self._unsub_tick):
            if unsubscribe:
                unsubscribe()
        self._unsub_update = self._unsub_tick = None
        for issue in (ISSUE_FAILED, ISSUE_DEPRECATED, ISSUE_SUPPRESSED):
            ir.async_delete_issue(
                self.hass, DOMAIN, issue.format(entry_id=self.entry.entry_id)
            )

    @callback
    def _tick(self, _now: datetime) -> None:
        self._schedule()
        if not self._checking and not self._stopped:
            self.entry.async_create_task(
                self.hass,
                self.async_check_blueprint(),
                "energy_compass_notification_blueprint",
            )

    def _sender(self) -> str:
        """Who sends now: the published plan when it has spoken, else the live options."""
        published = (self.coordinator.data or {}).get("notification_preferences")
        sender = published.get("notify_sender") if isinstance(published, dict) else None
        if sender in (core.SENDER_INTEGRATION, "blueprint"):
            return sender
        return (
            core.SENDER_INTEGRATION
            if native_notifications_active(self._settings)
            else "blueprint"
        )

    async def async_check_blueprint(self) -> None:
        """DD-11: tell a user of the deprecated blueprint what it does and what to do."""
        if self._checking or self._stopped:
            return
        self._checking = True
        try:
            found: list[str] = []
            if self._settings.get("notify_enabled"):
                found = await notification_blueprint_automations(self.hass)
            if self._stopped:
                return
            sender = self._sender()
            wanted = {
                ISSUE_DEPRECATED: bool(found) and sender != core.SENDER_INTEGRATION,
                ISSUE_SUPPRESSED: bool(found) and sender == core.SENDER_INTEGRATION,
            }
            for template, raise_it in wanted.items():
                issue = template.format(entry_id=self.entry.entry_id)
                if raise_it:
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        issue,
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key=template.removesuffix("_{entry_id}"),
                        translation_placeholders={"automation": ", ".join(found)},
                    )
                else:
                    ir.async_delete_issue(self.hass, DOMAIN, issue)
        except Exception:
            _LOGGER.exception("Notification blueprint check failed")
        finally:
            self._checking = False

    async def async_send_test(self) -> dict:
        """DD-10: one fixed message to every target, whatever the gates and the state say."""
        settings = self._settings
        if not targets(settings):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="notify_no_target"
            )
        language = core.resolve_language(
            str(settings.get("notify_language") or "auto"), self.hass.config.language
        )
        title, message = core.test_texts(language)
        results = await async_send(self.hass, settings, title, message)
        sent = sum(1 for result in results if result["ok"])
        self._log_entry("test", f"sent to {sent} of {len(results)} targets")
        if sent == len(results):
            await self._attempt_ok()
        return {"results": results}

    @callback
    def _schedule(self) -> None:
        if self._running or self._stopped:
            return
        self.entry.async_create_task(
            self.hass, self.async_evaluate(), "energy_compass_notifications"
        )

    async def async_evaluate(self) -> None:
        """Single-flight: an evaluation that starts while another runs is dropped."""
        if self._running or self._stopped:
            return
        self._running = True
        try:
            await self._evaluate()
        except Exception:
            _LOGGER.exception("Window notification evaluation failed")
        finally:
            self._running = False

    async def _evaluate(self) -> None:
        now = dt_util.utcnow()
        tz = dt_util.get_default_time_zone()
        settings = self._settings
        configured = targets(settings)
        await self._targets_changed(configured)
        data = self.coordinator.data or {}
        language = core.resolve_language(
            str(settings.get("notify_language") or "auto"), self.hass.config.language
        )
        decision = core.evaluate(
            data, self.state, now, tz, has_target=bool(configured), language=language
        )
        self.last_code, self.last_at = decision.code, now.isoformat()
        if decision.code == "extended":
            if decision.state != self.state:
                self.state = decision.state
                await self._persist()
            return
        if decision.code != "send":
            return

        # DD-7: the window, count and attempt time are spent before the gate and the call.
        self.state = decision.state
        generation = data.get("generated_at")
        try:
            await self._store.async_save(core.dump_state(self.state))
        except Exception as err:
            _LOGGER.warning(
                "Could not save the notification state: %s", err, exc_info=True
            )
            self._attempt_failed([], _cut(err))
            self.last_code = "save_failed"
            return
        if not self._still_allowed(generation):
            self.last_code = "rechecked"
            return

        settings = self._settings
        results = await async_send(
            self.hass, settings, decision.title, decision.message
        )
        sent = sum(1 for result in results if result["ok"])
        self._log(decision, tz, sent, len(results))
        if sent == len(results):
            await self._attempt_ok()
        else:
            failed = [result for result in results if not result["ok"]]
            self._attempt_failed(
                [result["target"] for result in failed], failed[-1].get("error", "")
            )
            await self._persist()

    def _still_allowed(self, generation: object) -> bool:
        """The blueprint's final gate, on the data and configuration of now."""
        data = self.coordinator.data or {}
        now = dt_util.utcnow()
        prefs = core.preferences(data)
        return bool(
            prefs is not None
            and core.gates_open(data, now)
            and targets(self._settings)
            and data.get("generated_at") == generation
            and not core.in_quiet_hours(
                prefs, now.astimezone(dt_util.get_default_time_zone()).time()
            )
        )

    def _log(self, decision: core.Decision, tz: Any, sent: int, total: int) -> None:
        kind = "LIMIT" if decision.kind == "limit" else "favorable"
        window = decision.window
        label = core.window_label(window.s, window.e, tz) if window else ""
        self._log_entry(f"{kind} {label}", f"sent to {sent} of {total} targets")

    def _log_entry(self, subject: str, outcome: str) -> None:
        from homeassistant.components.logbook import async_log_entry

        async_log_entry(
            self.hass,
            "Energy Compass notification",
            f"{subject}: {outcome}",
            domain=DOMAIN,
            entity_id=er.async_get(self.hass).async_get_entity_id(
                "sensor", DOMAIN, f"{self.entry.entry_id}_plan"
            ),
        )

    async def _persist(self) -> None:
        try:
            await self._store.async_save(core.dump_state(self.state))
        except Exception as err:
            _LOGGER.warning(
                "Could not save the notification state: %s", err, exc_info=True
            )

    def _attempt_failed(self, failed: list[str], error: str) -> None:
        self.state = replace(self.state, failures=self.state.failures + 1)
        self._failed_targets = failed or list(self._seen)
        self._last_error = error
        if self.state.failures >= FAILED_ATTEMPTS_FOR_ISSUE:
            self._raise_issue()

    async def _attempt_ok(self) -> None:
        if self.state.failures:
            self.state = replace(self.state, failures=0)
            await self._persist()
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)

    def _raise_issue(self) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="notify_send_failed",
            translation_placeholders={
                "count": str(self.state.failures),
                "targets": ", ".join(self._failed_targets),
                "error": _cut(self._last_error),
            },
        )

    async def _targets_changed(self, configured: tuple[str, ...]) -> None:
        """DD-12: another target list starts the failure count and the issue over."""
        if configured == self._seen:
            return
        self._seen = configured
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        if self.state.failures:
            self.state = replace(self.state, failures=0)
            await self._persist()

    @property
    def diagnostics(self) -> dict:
        return {
            "state": core.dump_state(self.state),
            "last_decision": {"code": self.last_code, "at": self.last_at},
            "targets": list(targets(self._settings)),
        }
