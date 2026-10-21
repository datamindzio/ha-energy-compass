"""Bundled Polish tariff rates: the `tariff_rates.json` snapshot, loaded and validated.

Pure module, no Home Assistant imports. The snapshot is written by
`tools/build_tariff_rates.py` from the energy-compass data and only pre-fills
editable settings; the runtime never reads it. It is loaded once at import
(Home Assistant imports integrations in its executor, so no event-loop I/O).
"""

import json
import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

from .tariffs import (
    BAND_DISTRIBUTION_KEYS,
    BAND_RATE_KEYS,
    BANDS,
    CATALOG,
    Prefill,
    tariff_bands,
)

SNAPSHOT_FORMAT = 1
SNAPSHOT_PATH = Path(__file__).with_name("tariff_rates.json")
SUBSCRIPTION_MONTHS = frozenset({"1", "2", "3", "6", "12"})
OFFER_KINDS = frozenset({"ure", "fixed"})
_CHARGE_RATES = ("qualityPerKWh", "ozePerKWh", "cogenerationPerKWh")


class SnapshotError(ValueError):
    """The tariff rate snapshot is malformed."""


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Any]:
    """Parse a snapshot file without validating it."""
    return json.loads(path.read_text(encoding="utf-8"))


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SnapshotError(f"{where} must be a number")
    if not math.isfinite(value) or value < 0:
        raise SnapshotError(f"{where} must be finite and not negative")
    return float(value)


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise SnapshotError(f"{where} must be a non-empty string")
    return value


def _iso(value: Any, where: str) -> date:
    try:
        return date.fromisoformat(_text(value, where))
    except ValueError as err:
        raise SnapshotError(f"{where} must be an ISO date") from err


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SnapshotError(f"{where} must be an object")
    return value


def _edges(rows: Any, where: str) -> list[int | None]:
    if not isinstance(rows, list) or not rows:
        raise SnapshotError(f"{where} must be a non-empty list")
    edges: list[int | None] = []
    for index, row in enumerate(rows):
        row = _mapping(row, f"{where}[{index}]")
        _number(row.get("pln"), f"{where}[{index}].pln")
        edge = row.get("maxKWh")
        if edge is not None and (
            isinstance(edge, bool) or not isinstance(edge, int) or edge <= 0
        ):
            raise SnapshotError(f"{where}[{index}].maxKWh must be a positive integer")
        edges.append(edge)
    return edges


def _validate_charges(charges: Any, zones: set[str], where: str) -> None:
    charges = _mapping(charges, f"{where}.charges")
    variable = _mapping(charges.get("variablePerKWh"), f"{where}.variablePerKWh")
    if set(variable) != zones:
        raise SnapshotError(f"{where}.variablePerKWh must price exactly the zones")
    for zone, rate in variable.items():
        _number(rate, f"{where}.variablePerKWh.{zone}")
    for key in _CHARGE_RATES:
        _number(charges.get(key), f"{where}.{key}")
    fixed = _mapping(charges.get("fixedPerMonth"), f"{where}.fixedPerMonth")
    for phase in ("onePhase", "threePhase"):
        _number(fixed.get(phase), f"{where}.fixedPerMonth.{phase}")
    subscription = _mapping(
        charges.get("subscriptionPerMonth"), f"{where}.subscriptionPerMonth"
    )
    if "1" not in subscription or not set(subscription) <= SUBSCRIPTION_MONTHS:
        raise SnapshotError(
            f"{where}.subscriptionPerMonth keys must be in 1,2,3,6,12 with 1"
        )
    for months, pln in subscription.items():
        _number(pln, f"{where}.subscriptionPerMonth.{months}")
    capacity = _edges(charges.get("capacityPerMonth"), f"{where}.capacityPerMonth")
    finite = [edge for edge in capacity[:-1]]
    if capacity[-1] is not None or None in finite or finite != sorted(set(finite)):
        raise SnapshotError(
            f"{where}.capacityPerMonth must ascend and end with maxKWh null"
        )
    transitional = _edges(
        charges.get("transitionalPerMonth"), f"{where}.transitionalPerMonth"
    )
    if not set(transitional) <= set(capacity):
        raise SnapshotError(
            f"{where}.transitionalPerMonth edges must be capacity band edges"
        )


