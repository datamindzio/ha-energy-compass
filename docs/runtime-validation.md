# Solver runtime validation

The supported validation target is the official Raspberry Pi 5 Home Assistant Core 2026.9.1 image, `ghcr.io/home-assistant/raspberrypi5-64-homeassistant:2026.9.1` at digest `sha256:3637812af79ccc020ad3c61787fedaae14dc89918b1ddfc9e96b8f1077b35943`. The checked image reports `aarch64`, Python 3.14.6, NumPy 2.3.2, SciPy 1.18.1, and Home Assistant 2026.9.1. The project requires Python 3.14.2 or newer and pins SciPy 1.18.1 to match this Core image.

The matched fixture package is `pytest-homeassistant-custom-component==0.13.364`; its published dependency metadata requires `homeassistant==2026.9.1`, NumPy 2.3.2, and pytest 9.0.3. The disposable test image installs those exact test dependencies. Home Assistant's extra wheel index is disabled for this install because it contains an older `pytest-asyncio` release and otherwise prevents resolution of the helper's `pytest-asyncio==1.4.0` requirement. The image keeps `boto3` and `botocore` at Core's 1.42.97 versions so the installed `aiobotocore` constraint remains satisfied. The project pytest configuration enables automatic asyncio fixture handling required by the HA fixture package.

From the repository root, with Docker available:

```sh
docker build -f Dockerfile.test -t ha-energy-compass-test:2026.9.1 .
docker run --rm -e PYTEST_ADDOPTS='-p no:cacheprovider' -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m pytest tests/test_runtime.py -q
docker run --rm ha-energy-compass-test:2026.9.1 -m pip check
```

The read-only repository mount and disabled pytest cache keep generated files out of the worktree. The same image can run later test selections by replacing `tests/test_runtime.py` in the second command.

Validation result: the image build completed, `tests/test_runtime.py` passed (1 test), and `pip check` reported no broken requirements. The test run emitted a `SyntaxWarning` from the base image's `rich` package; it did not affect collection or the solver result.

The tiny mixed-integer program maximizes a binary variable under bounds 0–1 with a two-second solver limit. The isolated wheel installation and test passed on the target aarch64 Python runtime. This establishes an importable solver and functioning HiGHS MILP interface; the advisory workload benchmark follows below. No dependency was installed on the live Home Assistant instance.

## Advisory optimizer benchmark

The deterministic `benchmarks.optimizer` workload varies buy and sell prices, solar generation and household load across quarter-hour slots, with a 13.5 kWh battery and finite grid and inverter limits, at the shipped settings (`minimum_mode_minutes` 60). `--strategy all` solves it once per dispatch strategy, in one process. `--accept-incumbent` solves the way the runtime solves the base plan: at the time limit the best validated solution found so far is the plan (`result` is `time_limited`, with `mip_gap`); without it the solve is the strict one that probes and the other checks use, where a limit without a proof is `timeout`. The container numbers below come from the matched aarch64 image on a Mac host (Apple M4 Max, Docker with 6 CPUs), three runs each, on a host that was also running other container jobs (load average 6-21 over the measurements). The solver does the same work every run, but its limit is wall-clock, so a case that ends at the limit ends at a slightly different point of the search when the host is busy. They measure that container and host combination, not Raspberry Pi 5 hardware. Peak RSS is the process high-water mark, including Python and SciPy startup and every strategy solved before it; it is taken from runs without `--compare-proven`, whose second solve raises it.

```sh
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.optimizer --slots 192 --time-limit 10 --strategy all --accept-incumbent
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.optimizer --slots 384 --time-limit 10 --strategy all --accept-incumbent
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.optimizer --slots 384 --time-limit 0.000001 --strategy all --accept-incumbent
```

