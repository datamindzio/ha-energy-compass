"""The native Deye executor: snapshot, decide and the write transaction loop.

`DeyeExecutor.run` is the run algorithm of the 0.1.38 blueprint
(`tools/deye_controller/build.py` ACTIONS and WRITE): up to three transactions,
each writing the planned commands with up to three attempts per command, every
write persisted first and read back from the registers. The profile math lives in
`controller_profile`; everything that touches Home Assistant sits behind
`ExecutorPort`, so tests drive the loop with a simulated register bank.

The snapshot is rebuilt after every await, at the points where the blueprint
renders its templates again: a change that lands between two writes (a newer
plan, a mode switch, an old automation starting) must be seen by the next guard.

The run lock with its re-run flag and the triggers live in `controller_native`. The
register layout probe runs here, once per session, before the first write and in
Simulation.
"""

import logging
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from . import controller_hardware as hardware
from . import controller_profile as profile
from . import controller_texts as texts

_LOGGER = logging.getLogger(__name__)
TRANSACTIONS = 3
WRITE_ATTEMPTS = 3
HISTORY_LIMIT = 50
NOTABLE_LIMIT = 20
MAX_RECORDED_WRITES = 60


class ControllerUnavailable(Exception):
    """The controller state could not be read or written."""


class ExecutorPort(Protocol):
    """What the executor needs from Home Assistant."""

    def now(self) -> datetime:
        """The local time, as the templates' `now()`."""

    def snapshot(self, stage: str) -> profile.Snapshot:
        """The current states; `stage` names the render point for test hooks.

        `Snapshot.now` must be timezone-aware local time. The executor replaces
        `Snapshot.runtime` with the runtime it last stored.
        """

    async def persist(
        self,
        runtime: Mapping[str, Any] | None = None,
        restore_pending: bool | None = None,
        *,
        durable: bool = False,
    ) -> tuple[dict, bool]:
        """Replace the stored runtime and/or restore flag; answer with both.

        `durable` asks for an immediate save: the write that follows must not outlive
        a lost restore obligation (DD-8). With neither argument it reads.
        """

    async def select_mode(self, mode: str) -> None:
        """Set the controller mode."""

    async def write(self, command: profile.Command) -> None:
        """Send one command; a failure is not an error, the read-back decides."""

    async def read_registers(self, address: int, count: int) -> Any:
        """The holding registers as a mapping, or anything else when unreadable."""


def _mark(record: Mapping[str, Any]) -> tuple[Any, Any]:
    return record.get("code"), record.get("outcome")


def _notable(record: Mapping[str, Any], before: tuple[Any, Any] | None) -> bool:
    """A run that sent a command, or whose code or outcome differs from the run before.

    A steady blocked, paused or released state repeats every minute; only its first
    run is kept, so it cannot push the writing runs out.
    """
    sent = any(not write.get("blocked") for write in record["writes"])
    return sent or _mark(record) != before


@dataclass
class _Run:
    limits: profile.Limits
    registers: dict[str, profile.Register]
    runtime: dict
    pending: bool
    ceiling: dict


