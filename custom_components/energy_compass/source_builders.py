"""Pure source binding builders shared by the source flow and source detection."""

from collections.abc import Mapping, Sequence
from typing import Any

from .config_models import NumericSetting
from .daily_export import DAILY_EXPORT_MEASUREMENTS
from .engine.models import InputError
from .presets import PRESETS
from .sources.bindings import EntityBinding, IntervalBinding
from .sources.tariffs import is_raw_rce_sell

RCE_UNIT = "PLN/MWh"


def solcast_pv_binding(entity: EntityBinding, timezone: str) -> IntervalBinding:
    """Bind a Solcast total sensor's half-hourly kW detail forecast."""
    preset = PRESETS["solcast"]
    return IntervalBinding(
        EntityBinding(entity.entity_id, entity.registry_id, preset.pv_attribute),
        value_path=preset.pv_value_field or "value",
        start_path=preset.pv_start_field,
        end_path=None,
        duration_path=None,
        interval_minutes=preset.pv_interval_minutes,
        unit=preset.pv_unit or "kW",
        unit_path=None,
        value_kind="power",
        value_sign=1.0,
        source_timezone=timezone,
        published_path=None,
        max_age_seconds=24 * 3600,
    )


def rce_sell_binding(entity: EntityBinding, timezone: str) -> IntervalBinding:
    """Bind a raw RCE PSE price sensor through the PSE preset mapping."""
    preset = PRESETS["pse"]
    return IntervalBinding(
        EntityBinding(entity.entity_id, entity.registry_id, preset.price_attribute),
        value_path=preset.price_value_field or "value",
        start_path=preset.price_start_field,
        end_path=preset.price_end_field,
        interval_minutes=preset.price_interval_minutes,
        unit=preset.price_unit or RCE_UNIT,
        value_kind="price",
        value_sign=1.0,
        source_timezone=timezone,
        max_age_seconds=24 * 3600,
    )


def template_sell_binding(entity: EntityBinding) -> IntervalBinding:
    """Bind the Energy Compass sell template with its documented mapping."""
    return IntervalBinding(
        EntityBinding(entity.entity_id, entity.registry_id, "prices"),
        value_path="price",
        start_path="start",
        end_path="end",
        duration_path=None,
        interval_minutes=15,
        unit="PLN/kWh",
        unit_path=None,
        value_kind="price",
        value_sign=1.0,
        source_timezone="UTC",
        published_path="attributes.published_at",
        max_age_seconds=4500,
    )


def rce_unit_error(attributes: Mapping[str, Any]) -> str | None:
    """Reject an RCE sensor whose unit would scale prices a thousand times off."""
    unit = attributes.get("unit_of_measurement")
    if unit is not None and unit != RCE_UNIT:
        return f"RCE sensor must report {RCE_UNIT}"
    return None


def set_pv_group(draft: dict, index: int, bindings: Sequence[IntervalBinding]) -> None:
    """Replace or add one PV continuation group and drop Energy solar forecasts."""
    pv = draft["sources"]["pv"]
    groups = pv["arrays"]
    if index < 0 or index > len(groups):
        raise InputError("add PV groups in consecutive order")
    data = [binding.to_dict() for binding in bindings]
    if index == len(groups):
        groups.append(data)
    else:
        groups[index] = data
    pv.pop("solar_forecasts", None)


def set_rce_sell(draft: dict, bindings: Sequence[IntervalBinding]) -> None:
    """Store raw RCE sell bindings with the zero price floor."""
    price = draft["sources"]["sell"]
    price.update(
        mode="forecast",
        forecast=[binding.to_dict() for binding in bindings],
        fixed=None,
        floor_per_kwh=0.0,
    )
    price.pop("schedule", None)
    draft["helpers"].pop("sell_rate", None)


def set_forecast_sell(draft: dict, bindings: Sequence[IntervalBinding]) -> None:
    """Replace the sell source by forecast bindings, keeping a raw RCE floor only."""
    price = draft["sources"]["sell"]
    price.update(
        mode="forecast",
        forecast=[binding.to_dict() for binding in bindings],
        fixed=None,
    )
    price.pop("schedule", None)
    if not is_raw_rce_sell(price):
        price.pop("floor_per_kwh", None)
    draft["helpers"].pop("sell_rate", None)


def set_load_statistic(draft: dict, statistic_id: str, unit: str, sign: float) -> None:
    """Read household load from a recorder energy statistic."""
    draft["sources"]["load"].update(
        mode="recorder",
        statistic_id=statistic_id,
        power=None,
        forecast=None,
        daily_estimate=None,
        history_unit=unit,
        history_sign=sign,
    )


def set_soc(
    draft: dict,
    entity: EntityBinding,
    *,
    bms: bool = False,
    unit: str = "%",
    sign: float = 1,
) -> None:
    """Bind the SOC or BMS SOC source with its reading options."""
    prefix = "bms_" if bms else ""
    options = draft["soc_options"]
    draft["sources"]["bms_soc" if bms else "soc"] = entity.to_dict()
    options[prefix + "sign"] = sign
    options[prefix + "unit"] = unit
    options[prefix + "timestamp_path"] = options.get(
        prefix + "timestamp_path", "last_updated"
    )
    options[prefix + "timestamp_policy"] = options.get(
        prefix + "timestamp_policy", "auto"
    )


def measurement_setting(
    row: str,
    entity: EntityBinding,
    *,
    source_unit: str,
    sign: float,
    max_age_seconds: float | None,
) -> dict:
    """Normalize one measurement entity to kW or kWh for diagnostics."""
    scaled = source_unit in ("W", "Wh")
    return NumericSetting(
        entity=entity,
        unit="kW" if "power" in row else "kWh",
        source_unit=source_unit,
        multiplier=sign * (0.001 if scaled else 1),
        minimum=0 if row in DAILY_EXPORT_MEASUREMENTS else None,
        maximum=None,
        max_age_seconds=max_age_seconds,
    ).to_dict()
