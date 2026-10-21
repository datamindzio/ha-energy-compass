"""Tariff rate prefill: golden prices of appendix A, charges, offers, labels and the VAT rule."""

from datetime import date
from decimal import Decimal

import pytest

from custom_components.energy_compass.sources import rates
from custom_components.energy_compass.sources.tariffs import (
    BAND_DISTRIBUTION_KEYS,
    BAND_RATE_KEYS,
)

# operator, catalog key, offer (None = distribution only),
# {band: (net buy rate, net distribution rate, gross per-zone price)}, gross monthly charge
# for one phase, monthly billing and the <=2800 kWh band. Literals of appendix A in
# docs/superpowers/specs/2026-10-20-tariff-rates-design.md.
GOLDEN = [
    (
        "pge",
        "g11",
        "energa-podstawowa-2-lata",
        {"peak": (0.5, 0.3904, 1.095192)},
        54.33,
    ),
    (
        "pge",
        "g11",
        "energa-podstawowa-3-lata",
        {"peak": (0.4992, 0.3904, 1.094208)},
        56.33,
    ),
    (
        "pge",
        "g11",
        "pge-prad-z-gwarancjami",
        {"peak": (0.495, 0.3904, 1.089042)},
        59.26,
    ),
    ("pge", "g11", "pge-ure-2026", {"peak": (0.5032, 0.3904, 1.099128)}, 33.43),
    (
        "pge",
        "pge_g12",
        "energa-podstawowa-2-lata",
        {"peak": (0.608, 0.4449, 1.295067), "off_peak": (0.4037, 0.12, 0.644151)},
        58.02,
    ),
    (
        "pge",
        "pge_g12",
        "energa-podstawowa-3-lata",
        {"peak": (0.6071, 0.4449, 1.29396), "off_peak": (0.4031, 0.12, 0.643413)},
        60.02,
    ),
    (
        "pge",
        "pge_g12",
        "pge-prad-z-gwarancjami",
        {"peak": (0.56184, 0.4449, 1.23829), "off_peak": (0.36966, 0.12, 0.602282)},
        62.95,
    ),
    (
        "pge",
        "pge_g12",
        "pge-ure-2026",
        {"peak": (0.5706, 0.4449, 1.249065), "off_peak": (0.3768, 0.12, 0.611064)},
        37.12,
    ),
    (
        "pge",
        "pge_g12w",
        "energa-podstawowa-2-lata",
        {"peak": (0.608, 0.4711, 1.327293), "off_peak": (0.4037, 0.128, 0.653991)},
        58.82,
    ),
    (
        "pge",
        "pge_g12w",
        "energa-podstawowa-3-lata",
        {"peak": (0.6071, 0.4711, 1.326186), "off_peak": (0.4031, 0.128, 0.653253)},
        60.82,
    ),
    (
        "pge",
        "pge_g12w",
        "pge-prad-z-gwarancjami",
        {"peak": (0.5782, 0.4711, 1.290639), "off_peak": (0.42093, 0.128, 0.675184)},
        63.75,
    ),
    (
        "pge",
        "pge_g12w",
        "pge-ure-2026",
        {"peak": (0.5871, 0.4711, 1.301586), "off_peak": (0.4285, 0.128, 0.684495)},
        37.92,
    ),
    (
        "tauron",
        "g11",
        "energa-podstawowa-2-lata",
        {"peak": (0.5, 0.2899, 0.971577)},
        56.72,
    ),
    (
        "tauron",
        "g11",
        "energa-podstawowa-3-lata",
        {"peak": (0.4992, 0.2899, 0.970593)},
        58.72,
    ),
    ("tauron", "g11", "tauron-ure-2026", {"peak": (0.502, 0.2899, 0.974037)}, 35.82),
    (
        "tauron",
        "tauron_g12",
        "energa-podstawowa-2-lata",
        {"peak": (0.608, 0.3276, 1.150788), "off_peak": (0.4037, 0.0993, 0.61869)},
        56.72,
    ),
    (
        "tauron",
        "tauron_g12",
        "energa-podstawowa-3-lata",
        {"peak": (0.6071, 0.3276, 1.149681), "off_peak": (0.4031, 0.0993, 0.617952)},
        58.72,
    ),
    (
        "tauron",
        "tauron_g12",
        "tauron-ure-2026",
        {"peak": (0.548, 0.3276, 1.076988), "off_peak": (0.418, 0.0993, 0.636279)},
        35.82,
    ),
    (
        "tauron",
        "tauron_g12w",
        "energa-podstawowa-2-lata",
        {"peak": (0.608, 0.3733, 1.206999), "off_peak": (0.4037, 0.0947, 0.613032)},
        56.72,
    ),
    (
        "tauron",
        "tauron_g12w",
        "energa-podstawowa-3-lata",
        {"peak": (0.6071, 0.3733, 1.205892), "off_peak": (0.4031, 0.0947, 0.612294)},
        58.72,
    ),
    (
        "tauron",
        "tauron_g12w",
        "tauron-ure-2026",
        {"peak": (0.627, 0.3733, 1.230369), "off_peak": (0.418, 0.0947, 0.630621)},
        35.82,
    ),
    (
        "tauron",
        "tauron_g13",
        "tauron-ure-2026",
        {
            "peak": (0.783, 0.4333, 1.496049),
            "mid_peak": (0.4718, 0.2638, 0.904788),
            "off_peak": (0.426, 0.0827, 0.625701),
        },
        35.82,
    ),
    ("enea", "g11", "enea-ure-2026", {"peak": (0.503, 0.2891, 0.974283)}, 35.02),
    (
        "enea",
        "g11",
        "energa-podstawowa-2-lata",
        {"peak": (0.5, 0.2891, 0.970593)},
        55.92,
    ),
    (
        "enea",
        "g11",
        "energa-podstawowa-3-lata",
        {"peak": (0.4992, 0.2891, 0.969609)},
        57.92,
    ),
    (
        "enea",
        "enea_g12",
        "enea-ure-2026",
        {"peak": (0.5829, 0.3214, 1.112289), "off_peak": (0.3419, 0.1348, 0.586341)},
        37.65,
    ),
    (
        "enea",
        "enea_g12",
        "energa-podstawowa-2-lata",
        {"peak": (0.608, 0.3214, 1.143162), "off_peak": (0.4037, 0.1348, 0.662355)},
        58.55,
    ),
    (
        "enea",
        "enea_g12",
        "energa-podstawowa-3-lata",
        {"peak": (0.6071, 0.3214, 1.142055), "off_peak": (0.4031, 0.1348, 0.661617)},
        60.55,
    ),
    (
        "enea",
        "enea_g12w",
        "enea-ure-2026",
        {"peak": (0.6568, 0.3137, 1.193715), "off_peak": (0.3515, 0.1248, 0.585849)},
        46.58,
    ),
    (
        "enea",
        "enea_g12w",
        "energa-podstawowa-2-lata",
        {"peak": (0.608, 0.3137, 1.133691), "off_peak": (0.4037, 0.1248, 0.650055)},
        67.48,
    ),
    (
        "enea",
        "enea_g12w",
        "energa-podstawowa-3-lata",
        {"peak": (0.6071, 0.3137, 1.132584), "off_peak": (0.4031, 0.1248, 0.649317)},
        69.48,
    ),
    (
        "enea",
        "enea_g13active",
        None,
        {
            "peak": (0.0, 0.3467, None),
            "mid_peak": (0.0, 0.2891, None),
            "off_peak": (0.0, 0.1165, None),
        },
        37.65,
    ),
    (
        "energa",
        "g11",
        "energa-podstawowa-2-lata",
        {"peak": (0.5, 0.392, 1.09716)},
        57.37,
    ),
    (
        "energa",
        "g11",
        "energa-podstawowa-3-lata",
        {"peak": (0.4992, 0.392, 1.096176)},
        59.37,
    ),
    ("energa", "g11", "energa-ure-2026", {"peak": (0.5018, 0.392, 1.099374)}, 36.47),
    (
        "energa",
        "energa_g12",
        "energa-podstawowa-2-lata",
        {"peak": (0.608, 0.4279, 1.274157), "off_peak": (0.4037, 0.1262, 0.651777)},
        65.39,
    ),
    (
        "energa",
        "energa_g12",
        "energa-podstawowa-3-lata",
        {"peak": (0.6071, 0.4279, 1.27305), "off_peak": (0.4031, 0.1262, 0.651039)},
        67.39,
    ),
    (
        "energa",
        "energa_g12",
        "energa-ure-2026",
        {"peak": (0.5839, 0.4279, 1.244514), "off_peak": (0.3803, 0.1262, 0.622995)},
        44.49,
    ),
    (
        "energa",
        "energa_g12w",
        "energa-podstawowa-2-lata",
        {"peak": (0.608, 0.4452, 1.295436), "off_peak": (0.4037, 0.1286, 0.654729)},
        65.39,
    ),
    (
        "energa",
        "energa_g12w",
        "energa-podstawowa-3-lata",
        {"peak": (0.6071, 0.4452, 1.294329), "off_peak": (0.4031, 0.1286, 0.653991)},
        67.39,
    ),
    (
        "energa",
        "energa_g12w",
        "energa-ure-2026",
        {"peak": (0.6107, 0.4452, 1.298757), "off_peak": (0.399, 0.1286, 0.648948)},
        44.49,
    ),
    (
        "stoen",
        "g11",
        "energa-podstawowa-2-lata",
        {"peak": (0.5, 0.2777, 0.956571)},
        59.67,
    ),
    (
        "stoen",
        "g11",
        "energa-podstawowa-3-lata",
        {"peak": (0.4992, 0.2777, 0.955587)},
        61.67,
    ),
    ("stoen", "g11", "eon-taryfa-2026", {"peak": (0.505, 0.2777, 0.962721)}, 55.04),
    (
        "stoen",
        "stoen_g12",
        "energa-podstawowa-2-lata",
        {"peak": (0.608, 0.298, 1.11438), "off_peak": (0.4037, 0.099, 0.618321)},
        59.67,
    ),
    (
        "stoen",
        "stoen_g12",
        "energa-podstawowa-3-lata",
        {"peak": (0.6071, 0.298, 1.113273), "off_peak": (0.4031, 0.099, 0.617583)},
        61.67,
    ),
    (
        "stoen",
        "stoen_g12",
        "eon-taryfa-2026",
        {"peak": (0.5394, 0.298, 1.030002), "off_peak": (0.4295, 0.099, 0.650055)},
        55.04,
    ),
    (
        "stoen",
        "stoen_g12w",
        "energa-podstawowa-2-lata",
        {"peak": (0.608, 0.3005, 1.117455), "off_peak": (0.4037, 0.1514, 0.682773)},
        59.67,
    ),
    (
        "stoen",
        "stoen_g12w",
        "energa-podstawowa-3-lata",
        {"peak": (0.6071, 0.3005, 1.116348), "off_peak": (0.4031, 0.1514, 0.682035)},
        61.67,
    ),
    (
        "stoen",
        "stoen_g12w",
        "eon-taryfa-2026",
        {"peak": (0.5294, 0.3005, 1.020777), "off_peak": (0.4445, 0.1514, 0.732957)},
        55.04,
    ),
]
VAT_OFF = {"sell_apply_vat": False}


