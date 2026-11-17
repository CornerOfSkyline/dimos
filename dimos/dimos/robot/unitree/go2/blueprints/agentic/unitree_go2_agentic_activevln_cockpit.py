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

"""The ActiveVLN agentic Go2 with the full cockpit layout: video over the 3D
map left, costmap+pose over keyboard teleop in the middle, the agent chat
right (where `active_vln` shows up as a tool alongside the rest), plus a
full-page camera view. Same panels as unitree-go2-agentic-cockpit; start the
ActiveVLN policy server first (see run_go2_activevln.sh):

    dimos --simulation run unitree-go2-agentic-activevln-cockpit \\
        --agent.model ollama:qwen3-vl:32b \\
        --activevlnskill.base-url http://localhost:8003/v1
"""

from dimos.agents.voice_input import VoiceInput
from dimos.agents.web_human_input import WebInput
from dimos.core.coordination.blueprints import autoconnect
from dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic_activevln import (
    unitree_go2_agentic_activevln,
)
from dimos.web.cockpit import Chat, Col, Map2D, Map3D, Row, Stats, Teleop, Video, cockpit

unitree_go2_agentic_activevln_cockpit = (
    autoconnect(
        unitree_go2_agentic_activevln,
        cockpit(
            layout=Row(
                Col(
                    Video("color_image", title="Front camera"),
                    Map3D(path="path", click="clicked_point", title="Map 3D"),
                ),
                Col(
                    Map2D(path="path", click="clicked_point", stop="stop_movement", title="Map"),
                    Teleop(title="Keyboard teleop"),
                    shares=[3, 1],
                ),
                Chat(title="Agent chat"),
                shares=[2, 1, 1],
            ),
            pages=[Video("color_image", title="Front camera"), Stats()],
        ),
        VoiceInput.blueprint(),
    )
    .disabled_modules(WebInput)
    .global_config(n_workers=9, robot_model="unitree_go2")
)
