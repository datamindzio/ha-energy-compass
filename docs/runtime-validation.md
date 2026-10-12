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
