"""Current input and recommendation validity, separate from future coverage."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.util import dt as dt_util

from . import controller_clock as clock
from .controller_entity import DeyeGridOutageSensor
from .entity import EnergyCompassEntity
from .sources.bindings import parse_timestamp


async def async_setup_entry(hass, entry, async_add_entities):
    """Publish the validity gate used by notification and dashboard consumers."""
    entities = [
        EnergyCompassValidity(entry.runtime_data, "forecast_valid"),
        EnergyCompassAlert(entry.runtime_data, "alert"),
    ]
    controller = entry.runtime_data.controller
    if controller is not None:
        entities.append(DeyeGridOutageSensor(controller))
    async_add_entities(entities)


class EnergyCompassAlert(EnergyCompassEntity, BinarySensorEntity):
    """Expose calculation and input failures independently of retained advice.

    A skewed inverter clock (controller enabled, T-123) also turns it on: the plan is
    fine but the inverter would run its windows at the wrong time.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def _clock(self):
        controller = self.coordinator.controller
        return controller.clock if controller is not None else None

    @property
    def _clock_alert(self):
        watch = self._clock
        return watch if watch is not None and watch.active else None

    @property
    def is_on(self):
        return bool(self.coordinator.data.get("alert")) or self._clock_alert is not None

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        alert = data.get("alert") or {}
        clock_alert = self._clock_alert
        if not alert and clock_alert is not None:
            alert = {
                "code": clock.CODE,
                "reason": f"{clock_alert.skew_minutes:+d} min",
                "since": clock_alert.over_since,
            }
        return {
            "code": alert.get("code"),
            "reason": alert.get("reason"),
            "since": alert.get("since"),
            "plan_retained": data.get("plan_retained", False),
            "last_successful_plan_at": self.coordinator.last_successful_plan_at,
            "clock_skew_minutes": clock_alert.skew_minutes if clock_alert else None,
        }

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        controller = self.coordinator.controller
        if controller is not None:
            self.async_on_remove(
                controller.async_add_listener(self._handle_coordinator_update)
            )


class EnergyCompassValidity(EnergyCompassEntity, BinarySensorEntity):
    """Mark advice available only within its current plan coverage."""

    @property
    def is_on(self):
        data = self.coordinator.data
        return bool(
            data.get("valid")
            and dt_util.utcnow() < parse_timestamp(data["valid_until"])
        )
