"""Entities and limits of the native Deye controller, derived from the Solarman device.

No Home Assistant imports. The entities are found by entity-registry translation key
(the way `resolve_tou` finds the TOU programs); the limits are the options the user
changed, over defaults computed from Energy Compass settings and the entities' own
ranges. No household number is written in code.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from . import controller_profile as profile
from .controller import TouFact, is_number

READ_SERVICE = ("solarman", "read_holding_registers")
# Lowest pack voltage the controller accepts: the power cap in amperes at that voltage.
MIN_PACK_VOLTS = 400
ROLES: Mapping[str, tuple[str, str]] = {
    "charge": ("number", "battery_max_charging_current"),
    "discharge": ("number", "battery_max_discharging_current"),
    "grid": ("number", "battery_grid_charging_current"),
    "operation": ("select", "battery_operation_mode"),
    "soc": ("sensor", "battery"),
    "voltage": ("sensor", "battery_voltage"),
}
ENERGY_ROLE = ("sensor", "total_battery_discharge")
LIMIT_FIELDS = (
    "max_power_w",
    "max_current",
    "relinquish_current",
    "max_grid_current",
    "hold_grid_current",
    "reached_discharge_current",
    "min_buffer_current",
    "balance_grid_current",
    "voltage_grid_charge_ceiling",
    "commissioned_battery_modes",
    "old_writers",
    "telemetry_entities",
    "discharge_energy_entity",
    "grid_presence_entity",
    "grid_voltage_entities",
    "outage_enter_seconds",
    "outage_stable_minutes",
)


@dataclass(frozen=True)
class Resolved:
    entities: Mapping[str, str]
    missing: tuple[str, ...]
    energy: str


def resolve(
    facts: Iterable[TouFact], overrides: Mapping[str, str] | None = None
) -> Resolved:
    """The six controller entities of one device, and its discharge-energy counter.

    `overrides` maps a role to an entity id for Solarman versions that renamed a key.
    A role without exactly one enabled match (and no override) is reported missing.
    """
    overrides = overrides or {}
    by_key: dict[tuple[str, str], list[str]] = {}
    for fact in facts:
        if fact.platform == "solarman" and fact.translation_key and not fact.disabled:
            by_key.setdefault((fact.domain, fact.translation_key), []).append(
                fact.entity_id
            )
    found: dict[str, str] = {}
    missing = []
    for role, key in ROLES.items():
        matches = by_key.get(key, [])
        if override := overrides.get(role):
            found[role] = override
        elif len(matches) == 1:
            found[role] = matches[0]
        else:
            missing.append(role)
    counters = by_key.get(ENERGY_ROLE, [])
    energy = overrides.get("discharge_energy") or (
        counters[0] if len(counters) == 1 else ""
    )
    return Resolved(found, tuple(missing), energy)


def entities_of(resolved: Resolved, prefix: str) -> profile.Entities:
    e = resolved.entities
    return profile.Entities(
        charge=e["charge"],
        discharge=e["discharge"],
        grid=e["grid"],
        operation=e["operation"],
        soc=e["soc"],
        voltage=e["voltage"],
        prefix=prefix,
    )


def _current(
    options: Mapping[str, Any], key: str, attribute_max: Any, cap: float
) -> float:
    if is_number(options.get(key)):
        return float(options[key])
    if is_number(attribute_max):
        return min(float(attribute_max), cap)
    return cap


def derive_limits(
    options: Mapping[str, Any] | None,
    *,
    charge_kw: float,
    discharge_kw: float,
    charge_max: Any,
    grid_max: Any,
    resolved: Resolved,
) -> profile.Limits:
    """Installation limits: stored options over the defaults of the design (DD-7)."""
    options = options or {}
    power = (
        float(options["max_power_w"])
        if is_number(options.get("max_power_w"))
        else min(charge_kw, discharge_kw) * 1000
    )
    cap = power / MIN_PACK_VOLTS
    values: dict[str, Any] = {
        "max_power_w": power,
        "max_current": _current(options, "max_current", charge_max, cap),
        "max_grid_current": _current(options, "max_grid_current", grid_max, cap),
        "relinquish_current": _current(options, "relinquish_current", charge_max, cap),
    }
    for key in (
        "hold_grid_current",
        "reached_discharge_current",
        "min_buffer_current",
        "balance_grid_current",
        "voltage_grid_charge_ceiling",
    ):
        if is_number(options.get(key)):
            values[key] = float(options[key])
    if options.get("commissioned_battery_modes"):
        values["commissioned_battery_modes"] = tuple(
            options["commissioned_battery_modes"]
        )
    values["old_writers"] = tuple(options.get("old_writers") or ())
    values["telemetry_entities"] = tuple(
        options.get("telemetry_entities")
        or (resolved.entities.get("soc"), resolved.entities.get("voltage"))
    )
    values["discharge_energy_entity"] = options.get(
        "discharge_energy_entity", resolved.energy
    )
    return profile.Limits(**values)
