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

"""VLN-M1-01B-OBSERVE step 1: a REAL-call-chain reproduction of the
observe-image lifecycle, before touching production code.

Everything is the production path:

  real Agent.__new__ + real fields
  → real Agent._process_message
  → real langchain `create_agent` state graph (a genuine LangGraph
    tool-node executor, not a fake .stream())
  → the tool built by the REAL Agent._mcp_tool_to_langchain
  → its real call_tool body (invocation stamping + _append_image_to_history
    + real Agent.add_message + queue)
  → real _close_agent_turn in _process_message's finally
  → the real _thread_loop-style queue consumption
  → the real liveness+age gate in _process_message

Only two things are faked, at the IO edges: the MCP HTTP request (mocked to
return a tool result carrying an image content block) and the LLM (MockModel
with scripted AIMessages). No test touches _tool_invocations,
_turn_tool_calls, or message metadata to force an outcome.

The timing question under test (from the task's call-chain note): within a
REAL agent turn, observe's call_tool runs INSIDE state_graph.stream, queues
its image via add_message, the stream then finishes, _close_agent_turn runs,
and only afterwards can the loop consume the queued image — whose invocation
ledger entry was just closed by its OWN turn. Does the gate drop it?
"""

from __future__ import annotations

import queue
import threading
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from dimos.agents.agent import Agent
from dimos.agents.testing.mock_model import MockModel


def _image_block() -> dict[str, Any]:
    return {"type": "image", "data": "data:image/jpeg;base64,x"}


