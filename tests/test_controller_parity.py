"""The integration's acceptance rules against the frozen v0.1.35 blueprint.

Each scenario is rendered twice: as the four helper entities the v0.1.35 blueprint read
(plan, optimizer status, validity, alert plus its cache and runtime) through the
blueprint's own templates, and as one coordinator publication through
`controller.py`. Both must agree on acceptance, revocation and the plan reason.
"""

import copy
import hashlib
import itertools
from datetime import UTC, datetime

import pytest
from deye_oracle import (
    CACHE,
    GOLDEN,
    GOLDEN_SHA256,
    RT,
    SESSION_START,
    A,
    F,
    Harness,
    O,
    P,
    timestamp,
)

from custom_components.energy_compass import controller as c

NOW = datetime(2026, 10, 8, 17, 0, 30, tzinfo=UTC)
SESSION = datetime(2026, 10, 8, 16, 0, 0, tzinfo=UTC).timestamp()
LATE_SESSION = datetime(2026, 10, 8, 17, 0, 5, tzinfo=UTC).timestamp()
ACCEPTED_GENERATION = "2026-10-08T17:00:00+00:00"
BATTERY = {key: 1.0 for key in c.BATTERY_KEYS}
GENERATIONS = {
    "older": "2026-10-08T16:55:00+00:00",
    "equal": ACCEPTED_GENERATION,
    "newer": "2026-10-08T17:00:10+00:00",
}
OTHER_GENERATION = "2026-10-08T16:30:00+00:00"
ALERT = {"code": "invalid_input", "status": "invalid_input", "reason": "r"}


def iso(hour, minute=0, second=0):
    return datetime(2026, 10, 8, hour, minute, second, tzinfo=UTC).isoformat()


def good_rows():
    def row(start, end, state):
        values = {key: 0.5 for key in c.ROW_NUMBERS}
        values.update(start=start, end=end, state=state, balance_hold=False)
        return values

    return [
        row(iso(16, 45), iso(17, 0), "SELF_CONSUME"),
        row(iso(17, 0), iso(17, 15), "CHARGE_PV"),
        row(iso(17, 15), iso(18, 0), "HOLD"),
    ]


def publication(generation="equal", **override):
    data = {
        "status": "ready",
        "valid": True,
        "generated_at": GENERATIONS.get(generation, generation),
        "valid_until": iso(19),
        "intervals": good_rows(),
        "dispatch_policy": {"limit_grid_charge_price": 0.6},
        "refreshing": False,
        "plan_retained": False,
        "alert": None,
        "reason": "r",
    }
    data.update(override)
    return data


def snapshot_v1(session):
    return {
        "schema": 1,
        "session": session,
        "accepted_at": iso(17, 0, 1),
        "generated_at": ACCEPTED_GENERATION,
        "valid_until": iso(19),
        "coverage_end": timestamp(iso(18)),
        "intervals": good_rows(),
        "dispatch_policy": {"limit_grid_charge_price": 0.6},
    }


def snapshot_v2(session):
    return {**snapshot_v1(session), "schema": 2, "battery": dict(BATTERY)}


def revocation(kind, data):
    return {
        "none": None,
        "before": c.Revocation(OTHER_GENERATION, iso(16, 30), "x"),
        "after": c.Revocation(OTHER_GENERATION, iso(17, 0, 5), "x"),
        "same_generation": c.Revocation(data.get("generated_at"), iso(16, 30), "x"),
        "accepted_generation": c.Revocation(ACCEPTED_GENERATION, iso(16, 30), "x"),
    }[kind]


_HARNESS = []


def harness():
    """One shared oracle: compiled templates are the slow part, states are reset."""
    if not _HARNESS:
        h = Harness()
        h.now = NOW
        for x in h.data.values():
            x["last_reported"] = NOW.isoformat()
        _HARNESS.append(h)
    return _HARNESS[0]


def oracle_for(data, *, accepted_session, session, revoked):
    """The helper entities v0.1.35 would have held for this publication."""
    h = harness()
    generated = data.get("generated_at")
    h.set(
        P,
        generated or "unknown",
        generated_at=generated,
        valid_until=data.get("valid_until"),
        refreshing=data.get("refreshing", False),
        plan_retained=data.get("plan_retained", False),
        intervals=data.get("intervals", []),
        dispatch_policy=data.get("dispatch_policy"),
    )
    h.set(O, data["status"], generated_at=generated)
    h.set(F, "on" if c.valid_now(data, NOW) else "off", generated_at=generated)
    h.set(A, "on" if data.get("alert") else "off")
    h.set(SESSION_START, str(session), timestamp=session)
    if accepted_session is None:
        h.set(CACHE, "none", snapshot={})
    else:
        h.set(CACHE, ACCEPTED_GENERATION, snapshot=snapshot_v1(accepted_session))
    runtime = {}
    if revoked is not None:
        runtime = {
            "revoked_generation": revoked.generation,
            "revoked_at": revoked.at,
            "revoked_reason": revoked.reason,
        }
    h.set(RT, "ok", runtime=runtime)
    return h


def reason_code(text):
    if text.startswith(("telemetria", "TOU")):
        raise AssertionError(text)
    if text.startswith("sesja"):
        return "session"
    if text.startswith("plan: generacja unieważniona"):
        return "revoked"
    if text.startswith("plan: błąd źródeł"):
        return "sources"
    return "ok"


