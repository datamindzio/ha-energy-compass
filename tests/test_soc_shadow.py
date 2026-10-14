"""tools/soc_shadow.py over the recorded template and a replayed estimator."""

import json
import sys
from pathlib import Path

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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import soc_shadow

ESTIMATOR = "sensor.soc_estimator"
TEMPLATE = "sensor.soc_template"


@pytest.fixture(scope="module")
def recording():
    return Recording()


def _template_rows(recording):
    yaml = recording.yaml
    anchors = yaml["anchors"]
    rows = []
    for index in range(recording.count):
        epoch = recording.start + 60 * index
        energy = recording.yaml_energy(epoch)
        if energy is None:
            continue
        anchor = [a for a in anchors if a["at"] <= epoch][-1]
        rows.append(
            {
                "entity_id": TEMPLATE,
                "state": str(round(energy / yaml["full_kwh"] * 100, 2)),
                "last_updated": at(epoch).isoformat(),
                "attributes": {
                    "energy_kwh": energy,
                    "anchor_kind": anchor["kind"],
                    "anchor_at": at(anchor["at"]).isoformat(),
                },
            }
        )
    return rows


def _estimator_rows(recording, *, follow):
    yaml = recording.yaml
    first = yaml["anchors"][0]
    settings = EstimatorSettings(
        capacity_kwh=yaml["full_kwh"], empty_voltage_v=480.0, eta_charge=yaml["eta"]
    )
    state = EstimatorState(
        Anchor(
            first["kind"],
            at(first["at"]),
            first["energy_kwh"],
            first["charge_kwh"],
            first["discharge_kwh"],
        ),
        at(recording.start),
        top_confirmed=True,
    )
    later = list(yaml["anchors"][1:])
    rows = []
    for epoch in recording.event_times():
        while later and later[0]["at"] <= epoch:
            anchor = later.pop(0)
            if follow:
                set_anchor(
                    state,
                    Anchor(
                        anchor["kind"],
                        at(anchor["at"]),
                        anchor["energy_kwh"],
                        anchor["charge_kwh"],
                        anchor["discharge_kwh"],
                    ),
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
        if (epoch - recording.start) % 60 or not estimate.available:
            continue
        rows.append(
            {
                "entity_id": ESTIMATOR,
                "state": str(round(estimate.soc_fraction * 100, 2)),
                "last_updated": at(epoch).isoformat(),
                "attributes": {
                    "anchor_kind": estimate.anchor_kind,
                    "anchor_at": estimate.anchor_at.isoformat(),
                },
            }
        )
    return rows


def _series(rows):
    return [
        (
            soc_shadow.parse_time(row["last_updated"]),
            row["state"],
            row["attributes"],
        )
        for row in rows
    ]


@pytest.fixture(scope="module")
def template(recording):
    return _template_rows(recording)


def test_an_estimator_that_follows_the_template_is_accepted(recording, template):
    report = soc_shadow.compare(
        _series(_estimator_rows(recording, follow=True)),
        _series(template),
        capacity=recording.yaml["full_kwh"],
    )
    assert report["minutes"] > 9000
    assert report["p95_kwh"] <= 0.2
    assert report["share_within_tolerance"] >= 0.95
    assert report["anchors_missing_in_estimator"] == []
    assert report["accepted"] is True


def test_the_false_full_anchor_of_the_template_is_flagged(recording, template):
    report = soc_shadow.compare(
        _series(_estimator_rows(recording, follow=False)),
        _series(template),
        capacity=recording.yaml["full_kwh"],
    )
    assert report["accepted"] is False
    kinds = [kind for _, kind in report["anchors_missing_in_estimator"]]
    assert kinds == ["full"]
    assert report["anchors_missing_in_estimator"][0][0].startswith("2026-10-11T21:00")
    assert report["max_kwh"] > 2.0


def test_the_command_line_reads_a_history_file(recording, template, tmp_path, capsys):
    history = tmp_path / "history.json"
    history.write_text(json.dumps([_estimator_rows(recording, follow=True), template]))
    code = soc_shadow.main(
        [
            "--history",
            str(history),
            "--estimator",
            ESTIMATOR,
            "--template",
            TEMPLATE,
            "--capacity",
            str(recording.yaml["full_kwh"]),
        ]
    )
    assert code == 0
    assert "ACCEPTED" in capsys.readouterr().out


def test_the_command_line_reads_a_dump_directory(recording, template, tmp_path):
    estimator = _estimator_rows(recording, follow=True)[:200]
    for ours, theirs in zip(estimator, template[:200], strict=False):
        stamp = soc_shadow.parse_time(ours["last_updated"]).strftime("%Y%m%dT%H%M%SZ")
        (tmp_path / f"{stamp}.json").write_text(
            json.dumps(
                [
                    {k: ours[k] for k in ("entity_id", "state", "attributes")}
                    | {"attributes": {**ours["attributes"], "capacity_kwh": 24.0}},
                    {k: theirs[k] for k in ("entity_id", "state", "attributes")},
                ]
            )
        )
    code = soc_shadow.main(
        [str(tmp_path), "--estimator", ESTIMATOR, "--template", TEMPLATE]
    )
    assert code == 0


def test_nothing_to_compare_is_exit_three(tmp_path):
    (tmp_path / "history.json").write_text("[]")
    code = soc_shadow.main(
        [
            "--history",
            str(tmp_path / "history.json"),
            "--estimator",
            ESTIMATOR,
            "--template",
            TEMPLATE,
        ]
    )
    assert code == 3
