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
    anchors = recording.yaml["anchors"]
    # The template keeps counting the charge it takes at a confirmed top (about
    # 0.2 kWh) as stored energy after its false 10-11 anchor, until its own
    # correction on 10-12; the core re-anchors at capacity there and sits that
    # far below it. Outside that window the tracking is tight (measured p95
    # 0.068, p99 0.123 kWh, largest 0.545); inside it the error stays in
    # [-0.94, -0.01] (the 10-12 16:00 episode is the -0.94).
    window = (anchors[1]["at"] + 3600, anchors[2]["at"])
    outside = sorted(
        abs(e) for epoch, e in errors if not window[0] <= epoch < window[1]
    )
    assert outside[int(0.95 * len(outside))] < 0.1
    assert outside[int(0.99 * len(outside))] < 0.15
    assert outside[-1] < 0.6
    inside = [e for epoch, e in errors if window[0] <= epoch < window[1]]
    assert all(-1.0 < e < 0.0 for e in inside)
    off = [epoch for epoch, error in errors if abs(error) > 0.4]
    # The template drops its pending energy without a counter change a few
    # times (one 10-12 afternoon episode, one on 10-14); the core does not.
    assert len(off) <= 5
    assert max(abs(e) for _, e in errors) < 1.0


def test_the_core_does_not_follow_the_template_s_false_full_anchor(recording):
    errors = _replay(recording, follow_yaml_anchors=False)
    cut = recording.yaml["anchors"][1]["at"]
    before = [abs(error) for epoch, error in errors if epoch < cut]
    after = [error for epoch, error in errors if cut + 600 < epoch < cut + 3600]
    assert max(before) < 0.2
    # +2.7 kWh the template added on one sample: the core stays 2.7 below it.
    assert all(-3.2 < error < -2.2 for error in after)
