# Cost card

`custom:energy-compass-cost-card` shows what electricity costs: the purchase cost of energy drawn from
the grid, the value of exported energy (for Polish net-billing, the prosumer deposit), and the balance
**purchase − deposit**. Measured values come from Home Assistant statistics; the rest of today comes from
the Energy Compass plan. The card is plain JavaScript with no build step and no third-party requests.
It is English or Polish.

## What it shows

- **Today** (default): balance so far, a featured projected balance for the whole day (measured +
  plan to midnight), and the impact of the remaining plan (improves / worsens the balance). A
  cumulative chart shows purchase (blue) and deposit (green): solid lines measured, dashed lines
  planned. Below: purchase and deposit so far and for the whole day, price per kWh (purchase / import
  kWh, deposit / export kWh, balance / import kWh), and an hourly table.
- **Month** and **Year**: recorder statistics per day or month, plus today's live values in the
  current period. Measurements only, no forecast.
- **‹ ›** moves to earlier days, months or years (statistics only). Clicking a tab returns to the
  current period.
- Notices explain every gap: a purchase-cost gap after a restart (estimated from import × the buy
  price, or counted as 0), missing export prices, a deposit counter that started later, an unconfirmed
  controller, or a plan that does not cover the whole day.

The balance is not an invoice amount or a cash payment; fixed fees and battery wear are not included.

## Install

1. Copy [`energy-compass-cost-card.js`](../cards/cost/energy-compass-cost-card.js) and
   [`energy-compass-cost-model.js`](../cards/cost/energy-compass-cost-model.js) to
   `config/www/energy-compass/` (both files, same folder).
2. **Settings → Dashboards → ⋮ → Resources → Add resource**: URL
   `/local/energy-compass/energy-compass-cost-card.js`, type **JavaScript module**. After an update,
   change the URL to `…/energy-compass-cost-card.js?v=2` (any new value) so browsers reload it.
3. Add the card to a view, for example from the [YAML builder](https://datamindzio.github.io/ha-energy-compass/builder.html)
   (section **Cost card**) or [`examples/dashboards/cost_card.yaml`](../examples/dashboards/cost_card.yaml).
   It works best as a full-width card in a Sections view.

## Configuration

| Key | Required | Meaning |
| --- | --- | --- |
| `cost_entity` | yes | purchase cost sensor with long-term statistics, for example the cost sensor the Energy dashboard creates for grid import |
| `import_entity` | yes | grid import energy, kWh, total increasing |
| `export_entity` | yes | grid export energy, kWh, total increasing |
| `plan_entity` | yes | Energy Compass Plan sensor |
| `valid_entity` | yes | Energy Compass Forecast valid binary sensor |
| `export_prices_entity` | yes | export price forecast with a `prices` attribute, for example the [RCE sell price](tariff-helper.md#rce-sell-price-polish-net-billing) |
| `import_price_entity` | no | current buy price; fills a purchase-cost gap after a restart. Without it the gap counts as 0 |
| `deposit_entity` | no | export value sensor with statistics (below); needed for deposit in the month and year views |
| `deposit_backfill` | no | external statistic id with the valued history before the counter started (below) |
| `runtime_entity`, `mode_entity` | no | Deye controller runtime sensor and mode select; defaults to the [controller package](guide.en.md#deye-inverter-controller-solarman) entities. Used to say whether the plan is being executed |
| `currency` | no | ISO currency code, default `PLN` |
| `language` | no | `en` or `pl`; default follows the Home Assistant language |
| `tariff_label` | no | short tariff name shown in the subtitle and purchase texts, for example `PGE G12` |
| `footnote` | no | extra text for the footnote, for example how your deposit is valued |

## Export value for the month and year views

Today's deposit is computed from the export meter and the price forecast. Longer periods need a
stored value, which the [export value counter blueprint](../blueprints/automation/energy_compass/export_value_counter.yaml)
builds from the day it is installed:

1. Create a **Number** helper, for example `input_number.export_value`: minimum 0, maximum 1000000,
   step 0.0001, display mode box. Do **not** set an initial value — it would reset the counter on
   every restart.
2. Create a **Template sensor** helper, for example `sensor.export_value`, with state
   `{% set v = states('input_number.export_value') %}{{ v | float if is_number(v) else none }}`,
   unit your currency, device class **Monetary**, state class **Total**. It gives the counter long-term
   statistics; use it as `deposit_entity`.
3. Import the blueprint and create an automation: `export_energy` (the export meter), `export_prices`
   (the export price forecast) and `counter` (the Number helper). `max_step_kwh` (default 5) ignores
   larger jumps such as meter resets. Every increase is valued at the price record for that moment,
   or the forecast's state if no record covers it; without a price nothing is added and a warning is
   logged.

### Valuing the history before the counter

[`tools/export_value_backfill.py`](../tools/export_value_backfill.py) values past export at RCE prices
and imports it as an external statistic, which the card adds through `deposit_backfill`. Run
`compute` first (read-only; prints a monthly summary and writes a JSON file), check the numbers, then
`import` (refuses if the statistic already exists). Stop the backfill (`--until`) at the hour the
counter started so the two do not overlap. Rollback: `recorder/clear_statistics` for that statistic
id only. See the script's help for all options.

## Limitations

Hourly export is spread evenly within each meter interval before it is valued at quarter-hour
prices, so hourly values are estimates at the meter's resolution. Month and year views use Home
Assistant's statistics buckets; a bucket without statistics is shown as missing, never as zero.
