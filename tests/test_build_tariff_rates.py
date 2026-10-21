"""tools/build_tariff_rates.py: canonical snapshot, --check modes and the refusals of DD-4/DD-7."""

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "tariff_rates_src"
_SPEC = importlib.util.spec_from_file_location(
    "build_tariff_rates", ROOT / "tools" / "build_tariff_rates.py"
)
tool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tool)


@pytest.fixture
def source(tmp_path):
    """A copy of the fixture tree a test may edit."""
    target = tmp_path / "src"
    shutil.copytree(FIXTURE, target)
    return target


def edit(path, change):
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def write(source, tmp_path):
    output = tmp_path / "tariff_rates.json"
    assert tool.main(["--source", str(source)], output=output) == 0
    return output


def test_writes_canonical_snapshot(source, tmp_path):
    output = write(source, tmp_path)
    text = output.read_text(encoding="utf-8")
    data = json.loads(text)
    assert text == json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    assert list(data) == ["format", "vatRate", "excise", "operators", "offers"]
    assert data["format"] == 1
    assert data["vatRate"] == 0.23
    assert data["excise"]["netPerKWh"] == 0.005
    assert isinstance(data["excise"]["source"], str)
    assert list(data["operators"]) == ["pge", "tauron"]
    tauron = data["operators"]["tauron"]
    assert tauron["zoneClock"] == "standard"
    assert list(tauron["groups"]) == ["G11", "G13"]
    assert tauron["groups"]["G13"]["catalog"] == "tauron_g13"
    assert tauron["groups"]["G11"]["catalog"] == "g11"
    assert tauron["groups"]["G13"]["bands"] == {
        "peak": "afternoon",
        "mid_peak": "morning",
        "off_peak": "offpeak",
    }
    assert set(tauron["groups"]["G13"]) == {"catalog", "bands", "zones", "charges"}
    offer = data["offers"]["tauron-fixed"]
    assert offer["seller"] == "Tauron Sprzedaż"
    assert offer["termMonths"] == 12
    assert offer["availableUntil"] == "2026-12-31"
    assert offer["groups"]["G13"] == {
        "morning": 0.4668,
        "afternoon": 0.778,
        "offpeak": 0.421,
    }
    assert "audience" not in offer and "contract" not in offer
    assert data["offers"]["pge-ure"]["availableUntil"] is None


def test_check_passes_on_committed_file(capsys):
    assert tool.main(["--check"]) == 0
    assert capsys.readouterr().err == ""


def test_check_fails_on_non_canonical_bytes(source, tmp_path, capsys):
    output = write(source, tmp_path)
    data = json.loads(output.read_text(encoding="utf-8"))
    output.write_text(json.dumps(data), encoding="utf-8")
    assert tool.main(["--check"], output=output) == 1
    assert "not canonical" in capsys.readouterr().err


def test_check_fails_on_invalid_snapshot(source, tmp_path, capsys):
    output = write(source, tmp_path)
    data = json.loads(output.read_text(encoding="utf-8"))
    data["vatRate"] = 23
    output.write_text(tool.canonical(data), encoding="utf-8")
    assert tool.main(["--check"], output=output) == 1
    assert "vatRate" in capsys.readouterr().err


def test_refuses_price_caps(source, tmp_path, capsys):
    edit(
        source / "programs.json",
        lambda d: d.update(priceCaps=[{"until": "2026-12-31"}]),
    )
    output = tmp_path / "out.json"
    assert tool.main(["--source", str(source)], output=output) == 1
    assert "priceCaps" in capsys.readouterr().err
    assert not output.exists()


def test_refuses_local_zone_clock(source, tmp_path, capsys):
    edit(source / "osd" / "pge.json", lambda d: d.update(zoneClock="local"))
    assert tool.main(["--source", str(source)], output=tmp_path / "out.json") == 1
    assert "zoneClock" in capsys.readouterr().err


def test_refuses_unmapped_zone(source, tmp_path, capsys):
    def rename(data):
        zones = data["groups"]["G13"]["zones"]
        next(zone for zone in zones if zone["id"] == "morning")["id"] = "forenoon"

    edit(source / "osd" / "tauron.json", rename)
    output = tmp_path / "out.json"
    assert tool.main(["--source", str(source)], output=output) == 1
    assert "zone" in capsys.readouterr().err
    assert not output.exists()


def test_refuses_swapped_band_map(source, tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(tool.ZONE_BANDS, "G12", {"peak": "night", "off_peak": "day"})
    output = tmp_path / "out.json"
    assert tool.main(["--source", str(source)], output=output) == 1
    assert "swapped" in capsys.readouterr().err
    assert not output.exists()


def test_skips_dynamic_offers(source, tmp_path):
    assert (source / "offers" / "dynamic.json").exists()
    data = json.loads(write(source, tmp_path).read_text(encoding="utf-8"))
    assert list(data["offers"]) == ["pge-ure", "tauron-fixed"]
    assert {offer["kind"] for offer in data["offers"].values()} == {"ure", "fixed"}


def test_refuses_transitional_edge_not_in_capacity(source, tmp_path, capsys):
    def edge(data):
        data["groups"]["G11"]["charges"]["transitionalPerMonth"] = [
            {"maxKWh": 600, "pln": 1.0},
            {"maxKWh": None, "pln": 2.0},
        ]

    edit(source / "osd" / "tauron.json", edge)
    assert tool.main(["--source", str(source)], output=tmp_path / "out.json") == 1
    assert "transitionalPerMonth" in capsys.readouterr().err


def test_check_with_source_detects_drift(source, tmp_path, capsys):
    output = write(source, tmp_path)
    assert tool.main(["--check", "--source", str(source)], output=output) == 0

    def reprice(data):
        data["groups"]["G13"]["charges"]["qualityPerKWh"] = 0.04

    edit(source / "osd" / "tauron.json", reprice)
    before = output.read_bytes()
    assert tool.main(["--check", "--source", str(source)], output=output) == 1
    assert "differs" in capsys.readouterr().err
    assert output.read_bytes() == before
