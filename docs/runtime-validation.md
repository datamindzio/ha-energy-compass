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

The deterministic `benchmarks.optimizer` workload varies buy and sell prices, solar generation and household load across quarter-hour slots, with a 13.5 kWh battery and finite grid and inverter limits. `--strategy all` solves it once per dispatch strategy, in one process. These measurements used the matched aarch64 image on a Mac host (one run each; the solver is deterministic but wall time varies by about ±15 %). They measure that container and host combination, not Raspberry Pi 5 hardware. Peak RSS is the process high-water mark, including Python and SciPy startup and every strategy solved before it.

```sh
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.optimizer --slots 192 --time-limit 10 --strategy all
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.optimizer --slots 384 --time-limit 10 --strategy all
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.optimizer --slots 384 --time-limit 0.000001 --strategy all
```

| Slots | Time limit | Strategy | Result | Variables / rows | Solve call elapsed | Peak RSS |
| ---: | ---: | --- | --- | ---: | ---: | ---: |
| 192 | 10 s | `cost_min` | Optimal | 5,368 / 7,799 | 2.8-6.1 s | 277,988-278,808 KiB |
| 192 | 10 s | `self_sufficiency` | Time limit | 5,369 / 7,991 | 10.0 s | 283,708-284,396 KiB |
| 192 | 10 s | `backup_ready` | Time limit | 5,369 / 7,991 | 10.0 s | 283,708-284,396 KiB |
| 192 | 10 s | `pv_swap` | Optimal | 5,369 / 7,991 | 0.4-1.1 s | 283,708-284,396 KiB |
| 192 | 10 s | `max_export` | Optimal | 5,176 / 7,220 | 1.4-3.0 s | 283,708-284,396 KiB |
| 192 | 10 s | `grid_friendly` | Optimal | 5,753 / 8,375 | 1.2-1.9 s | 283,708-284,396 KiB |
| 384 | 10 s | `cost_min` | Time limit | 10,742 / 15,599 | 10.1 s | 343,788-346,024 KiB |
| 384 | 10 s | `self_sufficiency` | Time limit | 10,743 / 15,983 | 10.1 s | 343,788-346,024 KiB |
| 384 | 10 s | `backup_ready` | Time limit | 10,743 / 15,983 | 10.1 s | 343,788-346,024 KiB |
| 384 | 10 s | `pv_swap` | Time limit | 10,743 / 15,983 | 10.1 s | 343,788-346,024 KiB |
| 384 | 10 s | `max_export` | Optimal | 10,358 / 14,442 | 7.3-7.7 s | 343,788-346,024 KiB |
| 384 | 10 s | `grid_friendly` | Optimal | 11,511 / 16,751 | 1.9 s | 343,788-346,024 KiB |
| 384 | 0.000001 s | each of the six | `SolveError(reason="timeout")` | 10,358-11,511 / 14,578-16,887 | 0.05-0.12 s | 262,664-264,256 KiB |

"Time limit" is the benchmark's strict solve ending at the limit with no proof (`SolveError(reason="timeout")`); the cells with two numbers are the minimum and maximum of three runs. The host ran other container jobs at the same time (load average about 10), which is why `cost_min` at 192 slots spans 2.8-6.1 s; the results (Optimal / Time limit) were the same in all three runs. Wall time at a limit therefore says little about how far a solve is from finishing, so the next table gives the time to a proven optimum with the limit lifted to 300 s (`time.process_time()` of one `solve()` call, same workload, same host):

| Slots | `cost_min` | `self_sufficiency` | `backup_ready` | `pv_swap` | `max_export` | `grid_friendly` |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 192 | 2.8 s | 34.7 s | 13.7 s | 0.3 s | 1.2 s | 0.9 s |
| 384 | 12.0 s | 38.8 s | 18.8 s | 19.6 s | 7.6 s | 2.2 s |

