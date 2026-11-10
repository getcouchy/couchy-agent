#!/usr/bin/env bash
# The cloud image's entrypoint: ComfyUI (with couchy-agent, in lockdown) on
# localhost only, and Caddy on $PORT forwarding to it with the server token.
# If either stops, the container stops.
#
#   COUCHY_SERVER_TOKEN         required: the token Caddy checks (Bearer)
#   COUCHY_AGENT_PAIRING_TOKEN  the one-time pairing token (the agent reads it)
#   COUCHY_AGENT_ALLOWED_HOSTS  hosts presigned pulls may fetch from
#   COUCHY_TLS_CERT, COUCHY_TLS_KEY
#                               a PEM certificate and its key (Vast.ai): Caddy
#                               serves HTTPS on $PORT with exactly that pair,
#                               never plain HTTP; a half or bad pair fails start
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

# TLS mode: adapt the Caddyfile and add the pair (tls.py checks it), held in
# memory and piped to Caddy, so the key is never written to disk. Neither
# ComfyUI nor Caddy keeps it in its environment.
caddy_tls=
if [ -n "${COUCHY_TLS_CERT+set}${COUCHY_TLS_KEY+set}" ]; then
  caddy_tls=$(set -o pipefail; caddy adapt --config /opt/couchy-cloud/Caddyfile --adapter caddyfile 2>/dev/null \
    | /usr/bin/python3 -I /opt/couchy-cloud/tls.py) || exit 64
  echo "couchy-cloud: TLS on port $PORT with the certificate in COUCHY_TLS_CERT" >&2
fi
unset COUCHY_TLS_CERT COUCHY_TLS_KEY

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
if [ -n "$caddy_tls" ]; then
  printf '%s' "$caddy_tls" | caddy run --config /dev/stdin &
else
  caddy run --config /opt/couchy-cloud/Caddyfile --adapter caddyfile &
fi
caddy_pid=$!
trap 'kill -TERM "$comfy_pid" "$caddy_pid" 2>/dev/null || true' TERM INT
wait -n "$comfy_pid" "$caddy_pid"
status=$?
kill -TERM "$comfy_pid" "$caddy_pid" 2>/dev/null || true
wait || true
exit "$status"
