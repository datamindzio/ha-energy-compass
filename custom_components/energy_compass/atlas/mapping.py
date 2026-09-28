"""HA state -> Atlas telemetry key mapping (ADR-0019 §6).

Reuses Compass's own measurement reader (`NumericSetting`/`resolve_numeric`,
`resolve_binding`) and SOC conversion (`balance_tracker.soc_percent`), never a new
normalisation path. Power measurements resolve to kW (Compass's own target unit, see
`source_flow.py` `output_unit`) and are scaled to W here; energy measurements resolve
to kWh already. `sources.load.power` and `sources.soc` carry their own
unit/sign (`history_unit`/`history_sign`, `soc_options`), applied here the same way
`sources/history.py` and `runtime._soc` apply them.

Every value then passes the contract bounds of `MetricValues` (ADR-0019 Amendment
2026-09-28 (T-402)): non-finite -> not fed; grid powers clamped to >= 0; SOC clamped to
0..100; negative energy counters -> not fed. Compass's `battery_power` is + = discharge,
Atlas `batt_w` is + = charge.
"""

import math

from ..balance_tracker import soc_percent
from ..config_models import NumericSetting, resolve_numeric
from ..engine.models import InputError
from ..engine.normalize import finite
from ..runtime import _soc_value
from ..sources.bindings import EntityBinding, resolve_binding

_LOAD_POWER_SCALE = {"W": 1.0, "kW": 1000.0}

_POWER_MEASUREMENTS = (
    "pv_power",
    "grid_import_power",
    "grid_export_power",
    "battery_power",
    "battery_charge_power",
    "battery_discharge_power",
)
_ENERGY_ATLAS_KEYS = {
    "pv_energy": "pv_kwh_total",
    "grid_import_energy": "import_kwh_total",
    "grid_export_energy": "export_kwh_total",
}


def _load_power_binding(config: dict) -> dict | None:
    load = config.get("sources", {}).get("load", {})
    if load.get("mode") == "recorder" and load.get("power"):
        return load["power"]
    return None


def tracked_entity_ids(config: dict) -> set[str]:
    """Entities whose changes can affect an Atlas-fed key."""
    ids = set()
    measurements = config.get("measurements", {})
    for name in (*_POWER_MEASUREMENTS, *_ENERGY_ATLAS_KEYS):
        setting = measurements.get(name)
        if setting and setting.get("entity"):
            ids.add(setting["entity"]["entity_id"])
    soc = config.get("sources", {}).get("soc")
    if soc:
        ids.add(soc["entity_id"])
    load_power = _load_power_binding(config)
    if load_power:
        ids.add(load_power["entity_id"])
    return ids


def _bound(config: dict, name: str) -> dict | None:
    setting = config.get("measurements", {}).get(name)
    if not setting or not setting.get("entity"):
        return None
    return setting


def _resolve_power_w(config: dict, states: dict, now, name: str) -> float | None:
    setting = _bound(config, name)
    if setting is None:
        return None
    try:
        kw = resolve_numeric(NumericSetting.from_dict(setting), states, now)
    except InputError:
        return None
    return kw * 1000.0


def _resolve_energy_kwh(config: dict, states: dict, now, name: str) -> float | None:
    setting = _bound(config, name)
    if setting is None:
        return None
    entity_id = setting["entity"]["entity_id"]
    state = states.get(entity_id)
    if not state:
        return None
    state_class = state.get("attributes", {}).get("state_class")
    if state_class not in ("total", "total_increasing"):
        return None
    try:
        return resolve_numeric(NumericSetting.from_dict(setting), states, now)
    except InputError:
        return None


