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

"""The `active_vln` skill: instruction-following navigation by a multi-turn
vision-language policy.

Wraps an ActiveVLN model (Zhang et al., ICRA 2026, "ActiveVLN: Towards Active
Exploration via Multi-Turn RL in Vision-and-Language Navigation",
https://github.com/arvillion/ActiveVLN) served behind any OpenAI-compatible
chat endpoint (vllm serve, sglang, ...). Each turn the policy reads the
instruction and the first-person camera image and answers with discrete
low-level actions ("move forward 25cm", "turn left 15 degrees", "stop"); the
skill converts them to world-frame goals for the local planner and runs them
one by one. The robot follows free-form natural-language routes ("walk past
the kitchen and stop by the sofas") without a map, without tagged locations,
and without the object detectors `navigate_with_text` chains — the policy and
the skill never touch that interface.

The Habitat action space the checkpoints were trained in does not exist here:
the planner owns collision avoidance, and the policy only picks where to try
next.
"""

from __future__ import annotations

import base64
import io
import math
import re
import threading
import time
from typing import Any

import numpy as np
from reactivex.disposable import Disposable

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CAP_MOVEMENT
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In, Out
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.geometry_msgs.Vector3 import Vector3, make_vector3
from dimos.msgs.sensor_msgs.Image import Image, ImageFormat
from dimos.navigation.go2.replanning_a_star.spec import NavigationInterfaceSpec
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

# ActiveVLN's R2R action space (its eval/vlnce/eval_vlnce.py): forward in 25cm
# steps, turns in 15-degree steps, up to 3 elementary actions per answer.
FORWARD_STEP_M = 0.25
TURN_STEP_DEG = 15.0
MAX_ACTIONS_PER_TURN = 3

SYSTEM_PROMPT = (
    "You are a helpful assistant. "
    "Your goal is to follow the given instruction to reach a specified destination. \n"
    "At each step, you receive a first-person image (starting view if first step (step 1), "
    "or post-action view otherwise). "
    "Your task is to select choose one action from: move forward 25cm, move forward 50cm, "
    "move forward 75cm, turn left 15 degrees, turn left 30 degrees, turn left 45 degrees, "
    "turn right 15 degrees, turn right 30 degrees, turn right 45 degrees, or stop. \n"
    "The instruction will be provided with each observation. You can take multiple actions at each turn. \n"
)

PROMPT_TEMPLATE = (
    "Instruction: {instruction}\n"
    "Decide your next action. "
    "You can take up to 3 actions at a time, separated by ', '. "
)

# ActiveVLN's own action encoding: 0 stop, 1 forward, 2 left, 3 right.
_STOP, _FORWARD, _LEFT, _RIGHT = 0, 1, 2, 3

_ACTION_NAMES = {_STOP: "stop", _FORWARD: "move forward", _LEFT: "turn left", _RIGHT: "turn right"}


class ActiveVlnConfig(ModuleConfig):
    base_url: str = "http://localhost:8003/v1"
    """OpenAI-compatible endpoint serving the ActiveVLN checkpoint (vllm serve)."""
    api_key: str = "EMPTY"
    model: str | None = None
    """Model name at the endpoint; None takes the first model it lists."""
    max_turns: int = 50
    """Turn budget per instruction; beyond it the robot stops where it is."""
    max_pixels: int = 80000
    """Per-image pixel budget the checkpoints were trained with (keep at 80k)."""
    temperature: float = 0.2
    top_p: float = 0.8
    max_tokens: int = 512
    step_timeout: float = 30.0
    """Seconds one elementary move's homing run may take before it gives up."""
    # PD homing controller: the official StreamVLN real-world deployment's
    # realworld/pid_controller.py constants. The VLN action space is tiny
    # position deltas (25 cm / 15 deg) — smaller than the A* planner's arrival
    # tolerances (0.2 m / 15 deg), which would accept each goal as reached
    # before the robot has moved — so the skills drive the micro-goals with
    # direct velocity commands instead (tele_cmd_vel), like the deployment.
    kp_trans: float = 3.0
    kd_trans: float = 0.5
    kp_yaw: float = 3.0
    kd_yaw: float = 0.5
    max_v: float = 0.5
    """Linear velocity clamp for the homing controller (m/s)."""
    max_w: float = 1.2
    """Angular velocity clamp for the homing controller (rad/s)."""
    done_m: float = 0.05
    """Position error (m) that completes a homing run."""
    done_deg: float = 3.0
    """Heading error (deg) that completes a homing run."""
    drive_hz: float = 10.0
    """Homing controller rate."""
    stuck_m: float = 0.03
    """Per-cycle ground covered (m) that still counts as making progress."""
    stuck_s: float = 3.0
    """Seconds of commanded speed without progress before a drive gives up
    (an obstacle the direct drive cannot see); the policy re-decides."""


