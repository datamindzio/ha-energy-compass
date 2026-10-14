"""Configuration of the counter SOC estimator: pure, no Home Assistant imports.

`sources.soc_mode` selects who supplies the SOC (`entity`, the default, or
`counter_estimator`); the rest lives in the top-level `soc_estimator` block, which
also configures the diagnostic-only shadow run beside a bound SOC entity.
"""

from collections.abc import Mapping
from math import isfinite

from .engine.models import InputError
from .soc_estimator import EstimatorSettings
from .sources.bindings import EntityBinding

COUNTER_ESTIMATOR = "counter_estimator"
SOC_MODES = ("entity", COUNTER_ESTIMATOR)
# The key under which the coordinator hands the estimate to the planner.
ESTIMATE_KEY = "soc_estimator"

REQUIRED_ROLES = ("charge_counter", "discharge_counter", "voltage", "current")
OPTIONAL_ROLES = (
    "power",
    "pv_power",
    "grid_power",
    "load_power",
    "charge_limit",
    "discharge_limit",
    "grid_charge_current",
    # Shown beside the estimate for comparison; never an input.
    "bms_soc",
)
ROLES = (*REQUIRED_ROLES, *OPTIONAL_ROLES)
# Roles that fall back to a measurement the installation already binds.
MEASUREMENT_FALLBACK = {"power": "battery_power", "pv_power": "pv_power"}

DEFAULT_ETA_CHARGE = 0.95
DEFAULT_FULL_HOLD_SECONDS = 3600
MIN_FULL_HOLD_SECONDS = 3600
DEFAULT_DRIFT_WARN_CYCLES = 3
DEFAULT_INITIAL_SOC_PERCENT = 50.0


def block(config: Mapping) -> dict:
    return config.get("soc_estimator") or {}


def counter_mode(config: Mapping) -> bool:
    """The planner and the controller read the estimator, not an entity."""
    sources = config.get("sources", {})
    return bool(
        sources.get("battery_enabled") and sources.get("soc_mode") == COUNTER_ESTIMATOR
    )


def shadow_mode(config: Mapping) -> bool:
    """The estimator runs as diagnostics only; the bound SOC entity still plans."""
    return (
        bool(config.get("sources", {}).get("battery_enabled"))
        and not counter_mode(config)
        and block(config).get("shadow") is True
    )


def estimator_active(config: Mapping) -> bool:
    return counter_mode(config) or shadow_mode(config)


def binding(config: Mapping, role: str) -> EntityBinding | None:
    """The entity of one role: the estimator's own, else the shared measurement."""
    own = (block(config).get("bindings") or {}).get(role)
    if own:
        return EntityBinding.from_dict(own)
    measurement = (config.get("measurements") or {}).get(MEASUREMENT_FALLBACK.get(role))
    if (
        measurement
        and measurement.get("entity")
        and not measurement.get("statistic_id")
    ):
        return EntityBinding.from_dict(measurement["entity"])
    return None


def entity_ids(config: Mapping) -> set[str]:
    return {
        found.entity_id
        for role in ROLES
        if (found := binding(config, role)) is not None
    }


def sign(config: Mapping, key: str) -> float:
    return -1.0 if block(config).get(key) == -1 else 1.0


def eta_charge(config: Mapping) -> float:
    return float(block(config).get("eta_charge", DEFAULT_ETA_CHARGE))


def settings(
    config: Mapping, *, rated_pv_w: float = 0.0, max_charge_current_a: float = 0.0
) -> EstimatorSettings:
    """Estimator settings; `InputError` when a required value is missing or invalid."""
    values = block(config)
    capacity = values.get("capacity_kwh") or config["settings"]["capacity_kwh"]
    empty = values.get("empty_voltage")
    if not _number(empty) or empty <= 0:
        raise InputError("the SOC estimator needs the pack voltage that is 0 %")
    full = values.get("full_min_voltage")
    if not _number(full) or full <= empty:
        raise InputError(
            "the SOC estimator needs the lowest pack voltage of a full pack,"
            " above the voltage that is 0 %"
        )
    configured_rated = values.get("rated_pv_w")
    configured_current = values.get("max_charge_current")
    export_kw = (config.get("settings") or {}).get("grid_export_kw")
    return EstimatorSettings(
        capacity_kwh=float(capacity),
        empty_voltage_v=float(empty),
        full_min_voltage_v=float(full),
        eta_charge=eta_charge(config),
        max_charge_current_a=float(
            configured_current if _number(configured_current) else max_charge_current_a
        ),
        rated_pv_w=float(configured_rated if _number(configured_rated) else rated_pv_w),
        export_limit_w=float(export_kw) * 1000 if _number(export_kw) else None,
        full_hold_seconds=float(
            values.get("full_hold_seconds", DEFAULT_FULL_HOLD_SECONDS)
        ),
        anchor_on_candidates=values.get("anchor_on_candidates") is True,
    )


def balance_lead_minutes(config: Mapping) -> float:
    """Minutes of full evidence a balance needs before its own hold starts.

    The balance tracker counts a full pack only once the estimator's full hold
    has passed, so the planner's window must cover that hold as well.
    """
    if not counter_mode(config):
        return 0.0
    return float(block(config).get("full_hold_seconds", DEFAULT_FULL_HOLD_SECONDS)) / 60


def drift_cycles(config: Mapping) -> float:
    return float(block(config).get("drift_warn_cycles", DEFAULT_DRIFT_WARN_CYCLES))


def validate(config: Mapping) -> None:
    """Reject an estimator configuration that cannot run."""
    sources = config.get("sources", {})
    mode = sources.get("soc_mode", "entity")
    if mode not in SOC_MODES:
        raise InputError("invalid SOC mode")
    if not estimator_active(config):
        if mode == COUNTER_ESTIMATOR:
            raise InputError("the counter SOC estimator needs the battery enabled")
        return
    if counter_mode(config) and (sources.get("soc") or sources.get("bms_soc")):
        raise InputError("the counter SOC estimator replaces the SOC sources")
    missing = [role for role in REQUIRED_ROLES if binding(config, role) is None]
    if missing:
        raise InputError(f"SOC estimator source missing: {', '.join(missing)}")
    settings(config)
    values = block(config)
    for key, low, high in (
        ("eta_charge", 0.01, 1.0),
        ("full_hold_seconds", MIN_FULL_HOLD_SECONDS, 86400),
        ("drift_warn_cycles", 0.5, 100),
    ):
        value = values.get(key)
        if value is not None and not (_number(value) and low <= value <= high):
            raise InputError(f"SOC estimator {key} outside [{low}, {high}]")
    for key in ("power_sign", "current_sign", "grid_sign"):
        if values.get(key, 1) not in (1, -1):
            raise InputError(f"SOC estimator {key} must be 1 or -1")
    capacity = values.get("capacity_kwh")
    if capacity is not None and not (_number(capacity) and capacity > 0):
        raise InputError("SOC estimator capacity must be positive")
    initial = values.get("initial_soc_percent")
    if initial is not None and not (_number(initial) and 0 <= initial <= 100):
        raise InputError("SOC estimator initial SOC outside [0, 100]")


def _number(value) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and isfinite(value)
    )
