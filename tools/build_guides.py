"""Render docs/guide.*.md into the static GitHub Pages site.

The Markdown guides are the single source of truth. This script writes
docs/guide.<lang>.html and pre-rendered light/dark Mermaid SVGs under
docs/assets/diagrams/, reusing the stylesheet embedded in docs/index.html so
the site keeps one look and needs no JavaScript.

Requirements: the `docs` extra (`pip install -e '.[docs]'` or
`pip install markdown==3.8`) and Node.js with npx for @mermaid-js/mermaid-cli.

Usage: python tools/build_guides.py
"""

import hashlib
import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DIAGRAMS = DOCS / "assets" / "diagrams"
REPO = "https://github.com/datamindzio/ha-energy-compass/blob/main/"
MERMAID_CLI = "@mermaid-js/mermaid-cli@11"
LANGUAGES = {
    "en": {
        "html_lang": "en",
        "other": ("pl", "Polski"),
        "back": "Entity and calculation guide",
        "contents": "Contents",
        "source": "Markdown source",
        "diagram": "Diagram",
    },
    "pl": {
        "html_lang": "pl",
        "other": ("en", "English"),
        "back": "Przewodnik po encjach i obliczeniach (EN)",
        "contents": "Spis treści",
        "source": "Źródło Markdown",
        "diagram": "Diagram",
    },
}
MERMAID_BLOCK = re.compile(r"^```mermaid\n(.*?)^```\n", re.DOTALL | re.MULTILINE)
EXTRA_CSS = """
    .lang { margin: .75rem 0 1.5rem; font-size: .9rem; }
    figure.diagram { margin: 1.4rem 0 1.8rem; padding: 1rem; background: var(--surface);
      border: 1px solid var(--line); border-radius: .35rem; overflow-x: auto; }
    figure.diagram img { display: block; max-width: 100%; height: auto; margin: auto; }
    pre { padding: 1rem 1.2rem; background: var(--surface); border: 1px solid var(--line);
      border-radius: .35rem; overflow-x: auto; font-size: .88rem; }
    pre code { background: none; padding: 0; }
    main table { min-width: 0; }
"""


def slugify(value: str, separator: str = "-") -> str:
    """GitHub heading anchors, so in-page links match the rendered Markdown."""
    value = re.sub(r"<[^>]+>", "", html.unescape(value)).strip().lower()
    value = re.sub(r"[^\w\- ]", "", value)
    return value.replace(" ", separator)


def stylesheet() -> str:
    index = (DOCS / "index.html").read_text(encoding="utf-8")
    match = re.search(r"<style>(.*?)</style>", index, re.DOTALL)
    if not match:
        sys.exit("docs/index.html has no <style> block")
    return match.group(1).rstrip() + EXTRA_CSS


def render_diagram(source: str, work: Path) -> str:
    """Render one Mermaid block to light and dark SVGs; return the file stem."""
    digest = hashlib.sha256(source.encode()).hexdigest()[:12]
    stem = f"diagram-{digest}"
    targets = {
        "default": DIAGRAMS / f"{stem}.svg",
        "dark": DIAGRAMS / f"{stem}-dark.svg",
    }
    if all(path.exists() for path in targets.values()):
        return stem
    mmd = work / f"{stem}.mmd"
    mmd.write_text(source, encoding="utf-8")
    for theme, target in targets.items():
        subprocess.run(
            [
                "npx",
                "-y",
                MERMAID_CLI,
                "-q",
                "-t",
                theme,
                "-b",
                "transparent",
                "-i",
                str(mmd),
                "-o",
                str(target),
            ],
            check=True,
        )
    return stem


def rewrite_href(href: str) -> str:
    """Point guide links at the sibling HTML page and everything else at GitHub."""
    if href.startswith(("http://", "https://", "mailto:", "#")):
        return href
    path, _, fragment = href.partition("#")
    suffix = f"#{fragment}" if fragment else ""
    if re.fullmatch(r"guide\.(en|pl)\.md", path):
        return path.removesuffix(".md") + ".html" + suffix
    if path == "index.html":
        return href
    target = (DOCS / path).resolve().relative_to(ROOT)
    return f"{REPO}{target.as_posix()}{suffix}"


