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

"""active_vln unit tests: answer parsing, coalescing, goal geometry, image
encoding, and the PD homing execution — no policy server, no robot.

The interface contract is ActiveVLN's own inference code (eval/vlnce/
eval_vlnce.py): every sub-action the answer names is kept and clamped to the
per-answer caps the official executor enforces (min(3, magnitude/step)
elementary steps -> 75 cm forward / 45 deg turn), merging never drops any
part of an action sequence, run state is per-instance, and every failure
path leaves the robot at rest (zero twist published).
"""

from __future__ import annotations

import base64
from collections.abc import Callable
import io
import threading
import time
from typing import Any

import numpy as np
import pytest

from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.geometry_msgs.Vector3 import Vector3, make_vector3
from dimos.msgs.sensor_msgs.Image import Image, ImageFormat
from dimos.navigation.experimental.active_vln.skill import (
    _FORWARD,
    _LEFT,
    _RIGHT,
    _STOP,
    ActiveVlnConfig,
    ActiveVlnSkill,
    _coalesce,
    _image_to_bgr_jpeg,
    _image_to_data_url,
    _parse_actions,
    _pd_drive,
    _relative_goal,
)


def _pose(x: float, y: float, yaw_deg: float) -> PoseStamped:
    return PoseStamped(
        position=Vector3(x, y, 0.0),
        orientation=Quaternion.from_euler(make_vector3(0.0, 0.0, np.radians(yaw_deg))),
        frame_id="map",
    )


def _red_bgr_image() -> Image:
    data = np.zeros((8, 8, 3), dtype=np.uint8)
    data[..., 2] = 255  # BGR red
    return Image(data=data, format=ImageFormat.BGR)


# -- answer parsing (the official executor's semantics) ----------------------


def test_parse_keeps_every_sub_action_in_order() -> None:
    assert _parse_actions("turn left 15 degrees, move forward 50cm") == [
        (_LEFT, 15.0),
        (_FORWARD, 50.0),
    ]


def test_parse_clamps_to_the_official_per_answer_caps() -> None:
    # The official executor expands each sub-action to at most 3 elementary
    # steps: min(3, 100/25) -> 75 cm, min(3, 60/15) -> 45 deg.
    assert _parse_actions("move forward 100cm") == [(_FORWARD, 75.0)]
    assert _parse_actions("turn left 60 degrees") == [(_LEFT, 45.0)]
    assert _parse_actions("turn right 200 degrees") == [(_RIGHT, 45.0)]


def test_parse_stop_and_noise() -> None:
    assert _parse_actions("stop") == [(_STOP, 0.0)]
    assert _parse_actions("we are there, stop here") == [(_STOP, 0.0)]
    assert _parse_actions("the view is nice") == []
    assert _parse_actions("") == []


def test_parse_defaults_when_the_magnitude_is_missing() -> None:
    assert _parse_actions("move forward") == [(_FORWARD, 25.0)]
    assert _parse_actions("turn left") == [(_LEFT, 15.0)]
    assert _parse_actions("turn right") == [(_RIGHT, 15.0)]


# -- coalescing (lossless) ----------------------------------------------------


def test_coalesce_merges_same_direction() -> None:
    assert _coalesce([(_FORWARD, 25.0), (_FORWARD, 50.0)]) == [(_FORWARD, 75.0)]
    assert _coalesce([(_LEFT, 15.0), (_LEFT, 15.0)]) == [(_LEFT, 30.0)]
    assert _coalesce([(_RIGHT, 15.0), (_RIGHT, 15.0)]) == [(_RIGHT, -30.0)]


def test_coalesce_never_drops_a_run_overflow() -> None:
    # 4x25 cm = 100 cm (a StreamVLN batch) -> a 75 cm + a 25 cm goal.
    assert _coalesce([(_FORWARD, 25.0)] * 4) == [(_FORWARD, 75.0), (_FORWARD, 25.0)]
    # 60 deg of turning -> 45 deg + 15 deg goals.
    assert _coalesce([(_LEFT, 15.0)] * 4) == [(_LEFT, 45.0), (_LEFT, 15.0)]
    assert _coalesce([(_RIGHT, 15.0)] * 4) == [(_RIGHT, -45.0), (_RIGHT, -15.0)]


