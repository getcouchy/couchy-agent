"""Pulls: a model file fetched from Civitai, Hugging Face, a peer agent or
a bucket (a presigned URL) into a location (host or a share), verified, then renamed into place.

Mechanism only: the API says what to fetch and where. The agent refuses
what its host doesn't allow (formats, hosts, room) and never evicts.

- A pull streams into .couchy-partial/<id> under its location's root, or
  under the target folder when that's on another volume, so the rename
  stays on one volume. It hashes as it goes, then checks size and SHA-256,
  and renames without replacing anything.
- Its id is derived from what it pulls and where, so the API starting the
  same pull again after a restart (when the pull answers 404) resumes from
  the partial file, with Range when the source allows it.
- At most two pulls run at once; the rest queue.
- A presigned URL (a bucket object, minted by the API for this pull) is
  fetched only from a host in allowed-hosts, and never follows a redirect.
- Credentials (headers, a Civitai token in the URL, a peer's ticket, a
  presigned URL and its signature) live
  in the Pull in memory for that pull only. They're never written to disk
  or logged, and never sent on to another host on a redirect.
"""

import asyncio
import ctypes
import errno
import hashlib
import logging
import os
import re
import shutil
import time
import urllib.parse

from . import models
from .obs import sha12

log = logging.getLogger("couchy_agent.models")

SAFE_FORMATS = (".safetensors", ".gguf", ".sft")
# These run code when ComfyUI loads them: only with allowPickle on the host.
PICKLE_FORMATS = (".ckpt", ".pt", ".pth", ".bin")

# Hosts a pull may fetch from with no config, over HTTPS: each and its
# subdomains (cdn-lfs.huggingface.co, cas-bridge.xethub.hf.co, b2.civitai.com).
KNOWN_HOSTS = ("civitai.com", "huggingface.co", "hf.co")
# CDNs they redirect to outside those names: Civitai's download bucket.
KNOWN_CDNS = (re.compile(r"^civitai-delivery-worker-prod\.[0-9a-f]{32}\.r2\.cloudflarestorage\.com$"),)

TICKET_HEADER = "X-Couchy-Agent-Ticket"

# A safetensors header may be this long when it's read only to tell a model
# file from a web page: some models' are over read_header's 1 MB.
UNHASHED_HEADER_CAP = 100 * 1024 * 1024

MAX_RUNNING = 2
MAX_REDIRECTS = 5
CHUNK = 4 * 1024 * 1024
KEEP_FINISHED = 24 * 3600

_SHA = re.compile(r"^[0-9a-f]{64}$")
_REDIRECTS = (301, 302, 303, 307, 308)

STATES = ("queued", "running", "verifying", "done", "failed")


class BadPull(ValueError):
    """The request itself is malformed: answered 400, no pull is made."""


class Failed(Exception):
    def __init__(self, code, keep_partial=False):
        super().__init__(code)
        self.code = code
        self.keep_partial = keep_partial


def valid_sha(value):
    return isinstance(value, str) and bool(_SHA.match(value))


def pull_id(sha256, location, folder, path):
    h = hashlib.sha256("\0".join((sha256, location, folder, path)).encode()).hexdigest()
    return "pull_" + h[:24]


def file_format(path):
    """safe, pickle or None for anything else."""
    ext = os.path.splitext(path)[1].lower()
    if ext in SAFE_FORMATS:
        return "safe"
    if ext in PICKLE_FORMATS:
        return "pickle"
    return None


def _under(host, name):
    return host == name or host.endswith("." + name)


def _url_host(url):
    """(scheme, host) of an http(s) URL with no user info, else None."""
    try:
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not host or u.username or u.password:
        return None
    return u.scheme, host


def in_allowed_hosts(url, allowed):
    """The URL's host is in allowed-hosts, or under one: the only check a
    presigned URL gets, since no bucket host is known in advance."""
    sh = _url_host(url)
    return sh is not None and any(_under(sh[1], a) for a in allowed)


