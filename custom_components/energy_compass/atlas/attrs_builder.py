"""AttrsV1 builder (ADR-0019 §6). Location only as H3 res 6; no tariff text or inverter model."""

import json
from pathlib import Path

_MANIFEST = Path(__file__).resolve().parent.parent / "manifest.json"


def _compass_version() -> str:
    return json.loads(_MANIFEST.read_text())["version"]


def build_attrs(
    config: dict, atlas_settings: dict, location_h3_res6: str | None = None
) -> dict:
    """`pv_kwp` comes from the Atlas settings step; battery figures from the installation.

    `location_h3_res6` is the home zone's res-6 cell (`atlas.location`); without it the
    `location` key is omitted, exactly as before.
    """
    settings = config.get("settings", {})
    battery_enabled = config.get("sources", {}).get("battery_enabled", False)
    if battery_enabled:
        capacity_kwh = float(settings.get("capacity_kwh", 0.0))
        floor = float(settings.get("operating_floor", 0.0))
        ceiling = float(settings.get("soc_ceiling", 100.0))
        battery_kwh_nominal = capacity_kwh
        battery_kwh_usable = capacity_kwh * (ceiling - floor) / 100.0
        soc_floor_pct = floor
    else:
        battery_kwh_nominal = 0.0
        battery_kwh_usable = 0.0
        soc_floor_pct = None
    attrs = {
        "pv_kwp": atlas_settings["pv_kwp"],
        "battery_kwh_nominal": battery_kwh_nominal,
        "battery_kwh_usable": battery_kwh_usable,
        "soc_floor_pct": soc_floor_pct,
        "compass_version": _compass_version(),
    }
    if location_h3_res6:
        attrs["location"] = {"h3_res6": location_h3_res6}
    return attrs
