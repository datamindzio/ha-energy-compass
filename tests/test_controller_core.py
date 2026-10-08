import ast
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_compass import controller as c

NOW = datetime(2026, 10, 8, 17, 0, 30, tzinfo=UTC)
SESSION = datetime(2026, 10, 8, 16, 0, 0, tzinfo=UTC).timestamp()
BATTERY = {
    "capacity_kwh": 24.0,
    "eta_charge": 0.97,
    "eta_discharge": 0.96,
    "charge_kw": 8.0,
    "discharge_kw": 7.0,
}
ROW_KEYS = c.ROW_NUMBERS


def iso(hour, minute=0, second=0):
    return datetime(2026, 10, 8, hour, minute, second, tzinfo=UTC).isoformat()


def row(start, end, state="SELF_CONSUME", **extra):
    base = {key: 0.5 for key in ROW_KEYS}
    base.update(start=start, end=end, state=state, balance_hold=False)
    base.update(extra)
    return base


def rows():
    return [
        row(iso(16, 45), iso(17, 0)),
        row(iso(17, 0), iso(17, 15), "CHARGE_PV"),
        row(iso(17, 15), iso(18, 0), "HOLD"),
    ]


def publication(**override):
    data = {
        "status": "ready",
        "valid": True,
        "generated_at": iso(17, 0),
        "valid_until": iso(19, 0),
        "intervals": rows(),
        "dispatch_policy": {"limit_grid_charge_price": 0.6},
        "refreshing": False,
        "plan_retained": False,
        "alert": None,
        "controller_parameters": dict(BATTERY),
    }
    data.update(override)
    return data


def accept(data=None, *, accepted=None, revoked=None, session=SESSION, now=NOW):
    return c.candidate(
        publication() if data is None else data,
        session=session,
        accepted=accepted,
        revoked=revoked,
        now=now,
        battery={key: 1.0 for key in c.BATTERY_KEYS},
    )


def test_core_has_no_home_assistant_imports():
    tree = ast.parse(Path(c.__file__).read_text())
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    assert not [name for name in modules if name.startswith("homeassistant")]
    assert not [name for name in modules if name.startswith(".")]


def test_baseline_is_accepted_with_the_documented_snapshot_shape():
    snapshot = accept()
    assert list(snapshot) == [
        "schema",
        "session",
        "accepted_at",
        "generated_at",
        "valid_until",
        "coverage_end",
        "intervals",
        "dispatch_policy",
        "battery",
    ]
    assert snapshot["schema"] == 2
    assert snapshot["session"] == SESSION
    assert snapshot["accepted_at"] == NOW.isoformat()
    assert snapshot["generated_at"] == iso(17, 0)
    assert snapshot["valid_until"] == iso(19, 0)
    assert snapshot["coverage_end"] == datetime(2026, 10, 8, 18, tzinfo=UTC).timestamp()
    assert snapshot["intervals"] == rows()
    assert snapshot["dispatch_policy"] == {"limit_grid_charge_price": 0.6}
    assert snapshot["battery"] == BATTERY


def test_battery_falls_back_to_the_passed_parameters():
    data = publication()
    del data["controller_parameters"]
    assert accept(data)["battery"] == {key: 1.0 for key in c.BATTERY_KEYS}
    data["controller_parameters"] = {"capacity_kwh": 24.0}
    assert accept(data)["battery"] == {key: 1.0 for key in c.BATTERY_KEYS}


def with_row(index, **change):
    data = publication()
    data["intervals"][index] = {**data["intervals"][index], **change}
    return data


def with_rows(new_rows):
    return publication(intervals=new_rows)


