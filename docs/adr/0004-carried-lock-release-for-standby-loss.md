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
- Not changed: `preserve_initial` that cannot be met after the forced discharge stays `infeasible` (T-001).
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
   other `HOLD` row, an idle one included, takes the normal release check. Without `bridges` every idle row reads as
   a bridge. Flexible-load probes are out of scope of the bridge reading: the model takes the idle set with the
   largest probe it may add, and the validator checks the bridges the solver reports against the problem it is
   given. The controller writes the `HOLD` registers for a bridge row, which is what the inverter does anyway with
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

Evidence: `tests/test_carried_commitment_sweep.py` keeps a seeded corpus of committed problems (2 to 16 quarter
hours, loads and PV around the minimum activity, both initial modes, 30/60/120 minute durations, sites that cannot
export, packs a hair above the reserve or below the ceiling, standby loss, a legacy direction guard, flexible-load
probes). It fails on main and passes here: every problem is planned or `infeasible`, never rejected by the
validator, and no plan starts `HOLD` on a chargeable surplus. Against main, seed 11 with 1200 problems in each of
three variants (plain, with the legacy direction guard, with flexible-load probes) gave no newly infeasible
problem. What stays `infeasible` there is `preserve_initial` that cannot be met (T-001) and the rare standby loss
that only scattered PV could pay.
