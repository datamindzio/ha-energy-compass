"""Battery energy estimated from lifetime charge and discharge counters.

Pure state machine, no Home Assistant imports. The energy above the pack's
0 % point is carried from an anchor (a known energy at known counter values):

    E = anchor.energy - (discharge - anchor.discharge)
        + eta * (charge - anchor.charge) + pending

`pending` integrates the battery power between counter steps, so the value
moves continuously although the counters report in coarse steps; it restarts
at zero whenever a counter changes. The result is clamped to [0, capacity], but
clamping never creates an anchor and the stored anchor is never moved by it:
only physical evidence re-anchors. A counter that goes down re-anchors at the
current energy, and a rise faster than any battery delivers is ignored as a
glitch, as the calibration counters are treated.
"""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from .calibration import COUNTER_SLACK_KWH, MAX_COUNTER_KW
from .engine.models import InputError

# The interpolation never integrates a longer gap than this, nor accumulates
# more than the bound, so a missing sample cannot invent energy.
MAX_PENDING_STEP_SECONDS = 120.0
MAX_PENDING_KWH = 2.0
# The estimate shown for a pack that the counters put at the top without a full
# anchor: not 100 %, because nothing proved the pack is full.
UNANCHORED_TOP_FRACTION = 0.999
# Leaving the top by this much ends the confirmation a full anchor gave.
TOP_LEAVE_KWH = 0.1


@dataclass(frozen=True)
class Anchor:
    """A known energy at known counter values."""

    kind: str
    at: datetime
    energy_kwh: float
    charge_kwh: float
    discharge_kwh: float


@dataclass(frozen=True)
class EstimatorSettings:
    capacity_kwh: float
    eta_charge: float = 0.95

    def __post_init__(self) -> None:
        if not (isfinite(self.capacity_kwh) and self.capacity_kwh > 0):
            raise InputError("invalid estimator capacity")
        if not (isfinite(self.eta_charge) and 0 < self.eta_charge <= 1):
            raise InputError("invalid estimator efficiency")


@dataclass(frozen=True)
class Inputs:
    """One reading of the sources; None where a source is unavailable."""

    at: datetime
    charge_kwh: float | None
    discharge_kwh: float | None
    # Battery power in W, positive while discharging.
    power_w: float | None = None


@dataclass
class EstimatorState:
    anchor: Anchor
    started_at: datetime
    pending_kwh: float = 0.0
    last_counters: tuple[float, float] | None = None
    last_counters_at: datetime | None = None
    last_at: datetime | None = None
    last_power_w: float = 0.0
    # A counter value rejected as a glitch; seen twice in a row it is a re-base.
    suspect: tuple[float | None, float | None] = (None, None)
    top_confirmed: bool = False


@dataclass(frozen=True)
class Estimate:
    """What one step produced; energy is None while the counters are unavailable."""

    energy_kwh: float | None
    soc_fraction: float | None
    available: bool
    unanchored_top: bool = False
    anchor_kind: str = ""
    anchor_at: datetime | None = None
    throughput_since_anchor_kwh: float = 0.0
    pending_kwh: float = 0.0
    events: tuple[str, ...] = ()


def initial_state(
    settings: EstimatorSettings,
    at: datetime,
    charge_kwh: float,
    discharge_kwh: float,
    soc_fraction: float,
) -> EstimatorState:
    """Start from a SOC the user states, shown as unanchored until a real anchor."""
    fraction = min(1.0, max(0.0, soc_fraction))
    anchor = Anchor(
        "initial",
        at,
        fraction * settings.capacity_kwh,
        float(charge_kwh),
        float(discharge_kwh),
    )
    return EstimatorState(
        anchor,
        at,
        last_counters=(float(charge_kwh), float(discharge_kwh)),
        last_counters_at=at,
        last_at=at,
    )


def set_anchor(
    state: EstimatorState, anchor: Anchor, *, top_confirmed: bool = False
) -> None:
    """Replace the anchor; the interpolation restarts from it."""
    state.anchor = anchor
    state.pending_kwh = 0.0
    state.top_confirmed = top_confirmed
    state.suspect = (None, None)


def counter_energy(
    state: EstimatorState,
    settings: EstimatorSettings,
    charge_kwh: float,
    discharge_kwh: float,
) -> float:
    """Unclamped energy from the anchor and the counters, without pending."""
    anchor = state.anchor
    return (
        anchor.energy_kwh
        - (discharge_kwh - anchor.discharge_kwh)
        + settings.eta_charge * (charge_kwh - anchor.charge_kwh)
    )


