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

"""The `stream_vln` skill: instruction-following navigation with the
StreamVLN SlowFast policy.

Wraps a StreamVLN model (Liu et al., ICRA 2026, "StreamVLN: Streaming
Vision-and-Language Navigation via SlowFast Context Modeling",
https://github.com/InternRobotics/StreamVLN) served by its official
real-world HTTP server (`streamvln/http_realworld_server.py`): every step
the skill posts the first-person camera frame and receives a sequence of
discrete actions (0 stop, 1 forward 25cm, 2 left 15 deg, 3 right 15 deg —
the same VLN-CE action space ActiveVLN uses and the same one StreamVLN's
own Go2 deployment drives). The SlowFast context (every-4-steps inference,
memory-token history) lives inside the server; the skill is a plain HTTP
client, converts actions to planner goals exactly like `active_vln`, and
reuses its coalescing and odom-verified execution so both skills behave
identically on the robot.

Use `active_vln` or `stream_vln` per instruction — or set the stack's
`vln_policy` (global config) to the one the agent should reach for.
"""

from __future__ import annotations

import io
import threading
import time
from typing import Any

import numpy as np
import requests

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CAP_MOVEMENT
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In, Out
from dimos_lcm.std_msgs import Bool
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.sensor_msgs.Image import Image, ImageFormat
from dimos.navigation.experimental.active_vln.skill import (
    _FORWARD,
    _LEFT,
    _RIGHT,
    _STOP,
    _coalesce,
    _image_to_data_url,
    _pd_drive,
    _relative_goal,
)
from dimos.navigation.go2.replanning_a_star.spec import NavigationInterfaceSpec
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class StreamVlnConfig(ModuleConfig):
    server_url: str = "http://localhost:5801/eval_vln"
    """The StreamVLN real-world server's /eval_vln endpoint."""
    max_turns: int = 60
    """Steps posted to the server per instruction; beyond it the robot stops."""
    request_timeout: float = 150.0
    """Seconds one server round trip may take (model inference included)."""
    # PD homing execution — the same controller and constants as active_vln's
    # (see active_vln.skill._pd_drive; the official StreamVLN real-world
    # deployment's pid_controller.py).
    step_timeout: float = 30.0
    kp_trans: float = 3.0
    kd_trans: float = 0.5
    kp_yaw: float = 3.0
    kd_yaw: float = 0.5
    max_v: float = 0.5
    max_w: float = 1.2
    done_m: float = 0.05
    done_deg: float = 3.0
    drive_hz: float = 10.0
    stuck_m: float = 0.03
    """Per-cycle ground covered (m) that still counts as making progress."""
    stuck_s: float = 3.0
    """Seconds of commanded speed without progress before a drive gives up;
    the policy sees the blocked view next turn and re-decides."""


def _parse_server_actions(actions: list[int]) -> list[tuple[int, float]]:
    """The server's action ids to (action_id, magnitude) pairs.

    1 -> forward 25cm, 2 -> left 15 deg, 3 -> right 15 deg (StreamVLN's own
    Go2 deployment constants), 0 -> stop. Coalesced like active_vln's
    answers so the execution below is shared.
    """
    STEP = {1: 25.0, 2: 15.0, 3: 15.0}
    pairs = [
        (_STOP, 0.0) if a == 0 else (a, STEP.get(a, 25.0))
        for a in actions[:3]
    ]
    return _coalesce(pairs)


