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

"""VLN-M1-01 task-lifecycle tests (T01-T15).

Covers the shared VLN task runtime (state machine, run handles, snapshot
retention, staleness) and both skills' wiring into it: status queries mid-run,
user cancel, duplicate-start refusal, stale-result dropping, every failure
path's zero-velocity guarantee, and the CAP_MOVEMENT release chain (the
tool_stopped frame) against the real CapabilityRegistry — with deterministic
Event/Barrier synchronization, no real backends, no robots.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from dimos.agents.capabilities import CAP_MOVEMENT, CapabilityRegistry
from dimos.core.module import Module
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.navigation.experimental.active_vln.skill import (
    _FORWARD,
    _STOP,
    ActiveVlnConfig,
    ActiveVlnSkill,
)
import dimos.navigation.experimental.active_vln.test_active_vln as avln_test
from dimos.navigation.experimental.stream_vln.skill import (
    StreamVlnConfig,
    StreamVlnSkill,
)
from dimos.navigation.experimental.vln_task import (
    CANCELLED,
    COMPLETED,
    CREATED,
    EXECUTION_ERROR,
    FAILED,
    INFERENCE,
    POLICY_ERROR,
    POLICY_STOP,
    RUNNING,
    SENSOR_UNAVAILABLE,
    STALE_AFTER_S,
    STOPPING,
    TURN_BUDGET_EXHAUSTED,
    USER_CANCELLED,
    VlnTaskRuntime,
)

# -- T01: state transitions ------------------------------------------------------


def test_t01_lifecycle_transitions() -> None:
    runtime = VlnTaskRuntime("stream_vln")
    handle = runtime.begin("go", 5)
    assert handle is not None
    assert runtime.snapshot(handle.run_id).state == CREATED
    runtime.mark_running(handle)
    assert runtime.snapshot(handle.run_id).state == RUNNING
    runtime.set_stage(handle, INFERENCE, turn=1)
    assert runtime.snapshot(handle.run_id).current_stage == INFERENCE
    assert runtime.snapshot(handle.run_id).current_turn == 1
    # A cancel request mid-run flips the observed state to STOPPING (cancel
    # in flight — not "stopped"), the loop is still unwinding.
    runtime.request_cancel()
    assert runtime.current_state() == STOPPING
    handle.finish(CANCELLED, USER_CANCELLED)
    assert runtime.snapshot(handle.run_id).state == CANCELLED
    assert runtime.snapshot(handle.run_id).termination_reason == USER_CANCELLED
    assert runtime.snapshot(handle.run_id).finished_at is not None


def test_t01_illegal_transitions_rejected() -> None:
    runtime = VlnTaskRuntime("stream_vln")
    handle = runtime.begin("go", 5)
    assert handle is not None
    with pytest.raises(ValueError):
        handle.finish(COMPLETED, POLICY_STOP)  # CREATED -> COMPLETED is illegal
    runtime.mark_running(handle)
    handle.finish(COMPLETED, POLICY_STOP)
    # Terminal states are final: a LATE teardown is a first-writer-wins no-op
    # (the run loop, the entry's exception path and the finally can all try).
    handle.finish(FAILED, EXECUTION_ERROR)
    assert runtime.snapshot(handle.run_id).state == COMPLETED


def test_t01_finish_is_first_writer_wins() -> None:
    runtime = VlnTaskRuntime("stream_vln")
    handle = runtime.begin("go", 5)
    assert handle is not None
    runtime.mark_running(handle)
    handle.finish(CANCELLED, USER_CANCELLED)
    handle.finish(COMPLETED, POLICY_STOP)  # a late teardown must not rewrite
    assert runtime.snapshot(handle.run_id).state == CANCELLED


# -- T02: normal end + snapshot retention ---------------------------------------


def test_t02_completion_and_retention() -> None:
    runtime = VlnTaskRuntime("stream_vln")
    run_ids = []
    for i in range(3):
        handle = runtime.begin(f"task {i}", 5)
        assert handle is not None
        run_ids.append(handle.run_id)
        runtime.mark_running(handle)
        handle.finish(COMPLETED, POLICY_STOP)
    # Every finished run stays queryable by its run_id...
    for i, run_id in enumerate(run_ids):
        snapshot = runtime.snapshot(run_id)
        assert snapshot.state == COMPLETED
        assert snapshot.instruction == f"task {i}"
    # ...and the most recent one is the default query target.
    assert runtime.snapshot().run_id == run_ids[-1]


def test_t02_history_is_bounded() -> None:
    runtime = VlnTaskRuntime("stream_vln")
    first = None
    for i in range(12):  # history keeps 8
        handle = runtime.begin(f"task {i}", 5)
        assert handle is not None
        if i == 0:
            first = handle.run_id
        runtime.mark_running(handle)
        handle.finish(COMPLETED, POLICY_STOP)
    assert runtime.snapshot(first) is None  # aged out
    assert runtime.snapshot() is not None


# -- stale snapshot marking (query must not present a wedged run as healthy) -----


def test_stale_running_snapshot_is_flagged() -> None:
    runtime = VlnTaskRuntime("stream_vln")
    handle = runtime.begin("go", 5)
    assert handle is not None
    runtime.mark_running(handle)
    fresh = runtime.snapshot(handle.run_id).as_dict()
    assert fresh["stale_s"] is None
    runtime._snapshots[handle.run_id].updated_at -= STALE_AFTER_S + 5.0
    stale = runtime.snapshot(handle.run_id).as_dict()
    assert stale["stale_s"] is not None and stale["stale_s"] > 0
    # Finished snapshots are never stale-marked.
    handle.finish(COMPLETED, POLICY_STOP)
    runtime._snapshots[handle.run_id].updated_at -= STALE_AFTER_S + 5.0
    assert runtime.snapshot(handle.run_id).as_dict()["stale_s"] is None


# -- skill-level lifecycle fixtures ----------------------------------------------


class _Gate:
    """Deterministic gating for threaded run tests (no sleep races)."""

    def __init__(self) -> None:
        self.inference_entered = threading.Event()
        self.release_inference = threading.Event()
        self.execute_entered = threading.Event()
        self.release_execute = threading.Event()


def _stream_skill(
    gate: _Gate | None = None, real_execute: bool = False
) -> tuple[StreamVlnSkill, list[Any]]:
    """A stream_vln stub whose backend blocks on the gate's events.

    The fakes sit where the loop expects parsed data: _post_frame returns
    the coalesced action list (what the real client produces), _execute
    records and succeeds instantly.
    """
    executed: list[Any] = []
    skill = StreamVlnSkill.__new__(StreamVlnSkill)
    skill.config = StreamVlnConfig(max_turns=3, request_timeout=5.0)
    skill._running = threading.Event()
    skill._runtime = VlnTaskRuntime("stream_vln")
    skill._latest_image = avln_test._red_bgr_image()
    skill._latest_odom = avln_test._pose(0.0, 0.0, 0.0)
    skill.tools_opened: list[str] = []
    skill.tools_closed: list[str] = []
    skill.tool_updates: list[Any] = []
    skill.start_tool = lambda name: skill.tools_opened.append(name)  # type: ignore[method-assign]
    skill.stop_tool = lambda name: skill.tools_closed.append(name)  # type: ignore[method-assign]
    skill.tool_update = lambda name, msg: skill.tool_updates.append((name, msg))  # type: ignore[method-assign]

    class _Tele:
        twists: list[Twist] = []

        def publish(self, twist: Twist) -> None:
            self.twists.append(twist)

    class _Nav:
        cancelled = 0

        def cancel_goal(self) -> bool:
            _Nav.cancelled += 1
            return True

    skill.tele_cmd_vel = _Tele()
    skill._navigation = _Nav()

    def fake_post_frame(image: Any, reset: bool, instruction: str) -> list[tuple[int, float]]:
        if gate is not None:
            gate.inference_entered.set()
            gate.release_inference.wait(timeout=10.0)
        return [(_FORWARD, 25.0)]

    def fake_execute(action_id: int, magnitude: float, handle: Any = None) -> bool:
        executed.append((action_id, magnitude))
        return True

    skill._post_frame = fake_post_frame  # type: ignore[method-assign]
    if not real_execute:  # T11 runs the real _execute -> _pd_drive chain
        skill._execute = fake_execute  # type: ignore[method-assign]
    return skill, executed


def _active_skill(gate: _Gate | None = None, answers: list[str] | None = None) -> ActiveVlnSkill:
    skill = ActiveVlnSkill.__new__(ActiveVlnSkill)
    skill.config = ActiveVlnConfig(max_turns=3)
    skill._running = threading.Event()
    skill._runtime = VlnTaskRuntime("active_vln")
    skill._latest_image = avln_test._red_bgr_image()
    skill._latest_odom = avln_test._pose(0.0, 0.0, 0.0)
    skill.tools_opened: list[str] = []
    skill.tools_closed: list[str] = []
    skill.tool_updates: list[Any] = []
    skill.start_tool = lambda name: skill.tools_opened.append(name)  # type: ignore[method-assign]
    skill.stop_tool = lambda name: skill.tools_closed.append(name)  # type: ignore[method-assign]
    skill.tool_update = lambda name, msg: skill.tool_updates.append((name, msg))  # type: ignore[method-assign]

    class _Tele:
        twists: list[Twist] = []

        def publish(self, twist: Twist) -> None:
            self.twists.append(twist)

    class _Nav:
        cancelled = 0

        def cancel_goal(self) -> bool:
            _Nav.cancelled += 1
            return True

    skill.tele_cmd_vel = _Tele()
    skill._navigation = _Nav()
    remaining = list(answers or [])

    def fake_chat(client: Any, messages: list[dict[str, Any]]) -> str:
        if gate is not None:
            gate.inference_entered.set()
            gate.release_inference.wait(timeout=10.0)
        return remaining.pop(0) if remaining else "stop"

    skill._client = lambda: object()  # type: ignore[method-assign]
    skill._chat = fake_chat  # type: ignore[method-assign]
    skill._run_action = lambda action_id, magnitude, handle=None: True  # type: ignore[method-assign]
    return skill


def _run_in_thread(call: Any) -> threading.Thread:
    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    return thread


def _status(skill: Any, run_id: str | None = None) -> dict[str, Any]:
    method = type(skill).get_vln_task_status
    return json.loads(method(skill, run_id) if run_id is not None else method(skill))


# -- T03/T04: status queries while running ---------------------------------------


def test_t03_query_during_inference() -> None:
    gate = _Gate()
    skill, _ = _stream_skill(gate)
    thread = _run_in_thread(lambda: StreamVlnSkill.stream_vln(skill, "walk to the sofa"))
    assert gate.inference_entered.wait(timeout=5.0)

    status = _status(skill)  # the agent queries while inference blocks
    assert status["state"] == RUNNING
    assert status["current_stage"] == INFERENCE
    assert status["current_turn"] == 1
    assert status["policy_name"] == "stream_vln"
    assert status["instruction"] == "walk to the sofa"
    assert status["stale_s"] is None

    gate.release_inference.set()
    thread.join(timeout=10.0)
    assert _status(skill)["state"] in (COMPLETED, CANCELLED)


def test_t04_query_during_action_execution() -> None:
    skill, _ = _stream_skill()
    gate = _Gate()
    executed: list[Any] = []

    def fake_execute(action_id: int, magnitude: float, handle: Any = None) -> bool:
        executed.append((action_id, magnitude))
        if not executed:
            return True
        gate.execute_entered.set()
        gate.release_execute.wait(timeout=10.0)
        return True

    skill._execute = fake_execute  # type: ignore[method-assign]
    thread = _run_in_thread(lambda: StreamVlnSkill.stream_vln(skill, "walk to the sofa"))
    assert gate.execute_entered.wait(timeout=5.0)

    status = _status(skill)
    assert status["state"] == RUNNING
    assert status["current_stage"] == "ACTION_EXECUTION"

    gate.release_execute.set()
    thread.join(timeout=10.0)


# -- T05: user stop ----------------------------------------------------------------


def test_t05_user_stop_cancels_and_settles() -> None:
    gate = _Gate()
    skill, _ = _stream_skill(gate)
    thread = _run_in_thread(lambda: StreamVlnSkill.stream_vln(skill, "walk to the sofa"))
    assert gate.inference_entered.wait(timeout=5.0)

    skill._runtime.request_cancel()  # what stop_stream_vln signals
    gate.release_inference.set()  # the stale answer arrives after the cancel
    thread.join(timeout=10.0)

    status = _status(skill)
    assert status["state"] == CANCELLED
    assert status["termination_reason"] == USER_CANCELLED
    assert status["finished_at"] is not None
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()
    assert skill._navigation.cancelled >= 1


# -- T06: duplicate start while running/stopPING ----------------------------------


def test_t06_duplicate_start_refused_until_exit() -> None:
    gate = _Gate()
    skill, _ = _stream_skill(gate)
    first = _run_in_thread(lambda: StreamVlnSkill.stream_vln(skill, "first task"))
    assert gate.inference_entered.wait(timeout=5.0)

    # RUNNING: a second start must not create a second motion-authoritative run.
    second = StreamVlnSkill.stream_vln(skill, "second task")
    assert "already in progress" in second
    assert "first task" == _status(skill)["instruction"]

    # STOPPING (cancel in flight, loop still unwinding): still refused.
    skill._runtime.request_cancel()
    assert "already in progress" in StreamVlnSkill.stream_vln(skill, "second task")
    assert _status(skill)["state"] in (STOPPING, CANCELLED)

    gate.release_inference.set()
    first.join(timeout=10.0)

    # Fully exited: a new run may start.
    third = _run_in_thread(lambda: StreamVlnSkill.stream_vln(skill, "third task"))
    assert gate.inference_entered.wait(timeout=5.0)
    gate.release_inference.set()
    third.join(timeout=10.0)
    assert _status(skill)["instruction"] == "third task"


# -- T07: stale model answer after cancel is dropped -------------------------------


def test_t07_stale_result_after_cancel_never_drives() -> None:
    gate = _Gate()
    skill, executed = _stream_skill(gate)
    thread = _run_in_thread(lambda: StreamVlnSkill.stream_vln(skill, "task A"))
    assert gate.inference_entered.wait(timeout=5.0)

    skill._runtime.request_cancel()  # user stops task A while inference blocks
    gate.release_inference.set()  # ...and the delayed answer returns actions
    thread.join(timeout=10.0)

    # Task A's late answer was dropped wholesale, nothing executed.
    assert executed == []
    assert _status(skill)["state"] == CANCELLED

    # Task B starts fresh and executes normally.
    gate_b = _Gate()
    skill_b, executed_b = _stream_skill(gate_b)
    executed_b.clear()
    thread_b = _run_in_thread(lambda: StreamVlnSkill.stream_vln(skill_b, "task B"))
    assert gate_b.inference_entered.wait(timeout=5.0)
    gate_b.release_inference.set()
    thread_b.join(timeout=10.0)
    assert _status(skill_b)["state"] in (COMPLETED, CANCELLED)


# -- T08/T09/T10/T11: failure paths and zero-velocity guarantees -------------------


def test_t08_policy_error_activevln() -> None:
    skill = _active_skill(answers=[])

    def broken_chat(client: Any, messages: list[dict[str, Any]]) -> str:
        raise RuntimeError("vllm down")

    skill._chat = broken_chat  # type: ignore[method-assign]
    result = ActiveVlnSkill.active_vln(skill, "walk to the sofa")
    assert result.startswith("Policy endpoint error")
    status = _status(skill)
    assert status["state"] == FAILED
    assert status["termination_reason"] == POLICY_ERROR
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()
    assert skill.tools_closed == ["active_vln"]


def test_t09_executor_exception_settles_failed() -> None:
    skill, _ = _stream_skill()

    def crashing_execute(action_id: int, magnitude: float, handle: Any = None) -> bool:
        raise RuntimeError("actuator lost")

    skill._execute = crashing_execute  # type: ignore[method-assign]
    result = StreamVlnSkill.stream_vln(skill, "walk to the sofa")
    assert "failed unexpectedly" in result
    status = _status(skill)
    assert status["state"] == FAILED
    assert status["termination_reason"] == EXECUTION_ERROR
    assert "actuator lost" in status["error_message"]
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()
    assert skill.tools_closed == ["stream_vln"]


def test_t10_sensor_unavailable() -> None:
    skill = _active_skill(answers=["move forward 25cm", "stop"])
    skill._latest_image = None  # camera already gone at the start
    result = ActiveVlnSkill.active_vln(skill, "walk to the sofa")
    assert result == "No camera or odometry data yet; cannot start active_vln."

    skill._latest_image = avln_test._red_bgr_image()
    skill._latest_odom = None  # odom gone: cannot start either
    assert (
        ActiveVlnSkill.active_vln(skill, "walk to the sofa")
        == "No camera or odometry data yet; cannot start active_vln."
    )

    # Mid-run camera loss (run started, then the stream dies).
    skill = _active_skill(answers=["move forward 25cm", "stop"])
    original = skill._run_action

    def drop_camera(action_id: int, magnitude: float, handle: Any = None) -> bool:
        skill._latest_image = None
        return original(action_id, magnitude, handle)

    skill._run_action = drop_camera  # type: ignore[method-assign]
    ActiveVlnSkill.active_vln(skill, "walk to the sofa")
    status = _status(skill)
    assert status["state"] == FAILED
    assert status["termination_reason"] == SENSOR_UNAVAILABLE
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()


def test_t11_unexpected_exception_zero_velocity_and_recovery() -> None:
    # A crash inside the real PD homing (broken odom object) must still leave
    # the robot at rest, settle the task, and let the next run start.
    skill, _ = _stream_skill(real_execute=True)

    class _BrokenPose:
        pass

    skill._latest_odom = _BrokenPose()  # .orientation missing -> AttributeError

    result = StreamVlnSkill.stream_vln(skill, "walk to the sofa")
    assert "failed unexpectedly" in result
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()
    assert skill.tools_closed == ["stream_vln"]
    assert _status(skill)["state"] == FAILED

    # Recovery: a fresh run starts cleanly after the crash.
    skill._latest_odom = avln_test._pose(0.0, 0.0, 0.0)
    gate = _Gate()
    skill_g, _ = _stream_skill(gate)
    thread = _run_in_thread(lambda: StreamVlnSkill.stream_vln(skill_g, "recovered run"))
    assert gate.inference_entered.wait(timeout=5.0)
    gate.release_inference.set()
    thread.join(timeout=10.0)
    assert _status(skill_g)["instruction"] == "recovered run"


# -- T12: CAP_MOVEMENT release via the tool_stopped frame ---------------------------


class _FakeTransport:
    """In-memory pub/sub standing in for the tool-stream transport."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._callbacks: list[Any] = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        return None

    def subscribe(self, callback: Any) -> Any:
        self._callbacks.append(callback)
        return lambda: None

    def publish(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)
        for callback in list(self._callbacks):
            callback(frame)


