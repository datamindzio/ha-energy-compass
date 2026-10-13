# ADR-0005: A full pack ends a carried PV-follow commitment in HOLD or CURTAIL, a released HOLD has no clock, and a CURTAIL run goes on through a PV gap

Status: accepted
Date: 2026-10-12 (amended 2026-10-13, T-037, T-047, T-050, T-057, T-058, T-064)
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
   (amended by T-050: not for a `HOLD` released from a depleted pack, point 13; by T-057: nor, for that release, in an
   interval whose PV the site cannot pass, point 19. A `HOLD` released from a full pack never goes back, point 15).
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
  the cap; a case where charging and curtailing in one interval would be needed to keep the state of charge (planned since T-064, below).

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
    `curtail_need > 0` (`_curtail_need` until T-041 made it public; PV above the house and the export cap, or above the inverter rating) the inverter
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
    offers the takeover in an interval only for the probe loads that leave `curtail_need` positive there
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
curtailing in one interval would be needed; planned since T-064, below), and the `HOLD` released from a carried PV-follow commitment whose
pack is depleted (no clock of its own, so nothing for a `CURTAIL` to take over): PV above the cap inside the
commitment's duration is infeasible for it, which is every sunrise on a zero-export site (planned since T-050,
below).

## Amendment (T-050): a HOLD released from a depleted pack goes back to the commitment

Status: decided in this amendment, implemented and swept on T-050 (the ticket asked for the decision in this
ADR's terms). The alternative it rejects is in point 13; a different rule is a new amendment.

A carried `CHARGE_PV`/`SELF_CONSUME` whose pack is too empty to go on (above the reserve, so no safety
exception) is released into `HOLD`, which has no clock of its own (point 2) and, until now, could not go
back (point 2: "it does not go back to `CHARGE_PV`/`SELF_CONSUME`"). Once PV above the house exceeded
`grid_export_kw` (or the inverter rating) inside the commitment, no mode was legal: `HOLD` passes no surplus
beyond the cap, `CURTAIL` is offered only after a full pack (point 1), and the T-047 takeover needs a `HOLD`
with a clock of its own (point 11). On a site with `grid_export_kw = 0` that is every sunrise with an empty
pack. Repros (15 min slots, 10 kWh, buy 1, sell 0.9, load 0.1, `minimum_mode_minutes = 60`, carried since
15 min before the first slot): cap 2 kW, `initial_kwh = 0.01`, `pv = (0, 0.9, 0.9, 0.9, 0.9, 0.9, ...)`; cap 0,
`pv = (0.05, 0.3, 0.5, 0.7, 0.9, 0.9, ...)`. Both are in `tests/test_curtail_dwell.py`.

What the published plan is not: the coordinator carries out the first interval of a plan and plans again at
the next boundary, from the `(mode, since)` it stored. A first version of this amendment changed the single
plan (`HOLD, CHARGE_PV, CHARGE_PV`) and nothing else. Followed, that plan was not carried out: after the `HOLD`
the coordinator stored a `HOLD` run with a clock of its own, which is what it does for every `HOLD` that
leaves a PV-follow commitment and is not a bridge. The next plan then had a `HOLD` that had to last
`minimum_mode_minutes` through the PV above the cap, and was `infeasible` (both repros, with
`allow_curtailment` true and false). A rolled sweep (`deproll`, below) found it; the single-plan sweeps did
not. The rule below is therefore carried across the boundary as well, in the stored commitment.

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
15. **A `HOLD` released from a full pack still does not go back** (point 2 stands for it, in every interval,
    also one whose PV the site cannot pass, point 19): a full pack that
    has room again is not a reason to leave the minimum duration, and a member mode after its `HOLD` would be
    the second change point 2 refuses. A full-pack release keeps its own clock and the `CURTAIL` takeover of
    points 1 and 11 across the boundary exactly as before.
16. **The release is carried across the coordinator boundary.** `Plan.released_holds` lists the rows of a `HOLD`
    released from a depleted pack, for as long as that `HOLD` runs (`validate_modes` returns them); `runtime`
    publishes their start times as `dispatch_policy.released_hold_starts`. When the first interval of the plan
    is such a row, `_commit_battery_direction` stores the commitment unchanged, `{mode, since}`, with
    `released: true`: no `HOLD` run, no clock of its own, as for a bridge. `restore_commitment` keeps the flag
    only for a PV-follow commitment, `build_problem` hands it on as `Problem.initial_dispatch_released`, and
    `validate_problem` refuses it without a PV-follow commitment. In the next problem the previous interval
    is a released `HOLD`:
    - the `HOLD` may go on as a `HOLD` run with no clock, also **past the commitment's deadline** (the flag is
      not tested against the deadline or the state of charge: a version whose flag was valid only inside the
      commitment, while the pack was still depleted, left two problems of the rolled sweep infeasible while
      followed, in seeds 22 and 44);
    - the first interval may go back unconditionally (the plan that released it saw the state; the next one
      may go back or stay in `HOLD`), the later ones by point 13 and 14;
    - any other mode change from it is the one point 2 refuses (`CURTAIL` raises `minimum mode duration` in
      the validator).
    The flag lives only while the released `HOLD` rows continue: the first plan that does not start in such a
    row clears it.
17. **A depleted pack may leave the commitment and re-enter it more than once.** Nothing counts the
    releases: every one needs the pack at the lock floor (or a released `HOLD` already running), every return
    needs a member mode and the `back` permission, and neither starts a clock. Within one commitment the pack
    can be released, go back, charge, discharge to empty through `SELF_CONSUME` and be released again;
    `test_a_depleted_pack_leaves_and_re_enters_the_commitment_more_than_once` pins `HOLD, CHARGE_PV,
    SELF_CONSUME, SELF_CONSUME, HOLD, CHARGE_PV, SELF_CONSUME, SELF_CONSUME` (rows 0 and 4 released) for a
    120-minute commitment. This is intended: the commitment's deadline is the one limit on the commitment's
    length (point 2), and an inverter with an empty pack idles when PV is below the house and charges when it
    is above; the optimizer is not asked to avoid either.
