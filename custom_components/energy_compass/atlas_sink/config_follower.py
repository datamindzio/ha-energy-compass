"""Config follower: apply pushed config without restart (S-008 edge half).

The sender reports every Config-ETag it sees (`on_config_etag`). When it differs from the ETag of the
config applied so far, `refresh()` GETs /config with If-None-Match and adopts the new push_interval_s
for the next cycle. Failures keep the last good config.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_PUSH_INTERVAL_S = 300
MIN_PUSH_INTERVAL_S = 300
MAX_PUSH_INTERVAL_S = 86_400


class ConfigFollower:
    def __init__(self, client: Any, clock: Any):
        self._client = client  # needs site_id and async get(path, headers=...)
        self._clock = clock
        self.push_interval_s = DEFAULT_PUSH_INTERVAL_S
        self.etag: str | None = None  # ETag of the applied config
        self._seen: str | None = None  # latest Config-ETag announced by the collector
        self._last_push: datetime | None = None
        self._retry_at: datetime | None = None  # no refetch before this (one per cycle)

    def on_config_etag(self, etag: str) -> None:
        """Sender callback: remember the collector's current config ETag."""
        self._seen = etag

    @property
    def stale(self) -> bool:
        return self._seen is not None and self._seen != self.etag

    async def refresh(self) -> bool:
        """Fetch config if the announced ETag changed. True when a new config was applied."""
        if not self.stale:
            return False
        now = self._clock.now()
        if self._retry_at is not None and now < self._retry_at:
            return False
        try:
            headers = {"If-None-Match": self.etag} if self.etag else {}
            resp = await self._client.get(
                f"/v1/sites/{self._client.site_id}/config", headers=headers
            )
        except Exception as e:
            return self._retry_later(f"config fetch failed: {e!r}")
        if resp.status_code == 304:
            # our If-None-Match is current; adopt an ETag only if the server names one
            self.etag = resp.headers.get("ETag", self.etag)
            self._retry_at = now + timedelta(seconds=self.push_interval_s)
            return False
        if resp.status_code != 200:
            return self._retry_later(f"config fetch failed: HTTP {resp.status_code}")
        try:
            interval = resp.json()["config"]["push_interval_s"]
        except Exception as e:
            return self._retry_later(f"config body unreadable: {e!r}")
        if (
            not isinstance(interval, int)
            or isinstance(interval, bool)
            or not MIN_PUSH_INTERVAL_S <= interval <= MAX_PUSH_INTERVAL_S
            or interval % MIN_PUSH_INTERVAL_S
        ):
            return self._retry_later(f"ignoring invalid push_interval_s {interval!r}")
        self.push_interval_s = interval
        self.etag = resp.headers.get("ETag", self._seen)
        self._retry_at = None
        return True

    def _retry_later(self, msg: str) -> bool:
        log.warning("%s; keeping current config", msg)
        self._retry_at = self._clock.now() + timedelta(seconds=self.push_interval_s)
        return False

    def push_due(self) -> bool:
        """True when a push cycle should run now (first call is always due)."""
        if self._last_push is None:
            return True
        return self._clock.now() >= self._last_push + timedelta(
            seconds=self.push_interval_s
        )

    def mark_pushed(self) -> None:
        self._last_push = self._clock.now()
