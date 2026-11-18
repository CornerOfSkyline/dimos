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

from collections.abc import Callable, Iterator
import os
from pathlib import Path
from queue import Empty, Queue
import re
import threading
from threading import Event, RLock, Thread
import time
from typing import Any
import uuid
import warnings

from langchain_core._api.deprecation import LangChainPendingDeprecationWarning

# Importing langchain_core un-mutes its pending-deprecation warnings, so this ignore
# must be registered after that import to take precedence. It silences the noisy
# `allowed_objects` warning emitted when langchain.agents pulls in langgraph.checkpoint.
warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.base import BaseMessage
from langchain_core.tools import StructuredTool
from langgraph.graph.state import CompiledStateGraph
from reactivex.disposable import Disposable
import requests

from dimos.agents.base_agent import BaseAgent
from dimos.agents.llm_trace import tracing_http_client
from dimos.agents.mcp import tool_stream
from dimos.agents.mcp.constants import (
    GENERATED_AT_KEY,
    INVOCATION_STARTED_KEY,
    MESSAGE_KIND_KEY,
    MESSAGE_KIND_TOOL_PROGRESS,
    MESSAGE_KIND_TOOL_RESULT,
    MESSAGE_KIND_USER_COMMAND,
    TOOL_ORIGIN_KEY,
    TOOL_RESULT_STALE_AFTER_S,
    TURN_FAILED_KEY,
)
from dimos.agents.mcp.spec import McpServerSpec
from dimos.agents.system_prompt import SYSTEM_PROMPT
from dimos.agents.utils import pretty_print_langchain_message
from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.core.module import ModuleConfig
from dimos.core.rpc_client import RPCClient
from dimos.utils.logging_config import setup_logger
from dimos.utils.sequential_ids import SequentialIds

logger = setup_logger()

_RESPONSES_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")
# How much of a failed turn's error message goes into the agent's history.
_FAILED_TURN_ERROR_CHARS = 300

# Tolerance absorbing within-turn clock jitter between an invocation stamp and
# a turn-open stamp (same thread, microseconds apart in practice).
_INVOCATION_TOLERANCE_S = 0.05

# Background motion tools a TOOL_RESULT-driven turn must not START off the
# artefact (the duplicate-run accident's shape). Extend as more appear; a
# proper per-skill "motion" annotation could drive this from the registry.
_PRODUCT_TURN_MOTION_TOOLS = ("active_vln", "stream_vln")


def _preload_agent_deps() -> None:
    import langchain.agents
    import langchain.chat_models  # noqa: F401
    import langchain_openai  # noqa: F401


def init_model(model_name: str, trace_dir: Path | None = None) -> Any:
    """Initialize a model while preserving LangChain provider resolution.

    With *trace_dir*, every request/response body goes to disk whole
    (:mod:`dimos.agents.llm_trace`). Only OpenAI-backed models take the
    ``http_client``; other providers keep working, untraced at the wire.
    """
    # ~2s: langchain's chat-model machinery pulls transformers+torch; deferred to
    # keep module import (test collection, CLI startup) light.
    from langchain.chat_models import init_chat_model
    from langchain_openai import ChatOpenAI

    client = None if trace_dir is None else tracing_http_client(trace_dir)
    # A remote Ollama server configured globally (--ollama-base-url) applies to
    # every ollama: model; ChatOllama would otherwise default to localhost.
    base_url = None
    if model_name.startswith("ollama:"):
        base_url = os.environ.get("OLLAMA_BASE_URL") or None
    if ":" in model_name or not model_name.startswith(_RESPONSES_REASONING_MODEL_PREFIXES):
        model = init_chat_model(model=model_name, **({"base_url": base_url} if base_url else {}))
        if client is not None and isinstance(model, ChatOpenAI):
            return init_chat_model(
                model=model_name, http_client=client, **({"base_url": base_url} if base_url else {})
            )
        return model

    return ChatOpenAI(
        model=model_name,
        use_responses_api=True,
        reasoning={"effort": "medium", "summary": "auto"},
        http_client=client,
    )


