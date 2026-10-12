import json
from datetime import UTC, datetime, timedelta

from custom_components.energy_compass import calibration
from custom_components.energy_compass.engine.models import Slot

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(days=30)


def feed(state, key, values, step=timedelta(hours=1), start=T0, now=NOW):
    """One reading per step; None skips the report, the clock still advances."""
    for index, value in enumerate(values):
        if value is not None:
            calibration.observe(state, {key: (start + index * step, value)}, now)


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
    # 30 hours around the silent stretch and the one hour that spans the reset.
    assert report["covered_hours"] == hours - 30 - 1
    assert report["charge_kwh"] > 0


def test_idle_counter_is_exact_across_a_long_gap_but_a_moving_one_is_not():
    state = calibration.empty_state()
    calibration.observe(state, {"pv": (T0, 10.0)}, NOW)
    calibration.observe(state, {"pv": (T0 + timedelta(hours=10), 10.0)}, NOW)
    assert state["hours"][(T0 + timedelta(hours=5)).isoformat()]["pv"] == 10.0
    calibration.observe(state, {"pv": (T0 + timedelta(hours=15), 12.0)}, NOW)
    assert (T0 + timedelta(hours=12)).isoformat() not in state["hours"]


def test_stale_or_repeated_reading_is_ignored():
    state = calibration.empty_state()
    at = T0 + timedelta(minutes=30)
    assert not calibration.observe(state, {"pv": (at, 1.0)}, NOW)
    calibration.observe(state, {"pv": (at + timedelta(hours=1), 2.0)}, NOW)
    before = json.dumps(state, sort_keys=True)
    calibration.observe(state, {"pv": (at, 9.0)}, NOW)
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
    start = NOW - timedelta(days=1)
    values = [500 + h for h in range(10)] + [h for h in range(15)]
    feed(state, "pv", values, start=start)
    for h in range(24):
        at = start + timedelta(hours=h)
        calibration.record_forecast(
            state,
            [{"hour_start": at.isoformat(), "kwh_hour": 1.0, "fraction": 1.0}],
            at,
        )
    report = calibration.pv_accuracy_report(state, NOW, "UTC")
    # 24 hours of readings, minus the hour that spans the reset.
    assert report["covered_hours"] == 23
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
            {"pv": (at, hour * 1.0), "charge": (at, hour * 0.5)},
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
    ):
        assert not calibration.valid_state(bad)


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
    assert readings == {"charge": (stamp, 12.5), "discharge": (NOW, 9.0)}