class DeyeExecutor:
    def __init__(
        self,
        port: ExecutorPort,
        limits: Callable[[], profile.Limits],
        language: str = "en",
        probe_layout: bool = True,
    ) -> None:
        self.port = port
        self.probe_layout = probe_layout
        self._limits = limits
        self.language = language
        self._layout_session: Any = None
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LIMIT)
        self.notable: deque[dict[str, Any]] = deque(maxlen=NOTABLE_LIMIT)
        self._mark: tuple[Any, Any] | None = None
        self._record: dict[str, Any] | None = None

    def _snapshot(self, run: _Run, stage: str) -> profile.Snapshot:
        """A fresh snapshot carrying the runtime this executor last stored.

        The profile's diff, guard and replan read `snapshot.runtime`, while every
        write updates `run.runtime`; overlaying it here leaves one source of truth
        whatever the port's own runtime view is.
        """
        return replace(self.port.snapshot(stage), runtime=run.runtime)

    def _text(self, code: str, args: Mapping[str, Any] | None = None) -> str:
        return texts.text(code, self.language, args)

    async def _persist(
        self,
        run: _Run,
        runtime: Mapping[str, Any] | None = None,
        pending: bool | None = None,
        *,
        durable: bool = False,
    ) -> None:
        run.runtime, run.pending = await self.port.persist(
            runtime, pending, durable=durable
        )
        if self._record is not None:
            self._record["code"] = run.runtime.get("code")

    def _decisions(
        self, snapshot: profile.Snapshot, run: _Run
    ) -> tuple[profile.Decision, profile.Decision]:
        return (
            profile.decide(snapshot, run.limits, run.ceiling),
            profile.decide(snapshot, run.limits, run.ceiling, base=True),
        )

    def _decision_runtime(
        self, run: _Run, decision: profile.Decision, stamp: str
    ) -> dict:
        runtime = run.runtime
        reason = self._text(decision.reason_code, decision.reason_args)
        return {
            **runtime,
            "reached_key": decision.reached_key
            if decision.target_reached
            else runtime.get("reached_key"),
            "slot_energy": decision.slot_energy,
            "desired": decision.desired,
            "requested_mode": decision.requested_mode,
            "battery_mode_commissioned": decision.commissioned,
            "state": decision.state,
            "accepted_generation": decision.generation,
            "original_deadline": decision.deadline,
            "active_tou": decision.active_tou,
            "retained": decision.retained,
            "code": "ok" if decision.valid else "blocked",
            "reason": reason,
            "reason_code": decision.reason_code,
            "warning": self._text(decision.warning_code),
            "takeover_blocked": not decision.writers_safe,
            "since": runtime.get("since", stamp)
            if runtime.get("reason") == reason
            else stamp,
        }

    def _outcome_runtime(self, run: _Run, code: str, **values: Any) -> dict:
        return {
            **run.runtime,
            "code": code,
            "reason_code": code,
            "reason": self._text(code),
            **values,
        }

    async def run(self, trigger: str = "") -> str:
        """One pass; the outcome code explains why it ended.

        The last 50 passes are kept in `history`, in memory only: what triggered the
        run, the decision, the planned and sent commands with their outcome, and how
        it ended. Passes that sent a command or ended with a code other than `ok` are
        also kept in `notable` (the last 20), so routine passes cannot push them out.
        """
        started = self.port.now()
        record: dict[str, Any] = {
            "trigger": trigger,
            "at": started.isoformat(),
            "decision": None,
            "transactions": [],
            "writes": [],
            "outcome": "cancelled",
            "code": None,
        }
        self._record = record
        try:
            outcome = await self._pass()
        except Exception:
            record["outcome"] = "error"
            raise
        else:
            record["outcome"] = outcome
            return outcome
        finally:
            record["seconds"] = round((self.port.now() - started).total_seconds(), 3)
            self._record = None
            self.history.append(record)
            if _notable(record, self._mark):
                self.notable.append(record)
            self._mark = _mark(record)

    async def _pass(self) -> str:
        port = self.port
        snapshot = port.snapshot("start")
        if snapshot.controller is None:
            return "controller_unavailable"
        run = _Run(
            limits=self._limits(),
            registers=profile.register_map(snapshot.entities),
            runtime={},
            pending=False,
            ceiling={},
        )
        await self._persist(run)
        snapshot = self._snapshot(run, "decision")
        decision = profile.decide(snapshot, run.limits, run.ceiling)
        await self._persist(
            run, self._decision_runtime(run, decision, port.now().isoformat())
        )
        if self._record is not None:
            self._record["decision"] = {
                "state": decision.state,
                "valid": decision.valid,
                "reason_code": decision.reason_code,
                "operation": decision.operation,
                "requested_mode": decision.requested_mode,
                "target_soc": decision.target_soc,
                "target_voltage": decision.target_voltage,
                "target_reached": decision.target_reached,
            }

        snapshot = self._snapshot(run, "mode")
        if snapshot.mode == "Simulation" and run.pending:
            await port.select_mode("Off")
            await self._persist(run, self._outcome_runtime(run, "simulation_blocked"))
            return "simulation_rejected"
        if snapshot.mode == "Simulation":
            if not await self._layout_holds(run, snapshot):
                return "layout_mismatch"
            return "simulation"
        if snapshot.mode == "Off" and not run.pending:
            return "released"
        if snapshot.blueprint_active:
            await self._persist(
                run,
                {
                    **run.runtime,
                    "code": "blocked",
                    "reason_code": "blueprint_active",
                    "reason": self._text(
                        "blueprint_active",
                        {"automation": ", ".join(snapshot.blueprint_active)},
                    ),
                    "takeover_blocked": True,
                },
            )
            return "takeover_blocked"
        if snapshot.paused:
            return await self._paused(run)
        if not decision.writers_safe:
            return "takeover_blocked"
        session = (snapshot.controller or {}).get("session")
        cleanup = (
            not decision.valid
            or snapshot.mode == "Off"
            or (run.pending and run.runtime.get("owned_session") != session)
        )
        # Restoring the base goes through Solarman's own entities and needs no
        # proven layout; only forcing does, and a blocked restore would leave a
        # forced plan in place.
        if not cleanup and not await self._layout_holds(run, snapshot):
            return "layout_mismatch"
        return await self._transactions(run, cleanup)

    @staticmethod
    def _layout_key(run: _Run, snapshot: profile.Snapshot) -> tuple:
        """What a passed probe is valid for: the session, the device and the map read."""
        controller = snapshot.controller or {}
        return (
            controller.get("session"),
            controller.get("device_id"),
            tuple((entity, r.address, r.scale) for entity, r in run.registers.items()),
        )

    async def _layout_holds(self, run: _Run, snapshot: profile.Snapshot) -> bool:
        """The register layout probe; a mismatch blocks forcing, not the restore."""
        key = self._layout_key(run, snapshot)
        if not self.probe_layout or self._layout_session == key:
            return True
        try:
            response = await self.port.read_registers(
                profile.READ_ADDRESS, profile.READ_COUNT
            )
        except Exception:
            _LOGGER.debug("Deye layout probe read failed", exc_info=True)
            response = None
        snapshot = self._snapshot(run, "probe")
        mismatched = hardware.probe(
            response,
            run.registers,
            {entity: snapshot.state(entity) for entity in run.registers},
        )
        if not mismatched:
            self._layout_session = key
            if self._record is not None:
                self._record["probe"] = "passed"
            return True
        if self._record is not None:
            self._record["probe"] = f"failed: {hardware.listed(mismatched)}"
        reason = self._text(
            "layout_mismatch", {"entities": hardware.listed(mismatched)}
        )
        await self._persist(
            run,
            {
                **run.runtime,
                "code": "blocked",
                "reason_code": "layout_mismatch",
                "reason": reason,
            },
        )
        return False

    async def _paused(self, run: _Run) -> str:
        """Hold every write; `restore_pending` and `uncertain` stay as they are."""
        await self._persist(run, self._outcome_runtime(run, "paused"))
        return "paused"

    async def _transactions(self, run: _Run, cleanup: bool) -> str:
        aborted = False
        for attempt in range(1, TRANSACTIONS + 1):
            snapshot = self._snapshot(run, "target")
            if snapshot.paused:
                return await self._paused(run)
            target = profile.decide(snapshot, run.limits, run.ceiling)
            replan: bool = False
            if cleanup or aborted:
                cleanup = True
                snapshot = self._snapshot(run, "target_base")
                target = profile.decide(snapshot, run.limits, run.ceiling, base=True)
            if not cleanup:
                entities = snapshot.entities
                run.ceiling = {
                    entities.charge: target.desired[entities.charge],
                    entities.discharge: target.desired[entities.discharge],
                    entities.grid: target.desired[entities.grid],
                }
            commands = profile.diff(snapshot, target)
            if self._record is not None:
                self._record["transactions"].append(
                    {
                        "attempt": attempt,
                        "cleanup": cleanup,
                        "commands": [list(command) for command in commands],
                    }
                )
            failed = False
            fresh = target
            if commands:
                await self._persist(
                    run,
                    {
                        **run.runtime,
                        "owned_session": (snapshot.controller or {}).get("session"),
                    },
                    True,
                    durable=True,
                )
            for command in commands:
                if replan:
                    continue
                confirmed, allowed, fresh = await self._write(
                    run, command, target, fresh, cleanup, failed
                )
                if not confirmed:
                    if not failed and not aborted and not allowed:
                        snapshot = self._snapshot(run, "replan")
                        replan = profile.cap_only_replan(
                            snapshot,
                            target,
                            fresh,
                            cleanup=cleanup,
                            transaction_attempt=attempt,
                        )
                    failed = True
                    aborted = True

            snapshot = self._snapshot(run, "fresh")
            fresh, baseline = self._decisions(snapshot, run)
            confirmed_profile = profile.profile_confirmed(run.runtime, target)
            if replan:
                replan = profile.cap_only_replan(
                    snapshot,
                    target,
                    fresh,
                    cleanup=cleanup,
                    transaction_attempt=attempt,
                )
            # A plan accepted between target and fresh, with nothing written, is
            # applied by the next run; aborting would restore BASE for no reason.
            if (
                attempt == 1
                and not cleanup
                and not aborted
                and not commands
                and fresh.valid
                and fresh.requested_mode == "Auto"
                and fresh.generation != target.generation
            ):
                return "plan_changed"
            if not cleanup and (
                not fresh.valid
                or fresh.desired != target.desired
                or fresh.generation != target.generation
                or fresh.row_start != target.row_start
                or fresh.requested_mode != "Auto"
            ):
                if not failed and not aborted:
                    replan = profile.cap_only_replan(
                        snapshot,
                        target,
                        fresh,
                        cleanup=cleanup,
                        transaction_attempt=attempt,
                    )
                aborted = True
            if cleanup and (
                baseline.desired != target.desired
                or baseline.operation != target.operation
                or baseline.times != target.times
                or not baseline.settings_ok
                or not baseline.writers_safe
                or snapshot.mode == "Simulation"
            ):
                aborted = True
            if not failed and not aborted:
                if cleanup and confirmed_profile:
                    await self._persist(run, None, False)
                await self._persist(
                    run, self._finished_runtime(run, target, cleanup, confirmed_profile)
                )
                return "run_finished"
            cleanup = not replan
            aborted = False
        if self._snapshot(run, "end").paused:
            return await self._paused(run)
        await self._persist(
            run,
            self._outcome_runtime(run, "write_failed", confirmed_mode="unconfirmed"),
        )
        return "write_failed"

    def _finished_runtime(
        self,
        run: _Run,
        target: profile.Decision,
        cleanup: bool,
        confirmed_profile: bool,
    ) -> dict:
        if confirmed_profile:
            return {
                **run.runtime,
                "confirmed_mode": "BASE" if cleanup else target.state,
                "code": "restored" if cleanup else "ok",
                "reason": self._text(target.reason_code, target.reason_args),
                "reason_code": target.reason_code,
            }
        return {
            **run.runtime,
            "confirmed_mode": "unconfirmed",
            "code": "verification_required",
            "reason": self._text("verification_required"),
            "reason_code": "verification_required",
        }

    async def _write(
        self,
        run: _Run,
        command: profile.Command,
        target: profile.Decision,
        fresh: profile.Decision,
        cleanup: bool,
        failed: bool,
    ) -> tuple[bool, bool, profile.Decision]:
        """Up to three guarded, persisted, read-back attempts at one command.

        Returns whether the register confirmed the value, the last guard answer and
        the last re-check decision.
        """
        port = self.port
        confirmed = False
        allowed = False
        register = run.registers[command.entity]
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            snapshot = self._snapshot(run, "write_fresh")
            fresh, baseline = self._decisions(snapshot, run)
            allowed = self._allowed(
                snapshot, command, target, fresh, baseline, cleanup, failed
            )
            if not allowed and attempt == 1:
                self._add_write(
                    {"entity": command.entity, "value": command.value, "blocked": True}
                )
            if allowed and not confirmed:
                await self._persist(
                    run, profile.mark_dirty(run.runtime, command.entity), durable=True
                )
                snapshot = self._snapshot(run, "write_recheck")
                fresh, baseline = self._decisions(snapshot, run)
                allowed = self._allowed(
                    snapshot, command, target, fresh, baseline, cleanup, failed
                )
                if allowed:
                    # Like the blueprint's continue_on_error: a failed call is not
                    # confirmed, the attempts and transactions carry on. Cancellation
                    # is not an Exception and must never be swallowed here.
                    write_error = False
                    try:
                        await port.write(command)
                    except Exception:
                        write_error = True
                        _LOGGER.debug("Deye write failed", exc_info=True)
                    try:
                        response = await port.read_registers(
                            profile.READ_ADDRESS, profile.READ_COUNT
                        )
                    except Exception:
                        _LOGGER.debug("Deye register read failed", exc_info=True)
                        response = {}
                    if not isinstance(response, Mapping):
                        response = {}
                    snapshot = self._snapshot(run, "observe")
                    observed = profile.observe(response, run.registers)
                    await self._persist(
                        run, profile.record_observation(run.runtime, observed, snapshot)
                    )
                    confirmed = profile.confirms(response, command, register)
                    self._note_write(command, attempt, write_error, confirmed)
                    if confirmed:
                        await self._persist(
                            run,
                            profile.mark_confirmed(
                                run.runtime, command, port.now().isoformat()
                            ),
                        )
        return confirmed, allowed, fresh

    def _add_write(self, entry: dict[str, Any]) -> None:
        """Keep the record small enough for a state attribute."""
        record = self._record
        if record is None:
            return
        if len(record["writes"]) < MAX_RECORDED_WRITES:
            record["writes"].append(entry)
        else:
            record["writes_truncated"] = True

    def _note_write(
        self, command: profile.Command, attempt: int, error: bool, confirmed: bool
    ) -> None:
        self._add_write(
            {
                "entity": command.entity,
                "value": command.value,
                "attempt": attempt,
                "write_error": error,
                "confirmed": confirmed,
            }
        )

    @staticmethod
    def _allowed(
        snapshot: profile.Snapshot,
        command: profile.Command,
        target: profile.Decision,
        fresh: profile.Decision,
        baseline: profile.Decision,
        cleanup: bool,
        failed: bool,
    ) -> bool:
        return profile.guard(
            snapshot,
            command,
            target=target,
            fresh=fresh,
            baseline=baseline,
            cleanup=cleanup,
            failed=failed,
        )
