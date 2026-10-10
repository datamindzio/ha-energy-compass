"""Services of the integration-owned Deye controller."""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_register_admin_service

from .controller import PackageError, RuntimeInvalid
from .settings import DOMAIN

SERVICE_RUNTIME = "controller_runtime"
SERVICE_IMPORT = "controller_import_package"

RUNTIME_SCHEMA = vol.Schema(
    {
        vol.Required("controller"): cv.entity_id,
        vol.Optional("runtime"): dict,
        vol.Optional("restore_pending"): cv.boolean,
    }
)
IMPORT_SCHEMA = vol.Schema(
    {
        vol.Required("controller"): cv.entity_id,
        vol.Optional("force", default=False): cv.boolean,
    }
)


def async_register(hass: HomeAssistant) -> None:
    """Register both services once, domain-wide."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_RUNTIME,
        _async_runtime,
        schema=RUNTIME_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_IMPORT,
        _async_import,
        schema=IMPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _controller(call: ServiceCall):
    entity_id = call.data["controller"]
    entry = er.async_get(call.hass).async_get(entity_id)
    config_entry = (
        call.hass.config_entries.async_get_entry(entry.config_entry_id)
        if entry
        and entry.platform == DOMAIN
        and entry.unique_id.endswith("_deye_controller")
        and entry.config_entry_id
        else None
    )
    controller = (
        getattr(getattr(config_entry, "runtime_data", None), "controller", None)
        if config_entry is not None and config_entry.state is ConfigEntryState.LOADED
        else None
    )
    if controller is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="controller_not_found",
            translation_placeholders={"entity": entity_id},
        )
    return controller


async def _async_runtime(call: ServiceCall) -> dict:
    controller = _controller(call)
    try:
        return controller.async_update_runtime(
            call.data.get("runtime"), call.data.get("restore_pending")
        )
    except RuntimeInvalid as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=err.code,
            translation_placeholders={"key": err.key or ""},
        ) from None


async def _async_import(call: ServiceCall) -> dict:
    controller = _controller(call)
    try:
        return await controller.async_import_package(force=call.data["force"])
    except PackageError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=err.code,
            translation_placeholders={"role": err.role},
        ) from None
