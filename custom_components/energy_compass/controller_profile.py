"""Pure Deye profile: the decision, the write plan and the register layout.

No Home Assistant imports. Every rule is a port of the 0.1.38 blueprint templates (0.1.37 plus #41 and #46)
(`tools/deye_controller/build.py` DECISION, BASE_DECISION, DIFF, GUARD,
CAP_ONLY_REPLAN, WRITE) and is proven against the frozen blueprint by
`tests/test_controller_native_parity.py`. The order of operations, rounding modes
and tolerances are those of the templates on purpose; change them only together
with a new oracle.

All Deye-specific numbers live here, so the executor and the options stay free of
inverter knowledge.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, NamedTuple

from .controller import is_number as _finite
from .controller import timestamp as _timestamp

TELEMETRY_MAX_AGE_SECONDS = 30
PACK_VOLTAGE_RANGE = (400, 610)
APPROVED_TOU_VOLTAGE_RANGE = (49.5, 56.0)
BASE_SOC = 10
BASE_VOLTAGE = 49.6
VOLTAGE_TOLERANCE = 0.00001
SOC_LATCH_MARGIN_KWH = 0.05
# Pack volts x 10 at the deciles of SOC; the last knot differs by direction.
CHARGE_KNOTS = (496, 512, 515, 520, 522, 523, 528, 531, 536, 584)
DISCHARGE_KNOTS = (496, 512, 515, 520, 522, 523, 528, 531, 536, 544)
SELECT_RAW = {"Disabled": 0, "Grid": 1, "Sell": 32}
SELECT_NAMES = {raw: name for name, raw in SELECT_RAW.items()}
UNSUPPORTED_SELECT = "unsupported"
READ_ADDRESS = 108
READ_COUNT = 70
TOU_PROGRAMS = range(1, 7)
# (field, first register - 1, scale); program i sits at base + i.
PROGRAM_FIELDS = (
    ("power", 153, 10),
    ("voltage", 159, 0.01),
    ("soc", 165, 1),
    ("charging", 171, 1),
)
CURRENT_REGISTERS = (("charge", 108), ("discharge", 109), ("grid", 128))
FORCING_STATES = ("CHARGE_PV", "SELF_CONSUME")
# A CURTAIL row charges when its charge_kwh exceeds this. It is the 1e-6 kWh the plan's
# display classifier calls zero (`engine.machine_state._TOL`): HiGHS leaves up to that
# much (and `settle_mode_noise` up to `noise_bound`, zeroed) on a flow the mode does not
# use, and the published rows carry it unrounded, so a plain CURTAIL row can read
# 1e-34..5e-13 and must still hold. Equality with the engine is a test.
CURTAIL_CHARGE_MIN_KWH = 1e-6


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, str)) and _finite(value)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except TypeError, ValueError:
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, str):
            return int(value, 10)
        return int(value)
    except TypeError, ValueError:
        try:
            return int(float(value))
        except TypeError, ValueError:
            return default


def _round(value: float, precision: int = 0, method: str = "common") -> float:
    """Jinja's `round` filter: `common` is Python rounding, floor/ceil keep a float."""
    if method == "common":
        return round(value, precision)
    function = math.floor if method == "floor" else math.ceil
    return function(value * (10**precision)) / (10**precision)


@dataclass(frozen=True)
class Entities:
    """The Solarman entities the profile reads or writes, and the TOU prefix."""

    charge: str
    discharge: str
    grid: str
    operation: str
    soc: str
    voltage: str
    prefix: str

    @property
    def currents(self) -> tuple[str, str, str]:
        return (self.charge, self.discharge, self.grid)


@dataclass(frozen=True)
class Limits:
    """Installation limits and takeover settings; applied live."""

    max_power_w: float
    max_current: float
    max_grid_current: float
    relinquish_current: float
    hold_grid_current: float = 1
    reached_discharge_current: float = 1
    balance_grid_current: float = 2
    voltage_grid_charge_ceiling: float = 55.2
    commissioned_battery_modes: tuple[str, ...] = ("Capacity",)
    old_writers: tuple[str, ...] = ()
    telemetry_entities: tuple[str, ...] = ()
    discharge_energy_entity: str = ""