def broken_rows():
    gap = rows()
    gap[2] = row(iso(17, 20), iso(18, 0), "HOLD")
    return gap


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(with_rows(broken_rows()), id="non-contiguous"),
        pytest.param(with_row(1, end=iso(17, 0)), id="end-equals-start"),
        pytest.param(with_row(1, state="BASE"), id="unknown-state"),
        pytest.param(with_row(1, balance_hold="yes"), id="balance-hold-not-bool"),
        pytest.param(with_row(1, balance_hold=None), id="balance-hold-none"),
        pytest.param(with_row(1, charge_kwh=-0.01), id="negative-energy"),
        pytest.param(with_row(1, pv_kwh="nan"), id="nan-string"),
        pytest.param(with_row(1, pv_kwh=None), id="missing-number"),
        pytest.param(with_row(1, start="nonsense"), id="bad-start"),
        pytest.param(with_rows(rows() + ["row"]), id="row-not-mapping"),
        pytest.param(with_rows([]), id="no-rows"),
        pytest.param(with_rows("rows"), id="rows-string"),
        pytest.param(
            publication(intervals=[row(iso(16, 0), iso(16, 30))]), id="not-covered"
        ),
        pytest.param(publication(dispatch_policy=None), id="no-policy"),
        pytest.param(publication(generated_at=iso(15, 59)), id="before-session"),
        pytest.param(publication(generated_at=iso(17, 5)), id="future"),
        pytest.param(publication(valid=False), id="not-valid"),
        pytest.param(publication(valid_until=iso(17, 0, 30)), id="valid-until-now"),
        pytest.param(publication(valid_until=iso(16, 59)), id="valid-until-past"),
        pytest.param(publication(alert={"code": "x"}), id="alert"),
        pytest.param(publication(refreshing=True), id="ready-refreshing"),
        pytest.param(publication(plan_retained=True), id="ready-retained"),
        pytest.param(
            publication(status="calculating", refreshing=True, plan_retained=False),
            id="calculating-not-retained",
        ),
        pytest.param(
            publication(status="calculating", refreshing=False, plan_retained=True),
            id="calculating-not-refreshing",
        ),
        pytest.param(publication(status="invalid_input"), id="invalid-input"),
        pytest.param(publication(status="error"), id="error"),
    ],
)
def test_candidate_rejects_each_broken_rule(data):
    assert accept(data) is None


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(with_row(1, buy_per_kwh=-0.5), id="negative-buy-price"),
        pytest.param(with_row(1, sell_per_kwh=-0.5), id="negative-sell-price"),
        pytest.param(with_row(1, pv_kwh="1.5"), id="numeric-string"),
        pytest.param(with_row(1, pv_kwh=True), id="bool-number"),
        pytest.param(with_row(1, charge_kwh=-0.0000001), id="tiny-negative"),
        pytest.param(
            {k: v for k, v in publication().items() if k != "refreshing"},
            id="refreshing-missing",
        ),
        pytest.param(
            {k: v for k, v in publication().items() if k != "plan_retained"},
            id="retained-missing",
        ),
        pytest.param(
            {k: v for k, v in publication().items() if k != "balance_hold"}
            | {
                "intervals": [
                    {k: v for k, v in r.items() if k != "balance_hold"} for r in rows()
                ]
            },
            id="balance-hold-missing",
        ),
        pytest.param(
            publication(status="calculating", refreshing=True, plan_retained=True),
            id="retained-while-calculating",
        ),
        pytest.param(publication(generated_at=NOW.isoformat()), id="generated-now"),
        pytest.param(
            publication(generated_at=datetime.fromtimestamp(SESSION, UTC).isoformat()),
            id="generated-at-session",
        ),
    ],
)
def test_candidate_accepts_the_lenient_cases(data):
    assert accept(data) is not None


def test_candidate_requires_a_newer_generation_than_the_accepted_one():
    accepted = accept()
    assert accept(accepted=accepted) is None
    older = publication(generated_at=iso(16, 59))
    assert accept(older, accepted=accepted) is None
    newer = publication(generated_at=iso(17, 0, 10))
    assert accept(newer, accepted=accepted)["generated_at"] == iso(17, 0, 10)


def test_candidate_respects_the_revocation():
    same = c.Revocation(iso(17, 0), iso(16, 30), "error: x")
    assert accept(revoked=same) is None
    later = c.Revocation(iso(16, 50), iso(17, 0), "error: x")
    assert accept(revoked=later) is None
    equal = c.Revocation(iso(16, 50), iso(17, 0), "error: x")
    assert accept(publication(generated_at=iso(17, 0)), revoked=equal) is None
    before = c.Revocation(iso(16, 50), iso(16, 59), "error: x")
    assert accept(revoked=before) is not None