18. **A return past the deadline is a run of its own.** A released `HOLD` that outlasts the commitment and
    then goes to `CHARGE_PV`/`SELF_CONSUME` is not the commitment going on (it has ended): in the model it is
    a new PV-follow run, and the coordinator stores it with a fresh `since` (the plan's `generated_at`) so
    that the next plan sees the run's own minimum duration, not the long-expired one.

Measured with `benchmarks/curtail_sweep.py` (the script is the same on both sides; it gained a generator for
the sunrise on a depleted pack: `deprun`, `deproll`, `deprobe`, a carried `CHARGE_PV`/`SELF_CONSUME` with the
pack at the reserve to 0.2 kWh above it, PV rising from nothing, export cap 0, 0.5 or 2 kW, and the rolled kinds
`roll`, `negroll` and `deproll` that feed every plan through `_commit_battery_direction`, the coordinator's own
code, one interval at a time up to 8 boundaries). Seeds 11, 22, 33, 44 × 300 each (1200 per kind); head is
this branch's code at `670cb8c`, main is origin/main `fbcffe0`:

| Kind | What it measures | Head | Main | Newly infeasible |
| --- | --- | --- | --- | --- |
| `run`, `negrun` (sell below zero), `probe` (`solve_flexible_load`) | plans on base draws | 1076, 1057, 1066 planned | identical | 0, nothing changed, 0 `NEW SOLVER FAILURE` |
| `roll`, `negroll` | the same, plans followed | 1073, 1049 followed to the end | identical | 0 while followed, 0 fixed |
| `deprun` | plan at the first boundary | 707 planned | 580 | 0 (127 newly planned, 16 cheaper, 0 worse) |
| `deprobe` | the same through a flexible-load probe | 813 planned | 686 | 0 (127 newly planned, 76 cheaper, 1 dearer by 1e-6, solver tolerance, same modes) |
| `deproll` | the plan followed one interval at a time | 697 followed to the end | 574 | 0 while followed, 123 fixed; infeasible at the first boundary 493 vs 620 |

The rule changes nothing where the pack is not depleted (the base kinds are identical to main). The 493
`deproll` draws that are still infeasible at the first boundary are out of scope (below).

Five of the draws that main cannot plan at all are planned on head and then fail at a later boundary (seed/draw
22/45, 22/278, 33/229, 44/91, 44/182, at boundary 1, 1, 3, 1, 1). None is newly infeasible and none has the
release flag in the failing state, so the failing problem is the same state on main and is infeasible there
too (checked on `fbcffe0` with the pickled problem). Two of them (22/45, 44/182) are infeasible with no commitment
at all, the `preserve_initial` and `allow_curtailment = false` class below. The other three are feasible
without the commitment and infeasible with it: the suffix of a feasible plan, carried out one interval, has no
plan. That is not this change and is T-057 (closed by the amendment below). The first version of this amendment had 1 of 1200 newly infeasible while followed in `deproll`
(`allow_curtailment = false`, PV above the inverter rating, an idle first interval: the plan was a real `HOLD`
run rather than a bridge, and the coordinator then stored it with a clock of its own); it is closed by point
16, as are the 2 of the intermediate version.

T-055, which proposed carrying the release across the boundary on its own, is folded into this amendment and
cancelled as merged.

Still `infeasible` and out of scope: the cases listed in the T-037 and T-047 amendments, in particular
`allow_curtailment = false` with PV above the cap, a pack that cannot take the PV above the cap by its charge
power (charging and curtailing in one interval would be needed; planned since T-064), and a terminal state of charge out of reach.

## Amendment (T-057): a released HOLD goes back where the site cannot pass the PV

Status: decided in this amendment, implemented and swept on T-057. It amends points 2, 13 and 15 and closes
the three draws the T-050 table left to T-057 (22/278, 44/91, and the third of the class).

A plan followed one interval is the next problem's suffix, and that suffix must have a plan. Draw 22/278 of
`deproll` (a carried `SELF_CONSUME` on a pack just above the reserve, PV rising from nothing, export cap
0.5 kW, standby loss) has a plan at the first boundary and none at the next, with the same commitment and
the same intervals. The cause is the idle drain: its schedule is capped by the do-nothing trajectory from the
*initial* state of charge, so the observed pack (0.111 kWh, not the planned 0.123) pays a different drain
than the plan assumed and can no longer cover a deficit of 0.1 kWh before it empties. The commitment is
released into `HOLD`, which meets a surplus of 0.2 kWh against a cap of 0.125 kWh: no `HOLD` flow is legal
there, `CURTAIL` is offered only after a full pack (point 1), and the way back (point 13) needs a depleted
pack, while this one (0.086 kWh) is above its lock floor (0.039 kWh). No mode is left.

19. **A `HOLD` released from a depleted pack goes back to `CHARGE_PV`/`SELF_CONSUME` in any committed interval
    whose PV the site cannot pass**, that is, where the curtail need of the interval (`max(pv - load -
    grid_export_kw × h, pv - inverter_kw × h, 0)`, less the margin) is positive: a `HOLD` there has no legal
    flow, and the member mode is the only mode that takes the surplus. The `lock_depleted` bound of point 13
    is lifted in such an interval and stays everywhere else, so the return is no wider than that. It is the
    commitment going on (point 14): no start flag, no coverage rule, the commitment's deadline is the only
    clock.
20. **A `HOLD` released from a full pack does not**, whatever the interval. The first version of this
    amendment did not tell the two releases apart, and the review found why that is wrong: the coordinator
    carries a depleted release across the boundary (point 16) but stores a full-pack release as a plain
    `HOLD` with a clock of its own, whose next problem has only `HOLD` and the `CURTAIL` takeover
    (`follows_hold`). A plan that returned such a `HOLD` to the member mode in an unpassable interval was
    infeasible at the boundary after the first interval followed, for either value of `allow_curtailment`
    (a new failure the sweeps had not covered, since none rolled a full-pack release). Open there is `CURTAIL`,
    which a pack that was full is offered (point 1).
    In the model the release is tracked, not guessed: `emptied_hold` is 1 on a `HOLD` run that began on a
    depleted pack (a continuous variable bounded by the `HOLD` of the interval, by the depleted origin or the
    run before it, and by the return not being counted as a `HOLD`), and `_resume` lifts the `lock_depleted`
    bound only by it. A run begins on a depleted pack where the interval releases it as point 13 describes (the
    first interval: the carried `released` flag or the observed pack, as a constant). The tracking is built
    only where an unpassable interval comes later in the horizon.
21. **`validate_modes` mirrors it** (`resumes`): a return is accepted when the pack at the start of the
    interval is at or below the lock floor, or the `HOLD` run began on a depleted pack (the `emptied` flag the
    validator follows: the carried released `HOLD`, or a release with the pack spent) and the interval has a
    positive curtail need. A `HOLD` released from a full pack that goes back is rejected, in an interval whose
    PV the site cannot pass too.
22. **The model's bound is the narrower one** with a flexible-load probe: it lifts `lock_depleted` where the
    curtail need is positive at the probe's *upper* load, less the probe margin (`unpassable`), so the return
    holds for every probe load, while `validate_modes` reads the need of the interval without a probe. Every
    return the model plans is therefore one the validator accepts, and the converse need not hold, which is
    the safe direction; without a flexible load the two are the same test (`need > 0`).

Measured with `benchmarks/curtail_sweep.py`, seeds 11, 22, 33, 44 × 300 each (1200 per kind); head is this
branch, main is origin/main `4ba2232` (origin/main is `b2d6ffb` now, with the same engine files):

| Kind | Result against main |
| --- | --- |
| `run`, `negrun`, `probe`, `fullrun` | identical, 0 changed |
| `roll`, `negroll`, `fullroll` | identical: 0 newly infeasible while followed, no first failing boundary moved, no plan whose next boundary changes the mode |
| `deprun`, `deprobe` | 0 newly infeasible, 0 worse, 9 and 3 newly planned |
| `deproll` | 0 newly infeasible while followed, 10 fixed (seed 11: 2, 22: 4, 33: 2, 44: 2), 1 whose first failing boundary moves later |

`fullrun` and `fullroll` are new generators of this amendment: a pack at the ceiling, a carried PV-follow
commitment released into `HOLD` and PV above the export cap in a later interval, single-plan and rolled. They
are the generators that separate the revisions: on the same seeds the first version of the amendment (f65995f)
differs from main in 3 of the 1200 `fullroll` draws (the first failing boundary moves later in 2, the next
boundary changes the mode of the plan in 2), this one in none. `rollcompare` now also counts the
rows whose first failing boundary is earlier or later and the plans whose next boundary changes the mode (the
last is common on main, so only the difference against main counts).

The `deproll` draws still infeasible at a later boundary are not this change and none is newly infeasible:
22/45, 22/167, 44/182 and 33/229 are the observed-SoC-at-the-reserve safety exception (T-058), 44/62 and
44/286 a `preserve_initial` terminal state out of reach after a `SELF_CONSUME` draw (a documented class
below), and 22/42 a carried `HOLD` with `allow_curtailment = false` (out of scope above).

## Amendment (T-058, decided in T-060): a PV-follow commitment with the pack at the reserve is the depleted release

Status: decided in this amendment, implemented and swept on T-058 (reviewed three times: the sweeps below are
against the merged main `38d560f`, T-057 and T-065 included). It narrows the T-050 amendment's "above the
reserve, so no safety exception".

A carried `SELF_CONSUME` whose observed pack is exactly at the reserve (`initial_kwh <= capacity ×
minimum_soc_fraction + 1e-9`) inside its minimum duration raised the safety exception `observed_soc_minimum`,
and `_initial()` read the previous mode as `HOLD` with the commitment's clock. That `HOLD` had to last to the
deadline, so once PV above the house exceeded `grid_export_kw` it was the T-047 `CURTAIL` takeover (PV curtailed
beside an empty pack, the outcome point 13 rejects) or no plan at all (`allow_curtailment` false). The exact
reserve is the usual empty state (the inverter stops at the minimum SOC and load-following `SELF_CONSUME` in a
plan drains the pack to exactly the reserve), so a followed T-050 plan landed here.

23. **A `SELF_CONSUME` commitment at the observed reserve is not a safety exception.** `safety_exception`
    returns nothing for a carried `SELF_CONSUME` (its only SOC reason was the reserve), so the commitment is
    the released `HOLD` of point 13: no clock of its own, back to `CHARGE_PV`/`SELF_CONSUME` where the surplus
    is chargeable, carried across the boundary as point 16. A pack that drops to the reserve during a carried
    release is not locked again. `CHARGE_GRID`, `DISCHARGE_GRID` and `CHARGE_PV` keep their exceptions (the
    reserve is no bound for the charging modes; `DISCHARGE_GRID` is not PV-follow).
24. **The house is never left without a plan (owner, T-060).** Where the released `HOLD` has no plan, a plan is
    still made, by a ladder in which each rung is tried only when the one before raised `infeasible` (any other
    solver error ends it). A rung's solve gets `min(solve_time_limit_s, total_time_limit_s less the time the
    ladder has used so far)`, so the rungs share one budget and `runtime.compute` still checks the whole
    recalculation against it afterwards:
    1. the problem as read (the release of point 23);
    2. `dispatch_policy.reserve_fallback(problem)`: the previous reading, `Problem.initial_dispatch_reserve_hold`
       set, which makes the exception back. The plan is `HOLD` with the commitment's clock and `CURTAIL` from
       the T-047 takeover where `allow_curtailment` is true; `safety_exception` reports `observed_soc_minimum`
       and the coordinator stores nothing for it, as for every exception. The class: the pack's charge power
       cannot take the PV above the export cap, and charging and curtailing in one interval is not modelled
       (the T-047 out-of-scope list; 0.01 kWh above the reserve it has no plan either);
    3. `dispatch_policy.uncommitted_fallback(problem)`: the plan without the carried commitment, for a carried
       `CHARGE_PV` or `SELF_CONSUME` only. The plan is solved without the commitment and its clock; the
       coordinator never learns that and still holds the commitment, so a first interval that is still
       `CHARGE_PV` or `SELF_CONSUME` keeps it and its clock (`_commit_battery_direction`), and any other
       first mode starts a new one, stored like any mode change. The class: a plan followed one
       interval leaves a state in which the commitment's minimum duration makes the next plan infeasible
       (a tail the previous plan allowed and the next boundary does not: closed by T-057; a carried `CHARGE_PV`
       inside its duration with the pack just above the reserve under the `preserve_initial` terminal rule:
       T-065 released the part where the terminal rule cannot be met at all, and what is left, a requirement
       the pack could meet but the commitment cannot bank, left to this rung: T-070 measured it, on 1600 aimed first-boundary
       problems of `main` 06f935e, as 123 in the class (no plan with the commitment, a plan without it and one with the
       terminal value), 222 planned by this rung and 62 with no plan on any rung, and did not tighten the bound; T-081
       decided it stays so, since on 49 152 followed boundaries of `benchmarks/day_replay_rungs.py` none is planned by
       this rung with or without the tightening: ADR-0004 follow-ups T-070 and T-081). It is the last resort: T-057 and T-065 made the rung rarer (below).
25. **One function solves it.** `optimize.solve_released(problem, time_limit_s=, budget_s=, accept_incumbent=,
    solver=)` returns `(plan, problem)`, the problem being the rung that was solved (a flexible-load probe runs on
    it). `runtime.compute`, `tools/replay.py`,
    `tools/replay_year.py` and `benchmarks/curtail_sweep.py` all call it, so what the replay and the sweeps
    measure is what ships. One difference is older than this amendment and stays: after a safety exception
    (rung 2's `HOLD` included) `tools/replay.py` and `tools/replay_year.py` carry a new mode and clock, where
    the coordinator stores nothing for an exception; the benchmark's `commit` calls the coordinator's own
    `_commit_battery_direction` with the commitment of the problem as built. With `allow_curtailment` false and no plan on any rung the problem stays infeasible, as
    before. Charging at the charge power and curtailing the rest in one interval is the proper plan for rung 2's
    class: planned since T-064 (amendment below), which makes rung 2 rarer, not the safety exception only (the
    T-069 amendment measures what is left); forced spill with room is T-066, the unmoded CHARGE_PV rule T-041.

    **25a. A flexible-load level takes the ladder too (T-067).** (Numbered 26 until T-079; the T-069 amendment
    below has its own point 26, so "point 26" written before T-079 with T-067 beside it means this one.) The base plan of the draw above is `CHARGE_PV` five
    times and then `CURTAIL`, the pack just full where the `CURTAIL` interval starts. `lock_full` (the pack at the next
    interval's ceiling) is set in the probe's model with the largest draw the probe may make in that interval
    (`extra`), which keeps the plan valid for any load up to it; the realised load the certificate reads is
    smaller, so a probe of 0.25 kWh at 2 kW has no solution anywhere in the horizon, while 0.05 kWh at 0.5 kW, 0.5 kWh at 2 kW, a pack
    of 4 or 6 kWh or a commitment of 75 minutes each have one. Making the ceiling follow the realised load needs a
    disjunction per interval (the ceiling is concave in the load, so no single linear bound is both sound and as
    loose as the constant), so the model is left as it is, and `consumption.analyze_flexible_loads` takes the ladder instead: a level that is `infeasible`
    on the problem the plan was solved for is solved on `optimize.later_rungs(problem)` in order (`reserve_fallback`,
    then `uncommitted_fallback`; a problem that is already rung 2 has only the last), against the plan of that
    rung (solved once, only when a level needs it, with the probe time limit). A level is priced only against
    a plan of its own problem; the block between two levels on different rungs is `None` and the second is
    `UNKNOWN`, and so is a level on another rung than the anchor (the first level), which is not judged against the
    anchor's allowed price. A level is published with the reason that ended it: `infeasible` only where every rung it
    reached had no solution (a rung whose own base plan is `infeasible` is skipped); a timeout or any other solver
    error, on the probe or on the base plan of a rung, ends it at once as that error. The rung's base plan and the
    probe each get what is left of the total limit, and a base plan that returns after it leaves no probe to start
    (`timeout`; the first version passed the probe a limit of zero, an `InputError` that dropped the plan in the
    coordinator, found in review). `benchmarks/curtail_sweep.py` plans a probe the same way, and a rung only when its
    base plan has a solution. Measured with `deprobe` seeds 11, 22, 33, 44 × 300, this branch's
    `benchmarks/curtail_sweep.py` run on every tree so that the draws and the need of a base plan are the same. Head
    is this branch merged with main `fde1cc5`. Against main `fde1cc5`: 0 newly infeasible, and 0, 0, 1 and 1 newly
    planned (seed 33 draw 230 and seed 44 draw 10, both priced on rung 1). Against `38d560f` (T-052, T-057 and T-065
    merged, solved once, as for point 24's table): 0 newly infeasible (main `fde1cc5` without this change: 1, seed
    44 draw 10), 105, 101, 107 and 121 newly planned. The `curtail_sweep.py` of `38d560f` itself probes without
    a base plan, so a comparison made with it counts 14 newly infeasible draws (5, 5, 4 and 0): every one has
    `allow_curtailment` false and no base plan on `38d560f` or on head (T-041, T-064 and T-066 as below), and the
    runtime never probes a problem without a plan.

Measured with `benchmarks/curtail_sweep.py` (its `run`/`roll` solve through `solve_released` where the tree has
it; `resrun`/`resroll` draw the pack exactly at the reserve with a `SELF_CONSUME` commitment carried 0 to 45
minutes, the state the exception reads; `fullrun`/`fullroll` are T-057's full-pack release), seeds 11, 22, 33,
44 × 300 = 1200 draws per kind. Head is this branch merged with main; main is origin/main `38d560f` (T-052,
T-057 and T-065 merged; its engine is that of `991a587`), run with this branch's benchmark file and solved the
old way (once, no rungs). The branch has since been merged with main `547bf7f`, which changes no file the sweep
runs (`git diff 38d560f 547bf7f -- custom_components benchmarks` is empty: T-476, T-478 and T-480 are the replay
tools' price input, a test fixture and CI), so the numbers are those of `547bf7f` as well:

| Kind | Planned, main → head | Newly infeasible | Newly planned |
| --- | --- | --- | --- |
| `run` (plan at the first boundary) | 1081 → 1117 | 0 | 36 |
| `negrun` (negative prices) | 1062 → 1103 | 0 | 41 |
| `deprun` (`make_depleted`: sunrise, pack at the reserve) | 718 → 1121 | 0 | 403 |
| `resrun` (`make_reserve`) | 1111 → 1145 | 0 | 34 |
| `fullrun` (`make_full`: full-pack release, T-057) | 624 → 625 | 0 | 1 |
| `probe` (flexible load on `run`) | 1057 → 1099 | 0 | 42 |
| `deprobe` (flexible load on `deprun`) | 685 → 1115 | 1 | 431 |
| `roll` (followed one interval at a time, up to 8) | 1078 → 1117 | 0 | 39 |
| `negroll` | 1057 → 1102 | 0 | 45 |
| `deproll` | 712 → 1121 | 0 | 409 |
| `resroll` | 1110 → 1145 | 0 | 35 |
| `fullroll` | 597 → 597 | 0 | 0 |

The rolled kinds are the criterion: no draw that main follows to the end is lost while followed, and no first
failing boundary moves earlier (`fullroll`: one moves later). Without rung 3 (the same head with
`uncommitted_fallback` switched off) `resroll` loses 17 draws that main `38d560f` follows to the end (42 against
main `0778580`, before T-065 landed). All 17 fail on a carried `CHARGE_PV` inside its minimum duration, 0.07 to
2.4 kWh above the reserve, under the `preserve_initial` terminal rule; every one has a plan without the
commitment and every one has a plan with the terminal value instead of the requirement: the part of rung 3's
class T-065 left open (rung 3 is its answer; T-070 measured how much of it the rung plans, ADR-0004). The one newly infeasible `deprobe` draw (seed 44, draw 10) is the base plan solving on
rung 1 while the 0.25 kWh probe on that same problem has no solution (it has one on rungs 2 and 3): one consumption
level reports `infeasible`, the plan is untouched; closed by point 25a (T-067). Every draw with no plan on any rung (79 of 1200
`deprun`, 55 `resrun`, 83 `run`) has `allow_curtailment` false: T-041, T-064 and T-066, which main has as well;
the 575 `fullrun` draws without a plan have none on main either.

`rollcompare` also counts T-057's metric, the plan contradicted at the next boundary: a row where the first
interval of a plan is not what the plan before promised as its second, on the boundaries both trees planned,
counted only where one tree changes and the other does not. `roll`, `negroll` and `fullroll`: 0 newly
contradicted (`roll` 1 no longer). `deproll`: 15 draws newly contradicted, 102 no longer. `resroll`: 120 newly,
214 no longer. The new ones are the ladder being decided afresh at every boundary: of the 125 newly contradicted
boundaries of `resroll`, 12 are a rung-3 row (the plan before kept the commitment the next boundary cannot) and
113 a rung-1 row, 41 of them directly after a rung-2 row (seed 11, draw 15: `CURTAIL`, `CURTAIL` on rung 2, then
`CHARGE_PV`, then `CURTAIL`): the rung-2 plan publishes the commitment's `HOLD`/`CURTAIL` to its deadline and the
next boundary can release after all, so the pack takes the PV where the plan before said `CURTAIL`. More plans
hold up than before (the "no longer" counts), but a rung-2 plan is not a promise for its later intervals, and the
mode can change inside the minimum duration there. Whether to carry rung 2 across the boundary was T-069: it is
not carried (the amendment below, points 26 to 28).

On draws that main also plans, a plan's cost is not comparable between the readings (`deprun` 21 higher, 20 lower;
`resrun` 287 higher, 250 lower): the commitment now binds, so the pack takes the surplus where main idled in a
`HOLD` with a clock (`resrun` seed 11: 78 of the 79 higher first rows are `CHARGE_PV` where main has `HOLD` or
`CURTAIL`). That is point 13's intention (a `CHARGE_PV`/`SELF_CONSUME` commitment follows the PV), paid in the
objective of a more constrained problem.

What the followed plans execute (rows of the first interval over the draws followed to the end on both trees, main
→ head). `deproll` (712 draws): `CHARGE_PV` 1034 → 1354, `CURTAIL` 2108 → 1843, `HOLD` 1382 → 1295,
`SELF_CONSUME` 343 → 379, `CHARGE_GRID` 18 → 14. `resroll` (1110 draws): `CHARGE_PV` 399 → 1842, `CURTAIL`
3245 → 2574, `HOLD` 3578 → 2440, `SELF_CONSUME` 206 → 586, `CHARGE_GRID` 130 → 112, `DISCHARGE_GRID` 0 → 4. The
pack at the reserve takes the surplus instead of `CURTAIL` beside it. `roll`, `negroll` and `fullroll` execute the
same rows on both trees but one (`roll`: one `CHARGE_GRID` is a `CHARGE_PV`). Rung use over all 1200 `deproll`
draws (7701 rows): rung 2 228 rows (179 `CURTAIL`, 49 `HOLD`), rung 3 402 rows (256 `HOLD`, 146 `CURTAIL`), the
rest rung 1; over `resroll` (7791 rows) rung 2 1225 rows (1091 `CURTAIL`, 134 `HOLD`: the reserve-aimed draws sit
in the charge-power class) and rung 3 19 rows (16 `HOLD`, 2 `CURTAIL`, 1 `CHARGE_GRID`; 45 before T-065). `roll` and `negroll` use
rung 3 for 39 and 45 rows, `fullroll` for one, and rung 2 never. The rolls carry the commitment the coordinator
holds (that of the problem as built, also after rung 3), so the traces are what ships.

## Amendment (T-069): a rung-2 plan is not carried across the boundary

Status: decided in this amendment. No engine or coordinator change; one test pins it. Measured on main `5baf226`
(T-058 merged). The numbers of the T-058 amendment above are this main's against its engine before T-058: `git diff
87e8410 5baf226 -- custom_components benchmarks` is empty, and so is `547bf7f..40886b4` (the first parent of the
T-058 merge) for those paths. T-064 re-measured it on its head (main `fde1cc5` with the engine of `3679099`, below the
first table): rung 2 is still reached after T-064, and the decision stands on the new numbers.

A rung-2 plan (point 24) publishes the commitment's `HOLD`/`CURTAIL` up to the commitment's deadline. The ladder is
decided afresh at every boundary, so at the next one the release (rung 1) may have a plan again and the first
interval is `CHARGE_PV` or the released `HOLD` where the plan before said `CURTAIL`: `resroll` seed 11, draw 15, a
`SELF_CONSUME` carried since 10:45 with a 60 minute duration executes `CURTAIL`, `CURTAIL` on rung 2 at 11:00 and
11:15, `CHARGE_PV` (0.18 kWh into the pack) at 11:30 and `CURTAIL` from 11:45. The question the T-058 review left:
accept that, or store that rung 2 was taken and keep its `HOLD` to the deadline.

26. **Rung 2 is decided afresh at every boundary and nothing is stored for it.** It is the safety exception
    `observed_soc_minimum`, and every safety exception is read from the observed state at each recalculation: the
    coordinator stores nothing for it (`_commit_battery_direction` returns before it touches the commitment), so
    the next boundary builds the problem of point 23 and plans the release first. Where the release has a plan it is
    the plan. A rung-2 plan is a promise for its first interval only; its later rows up to the deadline hold while
    the release still has no plan. The published plan says so: `dispatch_policy.safety_exception` is set on a rung-2
    plan and on no rung-1 plan of a carried `SELF_CONSUME`.
27. **The `CHARGE_PV` after it is the commitment going on, not a new run** (points 14 and 17). The exception paused
    the commitment and never moved its clock; `CHARGE_PV` and `SELF_CONSUME` are one PV-follow commitment, a
    depleted pack may leave it and re-enter it more than once, and the deadline stays the commitment's. What the
    minimum duration does not give here is a fixed inverter mode inside it: the executed row can go `CURTAIL`,
    `CHARGE_PV`, `CURTAIL`. That is accepted, for the reasons of point 28.
28. **Rejected: carrying rung 2 to the deadline.** Three forms, one executed result:
    1. *The coordinator stores that the commitment is held as `HOLD` to its deadline* and the next problem is built
       with `initial_dispatch_reserve_hold` set (option (b) of T-069). A new key in the stored commitment, read by
       `restore_commitment`, `build_problem` and `validate_problem` the way `released` is (point 16) and by the
       benchmark's `follow`, for one of the three exceptions; the others stay unstored.
    2. *Rung 2 tried first when the plan before was a rung-2 plan* (the ladder reordered inside the engine). The
       engine has no memory, so it needs the same stored fact as 1.
    3. *Deciding after T-064.* T-064 (charge and curtail in one interval) was expected to remove most of rung 2's
       class. It took `resroll` from 1225 rung-2 rows to 308 (table after T-064, below) and not the rest, which is
       a release problem with no plan under the commitment's minimum duration, so the rule for what is left does
       not depend on T-064.

    Why not, for 1 and 2: (i) the owner's rule (T-060) is that the house is never left without a plan, and rung 2
    exists only for the boundary where no other plan does. Kept past that boundary it is `CURTAIL` beside an empty
    pack where a charging plan exists, the outcome points 13 and 23 were written against. (ii) It does not make the
    rung-2 plan a promise either: the carried `HOLD` is re-planned too, and the contradicted boundaries move rather
    than go (below). (iii) The measurement: a small gain in boundaries for PV that is curtailed instead of stored.

Measured with a throwaway roll on `benchmarks/curtail_sweep.py` (`carry_rung2.py`, kept with the task's artifacts,
not in the repository): the same draws rolled twice, `ship` the benchmark's own roll and `carry` planning
`reserve_fallback` first at a boundary whose plan before was a rung-2 plan, while the coordinator holds the same
commitment and `reserve_fallback` still applies (pack at the reserve, before the deadline), the ordinary ladder
where that has no plan. Before T-064: seeds 11, 22, 33, 44 × 300 on main `5baf226`; `ship` reproduces the benchmark's
`resroll` and `deproll` rows and `changed` lists of the T-058 sweep on 1200 of 1200 draws each. These are absolute counts on this
main, not the "newly against the engine before T-058" counts of the amendment above (`roll`, `negroll` and
`fullroll` never use rung 2, so both rolls are the same there):

| | `resroll` ship | `resroll` carry | `deproll` ship | `deproll` carry |
| --- | --- | --- | --- | --- |
| Draws followed to the end (of 1200) | 1145 | 1145 | 1121 | 1121 |
| Rung-2 rows (of 7791 / 7701) | 1225 | 1519 | 228 | 236 |
| Boundaries contradicted, all rungs | 238 | 201 | 81 | 75 |
| – rung 1 after rung 1 (no fallback involved) | 129 | 122 | 71 | 71 |
| – directly after a rung-2 row | 93 | 64 | 9 | 3 |
| Draws with a contradicted boundary | 210 | 179 | 73 | 67 |
| Executed mode changes | 1803 | 1733 | 1514 | 1504 |
| Charged into the pack, kWh | 880.7 | 837.1 | 571.9 | 567.7 |
| Curtailed, kWh | 2155.6 | 2187.6 | 4311.7 | 4315.5 |
| Exported, kWh | 1313.2 | 1324.9 | 846.8 | 847.1 |

In `resroll`, the generator aimed at the reserve (374 of 1200 draws use rung 2), 1174 boundaries follow a rung-2
row: 851 are rung 2 again and 323 are rung 1. Of the 323, 200 are at or after the commitment's deadline, where the
exception is over in either reading (186 as the plan before promised). 123 leave rung 2 before the deadline, the
class of this task: 53 are the `HOLD` the plan before promised, 55 are `CHARGE_PV` and 15 the released `HOLD` where
it promised `CURTAIL`. So 70 of 1174 boundaries after a rung-2 row (6 %) are contradicted inside the minimum
duration; `deproll` has 7 of 214, all `CHARGE_PV`. Over half of all contradicted boundaries involve no fallback at
all (129 of 238, 71 of 81): a plan re-optimised over a horizon one interval shorter, as on main before T-058.

Carrying rung 2 changes the executed trace in 63 `resroll` draws (7 `deproll`). It has fewer contradicted boundaries
in 37 of them, more in 2 and as many in 24: of the 64 left directly after a rung-2 row, 42 are the carried plan
itself going from the promised `CURTAIL` to `HOLD` (9 in `ship`) and 22 are rung-1 plans at or after the deadline. It
executes 70 fewer mode changes of 1803 (4 %). For that the pack takes 43.6 kWh less in those 63 draws (it ends the
roll lower in 53 and higher in 1, 40.3 kWh in all), 32.0 kWh more PV is curtailed and 11.6 kWh more exported;
`CHARGE_PV` rows 1997 → 1915, `CURTAIL` 2574 → 2633. No draw gains or loses a plan. In the example above `carry`
executes `CURTAIL` eight times and the pack ends the roll 0.15 kWh lower.

After T-064, the same roll on this branch: main `fde1cc5` with the engine of `3679099`, seeds 11, 22, 33, 44 × 300,
`carry_rung2.py` unchanged; `ship` reproduces the benchmark's `resroll` and `deproll` rows and `changed` lists of that
head on 1200 of 1200 draws each. Main moved between the two tables (`5baf226` to `fde1cc5`, T-063 among it) and T-064
added the charging `CURTAIL`, so the tables are compared as two dated measurements, not as a one-change difference:

| | `resroll` ship | `resroll` carry | `deproll` ship | `deproll` carry |
| --- | --- | --- | --- | --- |
| Draws followed to the end (of 1200) | 1148 | 1148 | 1130 | 1130 |
| Rung-2 rows (of 7814 / 7772) | 308 | 545 | 32 | 43 |
| Boundaries contradicted, all rungs | 219 | 178 | 70 | 63 |
| – rung 1 after rung 1 (no fallback involved) | 122 | 116 | 58 | 58 |
| – directly after a rung-2 row | 84 | 49 | 11 | 4 |
| – rung 3 after rung 1 | 13 | 13 | 1 | 1 |
| Draws with a contradicted boundary | 199 | 163 | 63 | 56 |
| Executed mode changes | 1877 | 1794 | 1693 | 1681 |
| Charged into the pack, kWh | 1190.3 | 1148.3 | 1068.7 | 1064.4 |
| Curtailed, kWh | 1862.6 | 1894.9 | 3881.2 | 3885.0 |
| Exported, kWh | 1312.2 | 1321.9 | 826.7 | 827.1 |

Rung 2 is still reached after T-064, and the sentence of the first measurement that its remedy is T-064 is withdrawn.
124 of 1200 `resroll` draws (374 before) have a rung-2 row, 308 rows (1225 before), and 11 `deproll` draws, 32 rows
(228). T-064 took the rows of the class it closes, a pack that cannot take the PV beyond the cap by its charge power.
What stays on rung 2 is the commitment's `HOLD` where the release problem has no plan with the commitment's minimum
duration: of the 90 `resrun` draws (of 900) on rung 2, all 90 plan when the minimum duration is dropped (63 are at 120
minutes, 23 at 60 and 4 at 30), none plans with a `value` terminal rule in place of `preserve_initial` (44 of them have
it) and curtailment is allowed in all (`why2.py`).

In `resroll`, 306 boundaries follow a rung-2 row (1174 before): 184 are rung 2 again, 15 are rung 3 and 107 are rung 1,
every one of them before the commitment's deadline (before T-064, 200 of the 323 were at or after it). Of the 107, 80
are contradicted inside the minimum duration: 63 are `CURTAIL` promised and `CHARGE_PV` executed, 13 `CURTAIL` and
`HOLD`, 1 `CURTAIL` and `SELF_CONSUME`, 3 `HOLD` and `SELF_CONSUME`; 27 are the `HOLD` that was promised
(`leave.py`). So 80 of 306 boundaries after a rung-2 row (26 %) are contradicted, against 70 of 1174 (6 %) before
T-064: the count did not go down, the denominator did. `deproll` has 10 of 32 (7 of 214 before), all `CURTAIL`
promised and `CHARGE_PV` executed. Most contradicted boundaries still involve no rung-2 row before them (135 of 219
in `resroll`, 59 of 70 in `deproll`).

Carrying rung 2 changes the executed trace in 59 `resroll` draws (7 `deproll`). It has fewer contradicted boundaries
in 39 of them and as many in 20, none with more: of the 49 left directly after a rung-2 row, 28 are the carried plan
itself going from the promised `CURTAIL` to `HOLD` (3 in `ship`). It executes 83 fewer mode changes of 1877 (4 %). For
that the pack takes 41.0 kWh less in those 59 draws (it ends the roll lower in 52 and as high in 7: 36.9 kWh in all),
31.2 kWh more PV is curtailed and 9.7 kWh more exported; `CHARGE_PV` rows 2048 → 1975, `CURTAIL` 2571 → 2626. The cost
of the executed flows is lower with carry in 50 of the 59 draws and higher in 8 (−313.3 against −310.8 over the 1148
draws, 0.8 %), and it does not value the 36.9 kWh the packs hold less at the end of the roll. No draw gains or
loses a plan. The decision of points 26 to 28 stands on these numbers: reasons (i) and (ii) do not depend on them,
and (iii) is the same trade as before, a fifth of the contradicted boundaries for PV curtailed instead of stored.

Consequences:

- A rung-2 plan's rows after the first are the plan only while the release has none. A consumer that needs to know
  reads `dispatch_policy.safety_exception` (`observed_soc_minimum`, `interrupted_mode` `SELF_CONSUME`).
- The executed mode can change at the boundary after a rung-2 row inside the commitment's minimum duration, and go
  back to `CURTAIL` one interval later. The model's gap (no plan that charges at the charge power and curtails the
  rest) was closed by T-064 and the contradicted boundaries stayed (80 of 306 after a rung-2 row, table after T-064):
  the rung-2 rows that remain are those of a commitment the release cannot keep with its minimum duration. T-078
  found why the release had no plan there and closed it (amendment below): the decision of this amendment stands.
- `tests/test_curtail_dwell.py::test_the_commitments_hold_is_not_carried_to_the_next_boundary` pins it at the
  runtime and the coordinator: after a rung-2 plan the stored commitment is unchanged and the next recalculation
  plans the release, with no exception reported once it has a plan.
- No guide change: the behaviour is that of the T-058 amendment, and the guides already describe the ladder per
  plan ("the first of these that has a solution").

## Amendment (T-064): a CURTAIL interval charges the pack and curtails the rest

Status: implemented and swept on T-064. Measured against main `fde1cc5` (T-069 and T-063 merged on top of T-041), with
this branch's engine at `3679099`; the commits after it change documents and tests only. Main `f7f9ae1` (T-067, T-072)
was merged afterwards; the tables below were not re-swept, and `probe` and `deprobe` seed 11 × 300 were re-run on the
merge against main `f7f9ae1` in review (0 newly infeasible, 0 new solver failure). It closes the class that the T-037,
T-047 and T-058 amendments list as out of scope: a pack whose charge power cannot take the PV above what the site
passes.

The mode rules gave `CHARGE_PV` the pack's whole surplus and no curtailment, and `CURTAIL` no charge, so an
interval with more PV above the export cap (or the inverter rating) than `charge_kw × h` had no mode at all: the
pack could not take it in `CHARGE_PV`, and `CURTAIL` could not charge. The inverter does both at once. The released
`HOLD` of a carried `SELF_CONSUME` at the reserve with `grid_export_kw = 0` and a 1 kW pack beside 3.6 kW of PV was
the case (T-058 planned the commitment's `HOLD` with `CURTAIL`, rung 2).

29. **A `CURTAIL` row may charge, and what it charges is PV.** `CURTAIL` keeps its discharge at zero and its
    curtailment of at least the activity, but the charge is free between zero and the PV left after the house and
    the curtailment (`validate_modes`: `0 <= charge <= max(PV - curtailment - load, 0)`, plus the solver's noise). A
    row that charges and curtails reads `CURTAIL` (the classifier gives it precedence), as a row that only curtails
    always did. A charging row takes no grid energy, for the pack or beside it: where the direction flag says charge,
    `grid_load + grid_battery <= cap × (2 - CURTAIL - battery_mode)` is zero, so a plan with a negative buy price and
    grid charging allowed does not import into a `CURTAIL` row (the flag could otherwise stand for a grid-fed house
    with the PV going to the pack, one flow as good as the other); the validator reads the same bound, so a
    `CURTAIL` row that charges from the grid is not a valid mode. The rows are added only for an interval where the PV
    beyond what the pack takes (`curtail_need` less `charge_kw × h`) is positive, so a problem without such an
    interval has the same model as before (`tests/test_strategy_golden.py` pins the rows of the default strategy). A
    flexible-load probe takes its load first: the charge from the PV it leaves is bounded by `bc + flex_load <=
    surplus`, one `curtail_charge` binary per interval. Pinned by `tests/test_curtail_dwell.py`
    (`test_a_curtail_row_charges_only_from_the_pv_left_after_the_house`,
    `test_a_curtail_row_that_charges_from_the_grid_is_not_valid`).
30. **PV beyond the pack is a reason to curtail in a carried commitment.** `CURTAIL` is offered in a committed
    interval after a full pack (point 11) and where a released `HOLD` returns (point 13). It is also offered
    where the pack cannot take the PV above what the site passes by its charge power, whatever the pack held
    before: `CHARGE_PV` has no flow in the interval, so there is nothing to wait for. Only for a `CHARGE_PV`/
    `SELF_CONSUME` commitment, and for a probe only for the loads below what leaves the PV beyond the pack
    (`_open_beyond`). A `HOLD` that lasts the commitment still has no flow for PV above the cap, so the
    interval is `CURTAIL`; the T-047 takeover applies as before. The validator reads such a `CURTAIL` as legal
    (`_beyond_the_pack`); it reads the realised slot, the probe load already in it, so it is looser than the model's
    offer by the probe margin of 1e-5 kWh only.
31. **The rungs of point 24 are rarer.** Rung 2 (the commitment's own `HOLD`) serves the observed-SoC safety
    exception and the releases the minimum duration does not let the release problem keep (below); rung 1 plans the
    released `HOLD` of the sunrise on a pack that cannot take the PV, and rung 3 stays for the commitments no plan
    can keep (the class T-070 measured: 123 of 1600 aimed problems, ADR-0004).

`allow_curtailment = false` with PV above the cap and above what the pack takes by its charge power is planned only
as a forced spill (T-041), that is where the pack cannot take the need by its room and the gate is open. Point 29
holds on any row where `CURTAIL` can be chosen, so that row now charges what the pack takes and spills the rest,
where it spilled everything before: a site that cannot curtail is changed by this amendment as well. In a reviewer's
draw (bell-shaped days rolled to the end, seed 81 × 150, 64 of them with `allow_curtailment` off, this branch merged
with main `f7f9ae1` against main `f7f9ae1`) 22 of those plans have a charging `CURTAIL` row, none imports in it, 21
are cheaper, 1 is newly planned and none is newly infeasible; `tests/test_forced_spill.py` pins the row. A pack with
room for the need still has no plan there (point 24 end): no spill is forced and nothing else can take the surplus.
What the house does then (report over-capacity, throttle in the plan) is an owner decision, tracked in T-066 with the
forced-spill cases.

The Deye controller is not changed here. At this task it did not carry out the charge of such a row: `CURTAIL` ran
the `HOLD` profile, whose maximum charge current and grid current are both `hold_grid_current` (1 A by default, about
50 W; `controller_profile.py`), with the planned end state of charge rounded down as the target, so the pack took about
50 W where the plan has it take up to the charge power. What the controller writes for a charging `CURTAIL` row is
decided in T-076 (amendment at the end of this record).

Measured with `benchmarks/curtail_sweep.py`, seeds 11, 22, 33 × 300 = 900 draws per kind, this branch (engine
`3679099`, main `fde1cc5` merged in) against main `fde1cc5`, both solved through `solve_released`. Main's tree was
checked identical to `git archive fde1cc5` for `custom_components` and `benchmarks`; the draws of the compared files
are the same on both trees:

| Kind | Planned, main → head | Newly infeasible | Newly planned |
| --- | --- | --- | --- |
| `run` | 893 → 893 | 0 | 0 |
| `negrun` | 889 → 891 | 0 | 2 |
| `probe` | 870 → 879 | 0 | 9 |
| `deprun` | 854 → 854 | 0 | 0 |
| `deprobe` | 830 → 830 | 0 | 0 |
| `resrun` | 863 → 863 | 0 | 0 |
| `fullrun` | 872 → 872 | 0 | 0 |
| `roll` | 893 → 893 | 0 | 0 |
| `negroll` | 889 → 891 | 0 | 2 |
| `deproll` | 854 → 854 | 0 | 0 |
| `resroll` | 863 → 863 | 0 | 0 |
| `fullroll` | 835 → 835 | 0 | 0 |

No draw is newly infeasible, newly infeasible while followed, or a new solver failure, and the rolled kinds' first
failing boundary never moves earlier (`rollcompare`: 0 in all five kinds; two `negroll` draws that main's roll could
not follow to the end are followed). Draws with a boundary where the next plan contradicts the one before that main
did not contradict (`rollcompare`: `roll`, `negroll`, `deproll`, `resroll`, `fullroll`): 10, 10, 5, 21 and 3; draws
where main's contradicted boundary is none on head: 5, 1, 16, 26 and 3.

What changes is which rung plans a draw (`python benchmarks/curtail_sweep.py rungs <kind> <seed> <count>` prints the
counts): the draws planned only on rung 3 (the commitment dropped) fall from 310 to 73 of the 900 `deprun` draws (310
to 87 `deprobe`, 32 to 9 `run`, 30 to 10 `negrun`, 33 to 19 `probe`), and those on rung 2 (the commitment's own `HOLD`)
from 38 to 10 (`deprun`; 33 to 9 `deprobe`; `resrun` 279 to 90); the others are planned by rung 1 now. Draws with no
plan on any rung are the same on both trees: 46 of 900 `deprun`, 69 `deprobe`, 37 `resrun`, 28 `fullrun`. The
objective is lower than main's in 144, 135, 128, 175, 174, 120 and 34 draws (`run`, `negrun`, `probe`, `deprun`,
`deprobe`, `resrun`, `fullrun`): the pack takes what main curtailed. It is higher in 2, 1, 0, 69, 56, 15 and 0 draws,
and each one of those 143 draws is one that main planned on rung 2 or 3 and head plans on rung 1: a plan that keeps
the commitment costs more than one that leaves it.

The scripts behind these numbers (`rung_list.py`, `why2.py`, `probe.py`, `cmp.py`, `carry_rung2.py`,
`carry_rung2_analyse.py`, `leave.py`) are kept with the task's artifacts, not in the repository.

A reviewer's probe, rerun on this head (`probe.py`: 300 bell-shaped days, seeds 71 and 72 × 150, a small charge power,
rolled to the end): 135 plans contain a charging `CURTAIL` row, none imports in such a row and none charges where the
PV is not beyond the pack; 0 newly infeasible, 0 solver failures, 150 of 150 followed to the end on both trees in
both seeds.

Still `infeasible`: `allow_curtailment = false` with PV above what the pack takes by its charge power while the pack
has room for it (no forced spill; T-066), `preserve_initial`
whose terminal state of charge is out of reach (T-065, T-070), and a carried commitment no plan can keep (rung 3).

## Amendment (T-076): the controller carries out a charging CURTAIL row

A `CURTAIL` row that charges (T-064) ran the `HOLD` profile on the inverter, so the pack took about 50 W where the plan
has it take up to the charge power, the published state of charge was not reached, and the `SELF_CONSUME` it pays for
started lower, at every replan.

31. **A `CURTAIL` row with `charge_kwh` above 0.000001 kWh runs the PV charge profile at the planned current.** Charge
    current = `charge_kwh × eta_charge / (h × V)` rounded up to the step of the charge entity, at least
    `hold_grid_current` and at most the cap (the formula `CHARGE_GRID` uses for its planned grid share); grid current
    0 A, discharge current 0 A, every TOU program Disabled with SOC 10 % and 49.6 V. The pack takes the PV at that
    current and the inverter curtails the rest by itself. The charge of such a row is free between 0 and the PV left
    after the curtailment and the load (point 29), so the plan chooses it, and it often chooses less than the charge
    power: a row planning less than `charge_kw × h` while the pack ends below its ceiling is 75 of the 879 charging rows
    of the sweep below (8.5 %), for example 0.137 kWh where 0.75 kWh fits (pack 0.725 of 5 kWh, PV 2.2, curtailment
    1.54). At the cap such a row would end above the published trajectory and pay the model's wear cost for energy it
    chose to curtail, so the cap is not the current; a row at `charge_kw × h` rounds up to the cap and gets all of it.
    At 100 % SOC the row follows `CHARGE_PV` (0 A in `Capacity` mode, the cap in `Voltage` mode, whose SOC estimate
    drifts) and there is no balance hold on it. It raises no `curtail_unsupported` warning: the charge is carried out.
    A `CURTAIL` row with `charge_kwh` up to 0.000001 kWh stays the `HOLD` profile with the warning. The native
    controller (`controller_profile.py`) and the generated blueprint (`tools/deye_controller/build.py`) make the same
    decision.
32. **The threshold is 1e-6 kWh, the display classifier's zero** (`machine_state._TOL`; `CURTAIL_CHARGE_MIN_KWH`, with a
    test that the two are equal). The plan publishes the solver's flows unrounded (`machine_snapshot`), and
    `settle_mode_noise` leaves a flow up to 1e-6 kWh as it is, so a `CURTAIL` row that does not charge carries
    `charge_kwh` of 2e-34 to 4.7e-13 (or a hair below 0) as often as it carries 0. A test `> 0` would charge such a row at
    the cap, silently, and which rows do it would depend on float noise.
33. **The frozen goldens stay.** `tests/golden/` and `tests/deye_oracle_v0137.py` hold every `CURTAIL` row; the parity
    corpus and the replay of `tests/test_deye_controller.py` keep `charge_kwh` at 0 on their `CURTAIL` rows (no rng
    draw is added) and the new rule is pinned in `tests/test_controller_native_curtail.py` against both engines.

Measured with `benchmarks/curtail_charge_rows.py 150 11 22` (draws of `curtail_sweep.py` `run`, `negrun`, `deprun`,
`resrun`, `fullrun`, 1456 plans, planned through `solve_released`, rows as `machine_snapshot` publishes them), on this
branch with main `06f935e` under it and again with main `7c33fe8` merged in; the counts are the same on both, since
this task changes the controller only and not the engine: 7036 `CURTAIL` rows, 879 charge (above 0.001 kWh), 75 of them plan less than
`charge_kw × h` below the ceiling; of the 6157 that do not charge, 5129 are exactly 0, 524 a hair below 0 and 504
(8.2 %) between 2e-34 and 4.7e-13 kWh; none lies between 1e-6 and 1e-3 kWh, so the threshold sits six orders of
magnitude above the largest noise (4.67e-13) and below every charge the sweep drew.

Rejected: a grid direction on the row (the model never imports in a `CURTAIL` row, and the grid current would buy
energy the plan does not); the cap as the charge current (it charges above the plan in 8.5 % of the charging rows
and is right only where the row plans the charge power); a threshold of 0 (one plain `CURTAIL` row in twelve would
charge from PV at the cap, not hold).

## Amendment (T-078): a CURTAIL run that PV beyond the pack opens goes on through its minimum duration

Status: implemented and swept on T-078. Measured on main `bb70c60` (T-080 and T-084 merged over `ef55de2`, which had
T-070, T-074, T-076, T-079 and T-081 over `06f935e`) against the branch head `2cc08bb`, the merge of that main; its
engine is that of `ae75c25` (`dispatch_policy.py` is the only engine file that differs from main) and the sweeps ran
on it; the commits after `2cc08bb` change documents only. The first version of this amendment was measured on
`06f935e` and reviewed there, the second on `ef55de2`. The counts of the kinds the first one measured are the same on
all three mains, and every count below is the same on `ef55de2` and `bb70c60`: T-080 (the unmoded settle of
`mode_noise.py`) and T-084 (the guides) moved none. Decision 3 of the T-069 amendment (rung 2 is
decided afresh, nothing is stored) stands: the finding is a gap in the release problem, not a reason to carry the
commitment's `HOLD`.

After T-064 the draws planned only on rung 2 were 90 of 900 `resrun` and 124 of 1200 `resroll` draws (80 of 306
boundaries after a rung-2 row contradicted the next plan). The question was why the release problem, rung 1, had no
plan for them, and whether the model should have one.

34. **The release had no plan because rule 1 of `_offer_curtail` forbade `CURTAIL` inside a `CURTAIL` run.** The first
    interval of such a draw has PV the pack cannot take (point 30), so it is `CURTAIL`, and a `CURTAIL` first row is
    what the coordinator stores as the new commitment (`SELF_CONSUME` to `CURTAIL`, a new group and a new clock from the
    plan's time), with the commitment's minimum duration. The next interval has PV the pack and the export cap take
    between them, so `beyond` is empty there, and the dwell rows of the run (`holding`) forbid leaving `CURTAIL` before
    the minimum duration is over. `_offer_curtail` is called for that interval, and was before: `seen_full` is a
    non-empty dict from the second committed interval on (a `lock_full` binary is added at the end of every committed
    interval but the last, in a commitment that has `CHARGE_PV` among its modes), so the gate
    (`seen_full or beyond is not None`) is open. Its first rule,
    `CURTAIL <= sum(lock_full) + beyond`, is what forbids `CURTAIL`: every `lock_full` is 0 on a pack that was never
    full, `beyond` is empty, and the term of the `CURTAIL` before it was not there (the second rule, `full`,
    `curtailing`, `released`, `beyond`, had it). The interval had no mode and the problem no plan. Measured, not read:
    the spy of `artifacts/T-078/offer.py` records two calls of `_offer_curtail` for the pinned problem on `bb70c60` (the
    first with `beyond` True, the second with one `seen_full` term, one `curtailing` term and no `beyond`), and the
    release is rung 2 at a pack of 1.0 kWh and rung 3 at 2.0 kWh there, rung 1 on both with the edit of point 35. The
    case is the rung-2 draw whose commitment is the `SELF_CONSUME` of a pack at the reserve and whose first release row
    is the `CURTAIL` that charges
    (`tests/test_curtail_dwell.py::test_a_curtail_run_that_the_pv_beyond_the_pack_opens_goes_on_through_its_duration`).
35. **A `CURTAIL` under way is a licence to go on curtailing: `curtailing` joins rule 1.** `_offer_curtail` takes the
    `curtailing` terms of `constrain_modes` (the `CURTAIL` of the interval before) in its first rule as well, next to
    `seen_full` and `beyond`: `CURTAIL <= seen_full + beyond + curtailing`. It is the only change to the engine. A
    second edit was made with it, `or curtailing` in the gate, and removed in review: it never changes the offer
    (`curtailing` is non-empty only after an interval that was `CURTAIL` or a bridge, and there `seen_full` is non-empty
    already). Measured on `ef55de2` with the term, with a spy on `_offer_curtail` (`artifacts/T-078/gate.py`): of 18 337
    calls over 3600 rolled draws (seeds 22 and 33 × 300 × `roll`, `negroll`, `deproll`, `resroll`, `fullroll` and
    `aimroll`, 8 intervals) none has `seen_full` empty and `beyond` unset, so none was opened by `curtailing` alone;
    and the tests of the file pass without it. The edit of rule 1 is the whole change: without it the new tests fail (on
    the tree of `bb70c60` five cases of the file's 164 fail, 159 pass), and the pinned problem is rung 2 and rung 3
    again. A first `CURTAIL` still needs one of the other licences, and `CURTAIL` to `CURTAIL` needs no dwell rule (`validate_modes` checks the dwell
    groups only), so the validator did not change; the plans the new offer opens are legal under it
    (`test_the_resrun_draw_the_release_could_not_plan_is_the_first_rung` solves draw 144 of seed 33 `resrun` on rung 1
    and validates it).
36. **The licence is the previous interval's `CURTAIL`, not the minimum duration.** The chain is not cut where the
    minimum duration ends, and it is not a way to start a `CURTAIL` the pack could take as `CHARGE_PV`: the row
    still curtails at least the activity of the mode and, where the PV is not beyond the pack, charges nothing (point
    29), and the first row of a chain is licensed as before. The commitment the coordinator stores is the same, and
    `test_the_coordinator_stores_the_curtail_run_and_the_next_plan_goes_on_with_it` runs `_commit_battery_direction`
    on the first row (through the benchmark's `follow`) and plans the next boundary on rung 1 with the run as the
    commitment; the rungs of point 24 are as they were.

Measured with `benchmarks/curtail_sweep.py`, seeds 11, 22, 33 and 44 × 300 = 1200 draws per kind, main `bb70c60`
against this branch, both solved through `solve_released`. `compare` counts, main → head (`aimrun` is a kind of T-070):

| Kind | Newly infeasible | Newly planned | Solver failure | Objective worse | better |
| --- | --- | --- | --- | --- | --- |
| `run` | 0 | 0 | 0 | 1 | 2 |
| `negrun` | 0 | 0 | 0 | 1 | 1 |
| `probe` | 0 | 0 | 0 | 2 | 3 |
| `deprun` | 0 | 0 | 0 | 21 | 20 |
| `deprobe` | 0 | 0 | 0 | 32 | 40 |
| `resrun` | 0 | 0 | 0 | 23 | 9 |
| `fullrun` | 0 | 0 | 0 | 0 | 0 |
| `aimrun` | 0 | 0 | 0 | 1 | 4 |

The plan sets are the same (no plan is gained or lost); what changes is the rung. `rungs` counts, draws per rung,
main → head over 1200:

| Kind | Rung 1 | Rung 2 | Rung 3 | No plan |
| --- | --- | --- | --- | --- |
| `resrun` | 1024 → 1148 | 124 → 0 | 0 → 0 | 52 → 52 |
| `deprun` | 1026 → 1122 | 11 → 0 | 93 → 8 | 70 → 70 |
| `run` | 1181 → 1187 | 0 → 0 | 10 → 4 | 9 → 9 |
| `negrun` | 1175 → 1182 | 0 → 0 | 13 → 6 | 12 → 12 |
| `fullrun` | 1140 → 1140 | 0 → 0 | 25 → 25 | 35 → 35 |
| `aimrun` | 984 → 993 | 0 → 0 | 171 → 162 | 45 → 45 |

The rolled kinds (`rollcompare`, 1200 draws each): no draw is newly infeasible while followed, none is fixed, the
first failing boundary never moves and no draw newly has a boundary whose plan the next one contradicts; 83 draws of
`resroll` and 11 of `deproll` no longer have one. The executed trace differs in `roll` 6 draws, `negroll` 7,
`deproll` 96, `resroll` 124, `aimroll` 9 and `fullroll` 0.

**What the longer `CURTAIL` costs.** T-069 declined a longer `CURTAIL` ("the remedy is not a longer `CURTAIL`") on the
kWh a carried rung 2 left in the pack against the boundaries it fixed, so this change is measured the same way, on the
flows the rolls execute: each roll follows the first row of every plan for 8 intervals, the draws followed to the end
on both trees, `artifacts/T-078/review/scripts/energy.py` and `analyse.py` (kept with the task's artifacts, not in the
repository), main `bb70c60` → head `2cc08bb`:

| Executed over the rolls | `resroll` (1148 draws) | `deproll` (1130 draws) |
| --- | --- | --- |
| Draws with a different executed trace | 106 | 52 |
| Rung-2 rows | 308 → 0 | 32 → 0 |
| Rung-3 rows | 30 → 14 | 95 → 9 |
| Boundaries contradicted (draws with one) | 219 (199) → 129 (117) | 70 (63) → 59 (52) |
| Executed mode changes | 1877 → 1822 | 1693 → 1708 |
| `CHARGE_PV` rows | 2048 → 1999 | 1674 → 1714 |
| `CURTAIL` rows | 2571 → 2689 | 3992 → 3958 |
| Charged into the pack, kWh | 1190.3 → 1174.5 | 1068.7 → 1079.9 |
| Curtailed, kWh | 1862.6 → 1876.9 | 3881.2 → 3874.2 |
| Exported, kWh | 1312.2 → 1315.0 | 826.7 → 822.4 |
| Imported, kWh | 271.4 → 275.2 | 185.6 → 184.1 |
| Pack at the end of the rolls, kWh (sum) | 1446.4 → 1434.0 | 1429.9 → 1438.9 |
| Cost of the executed flows (negative: earned) | −310.81 → −309.00 | −190.89 → −190.29 |

Of the draws that differ, `resroll`: the pack ends lower in 50 and higher in 33, the cost is higher in 60 and lower in
46; `deproll`: lower in 20 and higher in 26, the cost higher in 18, lower in 21 and the same in 13. `roll` has 1 draw
and `negroll` 2 with a different executed trace (+0.25 and −0.37 kWh at the end). The worst draw is seed 22, number 135
of `resroll` (a pack at the reserve of 0 kWh, `SELF_CONSUME` since 10:59, 120 minutes, an export cap of 2 kW, a 5 kWh
pack that charges 1 kWh an interval, and PV 0.9 to 1.8 kWh above the house in each of the first eight intervals): main
executes `CURTAIL`, `CURTAIL` (rung 2, the second charging 1 kWh), `CHARGE_PV`, `CHARGE_PV`, `CURTAIL` (1 kWh) and
three more `CURTAIL`, 4 kWh charged in all; the branch executes `CURTAIL` in all eight intervals, charges nothing and
ends 3.72 kWh lower. Its intervals after the first have PV that the pack and the export cap take between them or leave
0.09 kWh over, and the plan charges in none of them: where the PV is not beyond the pack a `CURTAIL` row charges
nothing (point 29), and the row with PV beyond it is free to charge and the plan does not.

Over the 2400 draws that is 4.5 kWh less charged (−15.8 in `resroll`, +11.3 in `deproll`), 3.4 kWh lower at the end
of the rolls (−12.4 and +9.0), 7.4 kWh more PV curtailed (+14.3 and −7.0), 2.3 kWh more imported (+3.7 and −1.5), the
executed cost 0.6 % (`resroll`) and 0.3 % (`deproll`) higher, and 101 fewer contradicted boundaries (−90 and −11).
T-069's carry (table after T-064) took 46.3 kWh less charged (−42.0 and −4.3) for 48 fewer (−41 and −7): about 1 kWh of
PV not stored for each boundary fixed, against 0.045 kWh here, a factor of about 20. The trade is accepted for four
reasons:

1. *It is the model's own plan, not a carried one.* Carrying rung 2 kept the commitment's `HOLD` past a boundary where
   a charging plan exists (T-069 (i)) and needed a new stored key (reasons 1 and 2 of point 28). Here nothing is
   stored, rung 2 is not reached, and the plan is the one the release problem finds under the commitment's minimum
   duration.
2. *A free interval after a `CURTAIL` that PV beyond the pack opened already plans `CURTAIL` on main.* The same two
   intervals with the commitment over before the second (the 45-minute case of the test) plan `CURTAIL`, `CURTAIL`
   on `bb70c60`. What this change adds is that the setting the user chose, the minimum duration, binds the run and
   does not make the problem unsolvable: the plan is the same, and its rows no longer need rung 2 or 3 to exist.
3. *The minimum duration is the user's setting.* `CURTAIL` is a dwell group of its own (`dwell_group`; a bridge is
   the run going on, point 5): a run of it may not change mode inside its duration, and the first row of a `CURTAIL`
   is what the coordinator stores. A `CHARGE_PV` inside it is a mode change the setting forbids; the executed
   `CURTAIL`, `CHARGE_PV`, `CURTAIL` of point 27 was the rung-2 plan going back to the release, and it is not the
   plan a user who chose a minimum duration of 120 minutes asked for.
4. *The alternatives are worse for the pack.* Without this rule these draws are planned on rung 2, the commitment's
   `HOLD` beside the pack, or on rung 3 without the commitment: 124 and 0 of 1200 `resrun` draws, 11 and 93 of `deprun`
   (rungs table above).

**What the user sees.** After one interval with PV beyond the pack, a pack at the reserve does not charge in the
intervals of that `CURTAIL` run whose PV the pack and the export cap take between them, until the minimum duration is
over: up to 105 minutes after the first interval at a minimum duration of 120 (the run starts with the plan's time
and counts from it), 45 at 60, 15 at 30. In an interval of the run that has PV beyond the pack again the row may charge
(point 29). The energy not stored is curtailed or exported; the figures above are what that came to in the sweeps.

The scripts behind these numbers (`job3.sh`, `agg3.py`, `cmp3.sh`, `offer.py`, `draw.py` and the review's `energy.py`
and `analyse.py`; the earlier `sweep.sh`, `rungs.sh`, `rolltot.py`, `find.py`, `exp.py`) are kept with the task's
artifacts, not in the repository.

Consequences:

- The coordinator and the stored commitment are unchanged; `safety_exception` `observed_soc_minimum` is reported by
  rung 2 for the observed-SoC exception only, which the sweeps do not draw.
- The guides and `docs/model.md` say it in one sentence in the minimum-duration paragraph: a `CURTAIL` run that the PV
  beyond the pack opened goes on through its minimum duration, and does not charge in the intervals whose PV the pack
  takes.
