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

"""stream_vln unit tests: the server protocol client, without the server.

The interface contract is StreamVLN's own deployment
(streamvln/http_realworld_server.py + realworld/go2_vln_client.py): the
instruction and the reset flag travel with every post (the server injects
the instruction when it builds the conversation, on the first inference
after a reset), the frame is BGR-ordered JPEG (the server flips the channels
back to RGB), every returned action is consumed (the server advances 4
internal steps per answer — the SlowFast schedule — and the official client
chains the whole sequence into its goal), ids outside the VLN-CE space are
rejected instead of guessed, run state is per-instance, and every failure
path leaves the robot at rest (zero twist published).
"""

from __future__ import annotations

import io
import json
import threading
from typing import Any

import numpy as np
import pytest
import requests

from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.sensor_msgs.Image import Image, ImageFormat
from dimos.navigation.experimental.active_vln.skill import (
    _FORWARD,
    _LEFT,
    _RIGHT,
    _STOP,
    _image_to_bgr_jpeg,
)
from dimos.navigation.experimental.stream_vln.skill import (
    StreamVlnConfig,
    StreamVlnSkill,
    _parse_server_actions,
)
from dimos.navigation.experimental.vln_task import VlnTaskRuntime


def _red_bgr_image() -> Image:
    data = np.zeros((8, 8, 3), dtype=np.uint8)
    data[..., 2] = 255  # BGR red
    return Image(data=data, format=ImageFormat.BGR)


# -- server answer parsing -----------------------------------------------------


def test_all_returned_actions_are_consumed() -> None:
    # No [:3] truncation: the server advances 4 internal steps per answer
    # (its every-4-steps SlowFast inference) and the official client
    # executes every returned action.
    assert _parse_server_actions([1, 1, 1, 1]) == [(_FORWARD, 75.0), (_FORWARD, 25.0)]
    assert _parse_server_actions([2, 2, 2, 2]) == [(_LEFT, 45.0), (_LEFT, 15.0)]
    assert _parse_server_actions([3, 3, 3, 3]) == [(_RIGHT, -45.0), (_RIGHT, -15.0)]
    assert _parse_server_actions([1, 2, 1, 2]) == [
        (_FORWARD, 25.0),
        (_LEFT, 15.0),
        (_FORWARD, 25.0),
        (_LEFT, 15.0),
    ]


def test_stop_is_kept_in_order_after_the_preceding_moves() -> None:
    assert _parse_server_actions([1, 1, 0]) == [(_FORWARD, 50.0), (_STOP, 0.0)]
    assert _parse_server_actions([0]) == [(_STOP, 0.0)]
    assert _parse_server_actions([]) == []


@pytest.mark.parametrize(
    "bad",
    [[4], [1, -1], ["1"], [1, None], [True], [1.0], "010", {"action": [1]}, None],
)
def test_answers_outside_the_id_space_are_rejected(bad: Any) -> None:
    # Unknown ids must fail loudly, never map to a default move.
    with pytest.raises(ValueError):
        _parse_server_actions(bad)


# -- the wire: reset + instruction + BGR-ordered frame --------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _skill(scripted_answers: list[Any], executed: list[tuple[int, float]]) -> StreamVlnSkill:
    """A run-capable skill instance without the Module machinery.

    __new__ skips Module.__init__ (which opens real transports); the run loop
    only reads the attributes set here (its own _execute is replaced by a
    recorder — the shared PD homing has its own tests in active_vln). The
    tool-stream trio (start_tool/tool_update/stop_tool) is faked with
    recorders so lifecycle tests can assert on the stream open/close order.
    """
    skill = StreamVlnSkill.__new__(StreamVlnSkill)
    skill.config = StreamVlnConfig(max_turns=5, request_timeout=1.0)
    skill._running = threading.Event()  # the public entry sets it itself
    skill._runtime = VlnTaskRuntime("stream_vln")
    skill._latest_image = _red_bgr_image()
    skill._latest_odom = object()  # opaque: _execute is replaced below

    class _Tele:
        twists: list[Twist] = []

        def publish(self, twist: Twist) -> None:
            self.twists.append(twist)

    class _Nav:
        cancelled = 0

        def cancel_goal(self) -> bool:
            _Nav.cancelled += 1
            return True

    skill.tele_cmd_vel = _Tele()
    skill._navigation = _Nav()
    skill.tools_opened: list[str] = []
    skill.tools_closed: list[str] = []
    skill.tool_updates: list[str] = []
    skill.start_tool = lambda name: skill.tools_opened.append(name)  # type: ignore[method-assign]
    skill.stop_tool = lambda name: skill.tools_closed.append(name)  # type: ignore[method-assign]
    skill.tool_update = lambda name, msg: skill.tool_updates.append((name, msg))  # type: ignore[method-assign]

    def fake_execute(action_id: int, magnitude: float, handle: Any = None) -> bool:
        executed.append((action_id, magnitude))
        return True

    skill._execute = fake_execute  # type: ignore[method-assign]
    return skill


def _scripted_post(
    posts: list[tuple[str, dict[str, Any], dict[str, Any]]], answers: list[Any]
) -> Any:
    """A requests.post stand-in that records the wire and replays answers."""

    def fake_post(url: str, files: Any = None, data: Any = None, timeout: Any = None) -> Any:
        posts.append((url, dict(files), dict(data)))
        answer = answers.pop(0) if answers else {"action": [0]}
        if isinstance(answer, Exception):
            raise answer
        return _FakeResponse(answer)

    return fake_post