def test_coalesce_keeps_direction_changes_and_stop() -> None:
    # A left followed by a right is a direction change: two goals. The sign
    # is applied when same-direction runs merge (and again at execution for
    # un-merged rights); an un-merged action passes through unchanged.
    assert _coalesce([(_LEFT, 15.0), (_RIGHT, 15.0)]) == [(_LEFT, 15.0), (_RIGHT, 15.0)]
    assert _coalesce([(_FORWARD, 25.0), (_STOP, 0.0), (_FORWARD, 25.0)]) == [
        (_FORWARD, 25.0),
        (_STOP, 0.0),
        (_FORWARD, 25.0),
    ]
    # STOP never merges, not even into itself.
    assert _coalesce([(_STOP, 0.0), (_STOP, 0.0)]) == [(_STOP, 0.0), (_STOP, 0.0)]


def test_coalesce_is_lossless_in_total_magnitude() -> None:
    merged = _coalesce(
        [(_FORWARD, 75.0), (_FORWARD, 25.0), (_LEFT, 45.0), (_LEFT, 15.0), (_RIGHT, 15.0)]
    )
    assert sum(m for i, m in merged if i == _FORWARD) == 100.0
    assert sum(m for i, m in merged if i == _LEFT) == 60.0
    assert sum(abs(m) for i, m in merged if i == _RIGHT) == 15.0
    assert all(m <= 75.0 for i, m in merged if i == _FORWARD)
    assert all(abs(m) <= 45.0 for i, m in merged if i in (_LEFT, _RIGHT))


# -- goal geometry ------------------------------------------------------------


def test_relative_goal_forward_moves_along_the_heading() -> None:
    goal = _relative_goal(_pose(0.0, 0.0, 0.0), 0.25, 0.0)
    assert (goal.position.x, goal.position.y) == pytest.approx((0.25, 0.0))
    goal = _relative_goal(_pose(1.0, 2.0, 90.0), 0.25, 0.0)
    assert (goal.position.x, goal.position.y) == pytest.approx((1.0, 2.25))


def test_relative_goal_turn_keeps_the_position_and_adds_yaw() -> None:
    goal = _relative_goal(_pose(1.0, 2.0, 0.0), 0.0, 15.0)
    assert (goal.position.x, goal.position.y) == pytest.approx((1.0, 2.0))
    assert np.degrees(goal.orientation.to_euler().yaw) == pytest.approx(15.0)
    goal = _relative_goal(_pose(0.0, 0.0, 10.0), 0.0, -15.0)
    assert np.degrees(goal.orientation.to_euler().yaw) == pytest.approx(-5.0)


# -- image encoding ------------------------------------------------------------


def test_bgr_jpeg_keeps_the_official_wire_order() -> None:
    # The StreamVLN server flips channels on arrival, so the JPEG bytes must
    # be BGR-ordered (the official client's byte format): a red BGR frame
    # reaches the model as red, not as blue. JPEG is lossy — assert the
    # channel ORDER (blue channel dominant), not exact values.
    from PIL import Image as PILImage

    pil = PILImage.open(io.BytesIO(_image_to_bgr_jpeg(_red_bgr_image())))
    r, g, b = pil.getpixel((4, 4))
    assert b > 200 and r < 10 and g < 10  # red, in BGR order


def test_data_url_converts_bgr_to_rgb_for_the_chat_api() -> None:
    # The chat (vLLM) endpoint has no server-side flip: the data URL must
    # carry true RGB.
    from PIL import Image as PILImage

    url = _image_to_data_url(_red_bgr_image(), 80000)
    assert url.startswith("data:image/jpeg;base64,")
    pil = PILImage.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    r, g, b = pil.getpixel((4, 4))[:3]
    assert r > 200 and g < 10 and b < 10  # red, in RGB order


