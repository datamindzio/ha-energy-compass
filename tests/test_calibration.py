import json
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass import calibration
from custom_components.energy_compass.calibration import Reading
from custom_components.energy_compass.engine.models import Slot

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(days=30)


def feed(state, key, values, step=timedelta(hours=1), start=T0, now=NOW):
    """One reading per step; None skips the report, the clock still advances."""
    for index, value in enumerate(values):
        if value is not None:
            calibration.observe(state, {key: Reading(start + index * step, value)}, now)


def test_round_trip_of_synthetic_counters():
    state = calibration.empty_state()
    hours = 29 * 24
    # 1.0 kWh in and 0.9 kWh out every hour, cumulative, offset by a start value.
    feed(state, "charge", [100 + h * 1.0 for h in range(hours + 1)])
    feed(state, "discharge", [50 + h * 0.9 for h in range(hours + 1)])
    report = calibration.round_trip_report(state, NOW, 0.95)
    assert report["status"] == "ok"
    assert report["covered_hours"] == hours
    assert report["charge_kwh"] == hours
    assert report["measured"] == 0.9
    assert report["configured"] == 0.95
    assert report["difference"] == -0.05


def test_round_trip_interpolates_between_off_boundary_readings():
    state = calibration.empty_state()
    # Readings every 20 minutes, so no reading lands on an hour start.
    step = timedelta(minutes=20)
    count = 29 * 24 * 3
    feed(state, "charge", [h * 1 / 3 for h in range(1, count)], step, T0 + step)
    feed(state, "discharge", [h * 0.3 / 3 for h in range(1, count)], step, T0 + step)
    report = calibration.round_trip_report(state, NOW, None)
    assert report["measured"] == 0.3
    assert report["difference"] is None


def test_round_trip_needs_a_week_and_some_energy():
    state = calibration.empty_state()
    feed(state, "charge", [h * 1.0 for h in range(48)])
    feed(state, "discharge", [h * 0.9 for h in range(48)])
    report = calibration.round_trip_report(state, NOW, 0.95)
    assert report["status"] == "insufficient_data"
    assert report["measured"] is None
    assert report["covered_hours"] == 47
    idle = calibration.empty_state()
    feed(idle, "charge", [5.0] * (10 * 24))
    feed(idle, "discharge", [4.0] * (10 * 24))
    assert calibration.round_trip_report(idle, NOW, 0.95)["measured"] is None


def test_round_trip_survives_missing_hours_and_a_counter_reset():
    state = calibration.empty_state()
    hours = 29 * 24
    charge = [100 + h * 1.0 for h in range(hours + 1)]
    discharge = [50 + h * 0.9 for h in range(hours + 1)]
    # Reset both counters to near zero at hour 300 (inverter replaced).
    for h in range(300, hours + 1):
        charge[h] = (h - 300) * 1.0
        discharge[h] = (h - 300) * 0.9
    # The discharge counter has no reports for hours 100..130.
    for h in range(101, 130):
        discharge[h] = None
    feed(state, "charge", charge)
    feed(state, "discharge", discharge)
    report = calibration.round_trip_report(state, NOW, 0.95)
    assert report["status"] == "ok"
    # Hours around the reset and around the silent stretch are dropped, never
    # negative or invented: the ratio is still the real 0.9.
    assert report["measured"] == 0.9
    # 30 hours around the silent stretch, the hour that spans the reset and the
    # hour after it (the first reading of a reset counter is not trusted).
    assert report["covered_hours"] == hours - 30 - 2
    assert report["charge_kwh"] > 0


def test_idle_counter_is_exact_across_a_long_gap_but_a_moving_one_is_not():
    state = calibration.empty_state()
    calibration.observe(state, {"pv": Reading(T0, 10.0)}, NOW)
    calibration.observe(state, {"pv": Reading(T0 + timedelta(hours=10), 10.0)}, NOW)
    assert state["hours"][(T0 + timedelta(hours=5)).isoformat()]["pv"] == 10.0
    calibration.observe(state, {"pv": Reading(T0 + timedelta(hours=15), 12.0)}, NOW)
    assert (T0 + timedelta(hours=12)).isoformat() not in state["hours"]


