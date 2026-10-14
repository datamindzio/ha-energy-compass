# SOC estimator fixtures

Seven days of recorded Home Assistant history (2026-10-07 14:00Z to 2026-10-14 14:00Z) of one installation with a
Deye hybrid inverter and a high-voltage LFP pack, reduced and renamed. They feed the replay and episode tests of
`custom_components/energy_compass/soc_estimator.py`. Entity ids, serial numbers, device ids and the host are not
included; every series carries a generic name.

| File | Content | Renamed from |
| --- | --- | --- |
| `signals.json` | One value per minute from `start` (epoch seconds), `null` where the source was unavailable: `battery_power` (W, + discharge), `battery_voltage` (V), `battery_current` (A, + discharge), `pv_power`, `load_power`, `grid_power` (W, + import), `max_charge_current`, `max_discharge_current`, `grid_charge_current` (A, the controller-written limits), `bms_soc` (%, comparison only) | the inverter integration's sensors and the three current-limit numbers |
| `counters.json` | Lifetime battery counters in kWh as `[epoch, value]` change events (`charge`, `discharge`) | `total_battery_charge`, `total_battery_discharge` |
| `programs.json` | TOU program events `[epoch, program, field, value]` for `charging` (`Disabled`, `Sell`, `Grid`), `voltage` (V) and `soc` (%), the programs' local (Europe/Warsaw) start times and the battery operation mode | the six TOU programs |
| `controller.json` | Native controller runtime status changes: `status`, dispatch `state` (`CHARGE_PV`, `HOLD`, ...) and `reason_code`; recorded from 2026-10-11 05:23Z only (the blueprint ran before) | the controller runtime sensor |
| `yaml_estimator.json` | The recorded YAML template estimator: `energy_kwh` per minute (`null` when unavailable), its `anchors` (initial, the false full anchor 2026-10-11 21:00:22Z, the one-shot correction 2026-10-12 19:06:30Z), `eta` and `full_kwh` | the template SOC sensor |

Notes: the YAML version recorded here is the 10-03 rule set without the 600 s full hold and without the later
power-interpolation refinements, so its full anchor fired on a single sample (10-11 21:00Z). The minute grid is the
state at each minute mark (forward filled); counter events keep their exact times.
