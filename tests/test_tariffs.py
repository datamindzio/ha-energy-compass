import hashlib
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.sources import tariffs as t

WARSAW = "Europe/Warsaw"
NIGHT = set(range(22, 24)) | set(range(6))
NIGHT_21 = {21, 22, 23} | set(range(6))

KEYS = (
    "g11",
    "pge_g12",
    "pge_g12w",
    "tauron_g12",
    "tauron_g12w",
    "enea_g12",
    "enea_g12w",
    "energa_g12",
    "energa_g12w",
    "stoen_g12",
    "stoen_g12w",
    "tauron_g13",
    "enea_g13active",
)
EXISTING_KEYS = KEYS[:11]

# key -> (weekday off-peak hours in the winter season, in the summer season)
EXPECTED = {
    "g11": (set(), set()),
    "pge_g12": (NIGHT | {13, 14}, NIGHT | {15, 16}),
    "pge_g12w": (NIGHT | {13, 14}, NIGHT | {15, 16}),
    "tauron_g12": (NIGHT | {13, 14}, NIGHT | {13, 14}),
    "tauron_g12w": (NIGHT | {13, 14}, NIGHT | {13, 14}),
    "enea_g12": (NIGHT | {13, 14}, NIGHT | {13, 14}),
    "enea_g12w": (NIGHT_21, NIGHT_21),
    "energa_g12": (NIGHT | {13, 14}, NIGHT | {13, 14}),
    "energa_g12w": (NIGHT | {13, 14}, NIGHT | {13, 14}),
    "stoen_g12": (NIGHT | {13, 14}, NIGHT | {13, 14}),
    "stoen_g12w": (NIGHT, NIGHT),
}
WINTER_WEEKDAY = date(2026, 1, 14)
SUMMER_WEEKDAY = date(2026, 7, 15)
SATURDAY = date(2026, 1, 17)
HOLIDAY = date(2026, 11, 11)


def hours(key: str, day: date) -> set[int]:
    schedule = t.TariffSchedule.default(key)
    zone = ZoneInfo(WARSAW)
    return {
        hour
        for hour in range(24)
        if t.band_at(
            schedule,
            datetime(day.year, day.month, day.day, hour, 30, tzinfo=zone),
            WARSAW,
        )
        == "off_peak"
    }


def winter(key="pge_g12", **params):
    return t.TariffSchedule(key, True, params)


def test_catalog_keys_and_order():
    assert len(t.CATALOG) == 13
    assert tuple(t.CATALOG) == KEYS
    assert KEYS[-2:] == ("tauron_g13", "enea_g13active")


@pytest.mark.parametrize("key", EXISTING_KEYS)
def test_catalog_golden(key):
    cold, warm = EXPECTED[key]
    assert hours(key, WINTER_WEEKDAY) == cold
    assert hours(key, SUMMER_WEEKDAY) == warm
    if key.endswith("g12w"):
        assert hours(key, SATURDAY) == set(range(24))
        assert hours(key, HOLIDAY) == set(range(24))
    elif key != "g11":
        assert hours(key, SATURDAY) == cold
        assert hours(key, HOLIDAY) == cold


def test_pge_season_edge_on_tariff_clock():
    schedule = t.TariffSchedule.default("pge_g12")
    assert (
        t.band_at(schedule, datetime(2026, 3, 31, 14, 30, tzinfo=UTC), WARSAW) == "peak"
    )
    assert (
        t.band_at(schedule, datetime(2026, 4, 1, 14, 30, tzinfo=UTC), WARSAW)
        == "off_peak"
    )


