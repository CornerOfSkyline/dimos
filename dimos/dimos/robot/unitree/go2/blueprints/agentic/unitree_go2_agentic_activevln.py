#!/usr/bin/env python3
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

"""The agentic Go2 with a VLN policy skill on top: `--vln-policy` picks
which one is wired up (default activevln):

  activevln  the ActiveVLN multi-turn RL policy, served at an
             OpenAI-compatible endpoint (vllm serve of the
             Qwen2.5-VL-3B_rl_r2r_4000 checkpoint), config
             `--activevlnskill.base-url`.

  streamvln  the StreamVLN SlowFast policy, served by its official
             real-world HTTP server (streamvln/http_realworld_server.py),
             config `--streamvlnskill.server-url`.

Both skills share the robot-side execution (discrete VLN-CE actions ->
coalesced planner goals with odom-verified displacement); the choice is
which policy server drives. Start the policy server first — see
run_go2_activevln.sh / run_go2_streamvln.sh.

    dimos --simulation run unitree-go2-agentic-vln --agent.model ollama:qwen3-vl:32b
    dimos --simulation run unitree-go2-agentic-vln --vln-policy streamvln
"""

from dimos.agents.agent import Agent
from dimos.agents.mcp.mcp_server import McpServer
from dimos.core.coordination.blueprints import autoconnect
from dimos.core.global_config import global_config
from dimos.navigation.experimental.active_vln import active_vln_skill
from dimos.navigation.experimental.stream_vln import stream_vln_skill
from dimos.robot.unitree.go2.blueprints.agentic._common_agentic import _common_agentic
from dimos.robot.unitree.go2.blueprints.smart.unitree_go2_spatial import unitree_go2_spatial

_vln_skill = stream_vln_skill if global_config.vln_policy == "streamvln" else active_vln_skill

unitree_go2_agentic_vln = autoconnect(
    unitree_go2_spatial,
    McpServer.blueprint(),
    Agent.blueprint(),
    _common_agentic,
    _vln_skill,
)

# The original name keeps working (it is this same composition).
unitree_go2_agentic_activevln = unitree_go2_agentic_vln
