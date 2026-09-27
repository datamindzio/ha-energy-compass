"""Peak-period reserve: soft SOC floor ahead of each expensive-price run.

Pure math over plain sequences (no `models.py` import), like `autonomy.py`.
A run is a maximal sequence of slots whose buy price is above `cheap_price`.
For a run with a positive deficit, the energy at the end of the cheap slot
right before it, and at the end of every slot inside it, must cover the
remaining deficit of the run plus `margin_kwh`. The margin stays in the pack
when the forecast is right; it absorbs load above the forecast otherwise.
"""

from dataclasses import dataclass
from statistics import median

_EPSILON = 1e-9


@dataclass(frozen=True)
class PeakReserveResult:
    """Per-slot target (0.0 = none) and window id (-1 = none)."""

    targets_kwh: tuple[float, ...]
    window_ids: tuple[int, ...]


def _runs(buy_per_kwh: tuple[float, ...], cheap_price: float) -> list[tuple[int, int]]:
    """(first, last) index of every maximal run of expensive slots."""
    runs: list[tuple[int, int]] = []
    start = None
    for index, price in enumerate(buy_per_kwh):
        expensive = price > cheap_price + _EPSILON
        if expensive and start is None:
            start = index
        elif not expensive and start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(buy_per_kwh) - 1))
    return runs


def peak_reserve_targets(
    buy_per_kwh: tuple[float, ...],
    load_kwh: tuple[float, ...],
    pv_kwh: tuple[float, ...],
    *,
    cheap_price: float,
    reserve_kwh: float,
    usable_capacity_kwh: float,
    eta_discharge: float,
    margin_kwh: float,
) -> PeakReserveResult:
    """Per-slot floor `reserve + margin + remaining run deficit / eta_discharge`.

    Deficits are `max(0, load - pv)` per slot, so a surplus slot inside a run
    never offsets a deficit elsewhere in it. Targets are clamped to the usable
    capacity. Runs without a deficit and a non-positive margin get no floor.
    """
    count = len(buy_per_kwh)
    if not (len(load_kwh) == len(pv_kwh) == count):
        raise ValueError("buy_per_kwh, load_kwh and pv_kwh must have equal length")
    if eta_discharge <= 0:
        raise ValueError("eta_discharge must be strictly positive")
    targets = [0.0] * count
    window_ids = [-1] * count
    if margin_kwh <= 0:
        return PeakReserveResult(tuple(targets), tuple(window_ids))

    def floor(remaining: float) -> float:
        return min(
            usable_capacity_kwh,
            reserve_kwh + margin_kwh + max(0.0, remaining) / eta_discharge,
        )

    window_id = 0
    for first, last in _runs(buy_per_kwh, cheap_price):
        deficits = [max(0.0, load_kwh[i] - pv_kwh[i]) for i in range(first, last + 1)]
        remaining = sum(deficits)
        if remaining <= _EPSILON:
            continue
        if first > 0:
            targets[first - 1] = floor(remaining)
            window_ids[first - 1] = window_id
        for offset, index in enumerate(range(first, last + 1)):
            remaining -= deficits[offset]
            targets[index] = floor(remaining)
            window_ids[index] = window_id
        window_id += 1
    return PeakReserveResult(tuple(targets), tuple(window_ids))


def peak_reserve_weight(buy_per_kwh: tuple[float, ...], *, cheap_price: float) -> float:
    """Penalty per kWh below the floor: median expensive price minus the cheap price."""
    expensive = [price for price in buy_per_kwh if price > cheap_price + _EPSILON]
    if not expensive:
        return 0.0
    return max(0.0, median(expensive) - cheap_price)
