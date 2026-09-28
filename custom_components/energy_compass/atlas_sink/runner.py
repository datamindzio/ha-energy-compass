"""Edge runner: telemetry loop independent of the optimiser solver (invariant 6, S-005)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from .windows import Aggregator, Window

log = logging.getLogger(__name__)


def _payload(w: Window) -> dict[str, Any]:
    return {
        "window_start": w.window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "samples": w.samples,
        **w.values,
    }


class Runner:
    def __init__(
        self, outbox: Any, sender: Any, clock: Any, follower: Any | None = None
    ):
        self._ob = outbox
        self._sender = sender
        self._clock = clock
        self._follower = follower  # ConfigFollower: paces pushes, applies pushed config
        self._agg = Aggregator()
        self._unsaved: list[
            Window
        ] = []  # closed by the aggregator, not yet in the outbox

    def feed(self, ts: datetime, values: dict[str, float]) -> None:
        self._agg.feed(ts, values)

    async def tick(self) -> None:
        """Close finished windows into the outbox, then drain it. Never raises, never solves."""
        try:
            self._unsaved += self._agg.close(self._clock.now())
        except Exception:
            log.exception("window aggregation failed")
        # the aggregator forgets closed windows: keep them until the outbox has them
        try:
            while self._unsaved:
                w = self._unsaved[0]
                self._ob.add("telemetry", _payload(w), w.window_start)
                self._unsaved.pop(0)
        except Exception:
            log.exception(
                "outbox add failed; %d window(s) kept for retry", len(self._unsaved)
            )
        try:
            f = self._follower
            if f is not None:
                await f.refresh()
                # a retry in progress bypasses the interval gate: the sender paces itself
                # (backoff 5 s x2 up to 15 min), the push interval must not stretch it
                retrying = getattr(self._sender, "next_attempt_at", None) is not None
                if not retrying and not f.push_due():
                    return
            await self._sender.run_once()
            # mark the cycle only when clean; a failed cycle stays due and backs off in the sender
            if f is not None and getattr(self._sender, "next_attempt_at", None) is None:
                f.mark_pushed()
        except Exception:
            log.exception("sender run failed")

    async def run_solver(
        self, solve: Callable[[], Awaitable[Any]], timeout_s: float
    ) -> Any | None:
        """Run one solve isolated from telemetry: errors and timeouts are logged, return None.

        The solver must be truly async: a synchronous or CPU-bound solver blocks the event loop,
        so neither the timeout nor tick() can run. Pass `lambda: asyncio.to_thread(solve)`;
        note a timed-out thread cannot be cancelled and keeps running in the background.
        """
        try:
            async with asyncio.timeout(timeout_s):
                return await solve()
        except TimeoutError:
            log.warning("solver timed out after %ss; cancelled", timeout_s)
        except Exception:
            log.exception("solver failed")
        return None
