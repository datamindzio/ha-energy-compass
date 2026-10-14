from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_compass.engine import forecast as load_forecast
from custom_components.energy_compass.engine.forecast import forecast_load
from custom_components.energy_compass.engine.models import ForecastSettings, InputError


def stamp(day, hour, month=9, fold=0):
    from zoneinfo import ZoneInfo

    return datetime(2026, month, day, hour, tzinfo=ZoneInfo("Europe/Warsaw"), fold=fold)


def test_local_weekday_hour_mean_and_partial_interval():
    history = ((stamp(15, 10), 1.0), (stamp(16, 10), 3.0))
    slots = (
        (
            stamp(17, 10),
            stamp(
                17,
                10,
            ).replace(minute=30),
        ),
    )
    assert forecast_load(
        history, slots, "Europe/Warsaw", None, ForecastSettings(minimum_samples=2)
    ) == (1.0,)


def test_fallback_requires_value_and_policy():
    slots = ((stamp(17, 10), stamp(17, 11)),)
    assert forecast_load((), slots, "Europe/Warsaw", 24, ForecastSettings()) == (1.0,)
    with pytest.raises(InputError, match="insufficient"):
        forecast_load(
            (), slots, "Europe/Warsaw", 24, ForecastSettings(allow_fallback=False)
        )


def test_lookback_excludes_old_and_future_history():
    history = ((stamp(1, 10), 10.0), (stamp(17, 10), 20.0))
    slots = ((stamp(17, 10), stamp(17, 11)),)
    assert forecast_load(
        history, slots, "Europe/Warsaw", 24, ForecastSettings(lookback_days=14)
    ) == (1.0,)


def test_dst_spring_23_and_autumn_25_distinct_hours():
    spring_slots = (
        (datetime(2026, 3, 28, 23, tzinfo=UTC), datetime(2026, 3, 29, 22, tzinfo=UTC)),
    )
    autumn_slots = (
        (
            datetime(2026, 10, 24, 22, tzinfo=UTC),
            datetime(2026, 10, 25, 23, tzinfo=UTC),
        ),
    )
    assert forecast_load((), spring_slots, "Europe/Warsaw", 24, ForecastSettings()) == (
        23.0,
    )
    assert forecast_load((), autumn_slots, "Europe/Warsaw", 24, ForecastSettings()) == (
        25.0,
    )
    assert stamp(25, 2, 10, 0).astimezone(UTC) != stamp(25, 2, 10, 1).astimezone(UTC)


def test_history_and_fallback_coverage_tracks_same_split_as_values():
    history = ((stamp(15, 10), 1.0), (stamp(16, 10), 3.0))
    slots = ((stamp(17, 10).replace(minute=30), stamp(17, 11).replace(minute=30)),)
    result = load_forecast.forecast_load_with_quality(
        history, slots, "Europe/Warsaw", 24, ForecastSettings(minimum_samples=2)
    )
    assert (
        result.values
        == forecast_load(
            history, slots, "Europe/Warsaw", 24, ForecastSettings(minimum_samples=2)
        )
        == (1.5,)
    )
    assert [row.method for row in result.coverage] == ["history", "fallback"]
    assert [
        (row.samples_available, row.samples_required) for row in result.coverage
    ] == [(2, 2), (0, 2)]
    assert [row.end - row.start for row in result.coverage] == [
        timedelta(minutes=30)
    ] * 2


def test_empty_history_reports_zero_samples_even_when_fallback_energy_zero():
    now = datetime(2026, 9, 17, 10, tzinfo=UTC)
    slots = (
        (now, now + timedelta(minutes=15)),
        (now + timedelta(minutes=15), now + timedelta(hours=2)),
    )
    result = load_forecast.forecast_load_with_quality(
        (), slots, "UTC", 0, ForecastSettings(minimum_samples=3)
    )
    assert result.values == (0, 0)
    assert all(row.method == "fallback" for row in result.coverage)
    assert all(
        row.samples_available == 0 and row.samples_required == 3
        for row in result.coverage
    )
    assert sum(
        (row.end - row.start for row in result.coverage), timedelta()
    ) == timedelta(hours=2)
    with pytest.raises(InputError, match="2026-09-17T10:00:00.*0/3"):
        load_forecast.forecast_load_with_quality(
            (),
            slots,
            "UTC",
            0,
            ForecastSettings(minimum_samples=3, allow_fallback=False),
        )


