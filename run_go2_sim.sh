#!/usr/bin/env bash
# Launch the DimOS Unitree Go2 MuJoCo simulation (headless-safe).
# Usage: ~/workspace/dimos_ws/run_go2_sim.sh [--daemon] [--extra dimos-flags...]
#
# Environment notes for this machine:
#   MUJOCO_GL=egl       — offscreen GPU rendering (no display available)
#   LD_LIBRARY_PATH     — libturbojpeg borrowed from the vlfm conda env
#   rerun-open web      — Rerun viewer at http://localhost:9878 (add ?url=... param)
set -euo pipefail

REPO="$HOME/workspace/dimos_ws/dimos"
LIBS="$HOME/workspace/dimos_ws/libs"

cd "$REPO"
export MUJOCO_GL=egl
export LD_LIBRARY_PATH="$LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# Optional: start exploration automatically a few seconds after boot.
# Pass NO_EXPLORE=1 to skip.
if [[ "${NO_EXPLORE:-0}" != "1" ]]; then
    ( sleep 12 && "$REPO/.venv/bin/dimos" topic send explore_cmd "Bool(data=True)" \
        >/dev/null 2>&1 || true ) &
fi

echo "==> Go2 MuJoCo sim starting; viewer: http://localhost:9878/?url=rerun%2Bhttp%3A%2F%2F127.0.0.1%3A9877%2Fproxy"
exec "$REPO/.venv/bin/dimos" --simulation run unitree-go2 \
    --rerunbridgemodule.rerun-open web "$@"
