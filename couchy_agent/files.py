"""Files inside a tenant's namespace. Plain Python, no ComfyUI imports: the
caller passes the folder roots, which ComfyUI's folder_paths names.

The path helpers (_walk, resolve, delete_files, delete_older) are copied from
getcouchy/couchyforms-helper's files.py, not imported, so the two packs release
on their own. Their rules stay: every path is resolved, symlinks included, and
must stay inside its folder; symlinks are never listed; folders are never
deleted. On top of that, inside couchy/ only the caller's namespace is
reachable.

Layout:
    input/couchy/<namespace>/<owner>/<id>.png
    output/couchy/<namespace>/<owner>/<runId>/...
"""

import os
import re
import shutil
import time

MAX_LIMIT = 1000

# The folder every namespace lives under, in input, output and temp.
TOP = "couchy"

# The folders a namespace has. temp keeps no namespace.
NS_FOLDERS = ("input", "output")

# An owner is a Couchy user id or "lab"; a run id is Couchy's. Neither may
# hold a path separator or be a dot name.
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def valid_id(value):
    return isinstance(value, str) and bool(_ID.match(value))


def _walk(root):
    """Every regular file under root as (subfolder, filename, stat), sorted
    by path. Symlinks are skipped, so nothing outside root is listed."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            sub = os.path.relpath(dirpath, root)
            out.append(("" if sub == "." else sub.replace(os.sep, "/"), fn, st))
    out.sort(key=lambda e: (e[0], e[1]))
    return out


def _key(subfolder, filename):
    return f"{subfolder}/{filename}" if subfolder else filename


def resolve(root, subfolder, filename):
    """The real path of subfolder/filename inside root, or None when it
    leaves root, names no file, or is not a plain name."""
    if not isinstance(filename, str) or not isinstance(subfolder, str):
        return None
    if not filename or filename in (".", "..") or "/" in filename or "\\" in filename or "\0" in filename + subfolder:
        return None
    real_root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(real_root, subfolder, filename))
    if os.path.commonpath([real_root, path]) != real_root or path == real_root:
        return None
    return path


def namespace_of(root, path):
    """The namespace a real path inside root sits in, lowercased: the folder
    after couchy/. "" for a path inside couchy/ but in no namespace (couchy/
    itself or a file directly in it), None for a path outside couchy/. The
    match ignores case, since macOS folders do."""
    rel = os.path.relpath(path, os.path.realpath(root))
    parts = rel.split(os.sep)
    if not parts or parts[0].lower() != TOP or rel.startswith(".."):
        return None
    if len(parts) < 2:
        return ""
    return parts[1].lower()


def ns_root(root, namespace):
    return os.path.join(root, TOP, namespace)


def resolve_in(root, namespace, subfolder, filename):
    """resolve, then also refuse anything outside couchy/<namespace>/. The
    subfolder is relative to root, as ComfyUI's /view names it."""
    path = resolve(root, subfolder, filename)
    if path is None or namespace_of(root, path) != namespace.lower():
        return None
    if len(os.path.relpath(path, os.path.realpath(root)).split(os.sep)) < 3:
        return None  # the namespace folder itself
    return path


def _ns_walk(root, namespace, owner=None):
    """Every file in the namespace (or one owner's part of it) as
    (subfolder relative to root, filename, stat)."""
    base = ns_root(root, namespace)
    if owner is not None:
        base = os.path.join(base, owner)
    if not os.path.isdir(base) or os.path.islink(base):
        return []
    prefix = os.path.relpath(base, root).replace(os.sep, "/")
    return [(f"{prefix}/{s}" if s else prefix, n, st) for s, n, st in _walk(base)]


def usage(roots, namespace):
    """Count and bytes per folder and per owner. Never file names."""
    out = {}
    for folder in NS_FOLDERS:
        owners = {}
        count = size = 0
        base = ns_root(roots[folder], namespace)
        for sub, _, st in (_walk(base) if os.path.isdir(base) and not os.path.islink(base) else []):
            owner = sub.split("/", 1)[0]
            if not owner:
                continue  # a stray file directly in the namespace belongs to no one
            o = owners.setdefault(owner, {"count": 0, "bytes": 0})
            o["count"] += 1
            o["bytes"] += st.st_size
            count += 1
            size += st.st_size
        out[folder] = {"count": count, "bytes": size, "owners": owners}
    return out