def catalog_key(operator_id: str, group: str) -> str:
    """Catalog key of a snapshot operator and group: `g11`, else `<operator>_<group>`."""
    return "g11" if group == "G11" else f"{operator_id}_{group.lower()}"


def _validate_group(operator_id: str, group: str, body: Any) -> set[str]:
    where = f"operators.{operator_id}.groups.{group}"
    body = _mapping(body, where)
    key = body.get("catalog")
    spec = CATALOG.get(key) if isinstance(key, str) else None
    if spec is None:
        raise SnapshotError(f"{where}.catalog is not a catalog key")
    if spec.group != group or key != catalog_key(operator_id, group):
        raise SnapshotError(
            f"{where}.catalog {key} belongs to another operator or group"
        )
    zones = body.get("zones")
    if not isinstance(zones, list) or not zones:
        raise SnapshotError(f"{where}.zones must be a non-empty list")
    ids = {
        _text(_mapping(zone, f"{where}.zones[]").get("id"), f"{where}.zones[].id")
        for zone in zones
    }
    bands = _mapping(body.get("bands"), f"{where}.bands")
    if not set(bands) <= set(BANDS) or tuple(
        b for b in BANDS if b in bands
    ) != tariff_bands(key):
        raise SnapshotError(f"{where}.bands must be exactly the bands of {key}")
    if sorted(bands.values()) != sorted(ids):
        raise SnapshotError(f"{where}.bands must map every zone exactly once")
    _validate_charges(body.get("charges"), ids, where)
    return ids


def _validate_offer(
    offer_id: str, offer: Any, groups: Mapping[str, Mapping[str, set[str]]]
) -> None:
    where = f"offers.{offer_id}"
    offer = _mapping(offer, where)
    if offer.get("kind") not in OFFER_KINDS:
        raise SnapshotError(f"{where}.kind must be ure or fixed")
    scope = offer.get("osdScope")
    if not isinstance(scope, list) or not scope or not set(scope) <= set(groups):
        raise SnapshotError(f"{where}.osdScope must list known operators")
    for key in ("seller", "name"):
        _text(offer.get(key), f"{where}.{key}")
    _number(offer.get("tradeFeePerMonth"), f"{where}.tradeFeePerMonth")
    term = offer.get("termMonths")
    if term is not None:
        _number(term, f"{where}.termMonths")
    if offer.get("availableUntil") is not None:
        _iso(offer["availableUntil"], f"{where}.availableUntil")
    _iso(offer.get("asOf"), f"{where}.asOf")
    priced = _mapping(offer.get("groups"), f"{where}.groups")
    if not priced:
        raise SnapshotError(f"{where}.groups must price a group")
    for group, prices in priced.items():
        prices = _mapping(prices, f"{where}.groups.{group}")
        expected = [groups[op][group] for op in scope if group in groups[op]]
        if not expected:
            raise SnapshotError(f"{where}.groups.{group} is no group of its scope")
        for zones in expected:
            if set(prices) != zones:
                raise SnapshotError(
                    f"{where}.groups.{group} must price exactly the zones"
                )
        for zone, price in prices.items():
            _number(price, f"{where}.groups.{group}.{zone}")


