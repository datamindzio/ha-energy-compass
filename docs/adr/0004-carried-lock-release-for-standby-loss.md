# ADR-0004: A carried PV-follow commitment ends when the battery must keep later standby loss

Status: accepted
Date: 2026-10-12

## Context

Since T-001 `SELF_CONSUME` is load-following: in a committed (carried) PV-follow interval the battery must
cover the house, reach its power limit or end on its reserve (`_follow_load` in
`custom_components/energy_compass/engine/dispatch_policy.py`). The standby loss (`idle_drain_kw`) is a fixed
per-interval schedule sized on the "do nothing" trajectory (`engine/idle_drain.py`), so the energy rows make
every plan keep the loss of later intervals unless something recharges the pack.

A carried commitment forbids "do nothing". When the forced discharge would take energy the later standby loss
needs, and neither PV nor grid charging can pay it back in time, the program is `infeasible` for the rest of
the commitment (up to `minimum_mode_minutes`). T-001 documented this for a 0 % reserve only. It happens with
any reserve once the remaining standby loss exceeds what the forced discharge leaves (T-019 repro: 10 %
reserve, 0.03 kW, 20 h without PV). With the `deye_hybrid` profile (0.13 kW) and a long horizon the schedule
is capped by the initial SoC, so on a night with no usable PV the next day *any* forced discharge is
infeasible. The existing release (`lock_depleted`) could not help: it looked at the planned end of a committed
interval, never at the first one, and its threshold knew only the reserve.

## Decision

Keep the release mechanism and make its threshold, the lock floor, aware of the standby loss still owed:

1. `owed[i]` — the energy interval `i` must end with so that the relaxed reserve floor of every later interval
   can still be met with the **best-case recharge**: every later interval charges all the PV surplus the pack
   can take and, outside the commitment, from the grid where grid charging is open. It is a backward
   recursion over constants (`_owed`), and a bound the energy rows already imply.
2. Lock floor of interval `i` (`_lock_floor`): unchanged (`reserve + standby loss + activity / eta`) while
   `owed[i]` is at or below the reserve. Above it the pack cannot end on its reserve, so `SELF_CONSUME` needs
   `owed[i] + standby loss + max(activity, min(load - PV, power limit)) / eta`.
3. The release also applies to the **first** committed interval, tested on the observed SoC (a constant, no
   new binary), and only when that interval has no PV surplus the pack could charge from.
4. `validate_modes` uses the same two helpers, so it accepts the released rows and no other.

The released rows are `HOLD` with full import, the mode the unlocked solver picks in the same state, and
`HOLD` starts its own minimum duration, as with the existing release. (Amended by
[ADR-0005](0005-full-pack-curtail-in-a-carried-commitment.md): the released `HOLD` starts no clock of its own.)

## Alternatives rejected

1. **Re-solve without the commitment when the first solve is infeasible** — exact, but the validator could
   only trust a flag instead of certifying the rows, it cannot tell the standby case from the
   `preserve_initial` limit T-001 kept on purpose, and it doubles the solve time on the path that already
   runs against the time budget.
2. **Excuse the discharge once the pack is down to the owed energy** (a `SELF_CONSUME` row that covers only
   part of the house) — this is the trickle row T-001 removed: the inverter discharges to the reserve whatever
   the plan says, so a controller cannot hold that state.
3. **A release binary the solver is free to set, priced with a large penalty** — distorts the objective
   scale, cannot be mirrored in the validator, and lets a price spike buy its way out of a minimum duration.
4. **A pessimistic `owed` (no recharge assumed)** — releases commitments the battery can keep; with a capped
   schedule that is nearly every evening on the `deye_hybrid` profile.
5. **Docs only** (state the real condition) — leaves no plan for up to `minimum_mode_minutes` on exactly the
   nights when the battery is low.

## Consequences

- With no way to recharge, a carried PV-follow commitment always has a plan (covered by a seeded test over
  reserves, drains, loads and horizons). Against the previous code 2000 random committed problems gave 478
  newly feasible, none newly infeasible, and no first-interval release where the commitment could be kept.
- `owed` is a best case, so the release never ends a commitment the battery could keep. The price is a rare
  remaining `infeasible`: scattered PV surplus that the minimum mode durations do not let the plan bank.
