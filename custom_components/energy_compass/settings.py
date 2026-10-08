"""Installation settings and shared validation for all entry flows."""

import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config_models import (
    LoadSource,
    NumericSetting,
    PriceSource,
    PvSource,
    SourceConfig,
    resolve_numeric,
    validate_sources,
)
from .engine.models import STRATEGIES, InputError
from .engine.normalize import finite
from .engine.strategy import STRATEGY_OWNED_KEYS

DOMAIN = "energy_compass"

# STRATEGIES and STRATEGY_OWNED_KEYS are imported above: engine.models / engine.strategy
# are the single source of truth, so this module and its importers just re-use the name.

# Bounds also constrain helper inputs, which bypass form selectors.
NUMBERS = {
    "capacity_kwh": ("battery", 10, 0.1, 1000, "kWh"),
    "hardware_floor": ("battery", 0, 0, 99, "%"),
    "operating_floor": ("battery", 0, 0, 99, "%"),
    "soc_ceiling": ("battery", 100, 1, 100, "%"),
    "charge_kw": ("battery", 3, 0, 1000, "kW"),
    "discharge_kw": ("battery", 3, 0, 1000, "kW"),
    "eta_charge": ("battery", 1, 0.01, 1, ""),
    "eta_discharge": ("battery", 1, 0.01, 1, ""),
    "wear_per_kwh": ("battery", 0, 0, 1000, "currency/kWh"),
    "idle_drain_kw": ("battery", 0, 0, 10, "kW"),
    "daily_cycles": ("battery", 0, 0, 10, ""),
    "balance_interval_days": ("battery", 7, 1, 60, "d"),
    "balance_hold_minutes": ("battery", 60, 15, 360, "min"),
    "balance_soc_threshold": ("battery", 99, 90, 100, "%"),
    "balance_value": ("battery", 5.0, 0, 1000, "currency"),
    "soc_max_age_seconds": ("battery", 600, 1, 86400, "s"),
    "bms_max_age_seconds": ("battery", 600, 1, 86400, "s"),
    "soc_disagreement_percent": ("battery", 5, 0, 100, "%"),
    "soc_jump_percent": ("battery", 2, 0, 100, "%"),
    "inverter_kw": ("hardware", 0, 0, 1000, "kW"),
    "grid_import_kw": ("hardware", 10, 0, 1000, "kW"),
    "grid_export_kw": ("hardware", 0, 0, 1000, "kW"),
    "buy_rate": ("tariffs", 0, -1000, 1000, "currency/kWh"),
    "buy_off_peak_rate": ("tariffs", 0, -1000, 1000, "currency/kWh"),
    "sell_rate": ("tariffs", 0, -1000, 1000, "currency/kWh"),
    "buy_multiplier": ("tariffs", 1, 0, 1000, ""),
    "sell_multiplier": ("tariffs", 1, 0, 1000, ""),
    "buy_addition": ("tariffs", 0, -1000, 1000, "currency/kWh"),
    "sell_addition": ("tariffs", 0, -1000, 1000, "currency/kWh"),
    "vat_percent": ("tariffs", 0, 0, 100, "%"),
    "monthly_charge": ("tariffs", 0, 0, 10000, "currency"),
    "daily_load_kwh": ("forecast", 10, 0, 10000, "kWh"),
    "fallback_daily_kwh": ("forecast", 10, 0, 10000, "kWh"),
    "lookback_days": ("forecast", 14, 1, 60, "d"),
    "minimum_samples": ("forecast", 2, 1, 30, ""),
    "power_max_gap_minutes": ("forecast", 60, 1, 120, "min"),
    "horizon_hours": ("planning", 24, 1, 48, "h"),
    "refresh_minutes": ("planning", 15, 1, 60, "min"),
    "minimum_mode_minutes": ("planning", 60, 0, 1440, "min"),
    "minimum_mode_power_kw": ("planning", 0.1, 0.001, 1000, "kW"),
    "minimum_export_episode_benefit": ("planning", 1, 0, 1000, "currency"),
    "minimum_grid_charge_episode_benefit": ("planning", 0, 0, 1000, "currency"),
    "import_penalty_per_kwh": ("planning", 0, 0, 1000, "currency/kWh"),
    "maximum_grid_charge_price": ("planning", 0, -1000, 1000, "currency/kWh"),
    "terminal_value_per_kwh": ("planning", 0, -1000, 1000, "currency/kWh"),
    "self_sufficiency_import_price_per_kwh": ("planning", 5.0, 0, 1000, "currency/kWh"),
    "self_sufficiency_export_penalty_per_kwh": (
        "planning",
        0.20,
        0,
        1000,
        "currency/kWh",
    ),
    "pv_swap_margin_per_kwh": ("planning", 0.05, 0, 1000, "currency/kWh"),
    "backup_target_soc_percent": ("planning", 80, 0, 100, "%"),
    "backup_shortfall_price_per_kwh": ("planning", 2.0, 0, 1000, "currency/kWh"),
    "peak_import_price_per_kw": ("planning", 0.50, 0, 1000, "currency/kW"),
    "cap_violation_price_per_kwh": ("planning", 2.0, 0, 1000, "currency/kWh"),
    "grid_friendly_import_cap_kw": ("planning", 0, 0, 1000, "kW"),
    "grid_friendly_export_cap_kw": ("planning", 0, 0, 1000, "kW"),
    "autonomy_margin_per_kwh": ("planning", 0.10, 0, 1000, "currency/kWh"),
    "peak_reserve_margin_kwh": ("planning", 0, 0, 100, "kWh"),
    "display_horizon_hours": ("compass", 24, 1, 48, "h"),
    "reference_horizon_hours": ("compass", 24, 1, 48, "h"),
    "display_interval_minutes": ("compass", 60, 15, 60, "min"),
    "probe_kwh": ("compass", 1, 0.01, 5, "kWh"),
    "flexible_load_max_power_kw": ("compass", 3, 0.01, 100, "kW"),
    "flexible_price_degradation_percent": ("compass", 15, 0, 100, "%"),
    "boost_ceiling": ("compass", 0.01, -1000, 1000, "currency/kWh"),
    "cheap_percentile": ("compass", 25, 0, 100, "%"),
    "limit_percentile": ("compass", 75, 0, 100, "%"),
    "limit_floor": ("compass", 1, -1000, 1000, "currency/kWh"),
    "debounce_seconds": ("performance", 5, 0, 60, "s"),
    "minimum_replan_seconds": ("performance", 900, 0, 3600, "s"),
    "soc_trigger_percent": ("performance", 5, 0.1, 100, "%"),
    "solve_time_limit_s": ("performance", 10, 0.1, 30, "s"),
    "probe_time_limit_s": ("performance", 2, 0.01, 30, "s"),
    "total_time_limit_s": ("performance", 60, 1, 300, "s"),
    "cost_precision": ("presentation", 3, 0, 6, ""),
    "notify_minimum_hours": ("notifications", 2, 0, 48, "h"),
    "notify_limit_lead_minutes": ("notifications", 30, 0, 1440, "min"),
    "notify_daily_max": ("notifications", 3, 0, 100, ""),
    "notify_cooldown_minutes": ("notifications", 60, 0, 1440, "min"),
}
INTEGERS = {
    "lookback_days",
    "minimum_samples",
    "horizon_hours",
    "display_horizon_hours",
    "reference_horizon_hours",
    "display_interval_minutes",
    "cost_precision",
    "notify_daily_max",
    "refresh_minutes",
    "balance_interval_days",
    "balance_hold_minutes",
}
# backup_target_soc_percent is deliberately left out of INTEGERS: it is a float
# percent like hardware_floor/soc_disagreement_percent, not a whole-number count.
BOOLEANS = {
    "allow_grid_charge": ("hardware", False),
    "allow_battery_export": ("hardware", False),
    "limit_export_to_pv": ("planning", True),
    "limit_grid_charge_price": ("planning", False),
    "allow_curtailment": ("hardware", False),
    "buy_apply_vat": ("tariffs", False),
    "sell_apply_vat": ("tariffs", False),
    "group_weekends": ("forecast", True),
    "allow_fallback": ("forecast", False),
    "expose_costs": ("presentation", True),
    "expose_windows": ("presentation", True),
    "flexible_load_enabled": ("compass", True),
    "notify_enabled": ("notifications", False),
    "autonomy_reserve": ("planning", False),
    "lfp_balance": ("battery", False),
}
CHOICES = {
    "calibration": ("tariffs", ["unvalidated", "verified"]),
    "capacity_calibration": ("battery", ["unvalidated", "verified"]),
    "terminal_mode": ("planning", ["preserve_initial", "value"]),
    "export_limit_scope": ("planning", ["local_day", "produced"]),
    "short_coverage": ("compass", ["absolute_fallback", "unavailable"]),
    "forecast_method": ("forecast", ["bucketed_history"]),
    "strategy": ("planning", list(STRATEGIES)),
}
GROUPS = (
    "battery",
    "hardware",
    "tariffs",
    "forecast",
    "planning",
    "compass",
    "performance",
    "presentation",
    "notifications",
)
EXPERT_GROUPS = ("planning", "compass", "performance", "presentation")


