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

"""VLN-M1-01B-OBSERVE-FOLLOWUP phase 1: the ORIGINAL-INCIDENT call chain,
rebuilt on the real machinery, at the CURRENT HEAD (edf8f9c).

The exact sequence the task specifies:

  USER_COMMAND
  -> the agent's turn calls active_vln (the fake MCP answers COMPLETED)
  -> the SAME turn then calls observe (the fake MCP answers an image)
  -> the image queues via the real call_tool / add_message
  -> the turn closes
  -> the loop consumes the observe image
  -> the ADVERSARIAL model tries to call active_vln AGAIN off the image

Everything real except the two IO edges (MCP HTTP reply, MockModel script).
The ledger is never touched: invocations are stamped by the real call_tool.

The evidence required — timestamps, message order, task state, and the
decisive answer: does the current HEAD allow the second active_vln?

Also included: the pre-fix control (what the same chain looked like under
the GATE code, from the recorded reproduction) for the before/after report.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from dimos.agents.agent import Agent
from dimos.agents.testing.mock_model import MockModel


def _image_block() -> dict[str, Any]:
    return {"type": "image", "data": "data:image/jpeg;base64,x"}


class _IncidentMcp:
    """MCP-edge fake answering the incident's two tools, with full logging."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.vln_results: list[dict[str, Any]] = [
            {"content": [{"type": "text", "text": "Policy said stop."}]},  # run 1: completes
        ]
        self.observe_results: list[dict[str, Any]] = [
            {"content": [{"type": "text", "text": "observation ok"}, _image_block()]},
            {"content": [{"type": "text", "text": "observation ok"}, _image_block()]},
        ]
        # If the adversarial second active_vln fires, answer it too — the
        # evidence must show the full damage when it happens.
        self.second_vln_answered = False

    def __call__(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        name = params.get("name") if params else method
        self.calls.append({"name": name, "t": time.time(), "args": params})
        if name == "active_vln":
            if self.vln_results:
                return self.vln_results.pop(0)
            # The adversarial second run, if it ever fires:
            self.second_vln_answered = True
            return {"content": [{"type": "text", "text": "SECOND RUN executed."}]}
        if name == "observe":
            if self.observe_results:
                return self.observe_results.pop(0)
            return {"content": [{"type": "text", "text": "no more images"}]}
        return {"content": [{"type": "text", "text": "ok"}]}


class _IncidentHarness:
    """Real-chain harness for the incident: real graph with BOTH tools."""

    def __init__(self, scripted: list[BaseMessage]) -> None:
        self.agent = Agent.__new__(Agent)
        a = self.agent
        a.config = None  # type: ignore[assignment]
        a._lock = threading.RLock()
        a._invocation_lock = threading.Lock()
        a._message_queue = queue.Queue()
        a._history = []
        a._tool_invocations = {}
        a._turn_tool_calls = None
        a._turn_opened_at = None
        a._prev_turn_opened_at = None
        a._stop_event = threading.Event()
        a._tool_stream_cleanup = None
        a._mcp_url = "http://fake/mcp"
        self.mcp = _IncidentMcp()
        a._mcp_request = self.mcp  # type: ignore[method-assign]
        self.display: list[Any] = []
        a.agent = self._out()  # type: ignore[assignment]
        a.agent_idle = self._out()  # type: ignore[assignment]
        a._server_url = lambda: "http://fake/mcp"  # type: ignore[method-assign]

        self.vln_tool = a._mcp_tool_to_langchain(
            {
                "name": "active_vln",
                "description": "vln",
                "inputSchema": {"type": "object", "properties": {}},
            }
        )
        self.observe_tool = a._mcp_tool_to_langchain(
            {
                "name": "observe",
                "description": "look",
                "inputSchema": {"type": "object", "properties": {}},
            }
        )
        from langchain.agents import create_agent

        a._state_graph = create_agent(
            model=MockModel(responses=scripted),
            tools=[self.vln_tool, self.observe_tool],
            system_prompt="",
        )

        # Incident timeline record (observation only).
        self.timeline: list[tuple[str, float]] = []
        self._trace_module_append()

    def _out(self) -> Any:
        parent = self

        class _Out:
            def publish(self, message: Any) -> None:
                parent.display.append(message)

        return _Out()

    def _trace_module_append(self) -> None:
        import dimos.agents.agent as agent_mod

        real_append = agent_mod._append_image_to_history
        harness = self

        def traced(agent: Any, fn: str, u: str, r: Any, iv: Any = None) -> None:
            harness.timeline.append(("image_queued", time.time()))
            real_append(agent, fn, u, r, iv)

        agent_mod._append_image_to_history = traced

    # -- drivers -------------------------------------------------------------
    def user(self, text: str) -> None:
        # The production _on_human_input tags USER_COMMAND; the harness
        # matches it (the guard's ground truth).
        from dimos.agents.mcp.constants import (
            MESSAGE_KIND_KEY,
            MESSAGE_KIND_USER_COMMAND,
        )

        self.timeline.append(("user_command", time.time()))
        Agent._process_message(
            self.agent,
            self.agent._state_graph,
            HumanMessage(
                content=text,
                additional_kwargs={MESSAGE_KIND_KEY: MESSAGE_KIND_USER_COMMAND},
            ),
        )

    def drain(self) -> int:
        turns = 0
        while True:
            try:
                msg = self.agent._message_queue.get_nowait()
            except queue.Empty:
                return turns
            self.timeline.append(("consume", time.time()))
            Agent._process_message(self.agent, self.agent._state_graph, msg)
            turns += 1

    # -- assertions ----------------------------------------------------------
    def artefact_count(self) -> int:
        return sum("artefact" in str(m.content) for m in self.agent._history)

    def vln_calls_in_history(self) -> int:
        return sum(
            1
            for m in self.agent._history
            if isinstance(m, AIMessage)
            and any(tc.get("name") == "active_vln" for tc in (m.tool_calls or []))
        )

    def motion_commands(self) -> int:
        """Actual active_vln INVOCATIONS that reached the (fake) MCP layer —
        the real 'did motion start' evidence."""
        return sum(1 for c in self.mcp.calls if c["name"] == "active_vln")


def _vln_call(cid: str, instruction: str = "走到红色椅子前停下") -> dict[str, Any]:
    return {
        "name": "active_vln",
        "args": {"instruction": instruction},
        "id": cid,
        "type": "tool_call",
    }


def _observe_call(cid: str) -> dict[str, Any]:
    return {"name": "observe", "args": {}, "id": cid, "type": "tool_call"}


# -- phase 1: the original incident at the CURRENT HEAD --------------------------


def test_phase1_original_incident_chain_at_current_head() -> None:
    """USER_COMMAND -> active_vln (COMPLETED) -> observe in the SAME turn ->
    image queues -> turn closes -> image consumed -> the adversarial model
    tries active_vln AGAIN off the image.

    The decisive question: at edf8f9c, does the second active_vln happen?
    """
    scripted = [
        # Turn 1's model calls: run the VLN task, then look at the result.
        AIMessage(content="", tool_calls=[_vln_call("v1"), _observe_call("c1")]),
        AIMessage(content="任务完成, 我在红色椅子前"),
        # The image-consumption turn's ADVERSARIAL response: re-run the task.
        AIMessage(content="", tool_calls=[_vln_call("v2")]),
        AIMessage(content="ran again"),
    ]
    h = _IncidentHarness(scripted)
    h.user("走到红色椅子前停下")  # the USER_COMMAND turn
    h.drain()  # consume the queued observe image (adversarial turn)

    # Full incident record.
    print("\n=== INCIDENT TIMELINE (current HEAD edf8f9c) ===")
    for label, t in h.timeline:
        print(f"  {t:.3f}  {label}")
    print("mcp calls:", [(c["name"], round(c["t"], 3)) for c in h.mcp.calls])
    print(
        f"artefacts_in_history={h.artefact_count()}  vln_calls_in_history={h.vln_calls_in_history()}  motion_invocations={h.motion_commands()}"
    )

    # Facts of the chain, however they fall:
    assert len(h.mcp.calls) >= 2, "the turn must have called active_vln then observe"
    assert h.artefact_count() == 1, "the observe image must have reached the model"
    # THE decisive assertion at the CURRENT code (with the product-turn
    # motion guard): the adversarial second call is REFUSED at the tool
    # layer — the motion never starts — while the image stays visible and
    # the refusal is a relatable tool result.
    second_vln = h.motion_commands() > 1
    print(f"\nSECOND ACTIVE_VLN ALLOWED: {second_vln} (must be False)")
    assert not second_vln, "the product-turn motion guard failed to stop the duplicate run"
    # The image stayed visible (layer 1 intact):
    assert h.artefact_count() == 1
    # The refusal reached the model as a tool result it can relay:
    assert h.mcp.second_vln_answered is False
    assert any("Refused" in str(m.content) for m in h.agent._history)


def test_phase1_control_gate_dropped_all_images() -> None:
    """The recorded pre-fix control (GATE code), for the before/after report.

    Under the GATE commit, the same chain dropped the image entirely
    (test_observe_reproduction.py proved it; this test asserts the CURRENT
    code does not regress to that). Kept as a live control: the observe
    image MUST reach the model at the current HEAD.
    """
    scripted = [
        AIMessage(content="", tool_calls=[_vln_call("v1"), _observe_call("c1")]),
        AIMessage(content="done"),
    ]
    h = _IncidentHarness(scripted)
    h.user("走到红色椅子前停下")
    h.drain()
    assert h.artefact_count() == 1, "image dropped: the GATE regression is back"
    assert h.motion_commands() == 1


# -- T11-T13: the three boundary tests ---------------------------------------------


def test_t11_observe_after_vln_completion_in_turn() -> None:
    """T11: same turn as the navigation: the VLN tool completes, observe
    runs in the SAME turn, its image is consumed later and is visible for
    task verification; the guard must not have blocked anything about the
    USER-driven first run, and the verification image stays usable.
    """
    scripted = [
        AIMessage(content="", tool_calls=[_vln_call("v1"), _observe_call("c1")]),
        AIMessage(content="任务完成, 红色椅子在前"),
        AIMessage(content="看到了红色椅子, 任务确实完成"),  # cooperative verification
    ]
    h = _IncidentHarness(scripted)
    h.user("走到红色椅子前停下")
    h.drain()
    # Image visible (verification usable):
    assert h.artefact_count() == 1
    # Only the user-driven run moved the robot:
    assert h.motion_commands() == 1
    # Task final state: the (fake) first run completed; nothing re-ran.
    assert h.vln_calls_in_history() == 1


def test_t12_adversarial_duplicate_within_tolerance() -> None:
    """T12: the 50 ms tolerance window. A duplicate artefact re-delivered
    immediately (the boundary the window gate cannot drop) with an
    ADVERSARIAL model scripted to re-call active_vln off it. The turn
    origin is tool_result: the motion call is refused at the tool layer
    regardless of the image's liveness — visibility and motion authority
    are separate (requirement E).
    """
    scripted = [
        AIMessage(content="", tool_calls=[_vln_call("v1"), _observe_call("c1")]),
        AIMessage(content="done"),
        AIMessage(content="", tool_calls=[_vln_call("v2")]),  # adversarial
        AIMessage(content="ran again"),
        AIMessage(content="", tool_calls=[_vln_call("v3")]),  # via duplicate too
    ]
    h = _IncidentHarness(scripted)
    h.user("走到红色椅子前停下")
    h.drain()
    first = next(m for m in h.agent._history if "artefact" in str(m.content))
    # Immediate duplicate within tolerance: allowed to the MODEL (visible
    # twice is harmless), but the origin-driven motion call is refused.
    h.agent._message_queue.put(first)
    h.drain()
    assert h.artefact_count() >= 1  # visible (boundary: may be 1 or 2)
    assert h.motion_commands() == 1, "an origin-driven duplicate motion slipped through"
    assert h.vln_calls_in_history() >= 2  # the model did try (adversarial)
    assert h.mcp.second_vln_answered is False


def test_t13_urgent_stop_vs_queued_tool_result_fifo() -> None:
    """T13: an urgent STOP message and a queued TOOL_RESULT race in FIFO.
    The stop lands first: the stop-turn runs (origin=user), the queued
    image is consumed after; its product-turn motion attempt (if the model
    makes one) is refused. Zero motion after the stop; zero duplicate runs.
    """
    from dimos.agents.mcp.constants import (
        MESSAGE_KIND_KEY,
        MESSAGE_KIND_USER_COMMAND,
    )

    scripted = [
        AIMessage(content="", tool_calls=[_vln_call("v1"), _observe_call("c1")]),
        AIMessage(content="done"),
        AIMessage(content="stopped"),
        AIMessage(content="", tool_calls=[_vln_call("v2")]),  # adversarial off image
        AIMessage(content="ran again"),
    ]
    h = _IncidentHarness(scripted)
    h.user("走到红色椅子前停下")  # turn: vln + observe queued
    # Simulate the urgent-stop path's message (the real one queues a
    # system note; here a plain user stop command, USER-tagged like
    # production) RACING the queued image: FIFO puts the stop first.
    h.agent._message_queue.queue.appendleft(
        HumanMessage(
            content="停止",
            additional_kwargs={MESSAGE_KIND_KEY: MESSAGE_KIND_USER_COMMAND},
        )
    )
    h.drain()  # consumes: stop first, then the image (and its adversarial turn)
    # The stop turn ran as a user turn; motion after it: only refusals.
    assert h.motion_commands() == 1  # the one user-driven run only
    assert h.mcp.second_vln_answered is False
    # The image turn existed and was visible, but no motion came of it:
    assert h.artefact_count() == 1
    assert h.vln_calls_in_history() == 2  # the model tried; refused
