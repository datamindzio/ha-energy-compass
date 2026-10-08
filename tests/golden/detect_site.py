"""Sanitized structure of one real installation; every identifier and value is
synthetic. Never add a real entity id.

The site runs a Deye hybrid inverter through ha-solarman (one config entry with
an inverter device and a disabled BMS device), Solcast, RCE PSE, two Energy
Compass templates, a second grid meter and a set of lookalike distractors.
"""

from datetime import UTC, datetime, timedelta

from custom_components.energy_compass.detect import (
    DetectionContext,
    DetectionSnapshot,
    DeviceFacts,
    EntityFacts,
    SolarForecastFacts,
    normalize_energy_prefs,
)

SITE_NOW = datetime(2026, 10, 8, 10, 20, tzinfo=UTC)
ZONE = "Europe/Warsaw"
SOLARMAN_ENTRY = "fxe00001"
SOLCAST_ENTRY = "fxe00002"
RCE_ENTRY = "fxe00004"
METER_ENTRY = "fxe00005"
FRESH = (SITE_NOW - timedelta(minutes=5)).isoformat()

ENERGY_PREFS = {
    "energy_sources": [
        {"type": "gas", "stat_energy_from": "sensor.fx_gas_meter"},
        {
            "type": "solar",
            "stat_energy_from": "sensor.fx_inv_prod_total",
            "config_entry_solar_forecast": [SOLCAST_ENTRY],
            "stat_rate": "sensor.fx_inv_pv_power",
        },
        {
            "type": "battery",
            "stat_energy_from": "sensor.fx_inv_batt_discharge_total",
            "stat_energy_to": "sensor.fx_inv_batt_charge_total",
            "power_config": {"stat_rate": "sensor.fx_inv_battery_power"},
            "stat_rate": "sensor.fx_inv_battery_power",
        },
        {
            "type": "grid",
            "stat_energy_from": "sensor.fx_inv_import_total",
            "stat_energy_to": "sensor.fx_inv_export_total",
            "entity_energy_price": "sensor.fx_buy_price_scalar",
            "number_energy_price_export": None,
            "cost_adjustment_day": 0.0,
            "power_config": {"stat_rate": "sensor.fx_grid_total_power"},
            "stat_rate": "sensor.fx_grid_total_power",
        },
        {
            "type": "grid",
            "stat_energy_from": "sensor.fx_meter2_total",
            "stat_energy_to": None,
            "number_energy_price_export": None,
            "cost_adjustment_day": 0.0,
        },
    ]
}

_counter = 0


def _registry_id() -> str:
    global _counter
    _counter += 1
    return f"fxa{_counter:05x}"


def _fact(
    entity_id,
    *,
    platform=None,
    key=None,
    uid=None,
    entry=None,
    device=None,
    disabled=False,
    device_class=None,
    state="0",
    attributes=None,
    updated=FRESH,
):
    attributes = dict(attributes or {})
    return EntityFacts(
        entity_id=entity_id,
        registry_id=_registry_id() if platform else None,
        platform=platform,
        translation_key=key,
        unique_id=uid,
        config_entry_id=entry,
        device_id=device,
        disabled=disabled,
        device_class=device_class,
        state=None if disabled else state,
        attributes=attributes,
        last_updated=None if disabled else updated,
        last_reported=None if disabled else updated,
    )


def _solarman(entity_id, key, state, unit=None, state_class=None, **extra):
    attributes = {}
    if unit:
        attributes["unit_of_measurement"] = unit
    if state_class:
        attributes["state_class"] = state_class
    attributes.update(extra.pop("attributes", {}))
    return _fact(
        entity_id,
        platform="solarman",
        key=key,
        uid=f"{SOLARMAN_ENTRY}_{key}_sensor",
        entry=SOLARMAN_ENTRY,
        device=extra.pop("device", "fxd00001"),
        state=state,
        attributes=attributes,
        **extra,
    )


