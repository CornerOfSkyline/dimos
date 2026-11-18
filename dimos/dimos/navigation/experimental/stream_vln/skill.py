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
    _runtime: VlnTaskRuntime

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._running = threading.Event()
        self._runtime = VlnTaskRuntime("stream_vln")

    @rpc
    def start(self) -> None:
        super().start()
        from reactivex.disposable import Disposable

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
        self.stop_tool("stream_vln")
        super().stop()

    @skill(uses=[CAP_MOVEMENT])
    def stop_stream_vln(self) -> str:
        """Stop the stream_vln run under way, if any.

        The robot stops where it is; the next stream_vln call starts a fresh
        policy episode (reset), which is what the server does with its
        SlowFast memory on a new instruction anyway.
        """

        self._runtime.request_cancel()  # the run's own handle event
        self._running.clear()
        self.tele_cmd_vel.publish(Twist.zero())
        self._navigation.cancel_goal()
        return "Stopped stream_vln."

    def _on_color_image(self, image: Image) -> None:
        self._latest_image = image

    def _on_odom(self, odom: PoseStamped) -> None:
        self._latest_odom = odom

    @skill
    def get_vln_task_status(self, run_id: str | None = None) -> str:
        """Get the status of a VLN task run (stream_vln policy).

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
            return json.dumps({"error": f"no stream_vln task run {run_id!r}"})
        return json.dumps(snapshot.as_dict())

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

        handle = self._runtime.begin(instruction, self.config.max_turns)
        if handle is None:
            state = self._runtime.current_state()
            return (
                f"A stream_vln run is already in progress (run_id="
                f"{self._runtime.current_run_id()}, state={state}). "
                "It keeps its motion authority until it has fully stopped; "
                "call stop_stream_vln or wait for it to finish."
            )

        # Open (or re-stamp, on a same-tool MCP takeover) the tool-stream
        # BEFORE anything else, so the movement hold is always carried by a
        # live stream — the tool_stopped frame on close is what releases
        # CAP_MOVEMENT at the McpServer (see Module.start_tool).
        self.start_tool("stream_vln")
        self._running.set()
        self._runtime.mark_running(handle)
        try:
            return self._run(instruction, handle)
        except Exception as e:  # unexpected: settle the task, keep the robot safe
            logger.exception("stream_vln run crashed")
            handle.finish(FAILED, EXECUTION_ERROR, repr(e))
            return f"stream_vln run failed unexpectedly ({type(e).__name__}); stopped."
        finally:
            # Every exit path: robot at rest, goal cancelled, capability
            # released via the stream close, run state settled by _run.
            self.tele_cmd_vel.publish(Twist.zero())
            self._navigation.cancel_goal()
            self.stop_tool("stream_vln")
            self._running.clear()

    def _run(self, instruction: str, handle: VlnRunHandle) -> str:
        """The step loop: post frames, execute actions, until stop or budget.

        The server's SlowFast memory spans one instruction (reset on the
        first frame); a failed run should not poison the next, so a fresh
        reset is requested on the next call by tracking it per run here.
        All state transitions report through this run's handle — never
        through shared flags — so a policy answer that arrives after the
        run was cancelled is dropped, not executed (the handle's cancel
        event is checked before any action and inside every PD cycle).
        """
        reset = True
        for turn in range(1, handle.max_turns + 1):
            if handle.cancelled:
                self._runtime.set_stage(handle, STOPPING_STAGE, turn)
                handle.finish(CANCELLED, USER_CANCELLED)
                return "Stopped by stop_stream_vln, stop_navigation or stop_movement."
            if self._latest_image is None:
                handle.finish(FAILED, SENSOR_UNAVAILABLE, "camera stream stopped mid-run")
                return "Camera stream stopped mid-run."

            image = self._latest_image
            self._runtime.set_stage(handle, INFERENCE, turn)
            try:
                actions = self._post_frame(image, reset, instruction)
            except ValueError as e:
                handle.finish(FAILED, POLICY_ERROR, f"invalid action answer: {e}")
                return f"Rejected the policy's action answer ({e}); stopped."
            reset = False
            # A cancelled run's in-flight inference answer is stale by
            # definition: drop the whole batch instead of driving on it.
            if handle.cancelled:
                self._runtime.set_stage(handle, STOPPING_STAGE, turn)
                handle.finish(CANCELLED, USER_CANCELLED)
                return "Stopped by stop_stream_vln, stop_navigation or stop_movement."
            if actions is None:
                handle.finish(FAILED, POLICY_ERROR, "policy server unreachable")
                return "Policy server unreachable; stopped where the robot is."
            if not actions:
                self._runtime.set_stage(handle, WAITING_OBSERVATION, turn)
                continue  # empty answer: post the next frame, do not move blindly

            self._runtime.set_stage(handle, ACTION_EXECUTION, turn)
            self.tool_update(
                "stream_vln", f"turn {turn}/{handle.max_turns}: {len(actions)} actions"
            )
            for action_id, magnitude in actions:
                if handle.cancelled or action_id == _STOP:
                    if action_id == _STOP:
                        handle.finish(COMPLETED, POLICY_STOP)
                        return "Policy said stop."
                    handle.finish(CANCELLED, USER_CANCELLED)
                    return "Stopped by stop_stream_vln, stop_navigation or stop_movement."
                if not self._execute(action_id, magnitude, handle):
                    logger.info("stream_vln move did not complete: %s %.0f", action_id, magnitude)
                    self._runtime.set_stage(handle, WAITING_OBSERVATION, turn)
                    break  # next frame shows the blocked view; the policy re-decides

        handle.finish(COMPLETED, TURN_BUDGET_EXHAUSTED)
        return f"Step budget ({handle.max_turns}) spent; stopped where the robot is."

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

    def _execute(
        self, action_id: int, magnitude: float, handle: VlnRunHandle | None = None
    ) -> bool:
        """One coalesced action via the shared PD velocity homing (see
        active_vln's _pd_drive): the VLN's micro-goals (25 cm / 15 deg) sit at
        or below the A* planner's arrival tolerances, so they drive as direct
        velocity commands instead, like the official real-world deployment.
        The run's cancel event is threaded through so a stop mid-drive
        unblocks the PD loop within one cycle."""
        pose = self._latest_odom
        if pose is None:
            return False
        cancel = handle.cancel if handle is not None else None
        if action_id == _FORWARD:
            target = _relative_goal(pose, magnitude / 100.0, 0.0)
            return _pd_drive(self, target, cancel=cancel)
        # Turns drive the yaw loop alone (turn_only): a quadruped turning in
        # place drifts positionally, and correcting that drift makes it orbit
        # the target instead of turning. See active_vln's _pd_drive.
        degrees = magnitude if action_id == _LEFT else -abs(magnitude)
        target = _relative_goal(pose, 0.0, degrees)
        return _pd_drive(self, target, turn_only=True, cancel=cancel)


stream_vln_skill = StreamVlnSkill.blueprint()
