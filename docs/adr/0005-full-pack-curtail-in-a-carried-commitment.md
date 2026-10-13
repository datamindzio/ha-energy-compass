# ADR-0005: A full pack ends a carried PV-follow commitment in HOLD or CURTAIL, a released HOLD has no clock, and a CURTAIL run goes on through a PV gap

Status: accepted
Date: 2026-10-12 (amended 2026-10-13, T-037, T-047, T-050)
Amends: [ADR-0004](0004-carried-lock-release-for-standby-loss.md) (the released `HOLD` clock)

## Context

T-010 made `CHARGE_PV` follow the PV surplus and let a carried commitment end on a full pack, with `HOLD`
as the only released mode (`constrain_modes`, `lock_full`; `validate_modes`). That `HOLD` started its own
minimum duration (`minimum_mode_minutes`). A full pack with more PV than the site can pass is not `HOLD`:
the inverter curtails. So the plan was `infeasible` when, inside the commitment or the released `HOLD`'s
60 min, an interval had PV surplus above the export cap (`grid_export_kw × h`) or PV above the inverter
rating (`inverter_kw × h`). Before T-010 the model trickle-charged (not executable) and returned a plan.
T-030 repro: 50 of ~1620 random problems with a carried lock newly infeasible (17 export cap, 33 PV above
the inverter).

## Decision

1. The full-pack release also opens `CURTAIL`, in an interval that itself starts at the ceiling test of
   `lock_full` (the previous interval's, as for `HOLD`). A `CURTAIL` under way goes on: it has its own clock,
   and with standby loss the pack drops below the ceiling while it runs. A pack that was full and has room
   again does not curtail from the commitment: `CURTAIL` would then be a label to leave the minimum duration
   with (the HOLD-on-a-chargeable-surplus of T-019/T-025 under another name; 3 of 2100 problems in the review
   sweep used it, none needed it). Never from the empty-battery release.
2. A `HOLD` released from a carried PV-follow commitment starts **no clock of its own**: the commitment's
   deadline stays the only one. It may turn to `CURTAIL` (a normal mode with its own clock) inside the
   commitment if the pack was at the ceiling in an earlier committed interval (cumulative, so not after an
   empty-battery release alone), and is free after the deadline. It does not go back to `CHARGE_PV`/`SELF_CONSUME` (an explicit
   row; before, the `HOLD` clock forbade it). This applies to the empty-battery release as well
   (amended by T-050: not for a `HOLD` released from a depleted pack, point 13).
3. `validate_modes` mirrors it: a released `HOLD` keeps the commitment's clock, `CURTAIL` is accepted from
   the commitment in an interval that starts at the ceiling, or from the released `HOLD` once the pack was
   seen at the ceiling, nothing else is.
4. Idle committed intervals (T-025, [ADR-0004](0004-carried-lock-release-for-standby-loss.md)): a real `HOLD`
   run there (`hold_run` = 1) is a release like any other and starts no clock either, so a spent pack whose
   idle first interval is a bridge may start its `HOLD` in the next one; a bridge never follows a released
   `HOLD` (the commitment has ended). `CURTAIL` is offered in an idle interval on the terms of point 1 and 2
   as well, and a `CURTAIL` row is never a bridge (T-038: the idle set grew by the rows a legacy direction
   guard or a flexible-load probe leaves with nothing to follow, and an idle row that offered `HOLD` only
   left a full pack on a site without export no plan).

## Alternatives rejected

1. **Release to `CURTAIL` only, `HOLD` keeps its clock** — the `HOLD` must last the full duration, so a
   curtailing slot inside it is still blocked; and a released `CURTAIL` has to last `minimum_mode_minutes`
   with PV above the activity in every interval, which fails when PV drops.
2. **Release to `HOLD` and shorten its clock** — an arbitrary second duration to explain.

## Consequences

- Seeded sweep against the code before this change, 3 seeds × 700 random problems (export cap 8/2/0 kW,
  PV up to 2.2 kWh per interval, carried `CHARGE_PV`/`SELF_CONSUME`/`HOLD`/`CURTAIL`): 0 newly infeasible,
  9 newly feasible, 23 changed objectives, none worse.
- The review's sweep (3 seeds × 700, same families plus an idle drain, against the code before T-010, T-010
  and this change): 0 newly infeasible, 36 newly feasible, 84 better objectives, none worse beyond 1e-6. Of
  the 27 problems T-010 made infeasible, this change fixes 17; 10 remain (4 carried, 5 with a
  post-deadline clock, 1 free plan).
