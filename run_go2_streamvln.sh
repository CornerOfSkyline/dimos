#!/usr/bin/env bash
# Launch the StreamVLN real-world policy server + the dimos agentic stack
# with the stream_vln skill.
#
# StreamVLN (https://github.com/InternRobotics/StreamVLN, ICRA 2026) serves
# its SlowFast policy over the official real-world HTTP server
# (streamvln/http_realworld_server.py): POST a BGR JPEG frame + the
# instruction + a reset flag -> discrete action sequence (0 stop / 1 forward
# 25cm / 2 left 15deg / 3 right 15deg; up to 4 per answer). This workspace
# runs the PATCHED deployment copy (streamvln-deploy/http_realworld_server.py
# — instruction passthrough; see streamvln-deploy/README.md and the .patch).
# The server keeps the streaming context (memory tokens, every-4-steps
# inference) internally; dimos's stream_vln skill is a plain HTTP client with
# the same odom-verified execution as active_vln.
#
# One-time setup (NOT handled by this script):
#   - checkpoint: HF_ENDPOINT=https://hf-mirror.com .venv/bin/python -c "
#       from huggingface_hub import snapshot_download as s
#       s('mengwei0427/StreamVLN_Video_qwen_1_5_r2r_rxr_envdrop_scalevln_v1_3')"
#   - StreamVLN code at /tmp/StreamVLN (the server imports streamvln/, model/)
set -euo pipefail

WS="$HOME/workspace/dimos_ws"
REPO="$WS/dimos"
SV="$WS/streamvln-serve"
SVLN_REPO="${SVLN_REPO:-/tmp/StreamVLN}"
MODEL="${SVLN_MODEL:-$WS/streamvln-model}"
PORT="${SVLN_PORT:-5801}"
GPU="${SVLN_GPU:-0}"  # GPU 0 (with the sim); GPU 1 holds the ActiveVLN vllm

cd "$REPO"

