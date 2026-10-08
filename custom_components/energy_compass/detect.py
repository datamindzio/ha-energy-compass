"""Pure source detection for new installations.

Candidates are identified by integration identity and Energy dashboard
preferences, never by entity id or name. Every candidate is validated with the
runtime parsers on the exact binding that would be stored.
"""

import math
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from .config_models import NumericSetting, SolarForecastBinding, resolve_numeric
from .engine.models import InputError
from .source_builders import (
    measurement_setting,
    rce_sell_binding,
    rce_unit_error,
    set_forecast_sell,
    set_load_statistic,
    set_pv_group,
    set_pv_solar_forecasts,
    set_rce_sell,
    set_soc,
    solcast_pv_binding,
    template_sell_binding,
)
from .source_management import _labels, source_error_detail
from .sources.bindings import (
    EntityBinding,
    parse_intervals,
    resolve_binding,
)
from .sources.solar_forecast import solar_forecast_intervals

ROWS: tuple[str, ...] = (
    "pv",
    "sell",
    "load",
    "soc",
    "bms_soc",
    "battery_power",
    "pv_power",
    "grid_import_power",
    "grid_export_power",
    "pv_energy",
    "grid_import_energy",
    "grid_export_energy",
    "pv_energy_today",
    "grid_export_energy_today",
)
NEVER_DETECTED: frozenset[str] = frozenset(
    {
        "buy",
        "throughput_today",
        "battery_energy",
        "battery_charge_power",
        "battery_discharge_power",
    }
)
PROVIDER_PLATFORMS: frozenset[str] = frozenset(
    {"solcast_solar", "rce_pse", "solarman", "template"}
)
MAX_AGE: Mapping[str, float] = {
    "battery_power": 3600,
    "pv_power": 86400,
    "grid_import_power": 3600,
    "grid_export_power": 3600,
    "pv_energy": 86400,
    "grid_import_energy": 86400,
    "grid_export_energy": 86400,
    "pv_energy_today": 86400,
    "grid_export_energy_today": 86400,
}
POWER_UNITS = ("W", "kW")
ENERGY_UNITS = ("kWh", "Wh")
BMS_ATTRIBUTE = "BMS SOC"
TEMPLATE_PREFIX = "energy_compass_"

_ECHO_PLATFORM = "energy_compass"
_PACK = re.compile(r"battery_\d+")


@dataclass(frozen=True)
class Signal:
    """One verified identity: where a candidate for a row comes from."""

    provider: str
    row: str
    tier: int
    kind: str
    translation_key: str | None = None
    prefs: str | None = None
    attribute: str | None = None
    sign: float = 1.0
    units: tuple[str, ...] = ()


