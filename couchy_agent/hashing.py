"""The models index and the background hasher.

The index maps {location, folder, path, size, mtime} to {sha256,
verifiedAt}. It lives in user/couchy-agent/models-index.json (0600), or in
the index dir the host chose (config --index-dir, store --index-dir), so a
models folder that outlives the host keeps its index beside it (a RunPod
network volume, mounted as ComfyUI's models folder on each new pod). A
restart rehashes only files whose size or mtime changed, and an index that
is missing or can't be read is rebuilt in the background.

Hashing never runs on a request. One daemon thread scans the locations (the
host every minute, shares every five, since a NAS scan is slower and
changes less) and hashes one file at a time, at low priority: new or changed
files first, then weekly re-verifies, oldest first. A re-verify that still
matches sets verifiedAt; a mismatch clears sha256 and verifiedAt, keeps the
file out of the queue until its size or mtime changes, and logs it. The API's
eviction rules need a cold copy verified within 30 days.
"""

import hashlib
import json
import logging
import os
import sys
import threading
import time

from . import models
from .obs import sha12

INDEX_FILE = "models-index.json"

HOST_EVERY = 60
SHARE_EVERY = 5 * 60
VERIFY_EVERY = 7 * 24 * 3600
CHUNK = 4 * 1024 * 1024
IDLE = 5

log = logging.getLogger("couchy_agent.models")


class Index:
    def __init__(self, store, dir=None):
        self.store = store
        self.dir = dir or store.dir
        self._lock = threading.Lock()
        self._files = {}  # (location, folder, path) -> {size, mtime, sha256, verifiedAt, mismatchAt?}
        self.load()

    @property
    def file(self):
        return os.path.join(self.dir, INDEX_FILE)

    def _read(self):
        try:
            with open(self.file) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("models index unreadable; rebuilding it", extra={"error": e.__class__.__name__})
            return {}

    def load(self):
        data = self._read()
        files = {}
        for e in data.get("files", []) if isinstance(data, dict) else []:
            try:
                key = (e["location"], e["folder"], e["path"])
                files[key] = {"size": int(e["size"]), "mtime": float(e["mtime"]), "sha256": e.get("sha256"),
                              "verifiedAt": e.get("verifiedAt"), "mismatchAt": e.get("mismatchAt")}
            except (KeyError, TypeError, ValueError):
                continue
        with self._lock:
            self._files = files

    def save(self):
        with self._lock:
            rows = [{"location": k[0], "folder": k[1], "path": k[2], **v} for k, v in sorted(self._files.items())]
        text = json.dumps({"version": 1, "files": rows}, indent=2) + "\n"
        with self.store._locked():
            _write_atomic(self.dir, INDEX_FILE, text)

    def get(self, location, folder, path):
        with self._lock:
            e = self._files.get((location, folder, path))
            return dict(e) if e else None

    def lookup(self, location, folder, path, size, mtime):
        """The entry when it still describes the file on disk, else None."""
        e = self.get(location, folder, path)
        if e is None or e["size"] != size or e["mtime"] != mtime:
            return None
        return e

    def put(self, location, folder, path, entry):
        with self._lock:
            self._files[(location, folder, path)] = dict(entry)

    def keys(self, location):
        with self._lock:
            return [k for k in self._files if k[0] == location]

    def drop(self, keys):
        with self._lock:
            for k in keys:
                self._files.pop(k, None)

    def with_sha(self, sha256, location=None):
        """[(location, folder, path, entry)] for every file indexed with
        this hash, in the location if given."""
        with self._lock:
            return [(k[0], k[1], k[2], dict(v)) for k, v in sorted(self._files.items())
                    if v.get("sha256") == sha256 and (location is None or k[0] == location)]

    def shas(self):
        """{(location, folder, path): sha256} for every hashed file."""
        with self._lock:
            return {k: v["sha256"] for k, v in self._files.items() if v.get("sha256")}


