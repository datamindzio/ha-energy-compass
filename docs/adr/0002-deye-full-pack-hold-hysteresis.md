# ADR-0002: Deye controller — hysteresis on the full-pack `HOLD` profile, read from the previous decision

Status: accepted
Date: 2026-10-12

## Context

Since T-009 the Deye controller writes a different profile for `HOLD` once the SOC source reads 100 % in
battery operation mode `Voltage` (`full_pv` in `tools/deye_controller/build.py`, `DECISION` template):

| | charge | grid | TOU |
| --- | --- | --- | --- |
| `HOLD`, SOC < 100 % | `hold_grid_current` | `hold_grid_current` | Grid, target = planned end SOC |
| `HOLD`, SOC ≥ 100 % (full-pack profile) | cap | 0 A | all Disabled, SOC 10 % / 49.6 V |

The switch had no hysteresis and the blueprint runs on every SOC change. Each crossing is a six-write
transaction that starts with charge 0 A. Without PV the two profiles pull the SOC in opposite directions (the
inverter's own draw of about 140 W below, 1 A from the grid above), so the controller oscillates around the
threshold. An integer SOC source limits that to one cycle per percent (hours at 25 kWh). A fractional source,
the counter-based estimator that motivated T-009, can cross 99.9 ↔ 100.0 every few minutes.

`CHARGE_PV` and `SELF_CONSUME` are not affected: below 100 % in `Voltage` mode they write the same values as
the full-pack profile.

Constraints:

- The blueprint is imported by the user separately from the integration, so the two can be on different
  versions.
- The integration rejects unknown runtime keys (`validate_runtime`, `RUNTIME_KEYS` in
  `custom_components/energy_compass/controller.py`); a rejected `energy_compass.controller_runtime` call stops
  the whole run.
- The stored runtime already carries the previous run's decision: `state` and `desired` are persisted before
  the write transaction of every run.

## Decision

`HOLD` in `Voltage` mode enters the full-pack profile at SOC ≥ 100 % and keeps it while SOC > 99 %; at
SOC ≤ 99 % it returns to the `HOLD` profile and needs 100 % to enter again.

"Was in the full-pack profile" is read from the stored runtime, with no new key: the previous decision had
`state == 'HOLD'` and no TOU program in direction `Grid`. A plain `HOLD` always sets `Grid` on the active
program, so the two are distinguishable; a test pins that invariant.

The band is a constant of one percentage point, not a blueprint input. An integer SOC source behaves exactly
as after T-009 (99 % is the `HOLD` profile). Only consecutive `HOLD` runs hold the profile: after any other
state, mode `Off`, an invalid plan or `Capacity` mode the next `HOLD` decides from SOC ≥ 100 % alone.

## Alternatives rejected

1. New runtime key (`full_pack`) — rejected because the integration whitelists runtime keys: a blueprint
   updated before the integration would fail every run with `runtime_invalid_key`. It would also need an
   integration change, a release ordering rule and a migration note for a flag the stored decision already
   implies.
2. Keep the behaviour and document the write cadence — rejected because with a fractional SOC source the
   cadence is unbounded in practice (a transaction per crossing, each starting with charge 0 A), which is
   repeated register wear and charge interruptions through a whole night `HOLD`.
3. Read the applied profile back from the inverter entities (as the current-ceiling hysteresis does) —
   rejected because the entities show what was last written, not what was decided: after an aborted
   transaction or a restore to base they hold a partial or base profile, and telling the full-pack profile
   from `CHARGE_PV` or a reached `CHARGE_GRID` row (charge cap, grid 0 A, Disabled as well) needs a comparison
   of every program register. The stored decision is one dictionary written by the controller itself.
4. Hysteresis band as a blueprint input — rejected because nobody has asked to tune it and every input adds
   a guide row in two languages, a builder field and a test; one percentage point matches the resolution of
   the integer SOC sources already in use.
5. Hysteresis for `CHARGE_PV` and `SELF_CONSUME` too — rejected because their writes do not change at the
   threshold; holding the profile across a row change would only make `HOLD` skip its grid top-up after a PV
   row.

## Consequences

- At most one profile transaction per percentage point of SOC movement in `HOLD`, whatever the resolution of
  the SOC source. Without PV the remaining cycle (drift down on own draw, top-up from the grid) takes hours.
- Between 99 % and 100 % a `HOLD` that came from the full-pack profile does not charge from the grid; the
  pack may drift down by up to one percentage point before `HOLD` tops it up.
- The rule depends on plain `HOLD` writing `Grid` on the active program. A change to the `HOLD` profile must
  keep `tests/test_deye_controller.py::test_voltage_hold_profile_is_never_taken_for_the_full_pack_profile`
  green or replace this mechanism.
- If SOC changes between the first decision of a run and its write transaction right at 99 %, one extra
  transaction is possible on the next run. It cannot repeat, because re-entry needs 100 %.
- Generator only; no integration, store or schema change. Old and new blueprints work with the same
  integration version.