SIGNALS: tuple[Signal, ...] = (
    Signal("energy_prefs", "pv", 1, "solar_forecast", prefs="solar_forecast"),
    Signal(
        "solcast",
        "pv",
        2,
        "entity_forecast",
        translation_key="total_kwh_forecast_today",
    ),
    Signal("template", "sell", 1, "template_sell"),
    Signal("rce_pse", "sell", 2, "rce", translation_key="rce_pse_today_price"),
    Signal(
        "solarman",
        "load",
        1,
        "statistic",
        translation_key="total_load_consumption",
        units=ENERGY_UNITS,
    ),
    Signal("energy_prefs", "soc", 1, "soc", prefs="battery_soc"),
    Signal("template", "soc", 2, "soc"),
    Signal("solarman", "soc", 3, "soc", translation_key="battery"),
    Signal(
        "solarman",
        "bms_soc",
        1,
        "soc",
        translation_key="battery",
        attribute=BMS_ATTRIBUTE,
    ),
    Signal("solarman", "bms_soc", 2, "soc", translation_key="battery_N"),
    Signal(
        "energy_prefs",
        "battery_power",
        1,
        "measurement",
        prefs="battery_power",
        units=POWER_UNITS,
    ),
    Signal(
        "solarman",
        "battery_power",
        2,
        "measurement",
        translation_key="battery_power",
        units=POWER_UNITS,
    ),
    Signal(
        "energy_prefs",
        "pv_power",
        1,
        "measurement",
        prefs="solar_power",
        units=POWER_UNITS,
    ),
    Signal(
        "solarman",
        "pv_power",
        2,
        "measurement",
        translation_key="pv_power",
        units=POWER_UNITS,
    ),
    Signal(
        "energy_prefs",
        "grid_import_power",
        1,
        "measurement",
        prefs="grid_power",
        units=POWER_UNITS,
    ),
    Signal(
        "energy_prefs",
        "grid_export_power",
        1,
        "measurement",
        prefs="grid_power",
        sign=-1.0,
        units=POWER_UNITS,
    ),
    Signal(
        "solarman",
        "grid_import_power",
        2,
        "measurement",
        translation_key="grid_power",
        units=POWER_UNITS,
    ),
    Signal(
        "solarman",
        "grid_export_power",
        2,
        "measurement",
        translation_key="grid_power",
        sign=-1.0,
        units=POWER_UNITS,
    ),
    Signal(
        "energy_prefs",
        "pv_energy",
        1,
        "measurement",
        prefs="solar_energy",
        units=ENERGY_UNITS,
    ),
    Signal(
        "solarman",
        "pv_energy",
        2,
        "measurement",
        translation_key="total_production",
        units=ENERGY_UNITS,
    ),
    Signal(
        "energy_prefs",
        "grid_import_energy",
        1,
        "measurement",
        prefs="grid_import",
        units=ENERGY_UNITS,
    ),
    Signal(
        "solarman",
        "grid_import_energy",
        2,
        "measurement",
        translation_key="total_energy_import",
        units=ENERGY_UNITS,
    ),
    Signal(
        "energy_prefs",
        "grid_export_energy",
        1,
        "measurement",
        prefs="grid_export",
        units=ENERGY_UNITS,
    ),
    Signal(
        "solarman",
        "grid_export_energy",
        2,
        "measurement",
        translation_key="total_energy_export",
        units=ENERGY_UNITS,
    ),
    Signal(
        "solarman",
        "pv_energy_today",
        1,
        "measurement",
        translation_key="today_production",
        units=ENERGY_UNITS,
    ),
    Signal(
        "solarman",
        "grid_export_energy_today",
        1,
        "measurement",
        translation_key="today_energy_export",
        units=ENERGY_UNITS,
    ),
)


@dataclass(frozen=True)
class EntityFacts:
    entity_id: str
    registry_id: str | None
    platform: str | None
    translation_key: str | None
    unique_id: str | None
    config_entry_id: str | None
    device_id: str | None
    disabled: bool
    device_class: str | None
    state: str | None
    attributes: Mapping[str, Any]
    last_updated: str | None
    last_reported: str | None


@dataclass(frozen=True)
class DeviceFacts:
    device_id: str
    manufacturer: str | None
    model: str | None
    config_entry_ids: frozenset[str]


@dataclass(frozen=True)
class SolarForecastFacts:
    config_entry_id: str
    domain: str
    title: str
    wh_hours: Mapping[str, Any] | None
    error: str | None


@dataclass(frozen=True)
class EnergyPrefsFacts:
    solar_energy: tuple[str, ...]
    solar_power: tuple[str, ...]
    solar_forecast_entries: tuple[str, ...]
    grid_import: tuple[str, ...]
    grid_export: tuple[str, ...]
    grid_power: tuple[str, ...]
    battery_power: tuple[str, ...]
    battery_soc: tuple[str, ...]


@dataclass(frozen=True)
class DetectionSnapshot:
    entities: Mapping[str, EntityFacts]
    devices: Mapping[str, DeviceFacts]
    energy: EnergyPrefsFacts | None
    solar_forecasts: Mapping[str, SolarForecastFacts]


@dataclass(frozen=True)
class DetectionContext:
    currency: str
    timezone: str
    pv_enabled: bool
    battery_enabled: bool
    now: datetime


@dataclass(frozen=True)
class Target:
    entity_id: str
    registry_id: str | None
    published: bool = True


@dataclass(frozen=True)
class Option:
    row: str
    provider: Literal["energy_prefs", "solcast", "rce_pse", "solarman", "template"]
    kind: Literal[
        "solar_forecast",
        "entity_forecast",
        "template_sell",
        "rce",
        "statistic",
        "soc",
        "measurement",
    ]
    tier: int
    targets: tuple[Target, ...] = ()
    forecasts: tuple[SolarForecastBinding, ...] = ()
    titles: tuple[str, ...] = ()
    attribute: str | None = None
    source_unit: str | None = None
    sign: float = 1.0
    counts: tuple[int | None, ...] = ()
    value: float | None = None
    settlement: str | None = None


