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
`HOLD` starts its own minimum duration, as with the existing release.

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
- Not changed, and found here: a committed interval in which neither `SELF_CONSUME` nor `CHARGE_PV` reaches
  the minimum activity (net load and surplus both below it) is `infeasible` unless the interval before left
  the pack empty; it predates T-001. Requiring `CHARGE_PV` instead of `HOLD` in a released interval with PV
  surplus was tried and dropped for this reason: it turned plans into `infeasible` when the next committed
  interval was such an interval. Both go to a follow-up task.
- With standby loss off (`idle_drain_kw = 0`) `owed` never exceeds the reserve and the lock floor is the old
  one; the only new row is the first-interval release for a pack within one minimum activity of its reserve.
