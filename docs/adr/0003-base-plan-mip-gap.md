# ADR-0003: Base plan — no accepted MIP gap; a slow host ends `time_limited`

Status: accepted
Date: 2026-10-12

## Context

`self_sufficiency` and `backup_ready` are slow at long horizons: the 192-slot benchmark (48 h of quarter-hours)
takes 5-7 s of the 10 s base-solve limit on a fast host, so a Raspberry Pi 5 is expected to hit the limit. T-011
and T-022 tightened the formulation without gain. T-022 measured that passing `mip_rel_gap = 3e-3` for the base
plan cuts the benchmark to about 1.5 s with an objective change below 0.3 %, and asked for a decision: accept a
proven non-zero gap, or keep the proof and accept `time_limited` on slow hosts.

What the code does today (`engine/optimize.py`, `_Model.solve`):

- With an episode hurdle active the solve requests `mip_rel_gap = 0` (`exact_mip_gap`, set by
  `constrain_export_benefit` and `constrain_charge_benefit`). The shipped `minimum_export_episode_benefit` is 1,
  so every default installation with a battery already solves the base plan and every probe at gap 0, not at
  HiGHS's default 1e-4. On the benchmark 0 and 1e-4 cost the same time (6.7 s and 8.3 s, 5.5 s and 5.2 s).
- The base plan alone passes `accept_incumbent`: at the time limit the best validated solution is published
  with `Plan.time_limited` and the solver's gap in `Plan.mip_gap`. Probes, flexible-load and configuration
  solves raise `timeout` instead.
- `scipy.optimize.milp` (1.18.1) exposes `mip_rel_gap`, `time_limit`, `node_limit`, `presolve` and `disp`.
  There is no absolute gap, no warm start and no callback.

The relative gap is measured on the weighted MILP objective, not on money. That objective adds the strategy's
penalty terms (import penalty, autonomy and backup shortfall, episode reserves) to the monetary cost. On the
benchmark it is 20.29 against a monetary cost of 11.04 for `backup_ready` and 162.40 against 8.70 for
`self_sufficiency`.

### Measurements

Image `ha-energy-compass-test:2026.9.1`, Apple M4 Max host, `main` at 75c6870. The harness and raw results are
task artifacts of T-026. The reference is always a gap-0 solve.

Benchmark problem, 192 slots, one uncontended run each:

| `mip_rel_gap` | `backup_ready` time | weighted objective | money | `self_sufficiency` time | weighted objective | money |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 (today) | 6.7 s | 20.2909 | 11.0419 | 5.5 s | 162.3963 | 8.7033 |
| 1e-4 | 8.3 s | same | same | 5.2 s | same | same |
| 1e-3 | 5.5 s | same | same | 1.4 s | +0.03 % | −0.13 % |
| 3e-3 | 1.6 s | +0.14 % | +0.26 % | 1.4 s | +0.03 % | −0.13 % |
| 1e-2 | 0.8 s | +0.90 % | +1.65 % | 1.2 s | +0.90 % | +0.19 % |

Random corpus: the benchmark problem with PV scaled 0.3-2.0, load 0.6-2.0, per-slot noise on PV, load and both
prices, initial SOC 10-89 %, battery export allowed in 30 % of problems; 20 problems per strategy and size.
Plans at `3e-3` against the proven optimum (3 of the 20 `backup_ready` 192-slot references were not proven in
30 s and are left out):

| | weighted loss, max | money change | autonomy shortfall change, max | intervals with another mode, max | weighted / money, median (max) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `backup_ready`, 192 | 0.06 (0.23 %) | −0.31 … +0.06 (1.9 %) | 0.18 kWh | 45 of 192 | 1.5 (11) |
| `self_sufficiency`, 192 | 0.86 (0.16 %) | −0.40 … +1.57 (4.1 %) | 0.58 kWh | 34 of 192 | 17 (39) |
| `backup_ready`, 96 | 0.06 (0.25 %) | −0.23 … +0.06 (2.3 %) | 0.14 kWh | 23 of 96 | 1.7 (172) |
| `self_sufficiency`, 96 | 0.62 (0.27 %) | −0.33 … +1.06 (9.6 %) | 1.60 kWh | 20 of 96 | 15 (807) |

Money moves by more than the shipped export-episode hurdle (1) and by up to 9.6 % of the plan's cost, in both
directions, because a fraction of a percent of a penalty-dominated objective is large against the money term.

Slow-host proxy: the same 192-slot corpus with a 2 s limit on this host (roughly the 10 s limit on a host five
times slower; a proxy, not a Raspberry Pi measurement). Loss is against the proven optimum, so the three
unproven `backup_ready` references are left out of the loss rows:

| | gap 0 (today) | gap 3e-3 |
| --- | --- | --- |
| `backup_ready`: proven / `time_limited` / no plan | 2 / 18 / 0 | 9 / 11 / 0 |
| `backup_ready`: true loss, median (max) | 0.000 % (0.92 %) | 0.000 % (0.92 %) |
| `self_sufficiency`: proven / `time_limited` / no plan | 5 / 15 / 0 | 20 / 0 / 0 |
| `self_sufficiency`: true loss, median (max) | 0.000 % (0.06 %, 0.11 absolute) | 0.010 % (0.16 %, 0.86 absolute) |

