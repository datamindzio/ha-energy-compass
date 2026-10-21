"""The committed tariff_rates.json: valid, covers the catalog and agrees with the zone hours in tariffs.py."""

import copy
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.energy_compass.sources import rates
from custom_components.energy_compass.sources.tariffs import (
    CATALOG,
    CET,
    TariffSchedule,
    band_at,
    pl_holidays,
    tariff_bands,
)

SNAPSHOT = rates.load_snapshot()
GROUPS = [
    (operator_id, group)
    for operator_id, operator in SNAPSHOT["operators"].items()
    for group in operator["groups"]
]
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def test_committed_snapshot_is_valid():
    rates.validate_snapshot(SNAPSHOT)
    assert rates.SNAPSHOT == SNAPSHOT
    assert rates.operators() == ("pge", "tauron", "enea", "energa", "stoen")
    assert rates.operator_for("tauron_g13") == "tauron"
    assert rates.operator_for("enea_g13active") == "enea"
    assert rates.operator_for("g11") is None


def test_snapshot_covers_catalog():
    owners: dict[str, list[tuple[str, str]]] = {}
    for operator_id, group in GROUPS:
        key = SNAPSHOT["operators"][operator_id]["groups"][group]["catalog"]
        owners.setdefault(key, []).append((operator_id, group))
    assert set(owners) == set(CATALOG)
    for key, found in owners.items():
        if key == "g11":
            assert sorted(found) == sorted((op, "G11") for op in SNAPSHOT["operators"])
            assert len(found) == 5
        else:
            assert len(found) == 1
            assert found[0][1] == CATALOG[key].group
    for operator_id, group in GROUPS:
        body = SNAPSHOT["operators"][operator_id]["groups"][group]
        assert tuple(
            b for b in ("peak", "mid_peak", "off_peak") if b in body["bands"]
        ) == (tariff_bands(body["catalog"]))


def zone_of_hour(group, moment):
    """Port of energy-compass zoneOfHour on the standard clock (`moment` is on UTC+1)."""
    day = DAYS[moment.weekday()]
    hour = moment.hour
    fallback = None
    for zone in group["zones"]:
        if zone.get("default"):
            fallback = zone["id"]
        for rule in zone.get("rules", ()):
            if "months" in rule and moment.month not in rule["months"]:
                continue
            if day not in rule["days"]:
                continue
            if not any(start <= hour < end for start, end in rule["hours"]):
                continue
            if not rule["holidays"] and moment.date() in pl_holidays(moment.year):
                continue
            return zone["id"]
    assert fallback is not None
    return fallback


@pytest.mark.parametrize(("operator_id", "group"), GROUPS)
def test_snapshot_zones_match_catalog(operator_id, group):
    operator = SNAPSHOT["operators"][operator_id]
    body = operator["groups"][group]
    by_zone = {zone: band for band, zone in body["bands"].items()}
    schedule = replace(TariffSchedule.default(body["catalog"]), meter_winter_clock=True)
    moment = datetime(operator["tariffYear"], 1, 1, tzinfo=CET)
    end = datetime(operator["tariffYear"] + 1, 1, 1, tzinfo=CET)
    while moment < end:
        expected = by_zone[zone_of_hour(body, moment)]
        actual = band_at(schedule, moment.astimezone(UTC), "Europe/Warsaw")
        assert actual == expected, f"{body['catalog']} {moment.isoformat()}"
        moment += timedelta(hours=1)


def test_snapshot_vat_and_excise():
    assert SNAPSHOT["vatRate"] == 0.23
    assert SNAPSHOT["excise"]["netPerKWh"] == 0.005


def mutated(change):
    data = copy.deepcopy(SNAPSHOT)
    change(data)
    return data


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(format=2),
        lambda d: d.update(vatRate=0),
        lambda d: d["excise"].update(netPerKWh=-0.1),
        lambda d: d["operators"]["pge"].update(validFrom="2027-01-01"),
        lambda d: d["operators"]["pge"]["groups"]["G11"].update(catalog="tauron_g13"),
        lambda d: d["operators"]["tauron"]["groups"]["G13"]["bands"].pop("mid_peak"),
        lambda d: d["operators"]["pge"]["groups"]["G11"]["charges"].update(
            qualityPerKWh=-1
        ),
        lambda d: d["operators"]["pge"]["groups"]["G11"]["charges"][
            "subscriptionPerMonth"
        ].pop("1"),
        lambda d: d["operators"]["pge"]["groups"]["G11"]["charges"][
            "capacityPerMonth"
        ].pop(),
        lambda d: d["offers"]["pge-ure-2026"].update(kind="dynamic"),
        lambda d: d["offers"]["pge-ure-2026"].update(osdScope=[]),
        lambda d: d["offers"]["pge-ure-2026"]["groups"]["G12"].pop("night"),
    ],
)
def test_validator_rejects(change):
    with pytest.raises(rates.SnapshotError):
        rates.validate_snapshot(mutated(change))


def test_snapshot_dates_are_iso():
    for operator in SNAPSHOT["operators"].values():
        assert date.fromisoformat(operator["validFrom"]) <= date.fromisoformat(
            operator["validTo"]
        )
    assert json.dumps(SNAPSHOT)
