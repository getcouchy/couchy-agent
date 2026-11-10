#!/usr/bin/env bash
# smoke.sh <image>: start the cloud image on the CPU and check what Couchy
# relies on. Through Caddy, nothing answers without the server token, and
# with the token nothing but pairing answers without an agent key (so
# Couchy's open-network check, GET /system_stats with no credentials,
# fails). The pairing token pairs once with models: write, and the key it
# answers opens ComfyUI. ComfyUI itself listens on localhost only.
set -eu
image=${1:?usage: smoke.sh <image>}
token=smoke-server-token-0123456789abcdef0123456789
pairing=smoke-pairing-token-fedcba9876543210
name=couchy-cloud-smoke-$$
port=18188

docker run -d --name "$name" -p "127.0.0.1:$port:8188" \
  -e COUCHY_SERVER_TOKEN="$token" -e COUCHY_AGENT_PAIRING_TOKEN="$pairing" \
  -e COUCHY_AGENT_ALLOWED_HOSTS=acct.r2.cloudflarestorage.com -e COMFY_ARGS=--cpu "$image" >/dev/null
trap 'docker logs "$name" > smoke-container.log 2>&1 || true; docker rm -f "$name" >/dev/null 2>&1 || true' EXIT

url=http://127.0.0.1:$port
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
fail() { echo "smoke: $*" >&2; exit 1; }

# Wait for ComfyUI behind Caddy: with the token, lockdown answers 401 once
# the agent is loaded (Caddy alone answers 502 until then).
for _ in $(seq 1 180); do
  [ "$(code -H "Authorization: Bearer $token" "$url/system_stats")" = 401 ] && break
  sleep 2
done

[ "$(code "$url/system_stats")" = 401 ] || fail "no token: /system_stats should be 401"
[ "$(code -H "Authorization: Bearer wrong-token-0123456789abcdef0123456789" "$url/system_stats")" = 401 ] \
  || fail "wrong token should be 401"
[ "$(code -H "Authorization: Bearer $token" "$url/system_stats")" = 401 ] || fail "lockdown: no key should be 401"
[ "$(code -H "Authorization: Bearer $token" "$url/")" = 401 ] || fail "lockdown: ComfyUI's UI should be 401"

pair() {
  curl -s -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    -d "{\"code\":\"$pairing\",\"label\":\"smoke\"}" "$url/couchy/pair"
}
out=$(pair)
key=$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["models"] == "write", d; print(d["key"])') \
  || fail "the pairing token should pair with models: write: $out"
[ "$(printf '%s' "$(pair)" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error",""))')" != "" ] \
  || fail "the pairing token should work once"

auth=(-H "Authorization: Bearer $token" -H "X-Couchy-Agent-Key: $key")
[ "$(code "${auth[@]}" "$url/system_stats")" = 200 ] || fail "token and key: /system_stats should be 200"
[ "$(code "${auth[@]}" "$url/object_info")" = 200 ] || fail "token and key: /object_info should be 200"
[ "$(code -H "X-Couchy-Agent-Key: $key" "$url/system_stats")" = 401 ] || fail "a key without the token should be 401"
curl -s "${auth[@]}" "$url/couchy/usage" | grep -q '"lockdown": "on"' || fail "usage should report lockdown on"

ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$name")
[ "$(code --max-time 5 "http://$ip:8189/system_stats")" = 000 ] || fail "ComfyUI should listen on localhost only"
docker exec "$name" sh -c 'cd /opt/comfyui/ComfyUI/custom_nodes/couchy-agent && /opt/comfyui/venv/bin/python -m couchy_agent config' \
  | grep -q 'acct.r2.cloudflarestorage.com' || fail "COUCHY_AGENT_ALLOWED_HOSTS should be in the allowed hosts"
if docker logs "$name" 2>&1 | grep -q -e "$pairing" -e "$token"; then fail "a token reached the log"; fi
echo "smoke: ok"
