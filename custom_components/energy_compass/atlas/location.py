"""Home location as an H3 resolution-6 cell (ADR-0019 §6, ADR-0021 §3, ADR-0022).

The HA home zone's coordinates are reduced to a cell of roughly 36 km² on the spot and
dropped: the latitude/longitude are never stored, logged or sent, only the cell index.
The cell comes from `h3_res6`, a dependency-free port (no `h3` wheel exists for Home
Assistant on Raspberry Pi), and is only imported when Energy Atlas is enabled.
"""

import logging
import math

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def h3_res6(latitude: float | None, longitude: float | None) -> str | None:
    """Return the res-6 cell index for a point, or None when it cannot be computed."""
    if not isinstance(latitude, (int, float)) or not isinstance(
        longitude, (int, float)
    ):
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    try:
        from .h3_res6 import h3_res6_index

        return h3_res6_index(latitude, longitude)
    except ValueError, ArithmeticError, LookupError:  # no coordinates in the log
        _LOGGER.warning("Energy Atlas: home location cell unavailable", exc_info=False)
        return None


def home_h3_res6(hass: HomeAssistant) -> str | None:
    """Cell of the HA home zone (a few microseconds of arithmetic, safe in the loop)."""
    return h3_res6(hass.config.latitude, hass.config.longitude)