- After rebasing onto T-025 (idle bridges), a sweep of 3 seeds × 500 random problems (same families, idle
  rows and a full pack included) against origin/main: 0 newly infeasible, 0 `solver_failure`, 8 newly feasible.
- `benchmarks/optimizer.py` (96 slots, all strategies) unchanged within noise: the rows exist only inside a
  carried commitment.
- The empty-battery `HOLD` also loses its own clock; its plans can only get cheaper.
- T-037 (below): the two cases this ADR left `infeasible` are planned at the first boundary and when the
  plan is followed and re-solved; the sweeps show 0 newly infeasible. Other cases stay infeasible (list below).

## Amendment (T-037): CURTAIL dwell and the carried fill-and-curtail interval

Status: the amendment was written by the developer on a ticket that asked for a decision; the architect
confirms it (or changes it) at the gate.

Two cases stayed `infeasible` with one cause each. A `CURTAIL` run needs `curt ≥ minimum activity` in
every interval of its minimum duration, which fails when PV drops (a **free** plan on an export-capped site,
no carried lock needed). And a carried `CHARGE_PV` interval that fills the pack while the rest of the
surplus is still above the export cap or the inverter rating had no legal mode: `CHARGE_PV` fills and
cannot curtail (the classifier gives `CURTAIL` only with charge and discharge at zero), `HOLD` and
`CURTAIL` are not open until the pack is at the ceiling.

Repro of the second: `pv = (0.5, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9)`, load 0.1, `grid_export_kw = 2`,
`initial_kwh = 9.5`, carried `CHARGE_PV` since 15 min before the first slot. Slot 1 has 0.1 kWh of room and
0.8 kWh of surplus. It was `ok` before T-010.

Decision:

5. **Dwell semantics.** A `HOLD` in an interval whose PV is below the minimum activity, inside the minimum
   duration of a `CURTAIL` run, is that run going on (a *CURTAIL bridge*; `curtail_bridge`, in
   `Plan.carried_bridges`). The inverter has nothing to curtail and the pack and the grid do what `HOLD`
   does. A bridge counts for the `CURTAIL` clock, starts no `HOLD` clock and lasts only until the run's own
   duration is up; a `HOLD` after it starts its own clock. A bridge needs a `CURTAIL` (or bridge) in the
   interval before, and a run begun by PV that the cap or the inverter cannot pass, or carried in; a
   `CURTAIL` chosen for its price can always be left out, so it needs none (and the cost_min golden model
   of the standard fixtures is unchanged). `validate_modes` reads the same `HOLD` as the run going on.
6. **Fill and curtail.** Not a mode: `CHARGE_PV` and `CURTAIL` stay exclusive. The ceiling that sets
   `lock_full` (the pack cannot take the surplus any more) drops by the larger of the minimum activity and the *curtail need* of the next
   interval, the PV that must go to the pack or be curtailed (`pv − load − export cap`, `pv − inverter
   rating`; times the charge efficiency). The pack that fills in the committed interval is then "full" for
   the next one, whose `HOLD` or `CURTAIL` is open. Carried `CHARGE_PV` for the repro now gives
   `CHARGE_PV`, then `CURTAIL` with 0.3 kWh curtailed per interval.
7. **The first committed interval is released like the others.** The release of 6 is tested at the planned
   end of an interval, so a carried commitment whose pack was *already* at the first interval's lowered
   ceiling (the observed state of charge, in a re-plan after the pack filled in the interval just carried
   out) had none: the plan one boundary after a fill-and-curtail plan was infeasible, although the plan
   itself was not. The first interval now enters `lock_full` and `seen_full` as a constant when
   `initial_kwh ≥ ceiling − tolerance` (the ceiling is taken at the probe's upper bound, its highest, so the
   constant holds for any probe), and `validate_modes` tests interval 0 against the observed state of charge the
   same way. A first interval that is idle and has no probe keeps its bridge reading: its `HOLD` is the
   commitment going on, a released one would end the clock. The safety exception (an observed state of
   charge at the maximum turns a charging commitment into `HOLD`) is unchanged.
