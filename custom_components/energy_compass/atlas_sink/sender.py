"""Outbox drain: batching, exponential backoff, ack-only removal, poison isolation (S-005)."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from .outbox import Item, Outbox

log = logging.getLogger(__name__)

MAX_BATCH = 288
BACKOFF_START_S = 5
BACKOFF_MAX_S = 900

_DRAIN_ORDER = ("attributes", "telemetry", "hourly", "solves")


@dataclass
class Halt:
    reason: str
    backoff_s: int
    next_attempt_at: datetime


# kind -> (path suffix, batch body key or None for one item per POST)
_ROUTES: dict[str, tuple[str, str | None]] = {
    "attributes": ("attributes", None),
    "telemetry": ("telemetry", "windows"),
    "hourly": ("telemetry/hourly", "hours"),
    "solves": ("solves", "solves"),
}


_ITEM_SLUGS = {"invalid-body", "payload-too-large", "conflict"}


def _problem(resp: httpx.Response) -> dict | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _slug(resp: httpx.Response) -> str:
    p = _problem(resp)
    return str(p.get("type", "")).rsplit("/", 1)[-1] if p else ""


def classify(resp: httpx.Response) -> str:
    """ADR-0013: "transient" | "item" | "site" for a non-200 response, by slug not status."""
    code = resp.status_code
    slug = _slug(resp)
    if code >= 500 or code == 429 or (code == 401 and slug == "signature-expired"):
        return "transient"
    if slug in _ITEM_SLUGS:
        return "item"
    if slug == "invalid-field":
        errors = (_problem(resp) or {}).get("errors")
        if (
            isinstance(errors, list)
            and errors
            and all(
                isinstance(e, dict)
                and isinstance(e.get("field"), str)
                and e["field"].startswith("/")
                for e in errors
            )
        ):
            return "item"
    return "site"


def _retry_after(resp: httpx.Response) -> int:
    try:
        return max(0, int(resp.headers.get("Retry-After", "0")))
    except ValueError:
        return 0


class Sender:
    def __init__(
        self,
        outbox: Outbox,
        client: Any,
        clock: Any,
        on_config_etag: Callable[[str], None] | None = None,
    ):
        self._ob = outbox
        self._client = client
        self._clock = clock
        self._on_etag = on_config_etag
        self.backoff_s = 0
        self.next_attempt_at = None
        self._limit = MAX_BATCH  # shrinks while isolating a poison item
        self.halts: dict[str, Halt] = {}

    @property
    def blocked(self) -> str | None:
        """None if no kind is halted, else "<kind> <reason>" per halted kind (ADR-0013)."""
        parts = [
            f"{kind} {self.halts[kind].reason}"
            for kind in _DRAIN_ORDER
            if kind in self.halts
        ]
        return "; ".join(parts) if parts else None

    async def run_once(self) -> None:
        try:
            self._ob.enforce_retention(self._clock.now(), self._log_drop)
            now = self._clock.now()
            if self.next_attempt_at is not None and now < self.next_attempt_at:
                return
            for kind in _DRAIN_ORDER:
                if not await self._drain(kind):
                    return
        except Exception:
            log.exception("sender cycle failed")

    @staticmethod
    def _log_drop(item: Item) -> None:
        log.warning(
            "dropped %s item older than 30 days (item_time %s)",
            item.kind,
            item.item_time.isoformat(),
        )

    async def _drain(self, kind: str) -> bool:
        """Send everything pending for `kind`. False = stop the cycle (transient failure)."""
        suffix, key = _ROUTES[kind]
        path = f"/v1/sites/{self._client.site_id}/{suffix}"
        halt = self.halts.get(kind)
        if halt is not None and self._clock.now() < halt.next_attempt_at:
            return True
        while True:
            items = self._ob.pending(kind, self._limit if key else 1)
            if not items:
                self._limit = MAX_BATCH
                if kind in self.halts:
                    del self.halts[kind]
                return True
            body = {key: [i.payload for i in items]} if key else items[0].payload
            try:
                resp = await self._client.post(path, body)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                log.warning("send %s failed: %r", kind, e)
                self._fail()
                return False
            etag = resp.headers.get("Config-ETag")
            if etag and self._on_etag:
                self._on_etag(etag)
            code = resp.status_code
            if 200 <= code < 300:
                self._ob.remove_many([i.id for i in items])
                self.backoff_s = 0
                self.next_attempt_at = None
                halted = self.halts.get(kind)
                if halted is not None:
                    log.info("sender %s unblocked (was %s)", kind, halted.reason)
                    del self.halts[kind]
                continue
            cls = classify(resp)
            if cls == "transient":
                log.warning("send %s transient failure %d %s", kind, code, _slug(resp))
                self._fail(_retry_after(resp) if code == 429 else 0)
                return False
            if cls == "site":
                reason = f"{code} {_slug(resp) or 'no-problem-body'}"
                existing = self.halts.get(kind)
                if existing is None:
                    backoff_s = BACKOFF_START_S
                    warn = True
                else:
                    backoff_s = min(existing.backoff_s * 2, BACKOFF_MAX_S)
                    warn = existing.reason != reason
                if warn:
                    log.warning(
                        "send %s blocked by site-level error %s; items kept, retrying",
                        kind,
                        reason,
                    )
                self.halts[kind] = Halt(
                    reason=reason,
                    backoff_s=backoff_s,
                    next_attempt_at=self._clock.now() + timedelta(seconds=backoff_s),
                )
                self._limit = MAX_BATCH
                return True
            if len(items) > 1:
                # item-level 4xx on a batch: halve until the poison item is alone
                self._limit = max(1, len(items) // 2)
                continue
            slug = _slug(resp) or "error"
            log.warning(
                "item %d (%s, %s) is poison: %d %s; moved to dead",
                items[0].id,
                kind,
                items[0].item_time.isoformat(),
                code,
                slug,
            )
            self._ob.mark_dead(items[0].id, code, slug)
            self._limit = MAX_BATCH

    def _fail(self, retry_after: int = 0) -> None:
        self.backoff_s = (
            BACKOFF_START_S
            if self.backoff_s == 0
            else min(self.backoff_s * 2, BACKOFF_MAX_S)
        )
        wait = max(self.backoff_s, retry_after)
        self.next_attempt_at = self._clock.now() + timedelta(seconds=wait)
