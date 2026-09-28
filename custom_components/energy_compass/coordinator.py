"""Entry-owned scheduling, input generations and coherent result publication."""

import asyncio
import json
import logging
from collections import deque
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from functools import partial

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .balance_tracker import (
    balance_settings,
    empty_state,
    observe,
    planned_window,
    seed_from_history,
    sensor_state,
    valid_state,
)
from .balance_tracker import (
    status as balance_status,
)
from .config_models import SourceConfig
from .daily_export import (
    DAILY_EXPORT_MEASUREMENTS,
    daily_export_active,
    daily_export_observations,
)
from .engine.dispatch_policy import dwell_group
from .engine.models import InputError, SolveError
from .flow_schema import entity_ids, rebind_configuration, snapshot
from .runtime import (
    _coverage,
    _soc,
    async_balance_history,
    async_history,
    available_forecasts,
    compute,
    effective_settings,
    freshness_deadline,
    measurement_diagnostics,
    restore_commitment,
    restore_export_commitment,
    restore_grid_charge_commitment,
)
from .settings import DOMAIN, merged_configuration
from .sources.bindings import merge_continuations, parse_intervals, parse_timestamp

_LOGGER = logging.getLogger(__name__)
# An upward SOC jump (BMS recalibration near full) keeps the retained plan for
# this long while fresh reports confirm the new level; beyond it, it is an error.
SOC_REBASE_GRACE_SECONDS = 300