@pytest.mark.parametrize(
    "start,end,hours",
    [
        (
            datetime(2026, 3, 28, 23, tzinfo=UTC),
            datetime(2026, 3, 29, 22, tzinfo=UTC),
            23,
        ),
        (
            datetime(2026, 10, 24, 22, tzinfo=UTC),
            datetime(2026, 10, 25, 23, tzinfo=UTC),
            25,
        ),
    ],
)
def test_dst_coverage_uses_elapsed_utc_duration(start, end, hours):
    result = load_forecast.forecast_load_with_quality(
        (), ((start, end),), "Europe/Warsaw", 24, ForecastSettings()
    )
    assert result.values == (float(hours),)
    assert len(result.coverage) == hours
    assert sum(
        (row.end - row.start for row in result.coverage), timedelta()
    ) == timedelta(hours=hours)


def _same_hour_history(days_ago_values):
    """Hourly 10:00 samples on Mon-Fri, keyed by days before the Monday 21 Sep slot."""
    return tuple(
        (stamp(21 - days, 10), value) for days, value in days_ago_values.items()
    )


def test_half_life_off_is_the_plain_mean():
    history = _same_hour_history({7: 1.0, 14: 1.0, 3: 3.0})
    slots = ((stamp(21, 10), stamp(21, 11)),)
    plain = forecast_load(history, slots, "Europe/Warsaw", None, ForecastSettings())
    explicit = forecast_load(
        history, slots, "Europe/Warsaw", None, ForecastSettings(half_life_days=0.0)
    )
    assert plain == explicit == (pytest.approx(5.0 / 3),)


def test_half_life_pulls_the_profile_towards_the_recent_level():
    # Three weeks at 1.0 kWh, then the last three days at 2.0 kWh (a level change).
    history = _same_hour_history({16: 1.0, 15: 1.0, 14: 1.0, 3: 2.0, 2: 2.0, 1: 2.0})
    slots = ((stamp(21, 10), stamp(21, 11)),)

    def forecast(half_life):
        return forecast_load(
            history,
            slots,
            "Europe/Warsaw",
            None,
            ForecastSettings(lookback_days=28, half_life_days=half_life),
        )[0]

    plain, slow, fast = forecast(0), forecast(10), forecast(2)
    assert plain == pytest.approx(1.5)
    assert plain < slow < fast < 2.0


def test_half_life_weight_halves_per_half_life():
    # Ages 3 and 6 days at half-life 3: weights 0.5 and 0.25, so (3*0.5 + 0*0.25) / 0.75.
    history = _same_hour_history({3: 3.0, 6: 0.0})
    slots = ((stamp(21, 10), stamp(21, 11)),)
    value = forecast_load(
        history,
        slots,
        "Europe/Warsaw",
        None,
        ForecastSettings(half_life_days=3.0),
    )[0]
    assert value == pytest.approx(2.0)


def test_half_life_keeps_sample_count_and_coverage():
    history = _same_hour_history({3: 3.0, 6: 0.0})
    slots = ((stamp(21, 10), stamp(21, 11)),)
    from custom_components.energy_compass.engine.forecast import (
        forecast_load_with_quality,
    )

    result = forecast_load_with_quality(
        history,
        slots,
        "Europe/Warsaw",
        None,
        ForecastSettings(half_life_days=3.0, minimum_samples=2),
    )
    assert [(c.method, c.samples_available) for c in result.coverage] == [
        ("history", 2)
    ]


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_half_life_rejects_invalid_values(bad):
    slots = ((stamp(21, 10), stamp(21, 11)),)
    with pytest.raises(InputError, match="half-life"):
        forecast_load(
            (), slots, "Europe/Warsaw", 24, ForecastSettings(half_life_days=bad)
        )
