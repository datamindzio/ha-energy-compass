"""Status line shown in the Energy Atlas options step (ADR-0019 §B3, amendment T-411).

Never carries a `site_id`, key or URL (same hygiene scope as everywhere else in this
package): only `sink.status()`'s public fields (`registered`, `pending`, `last_success_at`,
`halted`) and the environment name feed it.
"""

from __future__ import annotations

from homeassistant.util import dt as dt_util

_TEMPLATES = {
    "en": {
        "off": "off",
        "registered": "registered on {environment} · last delivery {last_delivery} · waiting {pending}",
        "never": "never",
        "halted": " · halted: {kinds}",
    },
    "pl": {
        "off": "wyłączone",
        "registered": "zarejestrowano na {environment} · ostatnia dostawa {last_delivery} · oczekuje {pending}",
        "never": "nigdy",
        "halted": " · zablokowane: {kinds}",
    },
}


def render(status: dict, environment: str, language: str) -> str:
    """Build the status line: `off` / `registered on <env> ... waiting <n>` [+ halted]."""
    templates = _TEMPLATES.get(language, _TEMPLATES["en"])
    if not status.get("registered"):
        return templates["off"]
    last_success_at = status.get("last_success_at")
    last_delivery = (
        dt_util.as_local(last_success_at).strftime("%H:%M")
        if last_success_at is not None
        else templates["never"]
    )
    pending = sum(status.get("pending", {}).values())
    line = templates["registered"].format(
        environment=environment, last_delivery=last_delivery, pending=pending
    )
    halted = status.get("halted") or {}
    if halted:
        line += templates["halted"].format(kinds=", ".join(sorted(halted)))
    return line
