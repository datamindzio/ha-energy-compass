"""Entities of the integration-owned Deye controller."""

from typing import ClassVar

from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

from .controller import MODES
from .entity import entry_device_info

DETAIL_DELAY_SECONDS = 60
CONTROLLER_UNRECORDED = frozenset({"accepted", "tou"})
RUNTIME_UNRECORDED = frozenset({"runtime", "updated_at"})


class DeyeControllerEntity:
    """Shared identity: one entity per entry on the Energy Compass device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller, key):
        self.controller = controller
        self._attr_unique_id = f"{controller.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = entry_device_info(controller.entry)

    @callback
    def _changed(self):
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        self.async_on_remove(self.controller.async_add_listener(self._changed))


class DeyeControllerSensor(DeyeControllerEntity, SensorEntity):
    """State is the next instant the blueprint must run; attributes are the contract."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _unrecorded_attributes = CONTROLLER_UNRECORDED

    def __init__(self, controller):
        super().__init__(controller, "deye_controller")

    @property
    def native_value(self):
        return self.controller.view()[0]

    @property
    def extra_state_attributes(self):
        return self.controller.view()[1]


class DeyeModeSelect(DeyeControllerEntity, SelectEntity):
    """The user-facing mode; the store is the single source of truth."""

    _attr_options: ClassVar[list[str]] = list(MODES)

    def __init__(self, controller):
        super().__init__(controller, "deye_mode")

    @property
    def current_option(self):
        return self.controller.state.mode

    async def async_select_option(self, option):
        self.controller.async_set_mode(option)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # The controller sensor publishes this entity's id as `mode_entity`.
        self.controller.async_refresh_view()


class DeyeRuntimeSensor(DeyeControllerEntity, SensorEntity):
    """The last decision summary. Detail-only changes are published at most once a minute."""

    _unrecorded_attributes = RUNTIME_UNRECORDED

    def __init__(self, controller):
        super().__init__(controller, "deye_runtime")
        self._written = None
        self._timer = None

    def _recorded(self):
        state, attrs = self.controller.runtime_view()
        return state, {k: v for k, v in attrs.items() if k not in RUNTIME_UNRECORDED}

    @property
    def native_value(self):
        return self.controller.runtime_view()[0]

    @property
    def extra_state_attributes(self):
        return self.controller.runtime_view()[1]

    @callback
    def _async_write_ha_state(self):
        super()._async_write_ha_state()
        self._written = self._recorded()

    async def async_added_to_hass(self):
        self.async_on_remove(
            self.controller.async_add_listener(self._runtime_changed, runtime=True)
        )
        self.async_on_remove(self._cancel_timer)

    @callback
    def _runtime_changed(self):
        if self._recorded() != self._written:
            self._flush()
        elif self._timer is None:
            self._timer = async_call_later(
                self.hass, DETAIL_DELAY_SECONDS, self._flush_later
            )

    @callback
    def _flush_later(self, _now):
        self._timer = None
        self._flush()

    @callback
    def _flush(self):
        self._cancel_timer()
        self.async_write_ha_state()

    @callback
    def _cancel_timer(self):
        if self._timer is not None:
            self._timer()
            self._timer = None
