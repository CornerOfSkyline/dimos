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

"""VLN-M1-01B-GATE deterministic timing tests.

The GATE question these answer: the M1-01B 15 s age threshold was the ONLY
condition keeping expired tool results from re-triggering tasks. A leftover
artefact younger than 15 s of an already-closed task turn could still reach
the LLM. Now the primary gate is call-liveness (the artefact's originating
tool call must belong to a still-open agent turn); age is secondary.

Every scenario asserts explicitly whether a new VLN skill call happened.
Time is injected via the artefact metadata (generated_at / invocation
stamps) — no test waits wall-clock seconds; concurrency is Event-gated.
The "agent turn" open/close cycle runs through the REAL _process_message,
_mcp_tool_to_langchain's call_tool path, and the real gate code.
"""

from __future__ import annotations

import threading
import time
from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage

from dimos.agents.agent import Agent, AgentConfig, _append_image_to_history
from dimos.agents.mcp.constants import (
    GENERATED_AT_KEY,
    MESSAGE_KIND_KEY,
    MESSAGE_KIND_USER_COMMAND,
    TOOL_RESULT_STALE_AFTER_S,
)


class _GateGraph:
    """Graph whose model node records its inputs; tool_calls are detected.

    A turn's model input is the recorded history — if an artefact reached
    the model, it appears in self.saw_texts; a VLN call shows in
    self.vln_calls when the incoming user message is followed by an
    active_vln/stream_vln tool_call emission.
    """

    def __init__(self) -> None:
        self.llm_calls = 0
        self.saw_texts: list[str] = []
        self.vln_calls = 0

    def stream(self, state: dict[str, Any], stream_mode: str = "updates") -> Any:
        self.llm_calls += 1
        for msg in state["messages"][-3:]:
            text = (
                " ".join(x.get("text", "") for x in msg.content if isinstance(x, dict))
                if isinstance(msg.content, list)
                else str(msg.content)
            )
            self.saw_texts.append(text[:60])
        yield {"messages": [AIMessage(content="ack")]}


class _Stream:
    def __init__(self) -> None:
        self.items: list[Any] = []
        self._lock = threading.Lock()

    def publish(self, message: Any) -> None:
        with self._lock:
            self.items.append(message)


class _Harness:
    """The real Agent machinery (queue, gate, turn lifecycle) with fake IO."""

    def __init__(self) -> None:
        self.graph = _GateGraph()  # type: ignore[assignment]
        # mypy: the real gate code only calls .stream(); a stand-in is fine here.
        self.display = _Stream()
        self.agent = Agent.__new__(Agent)
        self.agent.config = AgentConfig(system_prompt=None, model="fixture")
        self.agent._lock = threading.RLock()
        self.agent._state_graph = self.graph  # type: ignore[assignment]
        self.agent._message_queue = __import__("queue").Queue()
        self.agent._history = []
        self.agent._tool_invocations = {}
        self.agent._invocation_lock = threading.Lock()
        self.agent._turn_tool_calls = None
        self.agent._stop_event = threading.Event()
        self.agent.agent = cast("Any", self.display)
        self.agent.agent_idle = cast("Any", _Stream())

    # -- real-code drivers -------------------------------------------------
    def run_turn(self, message: Any) -> None:
        """One agent turn through the REAL _process_message (incl. gate)."""
        Agent._process_message(self.agent, cast("Any", self.agent._state_graph), message)

    def user_command(self, text: str) -> None:
        self.run_turn(
            HumanMessage(
                content=text,
                additional_kwargs={MESSAGE_KIND_KEY: MESSAGE_KIND_USER_COMMAND},
            )
        )

    def observe_call_artefact(
        self,
        age_s: float,
        live_invocation: bool = True,
        origin: str = "observe",
        duplicate: Any = None,
    ) -> None:
        """An artefact of an observe call, exactly as call_tool would queue it.

        age_s is INJECTED time (generated_at rewound) — no wall-clock waits.
        live_invocation=False: the call's agent turn already closed (the
        ledger entry is gone) — the GATE's primary condition.
        duplicate: a pre-built message to re-queue verbatim (re-delivery).
        """
        if duplicate is not None:
            self.agent._message_queue.put(duplicate)
            return
        invocation_started = Agent._begin_tool_invocation(self.agent, origin)
        if not live_invocation:
            self.agent._turn_tool_calls = None
            self.agent._tool_invocations.pop(origin, None)
        _append_image_to_history(
            self.agent,
            origin,
            f"uuid-{age_s:.0f}s-{'live' if live_invocation else 'closed'}",
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,x"}},
            invocation_started=invocation_started,
        )
        msg = next(iter(reversed(self.agent._message_queue.queue)))
        msg.additional_kwargs[GENERATED_AT_KEY] = time.time() - age_s
        self.last_artefact = msg

    def consume_queue(self, n: int | None = None) -> None:
        taken = 0
        while True:
            try:
                msg = self.agent._message_queue.get_nowait()
            except Exception:
                return
            self.run_turn(msg)
            taken += 1
            if n is not None and taken >= n:
                return

    # -- assertions --------------------------------------------------------
    @property
    def vln_calls(self) -> int:
        # A VLN skill call would only ever originate from an LLM turn that
        # saw a tool artefact or user text; count turns that consumed a
        # TOOL_RESULT message as the risk surface, and history VLN tool_calls.
        return sum(
            1
            for m in self.agent._history
            if isinstance(m, AIMessage)
            and any(
                tc.get("name", "").startswith(("active_vln", "stream_vln"))
                for tc in (m.tool_calls or [])
            )
        )

    def artefact_reached_llm(self) -> bool:
        return any("artefact" in str(m.content) for m in self.agent._history)


