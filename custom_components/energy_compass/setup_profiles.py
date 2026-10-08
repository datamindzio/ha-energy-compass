"""Setup profiles: one-shot, editable tuning pre-fills for new entries; never
tariff prices, never strategy-owned keys. REVISIONS is append-only: never edit
a published revision.
"""

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from math import isclose
from types import MappingProxyType

from .engine.strategy import STRATEGY_OWNED_KEYS
from .sources.tariffs import TariffSchedule

TARIFF_PRICE_KEYS: frozenset[str] = frozenset(
    {
        "buy_rate",
        "buy_off_peak_rate",
        "sell_rate",
        "buy_addition",
        "sell_addition",
        "monthly_charge",
    }
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
    buy_tariff: str | None = None


@dataclasses.dataclass(frozen=True)
class ProfileRevision:
    axes: Mapping[str, Mapping[str, SetupProfile]]
    raw_rce_presets: frozenset[str]
    raw_rce_sources: bool = False
    settled_sell_sources: bool = False


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


def _tariff_profile(key: str, label_en: str, label_pl: str) -> SetupProfile:
    return SetupProfile(
        labels=MappingProxyType({"en": label_en, "pl": label_pl}),
        currency="PLN",
        buy_tariff=key,
    )


_GENERIC_BUY_TARIFF = SetupProfile(
    labels=MappingProxyType(
        {
            "en": "None (keep a fixed buy rate)",
            "pl": "Brak (stała cena zakupu)",
        }
    ),
)

_REVISION_2 = ProfileRevision(
    axes=MappingProxyType(
        {
            "settlement": _REVISION_1.axes["settlement"],
            "buy_tariff": MappingProxyType(
                {
                    "generic": _GENERIC_BUY_TARIFF,
                    "g11": _tariff_profile(
                        "g11", "G11 (any operator)", "G11 (dowolny operator)"
                    ),
                    "pge_g12": _tariff_profile(
                        "pge_g12", "PGE Dystrybucja G12", "PGE Dystrybucja G12"
                    ),
                    "pge_g12w": _tariff_profile(
                        "pge_g12w", "PGE Dystrybucja G12w", "PGE Dystrybucja G12w"
                    ),
                    "tauron_g12": _tariff_profile(
                        "tauron_g12", "Tauron Dystrybucja G12", "Tauron Dystrybucja G12"
                    ),
                    "tauron_g12w": _tariff_profile(
                        "tauron_g12w",
                        "Tauron Dystrybucja G12w",
                        "Tauron Dystrybucja G12w",
                    ),
                    "enea_g12": _tariff_profile(
                        "enea_g12", "Enea Operator G12", "Enea Operator G12"
                    ),
                    "enea_g12w": _tariff_profile(
                        "enea_g12w", "Enea Operator G12w", "Enea Operator G12w"
                    ),
                    "energa_g12": _tariff_profile(
                        "energa_g12", "Energa-Operator G12", "Energa-Operator G12"
                    ),
                    "energa_g12w": _tariff_profile(
                        "energa_g12w", "Energa-Operator G12w", "Energa-Operator G12w"
                    ),
                    "stoen_g12": _tariff_profile(
                        "stoen_g12", "Stoen Operator G12", "Stoen Operator G12"
                    ),
                    "stoen_g12w": _tariff_profile(
                        "stoen_g12w", "Stoen Operator G12w", "Stoen Operator G12w"
                    ),
                }
            ),
            "inverter": _REVISION_1.axes["inverter"],
        }
    ),
    raw_rce_presets=frozenset({"pse", "pse_solcast"}),
    raw_rce_sources=True,
)

_REVISION_3 = ProfileRevision(
    axes=_REVISION_2.axes,
    raw_rce_presets=_REVISION_2.raw_rce_presets,
    raw_rce_sources=True,
    settled_sell_sources=True,
)

REVISIONS: Mapping[int, ProfileRevision] = MappingProxyType(
    {1: _REVISION_1, 2: _REVISION_2, 3: _REVISION_3}
)
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
    raw_rce_sell: bool = False,
    settled_sell: bool = False,
) -> tuple[Assignment, ...]:
    """Resolve the ordered assignments for one selection under one revision.

    raw_rce_sell says the draft's sell source is a floored raw RCE forecast;
    only revisions with raw_rce_sources honour it, alongside the preset gate.
    settled_sell says the sell source already applies its own multiplier; only
    revisions with settled_sell_sources then withhold the raw RCE multiplier.
    """
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
            and (
                preset in rev.raw_rce_presets or (rev.raw_rce_sources and raw_rce_sell)
            )
            and not (rev.settled_sell_sources and settled_sell)
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


def _settled_sell_binding(
    price: Mapping[str, object], states: Mapping[str, Mapping[str, object]]
) -> tuple[str, str] | None:
    if price.get("mode") != "forecast":
        return None
    for binding in price.get("forecast") or ():
        entity_id = binding["entity"]["entity_id"]
        attr = states.get(entity_id, {}).get("attributes", {}).get("settlement")
        if isinstance(attr, str) and "multiplier" in attr:
            return entity_id, attr
    return None


def is_settled_sell(
    price: Mapping[str, object], states: Mapping[str, Mapping[str, object]]
) -> bool:
    """True when a forecast sell entity already applies its own multiplier."""
    return _settled_sell_binding(price, states) is not None


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


