# DimOS workspace

DimOS (https://github.com/dimensionalOS/dimos) deployment workspace with
Vision-and-Language Navigation integrations. The `dimos/` directory is a
full checkout of upstream at 5e0ebfd with local changes; everything around
it is this machine's deployment tooling.

## VLN skills (the main local work)

Two instruction-following navigation policies, switchable per run:

| Blueprint | Policy | Paper |
|---|---|---|
| `unitree-go2-agentic-activevln-cockpit` | `active_vln` | ActiveVLN (ICRA 2026, multi-turn RL) — https://github.com/arvillion/ActiveVLN |
| same, `--vln-policy streamvln` | `stream_vln` | StreamVLN (ICRA 2026, SlowFast context) — https://github.com/InternRobotics/StreamVLN |

Both skills share the robot-side execution: the policies answer in discrete
VLN-CE actions (forward 25/50/75 cm, turns 15/30/45 deg, stop) which are
coalesced and driven with a PD velocity homing controller (the official
real-world deployment's `pid_controller.py`) on `tele_cmd_vel` — the A*
planner's arrival tolerances (0.2 m / 15 deg) swallow the VLN step sizes, so
planner goals complete before the robot moves. Turns drive the yaw loop
alone (quadrupeds drift positionally when turning in place); a drive that
commands speed without making progress gives up in seconds and the policy's
next observation re-decides (reactive obstacle handling).

Switch policies with `--vln-policy activevln|streamvln` (or `.env`'s
`VLN_POLICY`); both servers can run side by side (one GPU each).

## Local changes to upstream dimos (all uncommitted-to-upstream)

- `dimos/navigation/experimental/active_vln/`, `.../stream_vln/` — the skills
- `dimos/robot/unitree/go2/blueprints/agentic/unitree_go2_agentic_activevln*.py` — blueprints (incl. cockpit variant)
- `dimos/agents/agent.py` — urgent-stop chat interrupt (short "stop" messages
  bypass the agent loop and call the stop skills directly; stop tools preempt
  capability holders instead of deadlocking on them)
- `dimos/agents/mcp/mcp_server.py` — stop-tool capability preemption
- `dimos/core/global_config.py` — `hf_endpoint` (HF mirror),
  `ollama_base_url` (remote Ollama), `vln_policy` (policy switch) fields
- `dimos/agents/ollama_agent.py`, `vlm_agent.py` — remote Ollama routing
- `dimos/simulation/mujoco/mujoco_process.py` — headless (no display) MuJoCo
- `web/relay/server.ts`, `dimos/web/relay_bridge/relay_bridge_module.py` —
  relay advertises its leaf cert hash in /api/info so browsers accept a
  private-CA relay for WebTransport (Chrome/Edge only accept public CAs or
  ≤14-day hash-pinned certs for QUIC; see certs/README.md)

## Deployment scripts (this directory)

| Script | What it starts |
|---|---|
| `run_go2_sim.sh` | MuJoCo Go2 simulation, Rerun web viewer |
| `run_go2_agent_ollama.sh` | agentic sim with the local/remote Ollama agent brain |
| `run_go2_cockpit_lan.sh` | full cockpit UI over LAN (TLS relay :7780) |
| `run_go2_activevln.sh` | ActiveVLN policy server (vllm) + agentic stack |
| `run_go2_streamvln.sh` | StreamVLN policy server + agentic stack (`--vln-policy streamvln`) |

One-time setup per script is documented in each script's header (model
downloads, certs, firewall). `scripts/parallel_fetch_lfs.py` pulls large LFS
objects through this network's slow links (chunked, sha256-verified).

## certs/

TLS material for the LAN cockpit relay (see `run_go2_cockpit_lan.sh`).
Private keys, CSRs and the relay auth file are NOT committed — regenerate
them with the openssl commands in the script header. The CA certificate
(`dimos-relay-ca.pem`) is committed: install it into client browsers' trust
store for the HTTPS page (the QUIC data path pins the leaf hash, so it does
not need the CA).

## Machine notes

Ubuntu 22.04, 2x RTX 4090, headless (VS Code remote). Everything assumes:
GPU 0 = simulation + StreamVLN server, GPU 1 = ActiveVLN vllm. Network
quirks and mirror choices (Tencent PyPI mirror, hf-mirror.com, parallel
chunked GitHub downloads) are baked into the scripts.
