# ADR-0005: A full pack ends a carried PV-follow commitment in HOLD or CURTAIL, and a released HOLD has no clock

Status: accepted
Date: 2026-10-12
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
- Not changed, one decision for both (T-037): `CHARGE_PV` cannot fill the pack and curtail in the same
  interval, and `CURTAIL` cannot last its minimum duration when PV drops.
  - A **carried** commitment, a committed interval in which the pack fills while the rest of the surplus is
    still above the export cap or the inverter rating, stays `infeasible` until the commitment ends. Repro:
    `pv = (0.5, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9)`, load 0.1, `grid_export_kw = 2`, `initial_kwh = 9.5`,
    carried `CHARGE_PV` since 15 min before the first slot. Slot 1 has 0.1 kWh of room (above the minimum
    activity, so `lock_full` is not set) and 0.8 kWh of surplus: `CHARGE_PV` fills the pack, 0.7 kWh stays
    above the 0.5 kWh cap, and neither `HOLD` nor `CURTAIL` is open yet. It was `ok` before T-010.
  - A **free** plan (no carried lock) with PV dropping inside a `CURTAIL`'s minimum duration on an
    export-capped site can still be `infeasible`.