class EnergyCompassCoordinator(DataUpdateCoordinator):
    """Keep one worker per entry and discard superseded or unloaded generations."""

    def __init__(self, hass, entry):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self.entry = entry
        self.configuration = merged_configuration(entry)
        self.data = {"status": "calculating", "valid": False}
        self._closed = False
        self._generation = 0
        # Bumped with _generation only when a running result becomes wrong
        # (configuration, registry or invalid input), not when it is merely old.
        self._epoch = 0
        self._pending = False
        self._runner = None
        self._worker = None
        self._debounce = None
        self._boundary = None
        self._refresh_due = None
        self._inputs_valid_until = None
        self._source_unsub = None
        self._registry_unsub = None
        self._fingerprint = None
        self._soc_reference = None
        self._previous_soc = None
        self._soc_recovery = None
        self._soc_recovery_last = None
        self._soc_rebase_since = None
        self._published_at = None
        # The rate limit measures starts, not publications: a solve that fails
        # or times out consumed the same budget as one that published.
        self._calculation_started_at = None
        self._calculation_starts = deque()
        self._calculations = 0
        self.last_successful_plan_at = None
        self._anchors = {}
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.anchors")
        self._battery_commitment = None
        self._dispatch_store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.dispatch")
        self._export_commitment = None
        self._export_store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.export")
        self._grid_charge_commitment = None
        self._grid_charge_store = Store(
            hass, 1, f"{DOMAIN}.{entry.entry_id}.grid_charge"
        )
        self._balance = None
        # False while the state is only a failed seed: nothing worth persisting,
        # so the next start retries the recorder.
        self._balance_persist = True
        self._balance_store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.balance")
        self.previous_plan = None
        # Set by async_setup_entry only when options["atlas"]["enabled"] (ADR-0019 §3).
        self.atlas = None

    async def async_start(self):
        """Restore small observation anchors and attach only this entry's listeners."""
        self._anchors = await self._store.async_load() or {}
        self._battery_commitment = restore_commitment(
            await self._dispatch_store.async_load(), dt_util.utcnow()
        )
        self._export_commitment = restore_export_commitment(
            await self._export_store.async_load(), dt_util.utcnow()
        )
        self._grid_charge_commitment = restore_grid_charge_commitment(
            await self._grid_charge_store.async_load(), dt_util.utcnow()
        )
        self._balance = await self._balance_store.async_load()
        if self._balance is not None and not valid_state(self._balance):
            _LOGGER.warning(
                "Invalid stored balance state %r; reseeding from history",
                self._balance,
            )
            self._balance = None
        if self._balance is None:
            self._balance = await self._seed_balance()
        self._registry_unsub = self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed
        )
        self._subscribe()
        await self.async_recalculate()

    async def async_apply_configuration(self, config: dict) -> None:
        """Adopt a configuration written outside the flows and supersede any run.

        Order is load-bearing: persist, then replace the cached snapshot, then bump
        the generation, then ask for a recalculation.
        """
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, "configuration": config}
        )
        self.configuration = rebind_configuration(self.hass, deepcopy(config))
        self._generation += 1
        self._epoch += 1
        self._fingerprint = None
        self._pending = True
        await self.async_recalculate()

    async def _seed_balance(self):
        """First start: replay recorder SOC; no qualifying hold means due now."""
        now = dt_util.utcnow()
        try:
            samples = await async_balance_history(self.hass, self.configuration, now)
        except Exception:
            _LOGGER.warning(
                "Balance history unavailable; balance is due now", exc_info=True
            )
            self._balance_persist = False
            return empty_state()
        # Recorder history stores changes only: a pinned SOC leaves no samples,
        # so the seed replays it gap-tolerant.
        settings = replace(
            balance_settings(self.configuration["settings"]), max_gap_seconds=None
        )
        state = seed_from_history(samples, settings)
        self._balance_store.async_delay_save(lambda: self._balance, 1)
        return state

    def _observe_balance(self, values, percent, at):
        if self._balance is None:
            return
        updated = observe(self._balance, percent, at, balance_settings(values))
        if updated != self._balance:
            self._balance = updated
            self._balance_persist = True
            self._balance_store.async_delay_save(lambda: self._balance, 1)

    def balance_report(self, now=None):
        """Tracker phase joined with the published plan's hold window."""
        if self._balance is None:
            return None
        now = now or dt_util.utcnow()
        report = balance_status(
            self._balance, now, balance_settings(self.configuration["settings"])
        )
        window = (
            planned_window(self.data.get("intervals", []))
            if self.data.get("valid")
            else None
        )
        return {
            **report,
            "state": sensor_state(report["phase"], window is not None),
            "planned_start": window["start"] if window else None,
            "planned_end": window["end"] if window else None,
            "planned_mode": window["mode"] if window else None,
        }

    def _subscribe(self):
        if self._source_unsub:
            self._source_unsub()
            self._source_unsub = None
        try:
            self.configuration = rebind_configuration(
                self.hass, merged_configuration(self.entry)
            )
            ids = entity_ids(self.configuration)
            if ids:
                self._source_unsub = async_track_state_change_event(
                    self.hass, ids, self._source_changed
                )
            ir.async_delete_issue(self.hass, DOMAIN, self.entry.entry_id)
        except InputError as err:
            self._invalidate("invalid_input", str(err))
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self.entry.entry_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="source_removed",
                translation_placeholders={"name": self.entry.title},
            )

    @callback
    def _registry_changed(self, event):
        tracked = entity_ids(self.configuration)
        if (
            event.data.get("entity_id") in tracked
            or event.data.get("old_entity_id") in tracked
        ):
            self._generation += 1
            self._epoch += 1
            self._fingerprint = None
            self._subscribe()
            self._schedule(0)

    def _content(self, states, values):
        source, missing = available_forecasts(
            SourceConfig.from_dict(self.configuration["sources"]), states
        )
        selected = [missing]

        def visit(value):
            if isinstance(value, dict):
                if "entity_id" in value:
                    state = states.get(value["entity_id"], {})
                    raw = (
                        state.get("attributes", {}).get(value.get("attribute"))
                        if value.get("attribute")
                        else state.get("state")
                    )
                    try:
                        raw = float(raw)
                    except TypeError, ValueError:
                        pass
                    if (
                        source.soc
                        and value["entity_id"] == source.soc.entity_id
                        and value.get("attribute") == source.soc.attribute
                    ):
                        energy, _ = _soc(
                            self.configuration,
                            source,
                            values,
                            states,
                            dt_util.utcnow(),
                            self._previous_soc,
                        )
                        percent = energy / values["capacity_kwh"] * 100
                        if (
                            self._soc_reference is not None
                            and abs(percent - self._soc_reference)
                            < values["soc_trigger_percent"]
                        ):
                            raw = self._soc_reference
                        else:
                            self._soc_reference = percent
                            raw = percent
                    selected.append((value["entity_id"], value.get("attribute"), raw))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        planning_inputs = {"helpers": self.configuration["helpers"]}
        if source.soc:
            planning_inputs["soc"] = source.soc.to_dict()
        if daily_export_active(values):
            for name in DAILY_EXPORT_MEASUREMENTS:
                planning_inputs[name] = self.configuration.get("measurements", {}).get(
                    name
                )
        if source.battery_enabled and values["daily_cycles"]:
            planning_inputs["throughput_today"] = self.configuration.get(
                "measurements", {}
            ).get("throughput_today")
        visit(planning_inputs)
        groups = [
            price.forecast
            for price in (source.buy, source.sell)
            if price.mode == "forecast"
        ]
        groups.extend(source.pv.arrays)
        if source.load.forecast:
            groups.append((source.load.forecast,))
        for group in groups:
            rows = merge_continuations(
                tuple(
                    parse_intervals(states, binding, dt_util.utcnow())
                    for binding in group
                )
            )
            selected.append(
                [
                    (row.start.isoformat(), row.end.isoformat(), row.value)
                    for row in rows
                ]
            )
        return json.dumps(selected, sort_keys=True, default=str)

    def _inputs(self):
        # A failed health check consumes the previous deadline; recovery keeps
        # using source events and health polling, never a past 100 ms timer.
        self._inputs_valid_until = None
        config = rebind_configuration(self.hass, merged_configuration(self.entry))
        self.configuration = config
        states = snapshot(self.hass, config)
        now = dt_util.utcnow()
        values = effective_settings(config, states, now)
        daily_export_observations(config, states, values, now)
        source, _ = available_forecasts(
            SourceConfig.from_dict(config["sources"]), states
        )
        required = entity_ids(
            {"sources": source.to_dict(), "helpers": config["helpers"]}
        )
        for entity_id in required:
            if entity_id not in states or states[entity_id]["state"] in (
                "unknown",
                "unavailable",
            ):
                if source.soc and entity_id == source.soc.entity_id:
                    self._soc_recovery = self._soc_recovery_last = None
                raise InputError(f"missing or unavailable source: {entity_id}")
        groups = [
            price.forecast
            for price in (source.buy, source.sell)
            if price.mode == "forecast"
        ]
        groups.extend(source.pv.arrays)
        if source.load.forecast:
            groups.append((source.load.forecast,))
        for group in groups:
            rows = merge_continuations(
                tuple(parse_intervals(states, binding, now) for binding in group)
            )
            _coverage(rows, now)
        if source.battery_enabled:
            self._validate_soc(config, source, values, states, now)
            energy, (stamp, _) = _soc(
                config, source, values, states, now, self._previous_soc
            )
            self._observe_balance(values, energy / values["capacity_kwh"] * 100, stamp)
        deadline = freshness_deadline(config, states, values, now)
        if deadline is not None and deadline <= now:
            raise InputError("expired_inputs")
        self._inputs_valid_until = deadline
        return config, states, now, values

    def _validate_soc(self, config, source, values, states, now):
        """Rebase a corrected SOC only after fresh plausible reports span a minute."""
        try:
            _soc(config, source, values, states, now, self._previous_soc)
        except InputError as err:
            if str(err) != "SOC measurement jump":
                self._soc_recovery = self._soc_recovery_last = None
                raise
            # Range, age, unit and optional BMS agreement still apply independently.
            try:
                _, observation = _soc(config, source, values, states, now)
            except InputError:
                self._soc_recovery = self._soc_recovery_last = None
                raise
            if observation[0] <= self._previous_soc[0]:
                self._soc_recovery = self._soc_recovery_last = None
                raise
            candidate = self._soc_recovery
            if candidate:
                try:
                    _soc(config, source, values, states, now, candidate)
                    _soc(config, source, values, states, now, self._soc_recovery_last)
                except InputError:
                    candidate = None
                else:
                    if (observation[0] - candidate[0]).total_seconds() >= 60:
                        self._previous_soc = observation
                        self._soc_reference = None
                        self._soc_recovery = self._soc_recovery_last = None
                        self._soc_rebase_since = None
                        return
            if candidate is None:
                self._soc_recovery = observation
            self._soc_recovery_last = observation
            raise
        self._soc_recovery = self._soc_recovery_last = None
        self._soc_rebase_since = None

    def _soc_rebase_deferred(self, err):
        """Keep the retained plan while an upward SOC jump awaits confirmation.

        Near full charge the BMS may correct SOC upward by several percent in a
        minute. The retained plan then only underestimates stored energy, so
        reporting invalid_input (which makes a controller drop the plan) costs
        more than it protects. Downward jumps, and upward ones that are not
        confirmed within SOC_REBASE_GRACE_SECONDS, still fail.
        """
        if (
            str(err) != "SOC measurement jump"
            or self._soc_recovery is None
            or self._previous_soc is None
            or self._soc_recovery[1] <= self._previous_soc[1]
        ):
            return False
        now = dt_util.utcnow()
        if self._soc_rebase_since is None:
            self._soc_rebase_since = now
        if (now - self._soc_rebase_since).total_seconds() > SOC_REBASE_GRACE_SECONDS:
            return False
        self._invalidate("calculating", "soc_rebase_pending")
        waited = (now - self._soc_recovery[0]).total_seconds()
        self._schedule(max(1, 61 - waited))
        return True

    def _replan_wait(self, values):
        """Seconds before an input-only change may start another calculation.

        Measured from the previous start, so the setting is the shortest
        interval between calculations rather than a pause that only applies
        while the optimizer happens to sit in 'ready'. Gating on 'ready' left
        the busiest states ungated: a solve that ended in timeout or error, or
        one still running behind a retained plan, accepted every input change
        immediately. The gate needs a usable plan to fall back on, so it lifts
        as soon as no valid plan is retained.
        """
        if not self.data.get("valid") or self._calculation_started_at is None:
            return 0
        elapsed = (dt_util.utcnow() - self._calculation_started_at).total_seconds()
        return max(0, values.get("minimum_replan_seconds", 0) - elapsed)

    def _count_calculation(self, now):
        """Record one calculation start for the rate limit and diagnostics."""
        self._calculation_started_at = now
        self._calculations += 1
        self._calculation_starts.append(now)
        horizon = now - timedelta(hours=24)
        while self._calculation_starts and self._calculation_starts[0] < horizon:
            self._calculation_starts.popleft()

    def calculation_counts(self):
        """Calculation starts observed in the last hour and the last 24 hours."""
        now = dt_util.utcnow()
        hour = now - timedelta(hours=1)
        return {
            "last_calculation_started_at": self._calculation_started_at.isoformat()
            if self._calculation_started_at
            else None,
            "calculations_last_hour": sum(
                1 for start in self._calculation_starts if start >= hour
            ),
            "calculations_last_24h": len(self._calculation_starts),
            "calculations_since_load": self._calculations,
        }

    @callback
    def _source_changed(self, event):
        if self._closed:
            return
        try:
            _, states, _, values = self._inputs()
            content = self._content(states, values)
        except (InputError, KeyError, ValueError) as err:
            self._generation += 1
            self._fingerprint = None
            if isinstance(err, InputError) and self._soc_rebase_deferred(err):
                return
            self._epoch += 1
            self._invalidate("invalid_input", str(err))
            self._schedule(0)
            return
        if content == self._fingerprint and self.data.get("status") != "invalid_input":
            if self.data.get("valid"):
                self._publish_current(self.data, states, values, dt_util.utcnow())
                self._next_boundary(values)
            return
        self._fingerprint = content
        self._generation += 1
        self._pending = True
        # Every solve uses its full time budget, so restarting on each input
        # change kept the optimizer permanently calculating and 'ready' lasted
        # milliseconds. The retained plan stays published until the limit ends.
        wait = self._replan_wait(values)
        if wait > 0:
            self._schedule(max(wait, values["debounce_seconds"]))
            return
        self._invalidate("calculating", "inputs_changed")
        self._schedule(values["debounce_seconds"])

    def _schedule(self, delay):
        """Run a recalculation after delay; an earlier request replaces a later one."""
        if self._closed:
            return
        if self._debounce is not None:
            if self._debounce.when() <= self.hass.loop.time() + delay:
                return
            self._debounce.cancel()
            self._debounce = None

        @callback
        def run():
            self._debounce = None
            if not self._closed:
                self.hass.async_create_task(self.async_recalculate())

        if delay == 0:
            run()
        else:
            self._debounce = self.hass.loop.call_later(delay, run)

    def _next_boundary(self, values):
        if self._boundary:
            self._boundary.cancel()
        now = dt_util.utcnow()
        delay = (
            max(0.1, (self._refresh_due - now).total_seconds())
            if self._refresh_due and self._runner is None
            else 300
        )
        if self.data.get("valid") and self.data.get("valid_until"):
            delay = min(
                delay,
                max(
                    0.1,
                    (parse_timestamp(self.data["valid_until"]) - now).total_seconds(),
                ),
            )
        if self._inputs_valid_until is not None:
            delay = min(
                delay, max(0.1, (self._inputs_valid_until - now).total_seconds())
            )
        for key in ("intervals", "outlook"):
            for row in self.data.get(key, []):
                end = parse_timestamp(row["end"])
                if end > now:
                    delay = min(delay, (end - now).total_seconds())
                    break
        source = SourceConfig.from_dict(self.configuration["sources"])
        freshness = (
            [values.get("soc_max_age_seconds", 600)] if source.battery_enabled else []
        )
        freshness.extend(
            item.get("max_age_seconds")
            for item in self.configuration.get("helpers", {}).values()
            if item.get("max_age_seconds")
        )
        if freshness:
            delay = min(delay, max(1, min(freshness) / 2))

        @callback
        def refresh():
            self._boundary = None
            now = dt_util.utcnow()
            if self._refresh_due and now >= self._refresh_due and self._runner is None:
                self._generation += 1
                self._invalidate("calculating", "interval_boundary")
                self._schedule(0)
                return
            try:
                _, states, now, current_values = self._inputs()
                if (
                    self.data.get("alert")
                    or self.data.get("reason") == "soc_rebase_pending"
                ) and self._runner is None:
                    # Identical SOC reports refresh last_reported without emitting
                    # state_changed. A health tick must recover those inputs too.
                    self._generation += 1
                    self._fingerprint = None
                    self._invalidate("calculating", "inputs_recovered")
                    self._schedule(0)
                    return
                if self.data.get("valid"):
                    self._publish_current(self.data, states, current_values, now)
            except (InputError, KeyError, ValueError) as err:
                self._generation += 1
                self._fingerprint = None
                if isinstance(err, InputError) and self._soc_rebase_deferred(err):
                    return
                self._epoch += 1
                self._invalidate("invalid_input", str(err))
            self._next_boundary(values)

        self._boundary = self.hass.loop.call_later(delay, refresh)

    def _publish_current(self, result, states, values, now):
        """Advance an existing plan and its clocks without invoking the solver."""
        rows = result.get("intervals", [])
        current = next(
            (
                i
                for i, row in enumerate(rows)
                if parse_timestamp(row["start"]) <= now < parse_timestamp(row["end"])
            ),
            None,
        )
        if current is None:
            self._invalidate("invalid_input", "no_current_interval")
            return
        retained = result.get("plan_retained", False)
        deadline = parse_timestamp(rows[-1]["end"])
        if not retained:
            deadline = min(
                parse_timestamp(result["valid_until"]),
                deadline,
            )
        if deadline <= now:
            self._invalidate("invalid_input", "expired_inputs")
            return
        mode_start = current
        while mode_start and rows[mode_start - 1].get("dispatch_mode") == rows[
            current
        ].get("dispatch_mode"):
            mode_start -= 1
        policy = dict(result.get("dispatch_policy", {}))
        exception = policy.get("safety_exception")
        if exception and now >= parse_timestamp(exception["deadline"]):
            policy["safety_exception"] = None
        self._commit_battery_direction(
            {
                **result,
                "dispatch_policy": policy,
                "intervals": rows[mode_start:],
                "generated_at": rows[mode_start]["start"],
            }
        )
        export_start = current
        if min(rows[current]["discharge_kwh"], rows[current]["grid_export_kwh"]) > 1e-6:
            while (
                export_start
                and min(
                    rows[export_start - 1]["discharge_kwh"],
                    rows[export_start - 1]["grid_export_kwh"],
                )
                > 1e-6
            ):
                export_start -= 1
        self._commit_current_export(
            {
                **result,
                "intervals": rows[export_start:],
                "generated_at": rows[export_start]["start"],
            }
        )
        grid_charge_start = current
        if rows[current].get("dispatch_mode") == "CHARGE_GRID":
            while (
                grid_charge_start
                and rows[grid_charge_start - 1].get("dispatch_mode") == "CHARGE_GRID"
            ):
                grid_charge_start -= 1
        self._commit_current_grid_charge(
            {
                **result,
                "intervals": rows[grid_charge_start:],
                "generated_at": rows[grid_charge_start]["start"],
            }
        )
        outlook = [
            row
            for row in result.get("outlook", [])
            if parse_timestamp(row["end"]) > now
        ]
        guidance = bool(
            outlook
            and parse_timestamp(outlook[0]["start"]) <= now
            and outlook[0]["level"] is not None
        )
        quality = dict(result.get("quality", {}))
        quality["warnings"] = [
            warning
            for warning in quality.get("warnings", [])
            if warning != "current_guidance_unavailable"
        ] + ([] if guidance else ["current_guidance_unavailable"])
        current_result = {
            **result,
            "valid": True,
            "plan_retained": retained,
            "alert": result.get("alert"),
            "intervals": rows[current:],
            "outlook": outlook,
            "guidance_valid": guidance,
            "quality": quality,
            "valid_until": deadline.isoformat(),
            "inputs_valid_until": (
                self._inputs_valid_until.isoformat()
                if self._inputs_valid_until
                else None
            )
            if states is not None
            else result.get("inputs_valid_until"),
            "dispatch_policy": policy,
            "measurements": result.get("measurements", {})
            if retained
            else measurement_diagnostics(self.configuration, states, now),
        }
        if not retained:
            self.last_successful_plan_at = result["generated_at"]
        self._anchor_windows({**current_result, "generated_at": now.isoformat()})
        self.async_set_updated_data(current_result)

    def _invalidate(self, status, reason):
        """Report a refresh problem and keep advancing the last covered plan."""
        now = dt_util.utcnow()
        alert = self.data.get("alert")
        if status != "calculating" and (
            not alert or (alert["status"], alert["reason"]) != (status, reason)
        ):
            alert = {
                "code": "soc_measurement_jump"
                if reason == "SOC measurement jump"
                else status,
                "status": status,
                "reason": reason,
                "since": now.isoformat(),
            }
        covered = any(
            parse_timestamp(row["start"]) <= now < parse_timestamp(row["end"])
            for row in self.data.get("intervals", [])
        )
        if covered:
            self._publish_current(
                {
                    **self.data,
                    "status": status,
                    "reason": reason,
                    "alert": alert,
                    "plan_retained": True,
                    "refreshing": status == "calculating",
                    "expired_previous_generated_at": None,
                },
                None,
                None,
                now,
            )
            self._next_boundary(self.configuration["settings"])
            return
        if self.data.get("valid"):
            self.previous_plan = {**deepcopy(self.data), "expired": True}
        self.async_set_updated_data(
            {
                "status": status,
                "valid": False,
                "refreshing": False,
                "plan_retained": False,
                "alert": alert,
                "reason": reason,
                "expired_previous_generated_at": self.previous_plan.get("generated_at")
                if self.previous_plan
                else None,
            }
        )

    def _anchor_windows(self, result):
        now = parse_timestamp(result["generated_at"])
        updated = {}
        for key, windows in result["windows"].items():
            active = next(
                (
                    window
                    for window in windows
                    if parse_timestamp(window["start"])
                    <= now
                    < parse_timestamp(window["end"])
                ),
                None,
            )
            if active:
                previous = self._anchors.get(key)
                start = (
                    previous["start"]
                    if previous and parse_timestamp(previous["end"]) >= now
                    else active["start"]
                )
                active["start"] = start
                active["start_basis"] = "first_observed"
                updated[key] = {"start": start, "end": active["end"]}
        self._anchors = updated
        self._store.async_delay_save(lambda: self._anchors, 1)

    def _clear_strategy_change(self) -> None:
        """Consume the one-shot release exactly once, after it produced a plan."""
        config = merged_configuration(self.entry)
        if not config.get("strategy_changed_at"):
            return
        config["strategy_changed_at"] = None
        self.configuration["strategy_changed_at"] = None
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, "configuration": config}
        )

    async def async_recalculate(self) -> None:
        """Snapshot on the event loop, calculate off-loop, and publish one generation."""
        if self._closed:
            return
        if self._runner is not None:
            self._pending = True
            return
        if self._boundary:
            self._boundary.cancel()
            self._boundary = None
        self._runner = asyncio.current_task()
        cancelled = False
        try:
            while not self._closed:
                self._pending = False
                generation = self._generation
                epoch = self._epoch
                values = self.configuration["settings"]
                now = dt_util.utcnow()
                self._count_calculation(now)
                seconds = values.get("refresh_minutes", 15) * 60
                self._refresh_due = now + timedelta(
                    seconds=seconds - now.timestamp() % seconds
                )
                try:
                    config, states, now, values = self._inputs()
                    seconds = values["refresh_minutes"] * 60
                    self._refresh_due = now + timedelta(
                        seconds=seconds - now.timestamp() % seconds
                    )
                    self._fingerprint = self._content(states, values)
                    self._invalidate("calculating", "calculating")
                    history, _ = await async_history(self.hass, config, states, now)
                    if self._closed:
                        return
                    self._worker = self.hass.async_add_executor_job(
                        partial(
                            compute,
                            deepcopy(config),
                            states,
                            now,
                            atlas_solve_builder=(
                                self.atlas.solve_builder
                                if self.atlas and self.atlas.sink
                                else None
                            ),
                            previous_soc=self._previous_soc,
                            battery_commitment=deepcopy(self._battery_commitment),
                            export_commitment=deepcopy(self._export_commitment),
                            grid_charge_commitment=deepcopy(
                                self._grid_charge_commitment
                            ),
                            balance_state=deepcopy(self._balance),
                            **history,
                        )
                    )
                    try:
                        result = await asyncio.wait_for(
                            asyncio.shield(self._worker),
                            timeout=values["total_time_limit_s"] + 1,
                        )
                    except TimeoutError:
                        self._invalidate("timeout", "worker_deadline")
                        # Executor threads cannot be cancelled; retain ownership until completion.
                        await asyncio.shield(self._worker)
                        raise SolveError("timeout") from None
                    finally:
                        self._worker = None
                    if self._closed:
                        return
                    # `atlas_solve_payload` is beside the result, never a published
                    # entity attribute (ADR-0019 §6): pop it before the result can
                    # reach `_publish_current`/`self.data`, whether or not this
                    # generation ends up published.
                    atlas_payload_present = "atlas_solve_payload" in result
                    atlas_payload = (
                        result.pop("atlas_solve_payload")
                        if atlas_payload_present
                        else None
                    )
                    # A result superseded only by newer inputs is still fresher
                    # than the retained plan. Publishing it before recalculating
                    # guarantees progress: at 8 kW the SOC crosses the trigger
                    # every few minutes, faster than one calculation, and
                    # discarding every superseded result let the retained plan
                    # expire. Configuration, registry and invalid-input changes
                    # bump the epoch and still discard.
                    current = generation == self._generation
                    if current or epoch == self._epoch:
                        # Only the actual publish path may feed the sink or count
                        # a skip: a solve discarded here (superseded epoch) was
                        # never published, so Atlas must not receive it either.
                        if self.atlas is not None and atlas_payload_present:
                            self.atlas.add_solve(atlas_payload)
                        self._previous_soc = result["quality"].pop(
                            "soc_observation", None
                        )
                        _, current_states, published_at, current_values = self._inputs()
                        self._publish_current(
                            result, current_states, current_values, published_at
                        )
                        self._published_at = published_at
                        if (
                            current
                            and result.get("strategy_released")
                            and self.data.get("valid")
                        ):
                            self._clear_strategy_change()
                except InputError as err:
                    if not self._closed and generation == self._generation:
                        if self._soc_rebase_deferred(err):
                            break
                        if self.atlas is not None:
                            self.atlas.skip_solve()
                        self._invalidate("invalid_input", str(err))
                except SolveError as err:
                    if not self._closed and generation == self._generation:
                        if self.atlas is not None:
                            self.atlas.skip_solve()
                        # The reason alone ("solver_failure") hides which
                        # validation failed; keep the detail for diagnosis.
                        _LOGGER.warning("Advisory calculation failed: %s", err)
                        self._invalidate(
                            err.reason
                            if err.reason in ("timeout", "infeasible")
                            else "error",
                            err.reason,
                        )
                except Exception as err:
                    if not self._closed and generation == self._generation:
                        _LOGGER.exception("Advisory calculation failed")
                        self._invalidate("error", type(err).__name__)
                if not self._pending and generation == self._generation:
                    break
                wait = self._replan_wait(values) if epoch == self._epoch else 0
                if wait > 0:
                    self._schedule(wait)
                    break
        except asyncio.CancelledError:
            cancelled = True
            if self._boundary:
                self._boundary.cancel()
                self._boundary = None
            raise
        finally:
            self._runner = None
            if not self._closed and not cancelled:
                self._next_boundary(self.configuration["settings"])

    def _commit_battery_direction(self, result):
        rows = result.get("intervals", [])
        policy = result.get("dispatch_policy", {})
        mode = rows[0].get("dispatch_mode") if rows else None
        if mode is None or not policy.get("enabled"):
            self._battery_commitment = None
        elif policy.get("safety_exception"):
            return
        elif self._battery_commitment and dwell_group(
            self._battery_commitment["mode"]
        ) == dwell_group(mode):
            # CHARGE_PV <-> SELF_CONSUME keeps the inverter state and its clock.
            self._battery_commitment = {**self._battery_commitment, "mode": mode}
        elif not self._battery_commitment or self._battery_commitment["mode"] != mode:
            previous = self._battery_commitment
            self._battery_commitment = {"mode": mode, "since": result["generated_at"]}
            if previous and previous["mode"] in ("charge", "discharge"):
                self._battery_commitment["legacy_direction"] = previous
            elif previous and previous.get("legacy_direction"):
                self._battery_commitment["legacy_direction"] = previous[
                    "legacy_direction"
                ]
        self._dispatch_store.async_delay_save(lambda: self._battery_commitment, 1)

    def _commit_current_export(self, result):
        now = parse_timestamp(result["generated_at"])
        until = now
        for row in result.get("intervals", []):
            if (
                parse_timestamp(row["start"]) != until
                or min(row.get("discharge_kwh", 0), row.get("grid_export_kwh", 0))
                <= 1e-6
            ):
                break
            until = parse_timestamp(row["end"])
        self._export_commitment = restore_export_commitment(
            {"generated_at": now.isoformat(), "until": until.isoformat()}, now
        )
        self._export_store.async_delay_save(lambda: self._export_commitment, 1)

    def _commit_current_grid_charge(self, result):
        now = parse_timestamp(result["generated_at"])
        until = now
        for row in result.get("intervals", []):
            if (
                parse_timestamp(row["start"]) != until
                or row.get("dispatch_mode") != "CHARGE_GRID"
            ):
                break
            until = parse_timestamp(row["end"])
        self._grid_charge_commitment = restore_grid_charge_commitment(
            {"generated_at": now.isoformat(), "until": until.isoformat()}, now
        )
        self._grid_charge_store.async_delay_save(
            lambda: self._grid_charge_commitment, 1
        )

    async def async_stop(self):
        """Detach listeners and prevent late worker results from owning entities."""
        self._closed = True
        self._generation += 1
        for handle in (self._debounce, self._boundary):
            if handle:
                handle.cancel()
        self._debounce = self._boundary = None
        for unsubscribe in (self._source_unsub, self._registry_unsub):
            if unsubscribe:
                unsubscribe()
        self._source_unsub = self._registry_unsub = None
        if self._worker is not None:
            try:
                await asyncio.shield(self._worker)
            except Exception:
                _LOGGER.debug(
                    "Discarded failed advisory worker during unload", exc_info=True
                )
        await self._store.async_save(self._anchors)
        await self._dispatch_store.async_save(self._battery_commitment)
        await self._export_store.async_save(self._export_commitment)
        await self._grid_charge_store.async_save(self._grid_charge_commitment)
        if self._balance is not None and self._balance_persist:
            await self._balance_store.async_save(self._balance)