class AgentConfig(ModuleConfig):
    system_prompt: str | None = SYSTEM_PROMPT
    model: str = "gpt-5.6-luna"
    model_fixture: str | None = None
    # None: the McpServer in this blueprint; without one the agent has no tools.
    mcp_server_url: str | None = None
    trace_dir: Path | None = None


class Agent(BaseAgent):
    config: AgentConfig

    # Injected when the blueprint has an McpServer.
    _mcp_server: McpServerSpec | None = None

    _lock: RLock
    _state_graph: CompiledStateGraph[Any, Any, Any, Any] | None
    _message_queue: Queue[BaseMessage]
    _agent_tools: list[StructuredTool] | None
    _tool_registry: dict[str, dict[str, Any]]
    _history: list[BaseMessage]
    _thread: Thread
    _stop_event: Event
    _http_client: requests.Session
    _seq_ids: SequentialIds
    _tool_stream_cleanup: Callable[[], None] | None
    # Monotonic map of in-flight tool invocations (tool_name -> started_at),
    # used to judge TOOL_RESULT freshness at consumption time (see
    # _process_message): an observe image that sat in the queue behind a
    # minutes-long VLN run must not present itself to the LLM as a fresh
    # observation (it caused a duplicate active_vln execution — see
    # artifacts/vln_m1/m1_01b/root_cause.md). The live set is swapped per
    # agent turn: a call belongs to the turn that made it, and an artefact
    # whose call's turn closed is a leftover, age notwithstanding.
    _tool_invocations: dict[str, float]
    _turn_tool_calls: set[str] | None
    # Turn-open timestamps (OBSERVE fix): an artefact whose call started
    # within the PREVIOUS turn's window is that turn's continuation (valid);
    # older stamps are cross-turn leftovers (the duplicate-run root cause).
    _turn_opened_at: float | None
    _prev_turn_opened_at: float | None
    # What kind of message drove the current turn ("user" | "tool_result").
    # The product-turn motion guard reads it (see _mcp_tool_call).
    _turn_origin: str | None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._lock = RLock()
        self._state_graph = None
        self._message_queue = Queue()
        self._agent_tools = None
        self._tool_registry = {}
        self._history = []
        self._thread = Thread(
            target=self._thread_loop,
            name=f"{self.__class__.__name__}-thread",
            daemon=True,
        )
        self._stop_event = Event()
        self._http_client = requests.Session()
        self._seq_ids = SequentialIds()
        self._tool_stream_cleanup = None
        self._tool_invocations = {}
        self._turn_tool_calls = None
        self._turn_opened_at = None
        self._prev_turn_opened_at = None
        self._turn_origin = None
        # Tools may execute on executor threads while the loop thread holds
        # self._lock inside _process_message — the invocation ledger must
        # never contend with it (a pre-GATE deadlock; call_tool used to
        # touch no shared state before the request).
        self._invocation_lock = threading.Lock()
        self._mcp_url: str | None = None
        Thread(target=_preload_agent_deps, name="Agent-preload", daemon=True).start()

    def _server_url(self) -> str | None:
        """The MCP endpoint: configured, else the blueprint's McpServer, else none."""
        if self._mcp_url is None:
            if self.config.mcp_server_url is not None:
                self._mcp_url = self.config.mcp_server_url
            elif self._mcp_server is not None:
                self._mcp_url = str(self._mcp_server.mcp_url())
        return self._mcp_url

    def _mcp_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._seq_ids.next(),
            "method": method,
        }
        if params is not None:
            body["params"] = params

        url = self._server_url()
        if url is None:
            raise RuntimeError("Agent has no MCP server")
        resp = self._http_client.post(url, json=body, timeout=120.0)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            raise RuntimeError(f"MCP error {data['error']['code']}: {data['error']['message']}")

        result: dict[str, Any] = data.get("result")
        return result

    def _interrupt_movement_skills(self) -> None:
        """Stop every movement-holding skill, from the human input thread.

        Runs on the input thread (not the agent loop) on purpose: the loop is
        busy inside a tool call for the length of the running skill, which is
        exactly what the user wants interrupted. Each stop tool is its own MCP
        call; failures are logged and the rest still run — a partial stop is
        better than none. The MCP server's capability layer preempts the
        holder when a stop tool conflicts with it (see mcp_server.py).
        """
        for stop_tool in (
            "stop_stream_vln",
            "stop_active_vln",
            "stop_navigation",
            "stop_patrol",
            "stop_following",
            "stop_looking_out",
            "end_exploration",
        ):
            try:
                self._mcp_tool_call(stop_tool, {})
            except Exception:
                logger.debug("Interrupt stop call failed", tool=stop_tool)

    def _mcp_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Hard layer of the product-turn guard (OBSERVE-FOLLOWUP): a turn
        # driven by a TOOL_RESULT (not a USER_COMMAND) may not START a
        # background motion task off the artefact — the original duplicate-run
        # accident's shape. The refusal is a normal tool result (the model
        # can relay it); the motion never happens. User-driven turns and
        # non-motion tools are unaffected; observe images stay visible.
        if (
            getattr(self, "_turn_origin", None) == "tool_result"
            and name in _PRODUCT_TURN_MOTION_TOOLS
        ):
            logger.info(
                "Refused product-driven motion tool call",
                tool=name,
                reason="the turn is driven by a tool result, not a user command",
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Refused: '{name}' was requested from a turn driven by "
                            "a tool's own result, not by the user. Do not start or "
                            "repeat motion tasks from tool results; wait for an "
                            "explicit user request. If the task result looks "
                            "wrong, describe what you see and let the user decide."
                        ),
                    }
                ]
            }
        progress_token = str(uuid.uuid4())
        return self._mcp_request(
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
                "_meta": {"progressToken": progress_token},
            },
        )

    def _on_tool_stream_message(self, msg: dict[str, Any]) -> None:
        """Route tool-stream frames.

        Progress/telemetry frames (TOOL_PROGRESS) are cockpit display
        traffic, NOT LLM input: each VLN turn used to cost one queued
        HumanMessage and, once the blocking tool returned, one LLM call per
        frame (12 calls for one 11-turn run) - and the queue backlog
        delayed every artefact behind them. They are published on the
        `agent` stream (the cockpit's chat channel) as structured telemetry
        instead, VLN progress carrying run/policy/turn/stage parsed out
        of the message text. Terminal `dimos/tool_stopped` frames are NOT
        handled here (the McpServer subscribes to them for the capability
        release; the Agent never sees them as input).
        """
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == tool_stream.NOTIFICATIONS_PROGRESS_METHOD:
            text = params.get("message") or ""
            tool_name = (params.get("_meta") or {}).get("tool_name") or "tool"
        elif method == tool_stream.NOTIFICATIONS_MESSAGE_METHOD:
            text = params.get("data") or ""
            tool_name = params.get("logger") or "tool"
        else:
            return
        if not text:
            return
        telemetry = _vln_progress_telemetry(tool_name, text)
        self.agent.publish(
            AIMessage(
                content=f"[tool:{tool_name}] {text}",
                additional_kwargs={
                    MESSAGE_KIND_KEY: MESSAGE_KIND_TOOL_PROGRESS,
                    **({"vln": telemetry} if telemetry else {}),
                },
            )
        )

    def _fetch_tools(self, timeout: float = 60.0, interval: float = 1.0) -> list[StructuredTool]:
        result = self._try_fetch_tools(timeout=timeout, interval=interval)
        if result is None:
            raise RuntimeError(f"Failed to fetch tools from MCP server {self._server_url()}")

        raw_tools = result.get("tools", [])
        self._tool_registry = {t["name"]: t for t in raw_tools}
        tools = [self._mcp_tool_to_langchain(t) for t in raw_tools]

        if not tools:
            logger.warning("No tools found from MCP server.")
        else:
            tool_names = [t.name for t in tools]
            logger.info("Discovered tools from MCP server.", tools=tool_names, n_tools=len(tools))

        return tools

    def _try_fetch_tools(self, timeout: float, interval: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout

        while True:
            try:
                self._mcp_request("initialize")
                break
            except requests.ConnectionError:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(interval)

        return self._mcp_request("tools/list")

    def _mcp_tool_to_langchain(self, mcp_tool: dict[str, Any]) -> StructuredTool:
        name = mcp_tool["name"]
        description = mcp_tool.get("description", "")
        input_schema = mcp_tool.get("inputSchema", {"type": "object", "properties": {}})

        def call_tool(**kwargs: Any) -> str:
            # The invocation stamp rides on every artefact this call queues
            # (see _begin_tool_invocation): the freshness gate can then tell
            # "belongs to an open agent turn" from "leftover of a closed one".
            invocation_started = self._begin_tool_invocation(name)
            result = self._mcp_tool_call(name, kwargs)
            content = result.get("content", [])
            parts = [c.get("text", "") for c in content if c.get("type") == "text"]
            text = "\n".join(parts)

            # Images need to be added to the history separately because they
            # cannot be included in the tool response for OpenAI models and
            # probably others.
            for item in content:
                if item.get("type") != "text":
                    uuid_ = str(uuid.uuid4())
                    text += f"Tool call started with UUID: {uuid_}. You will be updated with the result soon."
                    _append_image_to_history(self, name, uuid_, item, invocation_started)

            return text

        return StructuredTool(
            name=name,
            description=description,
            func=call_tool,
            args_schema=input_schema,
        )

    @rpc
    def start(self) -> None:
        super().start()

        def _on_human_input(string: str) -> None:
            # An urgent stop interrupts whatever skill holds the movement
            # capability instead of queueing behind the running agent turn
            # (which can be a minutes-long tool loop the user wants ended).
            if _is_urgent_stop(string):
                logger.info("Urgent stop from human input; interrupting skills", text=string[:50])
                self._interrupt_movement_skills()
                # Still queue the message so the agent knows it was stopped
                # and does not carry on with the interrupted task afterwards.
                self._message_queue.put(
                    HumanMessage(
                        content=f"[system] The user sent an urgent stop ('{string}'). "
                        "Movement skills were interrupted. Ask what to do next; "
                        "do not resume the interrupted task on your own."
                    )
                )
                return
            # USER_COMMAND: genuine human input — always reaches the LLM and
            # may lawfully start (or repeat) any task, unlike TOOL_RESULT.
            self._message_queue.put(
                HumanMessage(
                    content=string,
                    additional_kwargs={MESSAGE_KIND_KEY: MESSAGE_KIND_USER_COMMAND},
                )
            )

        self.register_disposable(Disposable(self.human_input.subscribe(_on_human_input)))

        # Subscribe directly over LCM rather than through the server's GET
        # /mcp SSE channel.  HTTP would add a startup race: the first few
        # updates of a short-lived stream can fire before the SSE connection
        # is established.  External clients like Claude Code still use GET
        # /mcp, which the server fans out to from the same LCM topic.
        self._tool_stream_cleanup = tool_stream.subscribe(self._on_tool_stream_message)

    @rpc
    def on_system_modules(self, _modules: list[RPCClient]) -> None:
        if self._server_url() is None:
            logger.info("No MCP server; the agent runs without tools.")
            self._agent_tools = []
        else:
            self._agent_tools = self._fetch_tools()
        self._rebuild_agent()
        with self._lock:
            if not self._thread.is_alive():
                self._thread.start()

    def _rebuild_agent(self) -> None:
        # ~2s: pulls transformers+torch; deferred to keep module import light.
        from langchain.agents import create_agent

        # Under the lock, or a concurrent set_trace_dir can lose its path to this build.
        with self._lock:
            if self.config.model_fixture is not None:
                from dimos.agents.testing.mock_model import MockModel

                model = MockModel(json_path=self.config.model_fixture)
            else:
                model = init_model(self.config.model, trace_dir=self.config.trace_dir)
            logger.info(
                "Agent model ready.",
                model=self.config.model_fixture or self.config.model,
                backend=type(model).__name__,
                trace_dir=str(self.config.trace_dir) if self.config.trace_dir else None,
            )
            self._state_graph = create_agent(
                model=model,
                tools=self._agent_tools or [],
                system_prompt=self.config.system_prompt,
            )

    @rpc
    def stop(self) -> None:
        # Unsubscribe first so no new tool-stream messages can arrive while
        # the worker thread is draining and joining.
        if self._tool_stream_cleanup is not None:
            self._tool_stream_cleanup()
            self._tool_stream_cleanup = None
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        self._http_client.close()
        super().stop()

    @rpc
    def trace_dir(self) -> Path | None:
        """Where raw LLM request/response bodies go; ``None`` when tracing is off."""
        return self.config.trace_dir

    @rpc
    def set_trace_dir(self, path: str | None) -> None:
        """Point raw LLM capture at *path* and rebuild the model to pick it
        up; ``None`` turns tracing off."""
        with self._lock:
            self.config.trace_dir = Path(path) if path is not None else None
            if self._state_graph is not None:
                self._rebuild_agent()

    @rpc
    def add_message(self, message: BaseMessage) -> None:
        self._message_queue.put(message)

    @rpc
    def dispatch_continuation(
        self, continuation: dict[str, Any], continuation_context: dict[str, Any]
    ) -> None:
        """Execute a tool continuation with detection data, bypassing the LLM.

        Called by trigger tools (e.g. look_out_for) to immediately invoke a
        follow-up tool when a detection fires, without waiting for the LLM to
        reason about the next action.

        Args:
            continuation: ``{"tool": "<name>", "args": {…}}`` — the tool to
                call and its arguments.  Argument values that are strings
                starting with ``$`` are treated as template variables and
                resolved against *continuation_context* (e.g. ``"$bbox"``).
            continuation_context: runtime detection data, e.g.
                ``{"bbox": [x1, y1, x2, y2], "label": "person"}``.
        """
        tool_name = continuation.get("tool")
        if not tool_name:
            self._message_queue.put(
                HumanMessage(f"Continuation failed: missing 'tool' key in {continuation}")
            )
            return

        if tool_name not in self._tool_registry:
            self._message_queue.put(
                HumanMessage(f"Continuation failed: tool '{tool_name}' not found")
            )
            return

        tool_args: dict[str, Any] = dict(continuation.get("args", {}))

        # Substitute $-prefixed template variables from continuation_context
        for key, value in tool_args.items():
            if isinstance(value, str) and value.startswith("$"):
                context_key = value[1:]
                if context_key in continuation_context:
                    tool_args[key] = continuation_context[context_key]

        try:
            result = self._mcp_tool_call(tool_name, tool_args)
            content = result.get("content", [])
            parts = [c.get("text", "") for c in content if c.get("type") == "text"]
            text = "\n".join(parts)
        except Exception as e:
            self._message_queue.put(
                HumanMessage(f"Continuation '{tool_name}' failed with error: {e}")
            )
            return

        label = continuation_context.get("label", "unknown")
        self._message_queue.put(
            HumanMessage(
                f"Automatically executed '{tool_name}' as a continuation of lookout "
                f"detection (detected: {label}). Result: {text or 'started'}"
            )
        )

    def _thread_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                message = self._message_queue.get(timeout=0.5)
            except Empty:
                continue

            with self._lock:
                if not self._state_graph:
                    raise ValueError("No state graph initialized")
                self._process_message(self._state_graph, message)

    def _begin_tool_invocation(self, name: str) -> float:
        """Mark a live tool invocation by this agent turn (returns its clock).

        The invocation-stamp rides on every artefact the call later queues
        (see _append_image_to_history), so _process_message can tell "the
        artefact belongs to a call whose agent turn is still open" (the
        valid case) from one that outlived its turn.
        """
        with self._invocation_lock:
            now = time.time()
            self._tool_invocations[name] = now
            if self._turn_tool_calls is not None:
                self._turn_tool_calls.add(name)
            return now

    def _tool_invocation_is_live(self, name: str, recorded_at: float) -> bool:
        """Whether an artefact's originating call is a valid continuation.

        The semantics (OBSERVE fix; see root_cause in the artefacts):

        VALID — the call started within the LAST OPENED turn's window: a
        tool image queued by that turn is its legitimate continuation into
        the consuming turn (the LangGraph convention this agent has always
        used: artefacts feed the next model call). This covers the exact
        production sequence — call_tool runs inside the stream, queues the
        image, the turn closes, and only then the loop consumes it — and
        background tools whose images arrive later while no newer same-tool
        call happened.

        INVALID — the call's stamp predates the last opened turn's window
        (a cross-turn leftover of an older task: the duplicate-run root
        cause), OR a NEWER invocation of the same tool superseded this
        artefact's call (consecutive same-tool calls must not cross-wire;
        the invocation map's latest stamp wins).

        Note the gate runs BEFORE the consuming turn's _open_agent_turn,
        so `_turn_opened_at` is exactly the producing turn's open time.

        The call key is the tool NAME (per-UUID tracking would need
        McpServer-side plumbing; same-name takeover scoping already matches
        the capability registry's token semantics).
        """
        with self._invocation_lock:
            window_start = self._turn_opened_at
            if window_start is None:
                # No turn has opened yet: nothing valid can predate it.
                return False
            if recorded_at + _INVOCATION_TOLERANCE_S < window_start:
                return False  # older than the producing turn: cross-turn leftover
            started = self._tool_invocations.get(name)
            if started is not None and recorded_at + _INVOCATION_TOLERANCE_S < started:
                return False  # superseded by a newer same-tool call
            return True

    def _open_agent_turn(self) -> None:
        """Open a fresh live-call set for the agent turn about to stream.

        The opening time is kept: an artefact whose originating call started
        within the PREVIOUS turn's open window is that turn's legitimate
        continuation — the LangGraph convention this agent has always used
        (a tool's image is fed to the NEXT model call as a user turn) — and
        must survive the previous turn's close. Artefacts whose call stamp
        predates the previous turn's window are cross-turn leftovers.
        """
        with self._invocation_lock:
            self._turn_tool_calls = set()
            self._prev_turn_opened_at = self._turn_opened_at
            self._turn_opened_at = time.time()

    def _close_agent_turn(self) -> None:
        """Close the turn's live-call set.

        Same-name takeovers stay impossible (a newer call's stamp still
        supersedes an older artefact via the invocation map), but the close
        no longer invalidates the turn's OWN queued products: they are judged
        against the previous-turn window (see _tool_invocation_is_live).
        """
        with self._invocation_lock:
            self._turn_tool_calls = None

    def _process_message(
        self, state_graph: CompiledStateGraph[Any, Any, Any, Any], message: BaseMessage
    ) -> None:
        # TOOL_RESULT gate (P0 + GATE hardening), primary-then-secondary:
        #
        # 1. CALL LIVENESS (primary): the artefact's originating tool call
        #    must belong to a still-open agent turn. A closed or superseded
        #    invocation's product is a leftover — dropped regardless of age,
        #    so a 1 s-old image of a finished task can never pose as fresh
        #    input (the 15 s threshold alone could not guarantee that).
        # 2. AGE (secondary): a live call's artefact that queued behind a
        #    long-running sibling tool (a VLN episode) is stale at
        #    consumption time — dropped for the LLM (reason logged; the
        #    cockpit already saw the image when the tool ran).
        #
        # Dropped products never start reasoning turns, so no re-execution of
        # finished VLN tasks is reachable from a tool result. USER_COMMAND
        # messages are never gated: an explicit user request is always
        # honored, including repeating a finished task.
        if isinstance(message, HumanMessage):
            kwargs = message.additional_kwargs or {}
            if kwargs.get(MESSAGE_KIND_KEY) == MESSAGE_KIND_TOOL_RESULT:
                origin = kwargs.get(TOOL_ORIGIN_KEY, "?")
                generated_at = kwargs.get(GENERATED_AT_KEY)
                invocation_started = kwargs.get(INVOCATION_STARTED_KEY)
                drop_reason: str | None = None
                if not isinstance(invocation_started, (int, float)):
                    # Pre-lifecycle artefact (older M1-01B form): fall back to
                    # the age-only check below.
                    invocation_started = None
                if invocation_started is not None and not self._tool_invocation_is_live(
                    origin, float(invocation_started)
                ):
                    drop_reason = "its originating tool call's agent turn already closed"
                elif isinstance(generated_at, (int, float)):
                    age = time.time() - float(generated_at)
                    if age > TOOL_RESULT_STALE_AFTER_S:
                        drop_reason = (
                            f"it arrived {age:.0f} s late (queued behind a long task turn)"
                        )
                if drop_reason is not None:
                    age_s = (
                        round(time.time() - float(generated_at), 1)
                        if isinstance(generated_at, (int, float))
                        else None
                    )
                    logger.info(
                        "Dropped leftover tool result (never reached the LLM)",
                        tool=origin,
                        age_s=age_s,
                        reason=drop_reason,
                    )
                    self.agent.publish(
                        AIMessage(
                            content=(
                                f"[tool:{origin}] An image result was not shown to "
                                f"the model: {drop_reason}."
                            ),
                            additional_kwargs={MESSAGE_KIND_KEY: MESSAGE_KIND_TOOL_PROGRESS},
                        )
                    )
                    return
        self.agent_idle.publish(False)
        turn_start = len(self._history)
        # What drove this turn: the guard's ground truth (USER_COMMAND turns
        # may start anything; TOOL_RESULT turns may not start motion).
        if isinstance(message, HumanMessage):
            kw = message.additional_kwargs or {}
            self._turn_origin = (
                MESSAGE_KIND_USER_COMMAND
                if kw.get(MESSAGE_KIND_KEY) == MESSAGE_KIND_USER_COMMAND
                else "tool_result"
            )
        else:
            self._turn_origin = None
        self._open_agent_turn()
        self._record(message)

        try:
            for update in state_graph.stream({"messages": self._history}, stream_mode="updates"):
                for node_output in update.values():
                    for msg in node_output.get("messages", []):
                        self._record(msg)
        except Exception as e:
            # Letting this escape would end the thread, and no later message, tool
            # updates included, would be processed. Log it and return; the failed
            # message is not retried.
            logger.exception("Agent turn failed", error=f"{type(e).__name__}: {e}")
            failure = f"{type(e).__name__}: {str(e)[:_FAILED_TURN_ERROR_CHARS]}"
            # Model APIs reject a history with an unanswered tool call, so answer
            # each one with an error.
            for call_id in _unanswered_tool_calls(self._history[turn_start + 1 :]):
                self._record(
                    ToolMessage(
                        content=(
                            f"No result: the agent turn failed with {failure}. "
                            "The call may or may not have run."
                        ),
                        tool_call_id=call_id,
                        status="error",
                    )
                )
            self._record(
                AIMessage(
                    content=f"This turn failed with {failure}",
                    additional_kwargs={TURN_FAILED_KEY: True},
                )
            )
        finally:
            # The turn is over (streamed out or failed): every tool call it
            # made is now closed, so any artefact still referencing one is a
            # leftover the liveness gate drops, age notwithstanding.
            self._turn_origin = None
            self._close_agent_turn()

        if self._message_queue.empty():
            self.agent_idle.publish(True)

    def _record(self, message: BaseMessage) -> None:
        self._history.append(message)
        pretty_print_langchain_message(message)
        self.agent.publish(message)


# A chat message that means "stop now" — the user interrupt. Matched on the
# message alone (no model round trip): the whole point is to interrupt a
# running tool loop without waiting for the current (possibly minutes-long)
# agent turn to finish.
_URGENT_STOP_WORDS = ("停止", "停下", "别动", "停", "stop", "halt")


def _is_urgent_stop(text: str) -> bool:
    """True when the chat message is (essentially) just an urgent stop.

    Long instructions that merely contain a stop word ("stop by the sofa",
    "走到沙发旁边停下") must not trigger it — only short messages that are the
    stop command itself, optionally padded with polite words ("please", 请).
    """
    stripped = text.strip().lower()
    if len(stripped) > 24:
        return False
    for polite in ("please", "请", "麻烦", "帮我", "now", "一下", "！!", "。", "!", "?", "？"):
        stripped = stripped.replace(polite, " ")
    stripped = stripped.strip()
    if not stripped:
        return False
    # Whole-word match for the latin stop words; "stop by the sofa" has words
    # after "stop" and must not trigger.
    if stripped in _URGENT_STOP_WORDS:
        return True
    return any(
        stripped == w or (w.isascii() is False and stripped.startswith(w))
        for w in _URGENT_STOP_WORDS
    )


def _unanswered_tool_calls(messages: list[BaseMessage]) -> Iterator[str]:
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            call_id = call["id"]
            if call_id is not None and call_id not in answered:
                yield call_id


_VLN_PROGRESS_RE = re.compile(r"^turn (?P<turn>\d+)/(?P<max>\d+): (?P<actions>\d+) actions$")


def _vln_progress_telemetry(tool_name: str, text: str) -> dict[str, Any] | None:
    """Structured VLN task telemetry off a tool-update message.

    The VLN skills' tool_update text is "turn 3/50: 4 actions"; the parsed
    fields ride the cockpit progress frame so the UI can show
    policy/turn/stage without any LLM round trip. The two policy skills'
    tool names map to the policy name.
    """
    match = _VLN_PROGRESS_RE.match(text.strip())
    if match is None:
        return None
    return {
        "tool": tool_name,
        "policy": {"active_vln": "activevln", "stream_vln": "streamvln"}.get(tool_name, tool_name),
        "current_turn": int(match["turn"]),
        "max_turns": int(match["max"]),
        "actions": int(match["actions"]),
        "stage": "ACTION_EXECUTION",
    }


def _append_image_to_history(
    agent: Agent,
    func_name: str,
    uuid_: str,
    result: Any,
    invocation_started: float | None = None,
) -> None:
    # Tagged with its tool, or chat UIs would show this user turn as typed input.
    # Generated-at rides along (TOOL_RESULT class, message_kind=tool_result)
    # AND the originating call's invocation stamp: _process_message gates on
    # call liveness first (a closed turn's artefact is a leftover no matter
    # how young) and age second — an image queued behind a minutes-long VLN
    # run must not reach the LLM as if fresh (it caused a duplicate
    # active_vln execution; root_cause.md).
    agent.add_message(
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": f"This is the artefact for the '{func_name}' tool with UUID:={uuid_}.",
                },
                result,
            ],
            additional_kwargs={
                TOOL_ORIGIN_KEY: func_name,
                MESSAGE_KIND_KEY: "tool_result",
                GENERATED_AT_KEY: time.time(),
                INVOCATION_STARTED_KEY: invocation_started,
            },
        )
    )
