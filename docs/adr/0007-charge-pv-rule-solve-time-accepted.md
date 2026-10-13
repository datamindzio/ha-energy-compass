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
   finish at duration 0 as well (`docs/runtime-validation.md` § Consumption probe limits). T-041 first kept the
   no-plan class; the owner then decided otherwise (T-062, answer B, 2026-10-13), see Amendment.

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
- T-041's first problem (no plan on a capped site when the pack fills early) is planned by the forced spill
  of the Amendment, not by lazy rows (decision 4), which are not a measured remedy for it; the Amendment
  names what stays unplanned.
- Consumption levels on a 48 h horizon do not depend on this rule alone: on the irregular corpus the rule-free
  model needs 4.7-8.1 s (median) for five of the six strategies against a 2 s probe limit. Removing the rule
  would not bring them back there; it did on the regular benchmark shape.
- The shipped benchmark is one regular shape and overstates the rule's share. Solve-time claims about these
  rows need the corpus beside it (decision 5).

## Amendment (T-041, owner decision T-062 B)

The no-plan class of decision 6 is narrowed by building the exception the rule needed, not closed: most of it
is planned now and the rest is measured and named below. A site that cannot curtail (`allow_curtailment` off)
and whose export cap or inverter rating is below the PV spills the PV that its pack cannot take: the inverter
throttles by itself, so the plan carries it as `CURTAIL` at cost 0 (the Deye controller runs its `HOLD`
profile) instead of ending `infeasible`.

- The spill is the need `max(PV − load − flexible load − grid_export_kw × h, PV − inverter_kw × h, 0)` and no
  more (with a minimum mode duration at least the minimum activity: a smaller need is offered as that much),
  only on a site with a battery, only in an interval whose pack cannot take the need (`previous energy ≥
  ceiling + standby loss − eta_charge × need`, `dispatch_policy.spill_gate`), and never because the sell price
  is negative: a pack that can take the need charges. The gate reads the room of the pack, so a pack with
  0.05 kWh of room spills where one with 1 kWh does not; reading "full" (the first version) left every pack
  with less room than the need without a plan in any mode. `engine.optimize._gate_forced_spill` writes it (one
  binary per interval with a need); the solution validator re-checks both limits independently.
- With a minimum mode duration a `CURTAIL` interval charges and discharges nothing, so the pack of a run that
  began at the gate drifts below it by its standby loss: the next interval's gate stays open when the interval
  before it was `CURTAIL` (`_relay_forced_spill`), and the validator accepts a spill only from an interval
  that starts at the gate or from a `CURTAIL` chain rooted at one. The unmoded model charges the loss back and
  needs no relay. The first version closed the gate after two intervals of standby loss. The chain is also
  rooted at a carried `CURTAIL` commitment (`dispatch_policy.carried_curtail`, model and validator): the second
  version closed the first interval on the pack's level alone, so a run followed for one interval had no plan
  once the need dropped below the room the standby loss had made. The price is that a `CURTAIL` commitment
  read with a pack below full (a hand-set state) is planned as a spill while the need lasts.
- A PV-follow dwell may end in a forced `CURTAIL` (a fresh `CHARGE_PV` run that fills the pack inside its
  minimum duration): ADR-0005's release for a carried PV-follow commitment with a full pack, applied to a run
  begun inside the plan. The dwell is not shortened or restarted; only the way it may end is wider. The
  hand-over holds through the whole `CURTAIL` run, its bridges included, and where the need ends with the
  pack full the run ends in a `HOLD` that starts its own clock (`dispatch_policy._fill_releases`, certified
  from a full pack only, offered within a minimum duration of an interval with a need or of a carried
  `CURTAIL`): the second version left a need that ended inside the PV-follow dwell without a legal mode.
- HiGHS is not sound on a model with the gate and relay rows: with presolve it returned a binary at 0.16 as
  optimal ("invalid solver result: fractional mode_CURTAIL") and reported a problem with a plan as infeasible.
  `optimize._Model.solve` gives a problem that has forced-spill binaries a second attempt without presolve when
  it comes back infeasible or with a fractional binary, within the remaining time budget. A site that can pass
  its PV has no such binary and is solved once (T-074: not once when its model has mode rows). The run without
  presolve is not sound either and neither run is a proof: see Amendment (T-072).