@dataclass(frozen=True)
class Fact:
    """One entity as the profile sees it: state, attributes, last report time."""

    state: str = "unknown"
    attributes: Mapping[str, Any] = MappingProxyType({})
    last_reported: float = 0.0


@dataclass(frozen=True)
class Snapshot:
    """Everything one decision reads, taken at one instant.

    `controller` is the attribute view of the Energy Compass controller (`accepted`,
    `session`, `plan_reason`, `retained`) or None when it is missing. `mode` is the
    requested controller mode; `paused` is the pause-writes switch;
    `blueprint_active` lists switched-on automations of the retired blueprint, whose
    writes would collide with ours. `full_candidate` is set while the SOC estimator
    holds a full-pack candidate: the decision then keeps the charge path open.
    """

    now: datetime
    mode: str
    entities: Entities
    states: Mapping[str, Fact]
    controller: Mapping[str, Any] | None
    runtime: Mapping[str, Any] = MappingProxyType({})
    paused: bool = False
    blueprint_active: tuple[str, ...] = ()
    full_candidate: bool = False

    def state(self, entity: str) -> str:
        fact = self.states.get(entity)
        return fact.state if fact is not None else "unknown"

    def attribute(self, entity: str, name: str) -> Any:
        fact = self.states.get(entity)
        return fact.attributes.get(name) if fact is not None else None

    def last_reported(self, entity: str) -> float:
        fact = self.states.get(entity)
        return fact.last_reported if fact is not None else 0.0


@dataclass(frozen=True)
class Decision:
    """The decision dict of the blueprint (`build.py:474-481`) with a reason code."""

    valid: bool
    reason_code: str
    reason_args: Mapping[str, Any]
    state: str
    desired: dict[str, Any]
    telemetry: bool
    writers_safe: bool
    settings_ok: bool
    active_tou: int
    times: list[int]
    retained: bool
    generation: Any
    deadline: Any
    row_start: Any
    row_end: Any
    operation: str
    requested_mode: str
    commissioned: bool
    target_reached: bool
    reached_key: list
    voltage: float
    soc: float
    target_voltage: float
    target_soc: float
    energy_mode: bool
    discharged_kwh: float
    slot_energy: Any
    warning_code: str = ""


class Command(NamedTuple):
    entity: str
    value: Any
    phase: str


@dataclass(frozen=True)
class Register:
    address: int
    scale: float


def register_map(entities: Entities) -> dict[str, Register]:
    """Entity -> Solarman holding register, in the order the blueprint builds it."""
    registers = {
        getattr(entities, name): Register(address, 1)
        for name, address in CURRENT_REGISTERS
    }
    for i in TOU_PROGRAMS:
        for name, base, scale in PROGRAM_FIELDS:
            domain = "select" if name == "charging" else "number"
            registers[f"{domain}.{entities.prefix}{i}_{name}"] = Register(
                base + i, scale
            )
    return registers


def _row_malformed(row: Mapping[str, Any], state: str) -> bool:
    """Whether the blueprint would raise on this row (Jinja `|float` or `as_timestamp`
    without a default), so the native controller must not act on it."""
    if state in ("HOLD", "CURTAIL"):
        required: tuple[str, ...] = ("end_soc_kwh",)
    elif state == "CHARGE_GRID":
        required = ("end_soc_kwh", "charge_kwh", "pv_kwh", "curtail_kwh", "load_kwh")
    elif state == "DISCHARGE_GRID":
        required = ("end_soc_kwh", "discharge_kwh")
    else:
        return False
    if not all(_is_number(row.get(key)) for key in required):
        return True
    if state in ("HOLD", "CURTAIL"):
        return False
    start, end = (_timestamp(row.get(key), None) for key in ("start", "end"))
    return start is None or end is None or end <= start


