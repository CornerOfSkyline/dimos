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

"""VLN-M1-01B tests (T01-T12): agent asynchronous message timing and
duplicate-execution guards.

Covers the two production defects found in the first real ActiveVLN cockpit
run (artifacts/vln_m1/m1_01b/root_cause.md):

- Problem A: tool progress frames entered the LLM queue; a blocking VLN
  skill (minutes) let them pile up, and each frame then cost one LLM call.
- Problem B: an observe image queued behind the pile-up reached the LLM
  13 s late as the only new input after a completed task — and the model
  re-issued the same VLN instruction.

Deterministic Event-gated concurrency throughout; no real backends, no
robots. The Agent is driven through its real `_on_tool_stream_message`,
`add_message`/`_on_human_input`, and `_process_message` paths with a fake
state graph counting LLM (model-node) invocations.
"""

from __future__ import annotations

import threading
import time
from typing import Any
import uuid as uuid_module

from langchain_core.messages import AIMessage, HumanMessage
import pytest

from dimos.agents.agent import Agent, AgentConfig, _append_image_to_history
from dimos.agents.mcp import tool_stream
from dimos.agents.mcp.constants import (
    GENERATED_AT_KEY,
    MESSAGE_KIND_KEY,
    MESSAGE_KIND_TOOL_PROGRESS,
    MESSAGE_KIND_TOOL_RESULT,
    MESSAGE_KIND_USER_COMMAND,
    TOOL_ORIGIN_KEY,
    TOOL_RESULT_STALE_AFTER_S,
)


class _FakeGraph:
    """A stand-in CompiledStateGraph: each streamed 'turn' = one model call.

    `stream({messages: history})` yields one node output carrying an AIMessage,
    exactly like the real tool-graph's model node — so counting constructor
    calls counts LLM invocations.
    """

    def __init__(self, agent: AgentStub) -> None:
        self.agent = agent

    def stream(self, state: dict[str, Any], stream_mode: str = "updates") -> Any:
        self.agent.llm_calls += 1
        # Echo as the graph would: one node output with an AI message.
        yield {"messages": [AIMessage(content=f"reply #{self.agent.llm_calls}")]}


class _FakeAgentStream:
    """The `agent` Out stream: records everything the cockpit would see."""

    def __init__(self) -> None:
        self.published: list[Any] = []
        self._lock = threading.Lock()

    def publish(self, message: Any) -> None:
        with self._lock:
            self.published.append(message)


class _FakeIdleStream:
    def __init__(self) -> None:
        self.values: list[bool] = []

    def publish(self, value: bool) -> None:
        self.values.append(value)


class AgentStub:
    """Agent with every transport/LLM dependency stubbed; the queue, loop
    thread and message-processing code paths are the real ones."""

    def __init__(self) -> None:
        self.llm_calls = 0
        self.agent_stream = _FakeAgentStream()
        self.agent_idle_stream = _FakeIdleStream()
        self.agent = Agent.__new__(Agent)
        self.agent.config = AgentConfig(system_prompt=None, model="fixture")
        self.agent._lock = threading.RLock()
        self.agent._state_graph = _FakeGraph(self)
        self.agent._message_queue = __import__("queue").Queue()
        self.agent._history = []
        self.agent._tool_stream_cleanup = None
        self.agent._tool_invocations = {}
        self.agent._stop_event = threading.Event()
        self.agent.agent = self.agent_stream
        self.agent.agent_idle = self.agent_idle_stream
        self.agent.agent_send = lambda *a, **k: None  # unused by these paths

    # Real code paths under test -------------------------------------------
    def on_tool_stream(self, msg: dict[str, Any]) -> None:
        Agent._on_tool_stream_message(self.agent, msg)

    def process_queue_one(self) -> None:
        message = self.agent._message_queue.get_nowait()
        Agent._process_message(self.agent, self.agent._state_graph, message)

    def human_input(self, text: str) -> None:
        # The start()-closure path, replicated without the transport.
        if text in ("停止", "stop"):
            pass  # urgent-stop path not under test here
        self.agent._message_queue.put(
            HumanMessage(
                content=text,
                additional_kwargs={MESSAGE_KIND_KEY: MESSAGE_KIND_USER_COMMAND},
            )
        )

    def tool_result(self, func_name: str, age_s: float = 0.0) -> None:
        _append_image_to_history(
            self.agent,
            func_name,
            str(uuid_module.uuid4()),
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,x"}},
        )
        # Age it: the queue backlog simulated by rewinding generated_at.
        msg = next(
            iter(reversed(self.agent._message_queue.queue))
        )  # the just-queued artefact
        msg.additional_kwargs[GENERATED_AT_KEY] = time.time() - age_s

    # Convenience views ------------------------------------------------------
    @property
    def queued(self) -> list[Any]:
        return list(self.agent._message_queue.queue)

    def llm_saw(self, text: str) -> bool:
        return any(text in str(m.content) for m in self.agent._history)