@pytest.fixture()
def real_tool_stream(monkeypatch: Any) -> tuple[_FakeTransport, dict[str, Any]]:
    """The REAL Module.start_tool/tool_update/stop_tool + ToolStream, on an
    in-memory bus, with a synthetic skill context carrying acquire tokens."""
    bus = _FakeTransport()
    import dimos.agents.mcp.tool_stream as tool_stream

    monkeypatch.setattr(tool_stream, "make_transport", lambda topic: bus)
    context = {"acquire_token": "token-1", "progress_token": "ptok"}
    monkeypatch.setattr(tool_stream, "current_skill_context", lambda: context)
    return bus, context


def _bind_real_tools(skill: Any) -> None:
    skill._tools = {}
    skill._tools_lock = threading.Lock()
    skill.start_tool = Module.start_tool.__get__(skill)  # type: ignore[method-assign]
    skill.tool_update = Module.tool_update.__get__(skill)  # type: ignore[method-assign]
    skill.stop_tool = Module.stop_tool.__get__(skill)  # type: ignore[method-assign]


def test_t12_capability_released_via_tool_stopped_frame(real_tool_stream: Any) -> None:
    bus, _context = real_tool_stream
    registry = CapabilityRegistry()
    assert registry.acquire([CAP_MOVEMENT], tool_name="stream_vln", token="token-1") is None, (
        "registry must take the hold"
    )

    skill, _ = _stream_skill()
    _bind_real_tools(skill)

    skill.start_tool("stream_vln")
    skill.tool_update("stream_vln", "turn 1/3: 1 actions")
    skill.stop_tool("stream_vln")

    stopped = [f for f in bus.frames if f.get("method") == "dimos/tool_stopped"]
    assert len(stopped) == 1
    assert stopped[0]["params"]["token"] == "token-1"  # the McpServer consumer
    assert registry.snapshot() == {CAP_MOVEMENT: "stream_vln"}  # still held...
    assert registry.release_by_token(stopped[0]["params"]["token"]) == [CAP_MOVEMENT]
    assert registry.snapshot() == {}  # ...released by exactly this frame


