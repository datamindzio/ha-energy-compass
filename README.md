# Energy Compass

**English** · [Polski](README.pl.md)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="custom_components/energy_compass/brand/dark_icon@2x.png">
  <img src="custom_components/energy_compass/brand/icon@2x.png" alt="Energy Compass logo" width="160" height="160">
</picture>

Energy Compass is an advisory Home Assistant integration. It estimates the incremental cost of one more kWh of household use against an optimized battery and grid plan, classifies near-term use as `BOOST`, `CHEAP`, `NORMAL`, or `LIMIT`, and exposes upcoming windows as native entities. It makes no inverter control writes and sends no messages by itself.

## Install

Requires Home Assistant **2026.9.1 or later**. The integration installs `scipy==1.18.1` through its manifest. Its tested ARM64 Core/Python/solver combination and representative benchmark results are recorded in [runtime validation](docs/runtime-validation.md).

In HACS, open **Custom repositories**, add `https://github.com/datamindzio/ha-energy-compass` as an **Integration**, then download Energy Compass. Restart Home Assistant, and add **Energy Compass** under **Settings → Devices & services → Add integration**. For a manual installation, copy `custom_components/energy_compass` into the Home Assistant config directory's `custom_components` folder and restart. The optional notification blueprint lives outside the HACS-installed integration folder and must be [copied separately](docs/installation.md#opt-in-notifications). The same holds for the optional [Deye (Solarman) controller](docs/installation.md#deye-inverter-controller) — a blueprint plus a package that execute the plan on a Deye inverter — the [dashboard examples](docs/installation.md#dashboard-examples) and the [cost card](docs/cost-card.md) (purchase, export deposit and balance).

