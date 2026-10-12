from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    name: str
    price_attribute: str | None = None
    price_start_field: str | None = None
    price_end_field: str | None = None
    price_value_field: str | None = None
    price_unit: str | None = None
    price_interval_minutes: int | None = None
    pv_attribute: str | None = None
    pv_start_field: str | None = None
    pv_value_field: str | None = None
    pv_pessimistic_value_field: str | None = None
    pv_unit: str | None = None
    pv_interval_minutes: int | None = None
    soc_unit: str | None = None


PRESETS = {
    "pse_solcast": Preset(
        "PSE RCE + Solcast",
        price_attribute="prices",
        price_end_field="dtime",
        price_value_field="rce_pln",
        price_unit="PLN/MWh",
        price_interval_minutes=15,
        pv_attribute="detailedForecast",
        pv_start_field="period_start",
        pv_value_field="pv_estimate",
        pv_pessimistic_value_field="pv_estimate10",
        pv_unit="kW",
        pv_interval_minutes=30,
    ),
}

PRESETS.update(
    {
        "pse": Preset(
            "PSE RCE",
            price_attribute="prices",
            price_end_field="dtime",
            price_value_field="rce_pln",
            price_unit="PLN/MWh",
            price_interval_minutes=15,
        ),
        "solcast": Preset(
            "Solcast",
            pv_attribute="detailedForecast",
            pv_start_field="period_start",
            pv_value_field="pv_estimate",
            pv_pessimistic_value_field="pv_estimate10",
            pv_unit="kW",
            pv_interval_minutes=30,
        ),
        "pstryk_bankilo": Preset(
            "Pstryk (Bankilo)",
            price_attribute="prices",
            price_start_field="time",
            price_value_field="price",
            price_unit="PLN/kWh",
            price_interval_minutes=60,
        ),
        "deye_solarman": Preset("Deye / Solarman aggregate SOC", soc_unit="%"),
    }
)
