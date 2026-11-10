"""The agent's host-side config, in user/couchy-agent/config.json (0600).
Set only from a shell on the host, with `python -m couchy_agent config`:
no route reads or writes it.

- shares: {name: path}, model roots mounted on the host (a NAS over CIFS or
  NFS) that use ComfyUI's models layout (checkpoints/, loras/, ...). Each is
  its own location next to ComfyUI's own models root, which is "host".
- allowedHosts: [host], hosts a pull may fetch from beyond the known ones
  (pulls.KNOWN_HOSTS). "example.com" also allows its subdomains.
- allowPickle: true lets a pull fetch pickle formats (.ckpt, .pt, .pth,
  .bin), which run code when ComfyUI loads them. Off by default.
- reserveBytes: the free space a pull must leave on its volume (default
  20 GB).
- indexDir: an absolute folder for the models hash index instead of
  user/couchy-agent/, such as one beside a models folder on a network
  volume that outlives the host. Read at start.

The running agent re-reads the file when it changes, so a new share shows up
on the next scan without a restart.
"""

import json
import os
import re

FILE = "config.json"

HOST = "host"

# A share's name is its location in every listing: short, lowercase, no
# separators, and never "host".
_SHARE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def parse_share(text):
    """name=path, as --share takes it. Answers (name, path) or raises
    ValueError with a message for the operator."""
    name, sep, path = (text or "").partition("=")
    name = name.strip()
    path = path.strip()
    if not sep or not name or not path:
        raise ValueError(f"--share takes name=path, not {text!r}")
    if not _SHARE_NAME.match(name) or name == HOST:
        raise ValueError(f"a share name is lowercase letters, digits, - and _, up to 32, and not {HOST!r}: {name!r}")
    if not os.path.isabs(path):
        raise ValueError(f"a share's path must be absolute: {path!r}")
    return name, os.path.normpath(path)


def valid_shares(shares):
    """The well-formed entries of a shares dict; anything else is dropped,
    so a hand-edited file can't add a location named host or a relative
    path."""
    out = {}
    if not isinstance(shares, dict):
        return out
    for name, path in shares.items():
        if not isinstance(name, str) or not isinstance(path, str):
            continue
        try:
            n, p = parse_share(f"{name}={path}")
        except ValueError:
            continue
        out[n] = p
    return out


DEFAULT_RESERVE = 20 * 10**9

_HOST_NAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def parse_host(text):
    """A host name for allowed-hosts, lowercased; ValueError otherwise. No
    scheme, port, path or wildcard: "example.com" covers its subdomains."""
    host = (text or "").strip().lower().rstrip(".")
    if not _HOST_NAME.match(host):
        raise ValueError(f"an allowed host is a host name such as models.example.com, not {text!r}")
    return host


def valid_hosts(hosts):
    out = []
    if not isinstance(hosts, list):
        return out
    for h in hosts:
        if not isinstance(h, str):
            continue
        try:
            h = parse_host(h)
        except ValueError:
            continue
        if h not in out:
            out.append(h)
    return out


def _clean(data):
    reserve = data.get("reserveBytes")
    if not isinstance(reserve, int) or isinstance(reserve, bool) or reserve < 0:
        reserve = DEFAULT_RESERVE
    index_dir = data.get("indexDir")
    if not isinstance(index_dir, str) or not os.path.isabs(index_dir):
        index_dir = None
    return {
        "indexDir": os.path.normpath(index_dir) if index_dir else None,
        "shares": valid_shares(data.get("shares")),
        "allowedHosts": valid_hosts(data.get("allowedHosts")),
        "allowPickle": data.get("allowPickle") is True,
        "reserveBytes": reserve,
    }


class Config:
    def __init__(self, store):
        self.store = store
        self._cache = None  # (signature, data)

    def _file(self):
        return os.path.join(self.store.dir, FILE)

    def load(self):
        """The config now, re-read only when the file changes."""
        try:
            st = os.stat(self._file())
        except FileNotFoundError:
            return _clean({})
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        if self._cache is None or self._cache[0] != sig:
            try:
                with open(self._file()) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            self._cache = (sig, _clean(data))
        return self._cache[1]

    def shares(self):
        return dict(self.load()["shares"])

    def allowed_hosts(self):
        return list(self.load()["allowedHosts"])

    def allow_pickle(self):
        return self.load()["allowPickle"]

    def reserve_bytes(self):
        return self.load()["reserveBytes"]

    def index_dir(self):
        return self.load()["indexDir"]

    def set(self, **changes):
        """Change some keys (shares, allowedHosts, allowPickle,
        reserveBytes) and keep the rest."""
        with self.store._locked():
            self._cache = None
            data = dict(self.load())
            data.update(changes)
            data = _clean(data)
            self.store._write_json(FILE, data)
            self._cache = None
        return self.load()

    def set_shares(self, shares):
        self.set(shares=shares)
        return self.shares()
