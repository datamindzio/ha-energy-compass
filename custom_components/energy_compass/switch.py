"""Switch of the native Deye controller."""

from homeassistant.components.switch import SwitchEntity

from .controller_entity import DeyeControllerEntity


async def async_setup_entry(hass, entry, async_add_entities):
    controller = entry.runtime_data.controller
    if controller is not None and controller.engine == "native":
        async_add_entities([DeyePauseSwitch(controller)])


class DeyePauseSwitch(DeyeControllerEntity, SwitchEntity):
    """Stops every inverter write while on; the decision is still computed."""

    def __init__(self, controller):
        super().__init__(controller, "deye_pause")

    @property
    def is_on(self):
        return self.controller.state.paused

    async def async_turn_on(self, **kwargs):
        self.controller.async_set_paused(True)

    async def async_turn_off(self, **kwargs):
        self.controller.async_set_paused(False)
