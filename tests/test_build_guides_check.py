"""tools/build_guides.py --check: clean tree passes, stale generated files fail, nothing is written."""

import importlib.util
import shutil
from pathlib import Path

import pytest

pytest.importorskip("markdown")

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "build_guides", ROOT / "tools" / "build_guides.py"
)
guides = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guides)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A copy of the docs the script reads and writes, so a test may edit it."""
    docs = tmp_path / "docs"
    shutil.copytree(ROOT / "docs", docs)
    monkeypatch.setattr(guides, "ROOT", tmp_path)
    monkeypatch.setattr(guides, "DOCS", docs)
    monkeypatch.setattr(guides, "DIAGRAMS", docs / "assets" / "diagrams")
    return docs


def snapshot(docs):
    return {p: p.read_bytes() for p in docs.rglob("*") if p.is_file()}


def test_clean_tree_passes(tree, capsys):
    assert guides.main(["--check"]) == 0
    assert capsys.readouterr().err == ""


def test_edited_markdown_without_rebuild_fails_and_writes_nothing(tree, capsys):
    source = tree / "guide.en.md"
    text = source.read_text(encoding="utf-8")
    assert "Energy" in text
    source.write_text(text.replace("Energy", "Power", 1), encoding="utf-8")
    before = snapshot(tree)
    assert guides.main(["--check"]) == 1
    err = capsys.readouterr().err
    assert "docs/guide.en.html" in err
    assert "docs/guide.pl.html" not in err
    assert snapshot(tree) == before


def test_stale_polish_html_is_named(tree, capsys):
    page = tree / "guide.pl.html"
    page.write_text(page.read_text(encoding="utf-8") + "\n<!-- x -->", encoding="utf-8")
    assert guides.main(["--check"]) == 1
    assert "docs/guide.pl.html" in capsys.readouterr().err


def test_edited_mermaid_block_reports_missing_and_orphan_svg(tree, capsys):
    source = tree / "guide.en.md"
    text = source.read_text(encoding="utf-8")
    start = text.index("```mermaid\n") + len("```mermaid\n")
    source.write_text(text[:start] + "%% edited\n" + text[start:], encoding="utf-8")
    assert guides.main(["--check"]) == 1
    err = capsys.readouterr().err
    assert "missing, referenced by a guide" in err
    assert "not referenced by any guide" in err


def test_missing_html_is_stale(tree, capsys):
    (tree / "guide.en.html").unlink()
    assert guides.main(["--check"]) == 1
    assert "docs/guide.en.html" in capsys.readouterr().err