@dataclass(frozen=True)
class Offer:
    row: str
    options: tuple[Option, ...]
    default: int | None


@dataclass(frozen=True)
class Unusable:
    row: str
    provider: str
    entity_ids: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class Detection:
    offers: tuple[Offer, ...]
    unusable: tuple[Unusable, ...]


def _ids(value: Any) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else [value]
    return [
        item for item in items if isinstance(item, str) and item and ":" not in item
    ]


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def normalize_energy_prefs(data: Mapping[str, Any] | None) -> EnergyPrefsFacts | None:
    """Reduce stored Energy preferences to the entity ids detection reads."""
    if not data or not isinstance(data, Mapping):
        return None
    fields: dict[str, list[str]] = {
        name: [] for name in EnergyPrefsFacts.__dataclass_fields__
    }

    def power(source: Mapping[str, Any]) -> list[str]:
        config = source.get("power_config")
        direct = _ids(source.get("stat_rate"))
        if direct:
            return direct
        if isinstance(config, Mapping):
            return _ids(config.get("stat_rate"))
        return []

    for source in data.get("energy_sources") or ():
        if not isinstance(source, Mapping):
            continue
        kind = source.get("type")
        if kind == "solar":
            fields["solar_energy"] += _ids(source.get("stat_energy_from"))
            fields["solar_power"] += power(source)
            for entry_id in source.get("config_entry_solar_forecast") or ():
                if isinstance(entry_id, str) and entry_id:
                    fields["solar_forecast_entries"].append(entry_id)
        elif kind == "grid":
            fields["grid_import"] += _ids(source.get("stat_energy_from"))
            fields["grid_export"] += _ids(source.get("stat_energy_to"))
            fields["grid_power"] += power(source)
        elif kind == "battery":
            fields["battery_power"] += power(source)
            fields["battery_soc"] += _ids(source.get("stat_soc"))
    return EnergyPrefsFacts(**{name: _dedupe(items) for name, items in fields.items()})


def snapshot_states(snapshot: DetectionSnapshot) -> dict[str, dict[str, Any]]:
    """State mapping in the shape the runtime parsers read."""
    states = {}
    for entity_id, facts in snapshot.entities.items():
        if facts.state is None:
            continue
        item: dict[str, Any] = {
            "state": facts.state,
            "attributes": dict(facts.attributes),
        }
        if facts.last_updated is not None:
            item["last_updated"] = facts.last_updated
        if facts.last_reported is not None:
            item["last_reported"] = facts.last_reported
        states[entity_id] = item
    return states


@dataclass(frozen=True)
class _Proposal:
    signal: Signal
    entity_ids: tuple[str, ...] = ()
    forecasts: tuple[SolarForecastBinding, ...] = ()
    titles: tuple[str, ...] = ()
    error: str | None = None


def _usable(facts: EntityFacts | None) -> bool:
    return (
        facts is not None
        and not facts.disabled
        and facts.entity_id.startswith("sensor.")
        and facts.platform != _ECHO_PLATFORM
    )


def _device_class(facts: EntityFacts) -> str | None:
    return facts.device_class or facts.attributes.get("device_class")


def _deye_entries(snapshot: DetectionSnapshot) -> frozenset[str]:
    entries: set[str] = set()
    for device in snapshot.devices.values():
        if (device.manufacturer or "").casefold() == "deye":
            entries |= device.config_entry_ids
    return frozenset(entries)


def _sorted_entities(snapshot: DetectionSnapshot) -> list[EntityFacts]:
    return sorted(snapshot.entities.values(), key=lambda facts: facts.entity_id)


def _signals(row: str, provider: str) -> list[Signal]:
    return [s for s in SIGNALS if s.row == row and s.provider == provider]


def _continuation(
    snapshot: DetectionSnapshot, first: EntityFacts, platform: str, key: str
) -> EntityFacts | None:
    for facts in _sorted_entities(snapshot):
        if (
            _usable(facts)
            and facts.platform == platform
            and facts.translation_key == key
            and facts.config_entry_id == first.config_entry_id
        ):
            return facts
    return None


