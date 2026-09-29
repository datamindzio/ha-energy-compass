"""SinkThread facade (ADR-0019 §1): the one entry point Home Assistant glue is allowed to call.

`register()` and `proof()` are blocking module functions; `SinkThread` owns a daemon thread with
its own event loop. All httpx/SQLite objects (Identity, Outbox, SignedClient, Sender,
ConfigFollower, Runner, AttributeTracker) are created and used only on that loop. Callers on any
other thread only ever touch `feed`/`add_solve`/`backfill` (queue puts, O(1), never raise) and
`status`/`stop`.
"""

import asyncio
import contextlib
import logging
import queue
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import proof as _proof
from .attributes import AttributeTracker
from .backfill import Backfill
from .clock import Clock, SystemClock
from .config_follower import ConfigFollower
from .identity import Identity, RegistrationFailed
from .outbox import Outbox
from .runner import Runner
from .sender import Sender

log = logging.getLogger(__name__)

SITE_FILE = "site.json"
_OUTBOX_KINDS = ("attributes", "telemetry", "hourly", "solves")


class RegistrationError(Exception):
    """Raised by `register()`; `.kind` is one of the ADR-0019 §4 slugs."""

    def __init__(self, kind: str, status: int | None = None):
        super().__init__(f"site registration failed: {kind}")
        self.kind = kind
        self.status = status


class NotRegistered(Exception):
    """Raised by `SinkThread.start()` when `dir/site.json` is missing."""


def _classify_registration_error(resp: httpx.Response) -> str:
    slug = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            slug = str(body.get("type", "")).rsplit("/", 1)[-1]
    except ValueError:
        pass
    status = resp.status_code
    if status == 401 and slug == "credential-invalid":
        return "invalid_enrollment_secret"
    if status == 409 and slug == "site-key-revoked":
        return "site_key_revoked"
    if status == 409 and slug == "conflict":
        return "site_conflict"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "cannot_connect"
    return "unknown"


