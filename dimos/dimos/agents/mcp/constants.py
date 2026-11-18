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

from dimos.utils.generic import connect_host

DEFAULT_MCP_PORT = 9990
DEFAULT_MCP_TIMEOUT = 30
DEFAULT_MCP_URL = f"http://localhost:{DEFAULT_MCP_PORT}/mcp"

# The MCP server's worker RPC timeout for background skills (their worker
# call runs as long as the task — VLN episodes run for minutes; the default
# 120 s RPC timeout would fire mid-run and release the capability token
# while the worker still drives the robot).
BACKGROUND_SKILL_RPC_TIMEOUT = 24 * 3600.0

# `HumanMessage.additional_kwargs` key naming the tool a message on Agent's
# `agent` stream came from. A tool's image result is fed to the model as a user
# turn (a tool response cannot carry images for OpenAI models), and chat UIs read
# this key to render that turn as tool output rather than as typed input.
TOOL_ORIGIN_KEY = "tool_origin"

# `AIMessage.additional_kwargs` key marking Agent's note that a turn failed:
# the note has no LLM trace, so the eval adapter must not treat it as a model call.
TURN_FAILED_KEY = "turn_failed"

# `HumanMessage.additional_kwargs` keys classifying externally-injected
# messages (see Agent._process_message / _on_tool_stream_message):
#   USER_COMMAND — genuine human input; always reaches the LLM.
#   TOOL_RESULT  — a tool's asynchronous product (e.g. an observe image),
#                  carried with GENERATED_AT_KEY; consumed only while fresh,
#                  never mistaken for a user command.
#   TOOL_PROGRESS — a running tool's telemetry; shown to the cockpit only,
#                  never queued for LLM inference (one progress message per
#                  VLN turn used to mean one LLM call each, after the fact).
MESSAGE_KIND_KEY = "message_kind"
GENERATED_AT_KEY = "generated_at"
MESSAGE_KIND_USER_COMMAND = "user_command"
MESSAGE_KIND_TOOL_RESULT = "tool_result"
MESSAGE_KIND_TOOL_PROGRESS = "tool_progress"

# A TOOL_RESULT older than this at consumption time is stale: the model must
# not treat it as a fresh observation (a 13 s-old observe image, consumed
# after a 117 s VLN run plus queued progress, triggered a duplicate task).
TOOL_RESULT_STALE_AFTER_S = 15.0


def mcp_url(host: str, port: int) -> str:
    """Convert a local server bind address into a dialable MCP URL."""
    return f"http://{connect_host(host)}:{port}/mcp"
