"""Collect the Home Assistant facts that source detection ranks."""

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .config_models import SolarForecastBinding
from .detect import (
    PROVIDER_PLATFORMS,
    DetectionSnapshot,
    DeviceFacts,
    EntityFacts,
    SolarForecastFacts,
    normalize_energy_prefs,
)
from .engine.models import InputError
from .runtime import async_fetch_solar_forecast

_LOGGER = logging.getLogger(__name__)


async def _energy_preferences(hass: HomeAssistant):
    try:
        from homeassistant.components.energy.data import async_get_manager

        return (await async_get_manager(hass)).data
    except Exception:
        _LOGGER.debug("Energy preferences unavailable for detection", exc_info=True)
        return None


def _entity_facts(
    hass: HomeAssistant, entity_id: str, entry: er.RegistryEntry | None
) -> EntityFacts:
    state = hass.states.get(entity_id)
    return EntityFacts(
        entity_id=entity_id,
        registry_id=entry.id if entry else None,
        platform=entry.platform if entry else None,
        translation_key=entry.translation_key if entry else None,
        unique_id=entry.unique_id if entry else None,
        config_entry_id=entry.config_entry_id if entry else None,
        device_id=entry.device_id if entry else None,
        disabled=bool(entry and entry.disabled_by),
        device_class=(entry.device_class or entry.original_device_class)
        if entry
        else None,
        state=state.state if state else None,
        attributes=dict(state.attributes) if state else {},
        last_updated=state.last_updated.isoformat() if state else None,
        last_reported=state.last_reported.isoformat() if state else None,
    )


async def _forecast_facts(
    hass: HomeAssistant, entry_ids: tuple[str, ...]
) -> dict[str, SolarForecastFacts]:
    result = {}
    for entry_id in entry_ids:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            continue
        try:
            wh_hours = await async_fetch_solar_forecast(
                hass, SolarForecastBinding(entry_id, entry.domain)
            )
            facts = SolarForecastFacts(
                entry_id, entry.domain, entry.title, wh_hours, None
            )
        except InputError as err:
            facts = SolarForecastFacts(
                entry_id, entry.domain, entry.title, None, str(err)
            )
        result[entry_id] = facts
    return result


async def async_detection_snapshot(hass: HomeAssistant) -> DetectionSnapshot:
    """Read registry, state, device, Energy preference and forecast facts."""
    registry = er.async_get(hass)
    energy = normalize_energy_prefs(await _energy_preferences(hass))
    referenced: set[str] = set()
    if energy is not None:
        for field in (
            energy.solar_energy,
            energy.solar_power,
            energy.grid_import,
            energy.grid_export,
            energy.grid_power,
            energy.battery_power,
            energy.battery_soc,
        ):
            referenced.update(field)
    entities: dict[str, EntityFacts] = {}
    for entry in registry.entities.values():
        if entry.platform in PROVIDER_PLATFORMS or entry.entity_id in referenced:
            entities[entry.entity_id] = _entity_facts(hass, entry.entity_id, entry)
    for entity_id in referenced - set(entities):
        entities[entity_id] = _entity_facts(hass, entity_id, None)
    device_registry = dr.async_get(hass)
    devices = {}
    for facts in entities.values():
        device = facts.device_id and device_registry.async_get(facts.device_id)
        if device and device.id not in devices:
            devices[device.id] = DeviceFacts(
                device.id,
                device.manufacturer,
                device.model,
                frozenset(device.config_entries),
            )
    forecasts = await _forecast_facts(
        hass, energy.solar_forecast_entries if energy else ()
    )
    return DetectionSnapshot(entities, devices, energy, forecasts)