def _knot_voltage(target_soc: float, knots: Sequence[int], method: str) -> float:
    lo = min(8, _int(target_soc / 10) - 1)
    physical = knots[lo] + (target_soc - (lo + 1) * 10) / 10 * (
        knots[lo + 1] - knots[lo]
    )
    return _round(physical, 0, method) / 10


def decide(
    snapshot: Snapshot,
    limits: Limits,
    ceiling: Mapping[str, float] | None = None,
    base: bool = False,
) -> Decision:
    """DECISION, or BASE_DECISION when `base`; `ceiling` is the transaction's current cap."""
    e = snapshot.entities
    if snapshot.now.tzinfo is None:
        raise ValueError("Snapshot.now must be timezone-aware local time")
    t = snapshot.now.timestamp()
    c = snapshot.controller or {}
    cache = c.get("accepted") or {}
    rt = snapshot.runtime or {}
    battery = cache.get("battery") or {}
    capacity_kwh = _float(battery.get("capacity_kwh"))
    eta_charge = _float(battery.get("eta_charge"))
    eta_discharge = _float(battery.get("eta_discharge"))
    operation = snapshot.state(e.operation)
    max_power_w = limits.max_power_w
    max_current = limits.max_current
    relinquish = limits.relinquish_current

    telemetry = True
    for entity in limits.telemetry_entities:
        if not _is_number(snapshot.state(entity)) or not (
            0 <= t - snapshot.last_reported(entity) <= TELEMETRY_MAX_AGE_SECONDS
        ):
            telemetry = False
    v = _float(snapshot.state(e.voltage))
    soc = _float(snapshot.state(e.soc), -1)
    if not (PACK_VOLTAGE_RANGE[0] <= v <= PACK_VOLTAGE_RANGE[1] and 0 <= soc <= 100):
        telemetry = False

    writers = True
    for entity in limits.old_writers:
        if (
            snapshot.state(entity) != "off"
            or snapshot.attribute(entity, "current") != 0
        ):
            writers = False
    if snapshot.blueprint_active:
        writers = False

    times: list[int] = []
    for i in TOU_PROGRAMS:
        parts = snapshot.state(f"time.{e.prefix}{i}_time").split(":")
        if (
            len(parts) == 3
            and all(_is_number(part) for part in parts)
            and 0 <= _int(parts[0]) < 24
            and 0 <= _int(parts[1]) < 60
            and 0 <= _int(parts[2]) < 60
        ):
            times = [
                *times,
                _int(parts[0]) * 3600 + _int(parts[1]) * 60 + _int(parts[2]),
            ]
    times_ok = len(times) == 6 and len(set(times)) == 6 and times == sorted(times)
    local = snapshot.now
    clock = local.hour * 3600 + local.minute * 60 + local.second
    active = 0
    if times_ok:
        active = 6
        for index, start in enumerate(times, 1):
            if start <= clock:
                active = index

    row: Mapping[str, Any] = {}
    for candidate in cache.get("intervals", []):
        if (
            _timestamp(candidate.get("start"), 0)
            <= t
            < _round(_timestamp(candidate.get("end"), 0), 0, "floor")
        ):
            row = candidate
    retained = c.get("retained") is True

    reason_args: dict[str, Any] = {}
    if not telemetry:
        reason = "telemetry_stale"
    elif not times_ok:
        reason = "tou_times"
    elif (
        c.get("plan_reason") == "session"
        or not cache
        or cache.get("session") != c.get("session")
    ):
        reason = "session_wait"
    elif c.get("plan_reason") == "revoked":
        reason = "plan_revoked"
    elif c.get("plan_reason") != "ok":
        reason = "plan_sources"
    elif not (capacity_kwh > 0 and 0 < eta_charge <= 1 and 0 < eta_discharge <= 1):
        reason = "battery_params"
    elif (
        t >= _round(_timestamp(cache.get("valid_until"), 0), 0, "floor")
        or t >= _round(_float(cache.get("coverage_end", 0)), 0, "floor")
        or not row
    ):
        reason = "plan_expired"
    elif operation not in ("Capacity", "Voltage"):
        reason = "operation_mode"
    else:
        reason = "ok"
    valid = reason == "ok"
    state = row.get("state", "BASE") if valid else "BASE"
    if base or snapshot.mode == "Off":
        state = "BASE"
    # Deliberate divergence from the 0.1.38 blueprint, whose template raises on such a
    # row: the native controller holds BASE instead of acting on invented zeros.
    if valid and _row_malformed(row, state):
        valid = False
        reason = "plan_malformed"
        state = "BASE"
    balance = row.get("balance_hold", False) is True and state in (
        "CHARGE_PV",
        "CHARGE_GRID",
    )

    # Current ceilings: the held value stays while it fits the power cap, a higher
    # step needs 2 % voltage margin (build.py:293-306, hysteresis of 01.10).
    charge_raw = min(max_current, max_power_w / v) if v > 0 and telemetry else 0
    discharge_raw = (
        min(max_current, max_power_w * eta_discharge / v) if v > 0 and telemetry else 0
    )
    charge_held = _float(snapshot.state(e.charge))
    discharge_held = _float(snapshot.state(e.discharge))
    charge_step = _float(snapshot.attribute(e.charge, "step"), 1)
    discharge_step = _float(snapshot.attribute(e.discharge, "step"), 1)
    charge_cap = (
        charge_held
        if 0 < charge_held <= charge_raw
        and charge_held + charge_step > min(max_current, max_power_w / (v * 1.02))
        else charge_raw
    )
    discharge_cap = (
        discharge_held
        if 0 < discharge_held <= discharge_raw
        and discharge_held + discharge_step
        > min(max_current, max_power_w * eta_discharge / (v * 1.02))
        else discharge_raw
    )
    reached_key = [cache.get("generated_at"), row.get("start"), state, operation]

    energy_entity = limits.discharge_energy_entity
    energy_mode = (
        operation == "Voltage"
        and isinstance(energy_entity, str)
        and energy_entity != ""
        and _is_number(snapshot.state(energy_entity))
    )
    counted = _float(snapshot.state(energy_entity)) if energy_mode else 0
    slot = rt.get("slot_energy")
    slot_start = (
        _float(slot.get("discharge"))
        if energy_mode
        and isinstance(slot, Mapping)
        and slot.get("key") == reached_key
        and _is_number(slot.get("discharge"))
        else counted
    )
    discharged = max(0, counted - slot_start)
    reached_discharge = min(discharge_cap, limits.reached_discharge_current)

    target_soc: float = 10.0
    target_voltage: float = BASE_VOLTAGE
    charge = (
        relinquish
        if state == "BASE"
        else (charge_cap if state in FORCING_STATES else 0)
    )
    discharge = (
        relinquish
        if state == "BASE"
        else (discharge_cap if state in FORCING_STATES else 0)
    )
    grid: float = 0
    direction = "Disabled"
    power = max_power_w
    reached = False

    if state in ("CHARGE_GRID", "DISCHARGE_GRID"):
        h = (_timestamp(row.get("end")) - _timestamp(row.get("start"))) / 3600
        end_soc = _float(row.get("end_soc_kwh"))
        target_soc = min(100, max(10, end_soc / capacity_kwh * 100))
        charging = state == "CHARGE_GRID"
        target_soc = _round(target_soc, 0, "floor" if charging else "ceil")
        if balance and charging:
            target_soc = 100
        knots = CHARGE_KNOTS if charging else DISCHARGE_KNOTS
        target_voltage = _knot_voltage(
            target_soc, knots, "floor" if charging else "ceil"
        )
        charge_kwh = _float(row.get("charge_kwh"))
        discharge_kwh = _float(row.get("discharge_kwh"))
        if charging:
            planned = min(charge_cap, max(0, charge_kwh) / h * 1000 * eta_charge / v)
            surplus = max(
                0,
                _float(row.get("pv_kwh"))
                - _float(row.get("curtail_kwh"))
                - _float(row.get("load_kwh")),
            )
            grid = min(
                limits.max_grid_current,
                planned,
                max(0, charge_kwh - surplus) / h * 1000 * eta_charge / v,
            )
            charge = charge_cap
            direction = "Grid" if grid >= 1 and planned >= 1 else "Disabled"
            if balance:
                grid = max(limits.balance_grid_current, grid)
                direction = "Grid"
            if not balance and operation == "Voltage":
                target_voltage = _float(limits.voltage_grid_charge_ceiling)
                if v >= target_voltage * 10:
                    grid = 0
                    direction = "Disabled"
                    reached = True
            elif not balance and operation == "Capacity" and soc >= target_soc:
                grid = 0
                direction = "Disabled"
                reached = True
        else:
            discharge = min(
                discharge_cap, max(0, discharge_kwh) / h * 1000 / eta_discharge / v
            )
            power = min(max_power_w, max(0, discharge_kwh) / h * 1000)
            direction = (
                "Sell" if state == "DISCHARGE_GRID" and discharge >= 1 else "Disabled"
            )
            if energy_mode:
                target_voltage = 49.6
                if discharged >= max(0, discharge_kwh) - SOC_LATCH_MARGIN_KWH:
                    discharge = reached_discharge
                    direction = "Disabled"
                    reached = True
            elif (operation == "Capacity" and soc <= target_soc) or (
                operation == "Voltage" and v <= target_voltage * 10
            ):
                discharge = reached_discharge
                direction = "Disabled"
                reached = True

    # A CURTAIL row that charges (the PV above what the site passes exceeds what the
    # pack takes at charge_kw, T-064) charges from PV at the planned current, at least
    # the HOLD current and at most the cap: grid 0 A, direction Disabled, discharge 0 A.
    # The row's charge is free between 0 and the PV left after the curtailment and the
    # load, so a row planning less than charge_kw x h charges less than the cap would
    # (the SOC ends where the plan says, the rest stays curtailed). The planned current
    # is rounded up to the charge step, so a row at charge_kw x h gets the whole cap.
    # Every other HOLD/CURTAIL row holds the SOC.
    curtail_charges = (
        state == "CURTAIL"
        and bool(row)
        and _float(row.get("charge_kwh")) > CURTAIL_CHARGE_MIN_KWH
    )
    if curtail_charges:
        # The current row starts at or before now and ends after it: h > 0.
        h = (_timestamp(row.get("end")) - _timestamp(row.get("start"))) / 3600
        step = charge_step if charge_step > 0 else 1
        planned = _float(row.get("charge_kwh")) / h * 1000 * eta_charge / v
        charge = min(
            charge_cap,
            max(
                limits.hold_grid_current,
                _round(_round(planned / step, 9), 0, "ceil") * step,
            ),
        )
        discharge = 0
    elif state in ("HOLD", "CURTAIL") and row:
        target_soc = _round(
            min(100, max(10, _float(row.get("end_soc_kwh")) / capacity_kwh * 100)),
            0,
            "floor",
        )
        target_voltage = _knot_voltage(target_soc, DISCHARGE_KNOTS, "floor")
        charge = (
            charge_cap
            if snapshot.full_candidate and state == "HOLD"
            else limits.hold_grid_current
        )
        discharge = 0
        grid = limits.hold_grid_current
        direction = "Grid"

    if (
        state in ("CHARGE_GRID", "DISCHARGE_GRID")
        and rt.get("reached_key") == reached_key
        and not balance
    ):
        charge = charge_cap if state == "CHARGE_GRID" else 0
        grid = 0
        discharge = reached_discharge if state == "DISCHARGE_GRID" else 0
        direction = "Disabled"
        reached = True

    held = rt.get("desired") if isinstance(rt.get("desired"), Mapping) else {}
    full_held = (
        state == "HOLD"
        and rt.get("state") == "HOLD"
        and soc > 99
        and len(held) > 0
        and "Grid" not in held.values()
    )
    full_pv = (
        operation == "Voltage"
        and (soc >= 100 or full_held)
        and not balance
        and (
            state in FORCING_STATES
            or curtail_charges
            or (state == "HOLD" and bool(row))
        )
    )
    if full_pv:
        charge = charge_cap
        grid = 0
        direction = "Disabled"
        target_soc = 10
        target_voltage = BASE_VOLTAGE
        power = max_power_w
    elif (
        (state in ("CHARGE_PV", "SELF_CONSUME", "CHARGE_GRID") or curtail_charges)
        and soc >= 100
        and not balance
    ):
        charge = charge_cap if snapshot.full_candidate else 0

    reason_args = {}
    if operation == "Voltage" and not (
        APPROVED_TOU_VOLTAGE_RANGE[0] <= target_voltage <= APPROVED_TOU_VOLTAGE_RANGE[1]
    ):
        valid = False
        reason = "voltage_range"
        state = "BASE"
        target_soc = 10
        target_voltage = BASE_VOLTAGE
        direction = "Disabled"
        charge = discharge = relinquish
        grid = 0
        power = max_power_w
    commissioned = operation in limits.commissioned_battery_modes
    if snapshot.mode != "Simulation" and not commissioned:
        valid = False
        reason = "mode_not_commissioned"
        reason_args = {
            "operation": operation,
            "modes": ", ".join(limits.commissioned_battery_modes),
        }
        state = "BASE"
        target_soc = 10
        target_voltage = BASE_VOLTAGE
        direction = "Disabled"
        charge = discharge = relinquish
        grid = 0
        power = max_power_w

    desired: dict[str, Any] = {e.charge: charge, e.discharge: discharge, e.grid: grid}
    for i in TOU_PROGRAMS:
        on = i == active and state != "BASE"
        prefix = f"number.{e.prefix}{i}_"
        desired[prefix + "soc"] = target_soc if on else BASE_SOC
        desired[prefix + "voltage"] = target_voltage if on else BASE_VOLTAGE
        desired[prefix + "power"] = power if on else max_power_w
        desired[f"select.{e.prefix}{i}_charging"] = direction if on else "Disabled"

    settings_ok = True
    values: dict[str, Any] = {}
    ceilings = ceiling or {}
    for entity, value in desired.items():
        if entity.startswith("number."):
            step = _float(snapshot.attribute(entity, "step"))
            minimum = snapshot.attribute(entity, "min")
            maximum = snapshot.attribute(entity, "max")
            if (
                not _is_number(minimum)
                or not _is_number(maximum)
                or step <= 0
                or _float(minimum) > value
                or _float(maximum) < value
            ):
                settings_ok = False
            else:
                if entity.endswith("_power"):
                    step = max(step, 100)
                rounding = (
                    "ceil"
                    if entity.endswith(("_soc", "_voltage"))
                    and state not in ("CHARGE_GRID", "HOLD", "CURTAIL")
                    else "floor"
                )
                # Decimal targets must not lose a step to binary division noise.
                units = (
                    _round(value / step, 9)
                    if entity.endswith(("_soc", "_voltage", "_power"))
                    else value / step
                )
                value = _round(_round(units, 0, rounding) * step, 6)
        elif value not in (snapshot.attribute(entity, "options") or []):
            settings_ok = False
        if state != "BASE" and entity in e.currents and entity in ceilings:
            value = min(value, ceilings[entity])
        values[entity] = value
    if not settings_ok:
        valid = False
        reason = "settings_range"
        reason_args = {}

    return Decision(
        valid=valid,
        reason_code=reason,
        reason_args=reason_args,
        state=state,
        desired=values,
        telemetry=telemetry,
        writers_safe=writers,
        settings_ok=settings_ok,
        active_tou=active,
        times=times,
        retained=retained,
        generation=cache.get("generated_at"),
        deadline=cache.get("valid_until"),
        row_start=row.get("start"),
        row_end=row.get("end"),
        operation=operation,
        requested_mode=snapshot.mode,
        commissioned=commissioned,
        target_reached=reached,
        reached_key=reached_key,
        voltage=v,
        soc=soc,
        target_voltage=target_voltage,
        target_soc=target_soc,
        energy_mode=energy_mode,
        discharged_kwh=_round(discharged, 3),
        slot_energy={"key": reached_key, "discharge": slot_start}
        if energy_mode
        else rt.get("slot_energy"),
        warning_code="curtail_unsupported"
        if state == "CURTAIL" and not curtail_charges
        else "",
    )


