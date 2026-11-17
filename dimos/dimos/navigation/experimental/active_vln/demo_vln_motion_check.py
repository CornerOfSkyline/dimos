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

"""M0-02 live motion verification: drive the Go2 (MuJoCo sim) through the
REAL PD homing controller (`_pd_drive`, the one both VLN skills run) over
the live `tele_cmd_vel` channel and record odometry evidence per case.

Closed loop under test (all live, no mocks):

    this script -> tele_cmd_vel (Twist) -> MovementManager (teleop priority,
    cancels nav goals) -> cmd_vel -> Go2 sim connection -> MuJoCo -> odom
    (PoseStamped) -> this script -> next PD step

Cases: forward 0.25 m, forward 0.50 m, left 15 deg, right 15 deg, a
coalesced sequence (forward 25 cm + left 15 deg + forward 25 cm as three
consecutive goals), and an urgent stop mid-drive (stop latency: from the
stop signal to the last non-zero commanded velocity).

Safety boundary (explicit): the PD homing drive is a DIRECT velocity
controller with no obstacle avoidance — it bypasses the A* planner on
purpose (the planner's 0.2 m / 15 deg arrival tolerances would swallow the
VLN's 25 cm / 15 deg steps; see active_vln.skill). If something blocks the
straight line, the stuck detector abandons the drive after `stuck_s` and
the policy re-decides; any teleop message overrides it instantly
(MovementManager's teleop priority), and a zero twist is published on every
exit path. Run this only in simulation.

Run (needs the sim stack running — see run_go2_streamvln.sh):

    cd dimos
    .venv/bin/python -m dimos.navigation.experimental.active_vln.demo_vln_motion_check \
        --out ../artifacts/vln_m0/basic_actions

Writes steps.jsonl (one record per case) and summary.json.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
from typing import Any

from dimos.core.transport import PubSubTransport
from dimos.core.transport_factory import make_transport
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.navigation.experimental.active_vln.skill import (
    ActiveVlnConfig,
    _pd_drive,
    _relative_goal,
)


def _yaw_deg(pose: PoseStamped) -> float:
    return math.degrees(pose.orientation.to_euler().yaw)


def _angle_delta(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


class _LiveTele:
    """tele_cmd_vel publisher that records every twist with a timestamp."""

    def __init__(self, transport: PubSubTransport[Any]) -> None:
        self._transport = transport
        self.sent: list[tuple[float, Twist]] = []

    def publish(self, twist: Twist) -> None:
        self.sent.append((time.time(), twist))
        self._transport.broadcast(None, twist)

    def last_nonzero_time(self) -> float | None:
        for t, twist in reversed(self.sent):
            if twist.linear.x != 0.0 or twist.angular.z != 0.0:
                return t
        return None


class _LiveSkill:
    """The _pd_drive surface against the live stack: the same attributes the
    unit tests fake, with tele_cmd_vel and the odom stream being real
    transport I/O (_latest_odom mirrors the live stream via a property).
    """

    def __init__(
        self, tele: _LiveTele, latest: dict[str, PoseStamped | None], **overrides: float
    ) -> None:
        self.config = ActiveVlnConfig(**overrides)  # PD constants (overridable)
        self.tele_cmd_vel = tele
        self._running = threading.Event()
        self._latest = latest

    @property
    def _latest_odom(self) -> PoseStamped | None:
        return self._latest["pose"]


def _pose_dict(pose: PoseStamped) -> dict[str, float]:
    return {"x": pose.position.x, "y": pose.position.y, "yaw_deg": _yaw_deg(pose)}


def _drive_goal(skill: _LiveSkill, name: str, forward_m: float, degrees: float) -> dict[str, Any]:
    """One PD-homing goal; returns the evidence record."""
    pose = skill._latest_odom
    assert pose is not None, "no odom"
    before = _pose_dict(pose)
    turn_only = forward_m == 0.0
    target = _relative_goal(pose, forward_m, degrees)
    skill._running.set()
    skill.tele_cmd_vel.sent.clear()

    t0 = time.time()
    done = _pd_drive(skill, target, turn_only=turn_only)
    duration = time.time() - t0

    pose = skill._latest_odom
    assert pose is not None, "odom lost mid-drive"
    after = _pose_dict(pose)
    distance = math.hypot(after["x"] - before["x"], after["y"] - before["y"])
    turned = _angle_delta(after["yaw_deg"], before["yaw_deg"])
    if turn_only:
        ok = abs(turned - degrees) <= skill.config.done_deg + 0.5
    else:
        ok = abs(distance - forward_m) <= skill.config.done_m + 0.005
    tele = skill.tele_cmd_vel
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "case": name,
        "requested": {"forward_m": forward_m, "turn_deg": degrees},
        "odom_before": before,
        "odom_after": after,
        "achieved": {"distance_m": round(distance, 4), "turn_deg": round(turned, 2)},
        "duration_s": round(duration, 3),
        "pd_drive_returned": bool(done),
        "twists_sent": len(tele.sent),
        "last_twist_zero": (tele.sent[-1][1] == Twist.zero()) if tele.sent else None,
        "pass": bool(ok),
    }


def _urgent_stop_case(skill: _LiveSkill, stop_after_s: float) -> dict[str, Any]:
    """Drive a long (0.75 m) goal; clear the run flag mid-drive (exactly what
    stop_stream_vln / the input-thread urgent stop do) and measure the
    latency from the stop signal to the last non-zero commanded velocity.
    """
    pose = skill._latest_odom
    assert pose is not None, "no odom"
    before = _pose_dict(pose)
    skill._running.set()
    tele = skill.tele_cmd_vel
    tele.sent.clear()
    stop_signal: dict[str, float] = {"t": 0.0}

    def stop() -> None:
        stop_signal["t"] = time.time()
        skill._running.clear()  # the urgent-stop path

    timer = threading.Timer(stop_after_s, stop)

    t0 = time.time()
    timer.start()
    done = _pd_drive(skill, _relative_goal(pose, 0.75, 0.0))
    duration = time.time() - t0

    # Physical settling: the commanded stop is instant (last non-zero
    # command), but the robot coasts; track displacement over the next 2 s
    # and over its last 0.5 s (residual motion near zero = at rest).
    pose = skill._latest_odom
    assert pose is not None, "odom lost mid-drive"
    settle_start = _pose_dict(pose)
    last_pose = pose
    max_disp = 0.0
    last_half_disp = 0.0
    half_t0 = time.time() + 1.5
    settle_t0 = time.time()
    while time.time() - settle_t0 < 2.0:
        time.sleep(0.05)
        pose = skill._latest_odom
        if pose is None:
            continue
        max_disp = max(
            max_disp,
            math.hypot(pose.position.x - settle_start["x"], pose.position.y - settle_start["y"]),
        )
        if time.time() >= half_t0:
            last_half_disp = math.hypot(
                pose.position.x - last_pose.position.x, pose.position.y - last_pose.position.y
            )
        last_pose = pose

    after = _pose_dict(last_pose)
    last_nonzero = tele.last_nonzero_time()
    latency = (last_nonzero - stop_signal["t"]) if last_nonzero else None
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "case": "urgent_stop_mid_drive",
        "requested": {"forward_m": 0.75, "stop_after_s": stop_after_s},
        "odom_before": before,
        "odom_after": after,
        "achieved": {
            "distance_m": round(math.hypot(after["x"] - before["x"], after["y"] - before["y"]), 4),
            "turn_deg": round(_angle_delta(after["yaw_deg"], before["yaw_deg"]), 2),
        },
        "duration_s": round(duration, 3),
        "pd_drive_returned": bool(done),
        "stop_signal_to_last_nonzero_command_s": (
            round(latency, 3) if latency is not None else None
        ),
        "settle_max_displacement_m_2s": round(max_disp, 4),
        "settle_residual_displacement_m_last_0.5s": round(last_half_disp, 4),
        "last_twist_zero": (tele.sent[-1][1] == Twist.zero()) if tele.sent else None,
        "pass": bool(
            latency is not None
            and latency < 2.0
            and not done
            and last_half_disp < 0.05  # at rest within the window
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("artifacts/vln_m0/basic_actions"),
        help="output directory (steps.jsonl + summary.json are written there)",
    )
    parser.add_argument("--settle", type=float, default=1.0, help="seconds to wait between cases")
    parser.add_argument("--kp-yaw", type=float, default=None, help="override kp_yaw")
    parser.add_argument("--kp-trans", type=float, default=None, help="override kp_trans")
    parser.add_argument("--max-w", type=float, default=None, help="override max_w")
    parser.add_argument("--max-v", type=float, default=None, help="override max_v")
    parser.add_argument(
        "--drive-hz",
        type=float,
        default=50.0,
        help="PD command rate. The MuJoCo Go2 tracks ~2-5x the commanded "
        "rate, so at the real-robot 10 Hz the per-cycle loop gain sits near 1 "
        "and turns relay-oscillate; 50 Hz is comfortably stable (this IS the "
        "sim stack's configured rate).",
    )
    args = parser.parse_args()

    overrides = {
        k: v
        for k, v in {
            "kp_yaw": args.kp_yaw,
            "kp_trans": args.kp_trans,
            "max_w": args.max_w,
            "max_v": args.max_v,
            "drive_hz": args.drive_hz,
        }.items()
        if v is not None
    }

    odom_transport: PubSubTransport[PoseStamped] = make_transport("odom", PoseStamped)
    tele_transport: PubSubTransport[Twist] = make_transport("tele_cmd_vel", Twist)
    with odom_transport, tele_transport:
        latest: dict[str, PoseStamped | None] = {"pose": None}
        odom_transport.subscribe(lambda pose: latest.__setitem__("pose", pose))

        t0 = time.time()
        while latest["pose"] is None:
            assert time.time() - t0 < 30.0, "no odom within 30 s — is the sim stack running?"
            time.sleep(0.1)
        print(f"odom live: {_pose_dict(latest['pose'])}", flush=True)

        skill = _LiveSkill(_LiveTele(tele_transport), latest, **overrides)
        records: list[dict[str, Any]] = []

        def run(record: dict[str, Any]) -> None:
            records.append(record)
            print(json.dumps(record), flush=True)
            time.sleep(args.settle)

        # -- the basic action set (VLN elementary + coalesced steps) --------
        run(_drive_goal(skill, "forward_0.25m", forward_m=0.25, degrees=0.0))
        run(_drive_goal(skill, "forward_0.50m", forward_m=0.50, degrees=0.0))
        run(_drive_goal(skill, "left_15deg", forward_m=0.0, degrees=15.0))
        run(_drive_goal(skill, "right_15deg", forward_m=0.0, degrees=-15.0))

        # A coalesced sequence as three consecutive goals (what _coalesce
        # leaves the executor): forward 25 cm, left 15 deg, forward 25 cm.
        seq_start = skill._latest_odom
        assert seq_start is not None
        before = _pose_dict(seq_start)
        skill.tele_cmd_vel.sent.clear()
        skill._running.set()
        t0 = time.time()
        seq_goals: list[dict[str, Any]] = []
        for i, (forward_m, degrees) in enumerate([(0.25, 0.0), (0.0, 15.0), (0.25, 0.0)], 1):
            pose = skill._latest_odom
            assert pose is not None
            goal_before = _pose_dict(pose)
            gt0 = time.time()
            done = _pd_drive(
                skill, _relative_goal(pose, forward_m, degrees), turn_only=forward_m == 0.0
            )
            pose = skill._latest_odom
            assert pose is not None
            goal_after = _pose_dict(pose)
            seq_goals.append(
                {
                    "goal": i,
                    "requested": {"forward_m": forward_m, "turn_deg": degrees},
                    "odom_before": goal_before,
                    "odom_after": goal_after,
                    "duration_s": round(time.time() - gt0, 3),
                    "pd_drive_returned": bool(done),
                }
            )
            print(json.dumps(seq_goals[-1]), flush=True)
        duration = time.time() - t0
        pose = skill._latest_odom
        assert pose is not None
        after = _pose_dict(pose)
        distance = math.hypot(after["x"] - before["x"], after["y"] - before["y"])
        turned = _angle_delta(after["yaw_deg"], before["yaw_deg"])
        all_done = all(g["pd_drive_returned"] for g in seq_goals)
        run(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "case": "sequence_fwd25_left15_fwd25",
                "requested": {"forward_m": 0.50, "turn_deg": 15.0},
                "odom_before": before,
                "odom_after": after,
                "achieved": {"distance_m": round(distance, 4), "turn_deg": round(turned, 2)},
                "duration_s": round(duration, 3),
                "goals": seq_goals,
                "pd_drive_returned": all_done,
                "twists_sent": len(skill.tele_cmd_vel.sent),
                "last_twist_zero": (skill.tele_cmd_vel.sent[-1][1] == Twist.zero())
                if skill.tele_cmd_vel.sent
                else None,
                "pass": bool(all_done),
            }
        )

        # -- urgent stop mid-drive: stop-latency evidence ---------------------
        # The sim robot covers 0.75 m in ~0.3 s (it tracks ~5x the commanded
        # rate), so the stop must come early enough to land mid-drive; the
        # case only passes if the drive was actually interrupted.
        run(_urgent_stop_case(skill, stop_after_s=0.12))

        # -- write the evidence ------------------------------------------------
        args.out.mkdir(parents=True, exist_ok=True)
        with (args.out / "steps.jsonl").open("w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        passed = sum(1 for r in records if r["pass"])
        summary = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "cases": len(records),
            "passed": passed,
            "failed": len(records) - passed,
            "controller": "active_vln.skill._pd_drive (PD velocity homing, deployment constants)",
            "pass": passed == len(records),
        }
        (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