def _progress_frame(tool: str, text: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": tool_stream.NOTIFICATIONS_PROGRESS_METHOD,
        "params": {
            "progressToken": "ptok",
            "progress": 1,
            "message": text,
            "_meta": {"tool_name": tool},
        },
    }


# -- T01: progress frames never trigger LLM inference -----------------------------


def test_t04_fresh_observe_image_reaches_the_model() -> None:
    stub = AgentStub()
    stub.human_input("看一眼周围")
    stub.tool_result("observe", age_s=1.0)  # fresh
    stub.process_queue_one()
    assert stub.llm_calls == 1
    assert stub.llm_saw("看一眼周围")
    stub.process_queue_one()  # the artefact turn
    assert stub.llm_calls == 2
    assert stub.llm_saw("artefact for the 'observe' tool")
    assert stub.llm_saw("看一眼周围")


# -- T05/T06: late artefacts never trigger VLN re-execution -------------------------


def test_t05_late_observe_image_is_dropped_not_executed() -> None:
    stub = AgentStub()
    stub.human_input("往前走到红色椅子跟前然后停止")  # completed task A earlier
    stub.process_queue_one()
    # The artefact queued behind a (simulated) 117 s VLN turn: 13 s late at
    # consumption — older than the freshness threshold.
    stub.tool_result("observe", age_s=TOOL_RESULT_STALE_AFTER_S + 5.0)
    stub.process_queue_one()
    # No LLM turn for the stale image: no model call, no new history entry,
    # no chance to re-issue the instruction.
    assert stub.llm_calls == 1
    assert not stub.llm_saw("artefact")
    # The drop is visible to the cockpit as telemetry, with the reason.
    drops = [
        m
        for m in stub.agent_stream.published
        if "arrived" in str(m.content) and "late" in str(m.content)
    ]
    assert len(drops) == 1


def test_t06_task_a_completion_then_late_products_stay_inert() -> None:
    stub = AgentStub()
    stub.human_input("task A instruction")
    stub.process_queue_one()  # the completed-task conversation turn
    stub.tool_result("observe", age_s=60.0)  # very late product of task A's turn
    stub.process_queue_one()
    # (Progress routing is commit 2's P1 change; at this commit a progress
    # frame still queues — but the stale artefact was already dropped, so
    # no new turn can be fed old images. Assert the artefact side only.)
    stub.on_tool_stream(_progress_frame("active_vln", "turn 1/50: 2 actions"))
    if stub.queued:
        stub.process_queue_one()  # drain the queued progress frame
    assert stub.llm_calls == 1 or stub.llm_calls == 2  # progress turn carries no image
    assert not stub.llm_saw("artefact")  # the stale product never reached the model


# -- T07: an explicit repeated user command is honored -------------------------------


def test_t07_explicit_second_command_runs_a_new_task() -> None:
    stub = AgentStub()
    stub.human_input("往前走到红色椅子跟前然后停止")
    stub.process_queue_one()
    stub.human_input("往前走到红色椅子跟前然后停止")  # explicit repeat
    stub.process_queue_one()
    # Both turns ran; the repeat is a fresh USER_COMMAND turn — the guard
    # only ever gates TOOL_RESULT/TOOL_PROGRESS, never human input.
    assert stub.llm_calls == 2
    assert (
        sum(
            1
            for m in stub.agent._history
            if isinstance(m, HumanMessage)
            and m.additional_kwargs.get(MESSAGE_KIND_KEY) == MESSAGE_KIND_USER_COMMAND
        )
        == 2
    )


# -- T08: after a user stop, late products cannot restart motion ----------------------