def register(
    dir: Path,
    base_url: str,
    enrollment_secret: str,
    timeout_s: float = 30,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Blocking. `Identity(dir).ensure_registered(...)` on a private event loop (ADR-0019 §1/§4)."""
    identity = Identity(Path(dir))

    async def _run() -> str:
        return await identity.ensure_registered(
            base_url, enrollment_secret, timeout_s, transport=transport
        )

    try:
        return asyncio.run(_run())
    except RegistrationFailed as e:
        kind = _classify_registration_error(e.response)
        log.debug("registration failed: %s (status=%s)", kind, e.response.status_code)
        raise RegistrationError(kind, e.response.status_code) from e
    except (httpx.TimeoutException, httpx.TransportError) as e:
        log.debug("registration failed: cannot_connect (%s)", type(e).__name__)
        raise RegistrationError("cannot_connect") from e


def proof(dir: Path, now: datetime) -> str:
    """Blocking, no I/O except reading the key. `proof.generate(Identity(dir), now)`."""
    return _proof.generate(Identity(Path(dir)), now)


def _parse_ts(s: str, default: datetime) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, AttributeError):
        return default


class _ObservingClient:
    """Wraps a SignedClient to record the last successful (2xx) response time, without
    changing the Sender/ConfigFollower contract (`.site_id`, async `post`/`get`)."""

    def __init__(self, inner: Any, on_response: Any):
        self._inner = inner
        self._on_response = on_response

    @property
    def site_id(self) -> str:
        return self._inner.site_id

    async def post(self, path: str, body: dict) -> httpx.Response:
        resp = await self._inner.post(path, body)
        self._on_response(resp)
        return resp

    async def get(
        self, path: str, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        resp = await self._inner.get(path, headers=headers)
        self._on_response(resp)
        return resp

    async def aclose(self) -> None:
        await self._inner.aclose()


class SinkThread(threading.Thread):
    """Nothing runs until `start()`. `transport` and `tick_s` are test-only keyword hooks."""

    def __init__(
        self,
        dir: Path,
        base_url: str,
        attrs: dict,
        *,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        tick_s: float = 30,
    ):
        super().__init__(name="atlas-sink", daemon=True)
        self._dir = Path(dir)
        self._base_url = base_url
        self._attrs = dict(attrs)
        self._clock = clock or SystemClock()
        self._transport = transport
        self._tick_s = tick_s
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._stop_requested = threading.Event()
        self._shutdown_bound = 10.0
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "registered": False,
            "pending": dict.fromkeys(_OUTBOX_KINDS, 0),
            "dead": 0,
            "last_success_at": None,
            "halted": {},
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._io_thread: threading.Thread | None = None
        self._last_success_at: datetime | None = None

    def start(self) -> None:
        if not (self._dir / SITE_FILE).exists():
            raise NotRegistered(
                f"{self._dir} is not registered; call sink.register() first"
            )
        super().start()

    def feed(self, ts: datetime, values: dict[str, float]) -> None:
        self._queue.put(("feed", ts, dict(values)))

    def add_solve(self, payload: dict) -> None:
        self._queue.put(("solve", dict(payload)))

    def backfill(self, stats: dict) -> None:
        self._queue.put(("backfill", stats))

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def stop(self, timeout_s: float = 10) -> None:
        """Idempotent. Joins within `timeout_s` even if the transport hangs (the IO loop may
        be left running in the background in that case; it never touches `self`). The inner
        bound is kept a bit tighter than `timeout_s` so `run()` has time to return before this
        method's own `join` deadline."""
        self._shutdown_bound = max(0.0, timeout_s - 0.3)
        self._stop_requested.set()
        self._wake_io()
        self.join(timeout_s)

    def _wake_io(self) -> None:
        """Interrupt the inter-tick wait on the IO loop so a stop is not delayed by `tick_s`."""
        loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            return
        with contextlib.suppress(RuntimeError):  # loop already closed
            loop.call_soon_threadsafe(wake.set)

    # -- runs on the "atlas-sink" thread --------------------------------------------------

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._io_thread = threading.Thread(
            target=self._run_io_loop, name="atlas-sink-io", daemon=True
        )
        self._io_thread.start()
        self._stop_requested.wait()
        # Bounded: a hung transport (never reached an await point) cannot be interrupted from
        # here, but this thread must still return so `is_alive()` goes false (ADR-0019 §1).
        self._io_thread.join(self._shutdown_bound)

    def _run_io_loop(self) -> None:
        loop = self._loop
        assert loop is not None
        try:
            loop.run_until_complete(self._main())
        except Exception:
            log.exception("sink IO loop crashed")
        finally:
            loop.close()

    # -- runs on the inner "atlas-sink-io" thread's event loop ----------------------------

    async def _main(self) -> None:
        wake = self._wake = asyncio.Event()
        identity = Identity(self._dir)
        outbox = Outbox(self._dir / "outbox.sqlite")
        client: Any = None
        try:
            client = _ObservingClient(
                identity.signed_client(self._base_url, transport=self._transport),
                self._on_response,
            )
            follower = ConfigFollower(client, self._clock)
            sender = Sender(
                outbox, client, self._clock, on_config_etag=follower.on_config_etag
            )
            runner = Runner(outbox, sender, self._clock, follower)
            tracker = AttributeTracker(outbox, self._clock, lambda: dict(self._attrs))
            self._publish_status(outbox, sender, registered=True)
            # ADR-0019 §1 tick order: drain input -> AttributeTracker.check() -> Runner.tick(),
            # so a snapshot queued on this tick (incl. the final one on stop) is pushed by it.
            while not self._stop_requested.is_set():
                await self._drain_queue(runner, outbox)
                self._check_attributes(tracker, sender)
                await runner.tick()
                self._publish_status(outbox, sender, registered=True)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), self._tick_s)
            # finish what is already in the queue and flush once more before closing
            await self._drain_queue(runner, outbox)
            self._check_attributes(tracker, sender)
            try:
                await runner.tick()
            except Exception:
                log.exception("final tick failed")
            self._publish_status(outbox, sender, registered=True)
        finally:
            if client is not None:
                await client.aclose()
            outbox.close()

    def _check_attributes(self, tracker: AttributeTracker, sender: Sender) -> None:
        # Skip while a transient (network-down) failure is being backed off: queuing a fresh
        # snapshot then wouldn't change anything (the hash is unchanged) and would only leave
        # an unrelated kind sitting ahead of telemetry in the shared drain order.
        if sender.next_attempt_at is not None:
            return
        try:
            tracker.check()
        except Exception:
            log.exception("attribute check failed")

    def _on_response(self, resp: httpx.Response) -> None:
        if 200 <= resp.status_code < 300:
            self._last_success_at = self._clock.now()

    async def _drain_queue(self, runner: Runner, outbox: Outbox) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            kind = item[0]
            try:
                if kind == "feed":
                    _, ts, values = item
                    runner.feed(ts, values)
                elif kind == "solve":
                    _, payload = item
                    now = self._clock.now()
                    outbox.add(
                        "solves", payload, _parse_ts(payload.get("solve_ts", ""), now)
                    )
                elif kind == "backfill":
                    _, stats = item
                    await Backfill(stats, outbox, self._clock).run()
            except Exception:
                log.exception("failed to process queued %s item", kind)

    def _publish_status(
        self, outbox: Outbox, sender: Sender, *, registered: bool
    ) -> None:
        pending = dict.fromkeys(_OUTBOX_KINDS, 0)
        for item in outbox.pending():
            pending[item.kind] = pending.get(item.kind, 0) + 1
        halted = {kind: halt.reason for kind, halt in sender.halts.items()}
        with self._status_lock:
            self._status = {
                "registered": registered,
                "pending": pending,
                "dead": len(outbox.dead()),
                "last_success_at": self._last_success_at,
                "halted": halted,
            }