def _equal(snapshot: Snapshot, entity: str, value: Any) -> bool:
    current = snapshot.state(entity)
    if entity.startswith("select."):
        return current == value
    return (
        _is_number(current) and abs(float(current) - _float(value)) < VOLTAGE_TOLERANCE
    )


def diff(snapshot: Snapshot, target: Decision) -> list[Command]:
    """The ordered write plan from the live states to `target` (DIFF)."""
    e = snapshot.entities
    dirty = list(snapshot.runtime.get("uncertain", []))
    active = f"select.{e.prefix}{target.active_tou}_charging"
    changed = [
        entity
        for entity, value in target.desired.items()
        if not _equal(snapshot, entity, value) or entity in dirty
    ]
    protective = False
    for entity in changed:
        if (
            entity == active
            or entity in dirty
            or (
                entity in e.currents
                and target.desired[entity] == 0
                and _float(snapshot.state(entity), -1) != 0
            )
            # Matches entity ids that contain `_program_N_` (build.py:499).
            or (
                f"_program_{target.active_tou}_" in entity
                and snapshot.state(active) != "Disabled"
            )
        ):
            protective = True
    stop: list[Command] = []
    stopped: list[str] = []
    if protective:
        for entity in (e.charge, e.grid, e.discharge):
            if _float(snapshot.state(entity), -1) != 0 or entity in dirty:
                stop.append(Command(entity, 0, "disable"))
                stopped.append(entity)
        for i in TOU_PROGRAMS:
            entity = f"select.{e.prefix}{i}_charging"
            if snapshot.state(entity) != "Disabled" or entity in dirty:
                stop.append(Command(entity, "Disabled", "disable"))
                stopped.append(entity)
    limits: list[Command] = []
    enable: list[Command] = []
    for entity, value in target.desired.items():
        if entity not in changed and entity not in stopped:
            continue
        if entity.startswith("select."):
            if value != "Disabled":
                enable.append(Command(entity, value, "enable"))
            elif entity not in stopped:
                stop.append(Command(entity, value, "disable"))
        elif entity in (e.grid, e.charge, e.discharge):
            if value > 0:
                enable.append(Command(entity, value, "enable"))
            elif entity not in stopped:
                stop.append(Command(entity, 0, "disable"))
        else:
            limits.append(Command(entity, value, "limits"))
    return stop + limits + enable


