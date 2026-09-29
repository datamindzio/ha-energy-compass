"""T-512: `SinkThread.stop()` wakes the IO loop instead of waiting out `tick_s`.

Drives the genuine vendored `SinkThread` with the production default `tick_s` (30 s); the
earlier lifecycle tests use `tick_s=0.01`, which hid the sleep.
"""

import threading
import time

import httpx

from custom_components.energy_compass.atlas_sink.sink import SinkThread, register


def _register_ok(request):
    return httpx.Response(201, json={"site_id": "site-1"})


def _ok(request):
    return httpx.Response(200, json={})


def _io_threads():
    return [t for t in threading.enumerate() if t.name == "atlas-sink-io"]


def test_stop_with_default_tick_returns_fast_and_io_thread_exits(tmp_path):
    register(
        tmp_path,
        "https://atlas-api-staging.example.invalid",
        "secret",
        transport=httpx.MockTransport(_register_ok),
    )
    sink = SinkThread(
        tmp_path,
        "https://atlas-api-staging.example.invalid",
        {"pv_kwp": 5.0},
        transport=httpx.MockTransport(_ok),
    )
    sink.start()
    time.sleep(0.3)  # first tick done, IO loop is in the inter-tick wait
    assert _io_threads()

    started = time.monotonic()
    sink.stop(10)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert not sink.is_alive()
    assert not _io_threads()


def test_stop_right_after_start_does_not_wait_a_tick(tmp_path):
    register(
        tmp_path,
        "https://atlas-api-staging.example.invalid",
        "secret",
        transport=httpx.MockTransport(_register_ok),
    )
    sink = SinkThread(
        tmp_path,
        "https://atlas-api-staging.example.invalid",
        {"pv_kwp": 5.0},
        transport=httpx.MockTransport(_ok),
    )
    sink.start()

    started = time.monotonic()
    sink.stop(10)

    assert time.monotonic() - started < 1.0
    assert not _io_threads()
