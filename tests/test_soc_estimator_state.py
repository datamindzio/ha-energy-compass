"""Persistence model (DD-5) and the seed from the template estimator (DD-11)."""

import datetime as dt
import json
from dataclasses import replace

import pytest

from custom_components.energy_compass.soc_estimator import (
    Anchor,
    ControllerFacts,
    EstimatorSettings,
    EstimatorState,
    Inputs,
    initial_state,
    seed_from_template,
    state_from_dict,
    state_to_dict,
    step,
)

T0 = dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC)
SETTINGS = EstimatorSettings(
    capacity_kwh=24.0,
    empty_voltage_v=496.0,
    eta_charge=0.9,
    anchor_on_candidates=True,
)
FACTS = ControllerFacts(charge_limit_a=14.0, state="CHARGE_PV")


def _inputs(seconds, charge=1000.0, discharge=500.0, **kwargs):
    base = {
        "power_w": 100.0,
        "voltage_v": 546.0,
        "current_a": -0.2,
        "pv_w": 4000.0,
        "load_w": 800.0,
        "facts": FACTS,
    }
    return Inputs(
        T0 + dt.timedelta(seconds=seconds), charge, discharge, **{**base, **kwargs}
    )


def _worked_state():
    """A state that has been through every kind of event."""
    state = initial_state(SETTINGS, T0, 1000.0, 500.0, 0.8)
    state.last_at = None
    for second in range(0, 660, 60):
        step(state, SETTINGS, _inputs(second))
    step(state, SETTINGS, _inputs(3600, 1002.0, 501.0))
    step(state, SETTINGS, _inputs(3660, 5000.0, 501.0))
    state.measured_capacity_kwh = 19.5
    return state


def test_the_state_round_trips_through_json():
    state = _worked_state()
    assert state.candidates and state.last_full and state.suspect[0] == 5000.0
    raw = json.loads(json.dumps(state_to_dict(state)))
    restored = state_from_dict(raw)
    assert restored == replace(
        state, full_candidate_since=None, full_candidate_logged=False
    )
    assert state_to_dict(restored) == state_to_dict(state)


def test_the_stored_form_is_versioned():
    assert state_to_dict(_worked_state())["version"] == 1
    raw = state_to_dict(_worked_state())
    raw["version"] = 2
    assert state_from_dict(raw) is None


def test_a_restart_breaks_a_hold_instead_of_completing_it():
    state = initial_state(SETTINGS, T0, 1000.0, 500.0, 0.8)
    state.last_at = None
    for second in range(0, 540, 60):
        step(state, SETTINGS, _inputs(second))
    restored = state_from_dict(json.loads(json.dumps(state_to_dict(state))))
    estimate = step(restored, SETTINGS, _inputs(86400))
    assert estimate.events == ()
    assert restored.candidates == []


def test_restored_state_reads_the_counters_again():
    state = initial_state(SETTINGS, T0, 1000.0, 500.0, 0.5)
    restored = state_from_dict(state_to_dict(state))
    estimate = step(restored, SETTINGS, _inputs(7200, 1003.0, 502.0, power_w=None))
    assert estimate.energy_kwh == pytest.approx(12 - 2 + 0.9 * 3)
    assert estimate.events == ()


def test_a_counter_that_went_down_while_stopped_re_anchors_after_restore():
    state = initial_state(SETTINGS, T0, 1000.0, 500.0, 0.5)
    restored = state_from_dict(state_to_dict(state))
    estimate = step(restored, SETTINGS, _inputs(7200, 2.0, 1.0, power_w=None))
    assert estimate.events == ("reset",)
    assert estimate.energy_kwh == pytest.approx(12.0)


def test_a_state_started_from_a_given_soc_is_unanchored():
    state = initial_state(SETTINGS, T0, 1000.0, 500.0, 1.0)
    estimate = step(state, SETTINGS, _inputs(60, power_w=None, facts=None))
    assert estimate.anchor_kind == "initial"
    assert estimate.unanchored_top
    assert estimate.soc_fraction == pytest.approx(0.999)


