from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from .engine.models import InputError
from .engine.normalize import aware, finite
from .sources.bindings import (
    EntityBinding,
    IntervalBinding,
    parse_timestamp,
    resolve_binding,
    validate_dependencies,
)
from .sources.tariffs import TariffSchedule, validate_schedule


@dataclass(frozen=True)
class NumericSetting:
    fixed: float | None = None
    entity: EntityBinding | None = None
    unit: str = ""
    source_unit: str | None = None
    multiplier: float = 1.0
    minimum: float | None = None
    maximum: float | None = None
    max_age_seconds: float | None = 86400.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize a fixed or helper-backed numeric selection."""
        return {
            **asdict(self),
            "entity": self.entity.to_dict() if self.entity else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> NumericSetting:
        """Restore a numeric selection from config-entry data."""
        return cls(
            **{
                **value,
                "entity": EntityBinding.from_dict(value["entity"])
                if value.get("entity")
                else None,
            }
        )


def resolve_numeric(
    setting: NumericSetting,
    states: Mapping[str, Any],
    now: datetime,
    *,
    max_age_seconds: float | None = None,
) -> float:
    """Read and validate a selected fixed or helper-backed number."""
    aware(now, "now")
    if (setting.fixed is None) == (setting.entity is None):
        raise InputError("numeric setting requires exactly one fixed or entity source")
    if setting.entity is not None:
        raw = resolve_binding(states, setting.entity)
        age_limit = (
            setting.max_age_seconds if max_age_seconds is None else max_age_seconds
        )
        if age_limit is not None:
            age_limit = finite(age_limit, "numeric source age limit")
            if age_limit <= 0:
                raise InputError("numeric source age limit must be positive")
            snapshot = states[setting.entity.entity_id]
            updated = parse_timestamp(snapshot.get("last_updated"))
            if (now - updated).total_seconds() > age_limit:
                raise InputError("stale numeric source")
        attributes = states[setting.entity.entity_id].get("attributes", {})
        actual_unit = attributes.get("unit_of_measurement")
        expected_unit = setting.source_unit or setting.unit
        if (
            setting.entity.attribute is None
            and expected_unit
            and actual_unit
            and actual_unit != expected_unit
        ):
            raise InputError("numeric source unit mismatch")
    else:
        raw = setting.fixed
    value = finite(raw, "numeric setting") * finite(
        setting.multiplier, "numeric multiplier"
    )
    if setting.minimum is not None and value < finite(setting.minimum, "minimum"):
        raise InputError("numeric setting below minimum")
    if setting.maximum is not None and value > finite(setting.maximum, "maximum"):
        raise InputError("numeric setting above maximum")
    return value


@dataclass(frozen=True)
class PriceSource:
    mode: Literal["forecast", "fixed", "schedule"]
    forecast: tuple[IntervalBinding, ...] = ()
    fixed: NumericSetting | None = None
    multiplier: NumericSetting = field(
        default_factory=lambda: NumericSetting(fixed=1.0)
    )
    addition_per_kwh: NumericSetting = field(
        default_factory=lambda: NumericSetting(fixed=0.0)
    )
    schedule: TariffSchedule | None = None
    floor_per_kwh: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize selected forecast, fixed or schedule settlement inputs."""
        data: dict[str, Any] = {
            "mode": self.mode,
            "forecast": [item.to_dict() for item in self.forecast],
            "fixed": self.fixed.to_dict() if self.fixed else None,
            "multiplier": self.multiplier.to_dict(),
            "addition_per_kwh": self.addition_per_kwh.to_dict(),
        }
        if self.schedule is not None:
            data["schedule"] = self.schedule.to_dict()
        if self.floor_per_kwh is not None:
            data["floor_per_kwh"] = self.floor_per_kwh
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PriceSource:
        """Restore selected settlement inputs."""
        return cls(
            value["mode"],
            tuple(IntervalBinding.from_dict(item) for item in value["forecast"]),
            NumericSetting.from_dict(value["fixed"]) if value.get("fixed") else None,
            NumericSetting.from_dict(value["multiplier"]),
            NumericSetting.from_dict(value["addition_per_kwh"]),
            TariffSchedule.from_dict(value["schedule"])
            if value.get("schedule")
            else None,
            value.get("floor_per_kwh"),
        )