def test_candidate_can_be_a_retained_generation_while_calculating():
    retained = publication(status="calculating", refreshing=True, plan_retained=True)
    assert accept(retained) is not None


def test_revoke_error_publication_stamps_the_accepted_generation():
    accepted = accept()
    data = publication(status="error", reason="boom")
    revocation = c.revoke(data, accepted=accepted, revoked=None, now=NOW)
    assert revocation == c.Revocation(iso(17, 0), NOW.isoformat(), "error: boom")


def test_revoke_keeps_the_first_time_per_generation():
    accepted = accept()
    first = c.revoke(
        publication(status="error", reason="boom"),
        accepted=accepted,
        revoked=None,
        now=NOW,
    )
    second = c.revoke(
        publication(status="invalid_input", reason="other"),
        accepted=accepted,
        revoked=first,
        now=NOW + timedelta(minutes=5),
    )
    assert second.at == first.at
    assert second.reason == "invalid_input: other"
    renewed = accept(publication(generated_at=iso(17, 0, 20)), accepted=accepted)
    third = c.revoke(
        publication(status="error", reason="boom"),
        accepted=renewed,
        revoked=second,
        now=NOW + timedelta(minutes=6),
    )
    assert third.generation == iso(17, 0, 20)
    assert third.at == (NOW + timedelta(minutes=6)).isoformat()


def test_revoke_without_an_accepted_plan_keeps_its_time():
    first = c.revoke(
        publication(status="error", reason="x"), accepted=None, revoked=None, now=NOW
    )
    assert first.generation is None
    second = c.revoke(
        publication(status="error", reason="x"),
        accepted=None,
        revoked=first,
        now=NOW + timedelta(minutes=1),
    )
    assert second.at == first.at


def test_revoke_ignores_calculating_and_ready_without_alert():
    assert (
        c.revoke(
            publication(status="calculating"), accepted=None, revoked=None, now=NOW
        )
        is None
    )
    assert c.revoke(publication(), accepted=None, revoked=None, now=NOW) is None
    soc = {"status": "calculating", "valid": False}
    assert c.revoke(soc, accepted=None, revoked=None, now=NOW) is None


def test_revoke_on_an_alert_even_when_the_status_is_ready():
    data = publication(alert={"code": "x", "reason": "soc jump"})
    revocation = c.revoke(data, accepted=accept(), revoked=None, now=NOW)
    assert revocation.reason == "ready: soc jump"


def reason(data, accepted, revoked=None, session=SESSION, now=NOW):
    return c.plan_reason(
        data, session=session, accepted=accepted, revoked=revoked, now=now
    )


def test_plan_reason_codes():
    accepted = accept()
    assert reason(publication(), accepted) == "ok"
    assert reason(publication(), None) == "session"
    assert reason(publication(), accepted, session=SESSION + 1) == "session"
    revoked = c.Revocation(accepted["generated_at"], iso(17, 0, 20), "x")
    assert reason(publication(), accepted, revoked) == "revoked"
    assert reason(publication(status="error"), accepted) == "sources"
    assert reason(publication(alert={"code": "x"}), accepted) == "sources"
    assert reason(publication(valid=False), accepted) == "sources"
    assert reason(publication(generated_at=iso(17, 0, 10)), accepted) == "sources"
    assert reason(publication(plan_retained=True), accepted) == "sources"
    assert reason(publication(refreshing=True), accepted) == "sources"


def test_plan_reason_precedence_is_session_then_revoked_then_sources():
    accepted = accept()
    revoked = c.Revocation(accepted["generated_at"], iso(17, 0, 20), "x")
    broken = publication(status="error")
    assert reason(broken, accepted, revoked) == "revoked"
    assert reason(broken, accepted, revoked, session=SESSION + 1) == "session"
    assert reason(broken, accepted) == "sources"


def test_plan_reason_is_ok_for_a_reload_without_a_plan():
    accepted = accept()
    reloaded = {"status": "calculating", "valid": False}
    assert reason(reloaded, accepted) == "ok"
    with_alert = {"status": "calculating", "valid": False, "alert": {"code": "x"}}
    assert reason(with_alert, accepted) == "sources"


