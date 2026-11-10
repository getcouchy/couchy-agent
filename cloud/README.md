# The cloud image

`ghcr.io/getcouchy/couchy-comfyui:<version>` is the image Couchy starts cloud GPUs from
(RunPod first). It holds:

- ComfyUI and the packs Couchy needs, at the commits [`manifest.json`](manifest.json) pins:
  ComfyUI-Manager, WAS Node Suite, ComfyUI-GGUF, ComfyUI-VideoHelperSuite and
  couchyforms-helper. It's a copy of getcouchy/couchy's `runtime/manifest.json` (the managed
  runtime, as of 27bf6bf), so update the two together. `install.sh` is that repo's
  `runtime/install.sh`, plus `COUCHY_SKIP_PACKS`.
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
| `COUCHY_AGENT_PAIRING_TOKEN` | The one-time token the API minted for this pod. `POST /couchy/pair {code: <token>}` pairs once, within 30 minutes of the agent's start, and the key it answers has `models: write` |
| `COUCHY_AGENT_ALLOWED_HOSTS` | Comma-separated hosts added to `allowed-hosts`, such as the workspace's model buckets, for presigned pulls |
| `COUCHY_MODELS_DIR` | A mounted folder, such as a RunPod network volume, to use as ComfyUI's models folder. The hash index goes in `<dir>/.couchy-index` so a new pod rehashes only what changed |
| `COUCHY_AGENT_INDEX_DIR` | Where the hash index goes instead |
| `PORT`, `COMFY_PORT`, `COMFY_ARGS` | Caddy's port (8188), ComfyUI's localhost port (8189), and extra `main.py` arguments |

The pod never calls the Couchy API and holds no bucket keys and no API address. The API calls
the pod, through RunPod's proxy URL.

## Build and test

CI (`.github/workflows/cloud-image.yml`) builds the image on every pull request that changes it,
runs `smoke.sh` against it on the CPU, and on a release tag `v<version>` pushes it to GHCR. Its
secret `COUCHY_CLOUD_REPO_TOKEN` must be able to read getcouchy/couchyforms-helper, which is
private and is handed to the build as a build context.

By hand, from the repository root:

```sh
git clone https://github.com/getcouchy/couchyforms-helper ../couchyforms-helper
git -C ../couchyforms-helper checkout "$(python3 -c 'import json; print(next(p["commit"] for p in json.load(open("cloud/manifest.json"))["packs"] if p["id"] == "comfyform-helper"))')"
docker build -f cloud/Dockerfile --build-context couchyforms-helper=../couchyforms-helper -t couchy-comfyui:dev .
cloud/smoke.sh couchy-comfyui:dev
```

`smoke.sh` checks four things:
- Nothing answers without the token, or with the token but no key.
- The pairing token pairs once, with `models: write`.
- The key opens ComfyUI.
- ComfyUI isn't reachable except through Caddy, the env's allowed hosts are in effect, and no token reaches the container log.

## Pulling it

GHCR packages from a private repository are private. RunPod needs registry credentials for
`ghcr.io` (a token with `read:packages`) in the pod template, or the package made public.