def test_t12_without_the_frame_the_hold_leaks_until_takeover(real_tool_stream: Any) -> None:
    """The M0 behavior, kept as evidence: a run that never closes its stream
    (what a hard-crashed worker looks like) leaves the registry held; the
    stop-tool takeover pattern recovers it."""
    _bus, _context = real_tool_stream
    registry = CapabilityRegistry()
    registry.acquire([CAP_MOVEMENT], tool_name="stream_vln", token="token-A")
    # The worker dies: no stop_tool, no tool_stopped frame, no release.
    assert registry.snapshot() == {CAP_MOVEMENT: "stream_vln"}
    registry.release_by_token("token-A")  # even a correct late release works...
    assert registry.snapshot() == {}

    # But the residue case: the frame never comes; a stop tool call takes
    # the same-tool hold over and releases it cleanly.
    registry.acquire([CAP_MOVEMENT], tool_name="stream_vln", token="token-A")
    registry.acquire([CAP_MOVEMENT], tool_name="stream_vln", token="token-B")  # takeover
    assert registry.release_by_token("token-B") == [CAP_MOVEMENT]
    assert registry.snapshot() == {}


# -- T13: stale run_id does not disturb the new run ---------------------------------


def test_t13_stale_run_id_queries_and_new_runs() -> None:
    runtime = VlnTaskRuntime("stream_vln")
    handle = runtime.begin("old task", 5)
    assert handle is not None
    runtime.mark_running(handle)
    handle.finish(CANCELLED, USER_CANCELLED)

    # Queries for the stale run only read history.
    assert runtime.snapshot(handle.run_id).state == CANCELLED
    # A new run is unaffected by the old one's identity...
    handle_b = runtime.begin("new task", 5)
    assert handle_b is not None
    assert handle_b.run_id != handle.run_id
    runtime.mark_running(handle_b)
    # ...and cancelling the new run never signals the old handle.
    runtime.request_cancel()
    assert handle_b.cancelled
    assert runtime.snapshot(handle.run_id).termination_reason == USER_CANCELLED