def default_configuration(currency: str, timezone: str) -> dict:
    """Build visible generic defaults without provider or household assumptions."""
    settings = {key: spec[1] for key, spec in NUMBERS.items()}
    settings.update({key: spec[1] for key, spec in BOOLEANS.items()})
    settings.update({key: spec[1][0] for key, spec in CHOICES.items()})
    settings.update(
        quiet_start="22:00:00",
        quiet_end="08:00:00",
        notify_actions=[],
        notify_events=["favorable", "limit"],
        window_label="Energy Compass",
        boost_color="#2e7d32",
        cheap_color="#66bb6a",
        normal_color="#78909c",
        limit_color="#c62828",
    )
    sources = SourceConfig(
        PriceSource("fixed", fixed=NumericSetting(fixed=0, unit=f"{currency}/kWh")),
        PriceSource("fixed", fixed=NumericSetting(fixed=0, unit=f"{currency}/kWh")),
        PvSource(False),
        LoadSource(
            "daily_estimate", daily_estimate=NumericSetting(fixed=10, unit="kWh")
        ),
        False,
        currency=currency,
    )
    return {
        "name": "Energy Compass",
        "currency": currency,
        "timezone": timezone,
        "preset": "generic",
        "sources": sources.to_dict(),
        "settings": settings,
        "helpers": {},
        "measurements": {},
        "soc_options": {
            "unit": "%",
            "bms_unit": "%",
            "timestamp_path": "last_reported",
            "bms_timestamp_path": "last_reported",
            "timestamp_policy": "auto",
            "bms_timestamp_policy": "auto",
        },
        "strategy_changed_at": None,
        "explicit_strategy_fields": [],
    }