These numbers replace the earlier ones, which predate the CHARGE_PV surplus-following rule (T-010; before it every strategy at 192 slots proved within 0.65 s except `self_sufficiency` at about 7 s and `backup_ready` at about 9 s). That rule adds one row per PV interval, and a binary (`charge_pv_full`, "the pack ends on its ceiling") where the pack can reach its ceiling, so a planned CHARGE_PV interval charges all the PV surplus or fills the pack. It is what moved the default `cost_min` 192-slot solve from about 0.6 s to 2-3 s, put `self_sufficiency` and `backup_ready` past the 10 s base-solve limit, and made `cost_min` and `pv_swap` at 384 slots miss it. `self_sufficiency` and `backup_ready` were already the slow strategies because they make SELF_CONSUME worth using all night: the load-following rule (see `docs/model.md`) then leaves a small gap that HiGHS needs a few hundred branch-and-bound nodes to close (the plan itself is found after 2-3 s, the rest is the proof). The model was not changed to recover the time (`docs/model.md` § Cost of the rule lists what was measured).

What the user sees when the base solve ends at its limit (`solve_time_limit_s`, default 10 s) is the plan, not an error. The best validated solution found so far is published, `optimizer_status` reads `ready` as for a proven plan, and the plan carries `time_limited: true` with `mip_gap`, the bound on how far its weighted objective can be from the optimum (`docs/guide.en.md` § Plan attributes for strategies). The same benchmark workload with `accept_incumbent`, which is what the runtime passes for the base plan, ended at the 10 s limit as follows:

| Slots | Strategy | `time_limited` | `mip_gap` at the limit |
| ---: | --- | --- | ---: |
| 192 | `self_sufficiency` | yes | 0.70 % |
| 192 | `backup_ready` | yes | 0.10 % |
| 384 | `cost_min` | yes | 0.007 % |
| 384 | `self_sufficiency` | yes | 0.51 % |
| 384 | `backup_ready` | yes | 0.03 % |
| 384 | `pv_swap` | yes | 0.10 % |

The other six strategy/horizon cases of the table ended proven. The gap is a bound, not the error: at 192 slots the `backup_ready` plan published at the limit has the optimal objective (11.0419, equal to the proven run) and `self_sufficiency` is 0.16 % above its optimum (8.7172 against 8.7033), inside its 0.70 % bound. A Raspberry Pi 5 is slower than this host, so it publishes `time_limited` plans for more cases than the table shows; raise `solve_time_limit_s` (0.1-30 s, below `total_time_limit_s`, which bounds base solve plus probes at 1-300 s, default 60 s) when a proven optimum matters more than refresh latency. The benchmark itself does not pass `accept_incumbent`, so its 0.000001 s rows show the strict solve that probes, flexible-load and configuration checks use: a limit without a proof raises `SolveError("timeout")`. All commands exited successfully; the script reports typed solver outcomes as JSON. The 192-slot case is the two-day quarter-hour representative horizon, and 384 slots stress a four-day horizon. These results should be repeated on actual Raspberry Pi 5 hardware before setting operational time and memory limits.

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

Measured at `main` b015ee7 (with the SELF_CONSUME and CHARGE_PV load-following rules), `minimum_mode_minutes` 60, Python 3.14 venv with SciPy 1.18.1 on the Mac development host (Apple M4 Max), four runs side by side on a host that was also running other work; treat the seconds as ±30 %. `--slots 96` is the default planning horizon of 24 h with quarter-hour prices; the probes cover the first 24 h (12 h at 48 slots).

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
- 48 h horizon: no strategy completes its probes, with hourly or quarter-hour display intervals. The base plan of `self_sufficiency` and `backup_ready` takes 17 s and 23 s wall-clock when run alone, above the 10 s `solve_time_limit_s`, so it is published as `time_limited`. The base-plan table above predates the CHARGE_PV load-following rule and still shows 5.28 s and 7.30 s for them and 0.42 s for `cost_min` (now about 1.4 s).
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