def _solcast_records(day):
    """Forty-eight half-hour kW rows of one local October day, zero at night."""
    rows = []
    for index in range(48):
        hour = index / 2
        kw = max(0.0, 3.0 - abs(hour - 12.5) * 0.7)
        local = datetime(2026, 10, day, tzinfo=UTC) + timedelta(minutes=30 * index)
        rows.append(
            {
                "period_start": local.strftime("%Y-%m-%dT%H:%M:%S") + "+02:00",
                "pv_estimate": round(kw, 3),
                "pv_estimate10": round(kw * 0.8, 3),
                "pv_estimate90": round(kw * 1.2, 3),
            }
        )
    return rows


def solcast_wh_hours():
    """Thirty-minute keys, night omitted, three days of history, up to day +2."""
    keys = {}
    for day in range(5, 11):
        for row in _solcast_records(day):
            hour = (
                int(row["period_start"][11:13]) + int(row["period_start"][14:16]) / 60
            )
            if row["pv_estimate"] > 0 or hour in (8.0, 17.0):
                keys[row["period_start"]] = round(row["pv_estimate"] * 500, 3)
    return keys


def _rce_records():
    start = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    rows = []
    for index in range(96):
        end = start + timedelta(minutes=15 * (index + 1))
        rows.append(
            {
                "dtime": end.strftime("%Y-%m-%d %H:%M:%S"),
                "period": f"{index:02d}",
                "business_date": "2026-10-08",
                "rce_pln": 300.0 + index,
            }
        )
    return rows


def _template_prices():
    start = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    rows = []
    for index in range(192):
        begin = start + timedelta(minutes=15 * index)
        rows.append(
            {
                "start": begin.isoformat(),
                "end": (begin + timedelta(minutes=15)).isoformat(),
                "price": round(0.30 + index * 0.001, 4),
            }
        )
    return rows


