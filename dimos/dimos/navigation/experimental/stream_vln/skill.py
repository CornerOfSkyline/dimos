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
real-world HTTP server (`streamvln/http_realworld_server.py` — this
workspace runs the patched deployment copy, streamvln-deploy/, which
passes the instruction through; see its README). Every step the skill
posts the first-person camera frame — BGR-ordered JPEG, the official
client's byte format (the server flips the channels back to RGB) — with
the instruction on the post that resets the policy's memory, and receives
a sequence of discrete actions (0 stop, 1 forward 25cm, 2 left 15 deg,
3 right 15 deg — the same VLN-CE action space ActiveVLN uses and the same
one StreamVLN's own Go2 deployment drives), all of which it executes: the
server advances 4 internal steps per answer (its every-4-steps SlowFast
inference), and its official client chains every returned action into its
goal, so consuming a subset would desynchronize the model's step count
from the robot. Ids outside that space are rejected, never guessed. The
SlowFast context (memory-token history) lives inside the server; the
skill is a plain HTTP client, converts actions to planner goals exactly
like `active_vln`, and reuses its coalescing and odom-verified execution
so both skills behave identically on the robot.

Use `active_vln` or `stream_vln` per instruction — or set the stack's
`vln_policy` (global config) to the one the agent should reach for.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import requests

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CAP_MOVEMENT
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In, Out
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.sensor_msgs.Image import Image
from dimos.navigation.experimental.active_vln.skill import (
    _FORWARD,
    _LEFT,
    _STOP,
    _coalesce,
    _image_to_bgr_jpeg,
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


def _parse_server_actions(actions: Any) -> list[tuple[int, float]]:
    """The server's action ids to (action_id, magnitude) pairs — all of them.

    1 -> forward 25cm, 2 -> left 15 deg, 3 -> right 15 deg, 0 -> stop
    (StreamVLN's own Go2 deployment constants). Every returned action is
    consumed: the official client (realworld/go2_vln_client.py,
    incremental_change_goal) chains the whole sequence into its goal, and the
    server advances 4 internal steps per answer (its every-4-steps SlowFast
    inference) — dropping one would desynchronize the model's step count from
    the robot's. Anything outside the id space (or a non-list answer) raises
    ValueError: the caller rejects it and moves nothing, rather than mapping
    an unknown id to a default move.
    """
    step = {1: 25.0, 2: 15.0, 3: 15.0}
    if not isinstance(actions, list):
        raise ValueError(f"action answer is not a list: {actions!r}")
    for a in actions:
        if not isinstance(a, int) or isinstance(a, bool) or a not in (0, 1, 2, 3):
            raise ValueError(f"action id outside the VLN-CE space: {a!r}")
    pairs = [(_STOP, 0.0) if a == 0 else (a, step[a]) for a in actions]
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
    # Per-instance run state (assigned in __init__). A class-level default
    # like `_running = threading.Event()` would share one Event object across
    # every instance of the class — stopping one skill's run would stop them
    # all, and two stacks in one process could never run at once.
    _running: threading.Event

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._running = threading.Event()
        # Held while one run drives the robot (stream_vln is not reentrant).
        self._run_lock = threading.Lock()

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
        Every exit path leaves the robot at rest (zero twist published).
        """
        reset = True
        for _turn in range(1, self.config.max_turns + 1):
            if not self._running.is_set():
                self.tele_cmd_vel.publish(Twist.zero())
                self._navigation.cancel_goal()
                return "Stopped by stop_navigation or stop_movement."
            if self._latest_image is None:
                self.tele_cmd_vel.publish(Twist.zero())
                return "Camera stream stopped mid-run."

            image = self._latest_image
            try:
                actions = self._post_frame(image, reset, instruction)
            except ValueError as e:
                self.tele_cmd_vel.publish(Twist.zero())
                self._navigation.cancel_goal()
                return f"Rejected the policy's action answer ({e}); stopped."
            reset = False
            if actions is None:
                self.tele_cmd_vel.publish(Twist.zero())
                return "Policy server unreachable; stopped where the robot is."
            if not actions:
                continue  # empty answer: post the next frame, do not move blindly

            for action_id, magnitude in actions:
                if not self._running.is_set() or action_id == _STOP:
                    self.tele_cmd_vel.publish(Twist.zero())
                    self._navigation.cancel_goal()
                    return "Policy said stop." if action_id == _STOP else "Stopped."
                if not self._execute(action_id, magnitude):
                    logger.info("stream_vln move did not complete: %s %.0f", action_id, magnitude)
                    break  # next frame shows the blocked view; the policy re-decides

        self.tele_cmd_vel.publish(Twist.zero())
        return f"Step budget ({self.config.max_turns}) spent; stopped where the robot is."

    def _post_frame(
        self, image: Image, reset: bool, instruction: str
    ) -> list[tuple[int, float]] | None:
        """One server round trip: JPEG frame + reset + instruction -> actions.

        The frame is BGR-ordered JPEG — the official client's byte format
        (see active_vln._image_to_bgr_jpeg; the server flips the channels
        back to RGB on arrival). The instruction travels with the request:
        the server injects it into the policy's prompt when it builds the
        conversation (the first inference after a reset) and keeps it in the
        KV cache afterwards, so later posts carry it harmlessly.

        Returns None on a transport failure; raises ValueError when the
        answer's action ids are outside the VLN-CE space (rejected, never
        executed).
        """
        payload = json.dumps({"reset": reset, "instruction": instruction})
        try:
            resp = requests.post(
                self.config.server_url,
                files={"image": ("rgb_image.jpg", _image_to_bgr_jpeg(image), "image/jpeg")},
                data={"json": payload},
                timeout=self.config.request_timeout,
            )
            resp.raise_for_status()
            raw = resp.json().get("action", [])
        except Exception as e:
            logger.error("StreamVLN server call failed", exc_info=e)
            return None
        logger.info("stream_vln turn: server actions %s", raw)
        return _parse_server_actions(raw)

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
            target = _relative_goal(pose, magnitude / 100.0, 0.0)
            return _pd_drive(self, target)
        # Turns drive the yaw loop alone (turn_only): a quadruped turning in
        # place drifts positionally, and correcting that drift makes it orbit
        # the target instead of turning. See active_vln's _pd_drive.
        degrees = magnitude if action_id == _LEFT else -abs(magnitude)
        target = _relative_goal(pose, 0.0, degrees)
        return _pd_drive(self, target, turn_only=True)


stream_vln_skill = StreamVlnSkill.blueprint()