@pytest.mark.parametrize(
    "damage",
    [
        lambda raw: raw.update(anchor=None),
        lambda raw: raw["anchor"].update(kind="guess"),
        lambda raw: raw["anchor"].update(at="2026-10-01T12:00:00"),
        lambda raw: raw["anchor"].update(energy_kwh=float("nan")),
        lambda raw: raw["anchor"].update(energy_kwh="12"),
        lambda raw: raw["anchor"].update(energy_kwh=True),
        lambda raw: raw.update(last_counters=[1.0]),
        lambda raw: raw.update(suspect="x"),
        lambda raw: raw.update(top_confirmed=1),
        lambda raw: raw.update(candidates={"a": 1}),
        lambda raw: raw.update(candidates=[{"a": object()}]),
        lambda raw: raw.update(candidates=[{"a": 1}] * 50),
        lambda raw: raw.pop("pending_kwh"),
        lambda raw: raw.update(started_at="yesterday"),
        lambda raw: raw.update(seed=["x"]),
    ],
)
def test_a_malformed_store_is_refused(damage):
    raw = json.loads(json.dumps(state_to_dict(_worked_state())))
    damage(raw)
    assert state_from_dict(raw) is None


@pytest.mark.parametrize("raw", [None, [], "state", 3, {}])
def test_anything_else_is_refused(raw):
    assert state_from_dict(raw) is None


TEMPLATE = {
    "anchor_kwh": 21.3,
    "anchor_charge": 6470.8,
    "anchor_discharge": 5836.1,
    "anchor_kind": "corrected",
    "anchor_at": "2026-10-12T19:06:30.505298+00:00",
    "pending_kwh": 0.0154,
    "energy_kwh": 19.9,
    "eta": 0.937,
}


def test_seed_from_the_template_attributes():
    state = seed_from_template(TEMPLATE, now=T0)
    assert state.anchor == Anchor(
        "seed",
        dt.datetime(2026, 10, 12, 19, 6, 30, 505298, tzinfo=dt.UTC),
        21.3,
        6470.8,
        5836.1,
    )
    assert state.pending_kwh == 0.0154
    assert state.seed == {"kind": "corrected", "at": "2026-10-12T19:06:30.505298+00:00"}
    assert state.started_at == T0
    assert state.last_counters is None
    assert not state.top_confirmed


def test_a_seed_gives_the_template_energy_at_the_first_step():
    state = seed_from_template(TEMPLATE, now=T0)
    estimate = step(state, SETTINGS, _inputs(0, 6472.8, 5837.1, power_w=None))
    assert estimate.energy_kwh == pytest.approx(21.3 - 1.0 + 0.9 * 2 + 0.0154)
    assert estimate.anchor_kind == "seed"
    assert state_from_dict(state_to_dict(state)).seed == state.seed


def test_a_template_full_anchor_is_not_trusted_as_a_physical_full():
    state = seed_from_template(
        {**TEMPLATE, "anchor_kind": "full", "anchor_kwh": 24.0}, now=T0
    )
    estimate = step(state, SETTINGS, _inputs(0, 6470.8, 5836.1, power_w=None))
    assert estimate.unanchored_top
    assert estimate.anchor_kind == "seed"


def test_seed_accepts_text_numbers_a_naive_time_and_no_pending():
    attributes = {
        **TEMPLATE,
        "anchor_kwh": "21.3",
        "anchor_at": "2026-10-12T19:06:30",
    }
    del attributes["pending_kwh"]
    state = seed_from_template(attributes, now=T0)
    assert state.anchor.energy_kwh == 21.3
    assert state.anchor.at == dt.datetime(2026, 10, 12, 19, 6, 30, tzinfo=dt.UTC)
    assert state.pending_kwh == 0.0


def test_seed_bounds_the_pending_energy():
    assert (
        seed_from_template({**TEMPLATE, "pending_kwh": 9.0}, now=T0).pending_kwh == 2.0
    )


@pytest.mark.parametrize(
    "damage",
    [
        {"anchor_kwh": None},
        {"anchor_kwh": "n/a"},
        {"anchor_kwh": -1.0},
        {"anchor_charge": float("nan")},
        {"anchor_discharge": True},
        {"anchor_kind": 3},
        {"anchor_at": "unknown"},
        {"anchor_at": None},
        {"pending_kwh": "x"},
    ],
)
def test_an_incomplete_template_gives_no_seed(damage):
    assert seed_from_template({**TEMPLATE, **damage}, now=T0) is None


@pytest.mark.parametrize("missing", ["anchor_kwh", "anchor_charge", "anchor_at"])
def test_a_template_without_anchor_attributes_gives_no_seed(missing):
    attributes = {key: value for key, value in TEMPLATE.items() if key != missing}
    assert seed_from_template(attributes, now=T0) is None
    assert seed_from_template({}, now=T0) is None


def test_the_estimator_state_class_is_the_one_restored():
    assert isinstance(state_from_dict(state_to_dict(_worked_state())), EstimatorState)