def test_stale_or_repeated_reading_is_ignored():
    state = calibration.empty_state()
    at = T0 + timedelta(minutes=30)
    assert not calibration.observe(state, {"pv": Reading(at, 1.0)}, NOW)
    calibration.observe(state, {"pv": Reading(at + timedelta(hours=1), 2.0)}, NOW)
    before = json.dumps(state, sort_keys=True)
    calibration.observe(state, {"pv": Reading(at, 9.0)}, NOW)
    assert json.dumps(state, sort_keys=True) == before


def _slot(start, hours, pv):
    return Slot(start, start + timedelta(hours=hours), 0.0, 0.0, pv, 0.0)


def test_forecast_hours_scale_the_remainder_to_a_full_hour():
    now = T0 + timedelta(minutes=30)
    slots = (
        Slot(now, T0 + timedelta(hours=1), 0, 0, 1.0, 0),
        _slot(T0 + timedelta(hours=1), 1, 3.0),
    )
    rows = calibration.forecast_hours(slots, now, hours=3)
    assert rows[0] == {
        "hour_start": T0.isoformat(),
        "kwh_hour": 2.0,
        "fraction": 0.5,
    }
    assert rows[1]["kwh_hour"] == 3.0
    assert len(rows) == 2


def test_record_forecast_keeps_the_fullest_plan_for_the_current_hour():
    state = calibration.empty_state()
    now = T0 + timedelta(minutes=10)
    row = {"hour_start": T0.isoformat(), "kwh_hour": 2.0, "fraction": 0.8}
    assert calibration.record_forecast(state, [row], now)
    later = {**row, "kwh_hour": 9.0, "fraction": 0.4}
    assert not calibration.record_forecast(state, [later], now)
    assert state["forecast"][T0.isoformat()] == [2.0, 0.8]
    # Other hours than the current one are not recorded.
    future = {**row, "hour_start": (T0 + timedelta(hours=1)).isoformat()}
    assert not calibration.record_forecast(state, [future, "junk", {}], now)


def test_pv_accuracy_total_and_per_local_hour_with_missing_hours():
    state = calibration.empty_state()
    start = NOW - timedelta(days=3)
    zone = "Europe/Warsaw"
    hours = 72
    # Forecast 1.0 kWh every hour; real 0.5 kWh each hour at 08:00 UTC and 2.0
    # at 10:00 UTC, 1.0 otherwise.
    counter = 1000.0
    values = []
    for h in range(hours + 1):
        values.append(counter)
        hour = (start + timedelta(hours=h)).hour
        counter += 0.5 if hour == 8 else 2.0 if hour == 10 else 1.0
    feed(state, "pv", values, start=start)
    for h in range(hours):
        at = start + timedelta(hours=h)
        # Hour 5 has no recorded forecast.
        if h != 5:
            calibration.record_forecast(
                state,
                [{"hour_start": at.isoformat(), "kwh_hour": 1.0, "fraction": 1.0}],
                at,
            )
    report = calibration.pv_accuracy_report(state, NOW, zone)
    assert report["status"] == "ok"
    assert report["covered_hours"] == hours - 1
    # 08:00 UTC is 10:00 local (CEST) and 10:00 UTC is 12:00 local.
    assert report["by_local_hour"]["10"]["ratio"] == 0.5
    assert report["by_local_hour"]["12"]["ratio"] == 2.0
    assert report["by_local_hour"]["09"]["ratio"] == 1.0
    assert report["by_local_hour"]["10"]["hours"] == 3
    total = report["measured_kwh"] / report["forecast_kwh"]
    assert report["ratio"] == round(total, 3)