def validate_snapshot(data: Any) -> None:
    """Raise SnapshotError when the snapshot breaks the format 1 rules."""
    data = _mapping(data, "snapshot")
    if data.get("format") != SNAPSHOT_FORMAT:
        raise SnapshotError("snapshot format must be 1")
    vat = _number(data.get("vatRate"), "vatRate")
    if not 0 < vat < 1:
        raise SnapshotError("vatRate must be between 0 and 1")
    excise = _mapping(data.get("excise"), "excise")
    _number(excise.get("netPerKWh"), "excise.netPerKWh")
    _iso(excise.get("asOf"), "excise.asOf")
    _text(excise.get("source"), "excise.source")
    operators_ = _mapping(data.get("operators"), "operators")
    if not operators_:
        raise SnapshotError("operators must not be empty")
    groups: dict[str, dict[str, set[str]]] = {}
    for operator_id, operator in operators_.items():
        where = f"operators.{operator_id}"
        operator = _mapping(operator, where)
        _text(operator.get("name"), f"{where}.name")
        year = operator.get("tariffYear")
        if isinstance(year, bool) or not isinstance(year, int):
            raise SnapshotError(f"{where}.tariffYear must be an integer")
        valid_from = _iso(operator.get("validFrom"), f"{where}.validFrom")
        valid_to = _iso(operator.get("validTo"), f"{where}.validTo")
        if valid_from > valid_to:
            raise SnapshotError(f"{where}.validFrom is after validTo")
        _iso(operator.get("asOf"), f"{where}.asOf")
        _text(operator.get("source"), f"{where}.source")
        body = _mapping(operator.get("groups"), f"{where}.groups")
        groups[operator_id] = {
            group: _validate_group(operator_id, group, value)
            for group, value in body.items()
        }
    for offer_id, offer in _mapping(data.get("offers"), "offers").items():
        _validate_offer(offer_id, offer, groups)


SNAPSHOT: Mapping[str, Any] = load_snapshot()


def operators() -> tuple[str, ...]:
    """Operator ids of the snapshot, in snapshot order."""
    return tuple(SNAPSHOT["operators"])


def operator_for(tariff: str) -> str | None:
    """Snapshot operator id of a catalog key; None for `g11` (any operator)."""
    for operator_id, operator in SNAPSHOT["operators"].items():
        for group in operator["groups"].values():
            if group["catalog"] == tariff and tariff != "g11":
                return operator_id
    return None


@dataclass(frozen=True)
class PrefillResult:
    """Settings a seller offer writes, with the provenance stored on the schedule."""

    values: Mapping[str, float | bool | str]
    prefill: Prefill
    unbound: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class ChargeOptions:
    """Answers `tariff_charges` offers for one operator and group."""

    billing_months: tuple[int, ...]
    consumption_edges: tuple[int | None, ...]


_DEFAULT_VAT_PERCENT = 0
_RATE_STEP = Decimal("0.000001")
_MONEY_STEP = Decimal("0.01")
_PHASE_KEYS = {"one_phase": "onePhase", "three_phase": "threePhase"}
_KIND_ORDER = {"ure": 0, "fixed": 1}
_TEXT = {
    "en": {
        "ure": "default seller tariff (URE)",
        "fixed": "fixed price",
        "fixed_months": "fixed price {months} months",
        "fee": "trade fee {fee} PLN/month",
        "until": "sign by {until}",
        "gone": "(no longer offered)",
        "stale": (
            "Warning: the bundled {year} tariff ended {valid_to}; "
            "the rates are out of date."
        ),
    },
    "pl": {
        "ure": "taryfa sprzedawcy z urzędu (URE)",
        "fixed": "cena stała",
        "fixed_months": "cena stała {months} mies.",
        "fee": "opłata handlowa {fee} zł/mies.",
        "until": "do podpisania do {until}",
        "gone": "(już niedostępna)",
        "stale": (
            "Uwaga: wbudowana taryfa {year} skończyła się {valid_to}; "
            "stawki są nieaktualne."
        ),
    },
}


def _d(value: Any) -> Decimal:
    """Decimal of a snapshot number, from its shortest text (the JSON literal)."""
    return Decimal(str(value))