To try a source-independent synthetic setup, choose `Synthetic`, `EUR`, `UTC`, the `generic` preset, and disable PV and battery. Set fixed buy and sell rates in **Tariffs → Fixed rates and transformations**, a daily household load in **Forecast**, and a finite grid import limit in **Hardware**. Then open **Preview** and confirm. The [installation guide](docs/installation.md) shows the full setup and dashboard steps, while [source requirements](docs/source-requirements.md) explain real entity and forecast inputs. For an illustrative G11/G12/G12w schedule and a Polish net-billing RCE sell price, see the [Home Assistant tariff helper example](docs/tariff-helper.md), or fill both in the [YAML builder](https://datamindzio.github.io/ha-energy-compass/builder.html).

## What it shows

For a diagram-based walkthrough of every state, the conditions that produce it, and each dispatch strategy, read the **states and strategies guide** ([English](docs/guide.en.md) · [Polski](docs/guide.pl.md)).

Read the [published entity and calculation guide](https://datamindzio.github.io/ha-energy-compass/) for every entity, state, formula, quality flag and worked example. You can also open [docs/index.html](docs/index.html) from a checkout for offline viewing. The guide includes English and Polish entity names and follows your system's light or dark theme. See [publishing setup](docs/publishing.md) for GitHub Pages maintenance.

The integration creates current consumption level and extra-kWh cost sensors, a flexible-energy depth sensor, optimized machine state and cost sensors, next change and next `BOOST`/`CHEAP`/`LIMIT` window timestamps, a plan with a bounded outlook, optimizer status, a forecast-valid binary sensor, and a diagnostic **Alert** binary sensor. Native entity names, diagnostics, and reasons are translated into English and Polish. Automations should compare level and machine state values using their stable uppercase names.

### Dispatch strategy

The `select.<name>_strategy` entity picks which of six bundles the optimizer solves: `cost_min` (the
plain lowest-cost plan, the default), `self_sufficiency` (minimize grid kWh over currency),
`backup_ready` (hold a higher reserve), `pv_swap` (buy cheap at night, sell PV production later),
`max_export` (unrestricted arbitrage), and `grid_friendly` (capped import/export power). The plan
sensor's attributes gain `strategy` (the bundle that produced this plan), `autonomy_shortfall_kwh` and
`cap_violation_kwh` (how hard the active strategy is fighting its own soft constraints, both `0` under
`cost_min` at defaults). See [the strategy guide](docs/guide.en.md#dispatch-strategies) for a description of each
strategy and [dispatch strategies](docs/model.md#dispatch-strategies) for the full
model, and the optional [`strategy_switch` blueprint](docs/installation.md#import-the-strategy-switch-blueprint)
for rule-based daily switching.

**Flexible energy depth** independently optimizes 3, 5, 10, 15 and 20 kWh loads with a default 3 kW power cap. The 3 kWh average incremental cost is the anchor. Each larger block remains stable while its marginal cost is no more than 15% worse than that anchor; the first degraded or unknown block stops the published depth. Groups are small (3/5 kWh), medium (10/15 kWh) and large (20 kWh). This metric is separate from the hourly CHEAP classification and exposes its anchor price, threshold, profile costs and schedules as attributes.

Input or calculation failures turn **Alert** on with `code`, `reason`, `since`, `plan_retained`, and `last_successful_plan_at`. A covered previous plan continues through its scheduled intervals, including during retries. `plan_retained: true` identifies advice based on that earlier snapshot; **Forecast valid** remains on until the plan coverage ends. A successful replacement clears the alert. SOC corrections can recover after fresh, plausible reports span at least one minute. See [retention and SOC recovery](docs/model.md#plan-retention-and-alerts).

Coordinator updates skip state writes for entities whose value, availability, and attributes are unchanged. Changes to forecast validity, plan attributes, refresh status, or window timing still publish even when the primary sensor value stays the same.

Window alerts require **Notify enabled** in integration options and a nonempty action selected in the [optional blueprint](docs/installation.md#opt-in-notifications). The blueprint uses live integration preferences unless its override toggle is on. It handles favorable and `LIMIT` windows only, sends nothing while the Alert is on or the optimizer is not ready, and writes its title and message in English or Polish. The integration does not execute actions.

The extra-kWh estimate assumes the optimizer's proposed plan. Until a controller follows that plan, actual consumption savings can differ under existing automation. A forecast-dependent recommendation is not a measurement of savings. See [model and limitations](docs/model.md) for energy balance, coverage, battery assumptions, and solver behavior.

Under **Planning**, **Sell only PV** defaults to on and limits total grid export to total PV generation **for each local calendar day**. Choose the `pv_energy_today` and `grid_export_energy_today` counters in **Sources** to include energy already generated and exported since midnight. Switch it off to remove this budget. Actual operating modes have a **60-minute** minimum duration and **0.1 kW (100 W)** minimum active power by default. Power may vary above the floor; changing PV/grid source, discharge destination, HOLD or CURTAIL starts a distinct mode. Set duration to zero to disable mode duration and mode power restrictions; the separate export-benefit rule below may still apply its export power floor. CURTAIL excludes battery activity under the strict policy. An observed SoC bound may temporarily require HOLD while preserving an existing commitment. The export budget includes direct solar and battery export; battery provenance is not tracked and initial SOC is eligible within the budget. See [policy details and limitations](docs/model.md#operating-mode-duration-and-pv-export-budget).

**Limit grid-charging price**, under **Planning**, optionally sets a **Maximum grid-charging price** in the installation currency/kWh. It compares the final tariff price, including configured adjustments; equality is allowed. The switch defaults to off, and zero is a valid ceiling for free or negative prices. Above the ceiling, household supply and surplus-PV charging remain available. A new grid-charge run must stay within the ceiling for its full minimum duration. If a carried charge commitment conflicts with the ceiling before its deadline, advice pauses immediately in HOLD until that original deadline, preserving its clock. The ceiling also works when mode duration is disabled. **Battery buffer for expensive periods** (`peak_reserve_margin_kwh`, default 0 = off) makes the plan enter each run of intervals priced above that ceiling with this much energy on top of the forecast need (it requires the ceiling), as a soft target on the last cheap interval, reported in `peak_reserve_shortfall_kwh`; see [peak-period reserve](docs/guide.en.md#peak-period-reserve).

**Minimum benefit per additional battery-export period** also appears under **Planning**. It defaults to **1 unit of the configured currency** (1 PLN in a PLN installation); finite values from 0 to 1000 are supported and **0 disables it**. The planner requires that each additional period of physical battery discharge with simultaneous grid export improve full-horizon economic cost by at least that amount compared with a feasible plan having fewer periods. Prices, losses, battery wear and optional terminal value already enter that comparison. This is an incremental planning hurdle, not a per-kWh spread, a tracked recharge cycle or a realized-profit guarantee. A currently published export period continues without another reserve; future periods are never paid in advance. Real low-power export can bridge a price valley without recharging. With mode duration **0**, the export rule still requires both discharge and export to meet the minimum power while exporting from the battery; standalone household discharge, PV-only export and simultaneous curtailment/charging remain possible. The plan reports the horizon's `new_export_episodes` and `export_episode_reserve` separately from physical costs and consumption-probe differences. See [the mathematical contract](docs/model.md#minimum-additional-export-benefit).

**Periodic LFP balance charge** (`lfp_balance`), under **Battery**, is off by default. LFP packs need a periodic full charge and a short hold at the top so the BMS can balance cells and re-anchor its SOC; with the setting on, Energy Compass tracks the last completed balance and plans the next one, offering the optimizer one hour-aligned CHARGE_PV/CHARGE_GRID hold window (due windows only within the next 24 h) instead of a dedicated mode. The diagnostic **Battery balance** sensor and its `balance_interval_days`, `balance_hold_minutes`, `balance_soc_threshold` and `balance_value` settings are described in the [states and strategies guide](docs/guide.en.md#lfp-balance-charge) and [the mathematical contract](docs/model.md#lfp-balance).


## License

Energy Compass source and original artwork are provided under [Apache License 2.0](LICENSE). SciPy and NumPy are installed as separate dependencies and retain their own licenses; their source is not included here.

Planning's refresh interval controls full periodic recalculation (up to once per hour). Important source changes still recalculate earlier; native quarter-hour boundaries advance the cached plan without a new solve. Performance allows an overall calculation budget up to five minutes and individual consumption probes up to 30 seconds. When the base solve reaches its time limit before proving an optimum, the best validated plan found so far is published and marked `time_limited` with its `mip_gap`; only a limit with no feasible plan reports `timeout`. Existing defaults stay unchanged.