def compare(data, *, accepted_session, session, revoked):
    """Assert the oracle and the integration agree; return the accepted snapshot."""
    oracle = oracle_for(
        data, accepted_session=accepted_session, session=session, revoked=revoked
    )
    accepted = None if accepted_session is None else snapshot_v2(accepted_session)
    expected = oracle.render(oracle.expression("candidate"))
    actual = c.candidate(
        data,
        session=session,
        accepted=accepted,
        revoked=revoked,
        now=NOW,
        battery=BATTERY,
    )
    assert bool(expected) == bool(actual), (expected, actual)
    if actual:
        for key in (
            "generated_at",
            "valid_until",
            "coverage_end",
            "intervals",
            "dispatch_policy",
            "session",
            "accepted_at",
        ):
            assert expected[key] == actual[key], key
        assert expected["schema"] == 1 and actual["schema"] == 2
        assert actual["battery"] == BATTERY

    condition, update = oracle.revoke_block()
    old_revokes = oracle.render(condition, trigger={"id": "change"})
    new_revoke = c.revoke(data, accepted=accepted, revoked=revoked, now=NOW)
    assert bool(old_revokes) == (new_revoke is not None)
    if new_revoke is not None:
        stamped = oracle.render(update)
        assert stamped["revoked_generation"] == new_revoke.generation
        assert timestamp(stamped["revoked_at"]) == timestamp(new_revoke.at)
        if revoked is not None and revoked.generation == new_revoke.generation:
            assert new_revoke.at == revoked.at

    code = c.plan_reason(
        data, session=session, accepted=accepted, revoked=revoked, now=NOW
    )
    assert reason_code(oracle.decision()["reason"]) == code
    return actual


def test_golden_blueprint_is_byte_identical_to_v0_1_35():
    assert hashlib.sha256(GOLDEN.read_bytes()).hexdigest() == GOLDEN_SHA256


STATUSES = ["ready", "calculating", "invalid_input", "error", "timeout"]


@pytest.mark.parametrize("status", STATUSES)
def test_corpus_a_acceptance_revocation_and_reason(status):
    seen = set()
    axes = itertools.product(
        [None, ALERT],
        [True, False],
        [(False, False), (True, True), (True, False), (False, True)],
        GENERATIONS,
        [SESSION, LATE_SESSION, timestamp(ACCEPTED_GENERATION)],
        ["none", "before", "after", "same_generation", "accepted_generation"],
        [None, "match", "mismatch"],
    )
    for alert, valid, (refreshing, retained), generation, session, kind, held in axes:
        data = publication(
            generation,
            status=status,
            alert=alert,
            valid=valid,
            refreshing=refreshing,
            plan_retained=retained,
        )
        accepted_session = {None: None, "match": session, "mismatch": session - 60}[
            held
        ]
        result = compare(
            data,
            accepted_session=accepted_session,
            session=session,
            revoked=revocation(kind, data),
        )
        seen.add(bool(result))
    if status in ("ready", "calculating"):
        assert seen == {True, False}
    else:
        assert seen == {False}


def broken(**row_change):
    data = publication()
    data["intervals"][1] = {**data["intervals"][1], **row_change}
    return data


def gapped():
    data = publication()
    data["intervals"][2] = {
        **data["intervals"][2],
        "start": iso(17, 20),
    }
    return data


def first_row_not_a_mapping():
    data = publication()
    data["intervals"].insert(0, "row")
    return data


CORPUS_B = {
    "good": publication(),
    "non-contiguous": gapped(),
    "end-equals-start": broken(end=iso(17, 0)),
    "end-before-start": broken(end=iso(16, 0)),
    "start-zero": broken(start=0),
    "start-garbage": broken(start="garbage"),
    "unknown-state": broken(state="BASE"),
    "state-missing": broken(state=None),
    "balance-hold-string": broken(balance_hold="yes"),
    "balance-hold-none": broken(balance_hold=None),
    "balance-hold-true": broken(balance_hold=True),
    "negative-charge": broken(charge_kwh=-0.01),
    "tiny-negative-charge": broken(charge_kwh=-0.0000001),
    "negative-buy": broken(buy_per_kwh=-0.5),
    "negative-sell": broken(sell_per_kwh=-0.5),
    "string-number": broken(pv_kwh="1.5"),
    "bool-number": broken(pv_kwh=True),
    "nan-number": broken(pv_kwh="nan"),
    "inf-number": broken(pv_kwh="inf"),
    "none-number": broken(pv_kwh=None),
    "garbage-number": broken(pv_kwh="abc"),
    "row-not-mapping": first_row_not_a_mapping(),
    "empty": publication(intervals=[]),
    "string-rows": publication(intervals="rows"),
    "not-covered": publication(intervals=[good_rows()[0]]),
    "no-policy": publication(dispatch_policy=None),
    "list-policy": publication(dispatch_policy=[]),
    "valid-until-now": publication(valid_until=NOW.isoformat()),
    "valid-until-garbage": publication(valid_until="garbage"),
    "generated-future": publication(generation="2026-10-08T17:01:00+00:00"),
    "generated-at-now": publication(generation=NOW.isoformat()),
}


@pytest.mark.parametrize("name", list(CORPUS_B))
@pytest.mark.parametrize("held", [None, "match"])
def test_corpus_b_row_variants(name, held):
    data = copy.deepcopy(CORPUS_B[name])
    if data["generated_at"] == ACCEPTED_GENERATION and held:
        data["generated_at"] = GENERATIONS["newer"]
    result = compare(
        data,
        accepted_session=SESSION if held else None,
        session=SESSION,
        revoked=None,
    )
    assert bool(result) == (
        name
        in (
            "good",
            "negative-buy",
            "negative-sell",
            "string-number",
            "bool-number",
            "balance-hold-true",
            "tiny-negative-charge",
            "generated-at-now",
        )
    )