def prefill(tariff, operator, offer, **kwargs):
    arguments = {
        "phases": "one_phase",
        "billing_months": 1,
        "consumption_max_kwh": 2800,
        "values": VAT_OFF,
    }
    arguments.update(kwargs)
    return rates.compute_prefill(tariff, operator, offer, **arguments)


@pytest.mark.parametrize(
    ("operator", "tariff", "offer", "bands", "monthly"),
    GOLDEN,
    ids=[f"{row[0]}-{row[1]}-{row[2]}" for row in GOLDEN],
)
def test_golden_prices(operator, tariff, offer, bands, monthly):
    result = prefill(tariff, operator, offer)
    values = result.values
    for band in rates.BANDS:
        rate = values[BAND_RATE_KEYS[band]]
        distribution = values[BAND_DISTRIBUTION_KEYS[band]]
        if band not in bands:
            assert (rate, distribution) == (0.0, 0.0)
            continue
        energy, expected_distribution, gross = bands[band]
        assert rate == energy
        assert distribution == expected_distribution
        exact = (Decimal(str(energy)) + Decimal(str(expected_distribution))) * Decimal(
            "1.23"
        )
        assert abs((rate + distribution) * 1.23 - float(exact)) < 1e-9
        if gross is not None:
            assert abs((rate + distribution) * 1.23 - gross) < 0.001
            assert abs(float(exact) - gross) < 1e-6
    assert values["buy_distribution_mode"] == "added"
    assert values["buy_apply_vat"] is True
    assert values["vat_percent"] == 23.0
    assert values["monthly_charge"] == monthly
    assert result.prefill.operator == operator
    assert result.prefill.offer == offer