# -- PD homing execution -------------------------------------------------------


class _SimTele:
    """Publishes the commanded twist and integrates it into the fake odom.

    *gain* models a plant that tracks faster than commanded (the MuJoCo Go2
    moves ~5x the commanded linear and ~2.8x the commanded angular rate);
    *lag_tau* gives that amplified response first-order momentum (None =
    instant, the adversarial limit).
    """

    def __init__(
        self,
        skill: Any,
        on_publish: Callable[[Twist], None] | None = None,
        freeze: bool = False,
        lose_odom_after: int | None = None,
        gain: float = 1.0,
        lag_tau: float | None = None,
    ) -> None:
        self.skill = skill
        self.on_publish = on_publish
        self.freeze = freeze  # a blocked robot: commanded speed, no ground
        self.lose_odom_after = lose_odom_after
        self.gain = gain
        self.lag_tau = lag_tau
        self._v_rate = 0.0
        self._w_rate = 0.0
        self.twists: list[Twist] = []

    def publish(self, twist: Twist) -> None:
        self.twists.append(twist)
        if self.on_publish is not None:
            self.on_publish(twist)
        pose = self.skill._latest_odom
        if pose is None or self.lose_odom_after == len(self.twists):
            if self.lose_odom_after == len(self.twists):
                self.skill._latest_odom = None
            return
        if self.freeze or twist == Twist.zero():
            return
        dt = 1.0 / self.skill.config.drive_hz
        if self.lag_tau is None:
            v_eff = self.gain * twist.linear.x
            w_eff = self.gain * twist.angular.z
        else:  # first-order lag toward the amplified command (momentum)
            k = min(1.0, dt / self.lag_tau)
            self._v_rate += (self.gain * twist.linear.x - self._v_rate) * k
            self._w_rate += (self.gain * twist.angular.z - self._w_rate) * k
            v_eff, w_eff = self._v_rate, self._w_rate
        yaw = pose.orientation.to_euler().yaw
        x = pose.position.x + v_eff * np.cos(yaw) * dt
        y = pose.position.y + v_eff * np.sin(yaw) * dt
        yaw += w_eff * dt
        self.skill._latest_odom = _pose(x, y, float(np.degrees(yaw)))


def _sim_skill(**cfg: Any) -> tuple[ActiveVlnSkill, _SimTele]:
    """A driving-capable skill instance without the Module machinery.

    __new__ skips Module.__init__ (which opens real transports); the driving
    code only reads the attributes set here. This is also why run state
    living on the class (not __init__) was a real cross-instance bug.
    """
    skill = ActiveVlnSkill.__new__(ActiveVlnSkill)
    skill.config = ActiveVlnConfig(step_timeout=5.0, **cfg)
    skill._running = threading.Event()
    skill._running.set()
    skill._run_lock = threading.Lock()
    skill._latest_odom = _pose(0.0, 0.0, 0.0)
    skill.tele_cmd_vel = _SimTele(skill)
    return skill, skill.tele_cmd_vel


def test_pd_drive_homes_a_forward_goal_and_rests() -> None:
    skill, tele = _sim_skill()
    target = _relative_goal(skill._latest_odom, 0.25, 0.0)
    assert _pd_drive(skill, target) is True
    pose = skill._latest_odom
    assert (pose.position.x, pose.position.y) == pytest.approx((0.25, 0.0), abs=0.05)
    assert tele.twists[-1] == Twist.zero()


def test_pd_drive_turns_in_place_and_rests() -> None:
    skill, tele = _sim_skill()
    target = _relative_goal(skill._latest_odom, 0.0, 15.0)
    assert _pd_drive(skill, target, turn_only=True) is True
    pose = skill._latest_odom
    assert (pose.position.x, pose.position.y) == pytest.approx((0.0, 0.0), abs=1e-6)
    # Completes inside the done threshold (done_deg = 3), not exactly on it.
    assert np.degrees(pose.orientation.to_euler().yaw) == pytest.approx(15.0, abs=3.5)
    assert tele.twists[-1] == Twist.zero()


