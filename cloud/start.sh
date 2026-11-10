#!/usr/bin/env bash
# The cloud image's entrypoint: ComfyUI (with couchy-agent, in lockdown) on
# localhost only, and Caddy on $PORT forwarding to it with the server token.
# If either stops, the container stops.
#
#   COUCHY_SERVER_TOKEN         required: the token Caddy checks (Bearer)
#   COUCHY_AGENT_PAIRING_TOKEN  the one-time pairing token (the agent reads it)
#   COUCHY_AGENT_ALLOWED_HOSTS  hosts presigned pulls may fetch from
#   COUCHY_MODELS_DIR           a mounted models folder (a RunPod volume) to use
#                               as ComfyUI's models; its hash index goes beside
#                               it unless COUCHY_AGENT_INDEX_DIR says otherwise
#   PORT (8188), COMFY_PORT (8189), COMFY_ARGS (appended to main.py)
set -eu

comfy=/opt/comfyui/ComfyUI
python=/opt/comfyui/venv/bin/python

token=${COUCHY_SERVER_TOKEN:-}
if ! printf '%s' "$token" | grep -Eq '^[A-Za-z0-9._~-]{32,256}$'; then
  echo "couchy-cloud: COUCHY_SERVER_TOKEN must be 32 to 256 of A-Z a-z 0-9 . _ ~ -" >&2
  exit 64
fi
export PORT=${PORT:-8188} COMFY_PORT=${COMFY_PORT:-8189} COUCHY_SERVER_TOKEN="$token"
export COUCHY_AGENT_LOCKDOWN=${COUCHY_AGENT_LOCKDOWN:-1}

if [ -n "${COUCHY_MODELS_DIR:-}" ]; then
  mkdir -p "$COUCHY_MODELS_DIR"
  if [ ! -L "$comfy/models" ]; then
    mv "$comfy/models" "$comfy/models.image"
    # Keep ComfyUI's folder layout on a fresh volume.
    (cd "$comfy/models.image" && find . -type d -exec mkdir -p "$COUCHY_MODELS_DIR/{}" \;)
  fi
  ln -sfn "$COUCHY_MODELS_DIR" "$comfy/models"
  export COUCHY_AGENT_INDEX_DIR=${COUCHY_AGENT_INDEX_DIR:-$COUCHY_MODELS_DIR/.couchy-index}
fi

umask 077
cd "$comfy"
# shellcheck disable=SC2086
"$python" main.py --listen 127.0.0.1 --port "$COMFY_PORT" --disable-auto-launch ${COMFY_ARGS:-} &
comfy_pid=$!
caddy run --config /opt/couchy-cloud/Caddyfile --adapter caddyfile &
caddy_pid=$!
trap 'kill -TERM "$comfy_pid" "$caddy_pid" 2>/dev/null || true' TERM INT
wait -n "$comfy_pid" "$caddy_pid"
status=$?
kill -TERM "$comfy_pid" "$caddy_pid" 2>/dev/null || true
wait || true
exit "$status"
