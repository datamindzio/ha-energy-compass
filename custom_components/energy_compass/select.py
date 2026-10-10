"""The dispatch strategy control, separate from the plan's own label."""

from typing import ClassVar

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

from .controller_entity import DeyeModeSelect
from .entity import EnergyCompassEntity
from .settings import STRATEGIES, merged_configuration


async def async_setup_entry(hass, entry, async_add_entities):
    """Publish the strategy select entity."""
    entities = [EnergyCompassStrategySelect(entry.runtime_data, "strategy")]
    if (controller := entry.runtime_data.controller) is not None:
        entities.append(DeyeModeSelect(controller))
    async_add_entities(entities)


class EnergyCompassStrategySelect(EnergyCompassEntity, SelectEntity):
    """Expose and change the stored strategy, independent of any plan in flight."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options: ClassVar[list[str]] = list(STRATEGIES)

    @property
    def available(self) -> bool:
        return True

    @property
    def current_option(self) -> str:
        return self.coordinator.configuration["settings"]["strategy"]

    async def async_select_option(self, option: str) -> None:
        if option not in STRATEGIES:
            raise ServiceValidationError(f"invalid strategy: {option}")
        config = merged_configuration(self.coordinator.entry)
        if config["settings"].get("strategy") == option:
            return
        config["settings"]["strategy"] = option
        config["strategy_changed_at"] = dt_util.utcnow().isoformat()
        await self.coordinator.async_apply_configuration(config)
        self.async_write_ha_state()