| Slots | Time limit | Strategy | Result (3 runs) | Variables / rows | Solve call elapsed | Peak RSS |
| ---: | ---: | --- | --- | ---: | ---: | ---: |
| 192 | 10 s | `cost_min` | Optimal | 5,368 / 7,799 | 2.3-8.2 s | 277,904-278,100 KiB |
| 192 | 10 s | `self_sufficiency` | Time limited | 5,369 / 7,991 | 10.0-10.1 s | 283,708-289,312 KiB |
| 192 | 10 s | `backup_ready` | Time limited | 5,369 / 7,991 | 10.0-10.2 s | 283,708-289,312 KiB |
| 192 | 10 s | `pv_swap` | Optimal | 5,369 / 7,991 | 0.3-1.5 s | 283,708-289,312 KiB |
| 192 | 10 s | `max_export` | Optimal | 5,176 / 7,220 | 1.1-5.6 s | 283,708-289,312 KiB |
| 192 | 10 s | `grid_friendly` | Optimal | 5,753 / 8,375 | 0.8-3.0 s | 283,708-289,312 KiB |
| 384 | 10 s | `cost_min` | Time limited | 10,742 / 15,599 | 10.1 s | 330,380-344,572 KiB |
| 384 | 10 s | `self_sufficiency` | Time limited | 10,743 / 15,983 | 10.2-10.4 s | 330,380-344,572 KiB |
| 384 | 10 s | `backup_ready` | Time limited | 10,743 / 15,983 | 10.0-10.3 s | 330,380-344,572 KiB |
| 384 | 10 s | `pv_swap` | Time limited | 10,743 / 15,983 | 10.1-10.3 s | 330,380-344,572 KiB |
| 384 | 10 s | `max_export` | Optimal in two runs, time limited in one | 10,358 / 14,442 | 7.2-10.1 s | 330,380-344,572 KiB |
| 384 | 10 s | `grid_friendly` | Optimal | 11,511 / 16,751 | 1.8-3.2 s | 330,380-344,572 KiB |
| 384 | 0.000001 s | each of the six | `timeout`, no plan | 10,358-11,511 / 14,442-16,751 | 0.05-0.17 s | 262,820-264,872 KiB |

The wide ranges of the quick cases (`cost_min` at 192 slots 2.3-8.2 s) are the busy host: their CPU time is 2.7 s in every run. The last row is the case below where the solver holds no feasible solution when the limit hits, so the benchmark exits 1 (its acceptance rule wants a plan from every strategy) after printing one JSON line per strategy; the other commands exit 0.

### How far the time limit leaves the plan from the proof

`--compare-proven 300` re-solves every time-limited plan strictly with a 300 s limit and reports the proof's `cpu_s`, its objectives and how the two plans differ:

```sh
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.optimizer --slots 192 --time-limit 10 --strategy all --accept-incumbent --compare-proven 300
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.optimizer --slots 384 --time-limit 10 --strategy all --accept-incumbent --compare-proven 300
```

Two objectives are in every JSON line and they are not the same quantity. `objective` is the plan cost in currency (grid cost plus wear minus terminal credit). `weighted_objective` is what HiGHS minimises: the cost vector times the solution, with the strategy weights and the soft-floor slacks (`self_sufficiency` weights imports far above their price, `backup_ready` charges the shortfall below its floor). `mip_gap` bounds only the weighted objective, as a fraction of the incumbent. The table gives both for the cases that ended at the limit, next to the proven plan of the same problem (three runs; a single value means every run agreed):

| Slots | Strategy | `mip_gap` at the limit | Weighted objective: limit / proven (excess) | Plan cost: limit / proven | Intervals with a different plan |
| ---: | --- | ---: | --- | --- | ---: |
| 192 | `self_sufficiency` | 0.70-0.72 % | 163.49-163.53 / 162.40 (+0.67-0.69 %) | 8.647-8.717 / 8.703 (-0.65 to +0.16 %) | 63-67 of 192 (23.6-25.0 kWh of grid flow) |
| 192 | `backup_ready` | 0.11 % | 20.292 / 20.291 (+0.005 %) | 11.043 / 11.042 (+0.01 %) | 43 of 192 (20.9 kWh) |
| 384 | `cost_min` | 0.007 % | 12.7056 / 12.7047 (+0.007 %) | 12.7056 / 12.7047 (+0.01 %) | 69 of 384 (31.7 kWh) |
| 384 | `self_sufficiency` | 0.34 % | 320.22 / 319.18 (+0.32 %) | 15.184 / 15.153 (+0.21 %) | 40 of 384 (19.9 kWh) |
| 384 | `backup_ready` | 0.03 % | 31.069 / 31.065 (+0.014 %) | 15.909 / 16.034 (-0.78 %) | 31 of 384 (12.3 kWh) |
| 384 | `pv_swap` | 0.10-0.22 % | 17.172-17.193 / 17.156 (+0.10-0.22 %) | 12.710-12.716 / 12.705 (+0.04-0.08 %) | 43-48 of 384 (14.8-15.7 kWh) |

In every run the weighted excess stays within `mip_gap`, as the bound promises. The plan cost carries no such bound: the weighted objective holds terms that are not currency (the strategy weights and, for `backup_ready`, the price of a shortfall below the floor), so a plan can be worse in the weighted objective and cheaper in currency. At 384 slots `backup_ready` the plan published at the limit costs 0.78 % less than the proven one, and at 192 slots `self_sufficiency` it costs less than the proven plan in two runs and more in one. So `mip_gap` says how far the solver's own objective can be from the optimum, not how many currency units the limit cost. Neither is the plan "the same with a bit less cost": tens of intervals differ, since near-equal weighted objectives are different dispatch schedules. A loaded host stops the search earlier: one of three further runs without the proof step ended `backup_ready` at 192 slots with `mip_gap` 0.41 % and plan cost 11.10 instead of 0.11 % and 11.04.