def test_holidays_golden():
    h26 = t.pl_holidays(2026)
    assert {
        date(2026, 4, 5),
        date(2026, 4, 6),
        date(2026, 5, 24),
        date(2026, 6, 4),
    } <= h26
    assert {
        date(2027, 3, 28),
        date(2027, 3, 29),
        date(2027, 5, 16),
        date(2027, 5, 27),
    } <= t.pl_holidays(2027)
    assert date(2024, 12, 24) not in t.pl_holidays(2024)
    assert date(2025, 12, 24) in t.pl_holidays(2025)
    assert len(h26) == 9 + 4 + 1
    assert len(t.pl_holidays(2024)) == 9 + 4
    assert t.is_free_day(SATURDAY) and t.is_free_day(HOLIDAY)
    assert not t.is_free_day(WINTER_WEEKDAY)
    assert t.holidays_between(date(2026, 11, 10), date(2026, 11, 12)) == (HOLIDAY,)
    assert t.holidays_between(date(2026, 12, 20), date(2027, 1, 7)) == (
        date(2026, 12, 24),
        date(2026, 12, 25),
        date(2026, 12, 26),
        date(2027, 1, 1),
        date(2027, 1, 6),
    )


def test_dst_days():
    local = t.TariffSchedule.default("pge_g12")
    old_meter = winter()
    same = "00:00–06:00, 13:00–15:00, 22:00–24:00"
    cases = [
        (date(2026, 3, 28), same, same),
        (date(2026, 3, 29), same, "00:00–07:00, 14:00–16:00, 23:00–24:00"),
        (
            date(2026, 7, 1),
            "00:00–06:00, 15:00–17:00, 22:00–24:00",
            "00:00–07:00, 16:00–18:00, 23:00–24:00",
        ),
        (date(2026, 10, 25), same, same),
    ]
    for day, expect_local, expect_winter in cases:
        assert t.local_off_peak(local, day, WARSAW) == expect_local, day
        assert t.local_off_peak(old_meter, day, WARSAW) == expect_winter, day


def test_local_off_peak_all_day_and_none():
    stoen = t.TariffSchedule.default("stoen_g12w")
    assert t.local_off_peak(stoen, SATURDAY, WARSAW) == "all day"
    assert t.local_off_peak(t.TariffSchedule.default("g11"), SATURDAY, WARSAW) == "none"


def test_band_rows_quarter_hours():
    schedule = winter()
    start = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    end = datetime(2026, 7, 1, 23, 0, tzinfo=UTC)
    rows = t.band_rows(schedule, start, end, WARSAW)
    assert [(r.start, r.end, r.band) for r in rows] == [
        (start, datetime(2026, 7, 1, 21, 0, tzinfo=UTC), "peak"),
        (datetime(2026, 7, 1, 21, 0, tzinfo=UTC), end, "off_peak"),
    ]
    local = t.band_rows(t.TariffSchedule.default("pge_g12"), start, end, WARSAW)
    assert [(r.band, r.start.hour) for r in local] == [("off_peak", 20)]
    odd_start = datetime(2026, 7, 1, 8, 7, tzinfo=UTC)
    odd_end = datetime(2026, 7, 2, 7, 3, tzinfo=UTC)
    odd = t.band_rows(t.TariffSchedule.default("pge_g12"), odd_start, odd_end, WARSAW)
    assert odd[0].start == odd_start and odd[-1].end == odd_end
    for first, second in pairwise(odd):
        assert first.end == second.start and first.band != second.band
    for row in odd[1:]:
        assert row.start.minute % 15 == 0 and row.start.second == 0


def test_local_clock_non_whole_hour_zone():
    schedule = t.TariffSchedule.default("tauron_g12")
    rows = t.band_rows(
        schedule,
        datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        "Asia/Kolkata",
    )
    assert all(row.start.minute == 30 for row in rows[1:])
    assert rows[1].start == datetime(2026, 7, 1, 0, 30, tzinfo=UTC)