def guard(
    snapshot: Snapshot,
    command: Command,
    *,
    target: Decision,
    fresh: Decision,
    baseline: Decision,
    cleanup: bool,
    failed: bool,
) -> bool:
    """Whether `command` may still be sent (GUARD); re-evaluated before every write."""
    if snapshot.paused:
        return False
    confirmed = snapshot.runtime.get("confirmed", {})
    current = baseline if cleanup else fresh
    prerequisites = True
    if command.phase == "enable":
        number_prefix = f"number.{snapshot.entities.prefix}"
        for entity, value in target.desired.items():
            if (
                entity.startswith(number_prefix) or value == 0 or value == "Disabled"
            ) and confirmed.get(entity) != value:
                prerequisites = False
    if not (prerequisites and current.writers_safe and snapshot.mode != "Simulation"):
        return False
    if command.phase == "disable":
        return True
    if not (current.settings_ok and not failed):
        return False
    if (
        cleanup
        and baseline.desired == target.desired
        and baseline.operation == target.operation
        and baseline.times == target.times
        and (command.phase != "enable" or baseline.telemetry)
    ):
        return True
    return bool(
        fresh.valid
        and fresh.requested_mode == "Auto"
        and fresh.generation == target.generation
        and fresh.deadline == target.deadline
        and fresh.row_start == target.row_start
        and fresh.active_tou == target.active_tou
        and fresh.times == target.times
        and fresh.operation == target.operation
        and fresh.desired == target.desired
    )