- Measured with `benchmarks/spill_sweep.py` (seeds 11 and 12 on main 5baf226 and here with that main merged in, 300 per seed and draw,
  the export-episode hurdle at zero so that a "worse" plan is not a hurdle artefact; every plan through
  `optimize.solve_released` as the runtime makes it, T-058): of 2400 problems drawn
  once (random PV, negative prices, flexible-load probes, a bell-shaped day with cloud dips) 533 of 589 are
  planned at minimum mode duration 0 (main 171), 565 of 606 at 15 minutes (160), 582 of 620 at 30 minutes
  (149), 503 of 585 at 60 minutes (97); of 1800 problems followed one interval at a time (600 of them a
  bell-shaped day to its end), 1625 are followed to the end (main 383): bell day 207 of 214 at 15 minutes,
  172 of 189 at 30, 158 of 197 at 60. No problem planned on main became infeasible, none ended in a solver
  failure, no rolling problem that main followed stops at a boundary now, and one plan is dearer beyond 1e-6
  (a bell day by 0.115: main plans it only on the last rung of `solve_released`, without the carried
  commitment, the spill plans it with the commitment kept). `tests/test_unmoded_charge_pv_sweep.py`: 247 of 300
  against 227 on main. The tables of `docs/model.md` have every draw and duration.
- Still unplanned at T-041, measured and pinned by `tests/test_forced_spill.py` (`test_pinned_limit_*`): of
  the 217 of 2400 without a plan (none has one on main), 139 were a pack with room whose charge power is below
  the surplus (closed by T-066: the part the pack cannot take by its charge power is spilled whatever the room,
  see the Amendment T-066 below and `docs/model.md`), 30 a carried dwell the model cannot end (open, pinned), 9
  have a base plan and none for the flexible-load probe on top, 1 is infeasible with curtailment on as well and
  38 are not characterized.
  Of the 175 rolling problems not followed to the end, 165 have no first plan, 8 stop at a problem without a
  plan on any site (T-057) and 2 only on a site that cannot curtail: one where main stops at the same
  boundary, and one `CURTAIL` run of 60 minutes with a gap in its need, started afresh because the safety
  exception kept the `CHARGE_PV` commitment over the forced `CURTAIL` (pinned, as is the `HOLD` with a clock
  of its own that lost the full level to standby loss before a need arrived). In the mode model
  a `CURTAIL` interval charges nothing, so the room of a pack below the gate stays empty while the surplus
  lasts, until replanning reads the real state of charge.
- Solve time: the benchmark site can pass its PV, so the rows and variables are identical to main (measured on f6eb8f2) and
  the per-strategy table of `docs/model.md` shows only measurement noise.

## Amendment (T-072): neither HiGHS run is a proof on a forced-spill model

The T-041 amendment named presolve as the cause of the two wrong results and the run without presolve as the
remedy. The run without presolve is wrong too, in a different way, so the second run of `_Model.solve` is a
second attempt that the certificate accepts or rejects, not a proof.

Measured on the 96-slot day of `tests/test_forced_spill.py` (`winter_day`, draw #61 of the T-041 review: 3 kW
inverter, 2 kW export cap, 10 kWh pack at 7.06, standby loss 0.02 kW, no minimum mode duration, `cost_min`),
SciPy 1.18.1 with HiGHS 1.12.0 in the test image, scripts and model files in the T-072 artifacts:

- Without presolve HiGHS reports `Optimal` at −1.746457 (five seeds, also at a relative gap of 0); the default
  run returns −4.504109 (five seeds), a point that satisfies every row to 3e-15 with exact integers.
- The root cuts cut the optimum off: bound −6.19 after the first relaxation, −1.76 after 612 root cuts. Given
  the feasible point as a start, or with the cut pool limited to one cut (`mip_pool_soft_limit`), the run
  without presolve returns −4.504109. With `mip_feasibility_tolerance` at 1e-9 it reports the model as
  infeasible.
- The model as an MPS file gives −1.746457 without presolve on HiGHS 1.8.1, 1.9.0, 1.10.0, 1.11.0, 1.12.0,
  1.13.1, 1.14.0 and 1.15.1, three seeds each: it is not a regression and no SciPy upgrade removes it. (With
  presolve, 1.8.1 to 1.10.0 stop at −4.499109.)
- Rows: without the 30 gate rows, without the 30 `curt ≤ bound × spill_open` rows, or with `spill_open`
  continuous, the two runs agree; with `spill_open` fixed at the feasible point they agree. With every binary
  but six fixed at the feasible point, 171 rows are left that HiGHS 1.12.0 without presolve calls infeasible
  after 41 root cuts, while two of the 64 assignments are feasible with a wide range of pack energy. That
  core holds two gate rows, two `curt ≤ bound × spill_open` rows, the full-pack rows of the CHARGE_PV rule,
  the rows that tie the battery flows to `discharging` and `battery_mode`, and the energy balance; another seed, HiGHS 1.8.1, or the same rows with
  the unused columns removed solve it. The forced-spill rows are needed for the wrong result on this day and
  are not the whole cause.
