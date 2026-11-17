#!/usr/bin/env bash
# Launch the DimOS Unitree Go2 agentic sim with a LOCAL Ollama LLM.
# Usage: ~/workspace/dimos_ws/run_go2_agent_ollama.sh [--daemon] [extra dimos flags...]
#
# Requirements this script handles:
#   - ollama serve running (starts it if not)
#   - a vision model with tool-calling; default qwen3-vl:32b (already local, fits a 4090)
#   - MUJOCO_GL=egl + LD_LIBRARY_PATH for the headless box
set -euo pipefail

REPO="$HOME/workspace/dimos_ws/dimos"
LIBS="$HOME/workspace/dimos_ws/libs"
MODEL="${OLLAMA_MODEL:-ollama:qwen3-vl:32b}"   # other local options: ollama:qwen3-vl:2b

cd "$REPO"

# 1. ollama serve (start if not responding)
if ! curl -s --max-time 3 http://localhost:11434/api/version >/dev/null 2>&1; then
    echo "==> starting ollama serve"
    nohup ollama serve > /tmp/ollama_serve.log 2>&1 &
    sleep 3
    curl -s --max-time 5 http://localhost:11434/api/version >/dev/null
fi

# 2. model must exist locally (do NOT let dimos auto-pull a 6GB default)
model_name="${MODEL#ollama:}"
if ! ollama list | awk '{print $1}' | grep -qx "$model_name"; then
    echo "ERROR: ollama model '$model_name' not found locally." >&2
    echo "  ollama list shows: $(ollama list | tail -n +2 | awk '{print $1}' | tr '\n' ' ')" >&2
    echo "  pick one and re-run with OLLAMA_MODEL=ollama:<name> $0" >&2
    exit 1
fi

export MUJOCO_GL=egl
export LD_LIBRARY_PATH="$LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

echo "==> Go2 agentic sim (ollama: $model_name); LAN interfaces:"
echo "    chat:  http://$(hostname -I | awk '{print $1}'):5555"
echo "    rerun: http://$(hostname -I | awk '{print $1}'):9878/?url=rerun%2Bhttp%3A%2F%2F$(hostname -I | awk '{print $1}')%3A9877%2Fproxy"
echo "==> talk to it:  .venv/bin/dimos agent-send \"你的指令\"   (watch: .venv/bin/dimos log -f)"
exec "$REPO/.venv/bin/dimos" --simulation run unitree-go2-agentic-ollama \
    --agent.model "$MODEL" \
    --listen-host "${LISTEN_HOST:-0.0.0.0}" \
    --rerunbridgemodule.rerun-open web "$@"
