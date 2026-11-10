# ghcr.io/getcouchy/couchy-comfyui:<version>, the image Couchy starts cloud
# GPUs from: ComfyUI and the packs at manifest.json's pins, couchy-agent from
# this checkout, and Caddy as the token proxy. No models. See cloud/README.md.
#
# Build from the repository root, with couchyforms-helper (private) checked
# out at the manifest's commit beside it:
#
#   docker build -f cloud/Dockerfile \
#     --build-context couchyforms-helper=../couchyforms-helper \
#     -t ghcr.io/getcouchy/couchy-comfyui:0.5.0 .
FROM caddy:2.10.2 AS caddy

FROM nvidia/cuda:13.0.3-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-pip git ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=caddy /usr/bin/caddy /usr/bin/caddy

COPY cloud/manifest.json cloud/install.sh /opt/couchy-cloud/
RUN COUCHY_SKIP_PACKS=comfyform-helper /opt/couchy-cloud/install.sh /opt/comfyui

# couchyforms-helper is private, so CI checks it out at the manifest's commit
# and hands it in as a build context; install.sh skipped its clone.
COPY --from=couchyforms-helper . /opt/comfyui/ComfyUI/custom_nodes/comfyform-helper/
RUN rm -rf /opt/comfyui/ComfyUI/custom_nodes/comfyform-helper/.git \
    && if [ -f /opt/comfyui/ComfyUI/custom_nodes/comfyform-helper/requirements.txt ]; then \
         /opt/comfyui/venv/bin/pip install -r /opt/comfyui/ComfyUI/custom_nodes/comfyform-helper/requirements.txt; fi

COPY __init__.py pyproject.toml README.md /opt/comfyui/ComfyUI/custom_nodes/couchy-agent/
COPY couchy_agent /opt/comfyui/ComfyUI/custom_nodes/couchy-agent/couchy_agent
COPY cloud/start.sh cloud/Caddyfile /opt/couchy-cloud/

# Lockdown is on in this image: every ComfyUI route needs an agent key.
ENV COUCHY_AGENT_LOCKDOWN=1 \
    PORT=8188

EXPOSE 8188
CMD ["/opt/couchy-cloud/start.sh"]
