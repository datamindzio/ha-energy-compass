"""The core against the recorded template estimator over seven days."""

import pytest
from soc_estimator_support import Recording, at

from custom_components.energy_compass.soc_estimator import (
    Anchor,
    EstimatorSettings,
    EstimatorState,
    Inputs,
    set_anchor,
    step,
)

FALSE_FULL = "2026-10-11 21:00"


@pytest.fixture(scope="module")
def recording():
    return Recording()


def _replay(recording, *, follow_yaml_anchors):
    yaml = recording.yaml
    first = yaml["anchors"][0]
    settings = EstimatorSettings(
        capacity_kwh=yaml["full_kwh"],
        empty_voltage_v=496.0,
        full_min_voltage_v=530.0,
        eta_charge=yaml["eta"],
    )
    state = EstimatorState(
        Anchor(
            "full",
            at(recording.start),
            first["energy_kwh"],
            first["charge_kwh"],
            first["discharge_kwh"],
        ),
        at(recording.start),
        top_confirmed=True,
    )
    later = list(yaml["anchors"][1:])
    errors = []
    for epoch in recording.event_times():
        while later and later[0]["at"] <= epoch:
            anchor = later.pop(0)
            if follow_yaml_anchors:
                set_anchor(
                    state,
                    Anchor(
                        anchor["kind"],
                        at(anchor["at"]),
                        anchor["energy_kwh"],
                        anchor["charge_kwh"],
                        anchor["discharge_kwh"],
                    ),
                    top_confirmed=anchor["kind"] == "full",
                )
        estimate = step(
            state,
            settings,
            Inputs(
                at(epoch),
                recording.counter("charge", epoch),
                recording.counter("discharge", epoch),
                recording.sample("battery_power", epoch),
            ),
        )
        reference = recording.yaml_energy(epoch)
        if (epoch - recording.start) % 60 == 0 and estimate.available and reference:
            errors.append((epoch, estimate.energy_kwh - reference))
    return errors


def test_the_core_tracks_the_template_between_its_anchors(recording):
    errors = _replay(recording, follow_yaml_anchors=True)
    assert len(errors) > 9000
    size = sorted(abs(error) for _, error in errors)
    # The template keeps counting the charge it takes at a confirmed top (about
    # 0.2 kWh on 10-11/12) as stored energy; the core re-anchors at capacity
    # there, so it sits that far below the template afterwards. Everything else
    # is the tight tracking it had before (p95 0.063, p99 0.118 kWh).
    assert size[int(0.95 * len(size))] < 0.25
    assert size[int(0.99 * len(size))] < 0.3
    off = [epoch for epoch, error in errors if abs(error) > 0.4]
    # The template drops its pending energy without a counter change a few
    # times (one 10-12 afternoon episode, one on 10-14); the core does not.
    assert len(off) <= 20
    assert size[-1] < 1.0
    assert all(error < 0.6 for _, error in errors)


def test_the_core_does_not_follow_the_template_s_false_full_anchor(recording):
    errors = _replay(recording, follow_yaml_anchors=False)
    cut = recording.yaml["anchors"][1]["at"]
    before = [abs(error) for epoch, error in errors if epoch < cut]
    after = [error for epoch, error in errors if cut + 600 < epoch < cut + 3600]
    assert max(before) < 0.2
    # +2.7 kWh the template added on one sample: the core stays 2.7 below it.
    assert all(-3.2 < error < -2.2 for error in after)