def cap_only_replan(
    snapshot: Snapshot,
    target: Decision,
    fresh: Decision,
    *,
    cleanup: bool,
    transaction_attempt: int,
) -> bool:
    """Whether only the current ceilings moved, so one more transaction may follow."""
    same = True
    changed = False
    for entity, value in target.desired.items():
        if entity in snapshot.entities.currents:
            if value != fresh.desired.get(entity):
                changed = True
        elif value != fresh.desired.get(entity):
            same = False
    return bool(
        not cleanup
        and transaction_attempt == 1
        and not snapshot.runtime.get("uncertain", [])
        and same
        and changed
        and fresh.valid
        and fresh.writers_safe
        and fresh.settings_ok
        and fresh.requested_mode == target.requested_mode == "Auto"
        and fresh.state == target.state
        and fresh.target_reached == target.target_reached
        and fresh.generation == target.generation
        and fresh.deadline == target.deadline
        and fresh.row_start == target.row_start
        and fresh.row_end == target.row_end
        and fresh.active_tou == target.active_tou
        and fresh.times == target.times
        and fresh.operation == target.operation
    )


def _raw(response: Any, register: Register) -> Any:
    if not isinstance(response, Mapping):
        return None
    return response.get(register.address, response.get(str(register.address)))


def observe(response: Any, registers: Mapping[str, Register]) -> dict[str, str | float]:
    """Raw holding registers -> entity values; entities without a numeric register are absent."""
    observed: dict[str, str | float] = {}
    for entity, register in registers.items():
        raw = _raw(response, register)
        if _is_number(raw):
            observed[entity] = (
                SELECT_NAMES.get(_int(raw), UNSUPPORTED_SELECT)
                if entity.startswith("select.")
                else _round(_float(raw) * register.scale, 6)
            )
    return observed