def test_monthly_charge_variants():
    # Tauron G13: (10.86 + 0.38 + 24.05 + 0 + 0) * 1.23
    three_phase = prefill(
        "tauron_g13",
        "tauron",
        "tauron-ure-2026",
        phases="three_phase",
        billing_months=12,
        consumption_max_kwh=None,
    )
    assert three_phase.values["monthly_charge"] == 43.41
    # Stoen G11 e.on: (11.46 + 0.48 + 4.29 + 13.23) * 1.23 = 36.2358
    stoen = prefill(
        "g11", "stoen", "eon-taryfa-2026", billing_months=6, consumption_max_kwh=500
    )
    assert stoen.values["monthly_charge"] == 36.24
    # Enea G11 ends its first band at 499 kWh: (7.45 + 3.84 + 4.29 + 0) * 1.23 = 19.1634
    enea = prefill("g11", "enea", "enea-ure-2026", consumption_max_kwh=499)
    assert enea.values["monthly_charge"] == 19.16
    with pytest.raises(ValueError, match="band edge"):
        prefill("g11", "enea", "enea-ure-2026", consumption_max_kwh=500)


def test_monthly_charge_follows_the_vat_setting():
    kept = prefill("g11", "pge", "pge-ure-2026", values={"sell_apply_vat": True})
    assert kept.values["monthly_charge"] == 27.18  # no vat_percent set: 0 %
    eight = prefill(
        "g11",
        "pge",
        "pge-ure-2026",
        values={"sell_apply_vat": True, "vat_percent": 8},
    )
    assert eight.values["monthly_charge"] == 29.35  # 27.18 * 1.08 = 29.3544


