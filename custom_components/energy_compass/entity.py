"""Stable entity identity and shared advisory attributes."""

from copy import deepcopy

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .settings import DOMAIN
from .sources.bindings import parse_timestamp

BALANCE_ATTRIBUTES = (
    "last_completed",
    "next_due",
    "days_overdue",
    "planned_start",
    "planned_end",
    "planned_mode",
    "hold_progress_minutes",
    "hold_required_minutes",
    "threshold_percent",
)

PLAN_ATTRIBUTES = frozenset(
    {
        "intervals",
        "outlook",
        "favorable_windows",
        "windows",
        "input_ages",
        "measurements",
        "load_quality",
        "flexible_load_profiles",
    }
)


class EnergyCompassEntity(CoordinatorEntity):
    """Expose a single coherent generation without recording forecast arrays."""

    _attr_has_entity_name = True
    _unrecorded_attributes = PLAN_ATTRIBUTES

    def __init__(self, coordinator, key):
        super().__init__(coordinator)
        self._last_published_data = None
        self.key = key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=coordinator.entry.title,
            manufacturer="Energy Compass",
            model="Advisory",
        )
        if key in ("optimizer_status", "alert", "battery_balance"):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def _publication_data(self):
        """Include freshness and attributes even when the primary value is stable."""
        available = self.available
        return (
            available,
            self.state if available else None,
            self.extra_state_attributes,
        )

    @callback
    def _async_write_ha_state(self):
        """Remember successful writes, including initial and HA-triggered writes."""
        super()._async_write_ha_state()
        self._last_published_data = deepcopy(self._publication_data())

    @callback
    def _handle_coordinator_update(self):
        """Publish only changes visible on this entity, not every plan update."""
        if self._publication_data() != self._last_published_data:
            super()._handle_coordinator_update()

    @property
    def available(self):
        if self.key == "battery_balance":
            return self.coordinator.balance_report() is not None
        if self.key in ("forecast_valid", "optimizer_status", "alert"):
            return True
        data = self.coordinator.data
        fresh = bool(
            data.get("valid")
            and data.get("valid_until")
            and dt_util.utcnow() < parse_timestamp(data["valid_until"])
        )
        if not fresh and not data.get("refreshing"):
            return False
        if self.key in ("consumption_compass", "consumption_cost"):
            return data.get("guidance_valid", False)
        if self.key == "flexible_energy_depth":
            return data.get("flexible_energy_depth") is not None
        if self.key == "next_change":
            return next_change(data) is not None
        if self.key.startswith("next_"):
            return self._window() is not None
        return True

    def _window(self, kind=None):
        data = self.coordinator.data
        kind = kind or self.key.removeprefix("next_").removesuffix("_start")
        now = dt_util.utcnow()
        return next(
            (
                row
                for row in data.get("windows", {}).get(kind, [])
                if parse_timestamp(row["end"]) > now
            ),
            None,
        )

    @property
    def extra_state_attributes(self):
        if self.key == "battery_balance":
            report = self.coordinator.balance_report() or {}
            return {key: report.get(key) for key in BALANCE_ATTRIBUTES}
        data = self.coordinator.data
        quality = data.get("quality", {})
        attrs = {
            "generated_at": data.get("generated_at"),
            "valid_until": data.get("valid_until"),
            "inputs_valid_until": data.get("inputs_valid_until"),
            "refreshing": data.get("refreshing", False),
            "plan_retained": data.get("plan_retained", False),
            "reasons": quality.get("warnings", [])
            + (
                [data["coverage_reason"]]
                if data.get("coverage_reason") != "complete"
                and data.get("coverage_reason")
                else []
            ),
            "reason": data.get("reason", data.get("coverage_reason")),
            "calibration": data.get("calibration"),
            "capacity_calibration": data.get("capacity_calibration"),
            "classification_mode": data.get("classification_mode"),
        }
        if self.key == "plan":
            attrs.update(
                {
                    key: data.get(key, [])
                    for key in (
                        "intervals",
                        "outlook",
                        "favorable_windows",
                        "windows",
                        "measurements",
                        "presentation",
                        "notification_preferences",
                        "dispatch_policy",
                        "flexible_load_profiles",
                    )
                }
            )
            window_status = {}
            for kind in data.get("windows", {}):
                window = self._window(kind)
                window_status[kind] = (
                    "none_in_coverage"
                    if window is None
                    else "active"
                    if parse_timestamp(window["start"]) <= dt_util.utcnow()
                    else "upcoming"
                )
            attrs["window_status"] = window_status
            attrs["attribute_schema_version"] = 3
            attrs["monthly_charge_reporting_only"] = data.get(
                "monthly_charge_reporting_only"
            )
            attrs["load_quality"] = quality.get("load")
            attrs["strategy"] = data.get("strategy")
            attrs["autonomy_shortfall_kwh"] = data.get("autonomy_shortfall_kwh")
            attrs["peak_reserve_shortfall_kwh"] = data.get("peak_reserve_shortfall_kwh")
            attrs["time_limited"] = data.get("time_limited")
            attrs["mip_gap"] = data.get("mip_gap")
            attrs["cap_violation_kwh"] = data.get("cap_violation_kwh")
        elif self.key == "consumption_cost":
            attrs.update(method="finite_difference", probe_kwh=data.get("probe_kwh"))
        elif self.key == "flexible_energy_depth":
            profiles = data.get("flexible_load_profiles", [])
            attrs.update(
                reason=(
                    profiles[0].get("reason")
                    if profiles and profiles[0].get("reason")
                    else attrs["reason"]
                ),
                anchor_energy_kwh=3,
                anchor_price_per_kwh=data.get("flexible_anchor_price_per_kwh"),
                allowed_block_price_per_kwh=data.get(
                    "flexible_allowed_block_price_per_kwh"
                ),
                degradation_percent=data.get("flexible_price_degradation_percent"),
                max_power_kw=data.get("flexible_load_max_power_kw"),
                currency=data.get("currency"),
                price_unit=f"{data.get('currency')}/kWh",
                flexible_load_profiles=profiles,
            )
        elif self.key == "energy_compass" and data.get("intervals"):
            attrs.update(data["intervals"][0])
        elif self.key == "next_change":
            change = next_change(data)
            attrs["next_level"] = change.get("level") if change else None
        elif self.key.startswith("next_") and self.key.endswith("_start"):
            window = self._window()
            attrs.update(status="none_in_coverage", end=None, duration_minutes=None)
            if window:
                start, end = (
                    parse_timestamp(window["start"]),
                    parse_timestamp(window["end"]),
                )
                attrs.update(
                    status="active" if start <= dt_util.utcnow() < end else "upcoming",
                    end=window["end"],
                    duration_minutes=(end - start).total_seconds() / 60,
                    remaining_minutes=max(
                        0, (end - max(start, dt_util.utcnow())).total_seconds()
                    )
                    / 60,
                    start_basis=window.get("start_basis", "forecast"),
                )
        elif self.key == "forecast_valid":
            attrs.update(
                current_guidance_valid=data.get("guidance_valid", False),
                coverage_end=quality.get("coverage_end"),
                requested_end=quality.get("requested_end"),
                coverage_complete=quality.get("coverage_complete", False),
                input_ages=quality.get("input_ages", {}),
                missing_sources=[data["reason"]]
                if not (data.get("valid") or data.get("refreshing"))
                and data.get("reason")
                else quality.get("missing_sources", []),
                load_quality=quality.get("load"),
            )
        elif self.key in ("expected_net_cost", "expected_wear_cost"):
            attrs["coverage_hours"] = data.get("cost_coverage_hours")
        elif self.key == "optimizer_status":
            attrs["expired_previous_generated_at"] = data.get(
                "expired_previous_generated_at"
            )
            # Cadence is a property of the coordinator, not of one publication:
            # a rate-limited change never reaches data at all. Only the two
            # values that cannot go stale between updates are published here;
            # the rolling windows live in diagnostics, which is read on demand.
            counts = self.coordinator.calculation_counts()
            attrs["last_calculation_started_at"] = counts["last_calculation_started_at"]
            attrs["calculations_since_load"] = counts["calculations_since_load"]
        return attrs


def next_change(data):
    """Stop at unknown coverage rather than forecasting through missing advice."""
    rows = data.get("outlook", [])
    if not rows:
        return None
    level = rows[0]["level"]
    for row in rows[1:]:
        if row["level"] is None:
            return None
        if row["level"] != level:
            return row
    return None