def test_pd_drive_stops_on_urgent_stop_and_rests() -> None:
    skill, tele = _sim_skill()

    def stop_after_three(twist: Twist) -> None:
        if len(tele.twists) >= 3:
            skill._running.clear()

    tele.on_publish = stop_after_three
    target = _relative_goal(skill._latest_odom, 0.25, 0.0)
    assert _pd_drive(skill, target) is False
    assert tele.twists[-1] == Twist.zero()


def test_pd_drive_abandons_when_blocked_and_rests() -> None:
    skill, tele = _sim_skill(stuck_s=0.2)
    tele.freeze = True
    target = _relative_goal(skill._latest_odom, 0.25, 0.0)
    t0 = time.monotonic()
    assert _pd_drive(skill, target) is False
    assert time.monotonic() - t0 < 4.0  # gave up long before step_timeout
    assert tele.twists[-1] == Twist.zero()


def test_pd_drive_survives_odom_loss_and_rests() -> None:
    skill, tele = _sim_skill()
    tele.lose_odom_after = 3
    target = _relative_goal(skill._latest_odom, 0.25, 0.0)
    assert _pd_drive(skill, target) is False
    assert tele.twists[-1] == Twist.zero()


def test_pd_drive_converges_on_a_plant_tracking_faster_than_commanded() -> None:
    # The MuJoCo Go2 tracks ~5x the commanded linear and ~2.8x the commanded
    # angular rate (measured live: 0.75 m in 0.3 s at a 0.5 m/s clamp; ~16
    # deg from one 0.79 rad/s command) with ~0.2 s of momentum. The official
    # controller (measured-velocity damping) handles it when the command rate
    # keeps the per-cycle loop gain well under 1 — the sim stack runs
    # drive_hz=50. At the real-robot 10 Hz the same amplified plant sits at a
    # loop gain near 1 and relay-oscillates (30 s timeouts observed live);
    # see artifacts/vln_m0/call_chain.md.
    for gain in (2.8, 5.0):
        skill, tele = _sim_skill(drive_hz=50.0)
        tele.gain = gain
        tele.lag_tau = 0.2  # the plant's momentum (no instant-gain teleporting)
        target = _relative_goal(skill._latest_odom, 0.25, 0.0)
        assert _pd_drive(skill, target) is True, f"forward goal did not converge at gain {gain}"
        pose = skill._latest_odom
        assert pose is not None
        assert (pose.position.x, pose.position.y) == pytest.approx((0.25, 0.0), abs=0.06)
        assert tele.twists[-1] == Twist.zero()

        skill, tele = _sim_skill(drive_hz=50.0)
        tele.gain = gain
        tele.lag_tau = 0.2
        target = _relative_goal(skill._latest_odom, 0.0, 15.0)
        assert _pd_drive(skill, target, turn_only=True) is True, (
            f"turn goal did not converge at gain {gain}"
        )
        pose = skill._latest_odom
        assert pose is not None
        assert np.degrees(pose.orientation.to_euler().yaw) == pytest.approx(15.0, abs=3.5)
        assert tele.twists[-1] == Twist.zero()


# -- run state is per-instance (the class-level Event regression) -------------


def test_run_state_is_not_shared_on_the_class() -> None:
    # A class-level `= threading.Event()` default shared one Event across
    # every instance — stopping one skill's run stopped them all. The state
    # must live on instances (assigned in __init__).
    assert "_running" not in ActiveVlnSkill.__dict__
    assert "_run_lock" not in ActiveVlnSkill.__dict__


def test_stop_skill_releases_the_run_and_rests() -> None:
    skill, tele = _sim_skill()

    class _Nav:
        cancelled = 0

        def cancel_goal(self) -> bool:
            _Nav.cancelled += 1
            return True

    skill._navigation = _Nav()
    assert ActiveVlnSkill.stop_active_vln(skill) == "Stopped active_vln."
    assert not skill._running.is_set()
    assert tele.twists[-1] == Twist.zero()
    assert _Nav.cancelled == 1


