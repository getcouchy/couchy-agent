"""Model files on this host: where they are, what's in a .safetensors
header, and how much room each volume has. Plain Python, no ComfyUI imports:
the caller passes the locations.

A location is ComfyUI's own models root ("host") or a share mounted on the
host (config.py). Each maps a ComfyUI folder type (checkpoints, loras,
text_encoders, ...) to the directories that hold it, in the order ComfyUI
searches them. A file is named by (location, folder, path), path relative to
its directory with / separators.

files.py's rules hold here too: below a folder's directory, symlinks are
never listed or read, and a path that resolves outside the directory is
refused. The directory itself may be a symlink (models/checkpoints pointing
at a bigger disk is common): it is the operator's own config, as it is
ComfyUI's. Model files aren't
namespaced: every tenant on the server shares them, which is why the models
grant on a key is separate from its rw/ro mode.
"""

import json
import os
import shutil
import stat
import struct

from .config import HOST

MAX_LIMIT = 1000

HEADER_CAP = 1024 * 1024
TENSOR_CAP = 2000

# ComfyUI's default layout under a models root: folder type -> the
# directory names it searches, in order (folder_paths.py, ComfyUI 0.44.0).
# A share uses the same layout; the host takes its own from folder_paths.
LAYOUT = {
    "checkpoints": ["checkpoints"],
    "configs": ["configs"],
    "loras": ["loras"],
    "vae": ["vae"],
    "text_encoders": ["text_encoders", "clip"],
    "diffusion_models": ["unet", "diffusion_models"],
    "clip_vision": ["clip_vision"],
    "style_models": ["style_models"],
    "embeddings": ["embeddings"],
    "diffusers": ["diffusers"],
    "vae_approx": ["vae_approx"],
    "controlnet": ["controlnet", "t2i_adapter"],
    "gligen": ["gligen"],
    "upscale_models": ["upscale_models"],
    "latent_upscale_models": ["latent_upscale_models"],
    "hypernetworks": ["hypernetworks"],
    "photomaker": ["photomaker"],
    "classifiers": ["classifiers"],
    "model_patches": ["model_patches"],
    "audio_encoders": ["audio_encoders"],
}

# Where pulls stream before they're renamed into place (pulls.py). Never
# listed, hashed or served.
PARTIAL = ".couchy-partial"

# folder_paths types that aren't model folders.
NOT_MODELS = ("custom_nodes",)


def share_folders(root):
    """A share's folders: ComfyUI's layout under its root."""
    return {folder: [os.path.join(root, d) for d in dirs] for folder, dirs in LAYOUT.items()}


def host_folders(folder_names_and_paths, share_roots=()):
    """The host's folders from folder_paths.folder_names_and_paths, minus
    non-model types and any directory inside a share (an extra_model_paths
    entry pointing at the NAS), which that share already lists."""
    shares = [os.path.realpath(r) for r in share_roots]
    out = {}
    for folder, entry in folder_names_and_paths.items():
        if folder in NOT_MODELS:
            continue
        paths = entry[0] if isinstance(entry, (tuple, list)) else entry
        dirs = []
        for d in paths:
            real = os.path.realpath(d)
            if any(real == s or real.startswith(s + os.sep) for s in shares):
                continue
            dirs.append(d)
        if dirs:
            out[folder] = dirs
    return out


class Locations:
    """The locations now, read each time: the host from a callable (ComfyUI
    can add paths) and the shares from the config."""

    def __init__(self, host, shares=lambda: {}, host_root=None):
        self._host = host
        self._shares = shares
        self._host_root = host_root  # ComfyUI's models dir, or a callable answering it

    def all(self):
        """{location: {folder: [dirs]}}, host first, shares by name."""
        shares = self._shares()
        host = self._host(list(shares.values())) if callable(self._host) else self._host
        out = {HOST: host}
        for name in sorted(shares):
            out[name] = share_folders(shares[name])
        return out

    def is_share(self, location):
        return location != HOST

    def root(self, location):
        """The location's models root: the share's path, or ComfyUI's
        models dir for host (None when it wasn't given)."""
        if location == HOST:
            return self._host_root() if callable(self._host_root) else self._host_root
        return self._shares().get(location)


def _within(real_root, path):
    """path is real_root itself or below it."""
    return path == real_root or path.startswith(real_root.rstrip(os.sep) + os.sep)


def walk_folder(dirs):
    """Every regular file of one folder type as (path, stat), sorted by
    path. A path found in an earlier directory hides the same path in a
    later one, as ComfyUI resolves it. Symlinks, to files or directories, are
    never followed or listed."""
    seen = {}
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [n for n in dirnames
                           if n != PARTIAL and not os.path.islink(os.path.join(dirpath, n))]
            rel_dir = os.path.relpath(dirpath, d)
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if not _is_regular(st):
                    continue
                rel = fn if rel_dir == "." else os.path.join(rel_dir, fn).replace(os.sep, "/")
                seen.setdefault(rel, st)
    return sorted(seen.items())


def _is_regular(st):
    return stat.S_ISREG(st.st_mode)


def walk(locations, location=None, folder=None):
    """(location, folder, path, stat) for every model file, sorted."""
    out = []
    for loc, folders in locations.items():
        if location is not None and loc != location:
            continue
        for f in sorted(folders):
            if folder is not None and f != folder:
                continue
            for path, st in walk_folder(folders[f]):
                out.append((loc, f, path, st))
    out.sort(key=lambda e: (e[0], e[1], e[2]))
    return out