# -- T14: worker abnormal death ------------------------------------------------------


def test_t14_graceful_module_stop_closes_the_stream(
    real_tool_stream: Any, monkeypatch: Any
) -> None:
    """The graceful-shutdown half of the worker-death story: Module.stop()
    (invoked by python_worker's per-module instance.stop()) closes the tool
    stream, so an orderly worker exit releases the capability."""
    bus, _context = real_tool_stream
    skill, _ = _stream_skill()
    _bind_real_tools(skill)
    # Module.stop (the base lifecycle) needs the full Module machinery; what
    # is under test is the SKILL's stop(): it must close the live stream.
    monkeypatch.setattr(Module, "stop", lambda self: None)
    skill.start_tool("stream_vln")
    StreamVlnSkill.stop(skill)  # the module-lifecycle stop (no run needed)
    stopped = [f for f in bus.frames if f.get("method") == "dimos/tool_stopped"]
    assert len(stopped) == 1
    assert stopped[0]["params"]["tool_name"] == "stream_vln"


def test_t14_hard_death_residue_is_bounded_and_recoverable() -> None:
    """The hard-crash half: nothing in the crashed worker can emit a frame,
    so the registry keeps the hold until takeover or the RPC timeout. This
    is the documented limitation (design.md §CAP); the stop-tool takeover
    and the registry's token semantics bound it. Robot safety is covered by
    the cmd_vel stream going silent (a dead worker publishes no velocity)."""
    registry = CapabilityRegistry()
    # Another tool sees the dead holder and is refused (background holders
    # are never waited on — mcp_server _can_wait).
    registry.acquire([CAP_MOVEMENT], tool_name="stream_vln", token="dead-token")
    conflict = registry.acquire(
        [CAP_MOVEMENT], tool_name="navigate_with_text", token="n1", timeout=0.1
    )
    assert conflict == (CAP_MOVEMENT, "stream_vln")
    # A same-tool invocation (the stop tool's preempt-retry) takes over...
    assert registry.acquire([CAP_MOVEMENT], tool_name="stream_vln", token="stop-token") is None
    # ...and its release (instant tool: MCP's finally) clears the hold.
    assert registry.release_by_token("stop-token") == [CAP_MOVEMENT]
    assert registry.snapshot() == {}