def _write_atomic(dir, name, text):
    """name in dir, 0600, written to a temp file in the same dir and
    renamed over it, so a crash or a second reader never sees half."""
    os.makedirs(dir, mode=0o700, exist_ok=True)
    tmp = os.path.join(dir, f".{name}.{os.getpid()}.{threading.get_native_id()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        data = text.encode()
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.remove(tmp)
        raise
    os.close(fd)
    os.replace(tmp, os.path.join(dir, name))


class Hasher:
    def __init__(self, locations, index, clock=time.time, host_every=HOST_EVERY, share_every=SHARE_EVERY,
                 verify_every=VERIFY_EVERY, chunk=CHUNK):
        self.locations = locations  # models.Locations
        self.index = index
        self.clock = clock
        self.host_every = host_every
        self.share_every = share_every
        self.verify_every = verify_every
        self.chunk = chunk
        self._seen = {}  # location -> {(folder, path): (size, mtime)}
        self._seen_lock = threading.Lock()
        self._scanned = {}  # location -> when
        self._thread = None
        self._stop = threading.Event()

    # Scanning --------------------------------------------------------------

    def _every(self, location):
        return self.host_every if location == models.HOST else self.share_every

    def scan(self, force=False):
        """Walk each location that's due. Index entries for files that are
        gone are dropped, except when a location turns up empty or missing
        (a share that didn't mount), so its hashes survive the outage."""
        now = self.clock()
        locs = self.locations.all()
        with self._seen_lock:
            for gone in set(self._seen) - set(locs):
                self._seen.pop(gone, None)
                self._scanned.pop(gone, None)
        changed = False
        for loc, folders in locs.items():
            if not force and now - self._scanned.get(loc, -1e18) < self._every(loc):
                continue
            started = time.monotonic()
            seen = {(f, p): (st.st_size, st.st_mtime) for _, f, p, st in models.walk({loc: folders})}
            self._scanned[loc] = now
            if not seen and self.index.keys(loc):
                log.warning("models location empty or missing; kept its index",
                            extra={"location": loc, "indexed": len(self.index.keys(loc))})
                with self._seen_lock:
                    self._seen[loc] = {}
                continue
            stale = [k for k in self.index.keys(loc) if (k[1], k[2]) not in seen]
            if stale:
                self.index.drop(stale)
                changed = True
            with self._seen_lock:
                self._seen[loc] = seen
            log.info("models scanned", extra={"location": loc, "files": len(seen),
                                              "ms": int((time.monotonic() - started) * 1000)})
        if changed:
            self.index.save()

    def found(self, locations, location=None, folder=None):
        """The inventory as of each location's last scan, as models.entries
        answers, so a listing doesn't walk a NAS on every page. A location
        not scanned yet is walked now."""
        out = []
        for loc, folders in locations.items():
            if location is not None and loc != location:
                continue
            with self._seen_lock:
                seen = dict(self._seen[loc]) if loc in self._seen else None
            if seen is None:
                out += models.entries({loc: folders}, loc, folder)
                continue
            out += [(loc, f, p, size, mtime) for (f, p), (size, mtime) in seen.items()
                    if folder is None or f == folder]
        return out

    def pending(self):
        """What to hash next, in order: [(location, folder, path, why)],
        why being "new" or "verify"."""
        now = self.clock()
        new, verify = [], []
        with self._seen_lock:
            snapshot = {loc: dict(seen) for loc, seen in self._seen.items()}
        for loc, seen in snapshot.items():
            for (f, p), (size, mtime) in sorted(seen.items()):
                e = self.index.get(loc, f, p)
                if e is None or e["size"] != size or e["mtime"] != mtime:
                    new.append((loc, f, p, "new"))
                elif e.get("sha256") and (e.get("verifiedAt") or 0) <= now - self.verify_every:
                    verify.append((e.get("verifiedAt") or 0, (loc, f, p, "verify")))
        return new + [v for _, v in sorted(verify)]

    # Hashing ---------------------------------------------------------------

    def _hash(self, path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while not self._stop.is_set():
                chunk = f.read(self.chunk)
                if not chunk:
                    return h.hexdigest()
                h.update(chunk)
                time.sleep(0)  # let ComfyUI's threads in between chunks
        return None

    def step(self):
        """Hash the next file. True when there was one."""
        work = self.pending()
        if not work:
            return False
        self.hash_one(*work[0])
        return True

    def hash_one(self, location, folder, path, why):
        folders = self.locations.all().get(location, {})
        real = models.resolve(folders.get(folder, []), path)
        if real is None:
            self._forget(location, folder, path)  # gone, or now a symlink: the next scan settles it
            return
        try:
            before = os.lstat(real)
            started = time.monotonic()
            sha = self._hash(real)
            after = os.lstat(real)
        except OSError:
            self._forget(location, folder, path)
            return
        if sha is None:
            return  # stopping
        sig = (before.st_size, before.st_mtime)
        with self._seen_lock:
            if location in self._seen:
                self._seen[location][(folder, path)] = (after.st_size, after.st_mtime)
        if sig != (after.st_size, after.st_mtime):
            return  # it changed while we read it: hash it again next time round
        ms = int((time.monotonic() - started) * 1000)
        now = self.clock()
        old = self.index.lookup(location, folder, path, before.st_size, before.st_mtime)
        if why == "verify" and old and old.get("sha256"):
            if old["sha256"] == sha:
                old["verifiedAt"] = now
                self.index.put(location, folder, path, old)
                log.info("model verified", extra={"location": location, "folder": folder,
                                                  "sha": sha12(sha), "bytes": before.st_size, "ms": ms})
            else:
                self.index.put(location, folder, path, {"size": before.st_size, "mtime": before.st_mtime,
                                                        "sha256": None, "verifiedAt": None, "mismatchAt": now})
                log.warning("model verify mismatch", extra={"location": location, "folder": folder,
                                                            "sha": sha12(old["sha256"]), "got": sha12(sha),
                                                            "bytes": before.st_size, "ms": ms})
        else:
            self.index.put(location, folder, path, {"size": before.st_size, "mtime": before.st_mtime,
                                                    "sha256": sha, "verifiedAt": now})
            log.info("model hashed", extra={"location": location, "folder": folder, "sha": sha12(sha),
                                            "bytes": before.st_size, "ms": ms})
        self.index.save()

    # Files the agent itself wrote or removed (pulls.py, a delete) --------

    def landed(self, location, folder, path, st, sha256):
        """A file the agent just wrote and verified: known by its hash at
        once, without hashing it again."""
        self.index.put(location, folder, path, {"size": st.st_size, "mtime": st.st_mtime, "sha256": sha256,
                                                "verifiedAt": self.clock()})
        with self._seen_lock:
            if location in self._seen:
                self._seen[location][(folder, path)] = (st.st_size, st.st_mtime)
        self.index.save()

    def removed(self, location, folder, path):
        self.index.drop([(location, folder, path)])
        self._forget(location, folder, path)
        self.index.save()

    def _forget(self, location, folder, path):
        with self._seen_lock:
            self._seen.get(location, {}).pop((folder, path), None)

    # The thread ------------------------------------------------------------

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="couchy-agent-hasher", daemon=True)
            self._thread.start()
        return self._thread

    def stop(self):
        self._stop.set()

    def _run(self):
        _lower_priority()
        while not self._stop.is_set():
            try:
                self.scan()
                if not self.step():
                    self._stop.wait(IDLE)
            except Exception as e:  # keep hashing whatever one file or scan does
                log.warning("models hasher error", extra={"error": e.__class__.__name__})
                self._stop.wait(IDLE)


def _lower_priority():
    """Nice this thread only. On Linux a thread has its own nice value; on
    macOS setpriority would renice ComfyUI itself, so it's left alone."""
    if sys.platform.startswith("linux"):
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
        except (AttributeError, OSError):
            pass
