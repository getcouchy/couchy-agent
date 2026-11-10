"""The agent's host-side config, in user/couchy-agent/config.json (0600).
Set only from a shell on the host, with `python -m couchy_agent config`:
no route reads or writes it.

- shares: {name: path}, model roots mounted on the host (a NAS over CIFS or
  NFS) that use ComfyUI's models layout (checkpoints/, loras/, ...). Each is
  its own location next to ComfyUI's own models root, which is "host".

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
            return {"shares": {}}
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        if self._cache is None or self._cache[0] != sig:
            try:
                with open(self._file()) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            self._cache = (sig, {"shares": valid_shares(data.get("shares"))})
        return self._cache[1]

    def shares(self):
        return dict(self.load()["shares"])

    def set_shares(self, shares):
        with self.store._locked():
            data = dict(self.load())
            data["shares"] = valid_shares(shares)
            self.store._write_json(FILE, data)
            self._cache = None
        return self.shares()
