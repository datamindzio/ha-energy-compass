from copy import deepcopy
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass.config_models import (
    PvSource,
    SolarForecastBinding,
    SourceConfig,
    validate_sources,
)
from custom_components.energy_compass.engine.models import InputError
from custom_components.energy_compass.runtime import (
    async_fetch_solar_forecast,
    async_history,
    build_problem,
)
from custom_components.energy_compass.settings import default_configuration
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)
from custom_components.energy_compass.sources.solar_forecast import (
    solar_forecast_intervals,
)

PLATFORMS = "homeassistant.components.energy.websocket_api.async_get_energy_platforms"


def kwh(rows):
    return [round(row.value, 6) for row in rows]


def hour(rows):
    return [
        (row.start.astimezone(UTC).isoformat(), row.end.astimezone(UTC).isoformat())
        for row in rows
    ]


def test_solcast_shaped_forecast_buckets_by_local_hour():
    now = datetime(2026, 9, 18, 10, 20, tzinfo=UTC)
    wh = {
        "2026-09-15T12:00:00+02:00": "garbage",
        "2026-09-16T12:00:00+02:00": 111,
        "2026-09-17T12:00:00+02:00": 222,
        "2026-09-18T11:30:00+02:00": 999,
        "2026-09-18T12:00:00+02:00": 400,
        "2026-09-18T12:30:00+02:00": 600,
        "2026-09-18T13:00:00+02:00": 500,
        "2026-09-18T13:30:00+02:00": 300,
        "2026-09-18T17:00:00+02:00": 100,
        "2026-09-18T17:30:00+02:00": 50,
        "2026-09-18T18:00:00+02:00": 0,
        "2026-09-19T06:00:00+02:00": 20,
        "2026-09-19T06:30:00+02:00": 80,
        "2026-09-19T07:00:00+02:00": 300,
        "2026-09-20T15:00:00+02:00": 7000,
    }
    rows = solar_forecast_intervals(wh, now, "Europe/Warsaw")
    assert len(rows) == 20
    assert rows[0].start == datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
    assert rows[-1].end == datetime(2026, 9, 19, 6, 0, tzinfo=UTC)
    assert kwh(rows) == [
        1.0,
        0.8,
        0.0,
        0.0,
        0.0,
        0.15,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.1,
        0.3,
    ]
    assert all(first.end == second.start for first, second in pairwise(rows))


def test_open_meteo_shaped_hourly_zeros_are_kept():
    now = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
    wh = {
        "2026-09-18T12:00:00+02:00": 0,
        "2026-09-18T13:00:00+02:00": 250.5,
        "2026-09-18T14:00:00+02:00": 0,
    }
    assert kwh(solar_forecast_intervals(wh, now, "Europe/Warsaw")) == [
        0.0,
        0.2505,
        0.0,
    ]


def test_forecast_solar_shaped_irregular_keys_follow_the_containing_hour():
    now = datetime(2026, 9, 18, 22, 30, tzinfo=UTC)
    wh = {
        "2026-09-19T00:00:00+02:00": 0,
        "2026-09-19T05:48:00+02:00": 120,
        "2026-09-19T07:15:00+02:00": 900,
        "2026-09-19T07:45:00+02:00": 1100,
    }
    rows = solar_forecast_intervals(wh, now, "Europe/Warsaw")
    assert kwh(rows) == [0.0, 0.0, 0.0, 0.0, 0.0, 0.12, 0.0, 2.0]
    assert rows[0].start == datetime(2026, 9, 18, 22, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "first", "count", "local_hours"),
    [
        (
            datetime(2026, 3, 28, 23, 0, tzinfo=UTC),
            datetime(2026, 3, 28, 23, 0, tzinfo=UTC),
            23,
            [0, 1, *range(3, 24)],
        ),
        (
            datetime(2026, 10, 24, 22, 0, tzinfo=UTC),
            datetime(2026, 10, 24, 22, 0, tzinfo=UTC),
            25,
            [*range(3), 2, *range(3, 24)],
        ),
    ],
)
def test_dst_days_have_contiguous_utc_hours(now, first, count, local_hours):
    wh = {(first + timedelta(hours=index)).isoformat(): 1000 for index in range(count)}
    rows = solar_forecast_intervals(wh, now, "Europe/Warsaw")
    assert len(rows) == count
    assert rows[0].start == first
    assert all(row.end - row.start == timedelta(hours=1) for row in rows)
    assert all(a.end == b.start for a, b in pairwise(rows))
    assert [row.start.astimezone(ZoneInfo("Europe/Warsaw")).hour for row in rows] == (
        local_hours
    )
    assert kwh(rows) == [1.0] * count