def test_pv_accuracy_drops_the_hours_across_a_counter_reset():
    state = calibration.empty_state()
    start = NOW - timedelta(days=2)
    values = [500 + h for h in range(30)] + [h for h in range(19)]
    feed(state, "pv", values, start=start)
    for h in range(48):
        at = start + timedelta(hours=h)
        calibration.record_forecast(
            state,
            [{"hour_start": at.isoformat(), "kwh_hour": 1.0, "fraction": 1.0}],
            at,
        )
    report = calibration.pv_accuracy_report(state, NOW, "UTC")
    # 48 hours of readings, minus the hour that spans the reset and the next.
    assert report["covered_hours"] == 46
    assert report["ratio"] == 1.0


def test_store_is_bounded_and_round_trips_through_json():
    state = calibration.empty_state()
    now = T0
    # A year of hourly readings of two counters and forecasts.
    for hour in range(365 * 24):
        at = T0 + timedelta(hours=hour)
        now = at
        calibration.observe(
            state,
            {"pv": Reading(at, hour * 1.0), "charge": Reading(at, hour * 0.5)},
            at,
        )
        calibration.record_forecast(
            state,
            [{"hour_start": at.isoformat(), "kwh_hour": 1.0, "fraction": 1.0}],
            at,
        )
    assert len(state["hours"]) <= 31 * 24 + 24
    assert len(state["forecast"]) <= 15 * 24 + 24
    restored = json.loads(json.dumps(state))
    assert calibration.valid_state(restored)
    assert calibration.pv_accuracy_report(restored, now, "UTC") == (
        calibration.pv_accuracy_report(state, now, "UTC")
    )


def test_invalid_stored_state_is_rejected():
    assert calibration.valid_state(calibration.empty_state())
    for bad in (
        None,
        [],
        {},
        {"last": {}, "hours": {}},
        {"last": {"x": ["2026-09-01T00:00:00+00:00", 1]}, "hours": {}, "forecast": {}},
        {"last": {}, "hours": {"2026-09-01T00:00:00": {"pv": 1}}, "forecast": {}},
        {
            "last": {},
            "hours": {"2026-09-01T00:00:00+00:00": {"pv": "a"}},
            "forecast": {},
        },
        {"last": {}, "hours": {}, "forecast": {"2026-09-01T00:00:00+00:00": [1]}},
        {
            "last": {"pv": ["2026-09-01T00:00:00+00:00", 1.0]},
            "hours": {},
            "forecast": {},
        },
        {
            "last": {
                "pv": [
                    "2026-09-01T00:00:00+00:00",
                    1.0,
                    "s",
                    "yes",
                    "2026-09-01T00:00:00+00:00",
                ]
            },
            "hours": {},
            "forecast": {},
        },
    ):
        assert not calibration.valid_state(bad)
    kept = {
        "last": {
            "pv": [
                "2026-09-01T00:00:00+00:00",
                1.0,
                "sensor.p||1.0|kWh",
                False,
                "2026-08-31T23:00:00+00:00",
            ]
        },
        "hours": {},
        "forecast": {},
    }
    assert calibration.valid_state(kept)


