"""Build the bundled tariff rate snapshot: custom_components/energy_compass/sources/tariff_rates.json.

The rates come from the energy-compass data (`packages/data/src`: `osd/*.json`,
`offers/*.json`, `programs.json`). The snapshot is canonical JSON, so a check is a
byte comparison and a yearly refresh is reviewable as a diff.

Usage:
  python tools/build_tariff_rates.py --source <energy-compass>/packages/data/src
      write the snapshot (maintainer, yearly); prints the energy-compass commit
      for the commit message
  python tools/build_tariff_rates.py --check
      CI: the committed file is canonical and passes validate_snapshot
  python tools/build_tariff_rates.py --check --source <path>
      maintainer: regenerate in memory and fail on any byte difference
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.energy_compass.sources import rates
from custom_components.energy_compass.sources.tariffs import tariff_bands

OUTPUT = rates.SNAPSHOT_PATH

# DD-4: the zone each band reads, fixed here and recorded per group in the snapshot.
ZONE_BANDS: dict[str, dict[str, str]] = {
    "G11": {"peak": "all"},
    "G12": {"peak": "day", "off_peak": "night"},
    "G12w": {"peak": "peak", "off_peak": "offpeak"},
    "G13": {"peak": "afternoon", "mid_peak": "morning", "off_peak": "offpeak"},
    "G13active": {
        "peak": "restricted",
        "mid_peak": "remaining",
        "off_peak": "recommended",
    },
}
OFFER_KINDS = ("ure", "fixed")


class BuildError(Exception):
    """The source data cannot be turned into a trustworthy snapshot."""


def canonical(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _group(operator_id: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
    where = f"{operator_id} {name}"
    bands = ZONE_BANDS.get(name)
    if bands is None:
        raise BuildError(f"{where}: no zone to band map for group {name}")
    zones = body["zones"]
    if sorted(zone["id"] for zone in zones) != sorted(bands.values()):
        raise BuildError(f"{where}: zone ids differ from the mapped zones")
    key = rates.catalog_key(operator_id, name)
    if tuple(band for band in ("peak", "mid_peak", "off_peak") if band in bands) != (
        tariff_bands(key)
    ):
        raise BuildError(f"{where}: mapped bands differ from the bands of {key}")
    variable = body["charges"]["variablePerKWh"]
    if "off_peak" in bands and variable[bands["peak"]] < variable[bands["off_peak"]]:
        raise BuildError(
            f"{where}: peak distribution rate is below off_peak (swapped map?)"
        )
    return {
        "catalog": key,
        "bands": dict(bands),
        "zones": zones,
        "charges": body["charges"],
    }


def _operator(path: Path) -> tuple[int, str, dict[str, Any]]:
    source = _read(path)
    operator_id = source["id"]
    if source["zoneClock"] != "standard":
        raise BuildError(
            f"{operator_id}: zoneClock {source['zoneClock']!r} is not standard"
        )
    groups = {
        name: _group(operator_id, name, body) for name, body in source["groups"].items()
    }
    return (
        source["order"],
        operator_id,
        {
            "name": source["name"],
            "tariffYear": source["tariffYear"],
            "validFrom": source["validFrom"],
            "validTo": source["validTo"],
            "asOf": source["asOf"],
            "source": source["source"]["title"],
            "zoneClock": source["zoneClock"],
            "groups": groups,
        },
    )


def _offer(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "seller": source["seller"]["name"],
        "name": source["name"],
        "kind": source["kind"],
        "osdScope": source["osdScope"],
        "groups": {
            group: body["energyPricePerKWh"] for group, body in source["groups"].items()
        },
        "tradeFeePerMonth": source["tradeFeePerMonth"],
        "termMonths": source["contract"].get("termMonths"),
        "availableUntil": source.get("availableUntil"),
        "asOf": source["asOf"],
        "source": source["source"]["title"],
    }


def build(source: Path) -> dict[str, Any]:
    """Snapshot dict from an energy-compass `packages/data/src` tree."""
    programs = _read(source / "programs.json")
    if programs.get("priceCaps"):
        raise BuildError(
            "programs.json has priceCaps: the prefill formula is no longer exact"
        )
    ordered = sorted(
        _operator(path) for path in sorted((source / "osd").glob("*.json"))
    )
    offers = {}
    for path in sorted((source / "offers").glob("*.json")):
        offer = _read(path)
        if offer["kind"] in OFFER_KINDS:
            offers[offer["id"]] = _offer(offer)
    excise = programs["excise"]
    data = {
        "format": rates.SNAPSHOT_FORMAT,
        "vatRate": programs["vatRate"],
        "excise": {
            "netPerKWh": excise["netPerKWh"],
            "asOf": excise["asOf"],
            "source": excise["source"]["title"],
        },
        "operators": {operator_id: body for _, operator_id, body in ordered},
        "offers": offers,
    }
    try:
        rates.validate_snapshot(data)
    except rates.SnapshotError as err:
        raise BuildError(str(err)) from err
    return data


def _commit(source: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError, subprocess.CalledProcessError:
        return "unknown (not a git checkout)"
    return result.stdout.strip()


def check_committed(output: Path) -> list[str]:
    """Problems of the committed file alone (what CI can see)."""
    text = output.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        rates.validate_snapshot(data)
    except (ValueError, KeyError) as err:
        return [f"{output.name} is invalid: {err}"]
    if canonical(data) != text:
        return [f"{output.name} is not canonical JSON; rebuild it"]
    return []


def main(argv: list[str] | None = None, output: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", type=Path, help="energy-compass packages/data/src")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    output = output or OUTPUT
    if not args.check and args.source is None:
        parser.error("--source is required unless --check")
    problems = check_committed(output) if args.check else []
    if args.source is not None:
        try:
            text = canonical(build(args.source))
        except (BuildError, KeyError, OSError, ValueError) as err:
            print(f"build_tariff_rates: {err}", file=sys.stderr)
            return 1
        if not args.check:
            output.write_text(text, encoding="utf-8")
            print(f"wrote {output}")
            print(f"energy-compass commit: {_commit(args.source)}")
            return 0
        if not problems and output.read_text(encoding="utf-8") != text:
            problems.append(
                f"{output.name} differs from the energy-compass data; rebuild it"
            )
    for problem in problems:
        print(f"build_tariff_rates: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
