"""Home location as an H3 resolution-6 cell (ADR-0019 §6, ADR-0021 §3, ADR-0022).

The HA home zone's coordinates are reduced to a cell of roughly 36 km² on the spot and
dropped: the latitude/longitude are never stored, logged or sent, only the cell index.
`h3` is imported lazily so the off path (ADR-0019 §3) never loads it.
"""

import logging
import math

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

H3_RESOLUTION = 6


def h3_res6(latitude: float | None, longitude: float | None) -> str | None:
    """Return the res-6 cell index for a point, or None when it cannot be computed."""
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    try:
        import h3

        return h3.latlng_to_cell(latitude, longitude, H3_RESOLUTION)
    except Exception:  # missing wheel or h3 error: attributes go without a location
        _LOGGER.warning("Energy Atlas: home location cell unavailable", exc_info=False)
        return None


async def async_home_h3_res6(hass: HomeAssistant) -> str | None:
    """Cell of the HA home zone, computed in the executor (C extension import)."""
    return await hass.async_add_executor_job(
        h3_res6, hass.config.latitude, hass.config.longitude
    )