def test_reset_instruction_and_bgr_frame_reach_the_wire(monkeypatch: Any) -> None:
    from PIL import Image as PILImage

    posts: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(
        requests, "post", _scripted_post(posts, [{"action": [1, 1, 1, 1]}, {"action": [0]}])
    )
    executed: list[tuple[int, float]] = []
    skill = _skill([], executed)

    result = StreamVlnSkill.stream_vln(skill, "walk to the sofa")

    assert result == "Policy said stop."
    # All four returned actions were consumed (coalesced, lossless).
    assert executed == [(_FORWARD, 75.0), (_FORWARD, 25.0)]

    # First post: reset + instruction + the BGR-ordered JPEG frame.
    url, files, data = posts[0]
    assert url.endswith("/eval_vln")
    payload = json.loads(data["json"])
    assert payload == {"reset": True, "instruction": "walk to the sofa"}
    name, jpeg, mime = files["image"]
    assert (name, mime) == ("rgb_image.jpg", "image/jpeg")
    pil = PILImage.open(io.BytesIO(jpeg))
    r, g, b = pil.getpixel((4, 4))
    assert b > 200 and r < 10 and g < 10  # red, in BGR order

    # Second post carries the instruction too; reset only on the first.
    payload = json.loads(posts[1][2]["json"])
    assert payload == {"reset": False, "instruction": "walk to the sofa"}


def test_invalid_answer_is_rejected_without_moving(monkeypatch: Any) -> None:
    posts: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(requests, "post", _scripted_post(posts, [{"action": [1, 9]}]))
    executed: list[tuple[int, float]] = []
    skill = _skill([], executed)

    result = StreamVlnSkill.stream_vln(skill, "walk to the sofa")

    assert result.startswith("Rejected the policy's action answer")
    assert executed == []  # nothing moved
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()
    assert skill._navigation.cancelled == 1


def test_server_unreachable_stops_at_rest(monkeypatch: Any) -> None:
    posts: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(requests, "post", _scripted_post(posts, [requests.ConnectionError("down")]))
    executed: list[tuple[int, float]] = []
    skill = _skill([], executed)

    result = StreamVlnSkill.stream_vln(skill, "walk to the sofa")

    assert result == "Policy server unreachable; stopped where the robot is."
    assert executed == []
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()


def test_empty_answer_reposts_without_moving(monkeypatch: Any) -> None:
    posts: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(requests, "post", _scripted_post(posts, [{"action": []}, {"action": [0]}]))
    executed: list[tuple[int, float]] = []
    skill = _skill([], executed)

    result = StreamVlnSkill.stream_vln(skill, "walk to the sofa")

    assert result == "Policy said stop."
    assert executed == []
    assert len(posts) == 2


def test_blocked_move_lets_the_policy_re_decide(monkeypatch: Any) -> None:
    posts: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(requests, "post", _scripted_post(posts, [{"action": [1]}, {"action": [0]}]))
    executed: list[tuple[int, float]] = []
    skill = _skill([], executed)

    def blocked_execute(action_id: int, magnitude: float, handle: Any = None) -> bool:
        executed.append((action_id, magnitude))
        return False  # the drive gave up (obstacle): policy re-decides

    skill._execute = blocked_execute  # type: ignore[method-assign]
    result = StreamVlnSkill.stream_vln(skill, "walk to the sofa")

    assert result == "Policy said stop."
    assert executed == [(_FORWARD, 25.0)]  # the blocked move, then a fresh frame
    assert len(posts) == 2


def test_stop_mid_run_interrupts_execution(monkeypatch: Any) -> None:
    posts: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(
        requests, "post", _scripted_post(posts, [{"action": [1, 1, 1, 1]}, {"action": [0]}])
    )
    executed: list[tuple[int, float]] = []
    skill = _skill([], executed)

    def execute_then_stop(action_id: int, magnitude: float, handle: Any = None) -> bool:
        executed.append((action_id, magnitude))
        if len(executed) >= 2:
            # urgent stop arrives mid-batch: the stop skill's own signal
            skill._runtime.request_cancel()
        return True

    skill._execute = execute_then_stop  # type: ignore[method-assign]
    result = StreamVlnSkill.stream_vln(skill, "walk to the sofa")

    # The batch finished (both goals executed); the stop surfaced at the
    # next turn's running-check.
    assert result == "Stopped by stop_stream_vln, stop_navigation or stop_movement."
    assert executed == [(_FORWARD, 75.0), (_FORWARD, 25.0)]
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()
    assert skill._navigation.cancelled == 1


# -- run state is per-instance (the class-level Event regression) ---------------


def test_run_state_is_not_shared_on_the_class() -> None:
    # A class-level `= threading.Event()` default shared one Event across
    # every instance — stopping one skill's run stopped them all. The state
    # must live on instances (assigned in __init__).
    assert "_running" not in StreamVlnSkill.__dict__
    assert "_run_lock" not in StreamVlnSkill.__dict__


def test_stop_skill_releases_the_run_and_rests() -> None:
    skill = _skill([], [])
    assert StreamVlnSkill.stop_stream_vln(skill) == "Stopped stream_vln."
    assert not skill._running.is_set()
    assert skill.tele_cmd_vel.twists[-1] == Twist.zero()
    assert skill._navigation.cancelled == 1


def test_frame_encoding_matches_the_official_client_bytes() -> None:
    # Cross-check against the shared encoder directly: the wire bytes are
    # BGR JPEG, whatever the camera image's declared format is.
    from PIL import Image as PILImage

    rgb_frame = Image(data=np.zeros((8, 8, 3), dtype=np.uint8), format=ImageFormat.RGB)
    rgb_frame.data[..., 0] = 255  # RGB red
    pil = PILImage.open(io.BytesIO(_image_to_bgr_jpeg(rgb_frame)))
    r, g, b = pil.getpixel((4, 4))
    assert b > 200 and r < 10 and g < 10  # red, in BGR order
