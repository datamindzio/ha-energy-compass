# Source requirements and selection

During setup or later through **Configure** or **Reconfigure**, open **Tariffs → Buy source** and **Tariffs → Sell source** independently. Choose **Fixed rate** for the rate in **Tariffs → Fixed rates and transformations**, **Numeric entity** for a current numeric state or attribute, or **Interval forecast** for structured interval data. For a Polish installation (currency PLN) the buy source also offers **Polish tariff schedule (G11/G12/G12w)**, a built-in catalog of zones in which you type the rates, and the sell source offers **RCE market price (PSE)**, a forecast source with a price floor of 0 (see [Built-in Polish tariffs](guide.en.md#built-in-polish-tariffs)). The source entity, attribute and mapping screens show the saved selection when editing. Opening them does not change the saved configuration; Preview validates current data and the base plan before the final confirmation.

Use **Sources → Review and edit sources** to identify one existing binding, then edit it, append a today/tomorrow continuation, or confirm removal. The inventory identifies each PV group and continuation separately and keeps unavailable or older unsupported selections visible for repair. **Sources → Add source** chooses a role and compatible mode. To add a separate PV array, choose the next group number; a continuation in an existing group extends that group's forecast. Removing a PV group is a separate explicit action. Required buy, sell, load and battery SOC selections need a replacement or the related component disabled before removal. A bare tariff group name such as `G12` is not a price; the built-in tariff schedule defines only the zones, and you still enter the rates.

| Source | Selected data | Required shape | Meaning |
| --- | --- | --- | --- |
| Current price | State of a sensor, number or input_number | Finite number | Held constant over the planning horizon; read again on recalculation |
| Current price attribute | Named attribute of an available numeric-source entity | Finite number | Same constant-horizon assumption |
| Price forecast | Attribute of an available entity | List of records, or a timestamp-to-price map | Future changes follow the supplied intervals |
| Tariff schedule (buy, PLN) | Catalog entry, optional winter-clock flag and Enea hours | No entity; rates `buy_rate` and `buy_off_peak_rate` must be above zero | Zones switch on the tariff clock inside the planning grid; no age or coverage limit |
| PV: Energy dashboard solar forecast | One or more loaded integrations with an Energy solar-forecast platform | No entity; `wh_hours` keys with Wh values | Wh per local hour, summed as the Energy dashboard does; missing hours are 0; no age check; cannot be combined with entity PV arrays (see [Energy dashboard solar forecast](guide.en.md#energy-dashboard-solar-forecast)) |

Use the installation currency, for example `PLN/kWh` or `PLN/MWh`. MWh prices convert to kWh once by dividing by 1000. No exchange conversion is performed. For a state value, existing `unit_of_measurement` must match the declared source unit; attribute values require an explicit unit declaration. Zero and negative prices are allowed within configured bounds; they are not automatically clipped.

Every selected source must exist and its parent entity must not be `unknown` or `unavailable`. Numeric strings such as `0.75` are accepted; names, blank values, NaN and infinity are not prices. An attribute cannot rescue an unavailable parent state.

## Numeric examples

Entity state: `sensor.import_price`, state `0.75`, `unit_of_measurement: PLN/kWh` → 0.75 PLN/kWh for the whole planning horizon.

Entity attribute: `sensor.market_price`, available parent state, numeric attribute `import_price: 750`, declared unit `PLN/MWh` → 0.75 PLN/kWh. Select the attribute explicitly; the parent's unit may describe another value.

A current cheap-zone rate does not reveal when the next expensive zone starts. Use an interval forecast when time-of-use changes matter. An [illustrative Home Assistant tariff helper](tariff-helper.md) (with an [RCE sell-price example](tariff-helper.md#rce-sell-price-polish-net-billing)) publishes a G11/G12/G12w schedule in this format.

## Forecast example

Illustrative attributes; substitute current timestamps and actual provider fields:

```yaml
prices:
  - start: '2026-09-18T10:00:00+02:00'
    end: '2026-09-18T11:00:00+02:00'
    price: 0.75
  - start: '2026-09-18T11:00:00+02:00'
    end: '2026-09-18T12:00:00+02:00'
    price: 0.90
```

Select attribute `prices`, value path `price`, start path `start`, end path `end`, and unit `PLN/kWh`. When the end is absent, configure an explicit duration field or interval length. A timestamp-to-number map can also supply start times and prices with a configured interval length. HA entity state is a string; JSON text in that state is not automatically parsed into a forecast.

Records require positive duration and finite prices. Prefer ISO 8601 timestamps with `Z` or an explicit UTC offset. Naive local timestamps need a configured source timezone; ambiguous/nonexistent daylight-saving timestamps are rejected. Nested fields use dot-separated paths, for example `tariff.amount`; expressions, list indexes and templates are not supported in mapping fields.

Today/tomorrow entities are continuations of one price series, not values to add. For example, select `sensor.price_today` with attribute `prices`, then use **Sources → Review and edit sources → Buy continuation → Append continuation** to select `sensor.price_tomorrow` with the same mapping. Exact duplicate intervals with the same price deduplicate; contradictory or partially overlapping intervals fail. Coverage begins at the calculation time. Missing tomorrow shortens known coverage and produces quality information, rather than inventing a future rate.

## Age and transformations

Age checks use Home Assistant `last_updated`, which changes when the state or attributes change. A configured forecast publication path adds a publication-age check. A deliberately stable manual helper may disable age checking explicitly; unavailable data still fail. Set limits to match the provider's publication schedule.

After unit normalization, the effective rate is `price × multiplier × VAT factor + addition`, where the VAT factor is `1 + VAT percent / 100` when enabled. A tariff schedule uses `rate(zone) × multiplier × VAT factor + addition` with the peak or off-peak rate. A forecast source with a price floor (`floor_per_kwh`, set only by the RCE market price shortcut) first applies `max(price, floor)` to the normalized value, before the multiplier, VAT and addition. Avoid applying charges or VAT twice if the entity already provides the final rate. Check the resolved values in Preview before saving.

## Daily battery throughput

Select **Sources → Add source → Daily AC-side charge plus discharge → Measurement** before setting a positive daily cycle limit in **Battery**. Use a numeric entity or attribute containing total AC-side charge plus discharge energy since midnight in the installation timezone. It must reset daily, stay nonnegative and update after midnight. Use kWh or Wh; Wh convert once to kWh. This is not instantaneous power, SOC, net energy, grid import or a lifetime counter.

The selected entity must be available, within the chosen age limit, and have `last_updated` dated today in the installation timezone. External recorder statistics are not supported as the input to this constraint. A legacy unsupported binding remains visible for replacement; it is not silently converted.

Daily cap: `2 × battery capacity kWh × daily_cycles`. Remaining today: `max(0, daily cap − measured throughput)`. Preview shows the selected entity/attribute, resolved throughput, cap and remaining today. A cycle limit of zero disables this constraint, and a battery-disabled installation does not require this source.

The existing optimizer budgets charge plus discharge on its AC-side flow basis; it accounts for SOC efficiency separately. A DC counter needs an upstream conversion that accounts for charging and discharging losses before selection here. Energy Compass does not infer or apply that conversion.

## Daily PV and grid export counters

When **Planning → Sell only PV** is enabled and grid export capacity is above zero, add both counters through **Sources → Add source → PV energy today → Measurement** and **Sources → Add source → Grid export energy today → Measurement**. Select kWh or Wh entities or attributes that count generation and grid export since local midnight, reset daily and never go negative. These roles accept measurements only, with a default maximum age of 86400 seconds; a recorder statistic or lifetime total cannot replace them. Both counters must report on the current local date. The policy combines observed totals with the forecast for each local day, as described in the [dispatch model](model.md).

## Automatic detection

A new installation opens **Detected sources** after the first form (see [Detected sources](guide.en.md#detected-sources-new-installations)). It never runs in Configure or Reconfigure.

**Identity rule.** A candidate is identified only by the integration platform and the entity's translation key (and, for Solarman, a Deye device in the same config entry), by the Energy Compass template identities (a `template` sensor whose unique id starts with `energy_compass_` and whose `settlement` attribute names a multiplier; a `template` sensor with device class battery and unit %), or by a reference in the Energy dashboard preferences. Entity ids and names are never matched. Disabled entities, domains other than `sensor`, Energy Compass's own entities and external statistic ids are excluded before ranking.

**Providers.** Energy dashboard preferences (`energy_prefs`), Solcast (`solcast_solar`), RCE PSE (`rce_pse`), ha-solarman (`solarman`, Deye config entries only) and `template` sensors. Dashboard sources rank first, integration signals are the fallback, and a row with several candidates offers all of them with **Do not bind** last. `buy`, `throughput_today`, `battery_energy`, `battery_charge_power`, `battery_discharge_power` and helpers are never detected.

**Stored measurement age limits.**

| Measurement | Maximum age (seconds) |
| --- | --- |
| `battery_power`, `grid_import_power`, `grid_export_power` | 3600 |
| `pv_power` | 86400 |
| `pv_energy`, `grid_import_energy`, `grid_export_energy` (lifetime counters) | 86400 |
| `pv_energy_today`, `grid_export_energy_today` (daily counters) | 86400 |

Power is stored in kW (`W` × 0.001), counters in kWh (`Wh` × 0.001); `grid_export_power` is the grid sensor with sign −1. The daily counters are bounded at 0 and fail when they are negative. The RCE sensor must report PLN/MWh; the Energy dashboard forecast needs no entity or age limit.