def _entities():
    global _counter
    _counter = 0
    facts = [
        _solarman(
            "sensor.fx_inv_load_total",
            "total_load_consumption",
            "8797.8",
            "kWh",
            "total_increasing",
        ),
        _solarman(
            "sensor.fx_inv_load_today",
            "today_load_consumption",
            "4.4",
            "kWh",
            "total_increasing",
        ),
        _solarman(
            "sensor.fx_inv_battery",
            "battery",
            "1",
            "%",
            "measurement",
            attributes={"BMS SOC": 12, "device_class": "battery"},
        ),
        _solarman(
            "sensor.fx_inv_battery_power",
            "battery_power",
            "-890",
            "W",
            "measurement",
        ),
        _solarman("sensor.fx_inv_pv_power", "pv_power", "1650", "W", "measurement"),
        _solarman("sensor.fx_inv_pv1_power", "pv1_power", "1380", "W", "measurement"),
        _solarman("sensor.fx_inv_grid_power", "grid_power", "15", "W", "measurement"),
        _solarman(
            "sensor.fx_inv_prod_total",
            "total_production",
            "9420.8",
            "kWh",
            "total_increasing",
        ),
        _solarman(
            "sensor.fx_inv_import_total",
            "total_energy_import",
            "4729.7",
            "kWh",
            "total_increasing",
        ),
        _solarman(
            "sensor.fx_inv_export_total",
            "total_energy_export",
            "2863",
            "kWh",
            "total_increasing",
        ),
        _solarman(
            "sensor.fx_inv_prod_today",
            "today_production",
            "1.1",
            "kWh",
            "total_increasing",
        ),
        _solarman(
            "sensor.fx_inv_export_today",
            "today_energy_export",
            "0",
            "kWh",
            "total_increasing",
        ),
        _solarman(
            "sensor.fx_inv_batt_charge_today",
            "today_battery_charge",
            "3",
            "kWh",
            "total_increasing",
        ),
        _solarman(
            "sensor.fx_inv_batt_discharge_today",
            "today_battery_discharge",
            "2",
            "kWh",
            "total_increasing",
        ),
        _solarman(
            "sensor.fx_inv_batt_charge_total",
            "total_battery_charge",
            "6406.6",
            "kWh",
            "total_increasing",
        ),
        _solarman(
            "sensor.fx_inv_batt_discharge_total",
            "total_battery_discharge",
            "5791.7",
            "kWh",
            "total_increasing",
        ),
        _fact(
            "number.fx_inv_pv_power",
            platform="solarman",
            key="pv_power",
            uid=f"{SOLARMAN_ENTRY}_pv_power_number",
            entry=SOLARMAN_ENTRY,
            device="fxd00001",
            state="8600",
            attributes={"unit_of_measurement": "W"},
        ),
        _solarman(
            "sensor.fx_pack_1",
            "battery_1",
            "55",
            "%",
            "measurement",
            device="fxd00002",
            disabled=True,
        ),
        _solarman(
            "sensor.fx_pack_2",
            "battery_2",
            "56",
            "%",
            "measurement",
            device="fxd00002",
            disabled=True,
        ),
        _fact(
            "sensor.fx_smoke_alarm",
            platform="tuya",
            key="battery",
            uid="fx-tuya-smoke",
            entry="fxe00006",
            device="fxd00003",
            device_class="battery",
            state="87",
            attributes={"unit_of_measurement": "%"},
        ),
        _fact(
            "sensor.fx_sc_tomorrow",
            platform="solcast_solar",
            key="total_kwh_forecast_today",
            uid="total_kwh_forecast_today",
            entry=SOLCAST_ENTRY,
            device="fxd00004",
            state="17.5",
            attributes={
                "unit_of_measurement": "kWh",
                "detailedForecast": _solcast_records(8),
            },
        ),
        _fact(
            "sensor.fx_sc_today",
            platform="solcast_solar",
            key="total_kwh_forecast_tomorrow",
            uid="total_kwh_forecast_tomorrow",
            entry=SOLCAST_ENTRY,
            device="fxd00004",
            state="20.9",
            attributes={
                "unit_of_measurement": "kWh",
                "detailedForecast": _solcast_records(9),
            },
        ),
        _fact(
            "sensor.fx_sc_day3",
            platform="solcast_solar",
            key="total_kwh_forecast_d3",
            uid="total_kwh_forecast_d3",
            entry=SOLCAST_ENTRY,
            device="fxd00004",
            state="12",
            attributes={
                "unit_of_measurement": "kWh",
                "detailedForecast": _solcast_records(10),
            },
        ),
        _fact(
            "sensor.fx_sc_rooftop",
            platform="solcast_solar",
            key="site_data",
            uid="fx-solcast-site",
            entry=SOLCAST_ENTRY,
            device="fxd00004",
            state="3",
            attributes={"detailedForecast": _solcast_records(8)},
        ),
        _fact(
            "sensor.fx_sc_duplicate",
            platform="solcast_solar",
            key="total_kwh_forecast_today",
            uid="total_kwh_forecast_today_dup",
            entry=SOLCAST_ENTRY,
            device="fxd00004",
            disabled=True,
        ),
        _fact(
            "sensor.fx_sc_lookalike",
            platform="template",
            uid="fx-solcast-lookalike",
            state="9",
            attributes={"detailedForecast": _solcast_records(8)},
        ),
        _fact(
            "sensor.fx_rce_today",
            platform="rce_pse",
            key="rce_pse_today_price",
            uid="rce_pse_today_price",
            entry=RCE_ENTRY,
            device="fxd00005",
            state="655.36",
            attributes={"unit_of_measurement": "PLN/MWh", "prices": _rce_records()},
        ),
        _fact(
            "sensor.fx_rce_tomorrow",
            platform="rce_pse",
            key="rce_pse_tomorrow_price",
            uid="rce_pse_tomorrow_price",
            entry=RCE_ENTRY,
            device="fxd00005",
            state="unknown",
            attributes={"unit_of_measurement": "PLN/MWh", "prices": []},
        ),
        _fact(
            "sensor.fx_ec_sell",
            platform="template",
            uid="energy_compass_rce_export_forecast_fx",
            state="0.637",
            attributes={
                "unit_of_measurement": "PLN/kWh",
                "prices": _template_prices(),
                "published_at": FRESH,
                "settlement": "RCE, floor 0, multiplier 1.23",
            },
        ),
        _fact(
            "sensor.fx_ec_buy",
            platform="template",
            uid="energy_compass_tariff_price_example",
            state="1.25",
            attributes={
                "unit_of_measurement": "PLN/kWh",
                "forecast": [],
                "generated_at": FRESH,
            },
        ),
        _fact(
            "sensor.fx_ec_soc",
            platform="template",
            uid="energy_compass_soc_estimator_fx",
            state="23.2",
            attributes={
                "unit_of_measurement": "%",
                "device_class": "battery",
                "state_class": "measurement",
            },
        ),
        _fact(
            "sensor.fx_compass_plan",
            platform="energy_compass",
            uid="fx-compass-plan",
            device_class="battery",
            state="50",
            attributes={"unit_of_measurement": "%"},
        ),
        _fact(
            "sensor.fx_grid_total_power",
            platform="template",
            uid="fx-grid-total-power",
            device_class="power",
            state="15",
            attributes={
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
            },
        ),
        _fact(
            "sensor.fx_meter2_total",
            platform="fx_meter",
            uid="fx-meter2-wh",
            entry=METER_ENTRY,
            device="fxd00006",
            state="123456",
            attributes={
                "unit_of_measurement": "Wh",
                "state_class": "total_increasing",
            },
        ),
        _fact(
            "sensor.fx_buy_price_scalar",
            platform="template",
            uid="fx-buy-price",
            state="1.2",
            attributes={"unit_of_measurement": "PLN/kWh"},
        ),
    ]
    for key in (
        "rce_pse_min_price",
        "rce_pse_max_price",
        "rce_pse_mean_price",
        "rce_pse_next_hour_price",
        "rce_pse_prosumer_price",
    ):
        facts.append(
            _fact(
                f"sensor.fx_{key}",
                platform="rce_pse",
                key=key,
                uid=key,
                entry=RCE_ENTRY,
                device="fxd00005",
                state="400",
                attributes={"unit_of_measurement": "PLN/MWh", "prices": _rce_records()},
            )
        )
    return {item.entity_id: item for item in facts}