def test_configured_blocks_and_counter_reading():
    from custom_components.energy_compass.config_models import NumericSetting
    from custom_components.energy_compass.settings import default_configuration
    from custom_components.energy_compass.sources.bindings import EntityBinding

    config = default_configuration("EUR", "UTC")
    assert calibration.configured_blocks(config) == (False, False)

    def counter(entity, unit="kWh", scale=1.0):
        return NumericSetting(
            entity=EntityBinding(entity),
            unit="kWh",
            source_unit=unit,
            multiplier=scale,
        ).to_dict()

    config["sources"]["battery_enabled"] = True
    config["measurements"]["battery_charge_energy"] = counter("sensor.c")
    assert calibration.configured_blocks(config) == (False, False)
    config["measurements"]["battery_discharge_energy"] = counter(
        "sensor.d", "Wh", 0.001
    )
    assert calibration.configured_blocks(config) == (True, False)
    config["measurements"]["pv_energy"] = {**counter("sensor.p"), "statistic_id": "x"}
    assert calibration.configured_blocks(config)[1] is False
    config["measurements"]["pv_energy"] = counter("sensor.p")
    config["sources"]["pv"]["enabled"] = True
    assert calibration.configured_blocks(config) == (True, True)
    config["sources"]["battery_enabled"] = False
    assert calibration.configured_blocks(config) == (False, True)

    stamp = NOW - timedelta(minutes=3)
    states = {
        "sensor.c": {
            "state": "12.5",
            "attributes": {"unit_of_measurement": "kWh"},
            "last_updated": NOW.isoformat(),
            "last_reported": stamp.isoformat(),
        },
        "sensor.d": {
            "state": "9000",
            "attributes": {"unit_of_measurement": "Wh"},
            "last_updated": NOW.isoformat(),
        },
        "sensor.p": {
            "state": "unavailable",
            "attributes": {"unit_of_measurement": "kWh"},
            "last_updated": NOW.isoformat(),
        },
    }
    readings = calibration.read_counters(config, states, NOW)
    assert readings == {
        "charge": Reading(stamp, 12.5, "sensor.c||1.0|kWh", NOW),
        "discharge": Reading(NOW, 9.0, "sensor.d||0.001|Wh", NOW),
    }
    # An unchanged counter is held only for the role's maximum age.
    config["measurements"]["battery_charge_energy"]["max_age_seconds"] = 60
    held = calibration.read_counters(config, states, NOW)["charge"]
    assert held.until == stamp + timedelta(seconds=60)
    config["measurements"]["battery_charge_energy"]["max_age_seconds"] = None
    assert calibration.read_counters(config, states, NOW)["charge"].until == NOW


MINUTE = timedelta(minutes=1)
POLL_OFFSET = timedelta(seconds=30)  # polling never lands on a full hour


def _minute_polled(days=9, glitch=None, source="", start=T0 + POLL_OFFSET):
    """1 kWh/h in and 0.9 kWh/h out, polled every minute; glitch = (index, kWh)."""
    state = calibration.empty_state()
    for index in range(days * 24 * 60):
        at = start + index * MINUTE
        charge = 5000 + index / 60
        if glitch and index == glitch[0]:
            charge = glitch[1]
        calibration.observe(
            state,
            {
                "charge": Reading(at, charge, source),
                "discharge": Reading(at, 4000 + 0.9 * index / 60, source),
            },
            NOW,
        )
    return state


def _clean_hours():
    return calibration.round_trip_report(_minute_polled(), NOW, 0.95)["covered_hours"]


@pytest.mark.parametrize(
    "glitch",
    [
        # One zero reading on the last poll before an hour, counter back next poll.
        (3 * 24 * 60 + 12 * 60 + 59, 0.0),
        # One absurd reading on the first poll after an hour.
        (3 * 24 * 60 + 13 * 60, 65535.0),
        # A small dip in the middle of an hour.
        (3 * 24 * 60 + 12 * 60 + 30, 4990.0),
    ],
)
def test_one_glitch_reading_drops_hours_but_never_invents_energy(glitch):
    report = calibration.round_trip_report(_minute_polled(glitch=glitch), NOW, 0.95)
    assert report["status"] == "ok"
    # The ratio stays the real one; only the hours around the glitch are lost.
    assert report["measured"] == 0.9
    assert _clean_hours() - 3 <= report["covered_hours"] <= _clean_hours()
    assert report["charge_kwh"] == pytest.approx(report["covered_hours"])


def test_down_step_and_the_segment_that_recovers_from_it_derive_no_boundary():
    state = calibration.empty_state()
    for minutes, value in ((10, 5.0), (50, 0.0), (70, 5.5), (80, 5.6)):
        calibration.observe(state, {"pv": Reading(T0 + minutes * MINUTE, value)}, NOW)
    # The step down, the recovery across 01:00 and nothing else.
    assert T0.isoformat() not in state["hours"]
    assert (T0 + timedelta(hours=1)).isoformat() not in state["hours"]
    calibration.observe(state, {"pv": Reading(T0 + timedelta(hours=2), 6.6)}, NOW)
    assert state["hours"][(T0 + timedelta(hours=2)).isoformat()]["pv"] == 6.6


