"""Links Home Assistant shows for the integration and its blueprints."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINTS = sorted((ROOT / "blueprints/automation/energy_compass").glob("*.yaml"))
SOURCE_BASE = (
    "https://github.com/datamindzio/ha-energy-compass/blob/main/"
    "blueprints/automation/energy_compass/"
)


def _source_url(path: Path) -> str | None:
    prefix = "  source_url: "
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def test_manifest_documentation_points_to_pages_guide() -> None:
    manifest = json.loads(
        (ROOT / "custom_components/energy_compass/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        manifest["documentation"] == "https://datamindzio.github.io/ha-energy-compass/"
    )
    assert manifest["issue_tracker"] == (
        "https://github.com/datamindzio/ha-energy-compass/issues"
    )


def test_all_four_blueprints_found() -> None:
    assert {p.name for p in BLUEPRINTS} == {
        "deye_solarman_controller.yaml",
        "export_value_counter.yaml",
        "notifications.yaml",
        "strategy_switch.yaml",
    }


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.name)
def test_blueprint_source_url(path: Path) -> None:
    assert _source_url(path) == SOURCE_BASE + path.name