def test_offers_for_scope_and_order():
    assert rates.offers_for("stoen", "G11") == (
        "eon-taryfa-2026",
        "energa-podstawowa-2-lata",
        "energa-podstawowa-3-lata",
    )
    assert rates.offers_for("pge", "G11") == (
        "pge-ure-2026",
        "energa-podstawowa-2-lata",
        "energa-podstawowa-3-lata",
        "pge-prad-z-gwarancjami",
    )
    assert rates.offers_for("tauron", "G13") == ("tauron-ure-2026",)
    assert rates.offers_for("enea", "G13active") == ()
    for operator, body in rates.SNAPSHOT["operators"].items():
        for group in body["groups"]:
            for offer in rates.offers_for(operator, group):
                assert rates.SNAPSHOT["offers"][offer]["kind"] in {"ure", "fixed"}
                assert operator in rates.SNAPSHOT["offers"][offer]["osdScope"]


def test_offers_skip_a_group_the_seller_does_not_price(monkeypatch):
    snapshot = {
        **rates.SNAPSHOT,
        "offers": {
            "unpriced": {
                **rates.SNAPSHOT["offers"]["tauron-ure-2026"],
                "groups": {"G11": {"all": 0.5}},
            }
        },
    }
    monkeypatch.setattr(rates, "SNAPSHOT", snapshot)
    assert rates.offers_for("tauron", "G13") == ()
    assert rates.offers_for("tauron", "G11") == ("unpriced",)