def test_a_revocation_of_another_generation_does_not_block_the_accepted_one():
    accepted = accept()
    revoked = c.Revocation(iso(16, 0), iso(16, 59), "x")
    assert reason(publication(), accepted, revoked) == "ok"


def fresh_state(**kwargs):
    return c.ControllerState(mode="Auto", restore_pending=True, **kwargs)


def advance(state, data, now=NOW):
    return c.step(
        state,
        data,
        session=SESSION,
        now=now,
        fallback_battery={key: 1.0 for key in c.BATTERY_KEYS},
    )


def test_step_accepts_and_remembers():
    state = advance(fresh_state(), publication())
    assert state.accepted["generated_at"] == iso(17, 0)
    assert [event.kind for event in state.history] == ["accepted"]
    assert state.history[0].generation == iso(17, 0)
    assert state.mode == "Auto" and state.restore_pending is True


def test_step_is_idempotent_for_the_same_publication():
    state = advance(fresh_state(), publication())
    assert advance(state, publication()) is state


def test_step_revokes_before_accepting():
    accepted_state = advance(fresh_state(), publication())
    error = publication(status="error", reason="boom", generated_at=iso(17, 0, 20))
    state = advance(accepted_state, error, NOW + timedelta(seconds=30))
    assert state.accepted == accepted_state.accepted
    assert state.revoked.generation == iso(17, 0)
    assert [event.kind for event in state.history] == ["accepted", "revoked"]
    again = advance(state, error, NOW + timedelta(seconds=60))
    assert again.revoked == state.revoked
    assert [event.kind for event in again.history] == ["accepted", "revoked"]


def test_step_does_not_accept_a_plan_older_than_the_revocation():
    accepted_state = advance(fresh_state(), publication())
    state = advance(
        accepted_state,
        publication(status="error", reason="x"),
        NOW + timedelta(seconds=40),
    )
    stale = publication(generated_at=iso(17, 0, 20))
    assert advance(state, stale, NOW + timedelta(seconds=50)) == state


def test_history_is_capped():
    state = fresh_state()
    for index in range(c.HISTORY_LIMIT + 10):
        generated = datetime(2026, 10, 8, 17, 0, 1, tzinfo=UTC) + timedelta(
            milliseconds=index
        )
        moment = NOW + timedelta(seconds=1, milliseconds=index)
        state = advance(state, publication(generated_at=generated.isoformat()), moment)
    assert len(state.history) == c.HISTORY_LIMIT


def test_state_round_trip_through_json():
    state = advance(fresh_state(), publication())
    state = advance(state, publication(status="error", reason="x"), NOW)
    raw = json.loads(json.dumps(state.to_dict()))
    loaded, valid = c.load_state(raw, session=SESSION, now=NOW)
    assert valid
    assert loaded.to_dict() == state.to_dict()
    assert loaded.revoked == state.revoked


def test_load_state_without_a_document_is_the_empty_default():
    assert c.load_state(None, session=SESSION, now=NOW) == (c.ControllerState(), True)


def test_load_state_drops_a_snapshot_of_another_session():
    state = advance(fresh_state(), publication())
    raw = json.loads(json.dumps(state.to_dict()))
    loaded, valid = c.load_state(raw, session=SESSION + 60, now=NOW)
    assert valid and loaded.accepted is None
    assert [event.kind for event in loaded.history] == ["accepted", "dropped"]
    assert loaded.mode == "Auto" and loaded.restore_pending is True


@pytest.mark.parametrize(
    "raw",
    [
        "garbage",
        {"mode": "Dance", "restore_pending": False},
        {"mode": "Auto", "restore_pending": "yes"},
        {"mode": "Auto", "restore_pending": False, "accepted": {"schema": 1}},
        {"mode": "Auto", "restore_pending": False, "runtime": {"revoked_at": "x"}},
        {"mode": "Auto", "restore_pending": False, "revoked": {"at": 1}},
        {"mode": "Auto", "restore_pending": False, "history": ["x"]},
        {"mode": "Auto", "restore_pending": False, "history": [{"at": "x"}]},
    ],
)
def test_load_state_garbage_is_the_safe_default(raw):
    assert c.load_state(raw, session=SESSION, now=NOW) == (
        c.ControllerState(mode="Off", restore_pending=True),
        False,
    )


