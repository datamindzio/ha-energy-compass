# ADR-0007: The CHARGE_PV surplus-following rule stays in the model; its solve time is accepted

Status: accepted
Date: 2026-10-13

## Context

T-010 made `CHARGE_PV` follow the PV surplus (`engine/dispatch_policy._follow_surplus`): the Deye controller
writes a charge cap for that mode, so the inverter charges with every watt the house does not use, and a
planned partial charge beside grid export is not a state it can hold. The rule costs solve time in the default
mode model (`minimum_mode_minutes` 60). Three things were left open:

- The owner was asked before the merge (T-021: `cost_min` 0.29 → 2.8 s at 192 slots, `self_sufficiency` and
  `backup_ready` to the 10 s limit, probes at 192 slots falling back for every strategy) and chose "accept,
  optimise in T-011" over "no rule, the controller limits the charge current" and "rule in the validator only".
  T-011 had merged before that answer and reformulated the minimum-duration rows, not these.
- ADR-0006 was written without that decision and says the regression "is not accepted by this ADR".
- T-039 measured the cost again and found no tightening; T-041 and T-042 (the same rule with
  `minimum_mode_minutes = 0`) name lazy rows as the remedy and use the default-mode time as their reference.

Cost on the shipped benchmark (`benchmarks/optimizer.py`, CPU seconds to a proven optimum, container
`ha-energy-compass-test:2026.9.1` on an Apple M4 Max, measured in T-039; `docs/runtime-validation.md`
§ Advisory optimizer benchmark has the tables, the native-venv numbers and the commands):

| Strategy | 192 slots before T-010 | 192 slots | 384 slots |
| --- | --- | --- | --- |
| `cost_min` | 0.64 | 2.7 | 11.9 |
| `self_sufficiency` | 7.0 | 34.8 | 36.7 |
| `backup_ready` | 8.7 | 14.1 | 18.5 |
| `pv_swap` | 0.41 | 0.3 | 20.0 |
| `max_export` | 0.53 | 1.3 | 7.7 |
| `grid_friendly` | 1.10 | 0.9 | 2.2 |

`horizon_hours` is 24 by default and 48 at most (`settings.py`), so with quarter-hour prices the longest
problem has 192 slots; 384 slots is the benchmark's four-day stress point, not a horizon that can be set.

Measured for this ADR (same container, `main` at afef29c, CPU seconds, several containers side by side; the
script is the task artifact `t044_lazy_rows.py` of T-044, not committed because it patches private functions
for a design that is rejected below). A lazy loop solves without the `_follow_surplus` rows, adds them for
the intervals `_check_surplus_following` rejects and solves again until the plan passes; every round is a
cold solve, `scipy.optimize.milp` has no warm start and no callback (ADR-0003).

| Benchmark, 192 slots | Full model | Rule-free first solve | Lazy, rejected intervals only | Lazy, rejected ± 8 intervals |
| --- | --- | --- | --- | --- |
| `cost_min` | 2.6 | 0.52 | 5.9 (14 rounds) | 3.8 (4) |
| `self_sufficiency` | 35.2 | 6.0 | no plan in 200 s (22) | 66.4 (5) |
| `backup_ready` | 14.5 | 7.0 | 193 (11) | 32.9 (3) |
| `pv_swap` | 0.28 | 0.34 | 5.0 (14) | 1.8 (4) |
| `max_export` | 1.35 | 0.37 | 4.9 (10) | 3.4 (4) |
| `grid_friendly` | 0.98 | 0.88 | 19.6 (18) | 5.4 (5) |

- Benchmark at 96, 192 and 384 slots, six strategies, both loop variants (36 runs): the rule-free plan breaks
  the rule in every one, so the loop never ends after one solve; it is slower than the full model in 35 runs
  and level in one (`cost_min`, 384 slots, ± 8: 11.8 s against 12.0 s). At 96 slots: `cost_min` 0.10 s full
  against 1.3 s (15 rounds) and 0.38 s (4); `self_sufficiency` 6.3 s against 29.9 s and 13.1 s.