def _group(operator: str, group: str) -> Mapping[str, Any]:
    body = SNAPSHOT["operators"].get(operator, {}).get("groups", {}).get(group)
    if body is None:
        raise ValueError(f"operator {operator} has no group {group}")
    return body


def _texts(language: str) -> Mapping[str, str]:
    return _TEXT["pl" if language.lower().startswith("pl") else "en"]


def offers_for(operator: str, group: str) -> tuple[str, ...]:
    """Offer ids priced for every zone of the group: `ure` first, then `fixed` by name."""
    zones = {zone["id"] for zone in _group(operator, group)["zones"]}
    found = [
        (offer_id, offer)
        for offer_id, offer in SNAPSHOT["offers"].items()
        if offer["kind"] in OFFER_KINDS
        and operator in offer["osdScope"]
        and zones == set(offer["groups"].get(group, ()))
    ]
    found.sort(key=lambda item: (_KIND_ORDER[item[1]["kind"]], item[1]["name"]))
    return tuple(offer_id for offer_id, _ in found)


def charge_options(operator: str, group: str) -> ChargeOptions:
    """Billing periods and consumption band edges the operator publishes for a group."""
    charges = _group(operator, group)["charges"]
    return ChargeOptions(
        tuple(sorted(int(months) for months in charges["subscriptionPerMonth"])),
        tuple(row["maxKWh"] for row in charges["capacityPerMonth"]),
    )


def _band_row(rows: list[Mapping[str, Any]], edge: int | None) -> Decimal:
    """`pln` of the band that holds the consumption band ending at `edge`."""
    for row in sorted(rows, key=lambda r: (r["maxKWh"] is None, r["maxKWh"] or 0)):
        limit = row["maxKWh"]
        if limit is None or (edge is not None and limit >= edge):
            return _d(row["pln"])
    raise ValueError(f"no band holds the consumption edge {edge}")