def test_validate_runtime_accepts_every_allowed_key():
    runtime = {key: 1 for key in c.RUNTIME_KEYS}
    assert c.validate_runtime(runtime) == runtime
    assert c.validate_runtime({}) == {}


def test_runtime_allow_list_is_exact():
    assert c.RUNTIME_KEYS == {
        "accepted_generation",
        "original_deadline",
        "state",
        "desired",
        "requested_mode",
        "active_tou",
        "retained",
        "code",
        "reason",
        "warning",
        "takeover_blocked",
        "since",
        "battery_mode_commissioned",
        "reached_key",
        "slot_energy",
        "owned_session",
        "uncertain",
        "confirmed",
        "last_confirmation",
        "confirmed_mode",
    }


@pytest.mark.parametrize(
    "key", ["revoked_at", "revoked_generation", "grid_current_limit_commissioned"]
)
def test_validate_runtime_rejects_other_keys(key):
    with pytest.raises(c.RuntimeInvalid) as raised:
        c.validate_runtime({"code": "ok", key: 1})
    assert (raised.value.code, raised.value.key) == ("runtime_invalid_key", key)


def test_validate_runtime_rejects_non_objects_and_non_json():
    for bad in ("x", [1], None, 3):
        with pytest.raises(c.RuntimeInvalid) as raised:
            c.validate_runtime(bad)
        assert raised.value.code == "runtime_invalid"
    with pytest.raises(c.RuntimeInvalid):
        c.validate_runtime({"reason": object()})


def test_validate_runtime_size_limit():
    overhead = len(json.dumps({"reason": ""}).encode())
    exact = {"reason": "x" * (c.RUNTIME_MAX_BYTES - overhead)}
    assert c.validate_runtime(exact) == exact
    with pytest.raises(c.RuntimeInvalid) as raised:
        c.validate_runtime({"reason": "x" * (c.RUNTIME_MAX_BYTES - overhead + 1)})
    assert raised.value.code == "runtime_too_large"


def test_runtime_summary_lists_the_recorded_attributes():
    summary = c.runtime_summary({"code": "ok", "reason": "r", "state": "HOLD"}, True)
    assert summary == {
        "reason": "r",
        "state": "HOLD",
        "requested_mode": None,
        "confirmed_mode": None,
        "since": None,
        "warning": None,
        "takeover_blocked": None,
        "accepted_generation": None,
        "restore_pending": True,
    }


WARSAW = ZoneInfo("Europe/Warsaw")
PROGRAMS = ["00:00:00", "06:00:00", "13:00:00", "15:00:00", "22:00:00", "23:00:00"]


def test_next_tou_picks_the_next_program_boundary():
    now = datetime(2026, 10, 8, 12, 59, tzinfo=WARSAW)
    assert c.next_tou(PROGRAMS, now) == datetime(2026, 10, 8, 13, 0, tzinfo=WARSAW)


def test_next_tou_rolls_over_to_the_next_day():
    now = datetime(2026, 10, 8, 23, 30, tzinfo=WARSAW)
    assert c.next_tou(PROGRAMS, now) == datetime(2026, 10, 9, 0, 0, tzinfo=WARSAW)


def test_next_tou_boundary_itself_moves_on():
    now = datetime(2026, 10, 8, 13, 0, tzinfo=WARSAW)
    assert c.next_tou(PROGRAMS, now) == datetime(2026, 10, 8, 15, 0, tzinfo=WARSAW)


def test_next_tou_skips_unusable_entries():
    now = datetime(2026, 10, 8, 12, 59, tzinfo=WARSAW)
    times = ["unavailable", "unknown", None, "garbage", "13:30:00"]
    assert c.next_tou(times, now) == datetime(2026, 10, 8, 13, 30, tzinfo=WARSAW)
    assert c.next_tou(["unavailable"], now) is None
    assert c.next_tou([], now) is None


