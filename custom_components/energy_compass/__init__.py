"""Read-only, configurable household energy advice."""

from copy import deepcopy

from homeassistant.const import Platform

from .coordinator import EnergyCompassCoordinator
from .settings import default_configuration, explicit_strategy_fields

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT]


async def async_setup(hass, config) -> bool:
    """Register the domain-wide Atlas backfill service (ADR-0019 §8).

    HA rule: a service is registered once in `async_setup`, not per entry. Registered
    unconditionally so it exists on the off path too (ADR-0019 §3: "registered but
    inert" — the handler itself raises `ServiceValidationError` when no entry has
    Atlas enabled and registered). This does import the `atlas` package (and
    transitively `atlas_sink`, for its type definitions) even with Atlas disabled
    everywhere, same as opening the `energy_atlas` options step already does; it
    starts no thread, opens no file and makes no network call, which is what the
    off-path test actually asserts (ADR-0019 §3's "not imported" text is scoped to
    `async_setup_entry`, entry-per-entry overhead, not this domain-wide call).
    """
    from .atlas.backfill_service import async_register as async_register_backfill

    async_register_backfill(hass)
    return True


async def async_setup_entry(hass, entry) -> bool:
    """Own all scheduling and source subscriptions through the config entry."""
    coordinator = EnergyCompassCoordinator(hass, entry)
    entry.runtime_data = coordinator
    try:
        atlas_settings = entry.options.get("atlas", {})
        if atlas_settings.get("enabled"):
            # ADR-0019 §3: only imported/started when Atlas is enabled. Started before
            # the coordinator so the first solve (dispatched by async_start) carries
            # the payload builder.
            from .atlas import AtlasBridge, build_attrs

            coordinator.atlas = AtlasBridge(
                hass, entry, coordinator.configuration, atlas_settings
            )
            await coordinator.atlas.async_start(
                build_attrs(coordinator.configuration, atlas_settings)
            )
        await coordinator.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        if coordinator.atlas is not None:
            await coordinator.atlas.async_stop()
        await coordinator.async_stop()
        raise
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload entities before releasing the entry's runtime ownership."""
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = entry.runtime_data
        if coordinator.atlas is not None:
            await coordinator.atlas.async_stop()
        await coordinator.async_stop()
        return True
    return False


async def async_remove_entry(hass, entry) -> None:
    """Delete every Atlas site key for this entry (ADR-0019 §4); orphaned sites stay in Atlas."""
    from .atlas import forget_entry

    await hass.async_add_executor_job(forget_entry, hass, entry.entry_id)


async def async_migrate_entry(hass, entry) -> bool:
    """Fill absent configuration fields while retaining every existing choice."""
    if entry.version > 3:
        return False
    if entry.version < 3:
        current = deepcopy(dict(entry.data))
        defaults = default_configuration(
            current.get("currency", "EUR"),
            current.get("timezone", hass.config.time_zone),
        )

        def fill(target, fallback):
            for key, value in fallback.items():
                if key not in target:
                    target[key] = deepcopy(value)
                elif isinstance(value, dict) and isinstance(target[key], dict):
                    fill(target[key], value)

        fill(current, defaults)
        options = deepcopy(dict(entry.options))
        if "configuration" in options:
            fill(options["configuration"], defaults)
        for target in (
            current,
            *([options["configuration"]] if "configuration" in options else []),
        ):
            target["explicit_strategy_fields"] = explicit_strategy_fields(
                target["settings"]
            )
        hass.config_entries.async_update_entry(
            entry, data=current, options=options, version=3
        )
    return True
