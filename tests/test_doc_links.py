"""Relative-link and anchor checker for the Markdown docs this PR touches.

No dedicated link checker runs in CI (`.github/workflows/validate.yml` only
runs pytest, the three generators, ruff and pip check); AGENTS.md step 5
("Check links") is otherwise unenforced.
"""

import html
import importlib.util
import re
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FILES = (
    "README.md",
    "README.pl.md",
    "docs/guide.en.md",
    "docs/guide.pl.md",
    "docs/installation.md",
    "docs/model.md",
    "docs/tariff-helper.md",
    "docs/source-contracts.md",
)

_ATX_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_INLINE_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")
_REF_DEF_RE = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)")
_HTML_ATTR_RE = re.compile(r'\b(?:href|src)="([^"]*)"')
_HTML_ID_RE = re.compile(r'\b(?:id|name)="([^"]*)"')
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


def github_slug(text: str, separator: str = "-") -> str:
    """GitHub heading anchor for Markdown source text (not rendered HTML)."""
    text = _INLINE_LINK_RE.sub(lambda m: m.group(1), text)
    text = re.sub(r"<[^>]+>", "", html.unescape(text)).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", separator)


def _is_fence(stripped: str) -> bool:
    return stripped.startswith(("```", "~~~"))


def _headings(path: Path) -> list[str]:
    headings = []
    in_fence = False
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if _is_fence(stripped):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _ATX_RE.match(line)
        if match:
            headings.append(match.group(1))
    return headings


def anchors(path: Path) -> set[str]:
    """ATX heading slugs (with GitHub duplicate suffixes) plus explicit ids."""
    result: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if _is_fence(stripped):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _ATX_RE.match(line)
        if match:
            slug = github_slug(match.group(1))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            result.add(slug if count == 0 else f"{slug}-{count}")
        for attr_match in _HTML_ID_RE.finditer(line):
            result.add(attr_match.group(1))
    return result


def _clean_target(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<"):
        end = raw.find(">")
        return raw[1:end] if end != -1 else raw[1:]
    match = re.match(r'^(\S+)(?:\s+"[^"]*")?$', raw)
    return match.group(1) if match else raw


def links(path: Path) -> list[tuple[int, str]]:
    """(line, target) for inline links/images, reference defs, href/src."""
    result: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        stripped = line.strip()
        if _is_fence(stripped):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        scan_line = _INLINE_CODE_RE.sub("", line)
        for match in _INLINE_LINK_RE.finditer(scan_line):
            result.append((lineno, _clean_target(match.group(2))))
        ref_match = _REF_DEF_RE.match(scan_line)
        if ref_match:
            result.append((lineno, _clean_target(ref_match.group(1))))
        for match in _HTML_ATTR_RE.finditer(scan_line):
            result.append((lineno, _clean_target(match.group(1))))
    return result


def _resolve(path: Path, target: str) -> tuple[Path, str | None]:
    url_path, _, fragment = target.partition("#")
    url_path = urllib.parse.unquote(url_path)
    resolved = path if url_path == "" else (path.parent / url_path).resolve()
    return resolved, (fragment or None)


@pytest.mark.parametrize("rel", FILES)
def test_relative_links_resolve(rel):
    path = ROOT / rel
    failures = []
    for lineno, target in links(path):
        if not target or _SCHEME_RE.match(target):
            continue
        resolved, fragment = _resolve(path, target)
        try:
            resolved.relative_to(ROOT)
        except ValueError:
            failures.append((rel, lineno, target, "resolves outside the repository"))
            continue
        if not resolved.exists():
            failures.append((rel, lineno, target, "target does not exist"))
            continue
        if fragment and resolved.suffix == ".md" and fragment not in anchors(resolved):
            failures.append((rel, lineno, target, f"missing anchor #{fragment}"))
    assert not failures, failures


def test_github_slug_examples():
    assert (
        github_slug("Setup profiles (new installations)")
        == "setup-profiles-new-installations"
    )
    assert (
        github_slug("Profile startowe (nowe instalacje)")
        == "profile-startowe-nowe-instalacje"
    )
    assert github_slug("Kadencja przeliczeń") == "kadencja-przeliczeń"
    assert github_slug("The `cost_min` strategy") == "the-cost_min-strategy"

    seen: dict[str, int] = {}
    slugs = []
    for heading in ("Setup", "Setup"):
        slug = github_slug(heading)
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        slugs.append(slug if count == 0 else f"{slug}-{count}")
    assert slugs == ["setup", "setup-1"]


def test_slug_matches_build_guides():
    pytest.importorskip("markdown")
    spec = importlib.util.spec_from_file_location(
        "build_guides", ROOT / "tools" / "build_guides.py"
    )
    build_guides = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_guides)

    for rel in ("docs/guide.en.md", "docs/guide.pl.md"):
        for heading in _headings(ROOT / rel):
            if "](" in heading:
                continue
            assert github_slug(heading) == build_guides.slugify(heading)