def host_allowed(url, allowed=()):
    """A URL a pull may fetch: a known host over HTTPS, or a host in the
    host's allowed-hosts (or a subdomain of one) over HTTP or HTTPS."""
    sh = _url_host(url)
    if sh is None:
        return False
    scheme, host = sh
    if scheme == "https" and (any(_under(host, k) for k in KNOWN_HOSTS) or any(p.match(host) for p in KNOWN_CDNS)):
        return True
    return any(_under(host, a) for a in allowed)


def _origin(url):
    u = urllib.parse.urlsplit(url)
    return (u.scheme, (u.hostname or "").lower(), u.port)


# Renaming without replacing ---------------------------------------------------

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
try:
    _renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
except (OSError, AttributeError):
    _renameat2 = None

_UNSUPPORTED = {errno.EINVAL, errno.ENOSYS, errno.EPERM, errno.EXDEV, errno.EMLINK,
                getattr(errno, "ENOTSUP", -1), getattr(errno, "EOPNOTSUPP", -1)}


def rename_noreplace(src, dst):
    """Move src to dst, raising FileExistsError when dst exists. renameat2
    with RENAME_NOREPLACE where the kernel and filesystem have it, else a
    hard link then unlink, else (a CIFS mount with no hard links) claim the
    name with O_EXCL and move onto that claim."""
    if _renameat2 is not None:
        if _renameat2(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE) == 0:
            return
        e = ctypes.get_errno()
        if e == errno.EEXIST:
            raise FileExistsError(e, os.strerror(e))
        if e not in _UNSUPPORTED:
            raise OSError(e, os.strerror(e))
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError as e:
        if e.errno not in _UNSUPPORTED:
            raise
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.close(fd)
        os.replace(src, dst)  # onto the empty file only this call made
        return
    os.unlink(src)


# The pulls ----------------------------------------------------------------------

class Pull:
    def __init__(self, id, sha256, location, folder, path, size, source):
        self.id = id
        self.sha256 = sha256  # None until it lands, for a link whose hash isn't known
        self.hashed = sha256 is not None
        self.location = location
        self.folder = folder
        self.path = path
        self.size = size  # None until the link says, when it isn't known
        self.sized = size is not None
        self._source = source  # memory only: never logged, never on disk
        self._link = link_of(source)
        self.state = "queued"
        self.bytes_done = 0
        self.error = None
        self.partial = None
        self.task = None
        self.finished_at = None

    def __repr__(self):
        return f"<Pull {self.id} {self.state}>"

    @property
    def active(self):
        return self.state in ("queued", "running", "verifying")

    def view(self):
        out = {"id": self.id, "state": self.state, "bytesDone": self.bytes_done, "bytesTotal": self.size or 0}
        if self.state == "done":
            out["sha256"] = self.sha256
        if self.error:
            out["error"] = self.error
        return out

    def _fields(self, **more):
        return {"pull": self.id, "location": self.location, "folder": self.folder, "sha": sha12(self.sha256), **more}


SOURCES = "source must be {url, headers?}, {peer, ticket} or {presigned}"


def parse_source(source):
    """{url, headers?}, {peer, ticket} or {presigned}, checked for shape
    only."""
    if not isinstance(source, dict):
        raise BadPull(SOURCES)
    if "presigned" in source:
        url = source.get("presigned")
        if not isinstance(url, str) or not url:
            raise BadPull("source.presigned must be a URL")
        return {"presigned": url}
    if "url" in source:
        url, headers = source.get("url"), source.get("headers") or {}
        if not isinstance(url, str) or not url:
            raise BadPull("source.url must be a URL")
        if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                    for k, v in headers.items()):
            raise BadPull("source.headers must be an object of strings")
        return {"url": url, "headers": dict(headers)}
    if "peer" in source:
        peer, ticket = source.get("peer"), source.get("ticket")
        if not isinstance(peer, str) or not peer or not isinstance(ticket, str) or not ticket:
            raise BadPull("a peer source is {peer: <agent URL>, ticket}")
        return {"peer": peer, "ticket": ticket}
    raise BadPull(SOURCES)