@pytest.mark.parametrize(
    "now_local, expected",
    [
        (
            datetime(2026, 3, 29, 1, 30, tzinfo=WARSAW),
            datetime(2026, 3, 29, 6, 0, tzinfo=WARSAW),
        ),
        (
            datetime(2026, 3, 28, 23, 30, tzinfo=WARSAW),
            datetime(2026, 3, 29, 0, 0, tzinfo=WARSAW),
        ),
        (
            datetime(2026, 10, 25, 5, 30, tzinfo=WARSAW),
            datetime(2026, 10, 25, 6, 0, tzinfo=WARSAW),
        ),
        (
            datetime(2026, 10, 24, 23, 30, tzinfo=WARSAW),
            datetime(2026, 10, 25, 0, 0, tzinfo=WARSAW),
        ),
    ],
)
def test_next_tou_on_dst_days_is_wall_clock(now_local, expected):
    result = c.next_tou(PROGRAMS, now_local)
    assert result == expected
    assert result.utcoffset() == expected.utcoffset()


def test_next_tou_tomorrow_across_the_dst_change_keeps_wall_clock():
    now = datetime(2026, 3, 28, 23, 30, tzinfo=WARSAW)
    result = c.next_tou(["00:00:00"], now)
    assert result.hour == 0 and result.day == 29
    assert result.astimezone(UTC) == datetime(2026, 3, 28, 23, 0, tzinfo=UTC)


def test_next_event_is_the_earliest_future_instant():
    snapshot = accept()
    now = NOW
    assert c.next_event(snapshot, None, now) == datetime(
        2026, 10, 8, 17, 15, tzinfo=UTC
    )
    tou = datetime(2026, 10, 8, 17, 5, tzinfo=UTC)
    assert c.next_event(snapshot, tou, now) == tou
    late = NOW.replace(hour=17, minute=59)
    assert c.next_event(snapshot, None, late) == datetime(2026, 10, 8, 18, tzinfo=UTC)
    after = datetime(2026, 10, 8, 18, 0, 1, tzinfo=UTC)
    assert c.next_event(snapshot, None, after) == datetime(2026, 10, 8, 19, tzinfo=UTC)


def test_next_event_is_strictly_after_now_and_none_when_empty():
    snapshot = accept()
    on_boundary = datetime(2026, 10, 8, 17, 15, tzinfo=UTC)
    assert c.next_event(snapshot, None, on_boundary) == datetime(
        2026, 10, 8, 18, tzinfo=UTC
    )
    assert c.next_event(None, None, NOW) is None
    assert c.next_event(snapshot, None, datetime(2026, 10, 9, tzinfo=UTC)) is None


def test_next_event_uses_the_coverage_end_when_it_is_the_earliest():
    snapshot = deepcopy(accept())
    snapshot["valid_until"] = iso(20)
    snapshot["intervals"] = []
    snapshot["coverage_end"] = datetime(2026, 10, 8, 17, 30, tzinfo=UTC).timestamp()
    assert c.next_event(snapshot, None, NOW) == datetime(
        2026, 10, 8, 17, 30, tzinfo=UTC
    )


def tou_facts(prefix="inverter_deye_program_"):
    facts = []
    for number in range(1, 7):
        for name, domain in c.TOU_FIELDS:
            facts.append(
                c.TouFact(
                    f"{domain}.{prefix}{number}_{name}",
                    domain,
                    "solarman",
                    f"program_{number}_{name}",
                    False,
                )
            )
    return facts


def test_resolve_tou_finds_the_prefix_and_the_thirty_entities():
    resolution = c.resolve_tou(tou_facts())
    assert resolution.prefix == "inverter_deye_program_"
    assert resolution.problem is None
    assert len(resolution.entities) == 30
    assert resolution.entities[0] == "time.inverter_deye_program_1_time"
    assert resolution.entities[-1] == "select.inverter_deye_program_6_charging"