def merged_configuration(entry) -> dict:
    """Resolve one atomic options document over its structural configuration."""
    return deepcopy(dict(entry.options.get("configuration", entry.data)))


# Settings whose value decides which entities exist (sensor.py setup and entity
# defaults). A change needs the platforms set up again, so it reloads the entry.
ENTITY_SET_SETTINGS = frozenset(
    {"lfp_balance", "flexible_load_enabled", "expose_costs", "expose_windows"}
)
# Entry-level values baked into entities at construction: title/device name and the
# cost sensors' unit of measurement.
ENTITY_SET_TOP_LEVEL = frozenset({"name", "currency"})
# Settings the entity platforms read on every publication; a live apply reaches them.
RUNTIME_READ_SETTINGS = frozenset({"cost_precision", "strategy"})


def options_require_reload(previous: Mapping, new: Mapping) -> bool:
    """True when saving `new` over `previous` changes the entity set, not just planning."""
    if any(previous.get(key) != new.get(key) for key in ENTITY_SET_TOP_LEVEL):
        return True
    old_settings, new_settings = previous["settings"], new["settings"]
    return any(
        old_settings.get(key, _default_of(key))
        != new_settings.get(key, _default_of(key))
        for key in ENTITY_SET_SETTINGS
    )


def _default_of(key: str) -> object:
    if key in NUMBERS:
        return NUMBERS[key][1]
    if key in BOOLEANS:
        return BOOLEANS[key][1]
    if key in CHOICES:
        return CHOICES[key][1][0]
    raise KeyError(key)


