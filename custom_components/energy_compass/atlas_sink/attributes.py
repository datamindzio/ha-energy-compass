"""Site attribute snapshots: RFC 8785 canonical JSON hash and change detection (S-007)."""

import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC
from typing import Any

from .outbox import Outbox

SCHEMA_VERSION = 1


def _num(v: int | float) -> str:
    if isinstance(v, int):
        return str(v)
    if not math.isfinite(v):
        raise ValueError("non-finite number in canonical JSON")
    if v == 0:
        return "0"
    sign = "-" if v < 0 else ""
    # shortest round-trip digits via repr
    r = repr(abs(v))
    if "e" in r:
        m, e = r.split("e")
        exp10 = int(e)
    else:
        m, exp10 = r, 0
    ip, _, fp = m.partition(".")
    digits = (ip + fp).lstrip("0")
    lead = len(ip + fp) - len((ip + fp).lstrip("0"))
    n = len(ip) - lead + exp10  # decimal point position relative to digits
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        es = f"{'+' if e >= 0 else '-'}{abs(e)}"
        out = (digits[0] + ("." + digits[1:] if k > 1 else "")) + "e" + es
    return sign + out


def _utf16_key(s: str) -> bytes:
    return s.encode("utf-16-be")


def _canon(v: Any) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return _num(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_canon(x) for x in v) + "]"
    if isinstance(v, dict):
        keys = sorted(v, key=_utf16_key)
        return "{" + ",".join(f"{_canon(k)}:{_canon(v[k])}" for k in keys) + "}"
    raise TypeError(f"not JSON-serialisable: {type(v).__name__}")


def canonical_json(value: Any) -> str:
    """RFC 8785 canonical form of a parsed JSON value."""
    return _canon(value)


def attrs_hash(attrs: dict) -> str:
    return hashlib.sha256(canonical_json(attrs).encode("utf-8")).hexdigest()


class AttributeTracker:
    """Queues an attribute snapshot at start and whenever the settings hash changes.

    `read_settings` returns the current attrs dict (supplied by the glue; H3 res 6 cell only).
    valid_from is the detection time. The first check after start always queues: the collector
    answers 200 when unchanged, so a restart is harmless.
    """

    def __init__(self, outbox: Outbox, clock: Any, read_settings: Callable[[], dict]):
        self._ob = outbox
        self._clock = clock
        self._read = read_settings
        self._last_hash: str | None = None

    def check(self) -> bool:
        """Returns True if a snapshot was queued."""
        attrs = self._read()
        h = attrs_hash(attrs)
        if h == self._last_hash:
            return False
        now = self._clock.now().astimezone(UTC).replace(microsecond=0)
        payload = {
            "valid_from": now.isoformat().replace("+00:00", "Z"),
            "schema_version": SCHEMA_VERSION,
            "attrs": attrs,
        }
        self._ob.add("attributes", payload, now)
        self._last_hash = h
        return True