DISTRACTORS = frozenset(
    {
        "sensor.fx_inv_load_today",
        "sensor.fx_inv_pv1_power",
        "sensor.fx_inv_batt_charge_today",
        "sensor.fx_inv_batt_discharge_today",
        "sensor.fx_inv_batt_charge_total",
        "sensor.fx_inv_batt_discharge_total",
        "number.fx_inv_pv_power",
        "sensor.fx_pack_1",
        "sensor.fx_pack_2",
        "sensor.fx_smoke_alarm",
        "sensor.fx_sc_day3",
        "sensor.fx_sc_rooftop",
        "sensor.fx_sc_duplicate",
        "sensor.fx_sc_lookalike",
        "sensor.fx_ec_buy",
        "sensor.fx_compass_plan",
        "sensor.fx_buy_price_scalar",
        "sensor.fx_rce_pse_min_price",
        "sensor.fx_rce_pse_max_price",
        "sensor.fx_rce_pse_mean_price",
        "sensor.fx_rce_pse_next_hour_price",
        "sensor.fx_rce_pse_prosumer_price",
    }
)


def site_devices():
    return {
        "fxd00001": DeviceFacts(
            "fxd00001", "Deye", "FX-HYBRID", frozenset({SOLARMAN_ENTRY})
        ),
        "fxd00002": DeviceFacts("fxd00002", "Deye", None, frozenset({SOLARMAN_ENTRY})),
        "fxd00003": DeviceFacts(
            "fxd00003", "Tuya", "FX-SMOKE", frozenset({"fxe00006"})
        ),
        "fxd00004": DeviceFacts(
            "fxd00004", "Solcast", None, frozenset({SOLCAST_ENTRY})
        ),
        "fxd00005": DeviceFacts("fxd00005", "RCE", None, frozenset({RCE_ENTRY})),
        "fxd00006": DeviceFacts("fxd00006", "FxMeter", None, frozenset({METER_ENTRY})),
    }


def site_snapshot() -> DetectionSnapshot:
    return DetectionSnapshot(
        entities=_entities(),
        devices=site_devices(),
        energy=normalize_energy_prefs(ENERGY_PREFS),
        solar_forecasts={
            SOLCAST_ENTRY: SolarForecastFacts(
                SOLCAST_ENTRY, "solcast_solar", "Fx Solcast", solcast_wh_hours(), None
            )
        },
    )


def site_context(**changes) -> DetectionContext:
    values = {
        "currency": "PLN",
        "timezone": ZONE,
        "pv_enabled": True,
        "battery_enabled": True,
        "now": SITE_NOW,
    }
    values.update(changes)
    return DetectionContext(**values)
