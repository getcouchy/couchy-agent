#!/usr/bin/env bash
# The cloud image's CI steps, shared by the pull request job
# (.github/workflows/cloud-image.yml) and the publish job
# (.github/workflows/publish-image.yml).
#
#   ci.sh pins           write version= and helper= to $GITHUB_OUTPUT; on a
#                        tag, fail unless it is v<the agent's version>, and
#                        with RELEASE_VERSION set, unless it is that version
#   ci.sh build <image>  make room, build the image with couchyforms-helper
#                        from .build/, and run smoke.sh against it
set -eu

case "${1:-}" in
  pins)
    version=$(python3 -c 'import re; print(re.search(r"__version__ = \"(.+)\"", open("couchy_agent/__init__.py").read()).group(1))')
    if [ "${GITHUB_REF_TYPE:-}" = tag ] && [ "${GITHUB_REF_NAME:-}" != "v$version" ]; then
      echo "tag ${GITHUB_REF_NAME} doesn't match the agent's version $version" >&2
      exit 1
    fi
    if [ -n "${RELEASE_VERSION:-}" ] && [ "$RELEASE_VERSION" != "$version" ]; then
      echo "release $RELEASE_VERSION doesn't match the agent's version $version" >&2
      exit 1
    fi
    helper=$(python3 -c 'import json; print(next(p["commit"] for p in json.load(open("cloud/manifest.json"))["packs"] if p["id"] == "comfyform-helper"))')
    echo "version=$version" >> "$GITHUB_OUTPUT"
    echo "helper=$helper" >> "$GITHUB_OUTPUT"
    ;;
  build)
    image=${2:?usage: ci.sh build <image>}
    sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc /opt/hostedtoolcache/CodeQL
    docker system prune -af >/dev/null
    df -h /
    docker build -f cloud/Dockerfile --build-context couchyforms-helper=.build/couchyforms-helper -t "$image" .
    cloud/smoke.sh "$image"
    ;;
  *)
    echo "usage: ci.sh pins | build <image>" >&2
    exit 64
    ;;
esac