class _McpServerFake:
    """HTTP-edge fake: answers tools/call with an image-bearing result."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(str(params.get("name") if params else method))
        return {
            "content": [
                {"type": "text", "text": "observation ok"},
                _image_block(),
            ]
        }


def _make_agent(
    scripted: list[BaseMessage],
) -> tuple[Agent, _McpServerFake, list[BaseMessage], list[BaseMessage]]:
    """A real-pathway agent: only the MCP HTTP call and the LLM are faked."""
    agent = Agent.__new__(Agent)
    agent.config = None  # type: ignore[assignment]  # not read on these paths
    agent._lock = threading.RLock()
    agent._invocation_lock = threading.Lock()
    agent._message_queue = queue.Queue()
    agent._history = []
    agent._tool_invocations = {}
    agent._turn_tool_calls = None
    agent._turn_opened_at = None
    agent._prev_turn_opened_at = None
    agent._stop_event = threading.Event()
    agent._tool_stream_cleanup = None
    agent._http_client = None  # type: ignore[assignment]
    agent._seq_ids = None  # type: ignore[assignment]
    agent._mcp_url = "http://fake/mcp"

    server_fake = _McpServerFake()
    agent._mcp_request = server_fake  # type: ignore[method-assign]

    published: list[BaseMessage] = []

    class _Out:
        def __init__(self) -> None:
            self.items: list[Any] = []

        def publish(self, message: Any) -> None:
            self.items.append(message)

    display = _Out()
    agent.agent = display  # type: ignore[assignment]
    agent.agent_idle = _Out()  # type: ignore[assignment]
    agent._server_url = lambda: "http://fake/mcp"  # type: ignore[method-assign]

    # The real tool, built by the real factory; the fake MCP answers it.
    tool = agent._mcp_tool_to_langchain(
        {
            "name": "observe",
            "description": "look",
            "inputSchema": {"type": "object", "properties": {}},
        }
    )

    # The REAL langchain create_agent graph, with the scripted model.
    model = MockModel(responses=scripted)
    from langchain.agents import create_agent

    agent._state_graph = create_agent(model=model, tools=[tool], system_prompt="")

    return agent, server_fake, published, display.items


def _run_turn(agent: Agent, message: BaseMessage) -> None:
    Agent._process_message(agent, agent._state_graph, message)


def _drain(agent: Agent) -> list[BaseMessage]:
    """Consume the queue exactly like _thread_loop does, one by one."""
    consumed = []
    while True:
        try:
            msg = agent._message_queue.get_nowait()
        except queue.Empty:
            return consumed
        _run_turn(agent, msg)
        consumed.append(msg)


def _call(name: str, cid: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


# -- the reproduction: a real turn whose tool returns an image ---------------


def test_repro_real_turn_obsserve_image_reaches_model_history() -> None:
    """THE reproduction. One user command; the model calls observe; the tool
    returns text+image through the real call_tool; the image is queued via
    the real add_message; the turn (stream) completes; the gate then judges
    the queued image on the real consumption path.
    """
    scripted = [
        AIMessage(content="", tool_calls=[_call("observe", "c1")]),
        AIMessage(content="I see the red chair."),
    ]
    agent, server, _published, _display = _make_agent(scripted)

    events: list[tuple[str, float]] = []

    # Instrument ONLY for observation (no outcome forcing): wrap the two
    # production functions with timestamps, calling the real ones.
    real_begin = Agent._begin_tool_invocation
    import dimos.agents.agent as agent_mod

    def timed_begin(self: Any, name: str) -> float:
        t = real_begin(self, name)
        events.append(("invocation_begin", t))
        return t

    Agent._begin_tool_invocation = timed_begin  # type: ignore[method-assign]
    try:
        real_close = Agent._close_agent_turn

        def timed_close(self: Any) -> None:
            events.append(("turn_close_before", 0.0))
            real_close(self)
            events.append(("turn_close_after", 0.0))

        Agent._close_agent_turn = timed_close  # type: ignore[method-assign]
        try:
            real_add = agent_mod._append_image_to_history

            def timed_add(ag: Any, fn: str, u: str, r: Any, iv: Any = None) -> None:
                import time as _t

                events.append(("image_queued", _t.time()))
                real_add(ag, fn, u, r, iv)

            agent_mod._append_image_to_history = timed_add
            try:
                # The user command turn.
                _run_turn(agent, HumanMessage(content="看一下周围"))
                # The queue now holds the image (queued DURING the stream).
                assert agent._message_queue.qsize() == 1, "image did not queue during the turn"
                # The turn has closed (its stream drained) — the exact
                # production sequence the task's call-chain note describes.
                assert ("turn_close_after", 0.0) in events
                # Now consume via the real path.
                consumed = _drain(agent)
                assert consumed, "queue drained empty"
            finally:
                agent_mod._append_image_to_history = real_add
        finally:
            Agent._close_agent_turn = real_close
    finally:
        Agent._begin_tool_invocation = real_begin

    # The measured real sequence.
    order = [e[0] for e in events]
    assert order.index("invocation_begin") < order.index("image_queued")
    assert order.index("image_queued") < order.index("turn_close_after")

    # THE question: did the image reach the model history, or was it dropped
    # by the liveness gate as a leftover of a closed turn?
    artefact_in_history = any("artefact" in str(m.content) for m in agent._history)
    consumed_turn = any(
        isinstance(m, HumanMessage) and "artefact" in str(m.content) for m in consumed
    )
    # Record BOTH facts — this test is the evidence, whichever way it goes.
    print(
        f"\nREAL-CHAIN RESULT: artefact_in_history={artefact_in_history} consumed_as_turn={consumed_turn}"
    )
    # Assert the full observable truth so the run fails loudly if the
    # behavior ever changes silently:
    assert artefact_in_history == consumed_turn  # gate and history agree
    # Whether VLN motion was triggered from this image: the scripted model
    # never calls active_vln; count any such call in history as a failure.
    assert not any(
        isinstance(m, AIMessage)
        and any(tc.get("name") == "active_vln" for tc in (m.tool_calls or []))
        for m in agent._history
    )


def test_repro_two_consecutive_turns_two_images() -> None:
    """Two real turns, each calling observe once. The production sequence
    repeated: does the second turn's image survive its own turn-close?"""
    scripted = [
        AIMessage(content="", tool_calls=[_call("observe", "c1")]),
        AIMessage(content="first seen"),
        AIMessage(content="", tool_calls=[_call("observe", "c2")]),
        AIMessage(content="second seen"),
    ]
    agent, _server, _published, _display = _make_agent(scripted)

    _run_turn(agent, HumanMessage(content="看一下周围"))
    first = _drain(agent)
    _run_turn(agent, HumanMessage(content="再看一次"))
    second = _drain(agent)

    n_artefacts = sum("artefact" in str(m.content) for m in agent._history)
    # Evidence print: both turns' images.
    print(f"\nTWO-TURN RESULT: artefacts_in_history={n_artefacts}")
    # Same agreement assertion as above.
    in_history = n_artefacts
    as_turns = sum(
        isinstance(m, HumanMessage) and "artefact" in str(m.content) for m in (first + second)
    )
    assert in_history == as_turns
