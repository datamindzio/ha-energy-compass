"""Setup profiles: one-shot, editable tuning pre-fills for new entries; never
tariff prices, never strategy-owned keys. REVISIONS is append-only: never edit
a published revision.
"""

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType

from .engine.strategy import STRATEGY_OWNED_KEYS

TARIFF_PRICE_KEYS: frozenset[str] = frozenset(
    {"buy_rate", "sell_rate", "buy_addition", "sell_addition", "monthly_charge"}
)
PRESET_OWNED_KEYS: frozenset[str] = frozenset({"boost_ceiling", "limit_floor"})
FORBIDDEN_KEYS: frozenset[str] = (
    frozenset(STRATEGY_OWNED_KEYS) | TARIFF_PRICE_KEYS | PRESET_OWNED_KEYS
)


@dataclasses.dataclass(frozen=True)
class SetupProfile:
    labels: Mapping[str, str]
    settings: Mapping[str, object] = MappingProxyType({})
    currency: str | None = None
    sell_ratio: float | None = None
    raw_rce_sell_multiplier: float | None = None


@dataclasses.dataclass(frozen=True)
class ProfileRevision:
    axes: Mapping[str, Mapping[str, SetupProfile]]
    raw_rce_presets: frozenset[str]


@dataclasses.dataclass(frozen=True)
class Assignment:
    axis: str
    profile: str
    key: str
    value: object


_GENERIC_SETTLEMENT = SetupProfile(
    labels=MappingProxyType(
        {
            "en": "Generic (no settlement assumptions)",
            "pl": "Ogólne (bez założeń rozliczenia)",
        }
    ),
)
_PL_NET_BILLING = SetupProfile(
    labels=MappingProxyType(
        {
            "en": "Poland: net-billing (RCE deposit)",
            "pl": "Polska: net-billing (depozyt RCE)",
        }
    ),
    settings=MappingProxyType(
        {
            "import_penalty_per_kwh": 0.20,
            "terminal_mode": "value",
            "terminal_value_per_kwh": 0.60,
        }
    ),
    currency="PLN",
    raw_rce_sell_multiplier=1.23,
)
_PL_NET_METERING_80 = SetupProfile(
    labels=MappingProxyType(
        {
            "en": "Poland: net-metering up to 10 kWp (80 %)",
            "pl": "Polska: system opustów do 10 kWp (80 %)",
        }
    ),
    currency="PLN",
    sell_ratio=0.8,
)
_PL_NET_METERING_70 = SetupProfile(
    labels=MappingProxyType(
        {
            "en": "Poland: net-metering above 10 kWp (70 %)",
            "pl": "Polska: system opustów powyżej 10 kWp (70 %)",
        }
    ),
    currency="PLN",
    sell_ratio=0.7,
)
_GENERIC_INVERTER = SetupProfile(
    labels=MappingProxyType(
        {
            "en": "Generic (no inverter defaults)",
            "pl": "Ogólny (bez ustawień falownika)",
        }
    ),
)
_DEYE_HYBRID = SetupProfile(
    labels=MappingProxyType(
        {
            "en": "Deye hybrid (Solarman)",
            "pl": "Deye hybrydowy (Solarman)",
        }
    ),
    settings=MappingProxyType({"idle_drain_kw": 0.13, "refresh_minutes": 60}),
)

_REVISION_1 = ProfileRevision(
    axes=MappingProxyType(
        {
            "settlement": MappingProxyType(
                {
                    "generic": _GENERIC_SETTLEMENT,
                    "pl_net_billing": _PL_NET_BILLING,
                    "pl_net_metering_80": _PL_NET_METERING_80,
                    "pl_net_metering_70": _PL_NET_METERING_70,
                }
            ),
            "inverter": MappingProxyType(
                {
                    "generic": _GENERIC_INVERTER,
                    "deye_hybrid": _DEYE_HYBRID,
                }
            ),
        }
    ),
    raw_rce_presets=frozenset({"pse", "pse_solcast"}),
)

REVISIONS: Mapping[int, ProfileRevision] = MappingProxyType({1: _REVISION_1})
SETUP_PROFILES_REVISION: int = max(REVISIONS)
CURRENT: ProfileRevision = REVISIONS[SETUP_PROFILES_REVISION]
AXES = CURRENT.axes
SETTLEMENT_PROFILES = CURRENT.axes["settlement"]
INVERTER_PROFILES = CURRENT.axes["inverter"]
RAW_RCE_PRESETS = CURRENT.raw_rce_presets


def profile_assignments(
    selections: Mapping[str, str],
    preset: str,
    *,
    revision: int = SETUP_PROFILES_REVISION,
) -> tuple[Assignment, ...]:
    """Resolve the ordered assignments for one selection under one revision."""
    rev = REVISIONS[revision]
    assignments: list[Assignment] = []
    for axis, profiles in rev.axes.items():
        profile_key = selections.get(axis, "generic")
        profile = profiles[profile_key]
        for key, value in profile.settings.items():
            assignments.append(Assignment(axis, profile_key, key, value))
        if profile.sell_ratio is not None:
            assignments.append(
                Assignment(axis, profile_key, "sell_multiplier", profile.sell_ratio)
            )
        if (
            profile.raw_rce_sell_multiplier is not None
            and preset in rev.raw_rce_presets
        ):
            assignments.append(
                Assignment(
                    axis,
                    profile_key,
                    "sell_multiplier",
                    profile.raw_rce_sell_multiplier,
                )
            )
    return tuple(assignments)