def _proposals(
    snapshot: DetectionSnapshot, context: DetectionContext
) -> list[_Proposal]:
    result: list[_Proposal] = []
    deye = _deye_entries(snapshot)
    packs = []
    for facts in _sorted_entities(snapshot):
        if not _usable(facts):
            continue
        key = facts.translation_key
        if facts.platform == "solcast_solar" and key == "total_kwh_forecast_today":
            tomorrow = _continuation(
                snapshot, facts, "solcast_solar", "total_kwh_forecast_tomorrow"
            )
            ids = (facts.entity_id, *((tomorrow.entity_id,) if tomorrow else ()))
            result.append(_Proposal(_signals("pv", "solcast")[0], ids))
        elif facts.platform == "rce_pse" and key == "rce_pse_today_price":
            tomorrow = _continuation(
                snapshot, facts, "rce_pse", "rce_pse_tomorrow_price"
            )
            ids = (facts.entity_id, *((tomorrow.entity_id,) if tomorrow else ()))
            result.append(_Proposal(_signals("sell", "rce_pse")[0], ids))
        elif facts.platform == "template":
            attributes = facts.attributes
            settlement = attributes.get("settlement")
            if (
                (facts.unique_id or "").startswith(TEMPLATE_PREFIX)
                and isinstance(settlement, str)
                and "multiplier" in settlement
                and "prices" in attributes
            ):
                result.append(
                    _Proposal(_signals("sell", "template")[0], (facts.entity_id,))
                )
            if (
                _device_class(facts) == "battery"
                and attributes.get("unit_of_measurement") == "%"
            ):
                result.append(
                    _Proposal(_signals("soc", "template")[0], (facts.entity_id,))
                )
        elif facts.platform == "solarman" and facts.config_entry_id in deye:
            if key and _PACK.fullmatch(key):
                packs.append(facts)
                continue
            for signal in SIGNALS:
                if signal.provider == "solarman" and signal.translation_key == key:
                    result.append(_Proposal(signal, (facts.entity_id,)))
    if len(packs) == 1:
        result.append(
            _Proposal(_signals("bms_soc", "solarman")[1], (packs[0].entity_id,))
        )
    energy = snapshot.energy
    if energy is not None:
        if energy.solar_forecast_entries:
            forecasts, titles, error = [], [], None
            for entry_id in energy.solar_forecast_entries:
                known = snapshot.solar_forecasts.get(entry_id)
                if known is None:
                    error = error or "Energy solar forecast entry missing"
                    titles.append(entry_id)
                    continue
                forecasts.append(SolarForecastBinding(entry_id, known.domain))
                titles.append(known.title)
                error = error or known.error
            result.append(
                _Proposal(
                    _signals("pv", "energy_prefs")[0],
                    (),
                    tuple(forecasts),
                    tuple(titles),
                    error,
                )
            )
        for signal in SIGNALS:
            if signal.provider != "energy_prefs" or signal.prefs == "solar_forecast":
                continue
            for entity_id in getattr(energy, signal.prefs):
                facts = snapshot.entities.get(entity_id)
                if facts is not None and not _usable(facts):
                    continue
                result.append(
                    _Proposal(
                        signal,
                        (entity_id,),
                        error=None if facts else "missing entity",
                    )
                )
    return result


def _gated(row: str, context: DetectionContext) -> bool:
    if row == "sell":
        return context.currency != "PLN"
    if row in ("pv", "pv_power", "pv_energy"):
        return not context.pv_enabled
    if row in ("soc", "bms_soc", "battery_power"):
        return not context.battery_enabled
    return False


def _target(facts: EntityFacts, published: bool = True) -> Target:
    return Target(facts.entity_id, facts.registry_id, published)


def _published(states: Mapping[str, Any], binding: Any) -> bool:
    state = states.get(binding.entity.entity_id)
    raw = (
        None
        if state is None
        else state.get("attributes", {}).get(binding.entity.attribute)
        if binding.entity.attribute
        else state.get("state")
    )
    return not (
        state is None
        or state.get("state") in (None, "unknown", "unavailable")
        or raw is None
        or isinstance(raw, (list, tuple, dict))
        and not raw
    )