The proof is what the limit does not reach, so its CPU time (`time.process_time()` of one `solve()` call, all threads) says how far each strategy is from finishing. It differs between the two environments of this document more than between the runs inside one, and not in one direction:

| Slots | Strategy | Container, aarch64 Linux | Native venv, macOS arm64 |
| ---: | --- | ---: | ---: |
| 192 | `cost_min` | 2.7-2.8 s | 1.5 s |
| 192 | `self_sufficiency` | 35.0-36.4 s | 17.7-17.9 s |
| 192 | `backup_ready` | 13.8-14.7 s | 22.7 s |
| 192 | `pv_swap` | 0.3 s | 0.25-0.27 s |
| 192 | `max_export` | 1.2-1.3 s | 0.8 s |
| 192 | `grid_friendly` | 0.9 s | 1.6-1.7 s |
| 384 | `cost_min` | 12.2-12.5 s | 6.5 s |
| 384 | `self_sufficiency` | 38.0-47.2 s | 74.8 s |
| 384 | `backup_ready` | 18.5-19.6 s | 7.0 s |
| 384 | `pv_swap` | 19.6-20.4 s | 6.8 s |
| 384 | `max_export` | 7.8-8.1 s | 12.6 s |
| 384 | `grid_friendly` | 2.3-2.5 s | 5.6 s |

The container column is the three runs above. The native column is the same command (`python -m benchmarks.optimizer ... --accept-incumbent --compare-proven 300`) in a Python 3.14.8 venv with NumPy 2.3.2 and SciPy 1.18.1 on the same Mac, which is the environment of the tables in § Consumption probe limits; two runs at 192 slots, one at 384. Both run the same model and the same SciPy version, and each strategy takes a different time in each: `self_sufficiency` at 192 slots proves in 35 s in the container and 18 s natively, `backup_ready` in 14 s and 23 s. At 384 slots the native build proves `cost_min`, `backup_ready` and `pv_swap` inside the 10 s limit and ends `max_export` and `self_sufficiency` at it (gaps 0.26 % and 1.36 %), the opposite of the container. The likely cause is a different branch-and-bound path in the two HiGHS builds (not investigated), so compare strategies only inside one environment and read an absolute second as an order of magnitude.

The numbers replace the earlier ones, which predate the CHARGE_PV surplus-following rule (T-010). Before it, at 192 slots in the container, `cost_min` proved in 0.64 s, `pv_swap` in 0.41 s, `max_export` in 0.53 s, `grid_friendly` in 1.1 s, `self_sufficiency` in 7.0 s and `backup_ready` in 8.7 s. The rule adds one row per PV interval and a binary (`charge_pv_full`, "the pack ends on its ceiling") where the pack can reach its ceiling, so a planned CHARGE_PV interval charges all the PV surplus or fills the pack. It moved the default `cost_min` 192-slot solve from about 0.6 s to 2.7 s, put `self_sufficiency` and `backup_ready` past the 10 s base-solve limit and, in the container, `cost_min` and `pv_swap` at 384 slots too. `self_sufficiency` and `backup_ready` were already the slow strategies because they make SELF_CONSUME worth using all night: the load-following rule (see `docs/model.md`) leaves a small gap that HiGHS needs a few hundred branch-and-bound nodes to close (the plan is found after 2-3 s, the rest is the proof). The model was not changed to recover the time (`docs/model.md` § Cost of the rule lists what was measured); whether to accept this cost or reformulate the rows is T-044.

### What the user sees when the base solve reaches its limit

The base solve stops at `solve_time_limit_s` (default 10 s, 0.1-30 s; `total_time_limit_s`, default 60 s and 1-300 s, bounds base solve plus probes). Two things can be true at that moment:

1. HiGHS holds a feasible solution (every time-limited row above). The best validated one is published as the plan, `optimizer_status` reads `ready` as for a proven plan, and the plan carries `time_limited: true` with `mip_gap` (`docs/guide.en.md` § Plan attributes for strategies). Nothing in the plan marks it as worse; the table above shows how it can differ.
2. HiGHS holds no feasible solution yet (the 0.000001 s row; on a heavily loaded host it also happened once to the 384-slot `max_export` case). `solve()` raises `SolveError("timeout")` and there is no new plan: `optimizer_status` becomes `timeout` with the alert `timeout`, the previous plan stays published (`plan_retained: true`) while it still covers the current interval and otherwise nothing is published until the next trigger (`docs/guide.en.md` § Optimizer status). The next refresh solves again from scratch. This is the likelier case on a Raspberry Pi 5 at long horizons, which was not measured: it should be slower than this host, and the first feasible solution should then come later.