def test_resolve_tou_incomplete_when_an_entity_is_missing_or_disabled():
    facts = [fact for fact in tou_facts() if fact.translation_key != "program_4_soc"]
    assert c.resolve_tou(facts) == c.TouResolution(None, (), "tou_incomplete")
    disabled = tou_facts()
    disabled[3] = c.TouFact(
        disabled[3].entity_id, disabled[3].domain, "solarman", "program_1_soc", True
    )
    assert c.resolve_tou(disabled).problem == "tou_incomplete"
    assert c.resolve_tou([]).problem == "tou_incomplete"


def test_resolve_tou_incomplete_for_a_duplicate_program_entity():
    facts = tou_facts()
    facts.append(c.TouFact("time.second", "time", "solarman", "program_1_time", False))
    assert c.resolve_tou(facts).problem == "tou_incomplete"


def test_resolve_tou_prefix_problem_when_one_entity_was_renamed():
    facts = tou_facts()
    facts[7] = c.TouFact(
        "number.custom_name", "number", "solarman", facts[7].translation_key, False
    )
    assert c.resolve_tou(facts) == c.TouResolution(None, (), "tou_prefix")


def test_resolve_tou_ignores_other_platforms():
    noise = [
        c.TouFact("time.other_1_time", "time", "other", "program_1_time", False),
        c.TouFact("sensor.x", "sensor", None, None, False),
    ]
    assert c.resolve_tou(noise + tou_facts()).problem is None
    assert c.resolve_tou(noise).problem == "tou_incomplete"


def test_resolve_tou_requires_the_expected_domain():
    facts = tou_facts()
    facts[0] = c.TouFact(
        "number.inverter_deye_program_1_time",
        "number",
        "solarman",
        "program_1_time",
        False,
    )
    assert c.resolve_tou(facts).problem == "tou_incomplete"


def package_runtime(**extra):
    runtime = {
        "code": "ok",
        "state": "CHARGE_PV",
        "owned_session": SESSION,
        "confirmed": {"number.x": 1.0},
        "uncertain": [],
        "revoked_generation": iso(16, 30),
        "revoked_at": iso(16, 31),
        "revoked_reason": "Błąd obliczeń lub źródeł: wymagany nowy plan",
        "conservative_grid_charge": True,
        "grid_current_limit_commissioned": 10,
    }
    runtime.update(extra)
    return runtime


def package_snapshot(**extra):
    snapshot = {
        "schema": 1,
        "session": SESSION,
        "accepted_at": iso(17, 0, 1),
        "generated_at": iso(17, 0),
        "valid_until": iso(19, 0),
        "coverage_end": 123.0,
        "intervals": rows(),
        "dispatch_policy": {"a": 1},
    }
    snapshot.update(extra)
    return snapshot


def package_facts(**override):
    base = {
        "mode": "Auto",
        "session": "on",
        "session_start": SESSION,
        "restore_pending": "on",
        "snapshot": package_snapshot(),
        "runtime": package_runtime(),
    }
    base.update(override)
    return c.PackageFacts(**base)


def do_import(facts):
    return c.import_package(facts, battery=BATTERY, now=NOW)


def test_import_package_maps_every_role():
    result = do_import(package_facts())
    state = result.state
    assert (state.mode, state.restore_pending) == ("Auto", True)
    assert result.session == SESSION
    assert result.snapshot_kept is True
    assert state.accepted["schema"] == 2
    assert state.accepted["battery"] == BATTERY
    assert state.accepted["generated_at"] == iso(17, 0)
    assert state.accepted["intervals"] == rows()
    assert state.revoked == c.Revocation(
        iso(16, 30), iso(16, 31), "Błąd obliczeń lub źródeł: wymagany nowy plan"
    )
    assert state.runtime["owned_session"] == SESSION
    assert state.runtime["confirmed"] == {"number.x": 1.0}
    assert not {"revoked_at", "conservative_grid_charge"} & set(state.runtime)
    assert result.dropped_keys == (
        "conservative_grid_charge",
        "grid_current_limit_commissioned",
    )
    assert state.imported_at == NOW.isoformat()
    assert [event.kind for event in state.history] == ["imported"]


def test_import_package_off_restore_pending():
    assert (
        do_import(package_facts(restore_pending="off")).state.restore_pending is False
    )