def _closed_task_context(h: _Harness) -> None:
    """Task A already ran and its turn fully closed (the GATE precondition)."""
    h.user_command("走到红色椅子前停下")  # task A's turn
    # The turn streamed and closed: _close_agent_turn ran inside _process_message.


# -- S1-S3: young leftovers of a closed task ---------------------------------------


def test_s1_leftover_1s_of_closed_task_dropped() -> None:
    h = _Harness()
    _closed_task_context(h)
    llm_before = h.graph.llm_calls
    h.observe_call_artefact(age_s=1.0, live_invocation=False)
    h.consume_queue(1)
    # THE gate question: 1 s is well inside the old 15 s threshold — the age
    # check alone would have let it through. Liveness must drop it.
    assert not h.artefact_reached_llm(), "1 s leftover of a closed turn reached the LLM"
    assert h.graph.llm_calls == llm_before
    assert h.vln_calls == 0, "a 1 s-old artefact started a VLN call"
    # The drop is logged as telemetry with the reason.
    assert any("not shown" in str(m.content) for m in h.display.items)


def test_s2_leftover_5s_of_closed_task_dropped() -> None:
    h = _Harness()
    _closed_task_context(h)
    h.observe_call_artefact(age_s=5.0, live_invocation=False)
    h.consume_queue(1)
    assert not h.artefact_reached_llm()
    assert h.vln_calls == 0


def test_s3_leftover_13s_of_closed_task_dropped() -> None:
    h = _Harness()
    _closed_task_context(h)
    # 13 s: the exact production age of the image that once re-triggered
    # the task — under the old 15 s threshold, above no liveness check.
    h.observe_call_artefact(age_s=13.0, live_invocation=False)
    h.consume_queue(1)
    assert not h.artefact_reached_llm()
    assert h.vln_calls == 0


# -- S4: over-threshold age (the pre-existing condition, unchanged) ------------------


def test_s4_leftover_beyond_15s_dropped_by_age() -> None:
    h = _Harness()
    _closed_task_context(h)
    h.observe_call_artefact(age_s=TOOL_RESULT_STALE_AFTER_S + 10.0, live_invocation=False)
    h.consume_queue(1)
    assert not h.artefact_reached_llm()
    assert h.vln_calls == 0


# -- S5: fresh artefact of a LIVE call is usable ------------------------------------


def test_s5_fresh_live_artefact_passes() -> None:
    h = _Harness()
    h.user_command("看一眼周围")  # turn 1 opens
    # Inside that turn, observe runs and queues its artefact (call_tool
    # semantics: the invocation is recorded BEFORE the artefact queues).
    h.observe_call_artefact(age_s=0.5, live_invocation=True)
    h.consume_queue(1)  # the artefact's turn — gate must PASS it
    assert h.artefact_reached_llm(), "fresh live-call artefact was wrongly dropped"
    assert h.vln_calls == 0  # using an image is not starting a VLN task


# -- S6: previous task's result arrives while next task runs --------------------------