def _forecast_option(
    proposal: _Proposal,
    snapshot: DetectionSnapshot,
    context: DetectionContext,
    states: Mapping[str, Any],
) -> Option:
    signal = proposal.signal
    facts = [snapshot.entities[eid] for eid in proposal.entity_ids]
    bindings = []
    for item in facts:
        entity = EntityBinding(item.entity_id, item.registry_id, None)
        if signal.kind == "entity_forecast":
            bindings.append(solcast_pv_binding(entity, context.timezone))
        else:
            if message := rce_unit_error(item.attributes):
                raise InputError(message)
            bindings.append(rce_sell_binding(entity, context.timezone))
    counts = []
    targets = []
    for index, (item, binding) in enumerate(zip(facts, bindings, strict=True)):
        published = index == 0 or _published(states, binding)
        if published:
            count = len(parse_intervals(states, binding, context.now))
            if index == 0 and count == 0:
                raise InputError("empty forecast")
        else:
            count = None
        counts.append(count)
        targets.append(_target(item, published))
    return Option(
        signal.row,
        signal.provider,
        signal.kind,
        signal.tier,
        tuple(targets),
        counts=tuple(counts),
    )


def _template_sell_option(
    proposal: _Proposal,
    snapshot: DetectionSnapshot,
    context: DetectionContext,
    states: Mapping[str, Any],
) -> Option:
    item = snapshot.entities[proposal.entity_ids[0]]
    binding = template_sell_binding(EntityBinding(item.entity_id, item.registry_id))
    count = len(parse_intervals(states, binding, context.now))
    if count == 0:
        raise InputError("empty forecast")
    signal = proposal.signal
    return Option(
        signal.row,
        signal.provider,
        signal.kind,
        signal.tier,
        (_target(item),),
        counts=(count,),
        settlement=str(item.attributes.get("settlement")),
    )


def _solar_forecast_option(
    proposal: _Proposal, context: DetectionContext, wh: Mapping[str, Mapping]
) -> Option:
    if proposal.error:
        raise InputError(proposal.error)
    counts = [
        len(
            solar_forecast_intervals(
                wh[binding.config_entry_id], context.now, context.timezone
            )
        )
        for binding in proposal.forecasts
    ]
    signal = proposal.signal
    return Option(
        signal.row,
        signal.provider,
        signal.kind,
        signal.tier,
        forecasts=proposal.forecasts,
        titles=proposal.titles,
        counts=tuple(counts),
    )


def _statistic_option(proposal: _Proposal, snapshot: DetectionSnapshot) -> Option:
    item = snapshot.entities[proposal.entity_ids[0]]
    if item.state is None:
        raise InputError("missing entity")
    unit = item.attributes.get("unit_of_measurement")
    if item.attributes.get("state_class") not in ("total", "total_increasing"):
        raise InputError("load statistic needs a cumulative state class")
    if unit not in ENERGY_UNITS:
        raise InputError(f"unsupported unit {unit}")
    signal = proposal.signal
    return Option(
        signal.row,
        signal.provider,
        signal.kind,
        signal.tier,
        (_target(item),),
        source_unit=unit,
    )


def _soc_option(
    proposal: _Proposal, snapshot: DetectionSnapshot, states: Mapping[str, Any]
) -> Option:
    item = snapshot.entities[proposal.entity_ids[0]]
    signal = proposal.signal
    binding = EntityBinding(item.entity_id, item.registry_id, signal.attribute)
    raw = resolve_binding(states, binding)
    value = float(raw)
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise InputError("SOC outside 0-100")
    unit = "%"
    if signal.attribute is None:
        unit = item.attributes.get("unit_of_measurement")
        if unit != "%":
            raise InputError(f"unsupported unit {unit}")
    return Option(
        signal.row,
        signal.provider,
        signal.kind,
        signal.tier,
        (_target(item),),
        attribute=signal.attribute,
        source_unit=unit,
        value=value,
    )


def _measurement_option(
    proposal: _Proposal,
    snapshot: DetectionSnapshot,
    context: DetectionContext,
    states: Mapping[str, Any],
) -> Option:
    signal = proposal.signal
    item = snapshot.entities[proposal.entity_ids[0]]
    unit = item.attributes.get("unit_of_measurement")
    if unit not in signal.units:
        raise InputError(f"unsupported unit {unit}")
    setting = NumericSetting.from_dict(
        measurement_setting(
            signal.row,
            EntityBinding(item.entity_id, item.registry_id),
            source_unit=unit,
            sign=signal.sign,
            max_age_seconds=None,
        )
    )
    resolve_numeric(setting, states, context.now)
    return Option(
        signal.row,
        signal.provider,
        signal.kind,
        signal.tier,
        (_target(item),),
        source_unit=unit,
        sign=signal.sign,
        value=float(item.state),
    )