@dataclass(frozen=True)
class SolarForecastBinding:
    config_entry_id: str
    domain: str

    def to_dict(self) -> dict[str, str]:
        """Serialize one Energy dashboard solar forecast integration entry."""
        return {"config_entry_id": self.config_entry_id, "domain": self.domain}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SolarForecastBinding:
        """Restore one Energy dashboard solar forecast integration entry."""
        return cls(value["config_entry_id"], value["domain"])


@dataclass(frozen=True)
class PvSource:
    enabled: bool
    arrays: tuple[tuple[IntervalBinding, ...], ...] = ()
    solar_forecasts: tuple[SolarForecastBinding, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize explicit additive arrays and continuation groups."""
        data: dict[str, Any] = {
            "enabled": self.enabled,
            "arrays": [[item.to_dict() for item in group] for group in self.arrays],
        }
        if self.solar_forecasts:
            data["solar_forecasts"] = [item.to_dict() for item in self.solar_forecasts]
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PvSource:
        """Restore explicit additive arrays and continuation groups."""
        return cls(
            value["enabled"],
            tuple(
                tuple(IntervalBinding.from_dict(item) for item in group)
                for group in value["arrays"]
            ),
            tuple(
                SolarForecastBinding.from_dict(item)
                for item in value.get("solar_forecasts", ())
            ),
        )


@dataclass(frozen=True)
class LoadSource:
    mode: Literal["forecast", "recorder", "daily_estimate"]
    forecast: IntervalBinding | None = None
    statistic_id: str | None = None
    power: EntityBinding | None = None
    daily_estimate: NumericSetting | None = None
    history_unit: str = "kWh"
    history_sign: float = 1.0
    power_max_gap_minutes: float = 60.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize the selected load forecasting mode."""
        return {
            "mode": self.mode,
            "forecast": self.forecast.to_dict() if self.forecast else None,
            "statistic_id": self.statistic_id,
            "power": self.power.to_dict() if self.power else None,
            "daily_estimate": self.daily_estimate.to_dict()
            if self.daily_estimate
            else None,
            "history_unit": self.history_unit,
            "history_sign": self.history_sign,
            "power_max_gap_minutes": self.power_max_gap_minutes,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LoadSource:
        """Restore the selected load forecasting mode."""
        return cls(
            value["mode"],
            IntervalBinding.from_dict(value["forecast"])
            if value.get("forecast")
            else None,
            value.get("statistic_id"),
            EntityBinding.from_dict(value["power"]) if value.get("power") else None,
            NumericSetting.from_dict(value["daily_estimate"])
            if value.get("daily_estimate")
            else None,
            value.get("history_unit", "kWh"),
            value.get("history_sign", 1.0),
            value.get("power_max_gap_minutes", 60.0),
        )


@dataclass(frozen=True)
class SourceConfig:
    buy: PriceSource
    sell: PriceSource
    pv: PvSource
    load: LoadSource
    battery_enabled: bool
    soc: EntityBinding | None = None
    bms_soc: EntityBinding | None = None
    numeric: Mapping[str, NumericSetting] = field(default_factory=dict)
    currency: str = "PLN"

    def to_dict(self) -> dict[str, Any]:
        """Serialize selected sources without any live state values."""
        return {
            "buy": self.buy.to_dict(),
            "sell": self.sell.to_dict(),
            "pv": self.pv.to_dict(),
            "load": self.load.to_dict(),
            "battery_enabled": self.battery_enabled,
            "soc": self.soc.to_dict() if self.soc else None,
            "bms_soc": self.bms_soc.to_dict() if self.bms_soc else None,
            "numeric": {
                name: setting.to_dict() for name, setting in self.numeric.items()
            },
            "currency": self.currency,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceConfig:
        """Restore selectors and policies from config-entry data."""
        return cls(
            PriceSource.from_dict(value["buy"]),
            PriceSource.from_dict(value["sell"]),
            PvSource.from_dict(value["pv"]),
            LoadSource.from_dict(value["load"]),
            value["battery_enabled"],
            EntityBinding.from_dict(value["soc"]) if value.get("soc") else None,
            EntityBinding.from_dict(value["bms_soc"]) if value.get("bms_soc") else None,
            {
                name: NumericSetting.from_dict(item)
                for name, item in value.get("numeric", {}).items()
            },
            value.get("currency", "PLN"),
        )


def validate_sources(config: SourceConfig, own_entity_ids: set[str]) -> None:
    """Check component enablement, selected modes, and direct feedback cycles."""
    dependencies: list[EntityBinding] = []
    for role, price in (("buy", config.buy), ("sell", config.sell)):
        if price.schedule is not None and price.mode != "schedule":
            raise InputError("tariff schedule key under a non-schedule mode")
        if price.floor_per_kwh is not None:
            if price.mode != "forecast":
                raise InputError("price floor needs a forecast source")
            finite(price.floor_per_kwh, "price floor")
        if price.mode == "schedule":
            if role != "buy":
                raise InputError("tariff schedule is a buy source only")
            if price.schedule is None or price.forecast or price.fixed is not None:
                raise InputError("schedule price needs a tariff only")
            if config.currency != "PLN":
                raise InputError("tariff schedule requires PLN")
            validate_schedule(price.schedule)
        elif price.mode == "forecast":
            if not price.forecast or price.fixed is not None:
                raise InputError("forecast price needs interval bindings only")
            dependencies.extend(item.entity for item in price.forecast)
        elif price.mode == "fixed":
            if price.fixed is None or price.forecast:
                raise InputError("fixed price needs one rate only")
        else:
            raise InputError("invalid price mode")
        for setting in (price.fixed, price.multiplier, price.addition_per_kwh):
            if setting and setting.entity:
                dependencies.append(setting.entity)
    if config.pv.enabled:
        if config.pv.arrays and config.pv.solar_forecasts:
            raise InputError(
                "PV forecast arrays and Energy solar forecasts cannot be combined"
            )
        if config.pv.solar_forecasts:
            identifiers = [item.config_entry_id for item in config.pv.solar_forecasts]
            if len(set(identifiers)) != len(identifiers):
                raise InputError("duplicate Energy solar forecast")
        elif not config.pv.arrays or any(not group for group in config.pv.arrays):
            raise InputError("enabled PV needs forecast arrays")
        dependencies.extend(
            binding.entity for group in config.pv.arrays for binding in group
        )
    elif config.pv.arrays or config.pv.solar_forecasts:
        raise InputError("disabled PV cannot have active bindings")
    if config.load.mode == "forecast":
        if config.load.forecast is None:
            raise InputError("forecast load source missing")
        dependencies.append(config.load.forecast.entity)
    elif config.load.mode == "recorder":
        if not config.load.statistic_id and config.load.power is None:
            raise InputError("recorder load source missing")
        if config.load.power:
            dependencies.append(config.load.power)
    elif config.load.mode == "daily_estimate":
        if config.load.daily_estimate is None:
            raise InputError("daily estimate missing")
    else:
        raise InputError("invalid load mode")
    if config.battery_enabled:
        if config.soc is None:
            raise InputError("enabled battery needs SOC")
        dependencies.append(config.soc)
        if config.bms_soc:
            dependencies.append(config.bms_soc)
    elif config.soc is not None or config.bms_soc is not None:
        raise InputError("disabled battery cannot have active SOC")
    if config.load.daily_estimate and config.load.daily_estimate.entity:
        dependencies.append(config.load.daily_estimate.entity)
    dependencies.extend(
        setting.entity for setting in config.numeric.values() if setting.entity
    )
    validate_dependencies(tuple(dependencies), own_entity_ids)
    if not config.currency or len(config.currency) != 3:
        raise InputError("currency must be an ISO 4217 code")