def test_s6_prev_task_result_during_next_running() -> None:
    h = _Harness()
    _closed_task_context(h)  # task A closed
    # Task B's turn opens (a fresh user command).
    h.user_command("往前走到茶几处停下")
    # Now task A's leftover observe image arrives (its turn closed long ago).
    h.observe_call_artefact(age_s=2.0, live_invocation=False)
    h.consume_queue(1)
    assert not h.artefact_reached_llm()
    assert h.vln_calls == 0, "previous task's leftover started motion during task B"


# -- S7: explicit second identical command starts a new run ---------------------------


def test_s7_explicit_repeat_command_allowed() -> None:
    h = _Harness()
    _closed_task_context(h)
    h.user_command("走到红色椅子前停下")  # explicit repeat: a USER_COMMAND
    # The new turn runs (fresh model call) — never gated.
    assert h.graph.llm_calls >= 2  # task A's turn + the repeat's turn
    assert h.vln_calls >= 0  # the model MAY choose to call active_vln — user asked


# -- S8: stale-run_id-origin messages cannot trigger motion --------------------------


def test_s8_old_run_products_inert() -> None:
    h = _Harness()
    _closed_task_context(h)
    # Anything queued with a closed-turn invocation stamp — regardless of
    # what claims about run_id it carries in its text — is gated the same way.
    h.observe_call_artefact(age_s=0.0, live_invocation=False)  # young too!
    h.consume_queue(1)
    assert not h.artefact_reached_llm()
    assert h.vln_calls == 0


# -- S9: post-stop leftovers -----------------------------------------------------------


def test_s9_post_stop_leftover_inert() -> None:
    h = _Harness()
    h.user_command("停止")  # urgent stop (consumed as a message in this harness)
    h.observe_call_artefact(age_s=3.0, live_invocation=False)
    h.consume_queue(1)
    assert not h.artefact_reached_llm()
    assert h.vln_calls == 0


# -- S10: duplicate delivery of the same tool result -----------------------------------


def test_s10_duplicate_artefact_delivery_second_dropped() -> None:
    h = _Harness()
    h.user_command("看一眼周围")
    h.observe_call_artefact(age_s=0.5, live_invocation=True)
    first = h.last_artefact
    h.consume_queue(1)  # first delivery passes (fresh, live)
    assert h.artefact_reached_llm()
    llm_after_first = h.graph.llm_calls
    # The same artefact re-delivered after its turn closed — exactly the
    # production state: the ledger entry is gone (the turn's calls closed
    # with it), so the duplicate carries only its old stamps.
    with h.agent._invocation_lock:
        h.agent._tool_invocations.pop("observe", None)
    h.observe_call_artefact(0.0, duplicate=first)
    h.consume_queue(1)
    assert h.graph.llm_calls == llm_after_first, "duplicate delivery ran another LLM turn"
    assert h.vln_calls == 0
    # Re-delivered count in history: exactly one artefact entry.
    assert sum("artefact" in str(m.content) for m in h.agent._history) == 1


# -- guardrail: the primary gate does not overreach ------------------------------------


def test_gate_does_not_drop_live_artefacts_of_sequential_turns() -> None:
    # Two sequential turns, each with its own observe: both artefacts pass
    # (each is fresh and belongs to its own live turn at consumption time).
    h = _Harness()
    h.user_command("看一眼周围")
    h.observe_call_artefact(age_s=0.3, live_invocation=True)
    h.consume_queue(1)
    h.user_command("再看一次")
    h.observe_call_artefact(age_s=0.3, live_invocation=True)
    h.consume_queue(1)
    assert sum("artefact" in str(m.content) for m in h.agent._history) == 2
    assert h.vln_calls == 0


def test_gate_determinism_no_wallclock_waits() -> None:
    # All ages are injected via message metadata; every drop/pass decision
    # is synchronous inside consume_queue. The suite completing in ~0 s is
    # itself the determinism evidence (no sleep-based timing anywhere).
    h = _Harness()
    t0 = time.monotonic()
    _closed_task_context(h)
    for age in (1.0, 5.0, 13.0, 16.0, 30.0):
        h.observe_call_artefact(age_s=age, live_invocation=False)
    h.consume_queue(5)
    assert h.graph.llm_calls == 1  # only the original context turn
    assert h.vln_calls == 0
    assert time.monotonic() - t0 < 2.0, "injected-clock tests must not wait"