def test_half_hour_zone_buckets_start_at_half_past_utc():
    now = datetime(2026, 9, 18, 10, 20, tzinfo=UTC)
    wh = {
        "2026-09-18T10:00:00+00:00": 1000,
        "2026-09-18T10:30:00+00:00": 500,
    }
    rows = solar_forecast_intervals(wh, now, "Asia/Kolkata")
    assert hour(rows) == [
        ("2026-09-18T09:30:00+00:00", "2026-09-18T10:30:00+00:00"),
        ("2026-09-18T10:30:00+00:00", "2026-09-18T11:30:00+00:00"),
    ]
    assert kwh(rows) == [1.0, 0.5]


def test_horizon_ignores_keys_from_forty_nine_hours_on():
    now = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
    wh = {
        "2026-09-18T10:00:00+00:00": 1000,
        (now + timedelta(hours=48, minutes=30)).isoformat(): 2000,
        (now + timedelta(hours=49)).isoformat(): 9000,
    }
    rows = solar_forecast_intervals(wh, now, "UTC")
    assert len(rows) == 49
    assert rows[-1].value == 2.0
    assert rows[-1].end == now + timedelta(hours=49)


@pytest.mark.parametrize(
    ("wh", "message"),
    [
        (
            {"2026-09-18T12:00:00": 1},
            "Energy solar forecast timestamp must be timezone-aware",
        ),
        ({"not-a-date": 1}, "Energy solar forecast timestamp invalid"),
        (
            {"2026-09-18T12:00:00+02:00": -1},
            "Energy solar forecast value invalid",
        ),
        (
            {"2026-09-18T12:00:00+02:00": float("nan")},
            "Energy solar forecast value invalid",
        ),
        (
            {"2026-09-18T12:00:00+02:00": "abc"},
            "Energy solar forecast value invalid",
        ),
        (
            {"2026-09-18T11:59:00+02:00": 5},
            "Energy solar forecast has no current or future values",
        ),
        ({}, "Energy solar forecast has no current or future values"),
    ],
)
def test_invalid_forecasts_raise_exact_messages(wh, message):
    now = datetime(2026, 9, 18, 10, 20, tzinfo=UTC)
    with pytest.raises(InputError) as raised:
        solar_forecast_intervals(wh, now, "Europe/Warsaw")
    assert str(raised.value) == message


def test_naive_now_and_bad_zone_are_rejected():
    with pytest.raises(InputError):
        solar_forecast_intervals(
            {"2026-09-18T12:00:00+02:00": 1},
            datetime.fromisoformat("2026-09-18T00:00:00"),
            "UTC",
        )
    with pytest.raises(InputError):
        solar_forecast_intervals(
            {"2026-09-18T12:00:00+02:00": 1},
            datetime(2026, 9, 18, tzinfo=UTC),
            "Nowhere/Land",
        )


# --- model -------------------------------------------------------------------


def pv_config(**pv):
    config = default_configuration("PLN", "Europe/Warsaw")
    config["sources"]["pv"].update(enabled=True, **pv)
    return config


def validate(config):
    validate_sources(SourceConfig.from_dict(config["sources"]), set())


def test_legacy_pv_dicts_round_trip_byte_identically():
    for data in (
        {"enabled": False, "arrays": []},
        {
            "enabled": True,
            "arrays": [
                [IntervalBinding(EntityBinding("sensor.fx_a"), unit="kW").to_dict()]
            ],
        },
    ):
        assert PvSource.from_dict(data).to_dict() == data
    assert "solar_forecasts" not in PvSource(True).to_dict()


