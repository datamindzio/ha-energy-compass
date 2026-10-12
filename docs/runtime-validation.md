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
| 192 | 10 s | `cost_min` | Optimal | 5,300 / 7,663 | 0.42 s | 264,232 KiB |
| 192 | 10 s | `self_sufficiency` | Optimal | 5,301 / 7,855 | 5.28 s | 284,848 KiB |
| 192 | 10 s | `backup_ready` | Optimal | 5,301 / 7,855 | 7.30 s | 284,848 KiB |
| 192 | 10 s | `pv_swap` | Optimal | 5,301 / 7,855 | 0.33 s | 284,848 KiB |
| 192 | 10 s | `max_export` | Optimal | 5,108 / 7,084 | 0.36 s | 284,848 KiB |
| 192 | 10 s | `grid_friendly` | Optimal | 5,685 / 8,239 | 0.77 s | 284,848 KiB |
| 384 | 10 s | `cost_min` | Optimal | 10,606 / 15,327 | 1.68 s | 301,720 KiB |
| 384 | 10 s | `self_sufficiency` | Optimal | 10,607 / 15,711 | 8.77 s | 313,008 KiB |
| 384 | 10 s | `backup_ready` | Optimal | 10,607 / 15,711 | 6.25 s | 316,664 KiB |
| 384 | 10 s | `pv_swap` | Optimal | 10,607 / 15,711 | 0.71 s | 316,664 KiB |
| 384 | 10 s | `max_export` | Optimal | 10,222 / 14,170 | 2.99 s | 319,404 KiB |
| 384 | 10 s | `grid_friendly` | Optimal | 11,375 / 16,479 | 1.28 s | 319,404 KiB |
| 384 | 0.000001 s | each of the six | `SolveError(reason="timeout")` | as above | 0.05-0.10 s | 261,716-263,532 KiB |

`self_sufficiency` and `backup_ready` are the slow strategies because they make SELF_CONSUME worth using all night: the load-following rule (see `docs/model.md`) then leaves a 0.1 % gap that HiGHS needs a few hundred branch-and-bound nodes to close (the plan itself is found after 2-3 s, the rest is the proof). On a 192-slot horizon they use 50-70 % of the 10 s base-solve limit even on this fast host, so a Raspberry Pi 5 is expected to end `time_limited`; the base plan then publishes its best validated incumbent (`Plan.time_limited`, `Plan.mip_gap`). The other four strategies stay well under a second at 192 slots. The benchmark itself does not pass `accept_incumbent`, so its 0.000001 s rows show the strict solve that probes, flexible-load and configuration checks use: a limit without a proof raises `SolveError("timeout")`. All commands exited successfully; the script reports typed solver outcomes as JSON. The 192-slot case is the two-day quarter-hour representative horizon, and 384 slots stress a four-day horizon. These results should be repeated on actual Raspberry Pi 5 hardware before setting operational time and memory limits.

## Consumption probes at 96 and 192 intervals

`benchmarks.optimizer --probes 96` solves the base plan and then the consumption probes of one 24 h hourly display/reference horizon (`--probes N` is the number of quarter-hour probes, a multiple of 4) and applies the acceptance rule: result `optimal`, every probe complete, base plus probes within 60 s. Probes keep the base plan's operating modes (`docs/model.md`, *Probe operating modes*). Before that every probe of `self_sufficiency` and `backup_ready` timed out at 96 and 192 intervals (0/96 at 192 for both minimum durations) and the levels fell back to `absolute_fallback`. These runs used a native macOS arm64 Python 3.13 with SciPy 1.18.1 and 10 s base limit, one run each, with `minimum_mode_minutes` at the default 60 and at 0 (a wrapper passing `minimum_mode_minutes=0` to the problem):

| Intervals | Strategy | Minimum mode | Probes complete | Base solve | Probes |
| ---: | --- | ---: | ---: | ---: | ---: |
| 192 | `self_sufficiency` | 60 min | 96/96 | 3.6 s | 7.4 s |
| 192 | `self_sufficiency` | 0 | 96/96 | 2.6 s | 8.3 s |
| 192 | `backup_ready` | 60 min | 96/96 | 6.9 s | 4.1 s |
| 192 | `backup_ready` | 0 | 96/96 | 4.5 s | 4.1 s |

`--strategy all` completes 96/96 for all six strategies at 96 intervals (probes 1.9-2.3 s) and at 192 intervals (probes 4.3-6.5 s). `tests/test_consumption_pinned.py` asserts the 96-interval case for both strategies and both durations. Repeat on Raspberry Pi 5 hardware before setting limits.

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
