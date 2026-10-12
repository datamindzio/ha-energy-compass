# ADR-0006: Consumption probes stay exact; a probe that misses its limit is unknown

Status: accepted
Date: 2026-10-12

## Context

A consumption probe (`engine/consumption._probe`) re-solves the whole planning horizon with `probe_kwh` of
extra load in one display interval and publishes `(cost of that plan − cost of the base plan) / kWh`. The
probes run one after another, each bounded by `probe_time_limit_s` (default 2 s), all of them inside
`total_time_limit_s` (default 60 s) minus the base solve and a one-second reserve. One unknown full reference
probe moves the levels to the `short_coverage` policy, and an interval whose own probe is unknown has no level.

Since the SELF_CONSUME load-following rule (T-001, T-012) the probes of `self_sufficiency` and `backup_ready`
do not fit that limit. T-016 put the cost to the owner, who accepted it and expected an optimisation (T-011).
T-011 and T-022 worked on the base plan only, ADR-0003 decided the base plan's MIP gap and left the probes to
T-027. T-027 then took three dev rounds:

1. Probes with the base plan's operating modes fixed outside a window around the probed slots. Fast, but the
   review measured published costs of 1.75 PLN/kWh where the full solve gives 0.45 and the dearest buy price
   is 0.85.
2. The full solve first and widening windows as a fallback after a timeout. At the shipped limits the windows
   still published up to +1.41 and −0.29 PLN/kWh against the full solve, now also for `cost_min`, presented as
   complete.
3. Windows removed, the limit documented. The text scoped it to 192-slot horizons and claimed the other four
   strategies finish 96 probes there; neither was measured.

Measurements for this ADR (`python -m benchmarks.probes`, CPU seconds, Apple M4 Max, `main` at b015ee7, table
in `docs/runtime-validation.md` § Consumption probe limits):

- 96 slots (the default 24 h of quarter-hour prices), 24 hourly probes: `self_sufficiency` 4.4-19 s per probe,
  0 of 24 known; `backup_ready` 1.9-3.5 s, 2 of 24; the other four strategies 0.1-0.6 s, 24 of 24.
- 48 slots: 12 of 12 for every strategy. 192 slots: no strategy completes its probes.
- HiGHS log of the first `self_sufficiency` probe at 96 slots: the optimum (89.0929 in the weighted objective)
  is found after 6.2 s of a 7.0 s solve; the incumbents before it are 0.05-1.3 above it; the root bound stays
  0.04 below it. The probe cost being measured is 0.09.
- Dev round 3: a full solve cut off at the windowed probe's objective took 9-16 s against 6-10 s plain.

`scipy.optimize.milp` (1.18.1) has no warm start, no absolute gap and no callback (ADR-0003).

## Decision

1. A published probe cost is always the difference of two proven optima of the same model: the base plan and
   the full-horizon candidate. No restricted, windowed, gapped or incumbent-based probe is published, with or
   without a flag.
2. A probe that does not prove its optimum inside `probe_time_limit_s` is unknown. The existing
   `short_coverage` policy and `coverage_reason=reference_probe_failed` are the user-visible result; no new
   state, attribute or setting is added.
3. The limit is documented where it starts, not where it was first noticed: the guides (EN and PL) state the
   known-probe counts per horizon and strategy and the remedies that were run; `docs/runtime-validation.md`
   holds the per-probe table; `benchmarks/probes.py` reproduces it; `tests/test_consumption_load_following_limit.py`
   asserts the fallback at 96 and 192 slots and that `cost_min` keeps its probe at 96.
4. Performance numbers in the guides come from a command in `benchmarks/` and name the host. A change that
   moves solve time re-runs that command and updates the table in the same PR.
5. Making the probes fit is a solve-time problem of the model, not of the probe: it is owned by the
   formulation work on the load-following rows (the follow-up to T-010 for the CHARGE_PV rule), and by the
   revisit conditions of ADR-0003.

Revisit when the solver interface offers a warm start together with an absolute gap in currency, when the
load-following rows are reformulated so that a 96-slot `self_sufficiency` solve proves in well under a second,
or when the owner asks for levels with these strategies at the price of an approximation — that is a product
question for the owner, with the measured error, not a default.

## Alternatives rejected

1. Probes with the base plan's modes fixed outside a window (dev rounds 1 and 2) — rejected because the
   restricted candidate is not optimal and the error has no bound: windows of 0, 4 and 16 slots agreed with
   each other while being 0.2-1.4 PLN/kWh from the full solve, so no publish rule based on window width can
   separate right from wrong, and the errors flip levels (NORMAL to LIMIT).
2. A relative MIP gap or an accepted incumbent for the probe — rejected because the difference being published
   (0.09 on the benchmark) is of the size of the distance between successive incumbents (0.05-1.3) and of the
   root-bound gap (0.04): any tolerance loose enough to stop early is as large as the result. ADR-0003,
   decision 4, already forbids a gap on one side of the difference.
3. An objective cutoff or an incumbent taken from a cheaper solve — rejected because the time goes into finding
   the optimum late in the tree and proving it, which a bound from above does not shorten (measured 9-16 s
   against 6-10 s), and `scipy.optimize.milp` cannot take the incumbent anyway.
4. Higher default `probe_time_limit_s` and `total_time_limit_s` — rejected because `self_sufficiency` needs
   both maxima (30 s and 300 s) on the fastest host we have, 199 s of probing per recalculation, and the
   default strategy would pay for it with a longer worst case on every host. The settings stay available and
   the guides state what was measured with them.
5. Probes solved on the model without the load-following rule on both sides — not adopted here: both sides
   would be exact and fast (the rule is what makes these strategies slow), but the published cost would be the
   marginal cost of a plan the controller cannot execute, and its distance from the real one is unmeasured.
   It is the one candidate worth measuring if the owner asks for levels with these strategies (revisit clause).
6. A flag on approximate costs (`approximate: true`) — rejected because the levels drive automations
   (notifications, strategy switching): a LIMIT that should be NORMAL acts the same with or without a flag.

## Consequences

- No engine change in T-027. `self_sufficiency` and `backup_ready` have no usable consumption levels on the
  default 24 h horizon with quarter-hour prices. The flexible energy depth shares the limit and the
  budget: its five solves time out at 2 s for the same two strategies, so the depth is unknown there too
  (measured, same table). The dispatch plan itself is unaffected. The guides say so.
- At 48 h no strategy completes its probes since the CHARGE_PV load-following rule (T-010) made every solve
  4-7 times slower at 192 slots; that regression has its own ticket and is not accepted by this ADR.
- The probe table goes stale with every change to solve time; decision 4 and the test tie it to the code.
- The test costs about a minute of suite time (eight real-solver cases, 180 s timeout each).