def confirms(response: Any, command: Command, register: Register) -> bool:
    """Whether the read-back register holds the value the command wrote."""
    raw = _raw(response, register)
    if not _is_number(raw):
        return False
    if command.entity.startswith("select."):
        return _int(raw) == SELECT_RAW.get(command.value, -1)
    return abs(_float(raw) * register.scale - _float(command.value)) < VOLTAGE_TOLERANCE


def mark_dirty(runtime: Mapping[str, Any], entity: str) -> dict[str, Any]:
    uncertain = list(dict.fromkeys([*runtime.get("uncertain", []), entity]))
    return {**runtime, "uncertain": uncertain}


def record_observation(
    runtime: Mapping[str, Any], observed: Mapping[str, Any], snapshot: Snapshot
) -> dict[str, Any]:
    """Merge a read-back into `confirmed`; entities that differ from their state are uncertain."""
    dirty = list(runtime.get("uncertain", []))
    for entity, value in observed.items():
        if not _equal(snapshot, entity, value):
            dirty.append(entity)
    return {
        **runtime,
        "uncertain": list(dict.fromkeys(dirty)),
        "confirmed": {**runtime.get("confirmed", {}), **observed},
    }


def mark_confirmed(
    runtime: Mapping[str, Any], command: Command, stamp: str
) -> dict[str, Any]:
    return {
        **runtime,
        "uncertain": [x for x in runtime.get("uncertain", []) if x != command.entity],
        "confirmed": {**runtime.get("confirmed", {}), command.entity: command.value},
        "last_confirmation": stamp,
    }


def profile_confirmed(runtime: Mapping[str, Any], target: Decision) -> bool:
    confirmed = runtime.get("confirmed", {})
    return not runtime.get("uncertain", []) and all(
        confirmed.get(entity) == value for entity, value in target.desired.items()
    )


__all__ = [
    "Command",
    "Decision",
    "Entities",
    "Fact",
    "Limits",
    "Register",
    "Snapshot",
    "cap_only_replan",
    "confirms",
    "decide",
    "diff",
    "guard",
    "mark_confirmed",
    "mark_dirty",
    "observe",
    "profile_confirmed",
    "record_observation",
    "register_map",
]
