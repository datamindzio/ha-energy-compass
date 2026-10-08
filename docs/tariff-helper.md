# Illustrative time-of-use tariff helper

**Built in first.** For Polish G11, G12 and G12w tariffs, **Tariffs → Buy source → Polish tariff schedule (G11/G12/G12w)** needs no YAML: pick the distributor and group, enter the rates, done (see [Built-in Polish tariffs](guide.en.md#built-in-polish-tariffs)). Likewise **Tariffs → Sell source → RCE market price (PSE)** replaces the RCE example below when the `rce_pse` integration is installed. Use the YAML on this page as the fallback: for an operator or group the catalog lacks, for custom dates, or without the integration.

[This YAML example](../examples/tariff-helper.yaml) creates one Home Assistant Template sensor with a current PLN/kWh state and a `forecast` attribute containing 49 consecutive hourly `{start, end, price}` records. It provides an illustrative fixed schedule for G11, G12 or G12w, not your operator's official tariff. Replace the rates and confirm every time band and date against your contract before using its prices for decisions. It does not predict dynamic market prices.

## Install and customize

Copy the example to your Home Assistant configuration directory as `packages/tariff_helper.yaml`. Home Assistant uses the filename as the package name with `!include_dir_named`, so it must be a valid slug. If `homeassistant.packages` already uses `!include_dir_named packages`, place the file there and keep that configuration. If it uses another package directory or a named package map, follow its existing layout, for example `tariff_helper: !include packages/tariff_helper.yaml` under the existing `packages:` map. If `homeassistant:` exists but has no `packages:` key, add `packages: !include_dir_named packages` under it. Otherwise add:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Do not replace existing `homeassistant:` or `template:` sections. You can instead merge the example's `template:` list entry into your existing `template:` list. When using `template: !include ...`, merge it into the included list rather than creating a second top-level key. Check the Home Assistant configuration, reload Template entities after editing the rates or dates, and inspect the entity state and attributes. The sensor updates at Home Assistant start, right after every Template reload, and at minutes 00, 15, 30 and 45. [Home Assistant packages](https://www.home-assistant.io/docs/configuration/packages/) and [Template integration configuration](https://www.home-assistant.io/integrations/template/) describe these options.

The [YAML builder](https://datamindzio.github.io/ha-energy-compass/builder.html) (section **Buy price: G11/G12/G12w tariff**) fills in the group, rates and afternoon window for you. Otherwise edit `tariff_group`, `base_rate`, `off_peak_rate`, `afternoon_window`, `meter_clock` and `off_peak_dates` near the top of the YAML. The example uses 0.90 and 0.55 PLN/kWh solely as placeholders; enter **final** prices in the same currency as your Energy Compass installation. `G11` uses `base_rate` for every hour. For `G12`, local hours 22:00–06:00 and a two-hour afternoon window use `off_peak_rate`. `afternoon_window: fixed` makes the afternoon window 13:00–15:00 all year; `seasonal` makes it 15:00–17:00 from April to September and 13:00–15:00 from October to March, as in some operators' G12 tariffs. Check which one your contract uses. `G12w` additionally applies that rate all Saturday and Sunday and on explicitly listed local calendar dates, for example `off_peak_dates: ['2026-12-24']`. `meter_clock: local` (the default) evaluates the zones on the wall clock, as remote-read meters billed after hourly balancing do; `winter` evaluates them on fixed winter time (UTC+1 all year), which old non-remote meters with a fixed internal clock follow (clause §2.2.x of the distributor's tariff). To decide, compare your invoice's monthly day/night kWh with your hourly import data split by the zones; if summer months are off by about one hour, switch to `winter`. This example has no public-holiday lookup; a distributor's meter that was not reprogrammed can also ignore a newly added holiday such as 24 December (Tauron says so in its tariff), so list such dates in `off_peak_dates`. Incorrect or nonfinite settings leave the sensor unavailable instead of publishing a free price.

The forecast begins at the current UTC hour and uses 49 real one-hour intervals, so it covers at least 48 hours after an update even between hour boundaries. Each interval is classified in Home Assistant's configured local time zone (or, with `meter_clock: winter`, in fixed UTC+1). A daylight-saving day can contain 23 or 25 real hours; timestamps retain their UTC offsets, including both occurrences of a repeated local hour. Set your Home Assistant installation time zone correctly (for example, `Europe/Warsaw`).

## Connect Energy Compass

Open **Tariffs → Buy source** (and **Sell source** separately if appropriate), choose **Interval forecast**, then select:

| Field | Value |
| --- | --- |
| Entity | `sensor.energy_compass_tariff_price` |
| Attribute | `forecast` |
| Value path | `price` |
| Start path | `start` |
| End path | `end` |
| Unit | `PLN/kWh` |
| Publication path, optional | `attributes.generated_at` |
| Maximum age, if checking age | `3600` seconds |

Confirm the actual entity ID in Home Assistant before selection, since an existing entity or an earlier registry rename can change it. The entity must have a finite numeric available state, a list-valued `forecast` attribute with contiguous aware intervals, and prices in the installation currency. Select the **forecast**, not the state: a current numeric state alone is treated as constant over the horizon. The generated timestamp and attributes refresh every 15 minutes, making a 3600-second age limit suitable under normal operation; a missed refresh eventually makes the source stale. Verify Preview's coverage, prices and resolved unit.

If these rates already include all taxes and charges, set the existing buy/sell tariff transformations to multiplier `1`, VAT disabled, and addition `0`. Configure buy and sell independently; copying a purchase tariff into sale pricing is only appropriate if your contract actually uses the same rate schedule. The [source selection guide](source-requirements.md) explains the supported mapping and age checks.

## RCE sell price (Polish net-billing)

[This YAML example](../examples/rce-sell-price.yaml) builds a **sell** price forecast from RCE, the market price published by PSE. A REST sensor, `sensor.energy_compass_rce_raw`, fetches today's and tomorrow's quarter-hour RCE prices (PLN/MWh) from the public PSE API every 30 minutes. A Template sensor, `sensor.energy_compass_rce_export_forecast`, converts them to PLN/kWh as `max(RCE, floor) × multiplier / 1000` and publishes the current price as its state and every quarter hour as `prices` records `{start, end, price}`. With the net-billing deposit, `floor: 0` counts negative prices as zero and `multiplier: 1.23` adds the deposit uplift; set both near the top of the YAML or in the [YAML builder](https://datamindzio.github.io/ha-energy-compass/builder.html) (section **Sell price: RCE (net-billing)**). Timestamps use the compact UTC form `20260925T1015Z` so the attribute stays under Home Assistant's recorder size limit.

Install it like the tariff helper, as `packages/rce_sell_price.yaml`. It updates at start, after a Template reload, every quarter hour and whenever the REST sensor fetches new prices. The sensor is unavailable, instead of publishing a wrong price, when the last fetch is older than 75 minutes, when no record covers the current quarter hour, or when any record is malformed.

Connect it under **Tariffs → Sell source → Interval forecast**:

| Field | Value |
| --- | --- |
| Entity | `sensor.energy_compass_rce_export_forecast` |
| Attribute | `prices` |
| Value path | `price` |
| Start path | `start` |
| End path | `end` |
| Unit | `PLN/kWh` |
| Publication path, optional | `attributes.published_at` |
| Maximum age, if checking age | `4500` seconds |

The published prices already include the floor and multiplier, so set the sell transformation to multiplier `1`, VAT disabled and addition `0`. Tomorrow's prices usually appear in the early afternoon; until then the forecast covers today only. If you chose the PL net-billing setup profile with a PSE preset, it pre-filled sell multiplier 1.23 for a raw RCE source; reset it to 1 with this template. The Preview warns when both apply. A new installation that binds this template through [Detected sources](guide.en.md#detected-sources-new-installations) gets sell multiplier 1 automatically, and setup profiles revision 3 then does not add 1.23 on top.