def test_enea_g13active_distribution_only():
    result = prefill("enea_g13active", "enea", None)
    assert result.values["buy_rate"] == 0.0
    assert result.values["buy_mid_peak_rate"] == 0.0
    assert result.values["buy_off_peak_rate"] == 0.0
    assert result.values["buy_distribution_rate"] == 0.3467
    assert result.values["buy_distribution_mid_peak_rate"] == 0.2891
    assert result.values["buy_distribution_off_peak_rate"] == 0.1165
    # (fixed + subscription + capacity) * 1.23 with no trade fee: appendix A, 30.61 net
    assert result.values["monthly_charge"] == 37.65
    assert result.prefill.offer is None


def test_vat_rule():
    sell_off = prefill("g11", "pge", "pge-ure-2026", values={"sell_apply_vat": False})
    assert sell_off.values["vat_percent"] == 23.0
    assert sell_off.notes == ()
    assert "vat_percent" in sell_off.prefill.values

    sell_on = prefill(
        "g11",
        "pge",
        "pge-ure-2026",
        values={"sell_apply_vat": True, "vat_percent": 8},
    )
    assert "vat_percent" not in sell_on.values
    assert "vat_percent" not in sell_on.prefill.values
    assert sell_on.values["buy_apply_vat"] is True
    assert sell_on.notes == (
        "VAT is 8 % (shared with the sell side); the offer assumes 23 %.",
    )

    same = prefill(
        "g11",
        "pge",
        "pge-ure-2026",
        values={"sell_apply_vat": True, "vat_percent": 23},
    )
    assert "vat_percent" not in same.values
    assert same.notes == ()

    bound = prefill(
        "g11",
        "pge",
        "pge-ure-2026",
        values={"sell_apply_vat": False, "vat_percent": 8},
        helpers={"vat_percent"},
    )
    assert "vat_percent" not in bound.values
    assert bound.unbound == ()
    assert len(bound.notes) == 1


def test_unbound_lists_the_helper_keys_it_writes():
    result = prefill(
        "g11",
        "pge",
        "pge-ure-2026",
        helpers={"buy_rate", "monthly_charge", "sell_rate"},
    )
    assert result.unbound == ("buy_rate", "monthly_charge")


def test_resets_multiplier_and_addition():
    result = prefill(
        "g11",
        "pge",
        "pge-ure-2026",
        values={"sell_apply_vat": False, "buy_multiplier": 1.4, "buy_addition": 0.2},
    )
    assert result.values["buy_multiplier"] == 1.0
    assert result.values["buy_addition"] == 0.0


def test_g11_off_peak_zeroed():
    result = prefill("g11", "tauron", "tauron-ure-2026")
    assert result.values["buy_rate"] == 0.502
    assert result.values["buy_mid_peak_rate"] == 0.0
    assert result.values["buy_off_peak_rate"] == 0.0
    assert result.values["buy_distribution_mid_peak_rate"] == 0.0
    assert result.values["buy_distribution_off_peak_rate"] == 0.0


def test_prefill_provenance():
    result = prefill(
        "tauron_g13",
        "tauron",
        "tauron-ure-2026",
        phases="three_phase",
        billing_months=2,
        consumption_max_kwh=1200,
    )
    prefill_ = result.prefill
    assert (prefill_.operator, prefill_.offer, prefill_.tariff_year) == (
        "tauron",
        "tauron-ure-2026",
        2026,
    )
    assert (prefill_.valid_from, prefill_.valid_to) == ("2026-01-01", "2026-12-31")
    assert prefill_.as_of == "2026-10-06"
    assert (prefill_.phases, prefill_.billing_months) == ("three_phase", 2)
    assert prefill_.consumption_max_kwh == 1200
    assert prefill_.values["buy_rate"] == 0.783
    assert prefill_.values["monthly_charge"] == result.values["monthly_charge"]
    assert "buy_distribution_mode" not in prefill_.values
    assert "buy_apply_vat" not in prefill_.values


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"operator": "pge", "tariff": "tauron_g13"}, "has no group"),
        ({"billing_months": 12}, "does not bill"),
        ({"phases": "two_phase"}, "unknown phases"),
        ({"offer": "tauron-ure-2026"}, "does not price"),
        ({"tariff": "pge_g99"}, "unknown tariff"),
    ],
)
def test_bad_arguments_are_refused(arguments, message):
    arguments = {
        "tariff": "g11",
        "operator": "pge",
        "offer": "pge-ure-2026",
    } | arguments
    with pytest.raises(ValueError, match=message):
        prefill(**arguments)


