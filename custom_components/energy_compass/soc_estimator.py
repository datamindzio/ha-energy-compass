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

Anchors come from physical evidence only: a full anchor when charging stops
although it could continue and the cause is not lack of sun, at a pack voltage
no lower than the site's full voltage and held for the full hold, an empty anchor
at the pack voltage that defines 0 %. `state_to_dict` and `state_from_dict`
are the versioned form the Store keeps; `seed_from_template` starts from the
attributes of the template estimator this replaces.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite

from .calibration import COUNTER_SLACK_KWH, MAX_COUNTER_KW
from .engine.models import InputError

_LOGGER = logging.getLogger(__name__)

# The interpolation never integrates a longer gap than this, nor accumulates
# more than the bound, so a missing sample cannot invent energy.
MAX_PENDING_STEP_SECONDS = 120.0
MAX_PENDING_KWH = 2.0
# The counter rise limit grows with the time since the counters last moved, up
# to this much, so a long quiet spell does not let any glitch through.
MAX_COUNTER_STEP_SECONDS = 3600.0
# The estimate shown for a pack that the counters put at the top without a full
# anchor: not 100 %, because nothing proved the pack is full.
UNANCHORED_TOP_FRACTION = 0.999
# Leaving the top by this much ends the confirmation a full anchor gave.
TOP_LEAVE_KWH = 0.1

STATE_VERSION = 1
ANCHOR_KINDS = ("initial", "seed", "full", "full_balance", "empty", "reset")

# Dispatch states in which the inverter is not trying to fill the pack, so a
# stopped charge proves nothing.
NON_CHARGING_STATES = ("HOLD", "CURTAIL", "BASE", "DISCHARGE_GRID")

# Full anchor evidence (DD-3).
MIN_CHARGE_LIMIT_A = 2.0
# The controller zeroes the charge current for the length of a transaction when
# the program direction changes (e.g. CHARGE_PV to HOLD); a running candidate
# lives through a limit below the minimum for this long.
CHARGE_LIMIT_GRACE_SECONDS = 120.0
MIN_TAPER_CURRENT_A = 1.0
# A pack reaches its full voltage only under charge current and relaxes a few
# volts once the charge stops (live: 556 V at the stop, 546 V at rest), so the
# full voltage gates the start of a candidate through a recent peak and the hold
# only needs the pack to stay near it.
FULL_PEAK_WINDOW_SECONDS = 1800.0
FULL_REST_VOLTAGE_FRACTION = 0.97
TAPER_CURRENT_FRACTION = 0.02
MIN_SURPLUS_W = 300.0
SURPLUS_RATED_FRACTION = 0.05
# Where export is capped, the derived surplus (DC PV less AC load) must clear
# the threshold by this share of the PV power: the inverter's conversion
# losses are that large and are not in the difference.
DERIVED_SURPLUS_PV_FRACTION = 0.10
# Export this close to the configured limit (as a fraction) counts as capped.
EXPORT_CAP_FRACTION = 0.95
# The inverter stops grid charging at the program voltage; a reading this close
# below it (as a fraction) counts as having reached it.
TARGET_VOLTAGE_MARGIN = 0.01
MAX_CANDIDATES = 20