def apply_assignments(settings: dict, assignments: Iterable[Assignment]) -> None:
    """Set each assignment's value in place; no arithmetic, last write wins."""
    for assignment in assignments:
        settings[assignment.key] = assignment.value


def reconcile_assignments(
    settings: dict,
    previous: Iterable[Assignment],
    current: Iterable[Assignment],
    *,
    baseline: Mapping[str, object],
) -> None:
    """Re-resolve a preset change: move untouched pre-fills, keep edits.

    Deliberately no `helpers` input (DD-16): the stored shadow of a
    helper-bound key is reconciled like any other value; the binding, and so
    the effective value, is never seen or touched.
    """
    prev = {a.key: a.value for a in previous}
    cur = {a.key: a.value for a in current}
    for key in sorted(prev.keys() | cur.keys()):
        old = prev.get(key, baseline[key])
        new = cur.get(key, baseline[key])
        if old == new:
            continue
        if settings.get(key) == old:
            settings[key] = new


def currency_error(selections: Mapping[str, str], currency: str) -> str | None:
    """Reject a non-PLN currency for any Polish settlement profile."""
    profile = SETTLEMENT_PROFILES.get(selections.get("settlement", "generic"))
    if (
        profile is not None
        and profile.currency is not None
        and profile.currency != currency
    ):
        return "settlement_currency"
    return None


def profile_options(axis: str, language: str | None) -> list[dict[str, str]]:
    """List selector options for one axis in the current revision."""
    lang_key = "pl" if (language or "").startswith("pl") else "en"
    return [
        {"value": key, "label": profile.labels[lang_key]}
        for key, profile in AXES[axis].items()
    ]


def profile_label(
    axis: str, profile: str, language: str | None, *, revision: int
) -> str:
    """Resolve a display label; never raises, falls back to the raw key."""
    rev = REVISIONS.get(revision)
    if rev is None:
        return profile
    profiles = rev.axes.get(axis, {})
    spec = profiles.get(profile)
    if spec is None:
        return profile
    lang_key = "pl" if (language or "").startswith("pl") else "en"
    return spec.labels.get(lang_key, profile)


def selection_record(selections: Mapping[str, str]) -> dict:
    """Build the persisted {revision, settlement, inverter} record."""
    return {
        "revision": SETUP_PROFILES_REVISION,
        "settlement": selections.get("settlement", "generic"),
        "inverter": selections.get("inverter", "generic"),
    }


def _format_value(value: object) -> str:
    """Render a value the way Preview text does: on/off, :g numbers, strings verbatim."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def preview_lines(
    record: Mapping[str, object] | None,
    assignments: Sequence[Assignment],
    settings: Mapping[str, object],
    helpers: Mapping[str, Mapping[str, object]],
    values: Mapping[str, object],
    *,
    new_entry: bool,
) -> list[str]:
    """Build the Preview's setup-profile provenance block (§1.8). Pure."""
    if not record:
        return []
    revision = record["revision"]
    axis_order = list(AXES)
    labels = {
        axis: profile_label(axis, record.get(axis, "generic"), "en", revision=revision)
        for axis in axis_order
    }
    summary = ", ".join(f"{axis} {labels[axis]}" for axis in axis_order)
    if not new_entry:
        return [
            (
                f"Setup profiles at creation (revision {revision}, not "
                f"re-applied): {summary}."
            )
        ]
    if not assignments:
        return [f"Setup profiles: {summary}; no values pre-filled."]
    lines = [f"Setup profiles (applied once at creation; editable): {summary}."]
    by_axis: dict[str, list[Assignment]] = {}
    for assignment in assignments:
        by_axis.setdefault(assignment.axis, []).append(assignment)
    for axis in axis_order:
        axis_assignments = by_axis.get(axis)
        if not axis_assignments:
            continue
        pairs = ", ".join(f"{a.key} {_format_value(a.value)}" for a in axis_assignments)
        lines.append(f"Pre-filled by {axis} {labels[axis]}: {pairs}.")
    edited = []
    for assignment in assignments:
        key = assignment.key
        if key in helpers:
            entity_id = helpers[key]["entity"]["entity_id"]
            effective = _format_value(values[key])
            edited.append(
                f"{key} {_format_value(assignment.value)} → helper {entity_id} "
                f"({effective})"
            )
        elif settings.get(key) != assignment.value:
            edited.append(
                f"{key} {_format_value(assignment.value)} → "
                f"{_format_value(settings[key])}"
            )
    if edited:
        lines.append("Edited after pre-fill: " + ", ".join(edited) + ".")
    return lines