Raise `solve_time_limit_s` when a proven optimum matters more than refresh latency. The probes, the flexible-load solves and the configuration checks use the strict solve, so for them every limit without a proof is a `timeout` (unknown) result; the benchmark without `--accept-incumbent` shows exactly that form. The 192-slot case is the two-day quarter-hour representative horizon, and 384 slots stress a four-day horizon. These results should be repeated on actual Raspberry Pi 5 hardware before setting operational time and memory limits.

## Incremental consumption benchmark

`benchmarks.consumption` solves a base plan and then makes serial full-horizon finite-difference probes. The default case uses 96 quarter-hour source slots, a 24-hour display/reference horizon, hourly display intervals, and a 1 kWh probe. The nondefault case uses 144 quarter-hour source slots, a 12-hour display horizon, 36-hour reference horizon, half-hour display intervals, a 0.5 kWh probe, and 20th/80th percentile thresholds. Both use the default 2-second per-probe cap and give the outlook the remainder of a 60-second pipeline allowance after the base solve.

```sh
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.consumption --scenario default
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.consumption --scenario nondefault
```

| Scenario | Base solve | Total elapsed | Reference coverage | Display costs | Peak RSS |
| --- | ---: | ---: | --- | ---: | ---: |
| Default | 0.0145 s | 0.3211 s | Complete, percentile mode | 24/24 known | 96,204 KiB |
| Nondefault | 0.0198 s | 1.3664 s | Complete, percentile mode | 24/24 known | 98,728 KiB |

Both commands exited successfully in the matched ARM64 container on the Mac development host. These timings are not physical Raspberry Pi 5 measurements. The nondefault run probes all 36 reference hours even though it displays only 12 hours. Actual Pi 5 timing remains to be measured before any control use.

## Consumption probe limits

A consumption probe is the strict full-horizon solve and is known only when it proves its optimum within `probe_time_limit_s` (default 2 s); the probes run one after another inside `total_time_limit_s` (default 60 s) minus the base solve and a one-second reserve. `benchmarks.probes` runs every probe to its proof with a long limit, reports the CPU seconds, and replays the serial loop at the shipped limits:

```sh
python -m benchmarks.probes --strategy self_sufficiency --slots 96
python -m benchmarks.probes --strategy cost_min --slots 192 --display-interval 15
```

Measured at `main` b015ee7 (with the SELF_CONSUME and CHARGE_PV load-following rules), `minimum_mode_minutes` 60, in a native macOS Python 3.14 venv with SciPy 1.18.1 on the Mac development host (Apple M4 Max), not in the container of § Advisory optimizer benchmark; four runs side by side on a host that was also running other work; treat the seconds as ±30 %. The per-strategy times differ from the container's (that section's native column reproduces this table's base plans: `self_sufficiency` 18 s and `backup_ready` 23 s at 192 slots). `--slots 96` is the default planning horizon of 24 h with quarter-hour prices; the probes cover the first 24 h (12 h at 48 slots).

