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

from dataclasses import dataclass, field
from datetime import datetime, timedelta
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

# Full anchor evidence (DD-3).
MIN_CHARGE_LIMIT_A = 2.0
MIN_TAPER_CURRENT_A = 1.0
TAPER_CURRENT_FRACTION = 0.02
MIN_SURPLUS_W = 300.0
SURPLUS_RATED_FRACTION = 0.05
# The inverter stops grid charging at the program voltage; a reading this close
# below it (as a fraction) counts as having reached it.
TARGET_VOLTAGE_MARGIN = 0.01
MAX_CANDIDATES = 20


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
    # Largest charge current the pack takes; 0 when unknown (the taper
    # threshold is then the 1 A floor). Rated PV power likewise.
    max_charge_current_a: float = 0.0
    rated_pv_w: float = 0.0
    full_hold_seconds: float = 600.0
    # E1: a full candidate only anchors after this many days since the
    # estimator started, unless the switch is on from day one.
    anchor_on_candidates: bool = False
    candidates_only_days: float = 14.0

    def __post_init__(self) -> None:
        if not (isfinite(self.capacity_kwh) and self.capacity_kwh > 0):
            raise InputError("invalid estimator capacity")
        if not (isfinite(self.eta_charge) and 0 < self.eta_charge <= 1):
            raise InputError("invalid estimator efficiency")
        for value in (
            self.max_charge_current_a,
            self.rated_pv_w,
            self.full_hold_seconds,
            self.candidates_only_days,
        ):
            if not (isfinite(value) and value >= 0):
                raise InputError("invalid estimator setting")


@dataclass(frozen=True)
class ControllerFacts:
    """What the controller and the inverter are set to do, as the site reports it.

    The Home Assistant layer fills it; None means unknown, which never counts
    as evidence. Voltages are in the pack-voltage scale of `Inputs.voltage_v`.
    """

    # Charge current limit in force: the controller's, or the inverter's own
    # limit entity without the native controller.
    charge_limit_a: float | None
    # Grid charge current the controller requested.
    grid_charge_a: float = 0.0
    # Dispatch state of the native controller; None without it.
    state: str | None = None
    # The controller reports its own SOC or voltage target met.
    target_reached: bool = False
    # Direction (Disabled, Grid, Sell) and voltage target of the active TOU program.
    program_direction: str | None = None
    program_voltage_v: float | None = None


@dataclass(frozen=True)
class Inputs:
    """One reading of the sources; None where a source is unavailable."""

    at: datetime
    charge_kwh: float | None
    discharge_kwh: float | None
    # Battery power in W, positive while discharging.
    power_w: float | None = None
    voltage_v: float | None = None
    # Battery current in A, positive while discharging.
    current_a: float | None = None
    pv_w: float | None = None
    load_w: float | None = None
    facts: ControllerFacts | None = None


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
    # Last full anchor, kept to measure the capacity at the next empty anchor.
    last_full: Anchor | None = None
    full_candidate_since: datetime | None = None
    full_candidate_logged: bool = False
    candidates: list[dict] = field(default_factory=list)


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
    full_candidate: bool = False
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
        _break_full_candidate(state)
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
        state.last_full = None
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
    if _full_anchor(state, settings, inputs, charge, discharge):
        events.append("full")
    return _estimate(state, settings, tuple(events))


def _full_anchor(
    state: EstimatorState,
    settings: EstimatorSettings,
    inputs: Inputs,
    charge: float,
    discharge: float,
) -> bool:
    """Track the full-anchor evidence (DD-3); True when the step anchored."""
    evidence = _full_evidence(settings, inputs)
    if evidence is None:
        _break_full_candidate(state)
        return False
    at = inputs.at
    if state.full_candidate_since is None:
        state.full_candidate_since = at
    if (
        state.full_candidate_logged
        or (at - state.full_candidate_since).total_seconds()
        < settings.full_hold_seconds
    ):
        return False
    state.full_candidate_logged = True
    anchored = settings.anchor_on_candidates or at - state.started_at >= timedelta(
        days=settings.candidates_only_days
    )
    state.candidates.append(
        {
            "started_at": state.full_candidate_since.isoformat(),
            "qualified_at": at.isoformat(),
            "anchored": anchored,
            "energy_before_kwh": round(
                _clamp(
                    counter_energy(state, settings, charge, discharge)
                    + state.pending_kwh,
                    settings,
                ),
                3,
            ),
            **evidence,
        }
    )
    del state.candidates[:-MAX_CANDIDATES]
    if not anchored:
        return False
    anchor = Anchor("full", at, settings.capacity_kwh, charge, discharge)
    set_anchor(state, anchor, top_confirmed=True)
    state.last_full = anchor
    return True


def _break_full_candidate(state: EstimatorState) -> None:
    state.full_candidate_since = None
    state.full_candidate_logged = False


def _full_evidence(settings: EstimatorSettings, inputs: Inputs) -> dict | None:
    """The values behind a full candidate, or None while any condition fails.

    1. the battery takes (almost) no charge and gives none;
    2. charging was possible: a charge limit of at least 2 A and no controller
       target that explains the stop;
    3. the cause is not lack of sun: PV above the load and the charge the pack
       still takes by the surplus threshold, or grid charging requested and
       still short of its voltage.
    """
    facts = inputs.facts
    current, voltage = _number(inputs.current_a), _number(inputs.voltage_v)
    pv, load = _number(inputs.pv_w), _number(inputs.load_w)
    if facts is None or current is None or voltage is None:
        return None
    taper = max(
        MIN_TAPER_CURRENT_A, TAPER_CURRENT_FRACTION * settings.max_charge_current_a
    )
    if not -taper <= current <= taper:
        return None
    limit = _number(facts.charge_limit_a)
    if limit is None or limit < MIN_CHARGE_LIMIT_A:
        return None
    if facts.target_reached or facts.state == "DISCHARGE_GRID":
        return None
    grid_charging = facts.program_direction == "Grid"
    target = _number(facts.program_voltage_v)
    if (
        grid_charging
        and target is not None
        and voltage >= target * (1 - TARGET_VOLTAGE_MARGIN)
    ):
        return None
    # What the battery already takes is not surplus: only PV that goes to
    # export or is curtailed shows a pack that cannot take more.
    surplus = (
        None if pv is None or load is None else pv - load - max(-current, 0.0) * voltage
    )
    threshold = max(MIN_SURPLUS_W, SURPLUS_RATED_FRACTION * settings.rated_pv_w)
    if surplus is not None and surplus >= threshold:
        path = "surplus"
    elif grid_charging and facts.grid_charge_a >= MIN_CHARGE_LIMIT_A:
        path = "grid"
    else:
        return None
    return {
        "path": path,
        "voltage_v": voltage,
        "current_a": current,
        "surplus_w": surplus,
        "charge_limit_a": limit,
        "grid_charge_a": facts.grid_charge_a,
        "controller_state": facts.state,
    }


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
        full_candidate=state.full_candidate_since is not None,
        events=events,
    )


def _clamp(value: float, settings: EstimatorSettings) -> float:
    return min(settings.capacity_kwh, max(0.0, value))


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if isfinite(value) else None
