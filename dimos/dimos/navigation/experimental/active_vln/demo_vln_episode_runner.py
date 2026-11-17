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

"""M0-04/M0-05 episode runner: run one REAL closed-loop VLN episode through
the running DimOS stack's MCP server and record the full execution evidence.

One episode:
  1. subscribe tele_cmd_vel / cmd_vel / odom (timestamps + values), tail the
     DimOS run log for the skill's own lines ("server actions" per policy
     turn; the "SKILL <name> duration_ms" line that marks the episode end),
     and note the StreamVLN server log's byte offset;
  2. call the MCP tool (stream_vln / active_vln) with the instruction — the
     real path: MCP -> CapabilityRegistry -> skill worker -> policy server
     (instruction + reset + BGR JPEG) -> parsed actions -> PD homing on
     tele_cmd_vel -> MovementManager -> MuJoCo -> odom -> next inference;
  3. optionally interrupt mid-episode with a stop (the real user paths:
     `dimos agent-send` urgent-stop input, or the paired MCP stop tool) and
     measure the stop latency (stop request -> last non-zero commanded
     velocity on tele_cmd_vel / cmd_vel);
  4. read the StreamVLN server log lines appended during the episode — the
     evidence that the task's instruction reached the server;
  5. write steps.jsonl (recorded events) + episode.json (episode summary)
     into --out.

The termination reason is inferred from the recorded evidence (stop events;
the last policy turn's actions containing 0; the SKILL duration line; the
MCP tool output when the CLI returns it) — the skill's result string itself
is not logged.

Run (needs the sim stack + the matching policy server running — see
run_go2_streamvln.sh / run_go2_activevln.sh):

    cd dimos
    .venv/bin/python -m dimos.navigation.experimental.active_vln.demo_vln_episode_runner \
        --policy streamvln --instruction "walk forward and stop by the desk" \
        --out ../artifacts/vln_m0/streamvln_runs/ep1
    # with a mid-episode urgent stop (latency evidence):
    ... --stop-after 6 --stop-mode agent   # `dimos agent-send` urgent stop
    ... --stop-after 6 --stop-mode mcp     # paired stop tool
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import glob
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

from dimos.core.transport import PubSubTransport
from dimos.core.transport_factory import make_transport
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Twist import Twist

STREAMVLN_SERVER_LOG = Path("/tmp/streamvln_server.log")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _yaw_deg(pose: PoseStamped) -> float:
    return math.degrees(pose.orientation.to_euler().yaw)


def _pose_dict(pose: PoseStamped) -> dict[str, float]:
    return {"x": pose.position.x, "y": pose.position.y, "yaw_deg": _yaw_deg(pose)}


def _run_log_path() -> Path:
    """The main.jsonl of the most recently active DimOS run log."""
    candidates = sorted(glob.glob("logs/*/main.jsonl"), key=os.path.getmtime) + sorted(
        glob.glob(os.path.expanduser("~/.local/state/dimos/logs/*/main.jsonl")),
        key=os.path.getmtime,
    )
    if not candidates:
        raise SystemExit("no DimOS run log found — is the stack running?")
    return Path(candidates[-1])


class _Recorder:
    """Topic + log recorder for one episode."""

    def __init__(self) -> None:
        self.tele: list[dict[str, Any]] = []
        self.cmd: list[dict[str, Any]] = []
        self.odom: list[dict[str, Any]] = []
        self.turns: list[dict[str, Any]] = []
        self.stop_events: list[dict[str, Any]] = []
        self.stop_delivery: dict[str, Any] | None = None
        self.skill_line: dict[str, Any] | None = None
        self._log_offset = 0

    def on_tele(self, twist: Twist) -> None:
        self.tele.append({"t": time.time(), "v": twist.linear.x, "w": twist.angular.z})

    def on_cmd(self, twist: Twist) -> None:
        self.cmd.append({"t": time.time(), "v": twist.linear.x, "w": twist.angular.z})

    def on_odom(self, pose: PoseStamped) -> None:
        self.odom.append({"t": time.time(), **_pose_dict(pose)})

    def poll_log(self, log_path: Path) -> None:
        """Tail the run log: the skill's per-turn server actions and the
        SKILL duration line that marks the episode end.
        """
        try:
            with log_path.open("r") as f:
                f.seek(self._log_offset)
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event = str(rec.get("event", ""))
                    if "server actions" in event:
                        self.turns.append(
                            {
                                "t": time.time(),
                                "log_ts": rec.get("timestamp"),
                                "server_actions": event.split("server actions", 1)[1].strip(),
                            }
                        )
                    elif "ActiveVLN turn" in event:
                        # "ActiveVLN turn 3: turn left 15 degrees, ..."
                        self.turns.append(
                            {
                                "t": time.time(),
                                "log_ts": rec.get("timestamp"),
                                "answer": event.split("ActiveVLN turn", 1)[1].strip()[:300],
                            }
                        )
                    elif event.startswith("SKILL ") and "duration_ms" in event:
                        self.skill_line = {"t": time.time(), "event": event}
                    elif "Urgent stop from human input" in event:
                        self.stop_delivery = {"t": time.time(), "log_ts": rec.get("timestamp")}
                self._log_offset = f.tell()
        except FileNotFoundError:
            pass

    @staticmethod
    def last_nonzero_time(trace: list[dict[str, Any]]) -> float | None:
        for rec in reversed(trace):
            if rec.get("v", 0.0) != 0.0 or rec.get("w", 0.0) != 0.0:
                return rec["t"]
        return None


def _mcp_call(tool: str, args: dict[str, Any], timeout: float) -> str:
    """`dimos mcp call <tool> --arg k=v` (the documented CLI path)."""
    cmd = [".venv/bin/dimos", "mcp", "call", tool, "--timeout", str(int(timeout))]
    for k, v in args.items():
        cmd += ["--arg", f"{k}={v}"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 60.0)
    return (out.stdout + out.stderr).strip()


def _termination_tag(
    skill_line: dict[str, Any] | None,
    turns: list[dict[str, Any]],
    stop_events: list[dict[str, Any]],
    mcp_output: str | None,
) -> str:
    """The termination reason, inferred from recorded evidence."""
    if stop_events:
        return "STOPPED_BY_USER"
    if skill_line is None:
        return "NO_SKILL_COMPLETION_LINE"
    last_turn = turns[-1] if turns else {}
    if (
        "0" in (last_turn.get("server_actions") or "")
        or "stop" in (last_turn.get("answer") or "").lower()
    ):
        return "POLICY_SAID_STOP"
    # ActiveVLN answers carry the turn number ("50: move forward ...");
    # hitting the configured budget (max_turns=50) is its own reason.
    last_answer = (last_turn.get("answer") or "").split(":", 1)[0]
    if last_answer.strip().isdigit() and int(last_answer) >= 50:
        return "TURN_BUDGET_SPENT"
    if mcp_output and "stop" in mcp_output.lower():
        return "COMPLETED"
    return "COMPLETED_UNVERIFIED"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", choices=["streamvln", "activevln"], required=True)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--episode-id", default=None)
    parser.add_argument(
        "--stop-after",
        type=float,
        default=None,
        help="seconds after episode start to issue the stop (latency evidence)",
    )
    parser.add_argument(
        "--stop-mode",
        choices=["agent", "mcp"],
        default="agent",
        help="agent: `dimos agent-send` urgent-stop input (the real user path); "
        "mcp: the paired stop tool",
    )
    parser.add_argument("--tool-timeout", type=float, default=300.0)
    parser.add_argument("--settle", type=float, default=3.0, help="post-episode settle window")
    args = parser.parse_args()

    tool = "stream_vln" if args.policy == "streamvln" else "active_vln"
    stop_tool = "stop_stream_vln" if args.policy == "streamvln" else "stop_active_vln"
    episode_id = args.episode_id or f"{args.policy}_{datetime.now().strftime('%H%M%S')}"

    log_path = _run_log_path()
    print(f"run log: {log_path}")

    tele_t: PubSubTransport[Twist] = make_transport("tele_cmd_vel", Twist)
    cmd_t: PubSubTransport[Twist] = make_transport("cmd_vel", Twist)
    odom_t: PubSubTransport[PoseStamped] = make_transport("odom", PoseStamped)
    rec = _Recorder()
    with tele_t, cmd_t, odom_t:
        tele_t.subscribe(rec.on_tele)
        cmd_t.subscribe(rec.on_cmd)
        odom_t.subscribe(rec.on_odom)

        t0 = time.time()
        while not rec.odom:
            assert time.time() - t0 < 30.0, "no odom — is the sim stack running?"
            time.sleep(0.1)
        pose_before = dict(rec.odom[-1])

        server_log_offset = (
            STREAMVLN_SERVER_LOG.stat().st_size if STREAMVLN_SERVER_LOG.exists() else 0
        )
        rec.poll_log(log_path)  # baseline: consume old lines WITHOUT recording
        rec.turns.clear()
        rec.skill_line = None
        rec.stop_delivery = None

        episode_start = time.time()
        started = _utc()
        mcp_out: list[str] = []
        stop_requested_at: float | None = None

        def call_tool() -> None:
            mcp_out.append(_mcp_call(tool, {"instruction": args.instruction}, args.tool_timeout))

        thread = threading.Thread(target=call_tool, daemon=True)
        thread.start()

        # Wait for the SKILL completion line (the episode end), issuing the
        # optional mid-episode stop along the way. The wait outlives the
        # CLI's own tool timeout (a long episode's skill call can outlast
        # the CLI's default HTTP timeout; the CLI result is then absent but
        # the log's SKILL line still marks the true end).
        hard_deadline = episode_start + args.tool_timeout + 120.0
        while rec.skill_line is None and time.time() < hard_deadline:
            rec.poll_log(log_path)
            if (
                args.stop_after is not None
                and stop_requested_at is None
                and time.time() - episode_start >= args.stop_after
            ):
                stop_requested_at = time.time()
                if args.stop_mode == "agent":
                    out = subprocess.run(
                        [".venv/bin/dimos", "agent-send", "停止"],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    rec.stop_events.append(
                        {
                            "t": stop_requested_at,
                            "mode": "agent_send_urgent_stop",
                            "payload": "停止",
                            "output": (out.stdout + out.stderr).strip()[:200],
                        }
                    )
                else:
                    _mcp_call(stop_tool, {}, 30.0)
                    rec.stop_events.append({"t": stop_requested_at, "mode": f"mcp_{stop_tool}"})
            time.sleep(0.05)
        # The CLI result is best-effort: the skill's true end marker is the
        # log's SKILL completion line. The CLI thread is a daemon — a hung
        # CLI (its HTTP patience is shorter than a long episode) must not
        # block recording the episode.
        thread.join(timeout=10.0)

        # Settle window: stop latency / at-rest evidence.
        settle_until = time.time() + args.settle
        while time.time() < settle_until:
            rec.poll_log(log_path)
            time.sleep(0.05)
        episode_end = time.time()

    # The StreamVLN server log lines appended during the episode: the
    # evidence that this task's instruction reached the policy server.
    server_log_lines: list[str] = []
    if STREAMVLN_SERVER_LOG.exists():
        with STREAMVLN_SERVER_LOG.open("r", errors="replace") as f:
            f.seek(server_log_offset)
            for line in f:
                if "instruction:" in line or "reset" in line:
                    server_log_lines.append(line.strip()[-300:])

    pose_after = dict(rec.odom[-1]) if rec.odom else None
    distance = (
        math.hypot(pose_after["x"] - pose_before["x"], pose_after["y"] - pose_before["y"])
        if pose_after
        else None
    )
    last_nonzero_tele = _Recorder.last_nonzero_time(rec.tele)
    last_nonzero_cmd = _Recorder.last_nonzero_time(rec.cmd)
    stop_latency_tele = (
        round(last_nonzero_tele - stop_requested_at, 3)
        if stop_requested_at and last_nonzero_tele
        else None
    )
    stop_latency_cmd = (
        round(last_nonzero_cmd - stop_requested_at, 3)
        if stop_requested_at and last_nonzero_cmd
        else None
    )
    tag = _termination_tag(
        rec.skill_line, rec.turns, rec.stop_events, mcp_out[0] if mcp_out else None
    )

    steps: list[dict[str, Any]] = []
    steps += [{"kind": "policy_turn", **turn} for turn in rec.turns]
    steps += [{"kind": "stop_request", **ev} for ev in rec.stop_events]
    if rec.skill_line:
        steps.append({"kind": "skill_completion", **rec.skill_line})
    # Raw traces (per-sample): the full velocity/odom evidence.
    steps.append({"kind": "trace_tele_cmd_vel", "samples": rec.tele})
    steps.append({"kind": "trace_cmd_vel", "samples": rec.cmd})
    odom_step = max(1, len(rec.odom) // 500)
    steps.append({"kind": "trace_odom", "samples": rec.odom[::odom_step]})
    steps.append(
        {
            "kind": "velocity_trace",
            "n_tele_cmd_vel": len(rec.tele),
            "n_cmd_vel": len(rec.cmd),
            "last_nonzero_tele_cmd_vel_t": last_nonzero_tele,
            "last_nonzero_cmd_vel_t": last_nonzero_cmd,
        }
    )
    steps.append({"kind": "odom_summary", "first": pose_before, "last": pose_after})

    episode: dict[str, Any] = {
        "episode_id": episode_id,
        "policy": args.policy,
        "instruction": args.instruction,
        "started": started,
        "ended": _utc(),
        "duration_s": round(episode_end - episode_start, 3),
        "mcp_tool_output": (mcp_out[0][:500] if mcp_out else None),
        "skill_completion_line": rec.skill_line,
        "termination_reason": tag,
        "policy_turns": len(rec.turns),
        "turn_details": [t.get("server_actions") or t.get("answer") for t in rec.turns],
        "server_log_lines": server_log_lines,
        "instruction_reached_server": (
            any(args.instruction[:20] in line for line in server_log_lines)
            if args.policy == "streamvln"
            else None
        ),
        "odom_before": pose_before,
        "odom_after": pose_after,
        "distance_m": round(distance, 4) if distance is not None else None,
        "stop": (
            {
                "requested_at_cli_launch": stop_requested_at,
                "delivery": rec.stop_delivery,
                "mode": rec.stop_events[0]["mode"] if rec.stop_events else None,
                # CLI-launch -> last non-zero command (includes the agent-send
                # subprocess startup, ~1.3 s); the SYSTEM latency is
                # delivery -> last non-zero command (below).
                "cli_launch_to_last_nonzero_tele_cmd_vel_s": stop_latency_tele,
                "cli_launch_to_last_nonzero_cmd_vel_s": stop_latency_cmd,
                "delivery_to_last_nonzero_tele_cmd_vel_s": (
                    round(last_nonzero_tele - rec.stop_delivery["t"], 3)
                    if rec.stop_delivery and last_nonzero_tele
                    else None
                ),
                "target_lt_2s": (stop_latency_tele is not None and stop_latency_tele < 2.0),
            }
            if stop_requested_at
            else None
        ),
    }

    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "steps.jsonl").open("w") as f:
        for s in steps:
            f.write(json.dumps(s) + "\n")
    (args.out / "episode.json").write_text(json.dumps(episode, indent=2) + "\n")
    print(json.dumps(episode, indent=2))


if __name__ == "__main__":
    main()