8. **A CURTAIL bridge is not a `HOLD` of the commitment.** The rule that keeps a `HOLD` after the commitment
   to a release (`HOLD ≤ held + release`) exempts the bridge rows, which are the `CURTAIL` run going on, so a
   forced `CURTAIL` released from the commitment can run through a PV gap inside the commitment's own
   duration, for either member mode. Idle rows (load below the minimum activity) get the bridge too: an idle
   `HOLD` is otherwise the commitment's bridge or a `HOLD` run of its own, and neither continues the
   `CURTAIL` clock. A row that has both readings is listed once in `Plan.carried_bridges`.
9. Rejected: letting `HOLD` or `CHARGE_PV` cover the curtailing interval without a bridge. It lets any
   `HOLD` leave the dwell of a `CURTAIL`, which is the label-to-leave-the-duration this ADR refuses in 1.

Measured with `benchmarks/curtail_sweep.py` (seeds 11, 22, 33 × 300 random export-capped problems with PV
gaps and carried modes; the merged branch against origin/main with T-038, the same seeds):

- `run`, the plan at the first boundary: 662 of 900 planned (main: 437); 0 newly infeasible, 225 newly
  planned, 4 cheaper, 0 worse.
- `roll`, each plan followed one interval the way the coordinator does (commitment, clock and pack carried,
  planned again, up to 8 boundaries): 0 newly infeasible while followed, 220 fixed; followed to the end 640
  of 900 (main: 420), infeasible at the first boundary 238 (main: 463). The two cases of this ADR are
  planned, and so is the plan after them, but the rolled plans do not all go to the end.

Still `infeasible`, found by the rolling sweep and out of scope here:

- (T-047, below: planned.) A `HOLD` inside its minimum duration cannot pass PV above the export cap: the
  carried `HOLD`, the safety-exception `HOLD` at the maximum state of charge, and a `HOLD` after a finished
  `CURTAIL` run.
- A free plan whose horizon ends before the minimum duration of the run it needs; `preserve_initial` and a
  standby loss that make the terminal state of charge unreachable; `allow_curtailment = false` with PV above
  the cap; a case where charging and curtailing in one interval would be needed to keep the state of charge.

## Amendment (T-047): CURTAIL takes over a HOLD that cannot pass the PV

Status: written by the developer on a ticket that asked for a decision in this ADR's terms.

A `HOLD` takes no curtailment, so a `HOLD` that must last had no legal mode once PV above the house
exceeded `grid_export_kw` (or PV the inverter rating) inside its minimum duration, and the plan was
`infeasible` until the duration was over. Three entrances: a carried `HOLD`; the `HOLD` that the safety
exception puts in place of a carried `CHARGE_PV` at the maximum state of charge (and, after T-037's release of
the first interval, the `HOLD` that a re-plan carries from a pack filled in the interval just carried out); a
`HOLD` that starts after a `CURTAIL` run that ended in a PV gap (the run's bridge ends with the run, the `HOLD`
starts in the gap, PV returns inside its hour). Repros: `tests/test_curtail_dwell.py`
(`test_curtail_takes_over_a_hold_that_cannot_pass_the_pv`).

Decision:

10. **A `CURTAIL` where the site cannot pass the PV takes a `HOLD` over.** In an interval with
    `_curtail_need > 0` (PV above the house and the export cap, or above the inverter rating) the inverter
    curtails whatever the mode, so a `HOLD` run inside its minimum duration goes on as a `CURTAIL` run (a
    *takeover*), for a carried `HOLD`, a `HOLD` released by the safety exception and a `HOLD` that began
    earlier in the plan alike. The `CURTAIL` run has its own minimum duration (point 5 applies: its dry
    intervals are bridges), which always outlasts the `HOLD`'s remainder because it starts inside it, so the
    `HOLD`'s duration is satisfied by it. A `HOLD` after the run starts its own clock. Nothing else changes
    clocks: no new duration.
11. **Only where the site cannot pass the PV.** Anywhere else a `CURTAIL` inside a `HOLD`'s duration would be
    a label to leave it with (point 1, point 9), so it is not offered and `validate_modes` rejects it.
    Not a release of a PV-follow commitment (those follow points 1 to 4): a `HOLD` released from one has no
    clock of its own to take over, and `validate_modes` accepts a takeover only from a `HOLD` that has one.