def test_enea_params():
    schedule = t.TariffSchedule(
        "enea_g12", False, {"night_start": 23, "afternoon_start": 15}
    )
    t.validate_schedule(schedule)
    assert (
        t.local_off_peak(schedule, WINTER_WEEKDAY, WARSAW)
        == "00:00–07:00, 15:00–17:00, 23:00–24:00"
    )
    for bad in (
        {"night_start": 21, "afternoon_start": 13},
        {"night_start": 22, "afternoon_start": 16},
        {"night_start": 22},
        {"night_start": 22, "afternoon_start": 13, "x": 1},
        {},
    ):
        with pytest.raises(InputError):
            t.validate_schedule(t.TariffSchedule("enea_g12", False, bad))
    assert t.TariffSchedule.default("enea_g12").params == {
        "night_start": 22,
        "afternoon_start": 13,
    }
    with pytest.raises(InputError):
        t.validate_schedule(t.TariffSchedule("pge_g12", False, {"night_start": 22}))
    with pytest.raises(InputError, match="unknown tariff; reconfigure the buy source"):
        t.validate_schedule(t.TariffSchedule("nope"))
    assert t.enea_text(t.TariffSchedule.default("pge_g12")) is None
    assert t.enea_text(schedule) == (
        "hours set by Enea Operator per meter: 23:00–07:00 and 15:00–17:00 tariff time "
        "(allowed 8 h within 22–7, 2 h within 13–17), check your meter or bill"
    )


def test_g11_always_peak():
    schedule = t.TariffSchedule.default("g11")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=10)
    rows = t.band_rows(schedule, start, end, WARSAW)
    assert [(r.start, r.end, r.band) for r in rows] == [(start, end, "peak")]


def test_clock_text():
    assert (
        t.clock_text(t.TariffSchedule.default("pge_g12"), WARSAW)
        == "local time (Europe/Warsaw)"
    )
    assert t.clock_text(winter("pge_g12"), WARSAW) == (
        "winter time all year (CET, UTC+1), old meter, per "
        "PGE Dystrybucja tariff 2026 §2.2.6/2.2.8/2.2.11"
    )
    assert (
        t.clock_text(winter("energa_g12"), WARSAW)
        == "winter time all year (CET, UTC+1), old meter"
    )


def test_tariff_options():
    options = t.tariff_options("pl")
    assert [o["value"] for o in options] == list(KEYS)
    assert options[0]["label"] == "G11 (dowolny operator)"
    assert t.tariff_options(None)[0]["label"] == "G11 (any operator)"
    assert t.tariff_options("en")[2]["label"] == "PGE Dystrybucja G12w"


def binding(unit="PLN/MWh"):
    return {"entity": {"entity_id": "sensor.rce"}, "unit": unit}


def test_is_raw_rce_sell():
    good = {"mode": "forecast", "forecast": [binding()], "floor_per_kwh": 0.0}
    assert t.is_raw_rce_sell(good)
    assert not t.is_raw_rce_sell({**good, "floor_per_kwh": None})
    assert not t.is_raw_rce_sell({"mode": "forecast", "forecast": [binding()]})
    assert not t.is_raw_rce_sell({**good, "forecast": [binding(), binding("PLN/kWh")]})
    assert not t.is_raw_rce_sell({**good, "forecast": []})
    assert not t.is_raw_rce_sell({**good, "mode": "fixed"})
    assert not t.is_raw_rce_sell(
        {"mode": "schedule", "forecast": [], "floor_per_kwh": 0.0}
    )


def test_schedule_round_trip():
    schedule = t.TariffSchedule(
        "enea_g12", True, {"night_start": 23, "afternoon_start": 14}
    )
    assert t.TariffSchedule.from_dict(schedule.to_dict()) == schedule
    assert t.TariffSchedule.from_dict({"tariff": "pge_g12"}) == t.TariffSchedule(
        "pge_g12", False, {}
    )
    assert schedule.to_dict() == {
        "tariff": "enea_g12",
        "meter_winter_clock": True,
        "params": {"night_start": 23, "afternoon_start": 14},
    }


