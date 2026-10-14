"""Home Assistant wiring of the counter SOC estimator.

Reads the configured sources from the state machine, steps the pure core on every
source change and once a minute, keeps its state in a Store, and hands the
estimate to the coordinator, which validates it like any SOC. No entity sits
between the estimator and the planner.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import soc_estimator as core
from . import soc_estimator_config as cfg
from .engine.models import InputError
from .settings import DOMAIN
from .sources.bindings import EntityBinding

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
MAX_ANCHOR_HISTORY = 20
SAVE_INTERVAL = timedelta(seconds=60)
# The coordinator is told of a drifting estimate at most this often; anchors and
# availability changes are told at once.
NOTIFY_INTERVAL = timedelta(seconds=20)
NOTIFY_FRACTION = 0.0005
TICK = timedelta(seconds=60)
# The sensor is rewritten when the SOC moved this much (percentage points), on an
# anchor, and at least once a minute.
SENSOR_STEP = 0.1
ISSUE = "soc_estimator_unanchored"
# The only attribute worth keeping in the recorder; the rest is live detail.
RECORDED_ATTRIBUTES = frozenset({"anchor_kind"})

_KINDS = {
    "energy": {"kWh": 1.0, "Wh": 0.001, "MWh": 1000.0},
    "power": {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0},
    "volts": {"V": 1.0, "mV": 0.001, "kV": 1000.0},
    "amps": {"A": 1.0, "mA": 0.001},
}
_ROLE_KIND = {
    "charge_counter": "energy",
    "discharge_counter": "energy",
    "voltage": "volts",
    "current": "amps",
    "power": "power",
    "pv_power": "power",
    "grid_power": "power",
    "load_power": "power",
    "charge_limit": "amps",
    "discharge_limit": "amps",
    "grid_charge_current": "amps",
    "bms_soc": None,
}
_UNAVAILABLE = ("unknown", "unavailable", "")


class SocEstimator:
    """One entry's estimator: sources in, estimate and persisted state out."""

    def __init__(self, hass: HomeAssistant, entry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._store = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.soc_estimator"
        )
        self.config: dict = {}
        self.settings: core.EstimatorSettings | None = None
        self.state: core.EstimatorState | None = None
        self.estimate: core.Estimate | None = None
        self.reported_at: datetime | None = None
        self.error: str | None = None
        self.history: list[dict] = []
        self.bms_percent: float | None = None
        self._listeners: list[CALLBACK_TYPE] = []
        self._watched: tuple[str, ...] = ()
        self._active = False
        self._loaded = False
        self._last_save: datetime | None = None
        self._last_notice: tuple[datetime, float | None] | None = None
        self._stopped = False
        # Held back while the coordinator starts or applies a configuration:
        # it recalculates once the setup is done.
        self._quiet = True
        self._listeners_ha: list[Callable[[], None]] = []
        self._last_sensor: tuple[datetime, float | None] | None = None

    @property
    def active(self) -> bool:
        return self._active

    async def async_start(self, configuration: dict) -> None:
        raw = await self._store.async_load()
        if isinstance(raw, dict):
            self.state = core.state_from_dict(raw.get("state"))
            history = raw.get("history")
            if isinstance(history, list):
                self.history = [item for item in history if isinstance(item, dict)][
                    -MAX_ANCHOR_HISTORY:
                ]
        self._loaded = True
        self.configure(configuration)

    async def async_stop(self) -> None:
        self._stopped = True
        self._unsubscribe()
        if self.state is not None:
            await self._store.async_save(self._data())

    def configure(self, configuration: dict) -> None:
        """Adopt a configuration: (re)subscribe and take a first reading."""
        self.config = configuration
        self._unsubscribe()
        self._active = False
        self.error = None
        self.settings = None
        if not cfg.estimator_active(configuration):
            self.estimate = None
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            return
        try:
            cfg.validate(configuration)
            self.settings = cfg.settings(
                configuration,
                rated_pv_w=self._rated_pv_w(),
                max_charge_current_a=self._max_charge_current(configuration),
            )
        except InputError as err:
            self.error = str(err)
            self.estimate = None
            _LOGGER.warning("SOC estimator not running: %s", err)
            return
        self._active = True
        self._watch(self._source_ids(configuration))
        self._listeners.append(async_track_time_interval(self.hass, self._minute, TICK))
        quiet, self._quiet = self._quiet, True
        self.tick()
        self._quiet = quiet

    def start_notifying(self) -> None:
        self._quiet = False

    @property
    def _issue_id(self) -> str:
        return f"{ISSUE}_{self.entry.entry_id}"

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        """Call `update` when the published estimate moved enough to show."""
        self._listeners_ha.append(update)

        @callback
        def remove() -> None:
            if update in self._listeners_ha:
                self._listeners_ha.remove(update)

        return remove

    def diagnostics(self) -> dict[str, Any]:
        """The estimator block of the diagnostics download: no entity ids."""
        if not self._active:
            return {"enabled": False, **({"error": self.error} if self.error else {})}
        return {"enabled": True, **self.attributes()}

    def view(self) -> dict | None:
        """What the planner reads: the fraction and when its inputs last reported."""
        estimate = self.estimate
        if (
            not self._active
            or estimate is None
            or not estimate.available
            or estimate.soc_fraction is None
            or self.reported_at is None
        ):
            return None
        return {
            "fraction": estimate.soc_fraction,
            "reported_at": self.reported_at,
            # Read like any source state for the published input ages.
            "last_updated": self.reported_at.isoformat(),
            "top_confirmed": estimate.top_confirmed,
            "full_qualified": estimate.full_qualified,
        }

    def attributes(self) -> dict[str, Any]:
        """Diagnostic detail; BMS SOC is shown for comparison and never used."""
        estimate, state, settings = self.estimate, self.state, self.settings
        if estimate is None or state is None or settings is None:
            return {"error": self.error} if self.error else {}
        now = dt_util.utcnow()
        anchor_at = estimate.anchor_at
        return {
            "energy_kwh": None
            if estimate.energy_kwh is None
            else round(estimate.energy_kwh, 3),
            "anchor_kind": estimate.anchor_kind,
            "anchor_at": anchor_at.isoformat() if anchor_at else None,
            "anchor_age_h": None
            if anchor_at is None
            else round((now - anchor_at).total_seconds() / 3600, 2),
            "throughput_since_anchor_kwh": round(
                estimate.throughput_since_anchor_kwh, 3
            ),
            "pending_kwh": round(estimate.pending_kwh, 3),
            "unanchored_top": estimate.unanchored_top,
            "excess_kwh": round(estimate.excess_kwh, 3),
            "full_candidate": estimate.full_candidate,
            "measured_capacity_kwh": estimate.measured_capacity_kwh,
            "eta_charge": settings.eta_charge,
            "capacity_kwh": settings.capacity_kwh,
            "mode": "counter_estimator" if cfg.counter_mode(self.config) else "shadow",
            "bms_soc": self.bms_percent,
            "full_candidates": list(state.candidates),
            "anchors": list(self.history),
        }

    def tick(self, now: datetime | None = None) -> None:
        if not self._active or self._stopped or self.settings is None:
            return
        now = now or dt_util.utcnow()
        inputs = self._inputs(now)
        if self.state is None:
            self.state = self._initial_state(inputs, now)
            if self.state is None:
                self._publish(None, now, ())
                return
            self._remember(self.state.anchor)
        before = self.state.anchor
        estimate = core.step(self.state, self.settings, inputs)
        if (self.state.anchor.kind, self.state.anchor.at) != (before.kind, before.at):
            self._remember(self.state.anchor)
        self._publish(estimate, now, estimate.events)

    def _publish(self, estimate, now, events) -> None:
        previous = self.estimate
        self.estimate = estimate
        anchored = bool(events)
        if self.state is not None and (
            anchored
            or self._last_save is None
            or now - self._last_save >= SAVE_INTERVAL
        ):
            self._last_save = now
            self._store.async_delay_save(self._data, 1)
        fraction = None if estimate is None else estimate.soc_fraction
        self._update_issue(estimate)
        self._update_sensor(now, fraction, anchored)
        was = None if previous is None else previous.soc_fraction
        last = self._last_notice
        due = (
            anchored
            or (fraction is None) != (was is None)
            or last is None
            or (
                fraction is not None
                and last[1] is not None
                and abs(fraction - last[1]) >= NOTIFY_FRACTION
                and now - last[0] >= NOTIFY_INTERVAL
            )
        )
        if due and not self._quiet:
            self._last_notice = (now, fraction)
            self.coordinator.soc_estimate_changed()

    def _update_sensor(self, now, fraction, anchored) -> None:
        last = self._last_sensor
        if (
            not anchored
            and last is not None
            and (fraction is None) == (last[1] is None)
            and now - last[0] < SAVE_INTERVAL
            and (fraction is None or abs(fraction - last[1]) * 100 < SENSOR_STEP)
        ):
            return
        self._last_sensor = (now, fraction)
        for update in list(self._listeners_ha):
            update()

    def _update_issue(self, estimate) -> None:
        """A repair issue while the throughput since the last anchor is large."""
        if estimate is None or not estimate.available or self.settings is None:
            return
        cycles = cfg.drift_cycles(self.config)
        if estimate.throughput_since_anchor_kwh > cycles * self.settings.capacity_kwh:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE,
                translation_placeholders={
                    "name": self.entry.title,
                    "cycles": f"{cycles:g}",
                    "kind": estimate.anchor_kind,
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)

    def _remember(self, anchor: core.Anchor) -> None:
        self.history.append(
            {
                "kind": anchor.kind,
                "at": anchor.at.isoformat(),
                "energy_kwh": round(anchor.energy_kwh, 3),
                "charge_kwh": anchor.charge_kwh,
                "discharge_kwh": anchor.discharge_kwh,
            }
        )
        del self.history[:-MAX_ANCHOR_HISTORY]

    def _data(self) -> dict:
        return {
            "state": core.state_to_dict(self.state) if self.state else None,
            "history": list(self.history),
        }

    def _initial_state(self, inputs: core.Inputs, now: datetime):
        """Seed or user-stated start; None while the counters cannot be read."""
        if inputs.charge_kwh is None or inputs.discharge_kwh is None:
            return None
        values = cfg.block(self.config)
        seed = values.get("seed")
        if isinstance(seed, dict):
            state = core.seed_from_template(seed, now=now)
            if state is not None:
                return state
            _LOGGER.warning("SOC estimator seed unusable; starting from the stated SOC")
        percent = values.get("initial_soc_percent", cfg.DEFAULT_INITIAL_SOC_PERCENT)
        return core.initial_state(
            self.settings,
            now,
            inputs.charge_kwh,
            inputs.discharge_kwh,
            float(percent) / 100,
        )

    def _inputs(self, now: datetime) -> core.Inputs:
        config = self.config
        reads = {role: self._read(role) for role in cfg.ROLES}

        def value(role: str, key: str | None = None) -> float | None:
            found = reads[role]
            if found is None:
                return None
            return found[0] * (cfg.sign(config, key) if key else 1.0)

        power = value("power", "power_sign")
        current = value("current", "current_sign")
        pv, grid = value("pv_power"), value("grid_power", "grid_sign")
        load = value("load_power")
        if load is None and None not in (pv, grid, power):
            load = max(0.0, pv + grid + power)
        stamps = [
            reads[role][1]
            for role in ("voltage", "current", "power")
            if reads[role] is not None
        ]
        self.reported_at = min(max(stamps), now) if stamps else None
        self.bms_percent = self._bms_percent()
        return core.Inputs(
            now,
            None if reads["charge_counter"] is None else reads["charge_counter"][0],
            None
            if reads["discharge_counter"] is None
            else reads["discharge_counter"][0],
            power,
            value("voltage"),
            current,
            pv,
            load,
            grid,
            self._facts(reads, now),
        )

    def _facts(self, reads, now: datetime | None = None) -> core.ControllerFacts:
        native = self._native()
        charge = reads["charge_limit"]
        discharge = reads["discharge_limit"]
        grid = reads["grid_charge_current"]
        state = direction = program = None
        if native is not None:
            entities, prefix, runtime = native
            charge = charge or self._state_number(entities.charge, "amps")
            discharge = discharge or self._state_number(entities.discharge, "amps")
            grid = grid or self._state_number(entities.grid, "amps")
            if runtime.get("code") == "ok":
                state = runtime.get("state")
            active = runtime.get("active_tou")
            if active and prefix:
                option = self.hass.states.get(f"select.{prefix}{active}_charging")
                direction = option.state if option else None
                volts = self._state_number(f"number.{prefix}{active}_voltage", None)
                # The program voltage is in units of the 48 V scale; the pack reads x10.
                program = None if volts is None else volts[0] * 10
        return core.ControllerFacts(
            charge_limit_a=None if charge is None else charge[0],
            grid_charge_a=0.0 if grid is None else grid[0],
            state=state,
            program_direction=direction,
            program_voltage_v=program,
            discharge_limit_a=None if discharge is None else discharge[0],
            balance_hold=self._balance_hold(now or dt_util.utcnow()),
        )

    def _balance_hold(self, now: datetime) -> bool:
        """Whether the published plan's row in force is an LFP balance hold."""
        data = getattr(self.coordinator, "data", None)
        if not isinstance(data, dict) or not data.get("valid"):
            return False
        for row in data.get("intervals") or ():
            if row.get("balance_hold") is not True:
                continue
            try:
                start = datetime.fromisoformat(row["start"])
                end = datetime.fromisoformat(row["end"])
            except KeyError, TypeError, ValueError:
                continue
            if start <= now < end:
                return True
        return False

    def _native(self):
        controller = getattr(self.coordinator, "controller", None)
        port = getattr(getattr(controller, "host", None), "port", None)
        setup = getattr(port, "setup", None)
        if controller is None or setup is None:
            return None
        return setup.entities, setup.entities.prefix, controller.state.runtime or {}

    def _read(self, role: str) -> tuple[float, datetime] | None:
        """Value in base units and the report time of one role, None if unreadable."""
        found = cfg.binding(self.config, role)
        if found is None:
            return None
        return self._state_number(found, _ROLE_KIND[role])

    def _state_number(self, source, kind) -> tuple[float, datetime] | None:
        binding = source if isinstance(source, EntityBinding) else EntityBinding(source)
        state = self.hass.states.get(binding.entity_id)
        if state is None or state.state in _UNAVAILABLE:
            return None
        raw = (
            state.attributes.get(binding.attribute)
            if binding.attribute
            else state.state
        )
        try:
            number = float(raw)
        except TypeError, ValueError:
            return None
        if not isfinite(number):
            return None
        if kind is not None and binding.attribute is None:
            unit = state.attributes.get("unit_of_measurement")
            factors = _KINDS[kind]
            if unit is not None and unit not in factors:
                return None
            number *= factors.get(unit, 1.0)
        return number, state.last_reported

    def _bms_percent(self) -> float | None:
        found = self._read("bms_soc")
        return None if found is None else found[0]

    def _rated_pv_w(self) -> float:
        kwp = (self.entry.options.get("atlas") or {}).get("pv_kwp")
        return float(kwp) * 1000 if isinstance(kwp, int | float) and kwp > 0 else 0.0

    def _max_charge_current(self, configuration: dict) -> float:
        found = cfg.binding(configuration, "charge_limit")
        state = self.hass.states.get(found.entity_id) if found else None
        maximum = state.attributes.get("max") if state else None
        return float(maximum) if isinstance(maximum, int | float) else 0.0

    def _source_ids(self, configuration: dict) -> tuple[str, ...]:
        ids = dict.fromkeys(cfg.entity_ids(configuration))
        native = self._native()
        if native is not None:
            entities = native[0]
            ids.update(
                dict.fromkeys((entities.charge, entities.discharge, entities.grid))
            )
        return tuple(ids)

    def _watch(self, entity_ids: tuple[str, ...]) -> None:
        self._watched = entity_ids
        if entity_ids:
            self._listeners.append(
                async_track_state_change_event(
                    self.hass, list(entity_ids), self._changed
                )
            )

    def _unsubscribe(self) -> None:
        for unsubscribe in self._listeners:
            unsubscribe()
        self._listeners = []
        self._watched = ()

    @callback
    def _changed(self, _event) -> None:
        self.tick()

    @callback
    def _minute(self, now: datetime) -> None:
        self.tick(now)


class _PreviewEntry:
    entry_id = "preview"

    def __init__(self) -> None:
        self.options: dict = {}


def preview_view(hass: HomeAssistant, candidate: dict, now: datetime) -> dict | None:
    """The planner's SOC view for a configuration still being edited.

    A throwaway estimator reads the sources once and starts from the seed or the
    stated SOC; nothing is stored or subscribed, because the running estimator
    adopts the configuration only when it is saved.
    """
    estimator = SocEstimator(hass, _PreviewEntry(), None)
    estimator.config = candidate
    try:
        estimator.settings = cfg.settings(
            candidate, max_charge_current_a=estimator._max_charge_current(candidate)
        )
    except InputError:
        return None
    inputs = estimator._inputs(now)
    estimator.state = estimator._initial_state(inputs, now)
    if estimator.state is None:
        return None
    estimator._active = True
    estimator.estimate = core.step(estimator.state, estimator.settings, inputs)
    return estimator.view()