def buy_tariff_schedule(
    selections: Mapping[str, str], *, revision: int = SETUP_PROFILES_REVISION
) -> dict | None:
    """Default tariff schedule dict for a buy_tariff selection; None for generic."""
    profiles = REVISIONS[revision].axes.get("buy_tariff")
    if profiles is None:
        return None
    profile = profiles[selections.get("buy_tariff", "generic")]
    if profile.buy_tariff is None:
        return None
    return TariffSchedule.default(profile.buy_tariff).to_dict()


def currency_error(selections: Mapping[str, str], currency: str) -> str | None:
    """Reject a non-PLN currency for any Polish profile, naming the axis."""
    for axis, profiles in AXES.items():
        profile = profiles.get(selections.get(axis, "generic"))
        if (
            profile is not None
            and profile.currency is not None
            and profile.currency != currency
        ):
            return f"{axis}_currency"
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
    """Build the persisted record: the revision and one profile key per axis."""
    return {
        "revision": SETUP_PROFILES_REVISION,
        **{axis: selections.get(axis, "generic") for axis in AXES},
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
    rev = REVISIONS.get(revision, CURRENT)
    axis_order = list(rev.axes)
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
    tariff_lines = []
    if "buy_tariff" in labels and record.get("buy_tariff", "generic") != "generic":
        tariff_lines.append(
            f"Buy source pre-selected by buy_tariff {labels['buy_tariff']}: "
            "tariff schedule; rates are entered by you (Tariffs → Values)."
        )
    if not assignments:
        return [f"Setup profiles: {summary}; no values pre-filled.", *tariff_lines]
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
    return [*lines, *tariff_lines]


def settlement_notes(
    config: Mapping[str, object],
    problem,
    values: Mapping[str, object],
    states: Mapping[str, Mapping[str, object]],
) -> list[str]:
    """Build Preview settlement notes N1-N4 (§1.8). DD-18: [] without a record."""
    record = config.get("setup_profiles")
    if not record:
        return []
    settlement = record.get("settlement", "generic")
    currency = config["currency"]
    sell = config["sources"]["sell"]
    forecast_bindings = (
        sell.get("forecast", []) if sell.get("mode") == "forecast" else []
    )
    notes: list[str] = []

    if (
        settlement.startswith("pl_")
        and currency == "PLN"
        and not values["limit_export_to_pv"]
    ):
        notes.append(
            "Note: Sell only PV is off (setting or strategy "
            f"{values['strategy']}); a Polish prosumer may only sell energy "
            "from their own PV."
        )

    sell_multiplier = values.get("sell_multiplier")
    if forecast_bindings and sell_multiplier is not None and sell_multiplier != 1:
        settled = _settled_sell_binding(sell, states)
        if settled is not None:
            entity_id, attr = settled
            notes.append(
                f"Note: sell source {entity_id} already applies its "
                f"settlement ({attr}); sell multiplier {sell_multiplier:g} "
                "applies a multiplier again — set it to 1."
            )

    if (
        settlement == "pl_net_billing"
        and currency == "PLN"
        and any(binding["unit"] == "PLN/MWh" for binding in forecast_bindings)
    ):
        if sell.get("floor_per_kwh") is None:
            text = (
                "Note: raw RCE sell prices are not floored at 0 (net-billing "
                "values negative prices at 0)"
            )
            if sell_multiplier != 1.23:
                text += (
                    " and the 1.23 deposit multiplier is not applied (sell "
                    f"multiplier {sell_multiplier:g})"
                )
            text += (
                "; Sell source → RCE market price (PSE) or "
                "examples/rce-sell-price.yaml gives max(RCE, 0) × 1.23."
            )
            notes.append(text)
        elif sell_multiplier != 1.23:
            notes.append(
                "Note: the 1.23 deposit multiplier is not applied to the RCE "
                f"sell price (sell multiplier {sell_multiplier:g}); net-billing "
                "values exported energy at max(RCE, 0) × 1.23."
            )

    rev = REVISIONS.get(record["revision"], CURRENT)
    profile = rev.axes["settlement"].get(settlement)
    ratio = profile.sell_ratio if profile is not None else None
    if ratio is not None and currency == "PLN":
        mismatch = None
        for slot in problem.slots:
            expected = ratio * slot.buy_per_kwh
            tolerance = 1e-6 * max(1.0, abs(slot.buy_per_kwh))
            if not isclose(slot.sell_per_kwh, expected, rel_tol=0, abs_tol=tolerance):
                mismatch = slot
                break
        if mismatch is not None:
            notes.append(
                "Note: net-metering sell is a copy, not linked to buy: first "
                f"mismatch {mismatch.start.isoformat()} sell "
                f"{mismatch.sell_per_kwh:g} ≠ {ratio:g} × buy "
                f"{mismatch.buy_per_kwh:g} {currency}/kWh. Bind sell to the "
                f"buy source and set sell multiplier = {ratio:g} × buy "
                f"multiplier, sell addition = {ratio:g} × buy addition, same "
                "VAT setting."
            )
        else:
            notes.append(
                f"Note: net-metering sell = {ratio:g} × buy in every "
                "previewed interval; this is a copy, later buy edits do not "
                "update sell."
            )

    return notes
