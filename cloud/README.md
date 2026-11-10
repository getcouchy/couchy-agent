# The cloud image

`ghcr.io/getcouchy/couchy-comfyui:<version>` is the image Couchy starts cloud GPUs from, on
RunPod and Vast.ai (the same image and tag on both). It holds:

- ComfyUI and the packs Couchy needs, at the commits [`manifest.json`](manifest.json) pins:
  ComfyUI-Manager, WAS Node Suite, ComfyUI-GGUF, ComfyUI-VideoHelperSuite and
  couchyforms-helper. It's a copy of getcouchy/couchy's `runtime/manifest.json` (the managed
  runtime, as of 27bf6bf), so update the two together, keeping one difference: the
  `comfyform-helper` pack's `repo` is `https://github.com/getcouchy/couchyforms-helper` (not
  `erstaples/comfyform-helper`) and it has `"private": true`, since CI checks it out with a token.
  The commit pins are the same. `install.sh` is that repo's `runtime/install.sh`, plus
  `COUCHY_SKIP_PACKS`.
- couchy-agent from this checkout, in **lockdown** (`COUCHY_AGENT_LOCKDOWN=1`).
- Caddy (2.10.2) as the token proxy.
- No models.

The tag is the agent's version (`couchy_agent/__init__.py`), and the API names the tag it
expects, so the two move together.

## What runs

`start.sh` starts ComfyUI on **127.0.0.1:8189 only**, and Caddy on `$PORT` (8188), the one port
the pod exposes. Caddy forwards a request only when it carries
`Authorization: Bearer $COUCHY_SERVER_TOKEN`. It drops that header on the way, and answers
everything else with a bare 401. Behind it, lockdown makes every ComfyUI route need an agent key
too (`X-Couchy-Agent-Key`), the websocket, `/object_info` and `/system_stats` included. So
Couchy's open-network check (`GET /system_stats` with no credentials) fails, as it must. If
ComfyUI or Caddy stops, the container stops.