def test_tariff_bands_per_group():
    both = ("peak", "off_peak")
    assert t.BANDS == ("peak", "mid_peak", "off_peak")
    assert t.tariff_bands("g11") == ("peak",)
    for key in EXISTING_KEYS[1:]:
        assert t.tariff_bands(key) == both, key
    assert t.tariff_bands("tauron_g13") == t.BANDS
    assert t.tariff_bands("enea_g13active") == t.BANDS
    with pytest.raises(InputError, match="unknown tariff"):
        t.tariff_bands("nope")
    assert t.BAND_RATE_KEYS == {
        "peak": "buy_rate",
        "mid_peak": "buy_mid_peak_rate",
        "off_peak": "buy_off_peak_rate",
    }
    assert t.BAND_DISTRIBUTION_KEYS == {
        "peak": "buy_distribution_rate",
        "mid_peak": "buy_distribution_mid_peak_rate",
        "off_peak": "buy_distribution_off_peak_rate",
    }


# sha256[:16] of every (start, end, band) row of 2026 (local midnight to midnight),
# frozen on main before Window.band existed: (local clock, old meter on winter time).
BAND_ROWS_2026 = {
    "g11": ("d4039fa4f3cf73fb", "d4039fa4f3cf73fb", 1),
    "pge_g12": ("4d5f397500d655dd", "7f4b8c3a68a631ea", 1461),
    "pge_g12w": ("2123179d21375dba", "5f074f21fa8be5b3", 1013),
    "tauron_g12": ("bc7edec262347cae", "1a530827df613bc4", 1461),
    "tauron_g12w": ("bb77f230d084aae4", "2d4a5f9130255de1", 1013),
    "enea_g12": ("bc7edec262347cae", "1a530827df613bc4", 1461),
    "enea_g12w": ("d9f6b67ed9d48322", "ab82ff67fa493fb8", 507),
    "energa_g12": ("bc7edec262347cae", "1a530827df613bc4", 1461),
    "energa_g12w": ("bb77f230d084aae4", "2d4a5f9130255de1", 1013),
    "stoen_g12": ("bc7edec262347cae", "1a530827df613bc4", 1461),
    "stoen_g12w": ("31671f56cbac1289", "e7276406b22a6960", 507),
}


def rows_digest(key: str, old_meter: bool) -> tuple[str, int]:
    default = t.TariffSchedule.default(key)
    schedule = t.TariffSchedule(key, old_meter, default.params)
    zone = ZoneInfo(WARSAW)
    rows = t.band_rows(
        schedule,
        datetime(2026, 1, 1, tzinfo=zone),
        datetime(2027, 1, 1, tzinfo=zone),
        WARSAW,
    )
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            f"{row.start.isoformat()}|{row.end.isoformat()}|{row.band}\n".encode()
        )
    return digest.hexdigest()[:16], len(rows)


@pytest.mark.parametrize("key", EXISTING_KEYS)
def test_existing_keys_unchanged_by_band_field(key):
    local, old, count = BAND_ROWS_2026[key]
    assert rows_digest(key, False) == (local, count)
    assert rows_digest(key, True) == (old, count)


def band_hour(key, day, hour, *, old_meter=False, tz=None):
    """Band at hh:30 of the given hour on `tz` (default Warsaw)."""
    default = t.TariffSchedule.default(key)
    schedule = t.TariffSchedule(key, old_meter, default.params)
    moment = datetime(
        day.year, day.month, day.day, hour, 30, tzinfo=tz or ZoneInfo(WARSAW)
    )
    return t.band_at(schedule, moment, WARSAW)


def band_map(key, day, **kwargs):
    return {hour: band_hour(key, day, hour, **kwargs) for hour in range(24)}


def expected_map(peak=(), mid=()):
    """Band per hour from half-open (start, end) hour ranges; the rest is off_peak."""
    result = dict.fromkeys(range(24), "off_peak")
    for band, ranges in (("peak", peak), ("mid_peak", mid)):
        for start, end in ranges:
            for hour in range(start, end):
                assert result[hour] == "off_peak", (band, hour)
                result[hour] = band
    return result