def test_rise_faster_than_any_device_delivers_is_rejected():
    one = (T0 + timedelta(hours=1)).isoformat()
    state = calibration.empty_state()
    calibration.observe(state, {"pv": Reading(T0 + 30 * MINUTE, 5.0)}, NOW)
    # 4995 kWh in an hour across 01:00 is a glitch, not energy.
    calibration.observe(state, {"pv": Reading(T0 + 90 * MINUTE, 5000.0)}, NOW)
    assert state["hours"] == {}
    # The same, landing exactly on the hour: the reading is not a boundary.
    glitch = calibration.empty_state()
    calibration.observe(glitch, {"pv": Reading(T0 + 30 * MINUTE, 5.0)}, NOW)
    calibration.observe(glitch, {"pv": Reading(T0 + 60 * MINUTE, 65535.0)}, NOW)
    assert glitch["hours"] == {}
    # A real 3 kWh/h is nowhere near the bound.
    real = calibration.empty_state()
    calibration.observe(real, {"pv": Reading(T0 + 30 * MINUTE, 5.0)}, NOW)
    calibration.observe(real, {"pv": Reading(T0 + 90 * MINUTE, 8.0)}, NOW)
    assert real["hours"][one]["pv"] == pytest.approx(6.5)
    # An absurd hour delta between two accepted boundaries is dropped too.
    odd = calibration.empty_state()
    odd["hours"] = {
        T0.isoformat(): {"pv": 5.0},
        one: {"pv": 5000.0},
    }
    assert calibration._delta(odd, T0, "pv") is None


def test_rebinding_a_counter_starts_its_history_over():
    # Two days of a daily counter, then the lifetime counter takes its place
    # while polling is off the hour; the other counter keeps its history.
    state = calibration.empty_state()
    rebound = T0 + POLL_OFFSET + 2 * 24 * 60 * MINUTE
    for index in range(10 * 24 * 60):
        at = T0 + POLL_OFFSET + index * MINUTE
        daily = at < rebound
        calibration.observe(
            state,
            {
                "charge": Reading(
                    at,
                    3.0 + index / 60 if daily else 5000 + index / 60,
                    "sensor.daily" if daily else "sensor.lifetime",
                ),
                "discharge": Reading(at, 4000 + 0.9 * index / 60, "sensor.out"),
            },
            NOW,
        )
    report = calibration.round_trip_report(state, NOW, 0.95)
    assert report["status"] == "ok"
    assert report["measured"] == 0.9
    # Nothing of the daily counter survives; the discharge history does.
    assert all(
        "charge" not in row
        for stamp, row in state["hours"].items()
        if datetime.fromisoformat(stamp) < rebound
    )
    assert any(
        "discharge" in row
        for stamp, row in state["hours"].items()
        if datetime.fromisoformat(stamp) < rebound
    )
    assert state["last"]["charge"][2] == "sensor.lifetime"
    assert report["covered_hours"] <= 8 * 24
    assert report["charge_kwh"] == pytest.approx(report["covered_hours"])


def test_source_identity_covers_attribute_multiplier_and_unit():
    from custom_components.energy_compass.config_models import NumericSetting
    from custom_components.energy_compass.sources.bindings import EntityBinding

    def identity(entity="sensor.c", attribute=None, multiplier=1.0, unit="kWh"):
        return calibration._identity(
            NumericSetting(
                entity=EntityBinding(entity, attribute=attribute),
                unit="kWh",
                source_unit=unit,
                multiplier=multiplier,
            )
        )

    base = identity()
    assert identity() == base
    assert len({base, identity("sensor.d"), identity(attribute="total")}) == 3
    assert len({base, identity(multiplier=0.001), identity(unit="Wh")}) == 3