def _image_to_data_url(image: Image, max_pixels: int) -> str:
    """The camera frame as a JPEG data: URL, downscaled to the training budget.

    Qwen2.5-VL works on smart-resized images (multiples of 28); matching that
    keeps the policy's inputs in-distribution.
    """
    from PIL import Image as PILImage

    # dimos Image.data is BGR by default (ImageFormat.BGR); PIL wants RGB.
    rgb = image.data[..., ::-1] if image.format == ImageFormat.BGR else image.data
    pil = PILImage.fromarray(np.ascontiguousarray(rgb)).convert("RGB")
    scale = (max_pixels / (pil.width * pil.height)) ** 0.5
    if scale < 1.0:
        width = max(28, int(pil.width * scale) // 28 * 28)
        height = max(28, round(pil.height * width / pil.width) // 28 * 28)
        pil = pil.resize((width, height))
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _parse_actions(text: str) -> list[tuple[int, float]]:
    """ActiveVLN's answer grammar: comma-separated actions with magnitudes.

    "turn left 15 degrees, move forward 50cm" -> [(_LEFT, 15.0), (_FORWARD, 50.0)].
    Text that names no action yields []: the caller re-asks with the same
    observation instead of inventing a move.
    """
    actions: list[tuple[int, float]] = []
    for part in re.split(r"[,;/]", text.lower()):
        if "stop" in part:
            actions.append((_STOP, 0.0))
        elif "forward" in part:
            m = re.search(r"-?\d+", part)
            actions.append((_FORWARD, float(m.group()) if m else 25.0))
        elif "left" in part:
            m = re.search(r"-?\d+", part)
            actions.append((_LEFT, float(m.group()) if m else TURN_STEP_DEG))
        elif "right" in part:
            m = re.search(r"-?\d+", part)
            actions.append((_RIGHT, float(m.group()) if m else TURN_STEP_DEG))
    return _coalesce(actions[:MAX_ACTIONS_PER_TURN])


def _coalesce(actions: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Merge adjacent same-direction actions into one planner goal.

    The policy answers in elementary steps ("forward 25cm, forward 25cm,
    turn left 15, turn left 15"); each elementary step costs one planner
    startup (goal acceptance, initial rotation, path search), which dominates
    the run time. Habitat executes steps in a frame; dimos does not, so
    consecutive steps in the same direction become one goal of the summed
    magnitude. The policy only ever plans against post-action views, and
    consecutive same-direction steps land it at the same pose either way —
    only the planner churn in between disappears.

    Turns keep their sign (left positive, right negative) so a left followed
    by a right is a direction change and stays two goals. Forward caps at the
    training grammar's 75cm; a turn sequence over 45 degrees splits at that
    cap (the largest turn the checkpoints were trained to answer with).
    """
    merged: list[tuple[int, float]] = []
    for action_id, magnitude in actions:
        if merged and merged[-1][0] == action_id and action_id != _STOP:
            prev_id, prev_mag = merged[-1]
            if action_id == _FORWARD:
                merged[-1] = (prev_id, min(prev_mag + magnitude, 0.75 * 100.0))
            else:  # turns: left positive, right negative magnitude
                sign = 1.0 if action_id == _LEFT else -1.0
                merged[-1] = (prev_id, sign * (abs(prev_mag) + magnitude))
        else:
            merged.append((action_id, magnitude))
    # Split any coalesced turn that grew past the grammar's 45-degree max.
    capped: list[tuple[int, float]] = []
    for action_id, magnitude in merged:
        if action_id in (_LEFT, _RIGHT) and abs(magnitude) > 45.0:
            remaining = magnitude
            while abs(remaining) > 45.0:
                capped.append((action_id, 45.0 if remaining > 0 else -45.0))
                remaining -= 45.0 if remaining > 0 else -45.0
            if remaining != 0.0:
                capped.append((action_id, remaining))
        else:
            capped.append((action_id, magnitude))
    return capped


def _relative_goal(pose: PoseStamped, forward_m: float, degrees: float) -> PoseStamped:
    """The world-frame goal one policy action asks for, from the current pose.

    forward: +x is straight ahead in the robot's frame. degrees: +left, so the
    turning actions flip the sign; a turn-in-place keeps the position.
    """
    yaw = pose.orientation.to_euler().yaw
    if forward_m == 0.0:
        position = pose.position
    else:
        position = pose.position + Vector3(
            forward_m * math.cos(yaw), forward_m * math.sin(yaw), 0.0
        )
    orientation = Quaternion.from_euler(make_vector3(0.0, 0.0, yaw + math.radians(degrees)))
    return PoseStamped(position=position, orientation=orientation, frame_id=pose.frame_id)


class ActiveVlnSkill(Module):
    """`active_vln`: drive a natural-language route with the ActiveVLN policy.

    A separate path from `navigate_with_text` (which chains tagged locations,
    object detection, and the semantic map): this skill hands the camera
    stream to a multi-turn RL policy and executes its low-level actions, for
    instructions like "walk past the kitchen and stop by the sofas" on a map
    the robot has never seen.
    """

    config: ActiveVlnConfig

    _navigation: NavigationInterfaceSpec

    color_image: In[Image]
    odom: In[PoseStamped]
    # Velocity channel: MovementManager gives teleop precedence over nav
    # planner goals, so the micro-step homing controller drives through here.
    # (The planner path is gone: its 0.2 m / 15-deg arrival tolerances swallow
    # the VLN's whole action size — every goal completed before the robot
    # moved. See _pd_drive.)
    tele_cmd_vel: Out[Twist]

    _latest_image: Image | None = None
    _latest_odom: PoseStamped | None = None
    _running: threading.Event = threading.Event()

    @rpc
    def start(self) -> None:
        super().start()
        self.register_disposable(Disposable(self.color_image.subscribe(self._on_color_image)))
        self.register_disposable(Disposable(self.odom.subscribe(self._on_odom)))
        self._running.clear()

    @rpc
    def stop(self) -> None:
        self._running.clear()
        self.tele_cmd_vel.publish(Twist.zero())
        super().stop()

    @skill(uses=[CAP_MOVEMENT])
    def stop_active_vln(self) -> str:
        """Stop the active_vln run under way, if any.

        The robot stops where it is; the next active_vln call starts fresh
        (its conversation history is per-instruction anyway).
        """

        self._running.clear()
        self.tele_cmd_vel.publish(Twist.zero())
        self._navigation.cancel_goal()
        return "Stopped active_vln."

    def _on_color_image(self, image: Image) -> None:
        self._latest_image = image

    def _on_odom(self, odom: PoseStamped) -> None:
        self._latest_odom = odom

    # -- policy client ------------------------------------------------------

    def _client(self) -> Any:
        """The OpenAI client, imported lazily so module import stays light."""
        from openai import OpenAI

        return OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)

    def _chat(self, client: Any, messages: list[dict[str, Any]]) -> str:
        model = self.config.model
        if model is None:
            model = client.models.list().data[0].id
        out = client.chat.completions.create(
            messages=messages,
            model=model,
            max_completion_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            top_p=self.config.top_p,
        )
        return (out.choices[0].message.content or "").strip()

    # -- the skill -----------------------------------------------------------

    @skill(uses=[CAP_MOVEMENT], lifecycle="background")
    def active_vln(self, instruction: str) -> str:
        """Follow a natural-language navigation instruction with the ActiveVLN policy.

        The policy looks at the camera each turn and decides small moves
        (forward up to 75cm, turns in 15-degree steps) until it believes the
        instruction is fulfilled, then stops. Use this for instructions that
        describe a route ("exit the room, turn left at the hallway, stop by
        the elevator"), especially where the place is not tagged and no
        object name identifies it. For going to known places or objects,
        navigate_with_text is the better tool.

        Args:
            instruction: the route to follow, in natural language.

        Returns:
            str: why the run ended (arrived, budget spent, stopped).
        """

        if not self._latest_image or not self._latest_odom:
            return "No camera or odometry data yet; cannot start active_vln."

        self._running.set()
        client = self._client()
        conversations: list[dict[str, Any]] = [
            {"role": "system", "content": [{"type": "text", "text": SYSTEM_PROMPT}]}
        ]

        for turn in range(1, self.config.max_turns + 1):
            if not self._running.is_set():
                self._navigation.cancel_goal()
                return "Stopped by stop_navigation or stop_movement."

            if self._latest_image is None:
                return "Camera stream stopped mid-run."

            first = len(conversations) == 1
            content = [
                {
                    "type": "text",
                    "text": "[Initial Observation]:" if first else "After that, the observation is:",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": _image_to_data_url(self._latest_image, self.config.max_pixels)},
                },
                {"type": "text", "text": PROMPT_TEMPLATE.format(instruction=instruction)},
            ]
            conversations.append({"role": "user", "content": content})

            try:
                answer = self._chat(client, conversations)
            except Exception as e:
                self._running.clear()
                logger.error("ActiveVLN inference failed", exc_info=e)
                return f"Policy endpoint error: {e}"
            conversations.append({"role": "assistant", "content": [{"type": "text", "text": answer}]})
            logger.info("ActiveVLN turn %d: %s", turn, answer)

            actions = _parse_actions(answer)
            if not actions:
                # Unparseable answer: ask again with a fresh observation
                # rather than moving blindly (the policy never saw this
                # question answered with silence during training).
                continue

            for action_id, magnitude in actions:
                if not self._running.is_set() or action_id == _STOP:
                    self._navigation.cancel_goal()
                    reason = "Policy said stop." if action_id == _STOP else "Stopped."
                    self._running.clear()
                    return reason
                if not self._run_action(action_id, magnitude):
                    # The planner failed on this move; the next observation
                    # shows the unchanged view, and the policy re-decides.
                    logger.info(
                        "ActiveVLN move did not complete: %s %.0f",
                        _ACTION_NAMES[action_id],
                        magnitude,
                    )
                    break

        self._running.clear()
        return f"Turn budget ({self.config.max_turns}) spent; stopped where the robot is."

    def _run_action(self, action_id: int, magnitude: float) -> bool:
        """One coalesced action as a PD velocity homing run; True when done.

        The VLN's actions are tiny position deltas (25 cm / 15 deg) — at or
        below the A* planner's arrival tolerances (0.2 m / 15 deg), which
        accept such goals as reached before the robot has moved. The official
        StreamVLN real-world deployment instead drives the incremental goals
        with a PD velocity controller (its realworld/pid_controller.py);
        this is that controller, on tele_cmd_vel.
        """
        pose = self._latest_odom
        if pose is None:
            return False
        if action_id == _FORWARD:
            target = _relative_goal(pose, min(magnitude, 75.0) / 100.0, 0.0)
            return _pd_drive(self, target)
        # Turns carry their sign through _coalesce (left +, right -).
        degrees = magnitude if action_id == _LEFT else -abs(magnitude)
        target = _relative_goal(pose, 0.0, degrees)
        return _pd_drive(self, target, turn_only=True)


def _pd_drive(skill: ActiveVlnSkill | Any, target: PoseStamped, turn_only: bool = False) -> bool:
    """PD velocity homing to *target* — the official deployment's controller.

    10 Hz loop: forward-axis and yaw errors from the odom to the target, PD
    step (the deployment's gains), clamped velocities on tele_cmd_vel.
    Completes — True — only when the driven errors are inside the done
    thresholds, so the asked displacement happened by construction; no
    planner arrival state to trust. False on stop (urgent stop clears the
    running flag), odom loss, or timeout, always leaving a zero twist.

    *turn_only* (turn-in-place goals) drives the yaw loop alone: a quadruped
    turning in place drifts positionally, and correcting that drift makes the
    robot orbit the target point instead of turning — the yaw then converges
    far slower than the step timeout. The position drift is left for the
    policy's next action to correct (VLN is reactive; the official
    deployment's incremental goals have the same semantics).

    A driving run that stops making progress (velocity commanded but the
    robot barely moves — an obstacle the direct drive does not see) gives up
    early instead of pushing until the timeout: the policy's next observation
    shows the blocked view and it re-decides, which is the VLN loop's own
    obstacle handling.
    """
    cfg = skill.config
    done_rad = math.radians(cfg.done_deg)
    dt = 1.0 / cfg.drive_hz
    deadline = time.monotonic() + cfg.step_timeout
    prev_v = 0.0
    prev_w = 0.0
    stuck_since: float | None = None
    reference = skill._latest_odom
    target_yaw = target.orientation.to_euler().yaw
    while time.monotonic() < deadline and skill._running.is_set():
        pose = skill._latest_odom
        if pose is None:
            break
        yaw = pose.orientation.to_euler().yaw
        e_r = (target_yaw - yaw + math.pi) % (2 * math.pi) - math.pi
        if turn_only:
            if abs(e_r) <= done_rad:
                skill.tele_cmd_vel.publish(Twist.zero())
                return True
            v = 0.0
        else:
            dx = target.position.x - pose.position.x
            dy = target.position.y - pose.position.y
            e_p = dx * math.cos(yaw) + dy * math.sin(yaw)  # forward-axis error
            if abs(e_p) <= cfg.done_m and abs(e_r) <= done_rad:
                skill.tele_cmd_vel.publish(Twist.zero())
                return True
            v = cfg.kp_trans * max(-1.0, min(1.0, e_p)) - cfg.kd_trans * prev_v
            v = max(-cfg.max_v, min(cfg.max_v, v))
            # Blocked detection: commanding real speed but covering no ground.
            if reference is not None and abs(v) > 0.1:
                moved = math.hypot(
                    pose.position.x - reference.position.x,
                    pose.position.y - reference.position.y,
                )
                if moved < cfg.stuck_m:
                    stuck_since = time.monotonic() if stuck_since is None else stuck_since
                    if time.monotonic() - stuck_since > cfg.stuck_s:
                        logger.info(
                            "ActiveVLN drive blocked (no progress); letting the policy re-decide"
                        )
                        break
                else:
                    stuck_since = None
                reference = pose
        w = cfg.kp_yaw * max(-1.0, min(1.0, e_r)) - cfg.kd_yaw * prev_w
        w = max(-cfg.max_w, min(cfg.max_w, w))
        skill.tele_cmd_vel.publish(
            Twist(linear=Vector3(v, 0.0, 0.0), angular=Vector3(0.0, 0.0, w))
        )
        prev_v, prev_w = v, w
        time.sleep(dt)
    skill.tele_cmd_vel.publish(Twist.zero())
    return False


active_vln_skill = ActiveVlnSkill.blueprint()