def list_files(root, namespace, owner, after="", limit=MAX_LIMIT):
    """A page of one owner's files after the path `after`, with the owner's
    totals. Subfolders are relative to root, ready for /view. next is the last
    path of the page when more follow."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    files = _ns_walk(root, namespace, owner)
    total = {"count": len(files), "bytes": sum(st.st_size for _, _, st in files)}
    page = [f for f in files if _key(f[0], f[1]) > after] if after else files
    more = len(page) > limit
    page = page[:limit]
    out = {
        "files": [{"subfolder": s, "filename": n, "size": st.st_size, "mtime": st.st_mtime} for s, n, st in page],
        "total": total,
    }
    if more:
        out["next"] = _key(page[-1][0], page[-1][1])
    return out


def delete_files(root, namespace, files):
    """Delete each {subfolder, filename} inside the namespace. Answers which
    went, which were already gone and which were refused."""
    out = {"deleted": [], "missing": [], "refused": []}
    for f in files:
        sub, name = (f.get("subfolder") or ""), f.get("filename")
        ref = {"subfolder": sub, "filename": name}
        path = resolve_in(root, namespace, sub, name)
        if path is None:
            out["refused"].append(ref)
            continue
        if not os.path.lexists(path):
            out["missing"].append(ref)
            continue
        if not os.path.isfile(path):
            out["refused"].append(ref)
            continue
        try:
            os.remove(path)
            out["deleted"].append(ref)
        except FileNotFoundError:
            out["missing"].append(ref)
        except OSError:
            out["refused"].append(ref)
    return out


def _delete_walked(root, walked, keep=lambda st: False):
    count = size = 0
    for sub, name, st in walked:
        if keep(st):
            continue
        try:
            os.remove(os.path.join(root, sub, name))
            count += 1
            size += st.st_size
        except OSError:
            pass
    return {"deleted": count, "bytes": size}


def delete_owner(root, namespace, owner):
    """Delete every file of one owner. Answers the count and bytes removed."""
    return _delete_walked(root, _ns_walk(root, namespace, owner))


def delete_older(root, namespace, older_than, now=None):
    """Delete every file in the namespace last changed more than older_than
    seconds ago. Answers the count and bytes removed."""
    cutoff = (time.time() if now is None else now) - float(older_than)
    return _delete_walked(root, _ns_walk(root, namespace), keep=lambda st: st.st_mtime >= cutoff)


def _free_name(directory, filename):
    """filename in directory, or name_1.ext, name_2.ext ... when taken."""
    stem, ext = os.path.splitext(filename)
    candidate, n = filename, 0
    while os.path.lexists(os.path.join(directory, candidate)):
        n += 1
        candidate = f"{stem}_{n}{ext}"
    return candidate


def _mkdirs(path):
    os.makedirs(path, mode=0o700, exist_ok=True)


def _move(src, dst):
    """Rename, or copy then remove across file systems. Never overwrites:
    the caller picked a free name."""
    try:
        os.rename(src, dst)
    except OSError as e:
        if getattr(e, "errno", None) != 18:  # EXDEV: another file system
            raise
        shutil.copy2(src, dst)
        os.remove(src)


# The folders adopt takes a file from. input is left alone: Couchy's uploads
# stay where they are until the API moves them itself.
ADOPT_FROM = ("output", "temp")


def adopt(roots, namespace, src, owner, run_id):
    """Move one file from outside any namespace into
    output/couchy/<namespace>/<owner>/<runId>/. Answers {"state": "adopted",
    "to": {...}}, {"state": "missing"} when the file is gone (a second adopt
    of the same file), or {"state": "refused"}."""
    if not isinstance(src, dict) or not valid_id(owner) or not valid_id(run_id):
        return {"state": "refused"}
    folder = src.get("folder")
    if folder not in ADOPT_FROM:
        return {"state": "refused"}
    root = roots[folder]
    path = resolve(root, src.get("subfolder") or "", src.get("filename"))
    if path is None or namespace_of(root, path) is not None:
        # Outside the folder, or already inside couchy/: another tenant's
        # file, or this tenant's own, which adopt has no business moving.
        return {"state": "refused"}
    if not os.path.lexists(path):
        return {"state": "missing"}
    if not os.path.isfile(path) or os.path.islink(path):
        return {"state": "refused"}
    out_root = roots["output"]
    dest_dir = os.path.join(ns_root(out_root, namespace), owner, run_id)
    _mkdirs(dest_dir)
    name = _free_name(dest_dir, os.path.basename(path))
    try:
        _move(path, os.path.join(dest_dir, name))
    except FileNotFoundError:
        return {"state": "missing"}
    except OSError:
        return {"state": "refused"}
    sub = os.path.relpath(dest_dir, out_root).replace(os.sep, "/")
    return {"state": "adopted", "to": {"folder": "output", "subfolder": sub, "filename": name}}


def move_owner(roots, namespace, src_owner, dst_owner):
    """Re-home every file of one owner to another, in input and output.
    Folders are never removed, so the old owner's empty folders stay.
    Answers how many files moved in each folder."""
    if not valid_id(src_owner) or not valid_id(dst_owner) or src_owner == dst_owner:
        return None
    out = {}
    for folder in NS_FOLDERS:
        root = roots[folder]
        base = ns_root(root, namespace)
        src = os.path.join(base, src_owner)
        moved = 0
        if os.path.isdir(src) and not os.path.islink(src):
            for sub, name, _ in _walk(src):
                dest_dir = os.path.join(base, dst_owner, sub)
                _mkdirs(dest_dir)
                try:
                    _move(os.path.join(src, sub, name), os.path.join(dest_dir, _free_name(dest_dir, name)))
                    moved += 1
                except OSError:
                    pass
        out[folder] = {"moved": moved}
    return out