class StreamVlnSkill(Module):
    """`stream_vln`: drive a natural-language route with the StreamVLN policy.

    A sibling of `active_vln` with the same robot-side behavior (planner
    goals, coalesced actions, odom-verified moves); the difference is the
    policy server: StreamVLN keeps its SlowFast context (streaming memory
    tokens, every-4-steps inference) inside the server and answers each
    posted frame with its next action sequence.
    """

    config: StreamVlnConfig

    _navigation: NavigationInterfaceSpec

    color_image: In[Image]
    odom: In[PoseStamped]
    # Velocity channel like active_vln's: MovementManager gives teleop
    # precedence, and the PD homing controller drives the micro-goals here.
    tele_cmd_vel: Out[Twist]

    _latest_image: Image | None = None
    _latest_odom: PoseStamped | None = None
    _running: threading.Event = threading.Event()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    @rpc
    def start(self) -> None:
        super().start()
        from reactivex.disposable import Disposable

        self.register_disposable(Disposable(self.color_image.subscribe(self._on_color_image)))
        self.register_disposable(Disposable(self.odom.subscribe(self._on_odom)))
        self._running.clear()

    @rpc
    def stop(self) -> None:
        self._running.clear()
        self.tele_cmd_vel.publish(Twist.zero())
        super().stop()

    @skill(uses=[CAP_MOVEMENT])
    def stop_stream_vln(self) -> str:
        """Stop the stream_vln run under way, if any.

        The robot stops where it is; the next stream_vln call starts a fresh
        policy episode (reset), which is what the server does with its
        SlowFast memory on a new instruction anyway.
        """

        self._running.clear()
        self.tele_cmd_vel.publish(Twist.zero())
        self._navigation.cancel_goal()
        return "Stopped stream_vln."

    def _on_color_image(self, image: Image) -> None:
        self._latest_image = image

    def _on_odom(self, odom: PoseStamped) -> None:
        self._latest_odom = odom

    @skill(uses=[CAP_MOVEMENT], lifecycle="background")
    def stream_vln(self, instruction: str) -> str:
        """Follow a natural-language navigation instruction with the StreamVLN policy.

        StreamVLN looks at the camera each step, keeps a streaming memory of
        where it has been (SlowFast context), and decides small moves
        (forward 25cm, turns in 15-degree steps) until the instruction is
        fulfilled. Use this like active_vln — for instructions that describe
        a route in natural language; the two policies trade off differently
        (ActiveVLN: multi-turn RL; StreamVLN: streaming context with faster
        stepping), so try both on your routes.

        Args:
            instruction: the route to follow, in natural language.

        Returns:
            str: why the run ended (arrived, budget spent, stopped).
        """
        # Held while one run drives the robot (stream_vln is not reentrant).
        import threading

        if not hasattr(self, "_run_lock"):
            self._run_lock = threading.Lock()
        if self._latest_image is None or self._latest_odom is None:
            return "No camera or odometry data yet; cannot start stream_vln."

        with self._run_lock:
            if self._running.is_set():
                return "A stream_vln run is already in progress."
            self._running.set()

        try:
            return self._run(instruction)
        finally:
            self._running.clear()

    def _run(self, instruction: str) -> str:
        """The step loop: post frames, execute actions, until stop or budget.

        The server's SlowFast memory spans one instruction (reset on the
        first frame); a failed run should not poison the next, so a fresh
        reset is requested on the next call by tracking it per run here.
        """
        reset = True
        for turn in range(1, self.config.max_turns + 1):
            if not self._running.is_set():
                self.tele_cmd_vel.publish(Twist.zero())
                self._navigation.cancel_goal()
                return "Stopped by stop_navigation or stop_movement."
            if self._latest_image is None:
                return "Camera stream stopped mid-run."

            image = self._latest_image
            actions = self._post_frame(image, reset)
            reset = False
            if actions is None:
                return "Policy server unreachable; stopped where the robot is."
            if not actions:
                continue  # empty answer: post the next frame, do not move blindly

            for action_id, magnitude in actions:
                if not self._running.is_set() or action_id == _STOP:
                    self.tele_cmd_vel.publish(Twist.zero())
                    self._navigation.cancel_goal()
                    return "Policy said stop." if action_id == _STOP else "Stopped."
                if not self._execute(action_id, magnitude):
                    logger.info(
                        "stream_vln move did not complete: %s %.0f", action_id, magnitude
                    )
                    break  # next frame shows the blocked view; the policy re-decides

        return f"Step budget ({self.config.max_turns}) spent; stopped where the robot is."

    def _post_frame(self, image: Image, reset: bool) -> list[tuple[int, float]] | None:
        """One server round trip: JPEG frame + reset flag -> parsed actions."""
        import base64

        data_url = _image_to_data_url(image, 80000)
        jpeg = base64.b64decode(data_url.split(",", 1)[1])
        try:
            resp = requests.post(
                self.config.server_url,
                files={"image": ("rgb_image.jpg", jpeg, "image/jpeg")},
                data={"json": f'{{"reset": {str(reset).lower()}}}'},
                timeout=self.config.request_timeout,
            )
            resp.raise_for_status()
            raw = resp.json().get("action", [])
        except Exception as e:
            logger.error("StreamVLN server call failed", exc_info=e)
            return None
        logger.info("stream_vln turn: server actions %s", raw)
        return _parse_server_actions(list(raw))

    # -- shared execution (identical semantics to active_vln) ------------------

    def _execute(self, action_id: int, magnitude: float) -> bool:
        """One coalesced action via the shared PD velocity homing (see
        active_vln's _pd_drive): the VLN's micro-goals (25 cm / 15 deg) sit at
        or below the A* planner's arrival tolerances, so they drive as direct
        velocity commands instead, like the official real-world deployment."""
        pose = self._latest_odom
        if pose is None:
            return False
        if action_id == _FORWARD:
            target = _relative_goal(pose, min(magnitude, 75.0) / 100.0, 0.0)
            return _pd_drive(self, target)
        # Turns drive the yaw loop alone (turn_only): a quadruped turning in
        # place drifts positionally, and correcting that drift makes it orbit
        # the target instead of turning. See active_vln's _pd_drive.
        degrees = magnitude if action_id == _LEFT else -abs(magnitude)
        target = _relative_goal(pose, 0.0, degrees)
        return _pd_drive(self, target, turn_only=True)


stream_vln_skill = StreamVlnSkill.blueprint()