| Slots | Probes | Strategy | Base plan CPU | Probe CPU min / median / max | All probes | Known at 2 s / 60 s |
| ---: | --- | --- | ---: | ---: | ---: | ---: |
| 48 | 12 hourly | `cost_min`, `pv_swap`, `max_export`, `grid_friendly` | 0.2-1.0 s | 0.2 / 0.4-0.5 / 0.9 s | 5-7 s | 12 of 12 |
| 48 | 12 hourly | `self_sufficiency` | 0.4 s | 0.4 / 1.0 / 1.5 s | 12 s | 12 of 12 |
| 48 | 12 hourly | `backup_ready` | 0.2 s | 0.1 / 0.2 / 0.2 s | 2 s | 12 of 12 |
| 96 | 24 hourly | `cost_min` | 0.1 s | 0.1 / 0.1 / 0.3 s | 3 s | 24 of 24 |
| 96 | 24 hourly | `pv_swap` | 0.4 s | 0.2 / 0.3 / 0.5 s | 8 s | 24 of 24 |
| 96 | 24 hourly | `max_export` | 0.1 s | 0.1 / 0.1 / 0.3 s | 4 s | 24 of 24 |
| 96 | 24 hourly | `grid_friendly` | 0.3 s | 0.2 / 0.3 / 0.6 s | 6 s | 24 of 24 |
| 96 | 24 hourly | `self_sufficiency` | 6.6 s | 4.4 / 7.5 / 19.0 s | 195 s | 0 of 24 |
| 96 | 24 hourly | `backup_ready` | 2.3 s | 1.9 / 2.8 / 3.5 s | 65 s | 2 of 24 |
| 192 | 24 hourly | `cost_min` | 2.1 s | 1.0 / 2.5 / 3.8 s | 56 s | 7 of 24 |
| 192 | 24 hourly | `pv_swap` | 0.4 s | 0.3 / 0.4 / 3.2 s | 19 s | 23 of 24 |
| 192 | 24 hourly | `max_export` | 1.1 s | 0.7 / 2.4 / 3.5 s | 53 s | 9 of 24 |
| 192 | 24 hourly | `grid_friendly` | 2.3 s | 0.7 / 2.3 / 4.3 s | 49 s | 12 of 24 |
| 192 | 24 hourly | `self_sufficiency` | 19 s | 9 / 24 / 35 s (every 2nd probe, busier host) | — | none of the 12 measured |
| 192 | 24 hourly | `backup_ready` | 23 s | 11 / 28 / 50 s (every 2nd probe, busier host) | — | none of the 12 measured |
| 192 | 96 quarter-hour | `cost_min` | 1.7 s | 0.5 / 1.8 / 4.3 s | 167 s | 18 of 96 |
| 192 | 96 quarter-hour | `pv_swap` | 0.3 s | 0.3 / 0.7 / 5.2 s | 79 s | 67 of 96 |
| 192 | 96 quarter-hour | `max_export` | 0.9 s | 0.5 / 1.5 / 4.7 s | 158 s | 28 of 96 |
| 192 | 96 quarter-hour | `grid_friendly` | 1.8 s | 0.6 / 1.5 / 4.3 s | 152 s | 38 of 96 |

One unknown full reference probe is enough for the `short_coverage` policy (`coverage_reason=reference_probe_failed`), and an interval whose own probe is unknown has no level. So on this host:

- 12 h horizon: every strategy keeps percentile levels. `self_sufficiency` uses up to 1.5 s of the 2 s probe limit, so a slower host will not fit.
- 24 h horizon (the default): the four fast strategies keep percentile levels with a wide margin. `self_sufficiency` has no known probe and `backup_ready` two of 24, so their consumption levels are unknown for nearly every interval.
- 48 h horizon: no strategy completes its probes, with hourly or quarter-hour display intervals. The base plan of `self_sufficiency` and `backup_ready` takes 17 s and 23 s wall-clock when run alone in this native venv (35 s and 14 s CPU in the container, § Advisory optimizer benchmark; the two builds order the strategies differently), above the 10 s `solve_time_limit_s` in both, so it is published as `time_limited`.
- `minimum_mode_minutes` 0 at 96 slots: `self_sufficiency` 13 of 24 known (probes 1.2-25 s), `backup_ready` 20 of 24 (0.2-10 s) — still the `short_coverage` policy.

Raised limits, wall-clock runs of `analyze_consumption` at 96 slots on the same host:

| Strategy | `probe_time_limit_s` / `total_time_limit_s` | Probes | Result |
| --- | --- | ---: | --- |
| `backup_ready` | 5 s / 120 s | 74 s | 23 of 24 known, `absolute_fallback` |
| `backup_ready` | 10 s / 120 s | 47 s | 24 of 24 known, percentile mode, complete |
| `self_sufficiency` | 30 s / 300 s (both maxima) | 199 s | 24 of 24 known, percentile mode, complete |

Flexible energy depth (`analyze_flexible_loads`, five solves of 3-20 kWh, each bounded by
`probe_time_limit_s` inside what the probes left of the total limit), wall-clock at 96 slots on the same
host: at the default 2 s all five solves of `self_sufficiency` and of `backup_ready` time out and the depth
is unknown, also with the whole 50 s to themselves; `cost_min` finishes all five in 1.2 s, and
`self_sufficiency` at 48 slots in 2.3 s. With `probe_time_limit_s` 10 `backup_ready` finishes all five in
21 s, with 30 `self_sufficiency` in 48 s.

These are fast-desktop numbers, not Raspberry Pi 5 measurements; a slower host reaches each limit at a shorter horizon. The probes stay exact by decision (`docs/adr/0006-consumption-probes-stay-exact.md`); `tests/test_consumption_load_following_limit.py` asserts the 96-slot and 192-slot fallback for the two slow strategies.