def test_solar_forecasts_round_trip():
    data = {
        "enabled": True,
        "arrays": [],
        "solar_forecasts": [
            {"config_entry_id": "fx0001", "domain": "fx_forecast"},
            {"config_entry_id": "fx0002", "domain": "fx_other"},
        ],
    }
    pv = PvSource.from_dict(data)
    assert pv.solar_forecasts[1] == SolarForecastBinding("fx0002", "fx_other")
    assert pv.to_dict() == data


def test_validate_sources_accepts_solar_forecasts_alone():
    validate(pv_config(solar_forecasts=[{"config_entry_id": "fx0001", "domain": "fx"}]))


@pytest.mark.parametrize(
    ("pv", "enabled", "message"),
    [
        ({}, True, "enabled PV needs forecast arrays"),
        (
            {
                "arrays": [[IntervalBinding(EntityBinding("sensor.fx_a")).to_dict()]],
                "solar_forecasts": [{"config_entry_id": "fx0001", "domain": "fx"}],
            },
            True,
            "PV forecast arrays and Energy solar forecasts cannot be combined",
        ),
        (
            {
                "solar_forecasts": [
                    {"config_entry_id": "fx0001", "domain": "fx"},
                    {"config_entry_id": "fx0001", "domain": "fx"},
                ]
            },
            True,
            "duplicate Energy solar forecast",
        ),
        (
            {"solar_forecasts": [{"config_entry_id": "fx0001", "domain": "fx"}]},
            False,
            "disabled PV cannot have active bindings",
        ),
    ],
)
def test_validate_sources_errors(pv, enabled, message):
    config = pv_config(**pv)
    config["sources"]["pv"]["enabled"] = enabled
    with pytest.raises(InputError) as raised:
        validate(config)
    assert str(raised.value) == message


# --- runtime -----------------------------------------------------------------

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)


def runtime_config(entries, hours=2, **settings):
    config = pv_config(solar_forecasts=entries)
    config["settings"].update(
        horizon_hours=hours,
        display_horizon_hours=hours,
        reference_horizon_hours=hours,
        **settings,
    )
    return config


def test_build_problem_uses_hourly_forecast_energy():
    config = runtime_config([{"config_entry_id": "fx0001", "domain": "fx_forecast"}])
    wh = {
        "2026-09-18T12:00:00+02:00": 1000,
        "2026-09-18T13:00:00+02:00": 2000,
        "2026-09-18T14:00:00+02:00": 500,
    }
    problem, _, _ = build_problem(config, {}, NOW, solar_forecasts={"fx0001": wh})
    assert [slot.pv_kwh for slot in problem.slots] == [1.0, 2.0]


def test_two_solar_forecasts_add():
    config = runtime_config(
        [
            {"config_entry_id": "fx0001", "domain": "fx_forecast"},
            {"config_entry_id": "fx0002", "domain": "fx_other"},
        ]
    )
    first = {"2026-09-18T12:00:00+02:00": 1000, "2026-09-18T13:00:00+02:00": 2000}
    second = {"2026-09-18T12:00:00+02:00": 250, "2026-09-18T13:00:00+02:00": 250}
    problem, _, _ = build_problem(
        config, {}, NOW, solar_forecasts={"fx0001": first, "fx0002": second}
    )
    assert [slot.pv_kwh for slot in problem.slots] == [1.25, 2.25]


def test_missing_forecast_key_raises_with_the_domain():
    config = runtime_config([{"config_entry_id": "fx0001", "domain": "fx_forecast"}])
    for kwargs in ({}, {"solar_forecasts": {}}):
        with pytest.raises(InputError) as raised:
            build_problem(config, {}, NOW, **kwargs)
        assert str(raised.value) == "Energy solar forecast missing: fx_forecast"


def autonomy_config(hours):
    config = runtime_config(
        [{"config_entry_id": "fx0001", "domain": "fx_forecast"}],
        hours,
        strategy="cost_min",
        autonomy_reserve=True,
        capacity_kwh=20,
        operating_floor=10,
        hardware_floor=0,
        soc_ceiling=100,
        inverter_kw=10,
        grid_import_kw=10,
        grid_export_kw=10,
        daily_load_kwh=24,
        limit_export_to_pv=False,
    )
    config["sources"].update(
        battery_enabled=True, soc=EntityBinding("sensor.fx_soc").to_dict()
    )
    return config


