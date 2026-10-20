"""Advisory sensors with stable entry-scoped unique IDs."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .balance_tracker import SENSOR_STATES
from .controller_entity import DeyeControllerSensor, DeyeRuntimeSensor
from .entity import (
    EnergyCompassEntity,
    next_change,
    price_interval,
)
from .settings import DOMAIN
from .soc_estimator_config import estimator_active
from .soc_estimator_ha import RECORDED_ATTRIBUTES
from .sources.bindings import parse_timestamp

PRICE_NOW_KEYS = {"buy_price_now": "buy_per_kwh", "sell_price_now": "sell_per_kwh"}
COST_KEYS = ("consumption_cost", "expected_net_cost", "expected_wear_cost")
COST_GATED_KEYS = COST_KEYS + tuple(PRICE_NOW_KEYS)

SENSOR_KEYS = (
    "consumption_compass",
    "consumption_cost",
    "buy_price_now",
    "sell_price_now",
    "flexible_energy_depth",
    "next_change",
    "next_boost_start",
    "next_cheap_start",
    "next_limit_start",
    "energy_compass",
    "plan",
    "expected_net_cost",
    "expected_wear_cost",
    "optimizer_status",
    "battery_balance",
)


async def async_setup_entry(hass, entry, async_add_entities):
    """Publish the same contract for every independent installation."""
    registry = er.async_get(hass)
    settings = entry.runtime_data.configuration["settings"]
    for key in SENSOR_KEYS:
        enabled = (
            settings.get("lfp_balance", False)
            if key == "battery_balance"
            else settings.get("flexible_load_enabled", True)
            if key == "flexible_energy_depth"
            else settings["expose_costs"]
            if key in COST_GATED_KEYS
            else settings["expose_windows"]
            if key.startswith("next_")
            else True
        )
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{key}"
        )
        if entity_id and (registered := registry.async_get(entity_id)):
            if not enabled and registered.disabled_by is None:
                registry.async_update_entity(
                    entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
                )
            elif (
                enabled
                and registered.disabled_by == er.RegistryEntryDisabler.INTEGRATION
            ):
                registry.async_update_entity(entity_id, disabled_by=None)
    entities = [EnergyCompassSensor(entry.runtime_data, key) for key in SENSOR_KEYS]
    if estimator_active(entry.runtime_data.configuration):
        entities.append(SocEstimatorSensor(entry.runtime_data))
    if (controller := entry.runtime_data.controller) is not None:
        entities += [DeyeControllerSensor(controller), DeyeRuntimeSensor(controller)]
    async_add_entities(entities)


class EnergyCompassSensor(EnergyCompassEntity, SensorEntity):
    """Render one public advisory value from the coordinator's current generation."""

    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        if key.startswith("next_") or key == "plan":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        if key == "flexible_energy_depth":
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = "kWh"
            self._attr_entity_registry_enabled_default = coordinator.configuration[
                "settings"
            ].get("flexible_load_enabled", True)
        if key in PRICE_NOW_KEYS:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        if key in COST_GATED_KEYS:
            self._attr_native_unit_of_measurement = coordinator.configuration[
                "currency"
            ] + ("" if key in ("expected_net_cost", "expected_wear_cost") else "/kWh")
            self._attr_entity_registry_enabled_default = coordinator.configuration[
                "settings"
            ]["expose_costs"]
        if key.startswith("next_"):
            self._attr_entity_registry_enabled_default = coordinator.configuration[
                "settings"
            ]["expose_windows"]
        if key == "battery_balance":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(SENSOR_STATES)
            self._attr_entity_registry_enabled_default = coordinator.configuration[
                "settings"
            ].get("lfp_balance", False)

    @property
    def suggested_display_precision(self):
        if self.key in COST_GATED_KEYS:
            return self.coordinator.data.get("presentation", {}).get(
                "cost_precision",
                self.coordinator.configuration["settings"]["cost_precision"],
            )
        return None

    @callback
    def _handle_coordinator_update(self):
        self._update_suggested_precision()
        super()._handle_coordinator_update()

    @property
    def native_value(self):
        if self.key == "battery_balance":
            report = self.coordinator.balance_report()
            return report["state"] if report else None
        data = self.coordinator.data
        if self.key == "optimizer_status":
            return data["status"]
        if not (data.get("valid") or data.get("refreshing")):
            return None
        if self.key == "consumption_compass":
            return data["outlook"][0]["level"]
        if self.key == "consumption_cost":
            return data["outlook"][0]["cost_per_kwh"]
        if self.key in PRICE_NOW_KEYS:
            row = price_interval(data, dt_util.utcnow())
            return row[PRICE_NOW_KEYS[self.key]] if row else None
        if self.key == "energy_compass":
            return data["intervals"][0]["state"]
        if self.key == "plan":
            return parse_timestamp(data["generated_at"])
        if self.key == "next_change":
            row = next_change(data)
            return parse_timestamp(row["start"]) if row else None
        if self.key.startswith("next_"):
            row = self._window()
            return parse_timestamp(row["start"]) if row else None
        return data.get(self.key)


class SocEstimatorSensor(EnergyCompassEntity, SensorEntity):
    """The counter SOC estimate: diagnostic, whether it plans or only runs in shadow."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_suggested_display_precision = 1
    _unrecorded_attributes = (
        frozenset(
            {
                "energy_kwh",
                "anchor_at",
                "anchor_age_h",
                "throughput_since_anchor_kwh",
                "pending_kwh",
                "unanchored_top",
                "excess_kwh",
                "full_candidate",
                "measured_capacity_kwh",
                "eta_charge",
                "capacity_kwh",
                "mode",
                "bms_soc",
                "full_candidates",
                "anchors",
                "error",
            }
        )
        - RECORDED_ATTRIBUTES
    )

    def __init__(self, coordinator):
        super().__init__(coordinator, "soc_estimator")

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.soc_estimator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self):
        estimate = self.coordinator.soc_estimator.estimate
        return estimate is not None and estimate.available

    @property
    def native_value(self):
        estimate = self.coordinator.soc_estimator.estimate
        if estimate is None or estimate.soc_fraction is None:
            return None
        return round(estimate.soc_fraction * 100, 2)

    @property
    def extra_state_attributes(self):
        return self.coordinator.soc_estimator.attributes()

    @callback
    def _handle_coordinator_update(self):
        """Plan updates carry nothing for this entity; the estimator pushes its own."""
