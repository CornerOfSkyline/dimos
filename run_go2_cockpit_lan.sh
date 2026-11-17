#!/usr/bin/env bash
# Launch the full Cockpit experience over LAN (TLS relay on 0.0.0.0:7780).
#
# One-time client setup (NOT handled by this script):
#   1. sudo ufw allow 7780/tcp && sudo ufw allow 7780/udp
#   2. Import certs/dimos-relay-ca.pem into the client browser/OS trust store
#      (needed for the HTTPS page; the QUIC data path pins the cert hash).
#   3. hosts entry (optional, prettier URL): "172.23.53.132 dimos.local"
# Then open https://172.23.53.132:7780 and enter the viewer token from
# certs/relay-auth.json ("viewers" -> "zjf").
#
# Cert notes: the leaf must be ECDSA P-256 with <14 days validity (Chrome's
# serverCertificateHashes rules) because the relay advertises its hash so
# browsers accept a private-CA cert for WebTransport. The script reissues the
# leaf on every start, so it never goes stale. The CA is long-lived.
set -euo pipefail

WS="$HOME/workspace/dimos_ws"
REPO="$WS/dimos"
CERTS="$WS/certs"
DENO="$HOME/.cache/dimos/deno/v2.9.8/deno"
LAN_IP="${LAN_IP:-172.23.53.132}"
MODEL="${OLLAMA_MODEL:-ollama:qwen3-vl:32b}"
ROBOT_KEY=$(python3 -c "import json;print(json.load(open('$CERTS/relay-auth.json'))['robots']['zjf-4090'])")

cd "$REPO"

# 1. fresh ECDSA leaf (<14d, Chrome hash-pinning cap) + fullchain
cd "$CERTS"
openssl ecparam -name prime256v1 -genkey -noout -out dimos-relay-ec.key 2>/dev/null
openssl req -new -key dimos-relay-ec.key -out dimos-relay-ec.csr -subj "/CN=dimos-relay" 2>/dev/null
openssl x509 -req -in dimos-relay-ec.csr -CA dimos-relay-ca.pem -CAkey dimos-relay-ca.key \
    -CAcreateserial -out dimos-relay.pem -days 9 -extfile san2.cnf -extensions v3_req 2>/dev/null
cat dimos-relay.pem dimos-relay-ca.pem > dimos-relay-fullchain.pem
cd "$REPO"

# 2. restart the TLS relay with the fresh cert
pkill -f "relay/main.ts" 2>/dev/null || true
sleep 1
cd "$REPO/web"
nohup "$DENO" run --v8-flags=--expose-gc --frozen --node-modules-dir=none \
    --allow-read="$REPO/web,$CERTS" --allow-net --config deno.json relay/main.ts \
    --host 0.0.0.0 --port 7780 \
    --cert "$CERTS/dimos-relay-fullchain.pem" --key "$CERTS/dimos-relay-ec.key" \
    --auth-file "$CERTS/relay-auth.json" \
    --cockpit-dir cockpit/dist --sdk-dir sdk/dist \
    > /tmp/relay_tls.log 2>&1 &
sleep 4
ss -tln 2>/dev/null | grep -q ":7780 " || { echo "relay failed:"; tail -5 /tmp/relay_tls.log; exit 1; }
cd "$REPO"

# 3. ollama (agent brain): a remote server set in OLLAMA_BASE_URL (or .env)
#    is used as-is; only a local setup starts one.
if [ -z "${OLLAMA_BASE_URL:-}" ]; then
    grep -q "^OLLAMA_BASE_URL=..*" "$REPO/.env" 2>/dev/null \
        || curl -s --max-time 3 http://localhost:11434/api/version >/dev/null 2>&1 \
        || { nohup ollama serve > /tmp/ollama_serve.log 2>&1 & sleep 3; }
fi

# 4. restart the dimos stack against the relay
"$REPO/.venv/bin/dimos" stop >/dev/null 2>&1 || true
sleep 2
export MUJOCO_GL=egl
export LD_LIBRARY_PATH="$WS/libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

echo "==> cockpit: https://$LAN_IP:7780  (viewer token in $CERTS/relay-auth.json)"
SYSTEM_PROMPT="${DIMOS_SYSTEM_PROMPT:-You are a Unitree Go2 robot assistant in a web cockpit. The user chats with you in a chat panel: always reply with plain text in your message content directly. NEVER use the speak skill - it has no TTS configured and always fails. Keep replies concise (1-3 sentences). Skills: begin_exploration, end_exploration, navigate_with_text, stop_navigation, move_to, observe, look_out_for, stop_looking_out, follow_person, stop_following, tag_location, start_patrol, stop_patrol, get_battery_soc, wait, current_time.}"
exec "$REPO/.venv/bin/dimos" --simulation run unitree-go2-agentic-cockpit \
    --agent.model "$MODEL" \
    --agent.system-prompt "$SYSTEM_PROMPT" \
    --relay-url "https://$LAN_IP:7780" \
    --dynamicrelaybridge_940788bb41.relay-ca "$CERTS/dimos-relay-ca.pem" \
    --dynamicrelaybridge_940788bb41.relay-key "$ROBOT_KEY" \
    "$@"
