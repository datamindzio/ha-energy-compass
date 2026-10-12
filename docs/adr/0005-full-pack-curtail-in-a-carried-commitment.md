# ADR-0005: A full pack ends a carried PV-follow commitment in HOLD or CURTAIL, and a released HOLD has no clock

Status: accepted
Date: 2026-10-12

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

1. The full-pack release also opens `CURTAIL`, once an interval has started at the ceiling test of
   `lock_full` (cumulative over the commitment; never from the empty-battery release).
2. A `HOLD` released from a carried PV-follow commitment starts **no clock of its own**: the commitment's
   deadline stays the only one. It may turn to `CURTAIL` (a normal mode with its own clock) inside the
   commitment, and is free after the deadline. It does not go back to `CHARGE_PV`/`SELF_CONSUME` (an explicit
   row; before, the `HOLD` clock forbade it). This applies to the empty-battery release as well.
3. `validate_modes` mirrors it: a released `HOLD` keeps the commitment's clock, `CURTAIL` is accepted from
   the commitment or the released `HOLD` once the pack was seen at the ceiling, nothing else is.

## Alternatives rejected

1. **Release to `CURTAIL` only, `HOLD` keeps its clock** — the `HOLD` must last the full duration, so a
   curtailing slot inside it is still blocked; and a released `CURTAIL` has to last `minimum_mode_minutes`
   with PV above the activity in every interval, which fails when PV drops.
2. **Release to `HOLD` and shorten its clock** — an arbitrary second duration to explain.

## Consequences

- Seeded sweep against the code before this change, 3 seeds × 700 random problems (export cap 8/2/0 kW,
  PV up to 2.2 kWh per interval, carried `CHARGE_PV`/`SELF_CONSUME`/`HOLD`/`CURTAIL`): 0 newly infeasible,
  9 newly feasible, 23 changed objectives, none worse.
- `benchmarks/optimizer.py` (96 slots, all strategies) unchanged within noise: the rows exist only inside a
  carried commitment.
- The empty-battery `HOLD` also loses its own clock; its plans can only get cheaper.
- Not changed: a free plan (no carried lock) with PV dropping inside a `CURTAIL`'s minimum duration on an
  export-capped site can still be `infeasible` (T-030 body, "Free plans"); it needs its own decision.
