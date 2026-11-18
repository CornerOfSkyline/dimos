# Copyright 2025-2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The shared VLN task runtime: one lightweight, in-memory task state
machine used by both VLN skills (`active_vln`, `stream_vln`).

Deliberately built out of DimOS primitives only — threading.Event, a lock,
and plain dataclasses. No database, no queue, no scheduler: a skill module
owns one `VlnTaskRuntime`, each `stream_vln`/`active_vln` invocation mints
a `VlnRunHandle` from it, and the run loop reports its own state/stage
transitions as it goes. `get_vln_task_status` (a plain `@skill` on the same
module) reads the runtime's snapshots — during a run and after it.

State machine:

    CREATED -> RUNNING -> {STOPPING} -> COMPLETED | FAILED | CANCELLED

COMPLETED means the run loop exited normally (the policy said stop, or the
turn budget was spent). It does NOT mean the navigation goal was reached —
success judgment needs ground truth this stack does not have; the
termination reason is always recorded separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any
import uuid

# -- states ---------------------------------------------------------------------

CREATED = "CREATED"
RUNNING = "RUNNING"
STOPPING = "STOPPING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

_TERMINAL = {COMPLETED, FAILED, CANCELLED}
# Legal transitions; STOPPING is reachable from RUNNING (a cancel was
# requested but the executing loop has not exited yet — STOPPING is not
# "stopped") and from CREATED never (a run must start before it can stop).
_TRANSITIONS = {
    CREATED: {RUNNING},
    RUNNING: {STOPPING, COMPLETED, FAILED, CANCELLED},
    STOPPING: {COMPLETED, FAILED, CANCELLED},
    COMPLETED: set(),
    FAILED: set(),
    CANCELLED: set(),
}

# -- termination reasons (recorded independently of the state) -------------------

POLICY_STOP = "POLICY_STOP"
TURN_BUDGET_EXHAUSTED = "TURN_BUDGET_EXHAUSTED"
USER_CANCELLED = "USER_CANCELLED"
POLICY_ERROR = "POLICY_ERROR"
SENSOR_UNAVAILABLE = "SENSOR_UNAVAILABLE"
EXECUTION_ERROR = "EXECUTION_ERROR"

# -- execution stages -----------------------------------------------------------

INFERENCE = "INFERENCE"
ACTION_EXECUTION = "ACTION_EXECUTION"
WAITING_OBSERVATION = "WAITING_OBSERVATION"
STOPPING_STAGE = "STOPPING"

# A RUNNING/STOPPING snapshot whose updated_at is older than this is
# flagged stale: the worker may be wedged (a crash has no snapshot at all —
# the query RPC itself fails then), and a stale snapshot must never be
# presented as a healthy RUNNING task.
STALE_AFTER_S = 30.0


def _now() -> float:
    return time.time()