@pytest.mark.parametrize(
    "role, value",
    [
        ("mode", None),
        ("session", None),
        ("session_start", None),
        ("restore_pending", None),
        ("snapshot", None),
        ("runtime", None),
    ],
)
def test_import_package_reports_a_missing_role(role, value):
    with pytest.raises(c.PackageError) as raised:
        do_import(package_facts(**{role: value}))
    assert (raised.value.code, raised.value.role) == ("package_missing", role)


@pytest.mark.parametrize(
    "overrides, code, role",
    [
        ({"session": "off"}, "package_session_inactive", "session"),
        ({"mode": "Dance"}, "package_mode_invalid", "mode"),
        ({"session_start": 0.0}, "package_missing", "session_start"),
        ({"session_start": float("nan")}, "package_missing", "session_start"),
        ({"restore_pending": "unknown"}, "package_missing", "restore_pending"),
    ],
)
def test_import_package_refusals(overrides, code, role):
    with pytest.raises(c.PackageError) as raised:
        do_import(package_facts(**overrides))
    assert (raised.value.code, raised.value.role) == (code, role)


@pytest.mark.parametrize(
    "snapshot",
    [
        package_snapshot(session=SESSION - 60),
        package_snapshot(schema=2),
        package_snapshot(intervals=[]),
        {},
    ],
)
def test_import_package_drops_a_snapshot_that_does_not_belong_to_the_session(snapshot):
    result = do_import(package_facts(snapshot=snapshot))
    assert result.snapshot_kept is False
    assert result.state.accepted is None


def test_import_package_without_revocation():
    runtime = package_runtime()
    for key in ("revoked_generation", "revoked_at", "revoked_reason"):
        del runtime[key]
    assert do_import(package_facts(runtime=runtime)).state.revoked is None


def test_controller_options_change():
    off = {"enabled": False, "device_id": None}
    on = {"enabled": True, "device_id": "a"}
    assert c.controller_options_change(None, None) == "none"
    assert c.controller_options_change(None, off) == "none"
    assert c.controller_options_change(off, on) == "reload"
    assert c.controller_options_change(None, on) == "reload"
    assert c.controller_options_change(on, off) == "reload"
    assert c.controller_options_change(on, on) == "none"
    assert c.controller_options_change(on, {**on, "device_id": "b"}) == "live"
    assert c.controller_options_change(off, {**off, "device_id": "b"}) == "none"


def test_controller_attributes_follow_the_documented_order():
    state = advance(fresh_state(), publication())
    attrs = c.controller_attributes(
        state,
        publication(),
        session=SESSION,
        now=NOW,
        mode_entity="select.deye_mode",
        fallback_battery={key: 1.0 for key in c.BATTERY_KEYS},
        tou={"time.p": "00:00:00"},
        device_id="dev",
        program_prefix="p_",
        tou_problem=None,
        next_tou_at=datetime(2026, 10, 8, 18, tzinfo=UTC),
    )
    assert tuple(attrs) == c.CONTROLLER_ATTRIBUTES
    assert attrs["controller_schema"] == 1
    assert attrs["plan_reason"] == "ok"
    assert attrs["retained"] is False
    assert attrs["generation"] == iso(17, 0)
    assert attrs["capacity_kwh"] == 24.0
    assert attrs["eta_discharge"] == 0.96
    assert attrs["mode_entity"] == "select.deye_mode"
    assert attrs["next_tou"] == datetime(2026, 10, 8, 18, tzinfo=UTC).isoformat()
    assert attrs["accepted"]["battery"] == BATTERY
    assert attrs["revoked_at"] is None
    empty = c.controller_attributes(
        c.ControllerState(),
        {"status": "calculating", "valid": False},
        session=SESSION,
        now=NOW,
        mode_entity=None,
        fallback_battery={key: 2.0 for key in c.BATTERY_KEYS},
        tou={},
        device_id=None,
        program_prefix=None,
        tou_problem="tou_incomplete",
        next_tou_at=None,
    )
    assert empty["plan_reason"] == "session"
    assert empty["retained"] is True
    assert empty["accepted"] == {}
    assert empty["capacity_kwh"] == 2.0
    assert empty["generation"] is None