def step(
    state: EstimatorState, settings: EstimatorSettings, inputs: Inputs
) -> Estimate:
    """Advance the estimator to `inputs.at` and return the estimate."""
    at = inputs.at
    if state.last_at is not None and at <= state.last_at:
        return _estimate(state, settings, ())
    events: list[str] = []
    power = _number(inputs.power_w)
    dt = (
        min(max((at - state.last_at).total_seconds(), 0.0), MAX_PENDING_STEP_SECONDS)
        if state.last_at is not None
        else 0.0
    )
    previous_power = state.last_power_w
    state.last_at = at
    state.last_power_w = 0.0 if power is None else power
    charge, discharge = _number(inputs.charge_kwh), _number(inputs.discharge_kwh)
    if charge is None or discharge is None:
        return Estimate(
            None,
            None,
            False,
            anchor_kind=state.anchor.kind,
            anchor_at=state.anchor.at,
        )
    charge, discharge, basis = _accept_counters(state, charge, discharge, at)
    reset = basis is not None
    if basis is not None:
        energy = _clamp(
            counter_energy(state, settings, *basis) + state.pending_kwh, settings
        )
        set_anchor(
            state,
            Anchor("reset", at, energy, charge, discharge),
        )
        events.append("reset")
    changed = state.last_counters is not None and (charge, discharge) != (
        state.last_counters
    )
    state.last_counters = (charge, discharge)
    state.last_counters_at = at
    if changed or reset:
        state.pending_kwh = 0.0
    else:
        moved = (
            -(
                previous_power
                if previous_power > 0
                else settings.eta_charge * previous_power
            )
            * dt
            / 3_600_000
        )
        state.pending_kwh = min(
            MAX_PENDING_KWH, max(-MAX_PENDING_KWH, state.pending_kwh + moved)
        )
    return _estimate(state, settings, tuple(events))


def _accept_counters(
    state: EstimatorState, charge: float, discharge: float, at: datetime
) -> tuple[float, float, tuple[float, float] | None]:
    """Counters to use, plus the counters the energy is read at when one was reset.

    A counter that went down, or whose glitch value persisted, is read at its
    last accepted value for the re-anchoring; one that moved normally in the
    same reading counts its step.
    """
    if state.last_counters is None or state.last_counters_at is None:
        return charge, discharge, None
    hours = max((at - state.last_counters_at).total_seconds(), 0.0) / 3600
    limit = MAX_COUNTER_KW * hours + COUNTER_SLACK_KWH
    accepted = []
    basis = []
    suspects = []
    reset = False
    for index, value in enumerate((charge, discharge)):
        last = state.last_counters[index]
        suspect = None
        basis_value = value
        if value < last:
            reset = True
            basis_value = last
        elif value - last > limit:
            if state.suspect[index] == value:
                reset = True
                basis_value = last
            else:
                suspect = value
                value = basis_value = last
        accepted.append(value)
        basis.append(basis_value)
        suspects.append(suspect)
    state.suspect = (suspects[0], suspects[1])
    return accepted[0], accepted[1], (basis[0], basis[1]) if reset else None


def _estimate(
    state: EstimatorState, settings: EstimatorSettings, events: tuple[str, ...]
) -> Estimate:
    anchor = state.anchor
    if state.last_counters is None:
        return Estimate(None, None, False, anchor_kind=anchor.kind, anchor_at=anchor.at)
    charge, discharge = state.last_counters
    total = counter_energy(state, settings, charge, discharge) + state.pending_kwh
    capacity = settings.capacity_kwh
    if total < capacity - TOP_LEAVE_KWH:
        state.top_confirmed = False
    unanchored_top = total >= capacity and not state.top_confirmed
    energy = (
        capacity * UNANCHORED_TOP_FRACTION
        if unanchored_top
        else _clamp(total, settings)
    )
    return Estimate(
        energy,
        energy / capacity,
        True,
        unanchored_top=unanchored_top,
        anchor_kind=anchor.kind,
        anchor_at=anchor.at,
        throughput_since_anchor_kwh=(
            (charge - anchor.charge_kwh) + (discharge - anchor.discharge_kwh)
        )
        / 2,
        pending_kwh=state.pending_kwh,
        events=events,
    )


def _clamp(value: float, settings: EstimatorSettings) -> float:
    return min(settings.capacity_kwh, max(0.0, value))


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if isfinite(value) else None