@dataclass
class TaskSnapshot:
    """The queryable state of one VLN task run (see design.md for fields)."""

    run_id: str
    policy_name: str
    instruction: str
    state: str
    current_turn: int = 0
    max_turns: int = 0
    current_stage: str | None = None
    started_at: float | None = None
    updated_at: float = float("nan")
    finished_at: float | None = None
    termination_reason: str | None = None
    error_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The snapshot as a plain JSON-able dict (the status skill's payload)."""
        now = _now()
        stale_s = None
        if self.state in (RUNNING, STOPPING) and self.updated_at == self.updated_at:
            age = now - self.updated_at
            if age > STALE_AFTER_S:
                stale_s = round(age, 1)
        return {
            "run_id": self.run_id,
            "policy_name": self.policy_name,
            "instruction": self.instruction,
            "state": self.state,
            "current_turn": self.current_turn,
            "max_turns": self.max_turns,
            "current_stage": self.current_stage,
            "started_at": self.started_at,
            "updated_at": None if self.updated_at != self.updated_at else self.updated_at,
            "finished_at": self.finished_at,
            "termination_reason": self.termination_reason,
            "error_message": self.error_message,
            # Non-null when a live-looking snapshot has not been refreshed
            # within STALE_AFTER_S — the worker may be wedged; the caller
            # must not treat a stale entry as a healthy RUNNING task.
            "stale_s": stale_s,
        }


@dataclass
class VlnRunHandle:
    """One task invocation's identity and its own, private cancel event.

    The run loop holds a direct reference to this handle (never to "the
    current run"): an inference answer that returns after the run was
    cancelled is checked against THIS handle's event and dropped, so a
    stale policy result can never drive the robot. `finish()` is idempotent
    and only this handle can settle its own terminal record.
    """

    run_id: str
    policy_name: str
    instruction: str
    max_turns: int
    runtime: VlnTaskRuntime
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def finish(
        self,
        state: str,
        termination_reason: str,
        error_message: str | None = None,
    ) -> None:
        """Settle this run's terminal record (idempotent; first writer wins)."""
        self.runtime._finish(self, state, termination_reason, error_message)


class VlnTaskRuntime:
    """In-memory task registry for one VLN skill module.

    One live run at a time (`begin` refuses while a run is RUNNING or
    STOPPING — the executing instance keeps its motion authority until it
    has fully exited), a bounded history of recent snapshots for
    post-run queries, and the run loop's own transition reporting.
    Thread-safe: the run loop, the stop skill, and the status skill all
    touch it from different threads.
    """

    _HISTORY = 8

    def __init__(self, policy_name: str) -> None:
        self._policy_name = policy_name
        self._lock = threading.Lock()
        self._current: VlnRunHandle | None = None
        self._snapshots: dict[str, TaskSnapshot] = {}
        self._order: list[str] = []

    # -- run lifecycle ---------------------------------------------------------

    def begin(self, instruction: str, max_turns: int) -> VlnRunHandle | None:
        """Mint a run handle; None when a run is still executing.

        RUNNING and STOPPING both refuse: STOPPING is a cancel in flight,
        not a completed stop — starting a second motion-authoritative task
        before the old instance has exited would leave two instances able
        to command motion.
        """
        with self._lock:
            if self._current is not None and self._current.cancelled:
                state = STOPPING
            elif self._current is not None:
                state = RUNNING
            else:
                state = None
            if state is not None:
                return None
            handle = VlnRunHandle(
                run_id=uuid.uuid4().hex,
                policy_name=self._policy_name,
                instruction=instruction,
                max_turns=max_turns,
                runtime=self,
            )
            self._current = handle
            snapshot = TaskSnapshot(
                run_id=handle.run_id,
                policy_name=self._policy_name,
                instruction=instruction,
                state=CREATED,
                max_turns=max_turns,
                updated_at=_now(),
            )
            self._remember(snapshot)
            return handle

    def current_run_id(self) -> str | None:
        with self._lock:
            return self._current.run_id if self._current is not None else None

    def request_cancel(self) -> None:
        """Ask the executing run to stop (idempotent; no run is a no-op).

        This is the stop skills' and the module lifecycle's signal: it sets
        the CURRENT handle's own cancel event — never a shared flag — so the
        run loop (and every PD cycle through the handle) unwinds, and any
        in-flight policy answer that arrives afterwards is checked against
        the same event and dropped. The snapshot flips to STOPPING
        immediately (a cancel in flight is not "stopped", but it is also
        no longer a healthy RUNNING), so status queries between the cancel
        and the loop's exit tell the truth.
        """
        with self._lock:
            if self._current is not None:
                self._current.cancel.set()
                snapshot = self._snapshots.get(self._current.run_id)
                if snapshot is not None and snapshot.state == RUNNING:
                    snapshot.state = STOPPING
                    snapshot.current_stage = STOPPING_STAGE
                    snapshot.updated_at = _now()

    def current_state(self) -> str | None:
        with self._lock:
            current = self._current
            snapshot = self._snapshots.get(current.run_id) if current else None
            if snapshot is None:
                return None
            if current is not None and current.cancelled and snapshot.state == RUNNING:
                return STOPPING  # cancel in flight; the loop has not exited yet
            return snapshot.state

    # -- run-loop reporting ----------------------------------------------------

    def mark_running(self, handle: VlnRunHandle) -> None:
        self._transition(handle, RUNNING)

    def set_stage(self, handle: VlnRunHandle, stage: str, turn: int | None = None) -> None:
        """Report the current execution stage (and optionally the turn number).

        Every call refreshes updated_at — this doubles as the liveness
        heartbeat the status query checks for staleness.
        """
        with self._lock:
            snapshot = self._snapshots.get(handle.run_id)
            if snapshot is None or snapshot.state in _TERMINAL:
                return
            if handle.cancelled and snapshot.state == RUNNING:
                snapshot.state = STOPPING
            snapshot.current_stage = stage
            if turn is not None:
                snapshot.current_turn = turn
            snapshot.updated_at = _now()

    def _transition(self, handle: VlnRunHandle, state: str) -> None:
        with self._lock:
            snapshot = self._snapshots.get(handle.run_id)
            if snapshot is None or snapshot.state in _TERMINAL:
                return
            if state not in _TRANSITIONS[snapshot.state]:
                raise ValueError(f"illegal transition {snapshot.state} -> {state}")
            snapshot.state = state
            snapshot.updated_at = _now()
            if state == RUNNING and snapshot.started_at is None:
                snapshot.started_at = _now()

    def _finish(
        self,
        handle: VlnRunHandle,
        state: str,
        termination_reason: str,
        error_message: str | None,
    ) -> None:
        with self._lock:
            snapshot = self._snapshots.get(handle.run_id)
            if snapshot is None or snapshot.state in _TERMINAL:
                return  # first writer wins; late teardowns are no-ops
            if state not in _TRANSITIONS[snapshot.state]:
                raise ValueError(f"illegal transition {snapshot.state} -> {state}")
            snapshot.state = state
            snapshot.current_stage = None
            snapshot.termination_reason = termination_reason
            snapshot.error_message = error_message
            snapshot.finished_at = _now()
            snapshot.updated_at = snapshot.finished_at
            if self._current is handle:
                self._current = None

    # -- queries ---------------------------------------------------------------

    def snapshot(self, run_id: str | None = None) -> TaskSnapshot | None:
        """The snapshot for `run_id`, or the most recent run's when None."""
        with self._lock:
            if run_id is not None:
                return self._snapshots.get(run_id)
            return self._snapshots[self._order[-1]] if self._order else None

    def _remember(self, snapshot: TaskSnapshot) -> None:
        self._snapshots[snapshot.run_id] = snapshot
        self._order.append(snapshot.run_id)
        while len(self._order) > self._HISTORY:
            del self._snapshots[self._order.pop(0)]