def _change_only(days=9, max_age=timedelta(hours=24)):
    """Counters that write state only when they move: charge 10-14 UTC at 1 kWh/h,
    discharge 18-24 UTC at 0.6 kWh/h, both idle otherwise; reported at last change."""
    state = calibration.empty_state()
    charge, discharge = 5000.0, 4000.0
    changed = {"charge": T0, "discharge": T0}
    # Three more idle hours: an idle counter is covered once it has been
    # unchanged for longer than the interpolation gap.
    for index in range((days * 24 + 3) * 60):
        at = T0 + index * MINUTE
        if 10 <= at.hour < 14:
            charge += 1 / 60
            changed["charge"] = at
        if 18 <= at.hour:
            discharge += 0.6 / 60
            changed["discharge"] = at

        charge_until = min(at, changed["charge"] + max_age)
        discharge_until = min(at, changed["discharge"] + max_age)
        calibration.observe(
            state,
            {
                "charge": Reading(changed["charge"], charge, "", charge_until),
                "discharge": Reading(
                    changed["discharge"], discharge, "", discharge_until
                ),
            },
            NOW,
        )
    return state


def test_counters_that_write_only_on_change_still_cover_their_idle_hours():
    report = calibration.round_trip_report(_change_only(), NOW, 0.95)
    assert report["status"] == "ok"
    assert report["measured"] == 0.9
    # Idle hours count: every hour of the nine days is covered, not only the
    # ones in which a counter moved (4 kWh charged and 3.6 discharged a day).
    assert report["covered_hours"] >= 9 * 24
    assert report["charge_kwh"] == pytest.approx(36.0)
    assert report["discharge_kwh"] == pytest.approx(32.4)


def test_unchanged_counter_holds_until_its_maximum_age_then_the_hours_drop():
    state = calibration.empty_state()
    hour = timedelta(hours=1)
    calibration.observe(state, {"pv": Reading(T0, 10.0, "", T0 + 30 * MINUTE)}, NOW)
    # No new report, the source is idle: the value holds up to the age limit.
    calibration.observe(state, {"pv": Reading(T0, 10.0, "", T0 + 3 * hour)}, NOW)
    for offset in (1, 2, 3):
        assert state["hours"][(T0 + offset * hour).isoformat()]["pv"] == 10.0
    # Past the limit the source is frozen, not idle: until stops advancing.
    calibration.observe(state, {"pv": Reading(T0, 10.0, "", T0 + 3 * hour)}, NOW)
    assert (T0 + 4 * hour).isoformat() not in state["hours"]
    # It moves again 5 hours later: the silence yields no boundary, the new
    # reading itself is one.
    calibration.observe(
        state, {"pv": Reading(T0 + 8 * hour, 10.5, "", T0 + 8 * hour)}, NOW
    )
    assert [stamp for stamp in state["hours"] if stamp > T0.isoformat()] == [
        (T0 + offset * hour).isoformat() for offset in (1, 2, 3, 8)
    ]
    assert state["hours"][(T0 + 8 * hour).isoformat()]["pv"] == 10.5


def test_pv_accuracy_needs_a_day_of_hours():
    state = calibration.empty_state()
    start = NOW - timedelta(hours=10)
    feed(state, "pv", [float(h) for h in range(11)], start=start)
    for h in range(10):
        at = start + timedelta(hours=h)
        calibration.record_forecast(
            state,
            [{"hour_start": at.isoformat(), "kwh_hour": 1.0, "fraction": 1.0}],
            at,
        )
    report = calibration.pv_accuracy_report(state, NOW, "UTC")
    assert report["status"] == "insufficient_data"
    assert report["covered_hours"] == 10
    assert report["minimum_hours"] == 24
    assert report["ratio"] is None
    assert all(row["ratio"] is None for row in report["by_local_hour"].values())
    assert report["forecast_kwh"] == 10.0
