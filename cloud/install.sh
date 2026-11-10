#!/usr/bin/env bash
# install.sh [--dry-run] <dir>
#
# Installs the managed ComfyUI runtime at <dir>: a venv, torch, ComfyUI at the
# commit manifest.json pins, and each node pack at its pin under custom_nodes.
# Re-running moves an existing checkout to the pins. --dry-run prints every
# command instead of running it. Needs python3 (3.10 or newer) and git.
#
#   COUCHY_TORCH_INDEX  pip index for torch; default the CUDA 13.0 wheels,
#                       "default" means PyPI's own wheel (macOS, CPU)
#   COUCHY_PYTHON       the python that makes the venv; default python3
#   COUCHY_SKIP_PACKS   pack ids (space-separated) already in place under
#                       custom_nodes, whose clone is skipped (the cloud image
#                       copies the private couchyforms-helper in itself)
#
# Copied from getcouchy/couchy runtime/install.sh (27bf6bf) for the cloud
# image; only COUCHY_SKIP_PACKS is new.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
manifest="$here/manifest.json"
dry=0
if [ "${1:-}" = "--dry-run" ]; then
  dry=1
  shift
fi
dir=${1:?usage: install.sh [--dry-run] <dir>}
torch_index=${COUCHY_TORCH_INDEX:-https://download.pytorch.org/whl/cu130}
py=${COUCHY_PYTHON:-python3}
pip="$dir/venv/bin/pip"
comfy="$dir/ComfyUI"
custom="$comfy/custom_nodes"

run() {
  echo "+ $*"
  if [ "$dry" = 0 ]; then "$@"; fi
}

# field prints one field of the manifest's comfyui object.
field() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["comfyui"][sys.argv[2]])' "$manifest" "$1"
}

# packs prints "id dir repo commit" per pack, in manifest order.
packs() {
  python3 -c 'import json, sys
for p in json.load(open(sys.argv[1]))["packs"]:
    print(p["id"], p["dir"], p["repo"], p["commit"])' "$manifest"
}

# clone_at <repo> <commit> <dest> leaves dest checked out at commit, fetching
# when dest is already a clone.
clone_at() {
  if [ -d "$3/.git" ]; then
    run git -C "$3" fetch --quiet origin
  else
    run git clone --quiet "$1" "$3"
  fi
  run git -C "$3" checkout --quiet --detach "$2"
}

# requirements <dir> installs dir/requirements.txt when the pack has one.
requirements() {
  if [ "$dry" = 1 ] || [ -f "$1/requirements.txt" ]; then
    run "$pip" install -r "$1/requirements.txt"
  fi
}

run mkdir -p "$dir"
if [ ! -x "$dir/venv/bin/python" ]; then
  run "$py" -m venv "$dir/venv"
fi
run "$pip" install --upgrade pip
if [ "$torch_index" = default ]; then
  run "$pip" install torch torchvision torchaudio
else
  run "$pip" install torch torchvision torchaudio --index-url "$torch_index"
fi

clone_at "$(field repo)" "$(field commit)" "$comfy"
requirements "$comfy"

run mkdir -p "$custom"
packs | while read -r id pdir repo commit; do
  echo "# $id"
  case " ${COUCHY_SKIP_PACKS:-} " in
    *" $id "*) requirements "$custom/$pdir"; continue ;;
  esac
  clone_at "$repo" "$commit" "$custom/$pdir"
  requirements "$custom/$pdir"
done

if command -v sha256sum >/dev/null 2>&1; then
  sum=$(sha256sum "$manifest" | cut -d' ' -f1)
else
  sum=$(shasum -a 256 "$manifest" | cut -d' ' -f1)
fi
stamp="$dir/couchy-runtime.json"
echo "+ write $stamp"
if [ "$dry" = 0 ]; then
  printf '{"manifestSha256":"%s","comfyui":"%s","installedAt":"%s"}\n' "$sum" "$(field version)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$stamp"
fi
echo "installed ComfyUI $(field version) at $comfy"