def _resolve_load_w(config: dict, states: dict) -> float | None:
    binding = _load_power_binding(config)
    if binding is None:
        return None
    try:
        value = finite(
            resolve_binding(states, EntityBinding.from_dict(binding)), "load power"
        )
    except InputError:
        return None
    load = config.get("sources", {}).get("load", {})
    # Absent unit -> not fed (LoadSource defaults to kWh, which is not a power unit).
    scale = _LOAD_POWER_SCALE.get(load.get("history_unit"))
    if scale is None:
        return None
    sign = load.get("history_sign", 1.0)
    return value * scale * sign


def _resolve_soc_pct(config: dict, states: dict) -> float | None:
    soc = config.get("sources", {}).get("soc")
    if not soc:
        return None
    options = config.get("soc_options", {})
    unit = options.get("unit", "%")
    try:
        # Same read as the runtime: unit_of_measurement mismatch and non-finite -> not fed.
        value = _soc_value(states, EntityBinding.from_dict(soc), unit)
    except InputError, KeyError:
        return None
    value *= options.get("sign", 1.0)
    capacity_kwh = config.get("settings", {}).get("capacity_kwh")
    try:
        capacity_kwh = float(capacity_kwh)
    except TypeError, ValueError:
        capacity_kwh = 0.0
    if unit == "kWh" and capacity_kwh <= 0:
        return None
    # Reuses Compass's own SOC->percent conversion (balance_tracker.soc_percent),
    # the same one the runtime uses to publish `soc_pct_last` (ADR-0019 §6).
    return soc_percent(value, unit, capacity_kwh)


def bound_metric(key: str, value: float) -> float | None:
    """Contract bounds of `MetricValues` (ADR-0019 Amendment T-402); None = not fed.

    Shared with `backfill_service.py` (ADR-0019 §8): the same clamp/drop rule applies
    to a live-fed value and to a recorder-statistics aggregate for the same key.
    """
    if not math.isfinite(value):
        return None
    if key in ("grid_import_w", "grid_export_w"):
        return max(0.0, value)
    if key == "soc_pct":
        return min(100.0, max(0.0, value))
    if key.endswith("_kwh_total") and value < 0:
        return None
    return value


def resolve_feed(config: dict, states: dict, now) -> dict[str, float]:
    """Every currently-available Atlas telemetry key/value (ADR-0019 §6 table).

    Unbound measurements, `unavailable`/non-numeric states and `*_today` bindings
    (never looked up here) are simply absent from the result.
    """
    feed = {}
    for key, value in _resolve_raw(config, states, now).items():
        value = bound_metric(key, value)
        if value is not None:
            feed[key] = value
    return feed


def _resolve_raw(config: dict, states: dict, now) -> dict[str, float]:
    feed: dict[str, float] = {}
    pv_w = _resolve_power_w(config, states, now, "pv_power")
    if pv_w is not None:
        feed["pv_w"] = pv_w
    load_w = _resolve_load_w(config, states)
    if load_w is not None:
        feed["load_w"] = load_w
    grid_import_w = _resolve_power_w(config, states, now, "grid_import_power")
    if grid_import_w is not None:
        feed["grid_import_w"] = grid_import_w
    grid_export_w = _resolve_power_w(config, states, now, "grid_export_power")
    if grid_export_w is not None:
        feed["grid_export_w"] = grid_export_w
    discharge_w = _resolve_power_w(config, states, now, "battery_power")
    batt_w = None if discharge_w is None else -discharge_w
    if batt_w is None:
        charge = _resolve_power_w(config, states, now, "battery_charge_power")
        discharge = _resolve_power_w(config, states, now, "battery_discharge_power")
        if charge is not None or discharge is not None:
            batt_w = (charge or 0.0) - (discharge or 0.0)
    if batt_w is not None:
        feed["batt_w"] = batt_w
    soc_pct = _resolve_soc_pct(config, states)
    if soc_pct is not None:
        feed["soc_pct"] = soc_pct
    for name, atlas_key in _ENERGY_ATLAS_KEYS.items():
        value = _resolve_energy_kwh(config, states, now, name)
        if value is not None:
            feed[atlas_key] = value
    return feed