def test_autonomy_reserve_sees_solar_forecast_pv():
    config = autonomy_config(2)
    start = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
    wh = {
        (start + timedelta(hours=index)).isoformat(): 0 if index < 30 else 5000
        for index in range(48)
    }
    states = {
        "sensor.fx_soc": {
            "state": "50",
            "attributes": {},
            "last_updated": NOW.isoformat(),
        }
    }
    _, _, quality = build_problem(
        deepcopy(config), states, NOW, solar_forecasts={"fx0001": wh}
    )
    assert "autonomy_floor_requires_pv" not in quality["warnings"]


# --- fetch -------------------------------------------------------------------


def make_entry(hass, state=ConfigEntryState.LOADED, domain="fx_forecast"):
    entry = MockConfigEntry(domain=domain, state=state)
    entry.add_to_hass(hass)
    return entry


async def test_fetch_returns_wh_hours(hass):
    entry = make_entry(hass)
    platform = AsyncMock(return_value={"wh_hours": {"2026-09-18T12:00:00+02:00": 5}})
    with patch(PLATFORMS, AsyncMock(return_value={"fx_forecast": platform})):
        result = await async_fetch_solar_forecast(
            hass, SolarForecastBinding(entry.entry_id, "fx_forecast")
        )
    assert result == {"2026-09-18T12:00:00+02:00": 5}
    platform.assert_awaited_once_with(hass, entry.entry_id)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "Energy solar forecast entry missing: fx_forecast"),
        ("not_loaded", "Energy solar forecast not loaded: fx_forecast"),
        ("no_platform", "no Energy solar forecast platform for fx_forecast"),
        ("raises", "Energy solar forecast failed: fx_forecast"),
        ("none", "Energy solar forecast unavailable: fx_forecast"),
        ("empty", "Energy solar forecast unavailable: fx_forecast"),
    ],
)
async def test_fetch_failures_use_exact_messages(hass, case, message):
    entry = make_entry(
        hass,
        ConfigEntryState.NOT_LOADED
        if case == "not_loaded"
        else ConfigEntryState.LOADED,
    )
    entry_id = "fx9999" if case == "missing" else entry.entry_id
    platform = {
        "raises": AsyncMock(side_effect=RuntimeError("boom")),
        "none": AsyncMock(return_value=None),
        "empty": AsyncMock(return_value={"wh_hours": {}}),
    }.get(case, AsyncMock())
    platforms = {} if case == "no_platform" else {"fx_forecast": platform}
    with (
        patch(PLATFORMS, AsyncMock(return_value=platforms)),
        pytest.raises(InputError) as raised,
    ):
        await async_fetch_solar_forecast(
            hass, SolarForecastBinding(entry_id, "fx_forecast")
        )
    assert str(raised.value) == message


@pytest.mark.parametrize("recorder_load", [True, False])
async def test_history_carries_solar_forecasts(
    recorder_mock, hass, enable_custom_integrations, recorder_load
):
    entry = make_entry(hass)
    config = runtime_config(
        [{"config_entry_id": entry.entry_id, "domain": "fx_forecast"}]
    )
    if recorder_load:
        config["sources"]["load"].update(
            mode="recorder", statistic_id="sensor.fx_load", daily_estimate=None
        )
    wh = {"2026-09-18T12:00:00+02:00": 5}
    platform = AsyncMock(return_value={"wh_hours": wh})
    with patch(PLATFORMS, AsyncMock(return_value={"fx_forecast": platform})):
        history, extra = await async_history(hass, config, {}, NOW)
    assert history["solar_forecasts"] == {entry.entry_id: wh}
    assert ("statistics" in history) is recorder_load
    assert extra == ()


async def test_history_without_solar_forecasts_is_unchanged(
    recorder_mock, hass, enable_custom_integrations
):
    config = default_configuration("PLN", "Europe/Warsaw")
    assert await async_history(hass, config, {}, NOW) == ({}, ())