# Empty anchor evidence (DD-4).
MIN_DISCHARGE_A = 0.5
LIGHT_LOAD_C = 0.1
STOPPED_VOLTAGE_MARGIN = 0.01
MIN_IMPORT_W = 300.0
# A reading below this share of the 0 % voltage is a dropout, not a pack at 0 %.
MIN_PLAUSIBLE_VOLTAGE_FRACTION = 0.5


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
    # Pack voltage that is 0 %; the site must state it, there is no default.
    empty_voltage_v: float
    # The pack voltage a charge reaches before it stops at full; the site must
    # state it, there is no default. A full candidate starts only within
    # FULL_PEAK_WINDOW_SECONDS of a reading at or above it and continues while
    # the resting pack stays above FULL_REST_VOLTAGE_FRACTION of it. Counters
    # that drifted low must not be able to keep a full pack from correcting them.
    full_min_voltage_v: float
    eta_charge: float = 0.95
    # Largest charge current the pack takes; 0 when unknown (the taper
    # threshold is then the 1 A floor). Rated PV power likewise.
    max_charge_current_a: float = 0.0
    rated_pv_w: float = 0.0
    # Largest grid export the site allows in W; 0 is zero-export, None unknown.
    export_limit_w: float | None = None
    full_hold_seconds: float = 3600.0
    empty_hold_seconds: float = 60.0
    # E1: a full candidate only anchors after this many days since the
    # estimator started, unless the switch is on from day one.
    anchor_on_candidates: bool = False
    candidates_only_days: float = 14.0

    def __post_init__(self) -> None:
        if not (isfinite(self.capacity_kwh) and self.capacity_kwh > 0):
            raise InputError("invalid estimator capacity")
        if not (isfinite(self.eta_charge) and 0 < self.eta_charge <= 1):
            raise InputError("invalid estimator efficiency")
        if not (isfinite(self.empty_voltage_v) and self.empty_voltage_v > 0):
            raise InputError("invalid estimator empty voltage")
        if not (isfinite(self.full_min_voltage_v) and self.full_min_voltage_v > 0):
            raise InputError("invalid estimator full voltage")
        for value in (
            self.max_charge_current_a,
            self.rated_pv_w,
            self.full_hold_seconds,
            self.empty_hold_seconds,
            self.candidates_only_days,
            *(() if self.export_limit_w is None else (self.export_limit_w,)),
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
    # Discharge current limit in force; None unknown.
    discharge_limit_a: float | None = None
    # The plan row in force is an LFP balance hold: it asked for a full pack.
    balance_hold: bool = False


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
    # Grid power in W, positive while importing.
    grid_w: float | None = None
    facts: ControllerFacts | None = None
    # Every source an anchor reads (current, voltage, PV, load, grid) reported
    # recently; a stale one breaks the hold rather than extend it.
    anchor_inputs_fresh: bool = True


@dataclass
class EstimatorState:
    anchor: Anchor
    started_at: datetime
    pending_kwh: float = 0.0
    last_counters: tuple[float, float] | None = None
    # When the counters last moved (not last read): the inverter publishes them
    # in steps, minutes apart, so a step is weighed against the time it covers.
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
    # Last reading at or above the full voltage; never persisted.
    full_voltage_at: datetime | None = field(default=None, compare=False)
    # Since when the charge limit has been below the minimum; never persisted.
    charge_low_since: datetime | None = field(default=None, compare=False)
    candidates: list[dict] = field(default_factory=list)
    empty_candidate_since: datetime | None = None
    empty_candidate_logged: bool = False
    empty_rejected_logged: bool = False
    # Energy between the last full anchor and the empty anchor that followed;
    # shown, never applied.
    measured_capacity_kwh: float | None = None
    # Kind and time of the template anchor this state was seeded from.
    seed: dict | None = None
    # Read from the Store and not stepped since: its stored power is as old as
    # the downtime. Never persisted and not part of the state's identity.
    restored: bool = field(default=False, compare=False)


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
    empty_candidate: bool = False
    measured_capacity_kwh: float | None = None
    # Energy the counters put above the capacity; kept, so leaving the top
    # first spends it, and shown beside the 99.9 % of an unanchored top.
    excess_kwh: float = 0.0
    # A full anchor confirmed the top and the pack has not left it since.
    top_confirmed: bool = False
    # Full evidence is standing right now (a logged, plausible full candidate or
    # balance stop), whether or not it was applied as an anchor.
    full_qualified: bool = False
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
    if state.restored:
        state.restored = False
        if state.last_at is not None and (
            (at - state.last_at).total_seconds() > MAX_PENDING_STEP_SECONDS
        ):
            previous_power = 0.0
    state.last_at = at
    state.last_power_w = 0.0 if power is None else power
    charge, discharge = _number(inputs.charge_kwh), _number(inputs.discharge_kwh)
    if charge is None or discharge is None:
        _break_full_candidate(state)
        _break_empty_candidate(state)
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
        _break_empty_candidate(state)
    changed = state.last_counters is not None and (charge, discharge) != (
        state.last_counters
    )
    if changed or reset or state.last_counters is None:
        state.last_counters_at = at
    state.last_counters = (charge, discharge)
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
    if kind := _full_anchor(state, settings, inputs, charge, discharge):
        events.append(kind)
    if _empty_anchor(state, settings, inputs, charge, discharge):
        events.append("empty")
    return _estimate(state, settings, tuple(events))


def _full_anchor(
    state: EstimatorState,
    settings: EstimatorSettings,
    inputs: Inputs,
    charge: float,
    discharge: float,
) -> str | None:
    """Track the full-anchor evidence (DD-3); the anchor kind when the step anchored."""
    running = state.full_candidate_since is not None
    voltage = _number(inputs.voltage_v)
    if (
        voltage is not None
        and inputs.anchor_inputs_fresh
        and voltage >= settings.full_min_voltage_v
    ):
        state.full_voltage_at = inputs.at
    peaked = (
        state.full_voltage_at is not None
        and 0
        <= (inputs.at - state.full_voltage_at).total_seconds()
        <= FULL_PEAK_WINDOW_SECONDS
    )
    limit = _number(inputs.facts.charge_limit_a) if inputs.facts else None
    if limit is None or limit >= MIN_CHARGE_LIMIT_A:
        state.charge_low_since = None
    elif state.charge_low_since is None:
        state.charge_low_since = inputs.at
    grace = (
        running
        and state.charge_low_since is not None
        and (inputs.at - state.charge_low_since).total_seconds()
        <= CHARGE_LIMIT_GRACE_SECONDS
    )
    evidence = (
        _full_evidence(settings, inputs, running, grace)
        if inputs.anchor_inputs_fresh and (running or peaked)
        else None
    )
    if evidence is None:
        if state.full_candidate_since is not None and _below_full_voltage(
            settings, inputs
        ):
            _LOGGER.info(
                "SOC estimator: full hold broken, below_full_voltage (%s V < %s V)",
                inputs.voltage_v,
                _rest_voltage(settings),
            )
        _break_full_candidate(state)
        return None
    at = inputs.at
    if state.full_candidate_since is None:
        state.full_candidate_since = at
    if (
        state.full_candidate_logged
        or (at - state.full_candidate_since).total_seconds()
        < settings.full_hold_seconds
    ):
        return None
    state.full_candidate_logged = True
    before = _clamp(
        counter_energy(state, settings, charge, discharge) + state.pending_kwh,
        settings,
    )
    anchored = settings.anchor_on_candidates or at - state.started_at >= timedelta(
        days=settings.candidates_only_days
    )
    state.candidates.append(
        {
            "started_at": state.full_candidate_since.isoformat(),
            "qualified_at": at.isoformat(),
            "anchored": anchored,
            "outcome": "anchored" if anchored else "candidate",
            "energy_before_kwh": round(before, 3),
            **evidence,
        }
    )
    del state.candidates[:-MAX_CANDIDATES]
    if not anchored:
        return None
    kind = "full_balance" if evidence["path"] == "balance" else "full"
    anchor = Anchor(kind, at, settings.capacity_kwh, charge, discharge)
    set_anchor(state, anchor, top_confirmed=True)
    state.last_full = anchor
    return kind


def _qualified(state: EstimatorState) -> bool:
    return state.full_candidate_since is not None and state.full_candidate_logged


def _rest_voltage(settings: EstimatorSettings) -> float:
    return FULL_REST_VOLTAGE_FRACTION * settings.full_min_voltage_v


def _below_full_voltage(settings: EstimatorSettings, inputs: Inputs) -> bool:
    voltage = _number(inputs.voltage_v)
    return voltage is not None and voltage < _rest_voltage(settings)


def _break_full_candidate(state: EstimatorState) -> None:
    state.full_candidate_since = None
    state.full_candidate_logged = False


def _empty_anchor(
    state: EstimatorState,
    settings: EstimatorSettings,
    inputs: Inputs,
    charge: float,
    discharge: float,
) -> bool:
    """Track the empty-anchor evidence (DD-4); True when the step anchored."""
    rule = _empty_evidence(settings, inputs) if inputs.anchor_inputs_fresh else None
    if rule is None:
        _break_empty_candidate(state)
        state.empty_rejected_logged = False
        return False
    problem = _empty_implausible(inputs, settings)
    if problem is not None:
        _break_empty_candidate(state)
        if not state.empty_rejected_logged:
            state.empty_rejected_logged = True
            _LOGGER.warning(
                "SOC estimator: no empty anchor, %s (voltage %s V, current %s A)",
                problem,
                inputs.voltage_v,
                inputs.current_a,
            )
        return False
    state.empty_rejected_logged = False
    at = inputs.at
    if state.empty_candidate_since is None:
        state.empty_candidate_since = at
    if (
        state.empty_candidate_logged
        or (at - state.empty_candidate_since).total_seconds()
        < settings.empty_hold_seconds
    ):
        return False
    state.empty_candidate_logged = True
    full = state.last_full
    if full is not None:
        measured = (discharge - full.discharge_kwh) - settings.eta_charge * (
            charge - full.charge_kwh
        )
        if measured > 0:
            state.measured_capacity_kwh = round(measured, 3)
    state.last_full = None
    set_anchor(state, Anchor("empty", at, 0.0, charge, discharge))
    return True


def _empty_implausible(inputs: Inputs, settings: EstimatorSettings) -> str | None:
    """Why an empty reading cannot be believed, or None."""
    voltage = _number(inputs.voltage_v)
    if (
        voltage is None
        or voltage < MIN_PLAUSIBLE_VOLTAGE_FRACTION * settings.empty_voltage_v
    ):
        return "the pack voltage reads as a dropout"
    current, power = _number(inputs.current_a), _number(inputs.power_w)
    if (
        current is not None
        and power is not None
        and current >= MIN_DISCHARGE_A
        and power < 0
    ):
        return "the current says discharging but the power says charging"
    return None


def _break_empty_candidate(state: EstimatorState) -> None:
    state.empty_candidate_since = None
    state.empty_candidate_logged = False


def _empty_evidence(settings: EstimatorSettings, inputs: Inputs) -> str | None:
    """The rule that holds ("light_load", "stopped"), or None.

    Light load: discharging at no more than 0.1 C with the voltage at or below
    the 0 % voltage; a heavier load sags the voltage and proves nothing.
    Stopped: the discharge ceased although the house imports from the grid and
    the controller allows discharging, with the voltage within 1 % above the
    0 % voltage.
    """
    current, voltage = _number(inputs.current_a), _number(inputs.voltage_v)
    if current is None or voltage is None:
        return None
    power = _number(inputs.power_w)
    discharge_w = power if power is not None else current * voltage
    light = LIGHT_LOAD_C * settings.capacity_kwh * 1000
    if (
        current >= MIN_DISCHARGE_A
        and discharge_w <= light
        and voltage <= settings.empty_voltage_v
    ):
        return "light_load"
    facts, grid = inputs.facts, _number(inputs.grid_w)
    if facts is None or grid is None:
        return None
    limit = _number(facts.discharge_limit_a)
    if (
        -MIN_DISCHARGE_A < current < MIN_DISCHARGE_A
        and grid >= MIN_IMPORT_W
        and limit is not None
        and limit >= MIN_CHARGE_LIMIT_A
        and not facts.target_reached
        and facts.state not in ("HOLD", "CURTAIL")
        and voltage <= settings.empty_voltage_v * (1 + STOPPED_VOLTAGE_MARGIN)
    ):
        return "stopped"
    return None


def _full_evidence(
    settings: EstimatorSettings,
    inputs: Inputs,
    continuing: bool = False,
    low_limit_grace: bool = False,
) -> dict | None:
    """The values behind a full candidate, or None while any condition fails.

    0. the pack voltage is at least FULL_REST_VOLTAGE_FRACTION of
       `full_min_voltage_v` (the caller starts a candidate only within
       FULL_PEAK_WINDOW_SECONDS of a reading at the full voltage itself);
    1. the battery takes (almost) no charge and gives none;
    2. charging was possible: a charge limit of at least 2 A, no controller
       target that explains the stop, and a dispatch state that charges (not
       HOLD, CURTAIL, BASE or DISCHARGE_GRID; CHARGE_GRID only with a Grid
       program below its voltage and a grid charge current of 2 A or more);
    3. the cause is not lack of sun: power measured leaving to the grid by at
       least max(300 W, 5 % of the rated PV) while the pack takes none; where
       export is impossible or capped, the derived surplus (PV less load and the
       charge still taken) at least 300 W plus 10 % of the PV power; or grid
       charging requested and still short of its voltage.

    In a balance row the plan asked for a full pack: charging that stops at the
    Grid program's voltage is itself the evidence (path "balance").

    HOLD ends a candidate that did not start in it, unless `continuing`: the
    controller keeps the charge path open for a running candidate, so the
    charge limit in force (at least 2 A, as above) shows it did. `low_limit_grace`
    lets a running candidate through a numeric limit below 2 A (the controller's
    own transient write); it never starts one. HOLD's Grid
    program only holds the SOC, so its voltage is no target and requested grid
    charging is no evidence there; only the export and surplus paths count.
    """
    facts = inputs.facts
    current, voltage = _number(inputs.current_a), _number(inputs.voltage_v)
    pv, load = _number(inputs.pv_w), _number(inputs.load_w)
    if facts is None or current is None or voltage is None:
        return None
    if voltage < _rest_voltage(settings):
        return None
    taper = max(
        MIN_TAPER_CURRENT_A, TAPER_CURRENT_FRACTION * settings.max_charge_current_a
    )
    if not -taper <= current <= taper:
        return None
    charge_limit = _number(facts.charge_limit_a)
    if charge_limit is None or (
        charge_limit < MIN_CHARGE_LIMIT_A and not (continuing and low_limit_grace)
    ):
        return None
    held_open = continuing and facts.state == "HOLD"
    if facts.state in NON_CHARGING_STATES and not held_open:
        return None
    grid_charging = facts.program_direction == "Grid"
    target = _number(facts.program_voltage_v)
    at_target = (
        not held_open
        and grid_charging
        and target is not None
        and voltage >= target * (1 - TARGET_VOLTAGE_MARGIN)
    )
    grid = _number(inputs.grid_w)
    export = None if grid is None else max(-grid, 0.0)
    # What the battery already takes is not surplus: only PV that goes to
    # export or is curtailed shows a pack that cannot take more.
    surplus = (
        None if pv is None or load is None else pv - load - max(-current, 0.0) * voltage
    )
    if facts.balance_hold and at_target and facts.state is not None:
        path = "balance"
    elif (
        facts.target_reached
        or at_target
        or facts.state == "CHARGE_GRID"
        and not (
            grid_charging
            and target is not None
            and facts.grid_charge_a >= MIN_CHARGE_LIMIT_A
        )
    ):
        return None
    else:
        threshold = max(MIN_SURPLUS_W, SURPLUS_RATED_FRACTION * settings.rated_pv_w)
        export_limit = settings.export_limit_w
        capped = export_limit is not None and (
            export_limit <= 0
            or (export is not None and export >= EXPORT_CAP_FRACTION * export_limit)
        )
        if export is not None and export >= threshold:
            path = "export"
        elif (
            capped
            and surplus is not None
            and pv is not None
            and surplus >= MIN_SURPLUS_W + DERIVED_SURPLUS_PV_FRACTION * pv
        ):
            path = "surplus"
        elif (
            grid_charging
            and not held_open
            and facts.state is not None
            and facts.grid_charge_a >= MIN_CHARGE_LIMIT_A
        ):
            path = "grid"
        else:
            return None
    return {
        "path": path,
        "voltage_v": voltage,
        "current_a": current,
        "surplus_w": surplus,
        "export_w": export,
        "charge_limit_a": charge_limit,
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
    seconds = (at - state.last_counters_at).total_seconds()
    hours = min(max(seconds, 0.0), MAX_COUNTER_STEP_SECONDS) / 3600
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
    elif state.top_confirmed and total > capacity:
        # A pack proven full stores nothing more: the rest is losses and float.
        # The anchor stays a full one from its time, only its counters move up.
        refreshed = Anchor(anchor.kind, anchor.at, capacity, charge, discharge)
        if state.last_full is anchor:
            state.last_full = refreshed
        state.anchor = anchor = refreshed
        state.pending_kwh = 0.0
        total = capacity
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
        empty_candidate=state.empty_candidate_since is not None,
        measured_capacity_kwh=state.measured_capacity_kwh,
        excess_kwh=max(0.0, total - capacity),
        top_confirmed=state.top_confirmed,
        full_qualified=_qualified(state),
        events=events,
    )


def _clamp(value: float, settings: EstimatorSettings) -> float:
    return min(settings.capacity_kwh, max(0.0, value))


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if isfinite(value) else None


def state_to_dict(state: EstimatorState) -> dict:
    """JSON-serialisable form for the Store, versioned.

    The hold timers of the anchor candidates are not kept: a restart breaks a
    hold, so a long downtime can never complete one.
    """
    return {
        "version": STATE_VERSION,
        "anchor": _anchor_to_dict(state.anchor),
        "started_at": state.started_at.isoformat(),
        "pending_kwh": state.pending_kwh,
        "last_counters": (
            None if state.last_counters is None else list(state.last_counters)
        ),
        "last_counters_at": _iso(state.last_counters_at),
        "last_at": _iso(state.last_at),
        "last_power_w": state.last_power_w,
        "suspect": list(state.suspect),
        "top_confirmed": state.top_confirmed,
        "last_full": (
            None if state.last_full is None else _anchor_to_dict(state.last_full)
        ),
        "candidates": [dict(candidate) for candidate in state.candidates],
        "measured_capacity_kwh": state.measured_capacity_kwh,
        "seed": None if state.seed is None else dict(state.seed),
    }


def state_from_dict(raw) -> EstimatorState | None:
    """Rebuild a state from the Store; None for anything malformed.

    The counters are read again on the next step: the stored ones only tell a
    reset or a glitch apart from the normal rise made while Home Assistant was
    down.
    """
    try:
        if not isinstance(raw, Mapping) or raw.get("version") != STATE_VERSION:
            return None
        last = raw["last_counters"]
        if last is not None:
            if not isinstance(last, list | tuple) or len(last) != 2:
                return None
            last = (_required(last[0]), _required(last[1]))
        suspect = raw["suspect"]
        if not isinstance(suspect, list | tuple) or len(suspect) != 2:
            return None
        candidates = raw["candidates"]
        if (
            not isinstance(candidates, list)
            or len(candidates) > MAX_CANDIDATES
            or not all(_plain_dict(candidate) for candidate in candidates)
        ):
            return None
        seed = raw["seed"]
        if seed is not None and not _plain_dict(seed):
            return None
        last_full = raw["last_full"]
        measured = raw["measured_capacity_kwh"]
        if not isinstance(raw["top_confirmed"], bool):
            return None
        return EstimatorState(
            _anchor_from_dict(raw["anchor"]),
            _stamp(raw["started_at"]),
            pending_kwh=_required(raw["pending_kwh"]),
            last_counters=last,
            last_counters_at=_optional_stamp(raw["last_counters_at"]),
            last_at=_optional_stamp(raw["last_at"]),
            last_power_w=_required(raw["last_power_w"]),
            suspect=(_optional_number(suspect[0]), _optional_number(suspect[1])),
            top_confirmed=raw["top_confirmed"],
            last_full=None if last_full is None else _anchor_from_dict(last_full),
            candidates=[dict(candidate) for candidate in candidates],
            measured_capacity_kwh=(None if measured is None else _required(measured)),
            seed=None if seed is None else dict(seed),
            restored=True,
        )
    except KeyError, TypeError, ValueError:
        return None


def seed_from_template(attributes: Mapping, *, now: datetime) -> EstimatorState | None:
    """State seeded from the attributes of the template estimator it replaces.

    Reads `anchor_kwh`, `anchor_charge`, `anchor_discharge`, `anchor_kind`,
    `anchor_at` and `pending_kwh`. The anchor is kept as the numbers say but is
    never trusted as a full anchor: its kind becomes "seed", the template's kind
    and time are kept in `seed`. None when any anchor value is missing or not
    a plausible number. The counters are read at the first step.
    """
    try:
        energy = _seed_number(attributes["anchor_kwh"])
        charge = _seed_number(attributes["anchor_charge"])
        discharge = _seed_number(attributes["anchor_discharge"])
        kind = attributes["anchor_kind"]
        anchored_at = attributes["anchor_at"]
        if energy < 0 or charge < 0 or discharge < 0:
            return None
        if not isinstance(kind, str) or not isinstance(anchored_at, str):
            return None
        stamp = datetime.fromisoformat(anchored_at)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        pending = _seed_number(attributes.get("pending_kwh", 0.0))
    except KeyError, TypeError, ValueError:
        return None
    return EstimatorState(
        Anchor("seed", stamp, energy, charge, discharge),
        now,
        pending_kwh=min(MAX_PENDING_KWH, max(-MAX_PENDING_KWH, pending)),
        seed={"kind": kind, "at": stamp.isoformat()},
    )


def _anchor_to_dict(anchor: Anchor) -> dict:
    return {
        "kind": anchor.kind,
        "at": anchor.at.isoformat(),
        "energy_kwh": anchor.energy_kwh,
        "charge_kwh": anchor.charge_kwh,
        "discharge_kwh": anchor.discharge_kwh,
    }


def _anchor_from_dict(raw) -> Anchor:
    if raw["kind"] not in ANCHOR_KINDS:
        raise ValueError("unknown anchor kind")
    return Anchor(
        raw["kind"],
        _stamp(raw["at"]),
        _required(raw["energy_kwh"]),
        _required(raw["charge_kwh"]),
        _required(raw["discharge_kwh"]),
    )


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _stamp(raw) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed


def _optional_stamp(raw) -> datetime | None:
    return None if raw is None else _stamp(raw)


def _required(value) -> float:
    number = _number(value)
    if number is None:
        raise ValueError("not a finite number")
    return number


def _optional_number(value) -> float | None:
    return None if value is None else _required(value)


def _seed_number(value) -> float:
    """A template attribute: a number, or the text of one."""
    if isinstance(value, str):
        value = float(value)
    return _required(value)


def _plain_dict(value) -> bool:
    """A JSON object of plain values, so the Store can always write it back."""
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(key, str)
        and (
            item is None
            or isinstance(item, str | bool)
            or (isinstance(item, int | float) and isfinite(item))
        )
        for key, item in value.items()
    )