def test_offer_labels_en_pl():
    assert rates.offer_label("pge-ure-2026", "en", date(2026, 10, 21)) == (
        "PGE Obrót: Taryfa G PGE Obrót 2026 (sprzedawca z urzędu)"
        " · default seller tariff (URE) · trade fee 0.00 PLN/month"
    )
    assert rates.offer_label("pge-ure-2026", "pl", date(2026, 10, 21)) == (
        "PGE Obrót: Taryfa G PGE Obrót 2026 (sprzedawca z urzędu)"
        " · taryfa sprzedawcy z urzędu (URE) · opłata handlowa 0,00 zł/mies."
    )
    assert rates.offer_label("energa-podstawowa-2-lata", "en", date(2026, 10, 21)) == (
        "Energa Obrót: Podstawowa 2 lata (stała cena)"
        " · fixed price 24 months · trade fee 16.99 PLN/month · sign by 2026-12-31"
    )
    assert rates.offer_label(
        "energa-podstawowa-2-lata", "pl-PL", date(2026, 10, 21)
    ) == (
        "Energa Obrót: Podstawowa 2 lata (stała cena)"
        " · cena stała 24 mies. · opłata handlowa 16,99 zł/mies."
        " · do podpisania do 2026-12-31"
    )
    # a fixed price without a term still says it is fixed
    assert " · fixed price · " in rates.offer_label(
        "pge-prad-z-gwarancjami", "en", date(2026, 10, 21)
    )


def test_expired_offer_marked():
    label = "energa-podstawowa-2-lata"
    assert rates.offer_label(label, "en", date(2026, 12, 31)).endswith("2026-12-31")
    assert rates.offer_label(label, "en", date(2027, 1, 2)).endswith(
        "sign by 2026-12-31 (no longer offered)"
    )
    assert rates.offer_label(label, "pl", date(2027, 1, 2)).endswith(
        "do podpisania do 2026-12-31 (już niedostępna)"
    )
    # still listed, and still computes
    assert label in rates.offers_for("pge", "G11")
    assert prefill("g11", "pge", label).values["buy_rate"] == 0.5
    # an offer without an end date is never marked
    assert "(no longer offered)" not in rates.offer_label(
        "pge-ure-2026", "en", date(2030, 1, 1)
    )


def test_snapshot_text_stale_en_pl():
    fresh = rates.snapshot_text("tauron", date(2026, 12, 31), "en")
    assert fresh == {
        "tariff": "Tauron Dystrybucja",
        "year": "2026",
        "valid": "2026-01-01 – 2026-12-31",
        "as_of": "2026-10-06",
        "note": "",
    }
    stale_en = rates.snapshot_text("tauron", date(2027, 1, 2), "en")
    assert stale_en["note"] == (
        "Warning: the bundled 2026 tariff ended 2026-12-31; the rates are out of date."
    )
    stale_pl = rates.snapshot_text("tauron", date(2027, 1, 2), "pl")
    assert stale_pl["note"] == (
        "Uwaga: wbudowana taryfa 2026 skończyła się 2026-12-31; stawki są nieaktualne."
    )
    assert {k: v for k, v in stale_pl.items() if k != "note"} == {
        k: v for k, v in fresh.items() if k != "note"
    }


def test_billing_choices_per_operator():
    months = {
        operator: rates.charge_options(operator, "G11").billing_months
        for operator in rates.operators()
    }
    assert months == {
        "pge": (1, 2, 6),
        "tauron": (1, 2, 6, 12),
        "energa": (1, 2),
        "stoen": (1, 6, 12),
        "enea": (1, 2, 6, 12),
    }
    assert rates.charge_options("enea", "G11").consumption_edges == (
        499,
        1200,
        2800,
        None,
    )
    assert rates.charge_options("pge", "G12").consumption_edges == (
        500,
        1200,
        2800,
        None,
    )
