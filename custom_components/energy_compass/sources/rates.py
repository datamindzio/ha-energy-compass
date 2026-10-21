"""Bundled Polish tariff rates: the `tariff_rates.json` snapshot, loaded and validated.

Pure module, no Home Assistant imports. The snapshot is written by
`tools/build_tariff_rates.py` from the energy-compass data and only pre-fills
editable settings; the runtime never reads it. It is loaded once at import
(Home Assistant imports integrations in its executor, so no event-loop I/O).
"""

import json
import math
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from .tariffs import BANDS, CATALOG, tariff_bands

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