def link_of(source):
    """What tells one link from another, without its query, which holds a
    token that may change; None for a peer or a presigned URL."""
    if "url" not in source:
        return None
    u = urllib.parse.urlsplit(source["url"])
    return hashlib.sha256("\0".join((u.scheme, u.netloc, u.path)).encode()).hexdigest()


def check_model_file(path, name):
    """Failed unless the file at path is what name says it is: an unknown
    link can answer a sign-in page. A pickle has no such mark, and only a host
    that allows them pulls one."""
    ext = os.path.splitext(name)[1].lower()
    if ext == ".gguf":
        with open(path, "rb") as f:
            ok = f.read(4) == b"GGUF"
    elif ext in SAFE_FORMATS:
        try:
            models.read_header(path, cap=UNHASHED_HEADER_CAP)
            ok = True
        except models.NotSafetensors:
            ok = False
    else:
        ok = os.path.getsize(path) > 0
    if not ok:
        raise Failed("not_a_model")


class Pulls:
    def __init__(self, locations, hasher, config, max_running=MAX_RUNNING, chunk=CHUNK, clock=time.time,
                 session=None):
        self.locations = locations  # models.Locations
        self.hasher = hasher  # hashing.Hasher: its index, and landed()
        self.config = config  # config.Config: allowed_hosts(), allow_pickle(), reserve_bytes()
        self.max_running = max_running
        self.chunk = chunk
        self.clock = clock
        self._session = session
        self._sem = None
        self._pulls = {}

    # Starting, reading, cancelling ------------------------------------------------

    def start(self, body, locs):
        """Start a pull from the route's body, or answer the one already
        running for the same file and target. Raises BadPull."""
        source = parse_source(body.get("source"))
        # A link may come without what its file is: it's hashed as it lands.
        sha = body.get("sha256") or (None if "url" in source else "")
        if sha is not None and not valid_sha(sha):
            raise BadPull("sha256 must be 64 lowercase hex characters")
        location = body.get("location", models.HOST)
        if location not in locs:
            raise BadPull(f"location must be one of {', '.join(locs)}")
        folder = body.get("folder")
        if folder not in locs[location]:
            raise BadPull("folder must be a ComfyUI models folder, such as checkpoints or loras")
        path = body.get("path")
        if models.split_path(path) is None:
            raise BadPull("path must be a plain relative path, with /")
        size = body.get("size")
        if (size is not None or "url" not in source) and (
                not isinstance(size, int) or isinstance(size, bool) or size <= 0):
            raise BadPull("size must be the file's size in bytes")
        pid = pull_id(sha or "", location, folder, path)
        old = self._pulls.get(pid)
        if old is not None and (old.active or self._landed(old, source, locs)):
            return old
        self._prune()
        pull = self._pulls[pid] = Pull(pid, sha, location, folder, path, size, source)
        pull.task = asyncio.get_running_loop().create_task(self._run(pull))
        return pull

    def _landed(self, old, source, locs):
        """Whether old pulled this link with no known hash and its file is
        still there: asked again after a restart of the API, it's done."""
        if old.hashed or old.state != "done" or old._link is None or old._link != link_of(source):
            return False
        real = models.resolve(locs[old.location].get(old.folder, []), old.path)
        if real is None:
            return False
        st = os.lstat(real)
        known = self.hasher.index.lookup(old.location, old.folder, old.path, st.st_size, st.st_mtime)
        return bool(known) and known.get("sha256") == old.sha256

    def get(self, pid):
        return self._pulls.get(pid)

    async def cancel(self, pid):
        """Stop a pull and remove its partial file. None when there's no
        such pull."""
        pull = self._pulls.get(pid)
        if pull is None:
            return None
        if pull.task is not None and not pull.task.done():
            pull.task.cancel()
            try:
                await pull.task
            except asyncio.CancelledError:
                pass
        if pull.partial:
            await asyncio.to_thread(_remove, pull.partial)
        if pull.state != "done":
            pull.state, pull.error = "failed", pull.error if pull.state == "failed" else "cancelled"
        pull._source = None
        return pull

    def _prune(self):
        now = self.clock()
        for pid, p in list(self._pulls.items()):
            if not p.active and p.finished_at is not None and now - p.finished_at > KEEP_FINISHED:
                del self._pulls[pid]

    async def close(self):
        for p in list(self._pulls.values()):
            if p.task is not None and not p.task.done():
                p.task.cancel()
        if self._session is not None:
            await self._session.close()

    # Running -------------------------------------------------------------------

    def _http(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120),
                auto_decompress=False)
        return self._session

    async def _run(self, pull):
        started = time.monotonic()
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_running)
        try:
            request = self._request(pull)  # refusals come before queueing
            async with self._sem:
                pull.state = "running"
                log.info("model pull started", extra=pull._fields(bytes=pull.size or 0))
                await self._fetch(pull, request)
        except asyncio.CancelledError:
            if pull.partial:
                await asyncio.to_thread(_remove, pull.partial)
            self._finish(pull, "failed", "cancelled", started)
            raise
        except Failed as e:
            if pull.partial and not e.keep_partial:
                await asyncio.to_thread(_remove, pull.partial)
            self._finish(pull, "failed", e.code, started)
        except Exception as e:  # a network or disk error: the partial file stays for a resume
            code = "io" if isinstance(e, OSError) and not _is_client_error(e) else "network"
            log.warning("model pull error", extra=pull._fields(error=e.__class__.__name__))
            self._finish(pull, "failed", code, started)
        else:
            self._finish(pull, "done", None, started)

    def _finish(self, pull, state, error, started):
        pull.state, pull.error = state, error
        pull.finished_at = self.clock()
        pull._source = None
        ms = int((time.monotonic() - started) * 1000)
        if state == "done":
            log.info("model pull done", extra=pull._fields(bytes=pull.bytes_done, ms=ms))
        else:
            log.warning("model pull failed", extra=pull._fields(error=error, bytes=pull.bytes_done, ms=ms))

    def _request(self, pull):
        """(url, headers, kind) for the pull, kind being url, peer or
        presigned, or Failed for what the host
        refuses."""
        fmt = file_format(pull.path)
        if fmt is None:
            raise Failed("format_refused")
        if fmt == "pickle" and not self.config.allow_pickle():
            raise Failed("pickle_refused")
        src = pull._source
        if "peer" in src:
            base = src["peer"].rstrip("/")
            u = urllib.parse.urlsplit(base)
            if u.scheme not in ("http", "https") or not u.hostname or u.query or u.fragment or u.username:
                raise Failed("bad_source")
            return base + "/couchy/models/blob/" + pull.sha256, {TICKET_HEADER: src["ticket"]}, "peer"
        if "presigned" in src:
            if not in_allowed_hosts(src["presigned"], self.config.allowed_hosts()):
                raise Failed("host_not_allowed")
            return src["presigned"], {}, "presigned"
        if not host_allowed(src["url"], self.config.allowed_hosts()):
            raise Failed("host_not_allowed")
        return src["url"], dict(src["headers"]), "url"

    async def _fetch(self, pull, request):
        url, headers, kind = request
        target, partial, have, sha = await asyncio.to_thread(self._prepare, pull)
        if target is None:
            return  # the same file is already there
        resp = None
        try:
            resp = await self._open(url, headers, kind, have)
            if resp.status == 206 and have and _range_start(resp) == have:
                pass  # resuming
            elif resp.status == 416 and have == pull.size:
                pass  # the partial file is already whole
            elif resp.status == 200 or (resp.status in (206, 416) and have):
                if have:  # the source ignored Range: start over
                    have, sha = 0, hashlib.sha256()
                    await asyncio.to_thread(_truncate, partial)
                if resp.status != 200:
                    resp.release()
                    resp = await self._open(url, headers, kind, 0)
                    if resp.status != 200:
                        raise Failed(_refusal(resp.status, kind), keep_partial=True)
            else:
                raise Failed(_refusal(resp.status, kind), keep_partial=True)
            pull.bytes_done = have
            if not pull.sized:
                pull.size = resp.content_length
                if pull.size is not None:
                    await asyncio.to_thread(self._check_room, partial, pull.size)
            if resp.status in (200, 206):
                await self._stream(pull, resp, partial, sha)
        finally:
            if resp is not None:
                resp.release()
        pull.state = "verifying"
        if pull.size is None:
            pull.size = pull.bytes_done
        if pull.bytes_done != pull.size:
            raise Failed("size_mismatch", keep_partial=pull.sized and pull.bytes_done < pull.size)
        if pull.hashed and sha.hexdigest() != pull.sha256:
            raise Failed("hash_mismatch")
        if not pull.hashed:
            await asyncio.to_thread(check_model_file, partial, pull.path)
            pull.sha256 = sha.hexdigest()
        await asyncio.to_thread(self._place, pull, partial, target)

    async def _open(self, url, headers, kind, offset):
        """GET url, following redirects by hand: each hop's host is checked
        again, and credentials go only to the URL's own origin."""
        session = self._http()
        origin = _origin(url)
        allowed = self.config.allowed_hosts()
        for _ in range(MAX_REDIRECTS + 1):
            h = dict(headers) if _origin(url) == origin else {}
            if offset:
                h["Range"] = f"bytes={offset}-"
            h.setdefault("Accept-Encoding", "identity")
            resp = await session.get(url, headers=h, allow_redirects=False)
            if resp.status not in _REDIRECTS:
                return resp
            location = resp.headers.get("Location", "")
            resp.release()
            if kind != "url" or not location:  # a peer or a presigned URL never redirects
                raise Failed("source_failed")
            url = urllib.parse.urljoin(url, location)
            if not host_allowed(url, allowed):
                raise Failed("host_not_allowed")
        raise Failed("too_many_redirects")

    async def _stream(self, pull, resp, partial, sha):
        fd = await asyncio.to_thread(os.open, partial, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(1024 * 1024):
                buf += chunk
                if pull.size is not None and pull.bytes_done + len(buf) > pull.size:
                    raise Failed("size_mismatch")
                if len(buf) >= self.chunk:
                    if pull.size is None:
                        await asyncio.to_thread(self._check_room, partial, len(buf))
                    await asyncio.to_thread(_write, fd, sha, bytes(buf))
                    pull.bytes_done += len(buf)
                    buf.clear()
            if buf:
                if pull.size is None:
                    await asyncio.to_thread(self._check_room, partial, len(buf))
                await asyncio.to_thread(_write, fd, sha, bytes(buf))
                pull.bytes_done += len(buf)
            await asyncio.to_thread(os.fsync, fd)
        finally:
            await asyncio.to_thread(os.close, fd)

    # On disk (run in a thread) ----------------------------------------------------

    def _check_room(self, partial, size):
        """Failed unless size more bytes leave the reserve free."""
        if shutil.disk_usage(os.path.dirname(partial)).free - size < self.config.reserve_bytes():
            raise Failed("no_room")

    def _folder_dirs(self, pull):
        locs = self.locations.all()
        dirs = locs.get(pull.location, {}).get(pull.folder)
        if not dirs:
            raise Failed("location_missing")
        return dirs

    def _same_file_there(self, pull, dirs):
        """True when the target already holds this hash; Failed exists
        when it holds anything else."""
        existing = models.resolve(dirs, pull.path)
        target = os.path.join(os.path.realpath(dirs[0]), *models.split_path(pull.path))
        if existing is None and not os.path.lexists(target):
            return False
        if existing is not None:
            st = os.lstat(existing)
            known = self.hasher.index.lookup(pull.location, pull.folder, pull.path, st.st_size, st.st_mtime)
            if known and pull.sha256 is not None and known.get("sha256") == pull.sha256:
                return True
        raise Failed("exists")

    def _prepare(self, pull):
        """(target, partial, bytes already there, running hash), or
        (None, ...) when the file is already in place."""
        dirs = self._folder_dirs(pull)
        if self._same_file_there(pull, dirs):
            if pull.size is None:
                pull.size = os.path.getsize(models.resolve(dirs, pull.path))
            pull.bytes_done = pull.size
            return None, None, 0, None
        folder_dir = dirs[0]
        root = self.locations.root(pull.location)
        if root is not None and not os.path.isdir(root):
            raise Failed("location_missing")
        os.makedirs(folder_dir, mode=0o755, exist_ok=True)
        real_dir = os.path.realpath(folder_dir)
        parts = models.split_path(pull.path)
        parent = real_dir
        for p in parts[:-1]:
            parent = os.path.join(parent, p)
            try:
                os.mkdir(parent, 0o755)
            except FileExistsError:
                pass
            if os.path.islink(parent) or not os.path.isdir(parent):
                raise Failed("exists")  # a symlink or a file in the way
        target = os.path.join(parent, parts[-1])

        pdir = os.path.join(real_dir, models.PARTIAL)
        if root is not None:
            real_root = os.path.realpath(root)
            if os.stat(real_root).st_dev == os.stat(real_dir).st_dev:
                pdir = os.path.join(real_root, models.PARTIAL)
        try:
            os.mkdir(pdir, 0o700)
        except FileExistsError:
            pass
        if os.path.islink(pdir) or not os.path.isdir(pdir):
            raise Failed("io")
        partial = pull.partial = os.path.join(pdir, pull.id)

        have = 0
        try:
            st = os.lstat(partial)
        except FileNotFoundError:
            st = None
        if st is not None:
            if not os.path.isfile(partial) or os.path.islink(partial):
                raise Failed("io")
            have = st.st_size
            # With no hash or size to tell whose bytes these are, start over.
            if not pull.hashed or not pull.sized or have > pull.size:
                _truncate(partial)
                have = 0
        free = shutil.disk_usage(pdir).free
        if free - ((pull.size or 0) - have) < self.config.reserve_bytes():
            raise Failed("no_room", keep_partial=True)
        sha = hashlib.sha256()
        if have:
            with open(partial, "rb") as f:
                while True:
                    chunk = f.read(self.chunk)
                    if not chunk:
                        break
                    sha.update(chunk)
            log.info("model pull resuming", extra=pull._fields(bytes=have))
        return target, partial, have, sha

    def _place(self, pull, partial, target):
        try:
            rename_noreplace(partial, target)
        except FileExistsError:
            if self._same_file_there(pull, self._folder_dirs(pull)):
                _remove(partial)
                return
            raise Failed("exists")  # never reached: _same_file_there raised
        self.hasher.landed(pull.location, pull.folder, pull.path, os.lstat(target), pull.sha256)


def _is_client_error(e):
    try:
        import aiohttp
    except ImportError:
        return False
    return isinstance(e, aiohttp.ClientError)


def _refusal(status, kind):
    """The error for a source that answered status. The partial file stays:
    a new ticket or a fixed token resumes it."""
    if kind == "peer" and status in (401, 403):
        return "ticket_refused"
    if status in (404, 410):
        return "not_found"
    if status in (401, 403):
        return "source_refused"
    return "source_failed"


def _range_start(resp):
    m = re.match(r"^bytes (\d+)-", resp.headers.get("Content-Range", ""))
    return int(m.group(1)) if m else None


def _write(fd, sha, data):
    sha.update(data)
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _truncate(path):
    with open(path, "r+b") as f:
        f.truncate(0)


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
