"""Recorder-statistics backfill service `energy_compass.atlas_backfill` (ADR-0019 §8).

Registered in `async_setup` (HA rule; domain-wide, independent of any entry's Atlas
state) so it exists whether or not Atlas is enabled anywhere ("registered but inert"
off path, ADR-0019 §3). Reads recorder statistics for every entity the live feed maps
(`atlas/mapping.py`) via the recorder executor, converts them into the same per-window
Atlas metric names a live window would carry, and hands the result to each running
sink's `backfill()` (`atlas_sink/backfill.py` enqueues them, 5-minute wins per hour).
"""

from datetime import UTC, datetime, timedelta

import voluptuous as vol
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from ..balance_tracker import soc_percent
from ..settings import DOMAIN
from .mapping import (
    _ENERGY_ATLAS_KEYS,
    _LOAD_POWER_SCALE,
    _bound,
    _load_power_binding,
    bound_metric,
    tracked_entity_ids,
)

SERVICE_ATLAS_BACKFILL = "atlas_backfill"

SERVICE_SCHEMA = vol.Schema(
    {vol.Optional("days"): vol.All(vol.Coerce(int), vol.Range(min=1, max=3650))}
)

_STAT_TYPES = {"mean", "max", "state"}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def async_register(hass: HomeAssistant) -> None:
    """Register the service once, domain-wide."""
    hass.services.async_register(
        DOMAIN, SERVICE_ATLAS_BACKFILL, _async_handle_backfill, schema=SERVICE_SCHEMA
    )


async def _async_handle_backfill(call: ServiceCall) -> None:
    hass = call.hass
    coordinators = _enabled_registered_coordinators(hass)
    if not coordinators:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="atlas_backfill_not_enabled"
        )
    days = call.data.get("days")
    end = dt_util.utcnow()
    start = _EPOCH if days is None else end - timedelta(days=days)
    instance = get_instance(hass)
    for coordinator in coordinators:
        stats = await _async_collect(
            hass, instance, coordinator.configuration, start, end
        )
        if stats["five_minute"] or stats["hourly"]:
            coordinator.atlas.sink.backfill(stats)


def _enabled_registered_coordinators(hass: HomeAssistant) -> list:
    """Every loaded entry whose Atlas sink is actually running (enabled + registered)."""
    coordinators = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = entry.runtime_data
        if (
            coordinator is not None
            and coordinator.atlas is not None
            and coordinator.atlas.sink is not None
        ):
            coordinators.append(coordinator)
    return coordinators


async def _async_collect(
    hass: HomeAssistant, instance, config: dict, start: datetime, end: datetime
) -> dict[str, list[dict]]:
    entity_ids = tracked_entity_ids(config)
    if not entity_ids:
        return {"five_minute": [], "hourly": []}
    five = await instance.async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        entity_ids,
        "5minute",
        None,
        _STAT_TYPES,
    )
    hourly = await instance.async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        entity_ids,
        "hour",
        None,
        _STAT_TYPES,
    )
    return {
        "five_minute": _merge_rows(config, five),
        "hourly": _merge_rows(config, hourly),
    }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def _merge_rows(config: dict, per_entity: dict[str, list[dict]]) -> list[dict]:
    """One recorder statistic list per entity -> one Atlas row per shared timestamp."""
    by_ts: dict[float, dict[str, dict]] = {}
    for entity_id, rows in per_entity.items():
        for row in rows:
            by_ts.setdefault(row["start"], {})[entity_id] = row
    merged = []
    for ts in sorted(by_ts):
        atlas_row = _atlas_row(config, by_ts[ts])
        if atlas_row:
            merged.append({"start": _iso(ts), **atlas_row})
    return merged


def _power_stat(config: dict, entity_at: dict, name: str, field: str) -> float | None:
    setting = _bound(config, name)
    if setting is None:
        return None
    entity = setting["entity"]
    if entity.get("attribute") is not None:
        return None  # a recorder statistic tracks the entity's own state only
    row = entity_at.get(entity["entity_id"])
    if row is None or row.get(field) is None:
        return None
    return row[field] * float(setting.get("multiplier", 1.0)) * 1000.0