def test_tauron_g13_weekday_summer():
    day = date(2026, 7, 15)
    assert band_hour("tauron_g13", day, 7) == "mid_peak"
    assert band_hour("tauron_g13", day, 13) == "off_peak"
    assert band_hour("tauron_g13", day, 19) == "peak"
    assert band_map("tauron_g13", day) == expected_map(peak=[(19, 22)], mid=[(7, 13)])
    assert band_at_quarter("tauron_g13", day, 21, 45) == "peak"
    assert band_at_quarter("tauron_g13", day, 22, 0) == "off_peak"
    assert band_hour("tauron_g13", day, 16) == "off_peak"


def band_at_quarter(key, day, hour, minute, **kwargs):
    schedule = t.TariffSchedule.default(key)
    moment = datetime(
        day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(WARSAW)
    )
    return t.band_at(schedule, moment, WARSAW)


def test_tauron_g13_weekday_winter():
    day = date(2026, 1, 14)
    assert band_hour("tauron_g13", day, 16) == "peak"
    assert band_at_quarter("tauron_g13", day, 20, 45) == "peak"
    assert band_at_quarter("tauron_g13", day, 21, 0) == "off_peak"
    assert band_hour("tauron_g13", day, 19) == "peak"
    assert band_map("tauron_g13", day) == expected_map(peak=[(16, 21)], mid=[(7, 13)])


@pytest.mark.parametrize("day", [date(2026, 7, 18), date(2026, 5, 1)])
def test_tauron_g13_saturday_and_holiday_off_peak(day):
    assert band_map("tauron_g13", day) == expected_map()
    rows = t.band_rows(
        t.TariffSchedule.default("tauron_g13"),
        datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(WARSAW)),
        datetime(day.year, day.month, day.day + 1, tzinfo=ZoneInfo(WARSAW)),
        WARSAW,
    )
    assert {row.band for row in rows} == {"off_peak"}


def test_tauron_g13_winter_clock():
    # Old meter on CET: the summer peak 19-22 CET is 20-23 on the Warsaw wall clock.
    schedule = t.TariffSchedule("tauron_g13", True, {})
    for hour in range(24):
        local = datetime(2026, 7, 15, hour, 30, tzinfo=ZoneInfo(WARSAW))
        cet_hour = (hour - 1) % 24
        expected = expected_map(peak=[(19, 22)], mid=[(7, 13)])[cet_hour]
        assert t.band_at(schedule, local, WARSAW) == expected, hour


# Appendix B of the tariff rates design: month -> (peak ranges, mid_peak ranges), rest off_peak.
ENEA = {
    1: ([(7, 10), (15, 20)], [(6, 7), (10, 15), (20, 23)]),
    2: ([(7, 9), (16, 21)], [(6, 7), (9, 16), (21, 23)]),
    3: ([(6, 9), (16, 23)], [(9, 10), (23, 24), (0, 6)]),
    4: ([(6, 9), (18, 23)], [(9, 10), (16, 18), (23, 24), (0, 6)]),
    5: ([(6, 9), (18, 23)], [(17, 18), (23, 24), (0, 6)]),
    6: ([(6, 9), (18, 23)], [(17, 18), (23, 24), (0, 6)]),
    7: ([(6, 9), (18, 23)], [(17, 18), (23, 24), (0, 6)]),
    8: ([(6, 9), (18, 23)], [(17, 18), (23, 24), (0, 6)]),
    9: ([(6, 9), (17, 23)], [(9, 10), (16, 17), (23, 24), (0, 6)]),
    10: ([(7, 9), (16, 23)], [(9, 10), (23, 24), (0, 7)]),
    11: ([(7, 9), (14, 21)], [(6, 7), (9, 14), (21, 23)]),
    12: ([(7, 10), (13, 20)], [(6, 7), (10, 13), (20, 23)]),
}