- Seeded random variations of the benchmark household (another sky per day, load, per-slot prices and starting
  charge; six seeds × six strategies at 96 slots, three seeds at 192, loop with ± 8 intervals; equal objectives
  in all 54):

  | Corpus | Full model | Rule-free solve | Lazy loop | Rule-free plan already obeys the rule | Loop more than 10 % faster |
  | --- | --- | --- | --- | --- | --- |
  | 96 slots, 36 problems, total CPU | 61.6 s | 46.7 s | 103.9 s | 18 | 3 |
  | 192 slots, 18 problems, total CPU | 150.8 s | 97.1 s | 208.0 s | 8 | 0 |

  Medians at 192 slots, full model against rule-free: `cost_min` 5.8 / 4.7 s, `pv_swap` 5.1 / 5.3 s,
  `max_export` 6.2 / 6.2 s, `grid_friendly` 8.5 / 6.7 s, `backup_ready` 15.8 / 8.1 s, `self_sufficiency`
  3.6 / 0.8 s.
- What the rule changes in the plan (`Plan.objective`, the plan cost in currency, full model against the
  rule-free plan): nothing on the benchmark, where all 18 rule-free plans break the rule at an equal cost (a
  tied plan with a partial charge). On the corpus every one of the 28 rule-free plans that break the rule
  has another cost: the plan with the rule is dearer in 27, by more than 1 % in 12 and by up to 4.8 %
  (median 0.5 %). That difference is a saving the rule-free plan reports and the inverter does not deliver.

So the rule costs 4-7 times on the regular benchmark shape and about 1.3 times (96 slots) to 1.6 times
(192 slots) of total CPU on irregular inputs, where five of the six strategies take 4-30 s at 192 slots with
or without it. The corpus draws prices, PV and load independently per slot, which is rougher than a real
forecast.

## Decision

1. The rule stays in the model on every interval where `CHARGE_PV` is available, in the form T-010 shipped.
   It is a correctness requirement of the plan: without it the published energy flows, cost and state of
   charge are not what the inverter does in that mode (on the corpus the plan cost is understated by up to
   4.8 %).
2. Its solve-time cost in the default mode model is accepted. This records the owner's decision T-021 with the
   numbers above. The "optimise later" attached to it is closed: the attempts are listed under Alternatives
   rejected, none is adopted, and no formulation task is opened.
3. What is accepted, as the user sees it:
   - 24 h horizon (default): no published state changes. All six strategies prove the benchmark base plan
     (0.1-6.3 s CPU in the container); the consumption probes are as ADR-0006 states them.
   - 48 h horizon (the maximum; 192 slots with quarter-hour prices): the base plan of `self_sufficiency` and
     `backup_ready` reaches the 10 s `solve_time_limit_s` and is published as `time_limited` with its
     `mip_gap` (0.7 % and 0.1 % of the weighted objective on the benchmark); the other four prove it. No
     strategy completes its consumption probes, so levels follow the `short_coverage` policy with
     `coverage_reason=reference_probe_failed`, and the flexible energy depth shares that budget (ADR-0006).
   - A host several times slower than the development host meets the same states at shorter horizons
     (ADR-0003, decision 2). `solve_time_limit_s`, `probe_time_limit_s` and `total_time_limit_s` stay settings.
4. Lazy rows are not adopted for the mode model. The loop was not measured with `minimum_mode_minutes = 0`
   (T-034, T-041), which builds the same rows; its option "lazy constraints first" in T-042 has no measured
   speed behind it, and the owner did not take it (decision 6). A lazy loop also cannot return a plan where the
   full model has none: it stops only on a plan that obeys the rule in every interval, which is a plan of the
   full model.
5. A change to the `_follow_surplus` rows is measured on all six strategies at 96 and 192 slots with
   `benchmarks/optimizer.py` and on a seeded corpus per strategy (LESSONS.md), before and after, and updates
   the tables in `docs/runtime-validation.md` in the same PR (ADR-0006, decision 4).