def build(lang: str, css: str, work: Path, used: set[str]) -> None:
    text = (DOCS / f"guide.{lang}.md").read_text(encoding="utf-8")
    labels = LANGUAGES[lang]
    figures = []

    def replace(match: re.Match) -> str:
        stem = render_diagram(match.group(1), work)
        used.update({f"{stem}.svg", f"{stem}-dark.svg"})
        caption = f"{labels['diagram']} {len(figures) + 1}"
        figures.append(
            '<figure class="diagram"><picture>'
            f'<source media="(prefers-color-scheme: dark)" '
            f'srcset="assets/diagrams/{stem}-dark.svg">'
            f'<img src="assets/diagrams/{stem}.svg" alt="{caption}" loading="lazy">'
            "</picture></figure>"
        )
        return f"\n\nDIAGRAMPLACEHOLDER{len(figures) - 1}\n\n"

    text = MERMAID_BLOCK.sub(replace, text)
    converter = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
        extension_configs={"toc": {"slugify": slugify}},
    )
    body = converter.convert(text)
    for number, figure in enumerate(figures):
        body = body.replace(f"<p>DIAGRAMPLACEHOLDER{number}</p>", figure)
    body = re.sub(
        r'href="([^"]+)"',
        lambda m: f'href="{html.escape(rewrite_href(html.unescape(m.group(1))))}"',
        body,
    )
    body = re.sub(r"<table>", '<section class="table-scroll"><table>', body)
    body = body.replace("</table>", "</table></section>")
    title_match = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.DOTALL)
    title = re.sub(r"<[^>]+>", "", title_match.group(1)) if title_match else ""
    nav = "\n".join(
        f'        <li><a href="#{entry["id"]}">{entry["name"]}</a></li>'
        for top in converter.toc_tokens
        for entry in top.get("children", [])
        if entry["id"] != slugify(labels["contents"])
    )
    other_lang, other_name = labels["other"]
    page = f"""<!DOCTYPE html>
<html lang="{labels["html_lang"]}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>{html.escape(title)}</title>
  <!-- Generated by tools/build_guides.py from docs/guide.{lang}.md; edit the Markdown. -->
  <style>{css}
  </style>
</head>
<body>
<div class="layout">
  <aside class="sidebar">
    <a class="brand" href="index.html">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="assets/dark_icon@2x.png">
        <img src="assets/icon@2x.png" alt="" width="60" height="60">
      </picture>
      <span>Energy Compass</span>
    </a>
    <p class="lang"><a href="guide.{other_lang}.html" hreflang="{other_lang}">{other_name}</a>
      · <a href="index.html">{labels["back"]}</a></p>
    <nav aria-label="{labels["contents"]}">
      <ul>
{nav}
      </ul>
    </nav>
    <a class="download" href="{REPO}docs/guide.{lang}.md">{labels["source"]}</a>
  </aside>
  <main id="main">
{body}
  </main>
</div>
</body>
</html>
"""
    (DOCS / f"guide.{lang}.html").write_text(page, encoding="utf-8")


def main() -> None:
    if shutil.which("npx") is None:
        sys.exit("npx not found: install Node.js to render Mermaid diagrams")
    DIAGRAMS.mkdir(parents=True, exist_ok=True)
    css = stylesheet()
    used: set[str] = set()
    with tempfile.TemporaryDirectory() as tmp:
        for lang in LANGUAGES:
            build(lang, css, Path(tmp), used)
    for stale in DIAGRAMS.glob("*.svg"):
        if stale.name not in used:
            stale.unlink()
    print(f"built {len(LANGUAGES)} guides, {len(used) // 2} diagrams")


if __name__ == "__main__":
    main()