def enea_expected(month):
    peak, mid = ENEA[month]
    return expected_map(peak=peak, mid=mid)


def test_enea_g13active_months():
    cet = timezone_cet()
    for day in (
        date(2026, 1, 12),
        date(2026, 3, 10),
        date(2026, 6, 10),
        date(2026, 11, 10),
    ):
        # Old meter on winter time: the table applies to the CET hour directly.
        assert band_map("enea_g13active", day, old_meter=True, tz=cet) == enea_expected(
            day.month
        ), day
    # Local clock in June (UTC+2): the same instants read one hour later on the table.
    june = date(2026, 6, 10)
    shifted = {hour: enea_expected(6)[(hour + 1) % 24] for hour in range(24)}
    assert band_map("enea_g13active", june, tz=cet) == shifted


def timezone_cet():
    return t.CET


@pytest.mark.parametrize(
    ("day", "weekday"),
    [(date(2026, 5, 1), date(2026, 5, 4)), (date(2026, 6, 13), date(2026, 6, 10))],
)
def test_enea_g13active_holiday_and_saturday_follow_zones(day, weekday):
    assert band_map("enea_g13active", day) == band_map("enea_g13active", weekday)
    assert band_map("enea_g13active", day) == enea_expected(day.month)


@pytest.mark.parametrize(
    ("day", "quarters"), [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100)]
)
def test_enea_g13active_dst_days(day, quarters):
    schedule = t.TariffSchedule.default("enea_g13active")
    zone = ZoneInfo(WARSAW)
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end = datetime(day.year, day.month, day.day + 1, tzinfo=zone)
    rows = t.band_rows(schedule, start, end, WARSAW)
    assert (
        sum((row.end - row.start) // timedelta(minutes=15) for row in rows) == quarters
    )
    table = enea_expected(day.month)
    for row in rows:
        assert row.band == table[row.start.astimezone(zone).hour], row


def test_local_band_text():
    tauron = t.TariffSchedule.default("tauron_g13")
    weekday = date(2026, 7, 15)
    assert t.local_band(tauron, weekday, WARSAW, "mid_peak") == "07:00–13:00"
    assert t.local_band(tauron, weekday, WARSAW, "peak") == "19:00–22:00"
    assert (
        t.local_band(tauron, weekday, WARSAW, "off_peak")
        == "00:00–07:00, 13:00–19:00, 22:00–24:00"
    )
    assert t.local_band(tauron, date(2026, 7, 18), WARSAW, "off_peak") == "all day"
    assert t.local_band(tauron, date(2026, 7, 18), WARSAW, "peak") == "none"
    assert t.local_band(tauron, date(2026, 7, 18), WARSAW, "mid_peak") == "none"
    pge = t.TariffSchedule.default("pge_g12")
    assert t.local_band(pge, date(2026, 3, 28), WARSAW, "off_peak") == t.local_off_peak(
        pge, date(2026, 3, 28), WARSAW
    )
    assert (
        t.local_band(pge, date(2026, 3, 28), WARSAW, "peak")
        == "06:00–13:00, 15:00–22:00"
    )
    with pytest.raises(InputError, match="invalid timezone"):
        t.local_band(pge, date(2026, 3, 28), "Nowhere/Land", "peak")


PREFILL = {
    "operator": "pge",
    "offer": "pge-ure-2026",
    "tariff_year": 2026,
    "valid_from": "2026-01-01",
    "valid_to": "2026-12-31",
    "as_of": "2026-09-30",
    "phases": "one_phase",
    "billing_months": 1,
    "consumption_max_kwh": 2800,
    "values": {"buy_rate": 0.5032, "monthly_charge": 33.43},
}


def test_schedule_without_prefill_round_trips_byte_identical():
    stored = {"tariff": "pge_g12", "meter_winter_clock": True, "params": {}}
    schedule = t.TariffSchedule.from_dict(stored)
    assert schedule.prefill is None
    assert schedule.to_dict() == stored
    assert "prefill" not in schedule.to_dict()
    enea = {
        "tariff": "enea_g12",
        "meter_winter_clock": False,
        "params": {"night_start": 22, "afternoon_start": 13},
    }
    assert t.TariffSchedule.from_dict(enea).to_dict() == enea


def test_prefill_round_trip():
    stored = {
        "tariff": "g11",
        "meter_winter_clock": False,
        "params": {},
        "prefill": PREFILL,
    }
    schedule = t.TariffSchedule.from_dict(stored)
    assert schedule.prefill == t.Prefill(
        "pge",
        "pge-ure-2026",
        2026,
        "2026-01-01",
        "2026-12-31",
        "2026-09-30",
        "one_phase",
        1,
        2800,
        {"buy_rate": 0.5032, "monthly_charge": 33.43},
    )
    assert schedule.to_dict() == stored
    t.validate_schedule(schedule)
    distribution_only = {**PREFILL, "offer": None, "consumption_max_kwh": None}
    restored = t.TariffSchedule.from_dict({**stored, "prefill": distribution_only})
    assert restored.to_dict()["prefill"] == distribution_only


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"phases": "two_phase"}, "phases"),
        ({"billing_months": 5}, "billing months"),
        ({"billing_months": True}, "billing months"),
        ({"consumption_max_kwh": -1}, "consumption"),
        ({"consumption_max_kwh": True}, "consumption"),
        ({"consumption_max_kwh": 2800.5}, "consumption"),
        ({"tariff_year": True}, "year"),
        ({"tariff_year": "2026"}, "year"),
        ({"valid_to": "31.12.2026"}, "valid_to"),
        ({"as_of": None}, "as_of"),
        ({"operator": ""}, "operator"),
        ({"offer": 3}, "offer"),
        ({"values": {"buy_rate": True}}, "numbers"),
        ({"values": {"buy_rate": "0.5"}}, "numbers"),
        ({"values": {"buy_rate": float("nan")}}, "numbers"),
        ({"values": [0.5]}, "incomplete"),
    ],
)
def test_validate_prefill_shape(change, message):
    stored = {
        "tariff": "g11",
        "meter_winter_clock": False,
        "params": {},
        "prefill": {**PREFILL, **change},
    }
    with pytest.raises(InputError, match=message):
        t.validate_schedule(t.TariffSchedule.from_dict(stored))