The incumbent at the limit was never missing and its median distance from the optimum was zero. The gap the
solver reports for those plans (median 0.29 %, max 3.2 % for `backup_ready`) is a bound, not the loss. For
`backup_ready` the looser gap publishes the same plans and leaves 11 of 20 at the limit anyway. For
`self_sufficiency` it stops earlier with a worse plan than the time limit would have produced.

Probes, benchmark with 24 probes and the shipped 2 s per probe: `cost_min` 192 slots 24 of 24 in 11.6 s;
`backup_ready` 96 slots 6 of 24, `self_sufficiency` 96 slots 0 of 24, both at 192 slots 0 of 24, each after
about 48 s and ending in `absolute_fallback` / `reference_probe_failed`. A base plan that proves in 1.5 s
instead of 6 s changes none of these.

## Decision

1. The base plan is not solved to a looser gap. It keeps the tolerance it has today: `mip_rel_gap = 0` while
   an episode hurdle is active (the shipped default), HiGHS's default otherwise. The gap is not a setting.
2. A slow host reaching `solve_time_limit_s` publishes the validated incumbent as `time_limited` with the
   solver's gap. That is the intended outcome for `self_sufficiency` and `backup_ready` at long horizons, not a
   fault state; `solve_time_limit_s` stays the one knob that trades CPU time for proof.
3. `Plan.mip_gap` reports the solver's gap for a completed solve as well, instead of a constant 0.0, so the
   attribute is true when no hurdle is active and HiGHS stops at its default 1e-4 (follow-up ticket).
4. A probe cost is the difference of two solves. Neither side may carry a gap or be an unproven incumbent while
   the other is exact: at `probe_kwh = 1` the money changes above are errors of the same size per kWh, against
   prices of 0.09-0.85. T-027 (probes not finishing for these strategies) has to keep that difference exact or
   change what is differenced, for example by re-solving with the base plan's modes fixed.

Revisit when the solver interface offers an absolute gap in currency or a warm start, when the weighted
objective is split into a strict order of objectives, or when a measurement on real hardware shows base solves
ending with no incumbent.

## Alternatives rejected

1. `mip_rel_gap = 3e-3` for the base plan (the T-022 proposal) — rejected because the bound holds on the
   weighted objective, not on money or on the plan: the corpus shows money changes up to 9.6 %, 1.6 kWh of
   autonomy shortfall and up to 45 of 192 intervals in another mode. That is more than the episode hurdle the
   zero gap was introduced to honour exactly (312ab59). On a slow host it gives no better plan than the
   time-limited incumbent and a worse one for `self_sufficiency`; on a fast host it gives up a proof that fits
   the limit. It saves CPU seconds only, and the probes that follow use 48 s regardless.
2. `mip_rel_gap = 1e-3` — rejected because it does not speed up `backup_ready` (5.5 s against 6.7 s) and
   already moves `self_sufficiency` money by up to 2.6 % on the corpus.
3. A gap setting in Performance — rejected because a unit-less tolerance on a weighted objective cannot be
   calibrated by a user: the same value means 0.06 for one strategy and 0.86 for another. `solve_time_limit_s`
   already exposes the trade in seconds and labels the result.
4. A gap scaled to money (absolute tolerance, or a relative one converted per problem) — rejected because
   `scipy.optimize.milp` has no absolute gap and the conversion needs the optimal objective before the solve; a
   pre-solve to estimate it costs what it is meant to save.
5. Loosen the gap for probes too so they finish — rejected for the reason in decision 4; it is T-027's subject.

## Consequences

- No solver change. Fast hosts keep a proven plan in 5-7 s for the two load-following strategies at 192 slots;
  slow hosts spend the full `solve_time_limit_s` on each recalculation of those strategies and publish
  `time_limited`. The guides already describe that state.
- `tests/test_export_benefit.py` and `tests/test_charge_benefit.py` keep pinning the zero gap for an active
  hurdle. Goldens do not change.
- `mip_gap` may read up to 0.0001 for a proven plan when no hurdle is active (decision 3).
- That is not a hard bound, with or without a hurdle. HiGHS also stops on its absolute gap (`mip_abs_gap`, default
  1e-6), which `scipy.optimize.milp` (1.18.1) does not expose, so `mip_rel_gap = 0` does not switch it off. When the
  plan's objective is within about ±0.01 of zero (import cost offset by export revenue or terminal value) the
  reported relative gap can reach roughly 1e-6 / |objective|. Re-measured in T-036 on 60 random 24/48-slot problems
  per cell (`solve(..., accept_incumbent=True)`, every plan proven): prices of normal size, hurdle on worst 5.5e-16,
  hurdle off worst 9.97e-5; prices x 0.01 (objective 1e-3 to 0.14) hurdle on worst 2.4e-6, off 9.9e-5; prices
  x 0.0001 (objective 1e-5 to 1e-3) 14 of 60 above 1e-4 with the hurdle on (worst 2.6e-2) and 15 of 60 with it off
  (worst 3.3e-3). `Plan.time_limited`, not `mip_gap == 0`, tells a proven plan from an unproven one.
- The sentence T-022 adds to `docs/model.md` ("proves its optimum to HiGHS's default relative gap of 1e-4") is
  wrong for the default configuration and is corrected by the follow-up ticket.
- Solve time for these strategies remains a formulation and horizon question (fewer binaries, coarser
  intervals beyond the first day). The consumption levels are the user-visible damage and are owned by T-027.