# -- T15: both policies share the lifecycle semantics --------------------------------


@pytest.mark.parametrize("policy", ["stream_vln", "active_vln"])
def test_t15_policies_agree_on_semantics(policy: str) -> None:
    if policy == "stream_vln":
        gate = _Gate()
        skill, _ = _stream_skill(gate)
        entry, stop_signal = (
            lambda s: StreamVlnSkill.stream_vln(s, "p"),
            lambda s: s._runtime.request_cancel(),
        )
    else:
        gate = _Gate()
        skill = _active_skill(gate, answers=["move forward 25cm", "stop"])
        entry, stop_signal = (
            lambda s: ActiveVlnSkill.active_vln(s, "p"),
            lambda s: s._runtime.request_cancel(),
        )

    thread = _run_in_thread(lambda: entry(skill))
    assert gate.inference_entered.wait(timeout=5.0)
    status = _status(skill)
    assert status["policy_name"] == policy
    assert status["state"] == RUNNING
    assert status["current_stage"] == INFERENCE

    stop_signal(skill)
    gate.release_inference.set()
    thread.join(timeout=10.0)

    status = _status(skill)
    assert status["state"] == CANCELLED
    assert status["termination_reason"] == USER_CANCELLED
    assert status["policy_name"] == policy
    assert skill.tools_closed == [policy]
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()


