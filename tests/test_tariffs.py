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
)

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
    assert tuple(t.CATALOG) == KEYS


@pytest.mark.parametrize("key", KEYS)
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