12. **The need is the one as realised.** A flexible-load probe's load is part of what the site passes, and the
    plan is certified against it (`validate_modes` reads the slots with the probe's load in them). The model
    offers the takeover in an interval only for the probe loads that leave `_curtail_need` positive there
    (`flex_load ≤ need − margin` for the export-cap term, `_takeover_room`; the inverter term does not depend
    on the load), so model and validator agree for every probe value. Without it a sell price below zero made
    the solver prefer a takeover in an interval where the probe had made a `HOLD` legal, and
    `solve_flexible_load` raised `solver_failure` ("minimum mode duration") for problems planned before.

Rejected: offering `CURTAIL` inside a `HOLD` in every interval with curtailable PV (a label to leave the
duration); shortening the `HOLD` to its first interval (a second, arbitrary duration).

Measured with `benchmarks/curtail_sweep.py` (seeds 11, 22, 33 × 300 random export-capped problems, then seeds
44, 55, 66, 77 × 300; this branch merged with origin/main `bf5d517`, the merge of T-045, against that `main`,
the same seeds; the sweep script is the same on both sides):

- `run`: seeds 11–33, planned 802 of 900 (main: 662); 0 newly infeasible, 140 newly planned, 57 cheaper, 0
  worse. Seeds 44–77: 0 newly infeasible, 189 newly planned, 79 cheaper, 0 worse.
- `roll` (each plan followed one interval and planned again, as the coordinator does, up to 8 boundaries):
  0 newly infeasible while followed in all seven seeds; followed to the end 800 of 900 (main: 640), infeasible at the first boundary 98 (main: 238).
- Sell prices from −0.6 to 0.8 (`negrun`, `negroll`; seeds 101, 202, 303, 404 × 300): planned 1076 of 1200
  (main: 886), 0 newly infeasible, 0 worse; followed to the end 1066 (main: 847), 0 newly infeasible while
  followed. These are the draws the base seeds do not make.
- Flexible-load probes on those problems (`probe`, through `solve_flexible_load`, so the plan is certified
  against the probe's load; same seeds): planned 1073 of 1200 (main: 915), 0 newly infeasible, 0 newly
  rejected by the solver-result check (`NEW SOLVER FAILURE`), 158 newly planned, 42 cheaper, 1 dearer by
  1e-6 with the same modes (solver tolerance). The first version of this amendment offered the takeover
  without regard to the probe and the review found `solver_failure` on problems planned on `main`; these
  rows are what shows it, and the base seeds never plan a probe.
- A plan can be cheaper because the `HOLD` no longer has to be avoided (a plan that grid-charged to keep a
  `HOLD` clear of a forced interval now holds and curtails the little that the cap forces); one existing test's
  expected bridges changed for it (`test_curtail_bridge_follows_the_run_not_any_forced_interval_before_it`).

Still `infeasible`, found by the same sweep and out of scope here (98 of 900): `allow_curtailment = false` with
PV above the cap, `preserve_initial` whose terminal state of charge is out of reach, a carried
`CHARGE_PV`/`SELF_CONSUME` with a pack that cannot take the PV above the cap by its charge power (charging and
curtailing in one interval would be needed), and the `HOLD` released from a carried PV-follow commitment whose
pack is depleted (no clock of its own, so nothing for a `CURTAIL` to take over): PV above the cap inside the
commitment's duration is infeasible for it, which is every sunrise on a zero-export site (planned since T-050,
below).

## Amendment (T-050): a HOLD released from a depleted pack goes back to the commitment

Status: written by the developer on a ticket that asked for a decision in this ADR's terms; the architect
confirms it (or changes it) at the gate.

A carried `CHARGE_PV`/`SELF_CONSUME` whose pack is too empty to go on (above the reserve, so no safety
exception) is released into `HOLD`, which has no clock of its own (point 2) and, until now, could not go
back (point 2: "it does not go back to `CHARGE_PV`/`SELF_CONSUME`"). Once PV above the house exceeded
`grid_export_kw` (or the inverter rating) inside the commitment, no mode was legal: `HOLD` passes no surplus
beyond the cap, `CURTAIL` is offered only after a full pack (point 1), and the T-047 takeover needs a `HOLD`
with a clock of its own (point 11). On a site with `grid_export_kw = 0` that is every sunrise with an empty
pack. Repros (15 min slots, 10 kWh, buy 1, sell 0.9, load 0.1, `minimum_mode_minutes = 60`, carried since
15 min before the first slot): cap 2 kW, `initial_kwh = 0.01`, `pv = (0, 0.9, 0.9, 0.9, 0.9, 0.9)`; cap 0,
`pv = (0.05, 0.3, 0.5, 0.7, 0.9, 0.9)`. Both are in `tests/test_curtail_dwell.py`.

Decision:

13. **A `HOLD` released from a depleted pack goes back to the commitment**, in any later committed interval
    in which the pack is still depleted (at or below the lock floor at the start of the interval; the
    `lock_depleted` permission of the interval before): the pack takes the surplus, which is what the inverter
    does there, and a `HOLD` could not pass PV above the cap. Not a `CURTAIL` takeover of the released `HOLD`
    (the other option of the ticket): that curtails PV beside an empty pack for an hour, the worse plan.
14. **The return is the commitment going on, not a new PV-follow run.** The commitment's deadline stays the
    only clock (point 2): the return starts no minimum duration and needs no forecast coverage beyond the
    horizon's end. In the model it is the `back` binary of the interval (`_resume`): at most the member mode,
    the `HOLD` released before and the `lock_depleted` permission, and the start flag and the coverage row of
    the PV-follow group are relaxed by it. A first version that left them in made a return near the end of the
    horizon infeasible (a new run of 60 min does not fit) and held the pack in the commitment for another
    hour. `validate_modes` accepts the return (`resumes`) where the state of charge at the start of the
    interval is at or below the lock floor, and no other mode change from a released `HOLD`.
15. **A `HOLD` released from a full pack still does not go back** (point 2 stands for it): a full pack that
    has room again is not a reason to leave the minimum duration, and a member mode after its `HOLD` would be
    the second change point 2 refuses.

Measured with `benchmarks/curtail_sweep.py` (the script is the same on both sides; it gained a generator for
the sunrise on a depleted pack: `deprun`, `deproll`, `deprobe`, a carried `CHARGE_PV`/`SELF_CONSUME` with the
pack at the reserve to 0.2 kWh above it, PV rising from nothing, export cap 0, 0.5 or 2 kW). Seeds 11, 22, 33,
44 × 300 each, this branch (merged with origin/main `5c11c2e`) against that `main`; the same counts come out against
`98ddc16`, the branch's first base:

- Base draws (`run`, `negrun` with sell prices below zero, `probe` through `solve_flexible_load`): 0 newly
  infeasible, 0 newly planned, 0 changed objectives, 0 `NEW SOLVER FAILURE`. The rule changes nothing where
  the pack is not depleted.
- `deprun` (the plan at the first boundary): planned 707 of 1200 (main: 573); 0 newly infeasible, 127 newly
  planned, 16 cheaper, 0 worse.
- `deprobe` (flexible-load probe on top, so the plan is certified against the probe's load): planned 813 of
  1200 (main: 686); 0 newly infeasible, 127 newly planned, 76 cheaper, 0 `NEW SOLVER FAILURE`, 1 dearer by
  1e-6 with the same modes (solver tolerance).
- `deproll` (each plan followed one interval, as the coordinator does, up to 8 boundaries): 691 followed to
  the end (main: 574), 118 fixed, infeasible at the first boundary 493 (main: 620); 1 of 1200 newly
  infeasible while followed (below). `roll` on the base draws: 0 newly infeasible, 0 fixed.

The one problem that is newly infeasible while followed (`deproll`, seed 11, problem 249) is not a rule
failure: `allow_curtailment = false`, PV above the inverter rating, a first interval that is idle. On `main`
the plan takes that interval as a bridge, the commitment goes on and the plan is followed to the end. With the
return allowed, a real `HOLD` run in that interval followed by the return is cheaper (the surplus is
exported in the interval between), the plan is not a bridge, and after the first interval the coordinator
records a `HOLD` run with a clock of its own (`mode_run_start`): the next plan has a `HOLD` that must last
through PV the site cannot pass without curtailment, which is in the list below. The coordinator forgets that
the `HOLD` was a release, so any plan that relies on a released `HOLD` going on without a clock is replanned
with one at the next boundary (on `main` too, for the full-pack release and the `CURTAIL` that follows it);
the follow-up task T-055 covers it.

Still `infeasible` and out of scope: the cases listed in the T-037 and T-047 amendments, in particular
`allow_curtailment = false` with PV above the cap, a pack that cannot take the PV above the cap by its charge
power (charging and curtailing in one interval would be needed), and a terminal state of charge out of reach.
