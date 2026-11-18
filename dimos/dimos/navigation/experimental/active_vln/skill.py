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
import json
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
from dimos.navigation.experimental.vln_task import (
    ACTION_EXECUTION,
    CANCELLED,
    COMPLETED,
    EXECUTION_ERROR,
    FAILED,
    INFERENCE,
    POLICY_ERROR,
    POLICY_STOP,
    SENSOR_UNAVAILABLE,
    STOPPING_STAGE,
    TURN_BUDGET_EXHAUSTED,
    USER_CANCELLED,
    WAITING_OBSERVATION,
    VlnRunHandle,
    VlnTaskRuntime,
)
from dimos.navigation.go2.replanning_a_star.spec import NavigationInterfaceSpec
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

# ActiveVLN's R2R action space (its eval/vlnce/eval_vlnce.py): forward in 25cm
# steps, turns in 15-degree steps. The prompt grammar allows up to 3
# sub-actions per answer; the official executor caps each sub-action at 3
# elementary steps (75 cm / 45 deg), which _parse_actions clamps to.
TURN_STEP_DEG = 15.0

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


def _image_to_bgr_jpeg(image: Image, quality: int = 90) -> bytes:
    """The camera frame as JPEG bytes with BGR channel order.

    The official StreamVLN client (realworld/go2_vln_client.py) wraps the raw
    cv_bridge 'bgr8' array with PIL and JPEG-encodes it as-is; the server
    flips the channels back to RGB on arrival (streamvln/http_realworld_server.py:
    np.asarray(image)[..., ::-1]). Encoding RGB here instead would reach the
    model color-flipped, so the channel order is part of the wire protocol.
    No downscale, like the official client: the server's image_processor
    handles resizing.
    """
    from PIL import Image as PILImage

    bgr = image.data if image.format == ImageFormat.BGR else image.data[..., ::-1]
    pil = PILImage.fromarray(np.ascontiguousarray(bgr))
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _parse_actions(text: str) -> list[tuple[int, float]]:
    """ActiveVLN's answer grammar: comma-separated actions with magnitudes.

    "turn left 15 degrees, move forward 50cm" -> [(_LEFT, 15.0), (_FORWARD, 50.0)].
    Text that names no action yields []: the caller re-asks with the same
    observation instead of inventing a move. Every sub-action the answer names
    is kept and clamped to the grammar's per-answer caps (75 cm forward,
    45 degrees turn): the official executor (eval/vlnce/eval_vlnce.py,
    extract_multi_result + act) runs all sub-actions and caps each at
    min(3, magnitude/step) elementary steps — it never slices the list.
    """
    actions: list[tuple[int, float]] = []
    for part in re.split(r"[,;/]", text.lower()):
        if "stop" in part:
            actions.append((_STOP, 0.0))
        elif "forward" in part:
            m = re.search(r"-?\d+", part)
            magnitude = float(m.group()) if m else 25.0
            actions.append((_FORWARD, min(magnitude, 0.75 * 100.0)))
        elif "left" in part:
            m = re.search(r"-?\d+", part)
            magnitude = float(m.group()) if m else TURN_STEP_DEG
            actions.append((_LEFT, min(magnitude, 3.0 * TURN_STEP_DEG)))
        elif "right" in part:
            m = re.search(r"-?\d+", part)
            magnitude = float(m.group()) if m else TURN_STEP_DEG
            actions.append((_RIGHT, min(magnitude, 3.0 * TURN_STEP_DEG)))
    return _coalesce(actions)


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
    by a right is a direction change and stays two goals. Merging is
    lossless: a run longer than one goal's cap (75 cm forward, 45 degrees
    turn — the grammar's per-answer maxima) splits at the cap into
    consecutive goals, so no part of an action sequence is ever dropped.
    (StreamVLN's 4-step batches reach 100 cm / 60 degrees; its official
    client executes every returned action.)
    """
    merged: list[tuple[int, float]] = []
    for action_id, magnitude in actions:
        if merged and merged[-1][0] == action_id and action_id != _STOP:
            prev_id, prev_mag = merged[-1]
            if action_id == _FORWARD:
                merged[-1] = (prev_id, prev_mag + magnitude)
            else:  # turns: left positive, right negative magnitude
                sign = 1.0 if action_id == _LEFT else -1.0
                merged[-1] = (prev_id, sign * (abs(prev_mag) + magnitude))
        else:
            merged.append((action_id, magnitude))
    # Split runs past one goal's cap into consecutive capped goals (lossless).
    capped: list[tuple[int, float]] = []
    for action_id, magnitude in merged:
        if action_id == _FORWARD:
            cap = 0.75 * 100.0
        elif action_id in (_LEFT, _RIGHT):
            cap = 3.0 * TURN_STEP_DEG
        else:
            capped.append((action_id, magnitude))
            continue
        remaining = magnitude
        while abs(remaining) > cap:
            capped.append((action_id, cap if remaining > 0 else -cap))
            remaining -= cap if remaining > 0 else -cap
        if remaining != 0.0:
            capped.append((action_id, remaining))
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
    # Per-instance run state (assigned in __init__). A class-level default
    # like `_running = threading.Event()` would share one Event object across
    # every instance of the class — stopping one skill's run would stop them
    # all, and two stacks in one process could never run at once.
    _running: threading.Event
    _runtime: VlnTaskRuntime

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._running = threading.Event()
        self._runtime = VlnTaskRuntime("active_vln")

    @rpc
    def start(self) -> None:
        super().start()
        self.register_disposable(Disposable(self.color_image.subscribe(self._on_color_image)))
        self.register_disposable(Disposable(self.odom.subscribe(self._on_odom)))
        self._running.clear()

    @rpc
    def stop(self) -> None:
        self._runtime.request_cancel()  # the run's own handle event
        self._running.clear()
        self.tele_cmd_vel.publish(Twist.zero())
        # Close the tool-stream on graceful module/worker shutdown so the
        # capability registry learns the hold is gone (the tool_stopped
        # frame); a hard crash still leaks until takeover/timeout — see
        # vln_task.py and artifacts/vln_m1/m1_01/design.md.
        self.stop_tool("active_vln")
        super().stop()

    @skill(uses=[CAP_MOVEMENT])
    def stop_active_vln(self) -> str:
        """Stop the active_vln run under way, if any.

        The robot stops where it is; the next active_vln call starts fresh
        (its conversation history is per-instruction anyway).
        """

        self._runtime.request_cancel()  # the run's own handle event
        self._running.clear()
        self.tele_cmd_vel.publish(Twist.zero())
        self._navigation.cancel_goal()
        return "Stopped active_vln."

    def _on_color_image(self, image: Image) -> None:
        self._latest_image = image

    def _on_odom(self, odom: PoseStamped) -> None:
        self._latest_odom = odom

    @skill
    def get_vln_task_status(self, run_id: str | None = None) -> str:
        """Get the status of a VLN task run (active_vln policy).

        Returns the run's lifecycle state (CREATED/RUNNING/STOPPING/
        COMPLETED/FAILED/CANCELLED), the current turn and execution stage,
        timestamps, and — for finished runs — the termination reason
        (POLICY_STOP, TURN_BUDGET_EXHAUSTED, USER_CANCELLED, POLICY_ERROR,
        SENSOR_UNAVAILABLE, EXECUTION_ERROR). COMPLETED means the run ended
        normally, NOT that the goal was reached. Without run_id: the most
        recent run. Runs are queryable while executing and after they end;
        a live state that stopped updating is flagged with stale_s. This
        read does not use any capability and never touches motion control.

        Args:
            run_id: the run to look up; None for the most recent one.

        Returns:
            str: the task snapshot as JSON.
        """
        snapshot = self._runtime.snapshot(run_id)
        if snapshot is None:
            return json.dumps({"error": f"no active_vln task run {run_id!r}"})
        return json.dumps(snapshot.as_dict())

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

        handle = self._runtime.begin(instruction, self.config.max_turns)
        if handle is None:
            state = self._runtime.current_state()
            return (
                f"An active_vln run is already in progress (run_id="
                f"{self._runtime.current_run_id()}, state={state}). "
                "It keeps its motion authority until it has fully stopped; "
                "call stop_active_vln or wait for it to finish."
            )

        # Open (or re-stamp, on a same-tool MCP takeover) the tool-stream
        # BEFORE anything else, so the movement hold is always carried by a
        # live stream — the tool_stopped frame on close is what releases
        # CAP_MOVEMENT at the McpServer (see Module.start_tool).
        self.start_tool("active_vln")
        self._running.set()
        self._runtime.mark_running(handle)
        try:
            return self._run(instruction, handle)
        except Exception as e:  # unexpected: settle the task, keep the robot safe
            logger.exception("active_vln run crashed")
            handle.finish(FAILED, EXECUTION_ERROR, repr(e))
            return f"active_vln run failed unexpectedly ({type(e).__name__}); stopped."
        finally:
            # Every exit path: robot at rest, goal cancelled, capability
            # released via the stream close, run state settled by _run.
            self.tele_cmd_vel.publish(Twist.zero())
            self._navigation.cancel_goal()
            self.stop_tool("active_vln")
            self._running.clear()

    def _run(self, instruction: str, handle: VlnRunHandle) -> str:
        """The turn loop: ask the policy, execute its actions, until stop or
        budget. All state transitions report through this run's handle —
        never through shared flags — so a policy answer that arrives after
        the run was cancelled is dropped, not executed (the handle's cancel
        event is checked before any action and inside every PD cycle).
        """
        client = self._client()
        conversations: list[dict[str, Any]] = [
            {"role": "system", "content": [{"type": "text", "text": SYSTEM_PROMPT}]}
        ]

        for turn in range(1, handle.max_turns + 1):
            if handle.cancelled:
                self._runtime.set_stage(handle, STOPPING_STAGE, turn)
                handle.finish(CANCELLED, USER_CANCELLED)
                return "Stopped by stop_active_vln, stop_navigation or stop_movement."

            if self._latest_image is None:
                handle.finish(FAILED, SENSOR_UNAVAILABLE, "camera stream stopped mid-run")
                return "Camera stream stopped mid-run."

            first = len(conversations) == 1
            content = [
                {
                    "type": "text",
                    "text": "[Initial Observation]:"
                    if first
                    else "After that, the observation is:",
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _image_to_data_url(self._latest_image, self.config.max_pixels)
                    },
                },
                {"type": "text", "text": PROMPT_TEMPLATE.format(instruction=instruction)},
            ]
            conversations.append({"role": "user", "content": content})

            self._runtime.set_stage(handle, INFERENCE, turn)
            try:
                answer = self._chat(client, conversations)
            except Exception as e:
                logger.error("ActiveVLN inference failed", exc_info=e)
                handle.finish(FAILED, POLICY_ERROR, repr(e))
                return f"Policy endpoint error: {e}"
            conversations.append(
                {"role": "assistant", "content": [{"type": "text", "text": answer}]}
            )
            logger.info("ActiveVLN turn %d: %s", turn, answer)
            # A cancelled run's in-flight inference answer is stale by
            # definition: drop it instead of acting on it.
            if handle.cancelled:
                self._runtime.set_stage(handle, STOPPING_STAGE, turn)
                handle.finish(CANCELLED, USER_CANCELLED)
                return "Stopped by stop_active_vln, stop_navigation or stop_movement."

            actions = _parse_actions(answer)
            if not actions:
                # Unparseable answer: ask again with a fresh observation
                # rather than moving blindly (the policy never saw this
                # question answered with silence during training).
                self._runtime.set_stage(handle, WAITING_OBSERVATION, turn)
                continue

            self._runtime.set_stage(handle, ACTION_EXECUTION, turn)
            self.tool_update(
                "active_vln", f"turn {turn}/{handle.max_turns}: {len(actions)} actions"
            )
            for action_id, magnitude in actions:
                if handle.cancelled or action_id == _STOP:
                    if action_id == _STOP:
                        handle.finish(COMPLETED, POLICY_STOP)
                        return "Policy said stop."
                    handle.finish(CANCELLED, USER_CANCELLED)
                    return "Stopped by stop_active_vln, stop_navigation or stop_movement."
                if not self._run_action(action_id, magnitude, handle):
                    # The planner failed on this move; the next observation
                    # shows the unchanged view, and the policy re-decides.
                    logger.info(
                        "ActiveVLN move did not complete: %s %.0f",
                        _ACTION_NAMES[action_id],
                        magnitude,
                    )
                    self._runtime.set_stage(handle, WAITING_OBSERVATION, turn)
                    break

        handle.finish(COMPLETED, TURN_BUDGET_EXHAUSTED)
        return f"Turn budget ({handle.max_turns}) spent; stopped where the robot is."

    def _run_action(
        self, action_id: int, magnitude: float, handle: VlnRunHandle | None = None
    ) -> bool:
        """One coalesced action as a PD velocity homing run; True when done.

        The VLN's actions are tiny position deltas (25 cm / 15 deg) — at or
        below the A* planner's arrival tolerances (0.2 m / 15 deg), which
        accept such goals as reached before the robot has moved. The official
        StreamVLN real-world deployment instead drives the incremental goals
        with a PD velocity controller (its realworld/pid_controller.py);
        this is that controller, on tele_cmd_vel. The run's cancel event is
        threaded through so a stop mid-drive unblocks the PD loop within one
        cycle.
        """
        pose = self._latest_odom
        if pose is None:
            return False
        cancel = handle.cancel if handle is not None else None
        if action_id == _FORWARD:
            target = _relative_goal(pose, magnitude / 100.0, 0.0)
            return _pd_drive(self, target, cancel=cancel)
        # Turns carry their sign through _coalesce (left +, right -).
        degrees = magnitude if action_id == _LEFT else -abs(magnitude)
        target = _relative_goal(pose, 0.0, degrees)
        return _pd_drive(self, target, turn_only=True, cancel=cancel)


def _pd_drive(
    skill: ActiveVlnSkill | Any,
    target: PoseStamped,
    turn_only: bool = False,
    cancel: threading.Event | None = None,
) -> bool:
    """PD velocity homing to *target* — the official deployment's controller.

    10 Hz loop: forward-axis and yaw errors from the odom to the target, PD
    step (the deployment's gains) damped by the MEASURED velocity — the
    official controller's own term (realworld/pid_controller.py solves with
    the robot's reported [v, w]; damping on the last commanded value instead
    underdamps any plant that tracks faster than commanded) — clamped
    velocities on tele_cmd_vel. Completes — True — only when the driven
    errors are inside the done thresholds, so the asked displacement
    happened by construction; no planner arrival state to trust. False on
    stop (urgent stop clears the running flag), odom loss, or timeout,
    always leaving a zero twist.

    The command rate is part of the stability story on plants that track
    faster than commanded: the MuJoCo Go2 runs ~2-5x the commanded rate
    (measured live), so at the default 10 Hz the per-cycle loop gain sits
    near 1 and turns relay-oscillate (30 s timeouts observed live); the sim
    stack configures drive_hz=50, where the loop is comfortably stable.
    Real hardware with tight velocity tracking runs the official 10 Hz.

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
    stuck_since: float | None = None
    reference = skill._latest_odom
    target_yaw = target.orientation.to_euler().yaw
    prev_pose = skill._latest_odom
    prev_t = time.monotonic()

    def _cancelled() -> bool:
        # The run's own cancel event (preferred); the skill-wide _running
        # flag keeps the pre-lifecycle callers and the M0 unit tests working.
        return cancel.is_set() if cancel is not None else not skill._running.is_set()

    while time.monotonic() < deadline and not _cancelled():
        pose = skill._latest_odom
        if pose is None:
            break
        now = time.monotonic()
        yaw = pose.orientation.to_euler().yaw
        # Measured velocity (finite differences on the odom between cycles) —
        # the official controller's damping term. Zero when the odom stands
        # still between cycles (or is a fresh equal pose).
        v_meas, w_meas = 0.0, 0.0
        if prev_pose is not None and pose is not prev_pose:
            dt_meas = now - prev_t
            if dt_meas > 1e-3:
                prev_yaw = prev_pose.orientation.to_euler().yaw
                dx = pose.position.x - prev_pose.position.x
                dy = pose.position.y - prev_pose.position.y
                v_meas = (dx * math.cos(prev_yaw) + dy * math.sin(prev_yaw)) / dt_meas
                w_meas = ((yaw - prev_yaw + math.pi) % (2 * math.pi) - math.pi) / dt_meas
        prev_pose, prev_t = pose, now
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
            v = cfg.kp_trans * max(-1.0, min(1.0, e_p)) - cfg.kd_trans * v_meas
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
        w = cfg.kp_yaw * max(-1.0, min(1.0, e_r)) - cfg.kd_yaw * w_meas
        w = max(-cfg.max_w, min(cfg.max_w, w))
        skill.tele_cmd_vel.publish(Twist(linear=Vector3(v, 0.0, 0.0), angular=Vector3(0.0, 0.0, w)))
        time.sleep(dt)
    skill.tele_cmd_vel.publish(Twist.zero())
    return False


active_vln_skill = ActiveVlnSkill.blueprint()
