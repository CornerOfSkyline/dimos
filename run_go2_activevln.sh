#!/usr/bin/env bash
# Launch the Go2 agentic sim with the ActiveVLN navigation skill.
#
# Serves the ActiveVLN RL fine-tuned checkpoint (Qwen2.5-VL-3B_rl_r2r_4000,
# the multi-turn RL policy from https://github.com/arvillion/ActiveVLN) with
# vllm, then starts the dimos agentic stack with the `active_vln` skill wired
# to it. The agent gains a new tool: active_vln(instruction) — the RL policy
# drives the route; navigate_with_text and every other skill keep working.
#
# One-time: the model must be downloaded first (HF mirror):
#   HF_ENDPOINT=https://hf-mirror.com .venv/bin/python -c "
#     from huggingface_hub import snapshot_download as s; s('Arvil/Qwen2.5-VL-3B_rl_r2r_4000')"
set -euo pipefail

WS="$HOME/workspace/dimos_ws"
REPO="$WS/dimos"
SERVE_VENV="$WS/activevln-serve"
PORT="${ACTIVEVLN_PORT:-8003}"
MODEL="${ACTIVEVLN_MODEL:-$WS/activevln-model}"

cd "$REPO"

# 1. resolve the model snapshot path
if [ ! -e "$MODEL" ]; then
    snap=$(ls -d ~/.cache/huggingface/hub/models--Arvil--Qwen2.5-VL-3B_rl_r2r_4000/snapshots/*/ 2>/dev/null | head -1)
    [ -n "$snap" ] || { echo "model not found; download it first (see header)"; exit 1; }
    MODEL=$(readlink -f "$snap")
fi
echo "==> ActiveVLN model: $MODEL"

# 2. vllm serve the checkpoint (idempotent). GPU 1 keeps it off the sim's
#    GPU; the native sampler avoids flashinfer's nvcc JIT (this box has no
#    system CUDA toolkit), enforce-eager for the same reason.
if ! curl -s --max-time 3 "http://localhost:$PORT/v1/models" >/dev/null 2>&1; then
    NV="$SERVE_VENV/lib/python3.12/site-packages/nvidia/cu13"
    VLLM_USE_FLASHINFER_SAMPLER=0 CUDA_VISIBLE_DEVICES=${ACTIVEVLN_GPU:-1} \
    CUDA_HOME=$NV PATH=$NV/bin:$SERVE_VENV/bin:$PATH \
    nohup "$SERVE_VENV/bin/python" -m vllm.entrypoints.openai.api_server \
        --model "$MODEL" \
        --served-model-name activevln \
        --trust-remote-code \
        --limit-mm-per-prompt '{"image": 200, "video": 0}' \
        --mm-processor-kwargs '{"max_pixels": 80000}' \
        --max-model-len 32768 \
        --enable-prefix-caching \
        --enforce-eager \
        --gpu-memory-utilization 0.85 \
        --port "$PORT" \
        > /tmp/activevln_vllm.log 2>&1 &
    echo "==> vllm starting on GPU ${ACTIVEVLN_GPU:-1}, :$PORT (model load takes a few minutes)"
fi

# 3. wait for the endpoint (the skill fails fast with a clear error otherwise)
for i in $(seq 1 120); do
    curl -s --max-time 2 "http://localhost:$PORT/v1/models" >/dev/null 2>&1 && break
    sleep 5
done
curl -s --max-time 2 "http://localhost:$PORT/v1/models" >/dev/null \
    || { echo "vllm did not come up:"; tail -20 /tmp/activevln_vllm.log; exit 1; }
echo "==> ActiveVLN endpoint ready: http://localhost:$PORT/v1 (model name: activevln)"

# 4. ollama (the agent's own brain): a remote server set in OLLAMA_BASE_URL
#    (or .env's OLLAMA_BASE_URL) is used as-is; only a local setup starts one.
if [ -z "${OLLAMA_BASE_URL:-}" ]; then
    grep -q "^OLLAMA_BASE_URL=..*" "$REPO/.env" 2>/dev/null \
        || curl -s --max-time 3 http://localhost:11434/api/version >/dev/null 2>&1 \
        || { nohup ollama serve > /tmp/ollama_serve.log 2>&1 & sleep 3; }
fi

# 5. restart the dimos stack with the skill wired to the endpoint.
#    WITH_COCKPIT=1 serves the full cockpit UI over the LAN TLS relay
#    (same panels as unitree-go2-agentic-cockpit) instead of the Rerun viewer.
"$REPO/.venv/bin/dimos" stop >/dev/null 2>&1 || true
sleep 2
export MUJOCO_GL=egl
export LD_LIBRARY_PATH="$WS/libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# Text-first agent prompt: the default Go2 SYSTEM_PROMPT routes every reply
# through the `speak` skill (TTS, needs OPENAI_API_KEY) which fails in a loop.
SYSTEM_PROMPT="${DIMOS_SYSTEM_PROMPT:-You are a Unitree Go2 robot assistant in a web cockpit. The user chats with you in a chat panel: always reply with plain text in your message content directly. NEVER use the speak skill - it has no TTS configured and always fails. Keep replies concise (1-3 sentences). Skills: active_vln (instruction-following routes by camera, e.g. 'walk past the desk and stop by the sofa'), begin_exploration, end_exploration, navigate_with_text, stop_navigation, move_to, observe, look_out_for, stop_looking_out, follow_person, stop_following, tag_location, start_patrol, stop_patrol, get_battery_soc, wait, current_time. Prefer active_vln for routes described in natural language; navigate_with_text for known places or objects.}"

BLUEPRINT=unitree-go2-agentic-vln
if [ "${WITH_COCKPIT:-0}" = "1" ]; then
    BLUEPRINT=unitree-go2-agentic-activevln-cockpit
    CERTS="$WS/certs"
    LAN_IP="${LAN_IP:-172.23.53.132}"
    ROBOT_KEY=$(python3 -c "import json;print(json.load(open('$CERTS/relay-auth.json'))['robots']['zjf-4090'])")
    # the standalone TLS relay from the cockpit setup, reused as-is
    if ! ss -tln 2>/dev/null | grep -q ":7780 "; then
        NV="$REPO/web"
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
    echo "==> cockpit: https://$LAN_IP:7780 (viewer token in $CERTS/relay-auth.json)"
    # drive-hz 50: the MuJoCo Go2 tracks ~2-5x the commanded rate, so the
    # real-robot 10 Hz PD rate sits at a per-cycle loop gain near 1 and turns
    # relay-oscillate (30 s timeouts measured); 50 Hz is comfortably stable
    # (all 6 basic-motion cases pass — artifacts/vln_m0/basic_actions).
    exec "$REPO/.venv/bin/dimos" --simulation run "$BLUEPRINT" \
        --agent.model "${OLLAMA_MODEL:-ollama:qwen3-vl:32b}" \
        --agent.system-prompt "$SYSTEM_PROMPT" \
        --activevlnskill.base-url "http://localhost:$PORT/v1" \
        --activevlnskill.model activevln \
        --activevlnskill.drive-hz 50 \
        --listen-host 0.0.0.0 \
        --relay-url "https://$LAN_IP:7780" \
        --dynamicrelaybridge_940788bb41.relay-ca "$CERTS/dimos-relay-ca.pem" \
        --dynamicrelaybridge_940788bb41.relay-key "$ROBOT_KEY" \
        "$@"
fi

echo "==> run 'active_vln' through the agent chat, or directly:"
echo "    .venv/bin/dimos mcp call active_vln --arg instruction=\"walk to the sofa and stop\""
exec "$REPO/.venv/bin/dimos" --simulation run "$BLUEPRINT" \
    --agent.model "${OLLAMA_MODEL:-ollama:qwen3-vl:32b}" \
    --agent.system-prompt "$SYSTEM_PROMPT" \
    --activevlnskill.base-url "http://localhost:$PORT/v1" \
    --activevlnskill.model activevln \
    --activevlnskill.drive-hz 50 \
    --rerunbridgemodule.rerun-open web \
    --listen-host "${LISTEN_HOST:-127.0.0.1}" \
    "$@"