def _percent(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _money(value: Decimal) -> float:
    return float(value.quantize(_MONEY_STEP, ROUND_HALF_UP))


def _rate(value: Decimal) -> float:
    return float(value.quantize(_RATE_STEP, ROUND_HALF_UP))


def compute_prefill(
    tariff: str,
    operator: str,
    offer: str | None,
    phases: Literal["one_phase", "three_phase"],
    billing_months: int,
    consumption_max_kwh: int | None,
    values: Mapping[str, Any],
    *,
    helpers: Collection[str] = (),
) -> PrefillResult:
    """Settings of the buy source for an offer (None = distribution only).

    `values` are the current settings (`sell_apply_vat`, `vat_percent`) and
    `helpers` the setting keys bound to a helper. Net rates come from the snapshot
    as Decimal; VAT stays in `buy_apply_vat` (DD-8).
    """
    spec = CATALOG.get(tariff)
    if spec is None:
        raise ValueError(f"unknown tariff {tariff}")
    body = _group(operator, spec.group)
    if body["catalog"] != tariff and tariff != "g11":
        raise ValueError(f"tariff {tariff} is not a group of operator {operator}")
    if phases not in _PHASE_KEYS:
        raise ValueError(f"unknown phases {phases}")
    options = charge_options(operator, spec.group)
    if billing_months not in options.billing_months:
        raise ValueError(f"{operator} does not bill every {billing_months} months")
    if consumption_max_kwh not in options.consumption_edges:
        raise ValueError(f"{consumption_max_kwh} is not a consumption band edge")
    seller = None
    if offer is not None:
        if offer not in offers_for(operator, spec.group):
            raise ValueError(f"offer {offer} does not price {operator} {spec.group}")
        seller = SNAPSHOT["offers"][offer]
    operator_body = SNAPSHOT["operators"][operator]
    charges = body["charges"]
    excise = _d(SNAPSHOT["excise"]["netPerKWh"])
    extra = sum((_d(charges[key]) for key in _CHARGE_RATES), Decimal(0))
    bands = tariff_bands(tariff)
    written: dict[str, float | bool | str] = {}
    for band in BANDS:
        energy = distribution = Decimal(0)
        if band in bands:
            zone = body["bands"][band]
            distribution = _d(charges["variablePerKWh"][zone]) + extra
            if seller is not None:
                energy = _d(seller["groups"][spec.group][zone]) + excise
        written[BAND_RATE_KEYS[band]] = _rate(energy)
        written[BAND_DISTRIBUTION_KEYS[band]] = _rate(distribution)
    written["buy_distribution_mode"] = "added"
    written["buy_multiplier"] = 1.0
    written["buy_addition"] = 0.0
    written["buy_apply_vat"] = True
    offered_vat = _d(SNAPSHOT["vatRate"]) * 100
    vat = offered_vat
    notes: list[str] = []
    if not values.get("sell_apply_vat", False) and "vat_percent" not in helpers:
        written["vat_percent"] = float(vat)
    else:
        vat = _d(values.get("vat_percent", _DEFAULT_VAT_PERCENT))
        if vat != offered_vat:
            notes.append(
                f"VAT is {_percent(vat)} % (shared with the sell side); "
                f"the offer assumes {_percent(offered_vat)} %."
            )
    net = (
        _d(charges["fixedPerMonth"][_PHASE_KEYS[phases]])
        + _d(charges["subscriptionPerMonth"][str(billing_months)])
        + _band_row(charges["capacityPerMonth"], consumption_max_kwh)
        + _band_row(charges["transitionalPerMonth"], consumption_max_kwh)
        + (_d(seller["tradeFeePerMonth"]) if seller is not None else Decimal(0))
    )
    written["monthly_charge"] = _money(net * (1 + vat / 100))
    unbound = tuple(key for key in written if key in helpers)
    as_of = max(
        operator_body["asOf"],
        SNAPSHOT["excise"]["asOf"],
        *([seller["asOf"]] if seller is not None else []),
    )
    prefill = Prefill(
        operator,
        offer,
        operator_body["tariffYear"],
        operator_body["validFrom"],
        operator_body["validTo"],
        as_of,
        phases,
        billing_months,
        consumption_max_kwh,
        {
            key: float(value)
            for key, value in written.items()
            if isinstance(value, float)
        },
    )
    return PrefillResult(written, prefill, unbound, tuple(notes))


def offer_label(offer: str, language: str, today: date) -> str:
    """Select-option label of an offer: seller, name, kind, trade fee, sign-by date.

    `today` (the caller's local date) decides whether the offer is marked as gone.
    """
    texts = _texts(language)
    data = SNAPSHOT["offers"][offer]
    if data["kind"] == "ure":
        kind = texts["ure"]
    elif data.get("termMonths"):
        kind = texts["fixed_months"].format(months=int(data["termMonths"]))
    else:
        kind = texts["fixed"]
    fee = f"{data['tradeFeePerMonth']:.2f}"
    if texts is _TEXT["pl"]:
        fee = fee.replace(".", ",")
    parts = [f"{data['seller']}: {data['name']}", kind, texts["fee"].format(fee=fee)]
    until = data.get("availableUntil")
    if until is not None:
        parts.append(texts["until"].format(until=until))
    label = " · ".join(parts)
    if until is not None and today > date.fromisoformat(until):
        label += f" {texts['gone']}"
    return label


def snapshot_text(operator: str, today: date, language: str) -> dict[str, str]:
    """Placeholders of the offer step: tariff, year, valid, as_of and the stale note."""
    body = SNAPSHOT["operators"][operator]
    stale = today > date.fromisoformat(body["validTo"])
    return {
        "tariff": body["name"],
        "year": str(body["tariffYear"]),
        "valid": f"{body['validFrom']} – {body['validTo']}",
        "as_of": body["asOf"],
        "note": _texts(language)["stale"].format(
            year=body["tariffYear"], valid_to=body["validTo"]
        )
        if stale
        else "",
    }