- Not changed here: `preserve_initial` that cannot be met after the forced discharge stayed `infeasible` (T-001); the T-065 follow-up below releases it.
- Found here, fixed in T-025 (below): a committed interval in which neither `SELF_CONSUME` nor `CHARGE_PV`
  reaches the minimum activity (net load and surplus both below it) was `infeasible` unless the interval before
  left the pack empty; it predates T-001. Requiring `CHARGE_PV` instead of `HOLD` in a released interval with PV
  surplus was tried in T-019 and dropped for this reason.
- With standby loss off (`idle_drain_kw = 0`) `owed` never exceeds the reserve and the lock floor is the old
  one; the only new row is the first-interval release for a pack within one minimum activity of its reserve.

## Follow-up (T-025): idle committed intervals and HOLD on a chargeable surplus

1. **Idle interval: bridge or HOLD run.** A committed interval in which no PV-follow member can reach the minimum
   activity (`_follow_open`: the bounds `constrain_modes` puts on both modes, taken from the problem) is `HOLD` by
   classification, and that alone is not leaving the commitment. The model gives each such interval a binary
   `hold_run`. 0 is a *bridge*: the interval continues the commitment, needs no release, carries PV-follow in the
   duration rows and starts no `HOLD` clock. 1 is a real `HOLD` run (it starts no minimum duration of its own, see
   [ADR-0005](0005-full-pack-curtail-in-a-carried-commitment.md)); it needs what any `HOLD` needs (a release flag,
   a `HOLD` already under way, or in the first interval the observed pack already spent). A first version had only the bridge, so an idle first interval lost the spent-pack release and the
   problem was `infeasible`; the run reading is what keeps the sweep at zero newly infeasible problems.
   The solver reports which rows it bridged (`Plan.carried_bridges`) and hands them to
   `validate_modes(problem, flows, bridges)`. A bridge row must be `HOLD` in an interval `idle_slots` lists; every
   other `HOLD` row, an idle one included, takes the normal release check. Without `bridges` the validator tries every
   idle row as a bridge (the T-025 reading), then the idle `HOLD` rows before each row in turn (the first row that is
   not a bridge ends the commitment, so the bridges are a prefix) and accepts the plan if one reading passes (T-038;
   the T-025 reading alone rejected a valid plan whose `HOLD` run started on an idle row).
   The controller writes the `HOLD` registers for a bridge row, which is what the inverter does anyway with
   nothing to follow. The coordinator keeps the persisted commitment (mode and start) on a bridge row, so the next
   calculation still carries `SELF_CONSUME`/`CHARGE_PV`, and the clock of a later real `HOLD` run starts at its own
   first row, not at the bridges before it (`mode_run_start`); `dispatch_policy.carried_idle_starts` lists the bridge
   rows.
2. **HOLD never *starts* on a chargeable surplus inside the commitment.** The release (`lock_depleted`, or the
   observed SoC in the first interval) allows `HOLD` to start only in an interval where `_takes_surplus` is false;
   in the first interval that was already so. A `HOLD` run that began earlier goes on through a surplus, since a
   released `HOLD` stays `HOLD` until the commitment's deadline (ADR-0005). The validator applies the same test at every committed interval. The full-pack
   release of T-010 (`lock_full`) is not bound by it: a pack too full to charge has nothing to take.
3. Only the carried commitment is covered. A PV-follow mode the solver starts itself still needs its next
   intervals to offer a member (no `HOLD` bridge); changing that is a separate decision.

## Follow-up (T-038): the legacy direction guard and flexible-load probes

T-025 left two neighbours of the rule without a plan: a direction the legacy guard blocks was read as open, and a
flexible-load probe was held to the tightening without the bridge reading.

1. **A blocked direction is not open.** `_takes_surplus`, `_follow_open`, the mode availability in
   `constrain_modes` (`CHARGE_PV` and `SELF_CONSUME` get no capacity in the direction the guard blocks) and
   `validate_modes` all take `_guarded`, so a guarded pack that may not charge has no `CHARGE_PV` to follow, one that
   may not discharge has no `SELF_CONSUME`, and an interval with only that direction to follow is idle (a bridge or a
   `HOLD` run, as above). Seeded repros E and F of the T-025 review are tests.