- Not re-measured here, from the T-041 review: 2 wrong optima without presolve in about 860 planned problems
  of 48, 96 and 192 slots, none where the default run was the worse one (the T-066 review found one, see its
  amendment below), 0 of 457 with curtailment on, a false
  infeasible with presolve in 1 of 60 at 96 slots. The 0 of 457 does not clear the models without forced-spill
  rows (at 2 in 860, 457 clean problems happen 35 % of the time); those models never take the run without
  presolve, and no wrong result of their default run is known (both halves are void since T-074: the models
  with mode rows take it, and one of them had a false `infeasible`).

Decision:

1. HiGHS is treated as untrusted on these models. What makes a plan safe to publish is the certificate
   (`_validate_solution`, `validate_modes`), which does not depend on the solver. Optimality and "no plan" are
   what the solver says, not what is proven.
2. The guard stays as T-041 built it: one run with presolve; a model with forced-spill binaries that comes
   back infeasible or with a fractional binary gets a second attempt without presolve in the remaining time.
   The flag on the model is `second_attempt` (T-074 widens the models that carry it to every model with mode
   rows).
3. The forced-spill formulation is not changed and no HiGHS setting is changed.
4. A plan from the second run is reported as any other, with the `mip_gap` HiGHS reports. It may be far from
   the optimum until the next recalculation: of the order of 1 solve in 25 000 on a site that cannot curtail
   (1 in 60 takes the second run with a plan to find, 2 in 860 of those are wrong; two rates from small
   counts; the models with mode rows only are in Amendment T-074). An `infeasible` from the second run takes
   the rungs of `solve_released`.
5. The day is pinned twice in `tests/test_forced_spill.py`: the plan is at −4.5041 or better, and the run
   without presolve returns −1.7465 (`test_pinned_limit_highs_without_presolve_calls_a_worse_plan_optimal`).
   The second test goes red when SciPy ships a HiGHS that behaves differently; this amendment and
   `docs/model.md` are then re-measured, not the test weakened.

Alternatives rejected:

1. Reformulate the gate (complemented binary, a gate tightened to the reachable energy, one binary per run
   of intervals) — rejected because it cannot be shown to help: the defect is in the solver's cuts, the reduced
   core needs rows of three other rules, a changed model changes the search path so a pass on one day shows
   nothing (the reduced core passes with another seed), and at 2 in 860 a sweep needs several thousand 96-slot
   models per variant to show an effect. None was tried.
2. A second attempt with another HiGHS setting (seed, cut pool limit, feasibility tolerance) — rejected because
   `scipy.optimize.milp` passes only `disp`, `presolve`, `time_limit`, `mip_rel_gap` and `node_limit`
   (ADR-0003); the rest needs SciPy's private bindings or `highspy` as a new dependency of a Home Assistant
   integration, and no setting is known to be sound (a tighter tolerance turned the wrong optimum into a false
   infeasible).
3. Keep the better validated plan of the two runs — rejected because when the guard fires the first run has no
   plan (infeasible, or a fractional binary), so there is nothing to compare; running both on every
   forced-spill model doubles the solve inside the same time limit to catch a wrong optimum of the default
   run that was not seen in about 860 problems.
4. Mark a plan of the second run as unproven (`mip_gap` empty or `time_limited`) — rejected for now because it
   changes the meaning of two published attributes for an event of the order above, and the gap of the
   default run is no more of a proof; to be reopened if a wrong optimum of the default run is ever recorded.

## Amendment (T-066): the gate opens the spill the pack could take; the rest is forced whatever the room

The gate of the forced spill reads the room of the pack (`spill_gate`). It was also the only door: a pack with room
whose charge power is below the surplus (139 of the 217 draws of the T-041 table without a plan) had no plan,
because the plan may not call a spill what the pack could still charge. T-064 let a `CURTAIL` row charge; since the
inverter throttles the part the pack cannot take by its charge power whatever the room, `forced_spill_limit` now
offers exactly that part without the gate (`curt ≤ forced + extra`, `extra ≤ (bound − forced) × spill_open`, see
below), and the spill the pack could take still needs it. The gate rows, the relay and the owner decision (T-062 B: a pack that can take the need
never spills) are unchanged; the first interval relays a forced `CURTAIL` like any other (ADR-0005 amendment T-066).