6. The same cost is accepted with `minimum_mode_minutes = 0` (owner, T-042, answer A, 2026-10-13: "accept the
   cost, close T-034"), against "no rule at duration 0" and "T-041 (lazy constraints) first". The owner was
   shown the per-strategy table of `docs/model.md` § Operating-mode duration and PV export budget (CPU seconds
   of one solve at 96 / 192 / 384 intervals, before → with the rule, `*` = 10 s limit: `cost_min` 0.04 → 0.46,
   0.24 → 1.06, 0.86 → 7.80*; `self_sufficiency` 2.23 → 3.38, 3.68 → 5.37, 4.19* → 7.81*; `backup_ready`
   1.67 → 1.41, 5.76 → 10.24*, 5.97 → 8.19*; `pv_swap` 0.05 → 0.12, 0.06 → 1.20, 1.01 → 3.99; `max_export`
   0.03 → 0.09, 0.10 → 0.19, 0.71 → 0.76; `grid_friendly` 0.07 → 0.35, 0.49 → 0.73, 2.04 → 6.35), the reference
   that the default duration of 60 takes 2.8 s (`cost_min`, 192 intervals) and reaches the limit at 384, and
   that 1 problem in 300 of the seeded sweep loses its plan (export cap or inverter below the PV, curtailment
   off, pack full before the last surplus; the mode model has no plan there either). Re-checked at `main`
   5c11c2e with the rule merged: the same picture, objectives equal where both prove. Fewer consumption probes
   finish at duration 0 as well (`docs/runtime-validation.md` § Consumption probe limits). T-041 keeps the
   no-plan class.

Revisit when the solver interface offers lazy constraints inside one solve or a warm start (the loop above
then stops paying for cold solves), when a reformulation proves the 192-slot benchmark base plan of
`self_sufficiency` and `backup_ready` inside 10 s and a 192-slot `cost_min` probe inside 2 s without a slower
corpus median for any strategy, or when the owner asks for consumption levels on a 48 h horizon.

## Alternatives rejected

1. Lazy rows: solve without the rule and add its rows where the plan breaks it — rejected because it is slower
   in 35 of 36 benchmark runs and in total on both corpora (tables above). The rule-free optimum breaks the
   rule in all 18 benchmark problems and in 28 of 54 corpus problems, each repair moves the violation to
   another interval, and every round pays a full cold solve. With a time limit the loop can also end with no
   rule-compliant plan where the full model publishes a `time_limited` one.
2. Tighter rows — rejected because none gave a gain beyond noise: tying `charge_pv_full` and the power binary
   to the CHARGE_PV indicator, a tighter big-M floor, no relief binary (T-039, `docs/model.md` § Cost of the
   rule); a cut on the previous interval's energy and removing the couplings (T-010); reusing `battery_mode`
   as the indicator, a reach cut and a small cost on the fill binary in the unmoded form (T-034).
3. No rule in the model, the controller limits the charge current to the planned energy (T-021, option B) —
   rejected by the owner: it moves the mismatch into the controller, which would have to throttle the charge
   against a surplus that changes inside the interval.
4. The rule in `validate_modes` only (T-021, option C) — rejected by the owner, and the measurement above
   shows the price: the rule-free optimum fails the check in 18 of 18 benchmark and 28 of 54 corpus problems,
   and each of those would be a `solver_failure` with no plan.
5. A relative MIP gap or an accepted incumbent in place of the proof — rejected by ADR-0003 for the base plan
   and by ADR-0006 for the probes; nothing measured here changes those numbers.
6. A higher default `solve_time_limit_s` — rejected because `self_sufficiency` at 192 slots needs 35 s of CPU
   in the container (18 s in the native venv) against a maximum of 30 s for the setting, and the default
   strategy would wait longer on every host for a proof that changes the plan's weighted objective by at most
   0.7 %.
7. The rule only on short horizons or only for some strategies — rejected because whether the inverter can
   hold a partial charge does not depend on the horizon or the strategy; a plan without the rule is as wrong
   at 48 h as at 24 h. Not measured.

## Consequences

- No engine change. ADR-0006 no longer leaves the regression open; its consequence line points here.
- The reference point of T-042 (default-mode solve time) is an accepted state, not an open regression, and T-042
  itself is decided (decision 6): the rule ships with `minimum_mode_minutes = 0` in T-034.
- T-041 keeps its first problem (no plan on a capped site when the pack fills early); lazy rows are not a
  remedy for it (decision 4) and are not a measured remedy for its solve time.
- Consumption levels on a 48 h horizon do not depend on this rule alone: on the irregular corpus the rule-free
  model needs 4.7-8.1 s (median) for five of the six strategies against a 2 s probe limit. Removing the rule
  would not bring them back there; it did on the regular benchmark shape.
- The shipped benchmark is one regular shape and overstates the rule's share. Solve-time claims about these
  rows need the corpus beside it (decision 5).