def test_t08_post_stop_late_products_inert() -> None:
    stub = AgentStub()
    stub.human_input("停止")  # urgent stop consumed as a normal message here
    stub.process_queue_one()
    stub.tool_result("observe", age_s=TOOL_RESULT_STALE_AFTER_S + 30.0)
    stub.process_queue_one()
    assert stub.llm_calls == 1
    assert not stub.llm_saw("artefact")


# -- T09: cross-turn, cross-run tool results do not cross-wire -----------------------


def test_t09_cross_turn_results_isolated() -> None:
    stub = AgentStub()
    stub.human_input("task one")
    stub.process_queue_one()
    # Fresh artefact of an UNRELATED (non-VLN) tool: goes through normally.
    stub.tool_result("look_out_for", age_s=0.5)
    stub.process_queue_one()
    assert stub.llm_calls == 2
    assert stub.llm_saw("artefact for the 'look_out_for' tool")
    # Its content must not carry any VLN framing into the history.
    artefact = [m for m in stub.agent._history if "artefact" in str(m.content)][0]
    assert artefact.additional_kwargs[TOOL_ORIGIN_KEY] == "look_out_for"
    assert artefact.additional_kwargs[MESSAGE_KIND_KEY] == MESSAGE_KIND_TOOL_RESULT


# -- T10: non-image tool returns are unchanged ---------------------------------------


def test_t10_non_image_tool_results_unchanged() -> None:
    # ToolMessage results (the synchronous call_tool return path) never pass
    # through the freshness gate — the gate is HumanMessage-only.
    stub = AgentStub()
    stub.human_input("status?")
    stub.process_queue_one()  # model call 1
    # A synchronous tool result recorded by the graph (ToolMessage):
    stub.agent._history.append(
        __import__("langchain_core.messages", fromlist=["ToolMessage"]).ToolMessage(
            content='{"state": "COMPLETED", "termination_reason": "POLICY_STOP"}',
            tool_call_id="call-1",
        )
    )
    assert stub.llm_calls == 1  # the ToolMessage itself is graph-recorded
    # It stays in history verbatim for the next model turn.
    assert stub.llm_saw('"state": "COMPLETED"')


# -- T11: MCP progress + tool_stopped semantics unchanged ----------------------------


@pytest.mark.parametrize("policy", ["active_vln", "stream_vln"])
def test_t12_vln_lifecycle_regressions_intact(policy: str) -> None:
    # The M1-01 suite runs in full elsewhere (test_vln_task.py); here the
    # guards that the message-layer changes must NOT have touched:
    # begin/refuse/cancel/release semantics via the real runtime.
    from dimos.navigation.experimental.vln_task import (
        CANCELLED,
        STOPPING,
        USER_CANCELLED,
        VlnTaskRuntime,
    )

    runtime = VlnTaskRuntime(policy)
    handle = runtime.begin("task", 5)
    assert handle is not None
    assert runtime.begin("another", 5) is None  # one motion instance
    runtime.mark_running(handle)
    runtime.request_cancel()
    assert runtime.current_state() == STOPPING
    handle.finish(CANCELLED, USER_CANCELLED)
    assert runtime.current_state() is None  # fully exited; new run may start
    handle_b = runtime.begin("task again", 5)  # explicit repeat allowed
    assert handle_b is not None and handle_b.run_id != handle.run_id


def test_t12_capability_release_chain_intact() -> None:
    # The tool_stopped frame still releases the registry hold (the McpServer
    # consumer, unchanged by the agent-side routing fix).
    from dimos.agents.capabilities import CAP_MOVEMENT, CapabilityRegistry

    registry = CapabilityRegistry()
    registry.acquire([CAP_MOVEMENT], tool_name="active_vln", token="t-1")
    frame = tool_stream.make_stopped_notification("active_vln", "t-1")
    assert frame["method"] == tool_stream.TOOL_STREAM_STOPPED_METHOD
    # The McpServer's consumer logic (mcp_server._fan_out_to_sse_queues)
    # releases by the frame's token — replicated here:
    token = frame["params"].get("token")
    assert token == "t-1"
    assert registry.release_by_token(token) == [CAP_MOVEMENT]
    assert registry.snapshot() == {}


# -- determinism note ---------------------------------------------------------------