def test_validate_prefill_shape_missing_field():
    prefill = dict(PREFILL)
    del prefill["billing_months"]
    stored = {
        "tariff": "g11",
        "meter_winter_clock": False,
        "params": {},
        "prefill": prefill,
    }
    with pytest.raises(InputError, match="incomplete"):
        t.TariffSchedule.from_dict(stored)


def test_validate_prefill_never_checks_the_snapshot():
    stored = {
        "tariff": "g11",
        "meter_winter_clock": False,
        "params": {},
        "prefill": {**PREFILL, "operator": "gone", "offer": "gone-2019"},
    }
    t.validate_schedule(t.TariffSchedule.from_dict(stored))


def test_older_reader_ignores_prefill():
    stored = {
        "tariff": "g11",
        "meter_winter_clock": False,
        "params": {},
        "prefill": {**PREFILL, "future_field": 1},
        "future_key": "x",
    }
    schedule = t.TariffSchedule.from_dict(stored)
    assert schedule.to_dict()["prefill"] == PREFILL
    assert "future_key" not in schedule.to_dict()
    # An older build knows neither the prefill nor the new keys: only the old three remain.
    older = {key: stored[key] for key in ("tariff", "meter_winter_clock", "params")}
    assert t.TariffSchedule.from_dict(older).prefill is None