@pytest.mark.parametrize("policy", ["stream_vln", "active_vln"])
def test_t15_policy_stop_semantics_agree(policy: str) -> None:
    """A policy that answers stop completes the task: COMPLETED/POLICY_STOP,
    never reported as navigation success."""
    if policy == "stream_vln":
        skill, _ = _stream_skill()
        skill._post_frame = lambda image, reset, instruction: [  # type: ignore[method-assign]
            (_STOP, 0.0)
        ]
        result = StreamVlnSkill.stream_vln(skill, "p")
    else:
        skill = _active_skill(answers=["stop"])
        result = ActiveVlnSkill.active_vln(skill, "p")

    assert result == "Policy said stop."
    status = _status(skill)
    assert status["state"] == COMPLETED
    assert status["termination_reason"] == POLICY_STOP
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()


@pytest.mark.parametrize("policy", ["stream_vln", "active_vln"])
def test_t15_turn_budget_semantics_agree(policy: str) -> None:
    if policy == "stream_vln":
        skill, _ = _stream_skill()
        skill._post_frame = lambda image, reset, instruction: [  # type: ignore[method-assign]
            (_FORWARD, 25.0)
        ]  # never stops
        result = StreamVlnSkill.stream_vln(skill, "p")
    else:
        skill = _active_skill(answers=["move forward 25cm"] * 3)
        skill._run_action = lambda action_id, magnitude, handle=None: True  # type: ignore[method-assign]
        result = ActiveVlnSkill.active_vln(skill, "p")

    assert "budget" in result.lower()
    status = _status(skill)
    assert status["state"] == COMPLETED
    assert status["termination_reason"] == TURN_BUDGET_EXHAUSTED