# 1. resolve the checkpoint (HF cache snapshot or the symlink)
if [ ! -e "$MODEL" ]; then
    snap=$(ls -d ~/.cache/huggingface/hub/models--mengwei0427--StreamVLN_Video_qwen_1_5_r2r_rxr_envdrop_scalevln_v1_3/snapshots/*/ 2>/dev/null | head -1)
    [ -n "$snap" ] || { echo "checkpoint not found; download it first (see header)"; exit 1; }
    MODEL=$(readlink -f "$snap")
fi

# 2. deps: server needs torch+transformers+flask+the repo's model code.
#    torch already installed in the venv by setup; the rest are light.
if ! "$SV/bin/python" -c "import transformers, flask" >/dev/null 2>&1; then
    UV_DEFAULT_INDEX=https://mirrors.cloud.tencent.com/pypi/simple \
    "$HOME/.local/bin/uv" pip install --python "$SV/bin/python" --no-config \
        "transformers==4.40.0" "accelerate==0.28.0" "flask" "einops==0.6.1" \
        "einops-exts==0.0.4" "timm==1.0.15" "sentencepiece" "protobuf" \
        "av==14.4.0" "deepspeed==0.14.4" --index-strategy unsafe-best-match \
        2>&1 | tail -2
fi

# 3. the server (idempotent) — the patched deployment copy in this workspace
#    (instruction passthrough; streamvln-deploy/README.md). NOTE: the server
#    hardcodes port 5801 in app.run; --port is not a real flag of it.
if ! ss -tln 2>/dev/null | grep -q ":$PORT "; then
    cd "$SVLN_REPO/streamvln"   # CWD: the server writes runs*/ annotated frames here
    STREAMVLN_CODE_ROOT="$SVLN_REPO" HF_HUB_OFFLINE=1 HF_HOME="$HOME/.cache/huggingface" CUDA_VISIBLE_DEVICES=$GPU \
    nohup "$SV/bin/python" "$WS/streamvln-deploy/http_realworld_server.py" \
        --model_path "$MODEL" \
        --device "cuda:0" \
        > /tmp/streamvln_server.log 2>&1 &
    echo "==> StreamVLN server (patched copy) starting on GPU $GPU, :$PORT (model load takes a while)"
    cd "$REPO"
fi
# NOTE: the official server hardcodes 0.0.0.0:5801; SVLN_PORT other than 5801
# needs a patched server.

# 4. ollama (agent brain): remote server set in OLLAMA_BASE_URL, else local
if [ -z "${OLLAMA_BASE_URL:-}" ]; then
    grep -q "^OLLAMA_BASE_URL=..*" "$REPO/.env" 2>/dev/null \
        || curl -s --max-time 3 http://localhost:11434/api/version >/dev/null 2>&1 \
        || { nohup ollama serve > /tmp/ollama_serve.log 2>&1 & sleep 3; }
fi

# 5. the dimos stack with the stream_vln skill (--vln-policy streamvln)
"$REPO/.venv/bin/dimos" stop >/dev/null 2>&1 || true
sleep 2
export MUJOCO_GL=egl
export LD_LIBRARY_PATH="$WS/libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

SYSTEM_PROMPT="${DIMOS_SYSTEM_PROMPT:-You are a Unitree Go2 robot assistant in a web cockpit. The user chats with you in a chat panel: always reply with plain text in your message content directly. NEVER use the speak skill - it has no TTS configured and always fails. Keep replies concise (1-3 sentences). Skills: stream_vln (instruction-following routes by camera, e.g. 'walk past the desk and stop by the sofa'), begin_exploration, end_exploration, navigate_with_text, stop_navigation, move_to, observe, look_out_for, stop_looking_out, follow_person, stop_following, tag_location, start_patrol, stop_patrol, get_battery_soc, wait, current_time. Prefer stream_vln for routes described in natural language; navigate_with_text for known places or objects.}"

CERTS="$WS/certs"
LAN_IP="${LAN_IP:-172.23.53.132}"
ROBOT_KEY=$(python3 -c "import json;print(json.load(open('$CERTS/relay-auth.json'))['robots']['zjf-4090'])")
if ! ss -tln 2>/dev/null | grep -q ":7780 "; then
    nohup "$HOME/.cache/dimos/deno/v2.9.8/deno" run --v8-flags=--expose-gc --frozen \
        --node-modules-dir=none --allow-read="$REPO/web,$CERTS" --allow-net \
        --config "$REPO/web/deno.json" "$REPO/web/relay/main.ts" \
        --host 0.0.0.0 --port 7780 \
        --cert "$CERTS/dimos-relay-fullchain.pem" --key "$CERTS/dimos-relay-ec.key" \
        --auth-file "$CERTS/relay-auth.json" \
        --cockpit-dir "$REPO/web/cockpit/dist" --sdk-dir "$REPO/web/sdk/dist" \
        >> /tmp/relay_tls.log 2>&1 &
    sleep 4
fi

echo "==> cockpit: https://$LAN_IP:7780 | stream_vln skill -> http://localhost:$PORT/eval_vln"
# drive-hz 50: the MuJoCo Go2 tracks ~2-5x the commanded rate, so the
# real-robot 10 Hz PD rate sits at a per-cycle loop gain near 1 and turns
# relay-oscillate (30 s timeouts measured); 50 Hz is comfortably stable
# (all 6 basic-motion cases pass — artifacts/vln_m0/basic_actions).
exec "$REPO/.venv/bin/dimos" --simulation run unitree-go2-agentic-activevln-cockpit \
    --vln-policy streamvln \
    --agent.model "${OLLAMA_MODEL:-ollama:qwen3-vl:32b}" \
    --agent.system-prompt "$SYSTEM_PROMPT" \
    --streamvlnskill.server-url "http://localhost:$PORT/eval_vln" \
    --streamvlnskill.drive-hz 50 \
    --listen-host 0.0.0.0 \
    --relay-url "https://$LAN_IP:7780" \
    --dynamicrelaybridge_940788bb41.relay-ca "$CERTS/dimos-relay-ca.pem" \
    --dynamicrelaybridge_940788bb41.relay-key "$ROBOT_KEY" \
    "$@"