def cursor(location, folder, path):
    return f"{location}/{folder}/{path}"


def _parse_cursor(after):
    parts = (after or "").split("/", 2)
    if len(parts) != 3:
        return None
    return tuple(parts)


def entries(locations, location=None, folder=None):
    """(location, folder, path, size, mtime) for every model file, walked
    now and sorted."""
    return [(loc, f, p, st.st_size, st.st_mtime) for loc, f, p, st in walk(locations, location, folder)]


def list_models(locations, index, location=None, folder=None, after="", limit=MAX_LIMIT, found=None):
    """A page of model files after the cursor `after`, each with its hash
    and when it was last verified once the index has them. next is the last
    file's cursor when more follow. found is the hasher's last scan, as
    entries() answers; without it the locations are walked now."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    rows = entries(locations, location, folder) if found is None else sorted(found)
    if after:
        start = _parse_cursor(after)
        if start is not None:
            rows = [e for e in rows if (e[0], e[1], e[2]) > start]
    more = len(rows) > limit
    rows = rows[:limit]
    files = []
    for loc, f, path, size, mtime in rows:
        known = index.lookup(loc, f, path, size, mtime) if index is not None else None
        files.append({
            "location": loc, "folder": f, "path": path, "size": size, "mtime": mtime,
            "sha256": known.get("sha256") if known else None,
            "verifiedAt": known.get("verifiedAt") if known else None,
        })
    out = {"files": files}
    if more:
        out["next"] = cursor(*rows[-1][:3])
    return out


def split_path(path):
    """A model path's parts, or None: a plain relative path with /, no
    empty, . or .. parts, no backslash or NUL, and nothing under
    .couchy-partial."""
    if not isinstance(path, str) or not path or "\0" in path or "\\" in path:
        return None
    parts = path.split("/")
    if path.startswith("/") or any(p in ("", ".", "..") for p in parts) or PARTIAL in parts:
        return None
    return parts


def resolve(dirs, path):
    """The real path of a model file of one folder type, searched in
    ComfyUI's order, or None. Refused: anything but a plain relative path,
    any symlink on the way (the file or a directory under the folder's own
    directory) and anything outside the directory."""
    parts = split_path(path)
    if parts is None:
        return None
    for d in dirs:
        if not os.path.isdir(d):
            continue
        real_root = os.path.realpath(d)
        candidate = os.path.join(real_root, *parts)
        real = os.path.realpath(candidate)
        if real != candidate or not _within(real_root, real) or real == real_root:
            continue  # a symlink on the way, or it leaves the folder
        try:
            st = os.lstat(real)
        except OSError:
            continue
        if _is_regular(st):
            return real
    return None


class NotSafetensors(ValueError):
    """The file isn't a .safetensors file, or its header is over the cap."""


def read_header(path, cap=HEADER_CAP, tensor_cap=TENSOR_CAP):
    """A .safetensors file's header: {metadata, tensors: [{name, dtype,
    shape}], tensorCount, truncated}. The format is an 8-byte little-endian
    length, then that many bytes of JSON; only those are read."""
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        head = f.read(8)
        if len(head) < 8:
            raise NotSafetensors("too short for a safetensors header")
        (n,) = struct.unpack("<Q", head)
        if n > cap:
            raise NotSafetensors(f"the header is over {cap} bytes")
        if n < 2 or 8 + n > size:
            raise NotSafetensors("not a safetensors file")
        raw = f.read(n)
    if len(raw) != n:
        raise NotSafetensors("not a safetensors file")
    try:
        data = json.loads(raw.decode("utf-8").rstrip(" "))
    except (UnicodeDecodeError, ValueError):
        raise NotSafetensors("not a safetensors file")
    if not isinstance(data, dict):
        raise NotSafetensors("not a safetensors file")
    meta = data.pop("__metadata__", None)
    if meta is not None and not isinstance(meta, dict):
        raise NotSafetensors("not a safetensors file")
    tensors = []
    for name, t in data.items():
        if not isinstance(t, dict) or not isinstance(t.get("dtype"), str) or not isinstance(t.get("shape"), list):
            raise NotSafetensors("not a safetensors file")
        tensors.append({"name": name, "dtype": t["dtype"], "shape": t["shape"]})
    count = len(tensors)
    return {"metadata": meta or {}, "tensors": tensors[:tensor_cap], "tensorCount": count, "truncated": count > tensor_cap}


def space(locations):
    """Free and total bytes for each volume that holds a models folder, per
    location: {volumes: [{location, folders, freeBytes, totalBytes}]}."""
    volumes = {}
    for loc, folders in locations.items():
        for folder in sorted(folders):
            for d in folders[folder]:
                if not os.path.isdir(d):
                    continue
                try:
                    dev = os.stat(d).st_dev
                except OSError:
                    continue
                v = volumes.get((loc, dev))
                if v is None:
                    try:
                        du = shutil.disk_usage(d)
                    except OSError:
                        continue
                    v = volumes[(loc, dev)] = {"location": loc, "folders": [], "freeBytes": du.free,
                                               "totalBytes": du.total}
                if folder not in v["folders"]:
                    v["folders"].append(folder)
    order = list(locations)
    return {"volumes": sorted(volumes.values(), key=lambda v: (order.index(v["location"]), v["folders"]))}