def _load_stat(config: dict, entity_at: dict, field: str) -> float | None:
    binding = _load_power_binding(config)
    if binding is None or binding.get("attribute") is not None:
        return None
    row = entity_at.get(binding["entity_id"])
    if row is None or row.get(field) is None:
        return None
    load = config.get("sources", {}).get("load", {})
    scale = _LOAD_POWER_SCALE.get(load.get("history_unit"))
    if scale is None:
        return None
    sign = load.get("history_sign", 1.0)
    return row[field] * scale * sign


def _batt_stat(config: dict, entity_at: dict, field: str) -> float | None:
    discharge = _power_stat(config, entity_at, "battery_power", field)
    if discharge is not None:
        return -discharge
    charge = _power_stat(config, entity_at, "battery_charge_power", field)
    split_discharge = _power_stat(config, entity_at, "battery_discharge_power", field)
    if charge is not None or split_discharge is not None:
        return (charge or 0.0) - (split_discharge or 0.0)
    return None


def _soc_stat(config: dict, entity_at: dict) -> float | None:
    soc = config.get("sources", {}).get("soc")
    if not soc or soc.get("attribute") is not None:
        return None
    row = entity_at.get(soc["entity_id"])
    if row is None or row.get("mean") is None:
        return None
    options = config.get("soc_options", {})
    unit = options.get("unit", "%")
    value = row["mean"] * options.get("sign", 1.0)
    capacity_kwh = config.get("settings", {}).get("capacity_kwh")
    try:
        capacity_kwh = float(capacity_kwh)
    except TypeError, ValueError:
        capacity_kwh = 0.0
    if unit == "kWh" and capacity_kwh <= 0:
        return None
    return soc_percent(value, unit, capacity_kwh)


def _energy_stat(config: dict, entity_at: dict, name: str) -> float | None:
    setting = _bound(config, name)
    if setting is None:
        return None
    entity = setting["entity"]
    if entity.get("attribute") is not None:
        return None
    row = entity_at.get(entity["entity_id"])
    if row is None or row.get("state") is None:
        return None
    return row["state"] * float(setting.get("multiplier", 1.0))


def _put(row: dict, store_key: str, bound_key: str, value: float | None) -> None:
    """`bound_key` is the live-feed key `bound_metric` recognises (e.g. `grid_import_w`,
    `soc_pct`); `store_key` is what actually goes on the row (e.g. `grid_import_w_avg`,
    `soc_pct_last`) — the two differ for every bounded key except the `*_kwh_total`
    counters, where they are the same string."""
    if value is None:
        return
    bounded = bound_metric(bound_key, value)
    if bounded is not None:
        row[store_key] = bounded


def _atlas_row(config: dict, entity_at: dict[str, dict]) -> dict:
    """One recorder-statistics timestamp -> the Atlas metric keys it can fill (ADR-0019 §8).

    Power mean/max -> `<key>_avg`/`<key>_max`, `_max` only for pv/load (matches the
    live `MetricValues` shape, ADR-0019 §6); grid import/export and battery: mean
    only; SOC mean -> `soc_pct_last`; energy counters: last `state` in the bucket ->
    `*_kwh_total`. Every value passes the same contract bound as the live feed
    (`mapping.bound_metric`, Amendment T-402).
    """
    row: dict[str, float] = {}
    _put(row, "pv_w_avg", "pv_w", _power_stat(config, entity_at, "pv_power", "mean"))
    _put(row, "pv_w_max", "pv_w", _power_stat(config, entity_at, "pv_power", "max"))
    _put(row, "load_w_avg", "load_w", _load_stat(config, entity_at, "mean"))
    _put(row, "load_w_max", "load_w", _load_stat(config, entity_at, "max"))
    _put(
        row,
        "grid_import_w_avg",
        "grid_import_w",
        _power_stat(config, entity_at, "grid_import_power", "mean"),
    )
    _put(
        row,
        "grid_export_w_avg",
        "grid_export_w",
        _power_stat(config, entity_at, "grid_export_power", "mean"),
    )
    _put(row, "batt_w_avg", "batt_w", _batt_stat(config, entity_at, "mean"))
    _put(row, "soc_pct_last", "soc_pct", _soc_stat(config, entity_at))
    for name, atlas_key in _ENERGY_ATLAS_KEYS.items():
        _put(row, atlas_key, atlas_key, _energy_stat(config, entity_at, name))
    return row