2. **Probes get the bridge reading, with the model and the validator on one idle set.** The alternative was to drop
   the tightening for probes, which would again let a plan start `HOLD` on a chargeable surplus the moment a probe
   is asked about. Instead the validator receives the realised problem (the slot load plus the probe) and the model
   reads it the same way: a row idle without the probe is a bridge only while the probe it takes stays below
   `probe_room` (the load at which `SELF_CONSUME` would open, less `_PROBE_MARGIN = 1e-5` kWh, which keeps HiGHS
   away from tiny coefficients); otherwise it is an ordinary committed row. The cap and its margin apply only where
   the probe can open the row. A row that is still idle with the largest probe the model may add (`_follow_open` on
   the slot load plus the probe's upper bound, the validator's own reading) is a bridge for every probe: with the
   margin taken there too, a probe pinned within 1e-5 kWh of `probe_room` was neither a bridge nor `SELF_CONSUME`,
   and a problem planned on main had no plan (review round 2, flex seed 3001 #856: 1.0 kWh at 0.5 kW pinned at
   0.125 kWh per quarter on a slot with 0.1 kWh of PV; pinned with PV 0.100005 kWh as well). A surplus the probe takes below the
   minimum activity is not chargeable as realised (`open_surplus`, with `release`), so `HOLD` may start on it.
   The realised probe is never below zero: the solver returns noise such as -3e-17 kWh, which lifts a slot sitting
   exactly at the minimum activity (`0.03 - 0.005` is `0.024999999999999998` kWh against `0.025`) from idle without
   the probe to open as realised, and the plan, which bridged it, failed `validate_modes` (`solver_failure`, seed 202
   #105, pinned). `_solve` clamps the probe at zero for the certificate and for `schedule_kwh`. A first version read a
   flow within `_OPEN_TOLERANCE = 1e-9` kWh of the activity as reaching it, which fixed that but also changed the
   plain path: four problems of the T-038 review (plain-nl seeds 1001 #313, #1478, 1002 #761, plain 1002 #312) were
   planned on main by bridging a slot at exactly the activity and were `infeasible`. The tolerance is gone; a slot
   exactly at the activity reads by float arithmetic on every path, as on main, and #761 is pinned.
3. **What stays infeasible until the commitment ends.** The commitment forbids a plan that started `HOLD` on a
   surplus the pack could take, and some problems had no other plan: a `preserve_initial` terminal the commitment
   leaves no way to meet (pinned problem #44, which has no plan without a probe and none with 0.1 kWh at 0.5 kW; a
   probe that puts load into every committed interval, 0.3 kWh at 2 kW, or one that takes a chargeable surplus
   below the activity, #917, does give a plan, so the probe is not a constant of the problem), a
   daily throughput budget smaller than the minimum activity the commitment asks for, and the standby loss case
   above. `docs/model.md` lists them; the optimizer reports `infeasible` and the last plan is retained.
4. `validate_modes(problem, flows, bridges=None)` tries the readings above, and a test covers `_publish_current`
   deriving `carried_idle` and `mode_start` from `carried_idle_starts`.
5. **An idle committed interval may be `CURTAIL`, on the terms of
   [ADR-0005](0005-full-pack-curtail-in-a-carried-commitment.md).** Points 1 and 2 make more rows idle than main
   has: a surplus the guard keeps out of the pack, and every row idle without the probe (main read the idle set
   with the largest probe, so with a probe almost none). An idle row offered `HOLD` only, in the model, while
   ADR-0005, which landed on main while this task was open, offers `CURTAIL` in every other committed row and
   `validate_modes` accepts it in any. A full pack on a site without export then had nowhere to put the PV, and a
   `CURTAIL` under way could not go on through the row (review round 4: four problems planned on main c44e3fb had
   no plan). Both idle branches of `constrain_modes` now offer `CURTAIL` on the same two rows as the ordinary
   branch (`_offer_curtail`): the pack has been at the ceiling, and it is there now, a `CURTAIL` is under way or
   the previous interval's `HOLD` was released. A `CURTAIL` row has `hold_run` = 1 and is never a bridge. Rejected:
   offering it only in the rows main did not read as idle (guarded and probe rows), which would keep the plain
   model without a guard exactly main's, but leaves the same hole there: a `CURTAIL` that must go on through an
   interval with nothing to follow has no plan on main either (plain-nl seed 11001 #151, pinned), and the
   validator already accepts the plan. Rejected as well: taking these rows out of the idle set, which is what
   main does and what points 1 and 2 exist to correct. The plain model keeps main's rows wherever `CURTAIL` is
   not available in the idle interval (no earlier interval, or less PV than the minimum activity to curtail).

Evidence (corrected in T-038): `tests/test_carried_commitment_sweep.py` keeps a seeded corpus of committed problems
(2 to 16 quarter hours, loads and PV around the minimum activity, both initial modes, 30/60/120 minute durations,
sites that cannot export, packs a hair above the reserve or below the ceiling, standby loss, a legacy direction guard,
flexible-load probes). It fails on main and passes here: every problem is planned or `infeasible`, never rejected by
the validator, and no plan starts `HOLD` on a chargeable surplus. T-025 stated that seed 11 gave no newly infeasible
problem against main; it did not: the review found problems with the legacy guard and with probes that were planned
on 34d6e1f and `infeasible` after T-025 (repros E and F, probes #44, #62, #917, #1424). The T-038 sweep
(`tests/test_carried_commitment_guard_probes.py` pins them) generates the corpora of the T-025 review (short first
interval, 0.05/0.1/0.2 kW minimum activity, packs that charge or discharge below it, a daily throughput budget; 1500
problems each, seeds 1001, 1002, 2001 and 2002; plain and flexible-load probes, with and without the guard, 16 corpora)
and compares head with main (6db538f) and with 34d6e1f. Against main: no problem that was planned is `infeasible`,
no `solver_failure`, and 57 to 186 problems per guard or probe corpus planned that main could not (none in the
plain corpora without a guard, where the engine is main's). That held for those four seeds only: review round 2
found one more on seed 3001 (flex #856, the same problem as flex-nl #853; point 2 above). With the fix, seeds 3001,
3002, 4001 and 4002 (16 corpora, 24000 problems) against main: no planned problem `infeasible`, no `solver_failure`,
no `HOLD` started on a chargeable surplus, 77 to 196 problems per guard or probe corpus planned that main could not,
and the plain corpora without a guard identical to main. Against 34d6e1f: 2 newly `infeasible` problems in 24000
(flex-nl seed 1001 #1420, flex seed 2002 #1033), both `preserve_initial` terminals whose old plan had started `HOLD`
on a chargeable surplus, the class under point 3. No plan starts `HOLD` on a chargeable surplus with a pack
that is not full, except slots exactly at the activity read by float arithmetic, in plans main makes too (flex-nl and
plain-nl seed 2002 #520, #715: `0.03 - 0.005` against `0.025`). The T-038 review found a
head-versus-main difference this sweep had not been run against (the tolerance above): measure against main as well as
against the base of the rule being tightened.

After the rebase onto main c44e3fb (ADR-0005 and four other tasks had landed) the statement "no planned problem
`infeasible` against main" was about a main the sweep had not been run against, and it was false: review round 4
found four (point 5; `t038_review_sweep.py` flex seed 8002 #592, `t038_review5_full.py` flex seed 9002 #581 and
flex-nl seed 9001 #604, and the hand-made guard problem G1, which has no probe). `t038_review5_full.py` is a
second generator aimed at the full-pack release: packs at the ceiling or the floor, no export in half the
problems, many idle slots. With point 5, against main f0b3618 (its engine is c44e3fb's): `t038_review_sweep.py`
seeds 10001 and 10002 and `t038_review5_full.py` seeds 11001 and 11002, plain and probes, with and without the
guard, 1500 problems each (16 corpora, 24000 problems): no planned problem `infeasible`, no `solver_failure`, 77
to 201 problems per guard or probe corpus planned that main could not (41 to 108 in the plain corpora with a
guard), and no `HOLD` started on a chargeable surplus except the float-arithmetic slots above, in plans main makes
too (plain seed 10001 #990, the same problem as plain-nl #986). The plain corpora without a guard are no longer identical
to main: 14 problems in 6000 are planned that main could not (1, 2, 3 and 8 per corpus), each with a `CURTAIL`
run (the idle rows are the only ones that changed there); the plans both trees make have the same modes in every interval, and in 16 to 32
of the 1500 per corpus the solver reads an idle `HOLD` row the other way (bridge or `HOLD` run, same flows, same
cost; main picks between the two the same way, by the order of the rows, which the `CURTAIL` rows change). The probe-tie sweep
of round 3 (`t038_review4_tie.py`, seeds 12001 and 12002, 3000 probes) against the same main: no planned problem
`infeasible`, no `solver_failure`. G1, #151, #581 and #604 are pinned in
`tests/test_carried_commitment_guard_probes.py`.

## Follow-up (T-065): the terminal rule is owed too

A carried `CHARGE_PV` with the pack just above the reserve, PV below the load in the first interval and
`preserve_initial` had no plan: the pack was not spent, so `HOLD` was not open, `SELF_CONSUME` draws the net load
(0.105 kWh) and the PV surplus later (0.095 kWh) cannot put it back. At the boundary before, with a longer horizon, the
same slot was planned `SELF_CONSUME`, so the plan disappeared at the next boundary (sweep `deproll` seed 11 draw 293
on `main` b2d6ffb, found in T-058; the state is the first test of `tests/test_carried_terminal_release.py`; the
generator and engine have moved since, and that draw is followed to the end on later `main`s, the pinned test is
the repro).

`_owed` now starts its backward recursion from the terminal requirement (`initial - total standby loss`, the bound
`optimize.py` puts on the last energy variable) instead of 0 when `terminal_mode` is `preserve_initial`. The lock
floor, the first-interval test, the `depleted` release and `validate_modes` read that value. A pack that cannot
follow a committed interval and still reach the terminal even with the best-case recharge is released to `HOLD` with
full import, the same row as for the standby loss, and ends where it began.

### The return is not the release (review of T-065)

The lock floor is the bound of two decisions. The release (HOLD may start, `lock_depleted`, `pack_spent`) takes the
terminal-driven floor. The return of a released HOLD to the commitment (`back`, `validate_modes` `resumes`) does not,
unless the HOLD run began on a depleted pack, which the coordinator carries as released (`released_holds`, T-050).
After T-057 (a HOLD released from a *full* pack does not go back where the pack is not depleted: the coordinator
carries it as a plain HOLD with a clock of its own) the terminal-driven floor sits above a nearly full pack
(`fullroll` seed 33 draw 241: floor 9.987 kWh, the pack 9.9775 kWh after the HOLD, 0.02 kWh under its ceiling), so
the first version of this follow-up counted that pack as depleted and planned the full-pack HOLD back to
`CHARGE_PV`: a plan at one boundary, infeasible at the next. The model and the validator now take the floor without
the terminal requirement (`_owed(..., terminal=False)`) for a run that did not begin on a depleted pack: a further row
on `back` (`_resume`, `physical`), relaxed by `emptied_hold`, which is tracked wherever the two floors differ; and
`resumes` reads `left <= floor(terminal=False)`, or the run began depleted and `left <= floor` or the PV is one the
site cannot pass. Where the two floors are equal nothing is added. Pinned in `tests/test_carried_terminal_release.py`
(draw 241 rolled; the validator refuses the return of the full-pack HOLD, a test that fails without the row).

A problem that has a plan keeps its objective (the states that open the release already violate the terminal bound on
every path that follows).

### `TIE_761`: bridge or released HOLD run

The last interval of `TIE_761` is a bridge or, since T-065, a HOLD run released from a pack that cannot refill for the
terminal rule (`released_holds` (2,)): the same flows and cost, the solver's tie, which the test accepts either way.
Both readings roll on. The coordinator stores the first interval of a plan only, and in both readings that is a bridge
(the commitment unchanged), so the next boundary gets the same problem (`test_both_readings_of_a_bridge_or_released_hold_tie_roll_on_from_the_same_state`
follows both to the end of the horizon). The last interval reaches the first place only in a one-interval problem,
which reads its own tie.

### Evidence

Checked against `main` 0778580 (T-057, T-052, T-473 and T-474 included; the branch merges it) on
`benchmarks/curtail_sweep.py` (the branch's copy run on both trees), seeds 11, 22 and 33, 300 problems each, for
`run`, `negrun`, `deprun`, `fullrun`, `probe`, `deprobe`, `roll`, `negroll`, `deproll` and `fullroll`, read with
`compare` and `rollcompare` (first-failure, changed-boundary and trace rows too, not only newly infeasible):

- plans: 0 newly infeasible, 0 new solver failures, 0 worse, 2 better (`probe`, 11 and 22), 16 newly planned
  (`run` 4, `negrun` 2, `deprun` 1, `fullrun` 0, `probe` 8, `deprobe` 1);
- rolls followed one interval at a time: 0 newly infeasible, 9 fixed (`roll` 5, `negroll` 3, `deproll` 1, `fullroll` 0),
  0 first failure earlier or later, 0 plans contradicted at the next boundary, 0 different traces. `fullroll` seed 33
  draw 241 behaves as on `main` (no plan at the first boundary).

Pinned problem #44 and the night with no refill (`test_carried_lock_without_a_way_to_refill_releases_to_hold`) are
planned now; #917 stays `infeasible` without a probe.
