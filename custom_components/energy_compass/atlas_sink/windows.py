"""5-minute window aggregator (S-004). Never reads the wall clock."""

from bisect import insort
from dataclasses import dataclass, field
from datetime import UTC, datetime

WINDOW_S = 300

# ADR-0019 Context / MetricValues (api/openapi.yaml): only these power keys have a `_max`
# variant in the contract; every other `*_w` key emits `_avg` only (LESSONS
# producer-keys-by-rule-never-checked-against-schema).
_MAX_KEYS = frozenset({"pv_w", "load_w"})


@dataclass(frozen=True)
class Window:
    window_start: datetime
    samples: int
    values: dict[str, float]


@dataclass(order=True)
class _Sample:
    ts: float
    seq: int
    values: dict[str, float] = field(compare=False)


def _out_key(key: str) -> str:
    return f"{key}_last" if key.endswith("_pct") else key


def _require_aware(ts: datetime) -> None:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(
            "naive datetime: timezone-aware required for UTC window alignment"
        )


class Aggregator:
    """Feed samples, close windows.

    A value holds until the next sample of the same key; the last one holds to window end.
    Power keys (``*_w``) give time-weighted ``_avg`` and ``_max``; other keys give the last
    sample in the window. Windows without samples are never produced.
    """

    def __init__(self) -> None:
        self._samples: list[_Sample] = []
        self._seq = 0
        self._carry: dict[str, float] = {}  # latest value per key before the cutoff
        self._cutoff = float("-inf")  # windows starting before this are closed

    def feed(self, ts: datetime, values: dict[str, float]) -> None:
        _require_aware(ts)
        t = ts.timestamp()
        if t < self._cutoff:
            return  # its window is already closed
        self._seq += 1
        insort(self._samples, _Sample(t, self._seq, dict(values)))

    def close(self, now: datetime) -> list[Window]:
        _require_aware(now)
        cutoff = now.timestamp() // WINDOW_S * WINDOW_S
        starts = sorted(
            {s.ts // WINDOW_S * WINDOW_S for s in self._samples if s.ts < cutoff}
        )
        windows = [self._build(start) for start in starts]
        rest = []
        for s in self._samples:
            if s.ts < cutoff:
                self._carry.update(s.values)
            else:
                rest.append(s)
        self._samples = rest
        self._cutoff = max(self._cutoff, cutoff)
        return windows

    def _build(self, start: float) -> Window:
        end = start + WINDOW_S
        before: dict[str, float] = dict(self._carry)
        inside: list[_Sample] = []
        for s in self._samples:
            if s.ts < start:
                before.update(s.values)
            elif s.ts < end:
                inside.append(s)
        keys = set(before) | {k for s in inside for k in s.values}
        out: dict[str, float] = {}
        for key in sorted(keys):
            if key.endswith("_w"):
                self._power(key, start, end, before.get(key), inside, out)
            else:
                for s in inside:
                    if key in s.values:
                        out[_out_key(key)] = s.values[key]
        return Window(datetime.fromtimestamp(start, UTC), len(inside), out)

    @staticmethod
    def _power(
        key: str,
        start: float,
        end: float,
        carried: float | None,
        inside: list[_Sample],
        out: dict[str, float],
    ) -> None:
        t, value = start, carried
        integral = covered = 0.0
        peak: float | None = None
        for s in inside:
            if key not in s.values:
                continue
            if value is not None:
                integral += value * (s.ts - t)
                covered += s.ts - t
                if s.ts > start:
                    peak = value if peak is None else max(peak, value)
            t, value = s.ts, s.values[key]
        if value is None:
            return
        integral += value * (end - t)
        covered += end - t
        peak = value if peak is None else max(peak, value)
        out[f"{key}_avg"] = integral / covered
        if key in _MAX_KEYS:
            out[f"{key}_max"] = peak