| Env | |
| --- | --- |
| `COUCHY_SERVER_TOKEN` | Required. The token Caddy checks: 32 to 256 of `A-Z a-z 0-9 . _ ~ -` |
| `COUCHY_AGENT_PAIRING_TOKEN` | The one-time token the API minted for this pod: at least 32 random characters (shorter ones are ignored). `POST /couchy/pair {code: <token>}` pairs once, within 30 minutes of the agent's start, and the key it answers has `models: write`. Tries share the pairing lockout, which behind Caddy is one bucket for every caller, so only a long random token is safe |
| `COUCHY_AGENT_ALLOWED_HOSTS` | Comma-separated hosts added to `allowed-hosts`, such as the workspace's model buckets, for presigned pulls |
| `COUCHY_MODELS_DIR` | A mounted folder, such as a RunPod network volume, to use as ComfyUI's models folder. The hash index goes in `<dir>/.couchy-index` so a new pod rehashes only what changed |
| `COUCHY_AGENT_INDEX_DIR` | Where the hash index goes instead |
| `COUCHY_TLS_CERT`, `COUCHY_TLS_KEY` | A PEM certificate and its unencrypted private key. With them, Caddy serves **HTTPS only** on `$PORT` with exactly that certificate (see [TLS mode](#tls-mode-vastai)). Newlines may be real or written `\n` |
| `PORT`, `COMFY_PORT`, `COMFY_ARGS` | Caddy's port (8188), ComfyUI's localhost port (8189), and extra `main.py` arguments |

The pod never calls the Couchy API and holds no bucket keys and no API address. The API calls
the pod: through RunPod's proxy URL, or on Vast.ai straight at the instance's mapped port.

## Providers

| | RunPod | Vast.ai |
| --- | --- | --- |
| How the API reaches it | RunPod's HTTPS proxy (`https://<pod>-8188.proxy.runpod.net`), which forwards plain HTTP to port 8188 | The instance's public IP and the host port Vast maps to 8188, over plain TCP |
| TLS | RunPod's proxy terminates it; the image speaks plain HTTP | The image serves it: TLS mode |
| Env | `COUCHY_SERVER_TOKEN`, `COUCHY_AGENT_PAIRING_TOKEN`, and optionally `COUCHY_AGENT_ALLOWED_HOSTS`, `COUCHY_MODELS_DIR` (a network volume) | The same, plus `COUCHY_TLS_CERT` and `COUCHY_TLS_KEY` |
| Registry | GHCR credentials in the pod template | GHCR credentials on the instance (`image_login`) |

## TLS mode (Vast.ai)

The API makes a fresh self-signed certificate for each session, passes the pair in the
instance's env, and accepts only that certificate, pinned by its SHA-256. The image doesn't make
or fetch certificates, and there's no ACME.

When either variable is set, `start.sh` runs `caddy adapt` on the Caddyfile and pipes it through
[`tls.py`](tls.py), which checks the pair and adds it to Caddy's JSON config. That config goes to
`caddy run --config /dev/stdin` and stays in memory: the key is written to no file, Caddy's
storage keeps nothing of an unmanaged certificate, and `persist_config off` keeps Caddy from
saving its config. `start.sh` then unsets both variables, so neither ComfyUI nor Caddy has them in
its environment (the container's own env, which the host can read, still does).

- Caddy serves TLS 1.2+ over HTTP/1.1 and HTTP/2 on `$PORT`, with that certificate whatever
  the SNI (an IP address sends none). No HTTP/3, since Vast maps one TCP port. Plain HTTP on the
  port gets Go's `400 Client sent an HTTP request to an HTTPS server` and never reaches the
  token check or ComfyUI.
- **It never falls back to plain HTTP.** A certificate without its key (or a key without its
  certificate), both empty, a certificate or key that doesn't parse, an encrypted key, or a key
  that isn't the certificate's fails start with exit 64 and one line such as
  `couchy-cloud: COUCHY_TLS_KEY isn't the private key of COUCHY_TLS_CERT; not starting`. ComfyUI
  isn't started. The line never quotes the env.
- The token proxy, lockdown, self-pairing and allowed hosts behind it are the same as on RunPod,
  and ComfyUI still listens on localhost only.

The host can read the env, as it can read everything else on the machine. Pinning protects the
path between the API and the instance, not the instance from its host.

## Build and test

CI (`.github/workflows/cloud-image.yml`, steps in `ci.sh`) builds the image on every pull
request that changes it and runs `smoke.sh` against it on the CPU, with read-only permissions.
On a release tag `v<version>` a separate job, the only one that may write packages, does the
same and pushes it to GHCR. Its
secret `COUCHY_CLOUD_REPO_TOKEN` must be able to read getcouchy/couchyforms-helper, which is
private and is handed to the build as a build context.

By hand, from the repository root:

```sh
git clone https://github.com/getcouchy/couchyforms-helper ../couchyforms-helper
git -C ../couchyforms-helper checkout "$(python3 -c 'import json; print(next(p["commit"] for p in json.load(open("cloud/manifest.json"))["packs"] if p["id"] == "comfyform-helper"))')"
docker build -f cloud/Dockerfile --build-context couchyforms-helper=../couchyforms-helper -t couchy-comfyui:dev .
cloud/smoke.sh couchy-comfyui:dev
```

`smoke.sh` starts the image twice, as on RunPod (plain HTTP) and as on Vast.ai (TLS mode), and
checks in both:
- Nothing answers without the token, or with the token but no key.
- The pairing token pairs once, with `models: write`.
- The key opens ComfyUI.
- ComfyUI isn't reachable except through Caddy, the env's allowed hosts are in effect, and no token reaches the container log.

In TLS mode it also checks that the port answers with exactly `COUCHY_TLS_CERT` (by SHA-256
fingerprint) and refuses plain HTTP, and that the key is in no log, in no file under the
folders the container writes, and in neither ComfyUI's nor Caddy's environment. Then it checks
that a half pair, a certificate that doesn't parse, and a key of another certificate each fail
start with exit 64 and the right line. `tests/test_cloud_tls.py` covers `tls.py`'s checks
without Docker.

## Pulling it

GHCR packages from a private repository are private. RunPod and Vast.ai need registry
credentials for `ghcr.io` (a token with `read:packages`), in RunPod's pod template or Vast's
instance login, or the package made public.