def _validate(
    proposal: _Proposal,
    snapshot: DetectionSnapshot,
    context: DetectionContext,
    states: Mapping[str, Any],
    wh: Mapping[str, Mapping],
) -> Option:
    if proposal.error and proposal.signal.kind != "solar_forecast":
        raise InputError(proposal.error)
    if any(eid not in snapshot.entities for eid in proposal.entity_ids):
        raise InputError("missing entity")
    kind = proposal.signal.kind
    if kind == "solar_forecast":
        return _solar_forecast_option(proposal, context, wh)
    if kind in ("entity_forecast", "rce"):
        return _forecast_option(proposal, snapshot, context, states)
    if kind == "template_sell":
        return _template_sell_option(proposal, snapshot, context, states)
    if kind == "statistic":
        return _statistic_option(proposal, snapshot)
    if kind == "soc":
        return _soc_option(proposal, snapshot, states)
    return _measurement_option(proposal, snapshot, context, states)


def _identity(option: Option) -> tuple:
    return (
        option.row,
        option.kind,
        tuple(target.entity_id for target in option.targets),
        tuple(item.config_entry_id for item in option.forecasts),
        option.attribute,
        option.sign,
    )


def _order(option: Option) -> tuple:
    first = option.targets[0].entity_id if option.targets else ""
    return (option.tier, first, tuple(i.config_entry_id for i in option.forecasts))


def _anchored(option: Option, snapshot: DetectionSnapshot, anchor: str) -> bool:
    return bool(option.targets) and all(
        snapshot.entities[target.entity_id].config_entry_id == anchor
        for target in option.targets
    )


def detect(snapshot: DetectionSnapshot, context: DetectionContext) -> Detection:
    """Rank validated candidates per row and choose a default only when sure."""
    states = snapshot_states(snapshot)
    deye = _deye_entries(snapshot)
    anchor = next(iter(deye)) if len(deye) == 1 else None
    wh = {
        entry_id: facts.wh_hours
        for entry_id, facts in snapshot.solar_forecasts.items()
        if facts.wh_hours is not None
    }
    usable: dict[str, dict[tuple, Option]] = {row: {} for row in ROWS}
    unusable: dict[str, dict[tuple, Unusable]] = {row: {} for row in ROWS}
    for proposal in _proposals(snapshot, context):
        row = proposal.signal.row
        if _gated(row, context):
            continue
        key = (
            proposal.signal.kind,
            proposal.entity_ids,
            proposal.signal.attribute,
            proposal.signal.sign,
            tuple(item.config_entry_id for item in proposal.forecasts),
        )
        try:
            option = _validate(proposal, snapshot, context, states, wh)
        except (InputError, ValueError, TypeError, KeyError) as err:
            names = proposal.entity_ids or tuple(
                item.domain for item in proposal.forecasts
            )
            unusable[row].setdefault(
                key, Unusable(row, proposal.signal.provider, names, str(err))
            )
            continue
        identity = _identity(option)
        kept = usable[row].get(identity)
        if kept is None or option.tier < kept.tier:
            usable[row][identity] = option
    offers = []
    notes = []
    for row in ROWS:
        options = tuple(sorted(usable[row].values(), key=_order))
        shown = {
            tuple(target.entity_id for target in option.targets) for option in options
        }
        notes.extend(
            item for item in unusable[row].values() if item.entity_ids not in shown
        )
        if not options:
            continue
        first = options[0].tier
        group = [index for index, o in enumerate(options) if o.tier == first]
        default = None
        if row != "bms_soc":
            if len(group) == 1:
                default = group[0]
            elif anchor is not None:
                matches = [
                    index
                    for index in group
                    if _anchored(options[index], snapshot, anchor)
                ]
                default = matches[0] if len(matches) == 1 else None
        offers.append(Offer(row, options, default))
    return Detection(tuple(offers), tuple(notes))


def _entity(option: Option, index: int, attribute: str | None = None) -> EntityBinding:
    target = option.targets[index]
    return EntityBinding(target.entity_id, target.registry_id, attribute)