def explicit_strategy_fields(settings: Mapping[str, object]) -> list[str]:
    """Record which strategy-owned settings deviate from their shipped default."""
    return sorted(
        key for key in STRATEGY_OWNED_KEYS if settings.get(key) != _default_of(key)
    )


def stamp_strategy_change(
    draft: dict, previous: Mapping[str, object] | None, now: datetime
) -> None:
    """Arm a one-shot lock release when the stored strategy actually changes.

    previous is None for a brand-new entry: nothing to release, so no stamp.
    An unrelated save must not lose a stamp that has not been consumed yet.
    """
    if previous is None:
        draft["strategy_changed_at"] = None
    elif previous["settings"].get("strategy") != draft["settings"]["strategy"]:
        draft["strategy_changed_at"] = now.isoformat()
    else:
        draft["strategy_changed_at"] = previous.get("strategy_changed_at")


def validate_configuration(
    config: dict, states: dict, now: datetime, *, sources: bool = True
) -> dict:
    """Resolve and bound settings before accepting a flow or runtime snapshot."""
    if not re.fullmatch("[A-Z]{3}", config.get("currency", "")):
        raise InputError(
            "currency must be a three-letter uppercase code; conversion is not supported"
        )
    try:
        ZoneInfo(config["timezone"])
    except (KeyError, ZoneInfoNotFoundError) as err:
        raise InputError("invalid timezone") from err
    values = dict(config["settings"])
    for prefix in ("", "bms_"):
        if config.get("soc_options", {}).get(
            prefix + "timestamp_policy", "auto"
        ) not in (
            "auto",
            "exact_path",
        ):
            raise InputError("invalid SOC timestamp policy")
    for key, (_, default, low, high, unit) in NUMBERS.items():
        if key in config.get("helpers", {}):
            selected = NumericSetting.from_dict(config["helpers"][key])
            expected_unit = unit.replace("currency", config["currency"])
            if "currency" in unit and (
                selected.unit != expected_unit
                or (selected.source_unit or selected.unit).split("/")[0]
                != config["currency"]
            ):
                raise InputError(
                    "helper currency must match the installation; conversion is not supported"
                )
            value = resolve_numeric(selected, states, now)
        else:
            if key == "maximum_grid_charge_price" and isinstance(values.get(key), bool):
                raise InputError("maximum_grid_charge_price must be numeric")
            value = finite(values.get(key, default), key)
        if not low <= value <= high:
            raise InputError(f"{key} outside supported bounds [{low}, {high}]")
        if key in INTEGERS:
            if value != int(value):
                raise InputError(f"{key} requires a whole number")
            value = int(value)
        values[key] = value
    for key, (_, default) in BOOLEANS.items():
        if type(values.get(key, default)) is not bool:
            raise InputError(f"{key} must be boolean")
        values.setdefault(key, default)
    for key, (_, choices) in CHOICES.items():
        values.setdefault(key, choices[0])
        if values[key] not in choices:
            raise InputError(f"invalid {key}")
    if (
        not values["hardware_floor"]
        <= values["operating_floor"]
        < values["soc_ceiling"]
    ):
        raise InputError(
            "operating floor must be at least hardware floor and below ceiling"
        )
    if (
        max(values["display_horizon_hours"], values["reference_horizon_hours"])
        > values["horizon_hours"]
    ):
        raise InputError(
            "outlook and reference horizons cannot exceed planning horizon"
        )
    if (
        values["display_interval_minutes"] not in (15, 30, 60)
        or max(values["display_horizon_hours"], values["reference_horizon_hours"])
        * 60
        / values["display_interval_minutes"]
        > 96
    ):
        raise InputError("unsupported display interval or more than 96 probes")
    if (
        values["boost_ceiling"] >= values["limit_floor"]
        or values["cheap_percentile"] > values["limit_percentile"]
    ):
        raise InputError("compass thresholds must be ordered")
    if values["solve_time_limit_s"] >= values["total_time_limit_s"]:
        raise InputError("total compute budget must exceed base solve budget")
    if sources:
        validate_sources(SourceConfig.from_dict(config["sources"]), set())
    return values
