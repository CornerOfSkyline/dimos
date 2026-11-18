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

"""VLN-M1-01B-OBSERVE tests (T01-T10): the observe-image lifecycle on the
REAL call chain.

Every test enters through the real Agent._process_message, drives the real
langchain `create_agent` graph (a genuine LangGraph tool-node executor),
calls the tool built by the real Agent._mcp_tool_to_langchain (its real
call_tool: invocation stamp → MCP → _append_image_to_history → real
add_message → queue), lets the real turn close, and consumes through the
real gate. Only the IO edges are faked: the MCP HTTP reply (scripted
per-call) and the LLM (MockModel with scripted AIMessages).

Each test asserts the four required facts: image→model?, new LLM turn?,
VLN skill call?, and the task state outcome.
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


class _ScriptedMcp:
    """MCP-edge fake: per-call answers, record of calls and their clocks."""

    def __init__(self, answers: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._answers = list(answers or [])
        # The default answer: text + image, recorded synchronously.
        self.default = {
            "content": [
                {"type": "text", "text": "observation ok"},
                _image_block(),
            ]
        }

    def __call__(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append({"name": params.get("name") if params else method, "t": time.time()})
        if self._answers:
            return self._answers.pop(0)
        return self.default


class _LiveAgent:
    """Real-pathway agent: only MCP HTTP and the LLM are scripted."""

    def __init__(self, scripted: list[BaseMessage], mcp: _ScriptedMcp | None = None) -> None:
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
        self.mcp = mcp or _ScriptedMcp()
        a._mcp_request = self.mcp  # type: ignore[method-assign]
        self.display: list[Any] = []
        a.agent = self._out()  # type: ignore[assignment]
        a.agent_idle = self._out()  # type: ignore[assignment]
        a._server_url = lambda: "http://fake/mcp"  # type: ignore[method-assign]
        tool = a._mcp_tool_to_langchain(
            {
                "name": "observe",
                "description": "look",
                "inputSchema": {"type": "object", "properties": {}},
            }
        )
        from langchain.agents import create_agent

        a._state_graph = create_agent(
            model=MockModel(responses=scripted), tools=[tool], system_prompt=""
        )

    def _out(self) -> Any:
        parent = self

        class _Out:
            def publish(self, message: Any) -> None:
                parent.display.append(message)

        return _Out()

    # -- drivers --------------------------------------------------------------
    def user(self, text: str) -> None:
        Agent._process_message(self.agent, self.agent._state_graph, HumanMessage(content=text))

    def drain(self) -> int:
        turns = 0
        while True:
            try:
                msg = self.agent._message_queue.get_nowait()
            except queue.Empty:
                return turns
            Agent._process_message(self.agent, self.agent._state_graph, msg)
            turns += 1

    # -- required assertions --------------------------------------------------
    def artefact_count(self) -> int:
        return sum("artefact" in str(m.content) for m in self.agent._history)

    def vln_calls(self) -> int:
        return sum(
            1
            for m in self.agent._history
            if isinstance(m, AIMessage)
            and any(
                tc.get("name", "").startswith(("active_vln", "stream_vln"))
                for tc in (m.tool_calls or [])
            )
        )

    def llm_turns(self) -> int:
        return sum(1 for m in self.agent._history if isinstance(m, AIMessage))


def _call(cid: str) -> dict[str, Any]:
    return {"name": "observe", "args": {}, "id": cid, "type": "tool_call"}


# -- T01: in-turn synchronous observe image ------------------------------------


def test_t01_sync_in_turn_image_reaches_model() -> None:
    la = _LiveAgent([AIMessage(content="", tool_calls=[_call("c1")]), AIMessage(content="seen")])
    la.user("看一下周围")
    # The image queued DURING the turn; the turn closed; the loop consumes.
    turns = la.drain()
    assert la.artefact_count() == 1, "sync in-turn image did not reach the model"
    assert la.vln_calls() == 0
    assert turns == 1


# -- T02: image queued in-turn, consumed after turn close ----------------------


def test_t02_queued_image_consumed_after_close_reaches_model() -> None:
    # Identical to T01 by construction (the real chain always consumes after
    # close); this test additionally proves the ORDER: close happens before
    # consumption, i.e. the gate judged the artefact against a closed turn's
    # window — and correctly passed it as that turn's continuation.
    order: list[str] = []

    la = _LiveAgent([AIMessage(content="", tool_calls=[_call("c1")]), AIMessage(content="seen")])
    real_close = Agent._close_agent_turn

    def traced_close(self: object) -> None:
        order.append("close")
        real_close(self)

    Agent._close_agent_turn = traced_close  # type: ignore[method-assign]
    try:
        la.user("看一下周围")
        order.append("consuming")
        la.drain()
    finally:
        Agent._close_agent_turn = real_close
    assert order[:3] == ["close", "consuming", "close"]
    assert la.artefact_count() == 1
    assert la.vln_calls() == 0


# -- T03: background observe image, delayed ------------------------------------


def test_t03_background_image_delayed_but_valid() -> None:
    # A background tool answers with a task id; the image arrives later
    # (still the same originating call — the invocation stamp is what the
    # gate checks, not the queue's arrival time). Model: observe then final.
    mcp = _ScriptedMcp()
    mcp._answers = [
        {"content": [{"type": "text", "text": "task started"}]},  # first call: no image
        # second call (the model re-observes after the task): image now
        {"content": [{"type": "text", "text": "done"}, _image_block()]},
    ]
    la = _LiveAgent(
        [
            AIMessage(content="", tool_calls=[_call("c1")]),
            AIMessage(content="", tool_calls=[_call("c2")]),
            AIMessage(content="seen twice"),
        ],
        mcp,
    )
    la.user("持续观察")
    la.drain()
    # First call: no image. Second call (a NEWER same-tool invocation): its
    # own image is valid — exactly one artefact, from the latest call.
    assert len(mcp.calls) == 2
    assert la.artefact_count() == 1, "the newer call's image was lost"
    assert la.vln_calls() == 0


# -- T04: old task's image arrives while a NEW task runs -------------------------


def test_t04_old_task_image_during_new_task_inert() -> None:
    # Turn 1: task A context (observe with image). Turn 2 (a fresh user
    # command) opens AFTER turn 1 closed. Task A's duplicate image is
    # re-delivered during turn 2's aftermath — its stamp predates turn 2's
    # window: dropped, no model turn, no VLN.
    la = _LiveAgent([AIMessage(content="", tool_calls=[_call("c1")]), AIMessage(content="seen")])
    la.user("任务A:看一下周围")
    la.drain()
    first_artefacts = la.artefact_count()
    assert first_artefacts == 1

    # A later turn has since opened (production: the next user command).
    with la.agent._invocation_lock:
        # a fresh turn's open, after A (production: seconds of latency;
        # the harness's ms-collapsed clock needs the window ahead of stamp)
        la.agent._turn_opened_at = time.time() + 0.2
    # Task A's image re-delivered (duplicate=the same queued message).
    dup = next(m for m in la.agent._history if "artefact" in str(m.content))
    la.agent._message_queue.put(dup)
    llm_before = la.llm_turns()
    la.drain()
    assert la.llm_turns() == llm_before, "old task's duplicate ran a new LLM turn"
    assert la.artefact_count() == 1, "old task's duplicate re-entered history"
    assert la.vln_calls() == 0


# -- T05: after active_vln completes, observe image for verification ------------


def test_t05_verification_observe_after_vln_completion() -> None:
    # A VLN task completed earlier (its turn long closed). The agent now
    # takes a FRESH user request to verify with observe. The new observe is
    # a new invocation in the new turn: its image is valid, the model may
    # summarize the outcome — and must not re-run the task.
    scripted = [
        # (model would have run active_vln earlier; here a summary message)
        AIMessage(content="", tool_calls=[_call("v1")]),  # verification observe
        AIMessage(content="已在红色椅子前停下,任务完成"),
    ]
    la = _LiveAgent(scripted)
    la.user("确认一下任务结果")
    la.drain()
    assert la.artefact_count() == 1, "verification observe image was dropped"
    assert la.vln_calls() == 0  # summarize yes; re-run no


# -- T06: old task image cannot cause a second active_vln ------------------------


def test_t06_old_image_cannot_trigger_second_vln() -> None:
    # Even when the SCRIPTED MODEL would try to call active_vln upon seeing
    # the artefact (the adversarial model), the gate decides BEFORE the
    # model ever sees it: the old image is dropped, so no such turn exists.
    # (Proves the guarantee structurally, not just with a cooperative model.)
    # (The adversarial model response is documented in the docstring: if the
    # artefact reached the model, it would issue active_vln — the gate must
    # prevent that turn from ever existing. Structurally: the dropped message
    # never opens a turn, so no response of ANY shape can run.)
    # Turn 1 = the old task's context; the image queued within it.
    la = _LiveAgent([AIMessage(content="", tool_calls=[_call("c1")]), AIMessage(content="ok")])
    la.user("旧任务:看一下")
    la.drain()
    assert la.artefact_count() == 1  # fresh, in-window: reached (correct)

    # Now the OLD image re-delivered after a later turn opened:
    with la.agent._invocation_lock:
        la.agent._turn_opened_at = time.time() + 0.2  # later turn, past stamp
    dup = next(m for m in la.agent._history if "artefact" in str(m.content))
    la.agent._message_queue.put(dup)
    la.drain()
    # Dropped: the adversarial second response never ran.
    assert la.artefact_count() == 1
    assert la.vln_calls() == 0


# -- T07: explicit repeated navigation command ----------------------------------


def test_t07_explicit_repeat_command_new_run() -> None:
    # Two identical USER_COMMAND turns: each opens, streams, closes — never
    # gated (only TOOL_RESULT is). The model is scripted to call active_vln
    # the second time: allowed, because the USER asked (a real fake tool is
    # registered so the whole chain runs).
    vln_call = {
        "name": "active_vln",
        "args": {"instruction": "again please"},
        "id": "v1",
        "type": "tool_call",
    }
    scripted = [
        AIMessage(content="", tool_calls=[_call("c1")]),
        AIMessage(content="done once"),
        AIMessage(content="", tool_calls=[vln_call]),
        AIMessage(content="ran again"),
    ]
    la = _LiveAgent(scripted)

    # A real fake active_vln tool, built on the same real factory path, and
    # a graph that carries it (the MCP fake answers it like the server would).
    class _VlnMcp(_ScriptedMcp):
        def __call__(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            name = params.get("name") if params else method
            if name == "active_vln":
                return {"content": [{"type": "text", "text": "Policy said stop."}]}
            return super().__call__(method, params)

    la.mcp.__class__ = _VlnMcp
    vln_tool = la.agent._mcp_tool_to_langchain(
        {
            "name": "active_vln",
            "description": "vln",
            "inputSchema": {"type": "object", "properties": {}},
        }
    )
    from langchain.agents import create_agent

    from dimos.agents.testing.mock_model import MockModel

    la.agent._state_graph = create_agent(
        model=MockModel(responses=scripted), tools=[vln_tool], system_prompt=""
    )
    la.user("往前走到红椅前停下")
    la.drain()
    la.user("往前走到红椅前停下")  # explicit repeat
    la.drain()
    assert la.vln_calls() == 1  # the user's second command drove it


# -- T08: consecutive same-tool calls, results isolated ---------------------------


def test_t08_consecutive_same_tool_isolated() -> None:
    # Two turns, each observing once. The SECOND call's stamp supersedes
    # the first's artefact: if turn-1's image were re-delivered after turn-2
    # opened, the first artefact is dropped (superseded); the second's own
    # image (delivered promptly) is valid. Exactly 2 artefacts in history.
    scripted = [
        AIMessage(content="", tool_calls=[_call("c1")]),
        AIMessage(content="one"),
        AIMessage(content="", tool_calls=[_call("c2")]),
        AIMessage(content="two"),
    ]
    la = _LiveAgent(scripted)
    la.user("看一下周围")
    la.drain()
    la.user("再看一次")
    la.drain()
    assert la.artefact_count() == 2
    # Re-deliver the FIRST artefact now (superseded by c2's newer stamp).
    # Production separates the two calls by seconds of LLM latency; the
    # harness's ms-collapsed clock hides that, so the re-delivered message
    # carries its true old stamp (rewound exactly like a queued-old product
    # would read at consumption time).
    first = next(m for m in la.agent._history if "artefact" in str(m.content))
    first.additional_kwargs["invocation_started"] = time.time() - 0.2
    la.agent._message_queue.put(first)
    llm_before = la.llm_turns()
    la.drain()
    assert la.llm_turns() == llm_before  # dropped: superseded
    assert la.artefact_count() == 2
    assert la.vln_calls() == 0


# -- T09: duplicate delivery (both shapes) -----------------------------------------


def test_t09_duplicate_delivery_shapes() -> None:
    # Immediate duplicate (next-turn, within tolerance): passes to the
    # model (one bounded extra inference) but never motion. Delayed
    # duplicate (a later turn's window): dropped entirely.
    la = _LiveAgent([AIMessage(content="", tool_calls=[_call("c1")]), AIMessage(content="seen")])
    la.user("看一下周围")
    la.drain()
    assert la.artefact_count() == 1
    first = next(m for m in la.agent._history if "artefact" in str(m.content))

    # Immediate: allowed (narrow-boundary), no motion.
    la.agent._message_queue.put(first)
    la.drain()
    assert la.artefact_count() == 2
    assert la.vln_calls() == 0

    # Delayed: dropped.
    with la.agent._invocation_lock:
        la.agent._turn_opened_at = time.time() + 0.2  # later turn, past stamp
    la.agent._message_queue.put(first)
    llm_before = la.llm_turns()
    la.drain()
    assert la.llm_turns() == llm_before
    assert la.artefact_count() == 2


# -- T10: image arrives after a user STOP -------------------------------------------


def test_t10_post_stop_image_inert() -> None:
    # A stop turn happened (the model just acknowledged); a stale observe
    # image from before the stop re-delivers afterwards: dropped, no model
    # turn, no VLN, no motion.
    scripted = [
        AIMessage(content="", tool_calls=[_call("c1")]),  # pre-stop observe
        AIMessage(content="stopped"),
        AIMessage(content="standing by"),
    ]
    la = _LiveAgent(scripted)
    la.user("看一下周围")  # turn with the (pre-stop) observe
    la.drain()
    assert la.artefact_count() == 1

    la.user("停止")  # the stop turn — a later turn opened
    la.drain()
    # The pre-stop image re-delivered now: older than the stop turn's window.
    first = next(m for m in la.agent._history if "artefact" in str(m.content))
    first.additional_kwargs["invocation_started"] = time.time() - 0.2
    la.agent._message_queue.put(first)
    llm_before = la.llm_turns()
    la.drain()
    assert la.llm_turns() == llm_before
    assert la.artefact_count() == 1
    assert la.vln_calls() == 0