def apply_detection(draft: Mapping[str, Any], chosen: Iterable[Option]) -> dict:
    """Write the chosen options into a copy of the draft through the shared builders."""
    result = deepcopy(dict(draft))
    timezone = result["timezone"]
    for option in chosen:
        if option.row not in ROWS:
            raise ValueError(f"row {option.row} is never detected")

        if option.kind == "solar_forecast":
            set_pv_solar_forecasts(result, option.forecasts)
        elif option.kind == "entity_forecast":
            set_pv_group(
                result,
                0,
                [
                    solcast_pv_binding(_entity(option, index), timezone)
                    for index in range(len(option.targets))
                ],
            )
        elif option.kind == "template_sell":
            set_forecast_sell(result, [template_sell_binding(_entity(option, 0))])
        elif option.kind == "rce":
            set_rce_sell(
                result,
                [
                    rce_sell_binding(_entity(option, index), timezone)
                    for index in range(len(option.targets))
                ],
            )
        elif option.kind == "statistic":
            set_load_statistic(
                result, option.targets[0].entity_id, option.source_unit or "kWh", 1
            )
        elif option.kind == "soc":
            set_soc(
                result,
                _entity(option, 0, option.attribute),
                bms=option.row == "bms_soc",
            )
        else:
            result["measurements"][option.row] = measurement_setting(
                option.row,
                _entity(option, 0),
                source_unit=option.source_unit or "",
                sign=option.sign,
                max_age_seconds=MAX_AGE[option.row],
            )
    return result


_PROVIDERS = {
    "en": {
        "energy_prefs": "Energy dashboard",
        "solcast": "Solcast",
        "rce_pse": "RCE PSE",
        "solarman": "Solarman (Deye)",
        "template": "Template",
    },
    "pl": {
        "energy_prefs": "Panel Energia",
        "solcast": "Solcast",
        "rce_pse": "RCE PSE",
        "solarman": "Solarman (Deye)",
        "template": "Szablon",
    },
}
_ROW_LABELS = {
    "en": {
        "pv": "PV forecast",
        "sell": "Sell price",
        "load": "Household load history",
        "soc": "Battery SOC",
        "bms_soc": "BMS SOC (agreement check)",
    },
    "pl": {
        "pv": "Prognoza PV",
        "sell": "Cena sprzedaży",
        "load": "Historia zużycia domu",
        "soc": "SOC baterii",
        "bms_soc": "SOC z BMS (kontrola zgodności)",
    },
}
SKIP = {"en": "Do not bind", "pl": "Nie wiąż"}
_TEXT = {
    "en": {
        "tomorrow_pending": "tomorrow: not yet published",
        "forecast": "{provider} — {names}: {n} hourly values ahead",
        "dual": "{provider} — {first} (today: {n0} intervals)",
        "tomorrow": " + {second} (tomorrow: {n1} intervals)",
        "tomorrow_unpublished": " + {second} (tomorrow: not yet published)",
        "template": "{provider} — {id} ({n} intervals; settlement {settlement}; "
        "sell multiplier set to 1)",
        "statistic": "{provider} — {id} (recorder statistic, {unit})",
        "attribute": " attribute {attribute}",
        "now": " (now {value:g} {unit}{sign})",
        "sign": "; sign −1",
        "ticked": "{label}: {detail}",
        "unticked": "{label}: {detail} — not ticked, see the note below",
        "preselected": "{label}: {chosen} (preselected); also: {others}",
        "none": "{label}: several candidates, none preselected: {others}",
        "bms": "BMS SOC is offered unticked. With imbalanced battery cells the "
        "BMS SOC and the battery SOC can differ by more than Battery → Soc "
        "disagreement percent (default 5 %), and then every plan is blocked. "
        "Tick it only if both agree over a full day.",
        "unusable": "{label}: {provider} {ids} is not usable now ({detail}); "
        "set it in Sources if needed.",
    },
    "pl": {
        "forecast": "{provider} — {names}: {n} wartości godzinowych naprzód",
        "dual": "{provider} — {first} (dziś: {n0} przedziałów)",
        "tomorrow": " + {second} (jutro: {n1} przedziałów)",
        "tomorrow_unpublished": " + {second} (jutro: jeszcze nieopublikowane)",
        "template": "{provider} — {id} ({n} przedziałów; rozliczenie "
        "{settlement}; mnożnik sprzedaży ustawiony na 1)",
        "statistic": "{provider} — {id} (statystyka rejestratora, {unit})",
        "attribute": " atrybut {attribute}",
        "now": " (teraz {value:g} {unit}{sign})",
        "sign": "; znak −1",
        "ticked": "{label}: {detail}",
        "unticked": "{label}: {detail} — niezaznaczone, zobacz uwagę poniżej",
        "preselected": "{label}: {chosen} (wybrane); także: {others}",
        "none": "{label}: kilku kandydatów, żaden nie jest wybrany: {others}",
        "bms": "SOC z BMS jest proponowany bez zaznaczenia. Przy "
        "niezbalansowanych ogniwach SOC z BMS i SOC baterii mogą się różnić o "
        "więcej niż Bateria → Tolerancja różnicy BMS (domyślnie 5 %), a wtedy "
        "każdy plan jest blokowany. Zaznacz tylko, jeśli oba są zgodne przez "
        "całą dobę.",
        "unusable": "{label}: {provider} {ids} jest teraz nieużywalne "
        "({detail}); w razie potrzeby ustaw w Źródłach danych.",
    },
}