# -- the turn loop against a scripted policy -----------------------------------


def _chat_skill(answers: list[str]) -> tuple[ActiveVlnSkill, list[Any], list[tuple[int, float]]]:
    """A skill whose policy endpoint answers from a script.

    Returns (skill, captured messages per turn, executed coalesced actions).
    """
    skill, tele = _sim_skill(max_turns=4)
    skill._latest_image = _red_bgr_image()
    captured: list[Any] = []
    executed: list[tuple[int, float]] = []
    remaining = list(answers)

    def fake_client() -> Any:
        return object()

    def fake_chat(client: Any, messages: list[dict[str, Any]]) -> str:
        captured.append(list(messages))  # snapshot: the skill mutates it per turn
        return remaining.pop(0)

    def fake_run_action(action_id: int, magnitude: float) -> bool:
        executed.append((action_id, magnitude))
        return True

    class _Nav:
        def cancel_goal(self) -> bool:
            return True

    skill._client = fake_client  # type: ignore[method-assign]
    skill._chat = fake_chat  # type: ignore[method-assign]
    skill._run_action = fake_run_action  # type: ignore[method-assign]
    skill._navigation = _Nav()
    skill._running.clear()  # a fresh run: active_vln sets it itself
    return skill, captured, executed


def _user_text(messages: list[dict[str, Any]]) -> str:
    return "".join(part["text"] for part in messages[-1]["content"] if part.get("type") == "text")


def test_instruction_reaches_the_prompt_and_stop_ends_the_run() -> None:
    skill, captured, executed = _chat_skill(["move forward 75cm, move forward 75cm", "stop"])
    result = ActiveVlnSkill.active_vln(skill, "walk to the sofa")
    assert result == "Policy said stop."
    # The prompt carries the instruction (the R2R grammar template).
    assert "Instruction: walk to the sofa" in _user_text(captured[0])
    # Both 75 cm sub-actions were kept, coalesced to 150 cm, split at the cap.
    assert executed == [(_FORWARD, 75.0), (_FORWARD, 75.0)]
    assert not skill._running.is_set()


def test_camera_loss_mid_run_releases_the_run_state() -> None:
    # The camera dropping mid-run must not leak the run flag (it did before
    # the try/finally: the next active_vln call would think one is running).
    skill, captured, executed = _chat_skill(["turn left 15 degrees", "stop"])

    def fake_run_action(action_id: int, magnitude: float) -> bool:
        executed.append((action_id, magnitude))
        skill._latest_image = None  # camera drops after the first move
        return True

    skill._run_action = fake_run_action  # type: ignore[method-assign]
    result = ActiveVlnSkill.active_vln(skill, "walk to the sofa")
    assert result == "Camera stream stopped mid-run."
    assert not skill._running.is_set()
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()


def test_endpoint_error_stops_at_rest() -> None:
    skill, captured, executed = _chat_skill([])

    def fake_chat(client: Any, messages: list[dict[str, Any]]) -> str:
        raise RuntimeError("vllm down")

    skill._chat = fake_chat  # type: ignore[method-assign]
    result = ActiveVlnSkill.active_vln(skill, "walk to the sofa")
    assert result.startswith("Policy endpoint error")
    assert not skill._running.is_set()
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()
    assert executed == []


def test_turn_budget_stops_at_rest() -> None:
    skill, captured, executed = _chat_skill(["turn left 15 degrees"] * 4)
    result = ActiveVlnSkill.active_vln(skill, "walk to the sofa")
    assert result == "Turn budget (4) spent; stopped where the robot is."
    assert not skill._running.is_set()
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()
    assert all(a == (_LEFT, 15.0) for a in executed)


def test_no_sensor_data_refuses_to_start() -> None:
    skill, _, _ = _chat_skill(["stop"])
    skill._latest_image = None
    skill._running.clear()  # as after any finished run
    assert (
        ActiveVlnSkill.active_vln(skill, "walk to the sofa")
        == "No camera or odometry data yet; cannot start active_vln."
    )
    assert not skill._running.is_set()