The model has the same binaries and, where a forced part is positive, one continuous variable (`extra`) and one row
more per gated slot. The solve time of the default mode is not re-measured by this amendment: it stays an accepted
state as above; the sweeps of the amendment run in the same time on main, the first form of the row and the split
(`spill_sweep.py bell` and `probe`, seed 77 × 300 alone on the machine: 32, 31, 29 s and 23, 25, 24 s).
`test_pinned_limit_highs_without_presolve_calls_a_worse_plan_optimal` pins HiGHS on the matrix of before this
amendment (it patches `forced_spill_limit` to 0), because the forced part changes the matrix it pinned.

**The first form of the row was solved worse (review of T-066).** The spill row was one row with an offset,
`curt − (bound − forced) × spill_open ≤ forced`. On `spill_sweep.py probe` seed 77 index 282 (14 slots, export cap
0.5 kW, pack 9.9 of 10 kWh, charge power 4 kW, a flexible load of 2 kWh at 2 kW) the default run reported `Optimal`,
status 0, gap 0, no fractional binary, at −0.339803; main's plan at −0.396053 satisfies every row and bound of that
matrix (2.8e-15), the run without presolve returns −0.396053 on it, and so does the default run with the forced part
set to 0. The plan was 16 % dearer and the certificate accepted it, as it accepts any plan that obeys the rows: the
second attempt of `_Model.solve` fires only on an infeasible answer or a fractional binary. Per LESSONS
(solver-defect-pinned-on-one-switch) both switches were solved against the certified point, and the row was split
into the ungated part and the gated one, `curt − extra ≤ forced` and `extra − (bound − forced) × spill_open ≤ 0`
with `extra ∈ [0, bound − forced]` (the same set of plans), after which the draw is −0.396053 in both runs
(`tests/test_forced_spill.py` `test_probe_with_a_spill_beyond_the_charge_power_is_planned_at_the_certified_cost`
and `test_both_solver_switches_find_the_certified_cost_of_the_probe_day`; both fail on the first form).

*What this does and does not show.* `benchmarks/switch_audit.py` solves every matrix of a draw with the default run,
`presolve=False`, `mip_rel_gap=0` and both, and counts those where the default run is dearer than another by more
than 1e-6 plus 1e-4 of the cost. On the same 5700 draws (`probe` seeds 5, 11, 22, 33, 44, 77, 91 and `run`,
`negrun`, `bell` seeds 5, 11, 22, 77, 300 each): main `dd40906` 2 of 7086 matrices (`probe` seed 5 index 7,
−0.206387 against −0.263997, and seed 22 index 294, −1.174629 against −1.226629: the default run is wrong on
main too), the first form of the row 1 of 7584, the split row 0 of 7584. The split removed the known draw; at main's
rate of 2.8e-4 per matrix, 7584 clean matrices happen 12 % of the time, so it is not a clearance and the sentence
above, "none where the default run was the worse one", holds for the T-041 corpus only. The alternative the review
named, solving a forced-spill model with both switches and keeping the cheaper certified plan, doubles the solve
inside the same time limit and is not shipped; T-085 decides it with the draws above.

## Amendment (T-074): the second attempt covers every model with mode rows

HiGHS's presolve reported as infeasible a model that has a plan, and no forced-spill binary is in it. The state
is `benchmarks/spill_sweep.py negroll 12`, problem 205, followed as the runtime follows it (`solve_released`,
the commitment stored with the safety exception of the plan before): a site that cannot curtail (`allow_curtailment`
off) with no PV above its export cap (2 kW) or its inverter rating (3 kW), so that `curtail_need` is 0 in every
interval and the model has no gate, relay or `spill_open` row; a 10 kWh pack at 9.995 kWh (standby loss 0.02 kW;
the 0.005 kWh it is below full is under the minimum activity); `CHARGE_PV` carried since 10:59 under
`observed_soc_maximum`, a 60 minute minimum that is over at 12:00; negative sell prices; the `preserve_initial`
terminal rule. The plan
of all HOLD satisfies every row (objective 0.3883) and is the only way through, since the pack can neither take
the 0.005 kWh nor give it up. At eight and nine intervals of that state the first run (presolve on) returns
infeasible and the run without presolve returns that plan; at seven or fewer the first run is sound. It is not
the class of T-064 or T-066, whose problems are infeasible as modelled (a pack that cannot take the surplus by
charge power, a need no mode can serve); this model is feasible. Which rows presolve misjudges is not reduced:
the evidence that the model has the plan is the plan, not a rule named.

