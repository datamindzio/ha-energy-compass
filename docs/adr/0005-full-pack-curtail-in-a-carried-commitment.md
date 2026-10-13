# ADR-0005: A full pack ends a carried PV-follow commitment in HOLD or CURTAIL, a released HOLD has no clock, and a CURTAIL run goes on through a PV gap

Status: accepted
Date: 2026-10-12 (amended 2026-10-13, T-037)
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
   row; before, the `HOLD` clock forbade it). This applies to the empty-battery release as well.
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

- A `HOLD` inside its minimum duration cannot pass PV above the export cap: the carried `HOLD`, the
  safety-exception `HOLD` at the maximum state of charge, and a `HOLD` after a finished `CURTAIL` run (T-047).
- A free plan whose horizon ends before the minimum duration of the run it needs; `preserve_initial` and a
  standby loss that make the terminal state of charge unreachable; `allow_curtailment = false` with PV above
  the cap; a case where charging and curtailing in one interval would be needed to keep the state of charge.
