#!/usr/bin/env bash
# smoke.sh <image>: start the cloud image on the CPU and check what Couchy
# relies on, first as on RunPod (plain HTTP behind its proxy), then as on
# Vast.ai (TLS on the mapped port with COUCHY_TLS_CERT and COUCHY_TLS_KEY).
# Through Caddy, nothing answers without the server token, and with the
# token nothing but pairing answers without an agent key (so Couchy's
# open-network check, GET /system_stats with no credentials, fails). The
# pairing token pairs once with models: write, and the key it answers opens
# ComfyUI. ComfyUI itself listens on localhost only. In TLS mode the port
# answers with exactly the given certificate and refuses plain HTTP, and a
# half or bad pair fails start. No token or TLS key reaches a log.
set -eu
image=${1:?usage: smoke.sh <image>}
token=smoke-server-token-0123456789abcdef0123456789
pairing=smoke-pairing-token-fedcba9876543210
names=()

work=$(mktemp -d)
cleanup() {
  : > smoke-container.log
  for n in "${names[@]}"; do
    { echo "== $n"; docker logs "$n" 2>&1 || true; } >> smoke-container.log
    docker rm -f "$n" >/dev/null 2>&1 || true
  done
  rm -rf "$work"
}
trap cleanup EXIT

pair_files() { # pair_files <name>: a self-signed certificate for 127.0.0.1, as the API makes one
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1 -subj /CN=couchy-session \
    -addext subjectAltName=IP:127.0.0.1 -keyout "$work/$1.key" -out "$work/$1.crt" 2>/dev/null
}
pair_files session
pair_files other
tls_cert=$(cat "$work/session.crt") tls_key=$(cat "$work/session.key")
key_line=$(sed -n 2p "$work/session.key")

code() { curl -s -o /dev/null -w '%{http_code}' "${tls[@]}" "$@"; }
fail() { echo "smoke: $*" >&2; exit 1; }

start() { # start <name> <port> [docker run args...]
  local n=$1 p=$2; shift 2
  names+=("$n")
  docker run -d --name "$n" -p "127.0.0.1:$p:8188" \
    -e COUCHY_SERVER_TOKEN="$token" -e COUCHY_AGENT_PAIRING_TOKEN="$pairing" \
    -e COUCHY_AGENT_ALLOWED_HOSTS=acct.r2.cloudflarestorage.com -e COMFY_ARGS=--cpu "$@" "$image" >/dev/null
}

checks() { # checks <name>: everything both modes share, against $url
  local name=$1 out key
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
    curl -s "${tls[@]}" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
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
  curl -s "${tls[@]}" "${auth[@]}" "$url/couchy/usage" | grep -q '"lockdown": "on"' || fail "usage should report lockdown on"

  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$name")
  [ "$(code --max-time 5 "http://$ip:8189/system_stats")" = 000 ] || fail "ComfyUI should listen on localhost only"
  docker exec "$name" sh -c 'cd /opt/comfyui/ComfyUI/custom_nodes/couchy-agent && /opt/comfyui/venv/bin/python -m couchy_agent config' \
    | grep -q 'acct.r2.cloudflarestorage.com' || fail "COUCHY_AGENT_ALLOWED_HOSTS should be in the allowed hosts"
  if docker logs "$name" 2>&1 | grep -q -e "$pairing" -e "$token"; then fail "a token reached the log"; fi
}

# RunPod: plain HTTP on the port, behind RunPod's proxy.
tls=()
url=http://127.0.0.1:18188
start couchy-cloud-smoke-$$ 18188
checks couchy-cloud-smoke-$$
docker rm -f couchy-cloud-smoke-$$ >/dev/null
echo "smoke: plain HTTP ok"

# Vast.ai: TLS on the mapped port with the API's certificate.
tls=(--cacert "$work/session.crt")
url=https://127.0.0.1:18443
start couchy-cloud-smoke-tls-$$ 18443 -e COUCHY_TLS_CERT="$tls_cert" -e COUCHY_TLS_KEY="$tls_key"
checks couchy-cloud-smoke-tls-$$
want=$(openssl x509 -in "$work/session.crt" -noout -fingerprint -sha256)
got=$(openssl s_client -connect 127.0.0.1:18443 </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256)
[ "$got" = "$want" ] || fail "TLS should answer with exactly COUCHY_TLS_CERT ($want), got $got"
plain=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" http://127.0.0.1:18443/system_stats || true)
case "$plain" in 400|000) ;; *) fail "plain HTTP on the TLS port should be refused, got $plain" ;; esac
docker logs couchy-cloud-smoke-tls-$$ 2>&1 | grep -q "couchy-cloud: TLS on port 8188" || fail "TLS mode should say so in the log"
if docker logs couchy-cloud-smoke-tls-$$ 2>&1 | grep -qF -e "$key_line"; then fail "the TLS key reached the log"; fi
if docker exec couchy-cloud-smoke-tls-$$ sh -c 'grep -rlF -e "$1" /root /tmp /run /var /etc /home /opt/couchy-cloud /opt/comfyui/ComfyUI 2>/dev/null' _ "$key_line"; then
  fail "the TLS key was written to a file"
fi
# PID 1 (start.sh) keeps the env it was started with; what it starts doesn't.
envs=$(docker exec couchy-cloud-smoke-tls-$$ sh -c 'for d in /proc/[0-9]*; do
  case "$(tr "\0" " " < "$d/cmdline" 2>/dev/null)" in
    "/opt/comfyui/venv/bin/python main.py"*|"caddy run"*) echo "== $d"; tr "\0" "\n" < "$d/environ" ;;
  esac
done')
[ "$(printf '%s\n' "$envs" | grep -c '^== ')" -ge 2 ] || fail "should find ComfyUI and Caddy's processes: $envs"
if printf '%s\n' "$envs" | grep -q '^COUCHY_TLS_'; then fail "ComfyUI or Caddy has the TLS pair in its environment"; fi
docker rm -f couchy-cloud-smoke-tls-$$ >/dev/null
echo "smoke: TLS ok"

# A half or bad pair fails start, before ComfyUI, and says why.
refused() { # refused <words> [docker run args...]
  local words=$1 out status=0; shift
  out=$(timeout 120 docker run --rm -e COUCHY_SERVER_TOKEN="$token" -e COMFY_ARGS=--cpu "$@" "$image" 2>&1) || status=$?
  [ "$status" = 64 ] || fail "start should fail (64) with $words, got $status: $out"
  printf '%s' "$out" | grep -qF "$words" || fail "start should say \"$words\": $out"
  if printf '%s' "$out" | grep -qF -e "$key_line"; then fail "the TLS key reached the log"; fi
}
refused "COUCHY_TLS_KEY is missing or empty" -e COUCHY_TLS_CERT="$tls_cert"
refused "COUCHY_TLS_CERT is missing or empty" -e COUCHY_TLS_KEY="$tls_key"
refused "COUCHY_TLS_CERT isn't a PEM certificate" -e COUCHY_TLS_CERT=nonsense -e COUCHY_TLS_KEY="$tls_key"
refused "COUCHY_TLS_KEY isn't the private key of COUCHY_TLS_CERT" \
  -e COUCHY_TLS_CERT="$tls_cert" -e COUCHY_TLS_KEY="$(cat "$work/other.key")"
echo "smoke: bad pairs refused"
echo "smoke: ok"