Decision:

1. `optimize._solve` sets `second_attempt` on every model with mode rows (a battery and
   `minimum_mode_minutes` above zero, the condition of `constrain_modes`), also where there is no forced-spill
   binary. `_Model.solve` is unchanged: one run with presolve and, when it comes back infeasible or with a
   fractional binary, a second without presolve in what is left of the time limit; the certificate decides
   what is published. A model without mode rows and without forced-spill binaries is solved once.
2. This replaces, for the models with mode rows, Decision 2 of Amendment T-072 ("a model with forced-spill
   binaries") and three statements of the amendments before it: that models without forced-spill rows never take
   the run without presolve; that no wrong result of their default run is known (T-074 is one: a false
   `infeasible`); and the scope of the rate in Decision 4 ("1 in 25 000 on a site that cannot curtail"), which
   is measured only for the forced-spill rows and is not carried over.
3. A site with `minimum_mode_minutes` at zero has no mode rows and keeps one run. A truly infeasible model with
   mode rows costs two runs from now on.

Measured on `origin/main` 06f935e (T-075, T-064, T-072, T-067) and on this branch with it merged in, the
Docker test image (SciPy 1.18.1, HiGHS 1.12.0), 300 problems per draw; the commands, per draw, are
`benchmarks/spill_sweep.py <draw> <seed> 300 <file>` and the same for `curtail_sweep.py`, compared with
`rollcompare` (rolled draws) and `compare` (`negrun`); the HiGHS runs were counted by wrapping
`_Model._run` around the same commands (scripts in the T-074 artifacts):

- 0 newly infeasible and no different trace in `spill_sweep` `negroll` 11 and 12, `bellroll` 12, `roll` 12 and
  `negrun` 12 (283 plans the same, none better or worse) and in `curtail_sweep` `roll`, `negroll`, `deproll`,
  `fullroll` and `resroll` seed 11. `negroll` 12 follows one more problem to the end: 273 of 300 against 272,
  problem 205 (a trace of 8 boundaries; main stops at the fifth).
- Of the 23 504 first HiGHS runs of the ten draws, 477 take the second run on this branch and 327 on main; 5
  find a plan (4 on main). Of the 150 second runs T-074 adds, 149 are infeasible again and one is problem 205.
  That is 1 solve in 23 504 where the second attempt on a model with mode rows only found a plan, on draws
  aimed at sites that cannot curtail; it is not a rate for the field.
- A plan of the second run replaces an `infeasible` or a fractional point only, so no plan that main had
  changes (different trace 0 above). What is not measured is how far such a plan is from the optimum on a model
  with mode rows only: the run without presolve may call a far worse plan optimal (Amendment T-072, measured on
  forced-spill models), and the certificate remains the only guarantee.
- At the runtime's horizon (`artifacts T-074: review_probe96.py`, `probe_summary.txt`): `solve_released` with
  10 s per rung and a 60 s budget over 420 problems stretched to 96 slots (seven draws of 60: random, negative
  prices, depleted, reserve, full, and curtailment off at 15, 30 and 60 minutes), head against main. Outcome
  and first eight modes are the same in 420 of 420 (380 plans, 39 infeasible, 1 timeout on both: its first run
  hit the limit, so no second attempt). The second run happens 82 times on this branch and 73 on main, every
  one infeasible again; median 0.005 s, 95th percentile 0.049 s, longest 0.068 s, 1.1 s in all; total solve
  time 265.2 s against 265.1 s. An earlier run on the branch before the merge (main da2c2e7) gave 111 and 74
  second runs, longest 0.06 s, the same 420 of 420.
- The hazard that is not observed: the second run is given what is left of the time limit, and one that times
  out reports status 1, which `solve_released` reads as `timeout` and takes no rung for (only `infeasible` takes
  one), so a problem that is infeasible would stop at `timeout` instead of reaching the later rungs. The longest
  second run is 0.068 s against a 10 s limit at 96 slots; models above 96 slots (192, 384) were not measured.

Pinned by `tests/test_mode_presolve.py`, built from the state above with no `benchmarks` import: the plan of all
HOLD at nine intervals, at seven to nine, through `solve_released` with no rung after the first, and a truly
infeasible model with mode rows that is reported as infeasible after two runs. The first test accepts one or two
runs, so it stays green on a HiGHS that is sound on this model; the SciPy upgrade that makes it so is a reason to
re-measure this amendment, not to remove the second attempt.