def _lang(language: str | None) -> str:
    return "pl" if language and language.startswith("pl") else "en"


def row_label(row: str, language: str | None) -> str:
    """Form label of one detectable row."""
    lang = _lang(language)
    return _ROW_LABELS[lang].get(row) or _labels(language)[row]


def option_label(option: Option, language: str | None) -> str:
    """One-line description of a candidate, with provider and current values."""
    lang = _lang(language)
    text = _TEXT[lang]
    provider = _PROVIDERS[lang][option.provider]
    if option.kind == "solar_forecast":
        names = " + ".join(
            f"{title} ({binding.domain})"
            for title, binding in zip(option.titles, option.forecasts, strict=False)
        )
        horizon = min((count for count in option.counts if count), default=0)
        return text["forecast"].format(provider=provider, names=names, n=horizon)
    if option.kind in ("entity_forecast", "rce"):
        label = text["dual"].format(
            provider=provider,
            first=option.targets[0].entity_id,
            n0=option.counts[0],
        )
        if len(option.targets) > 1:
            second = option.targets[1]
            key = "tomorrow" if second.published else "tomorrow_unpublished"
            label += text[key].format(second=second.entity_id, n1=option.counts[1])
        return label
    if option.kind == "template_sell":
        return text["template"].format(
            provider=provider,
            id=option.targets[0].entity_id,
            n=option.counts[0],
            settlement=option.settlement,
        )
    if option.kind == "statistic":
        return text["statistic"].format(
            provider=provider, id=option.targets[0].entity_id, unit=option.source_unit
        )
    label = f"{provider} — {option.targets[0].entity_id}"
    if option.attribute:
        label += text["attribute"].format(attribute=option.attribute)
    return label + text["now"].format(
        value=option.value,
        unit=option.source_unit,
        sign=text["sign"] if option.sign < 0 else "",
    )


def detection_text(detection: Detection, language: str | None) -> tuple[str, str]:
    """The `detected` lines and the `notes` lines of the Detected sources form."""
    lang = _lang(language)
    text = _TEXT[lang]
    lines = []
    notes = []
    for offer in detection.offers:
        label = row_label(offer.row, language)
        details = [option_label(option, language) for option in offer.options]
        if len(details) == 1:
            key = "ticked" if offer.default == 0 else "unticked"
            lines.append(text[key].format(label=label, detail=details[0]))
        elif offer.default is not None:
            others = [d for i, d in enumerate(details) if i != offer.default]
            lines.append(
                text["preselected"].format(
                    label=label,
                    chosen=details[offer.default],
                    others="; ".join(others),
                )
            )
        else:
            lines.append(text["none"].format(label=label, others="; ".join(details)))
    if any(offer.row == "bms_soc" for offer in detection.offers):
        notes.append(text["bms"])
    for item in detection.unusable:
        notes.append(
            text["unusable"].format(
                label=row_label(item.row, language),
                provider=_PROVIDERS[lang][item.provider],
                ids=", ".join(item.entity_ids),
                detail=source_error_detail(item.detail, language),
            )
        )
    return (
        "\n".join(f"- {line}" for line in lines),
        "\n".join(f"- {note}" for note in notes),
    )
