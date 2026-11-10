"""Pairing codes, namespaces and keys, kept in ComfyUI's user directory under
couchy-agent/ (0700, files 0600). Plain Python, no ComfyUI imports.

- pairing-code: the one outstanding code, in plain text, for the operator.
- pairing.json: that code's hash and expiry. Codes are single use.
- pairing-token.json: the hash of the pod's self-pairing token
  (COUCHY_AGENT_PAIRING_TOKEN), its expiry and whether it's spent. It pairs
  once, within 30 minutes of the agent's first start with it, with
  models: write.
- keys.json: namespaces and the hashes of their keys, each bound to its
  namespace, a mode (rw or ro) and a models grant (none, read or write).
  A key itself is never stored. Keys from before the models grant read as
  models: read.

Every change takes an flock on .lock, so `python -m couchy_agent pair` and a
running ComfyUI can share the files.
"""

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import time

DIR_NAME = "couchy-agent"

# 12 characters of Crockford's base32 (no I, L, O or U): 60 bits.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 12
CODE_TTL = 24 * 3600
TOKEN_TTL = 30 * 60
# A pairing token shares the code's lockout, which behind a proxy is one
# bucket for every caller, so only a long random one is taken.
TOKEN_MIN = 32
TOKEN_ENV = "COUCHY_AGENT_PAIRING_TOKEN"

MODES = ("rw", "ro")

# The models grant, separate from the mode: what a key may do under
# /couchy/models. Shared by every tenant, since models/ isn't namespaced.
MODELS_GRANTS = ("none", "read", "write")
DEFAULT_MODELS = "read"


def models_rank(grant):
    return MODELS_GRANTS.index(grant)

KEY_PREFIX = "cak_"


def _hash(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def normalize_code(code):
    """A code as typed: any case, with or without dashes or spaces."""
    if not isinstance(code, str):
        return ""
    code = "".join(c for c in code.upper() if c not in "- ")
    return code.translate(str.maketrans("OIL", "011"))


def format_code(code):
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def new_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def _new_id(prefix, nbytes):
    # Lowercase letters and digits only, so ids compare safely on folders
    # that ignore case.
    return prefix + secrets.token_hex(nbytes)


class Store:
    def __init__(self, user_dir, clock=time.time):
        self.dir = os.path.join(user_dir, DIR_NAME)
        self.clock = clock
        self._keys_cache = None  # (mtime_ns, size, data)

    # Files ---------------------------------------------------------------

    def ensure_dir(self):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)

    def _path(self, name):
        return os.path.join(self.dir, name)

    @contextlib.contextmanager
    def _locked(self):
        self.ensure_dir()
        fd = os.open(self._path(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read_json(self, name, default):
        try:
            with open(self._path(name)) as f:
                return json.load(f)
        except FileNotFoundError:
            return default

    def _write(self, name, text):
        """Write a file 0600, atomically: a temp file renamed over it."""
        tmp = self._path(f".{name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, text.encode())
        finally:
            os.close(fd)
        os.replace(tmp, self._path(name))

    def _write_json(self, name, data):
        self._write(name, json.dumps(data, indent=2) + "\n")

    # Pairing codes ---------------------------------------------------------

    def outstanding_code(self):
        """True when an unused code that hasn't expired is waiting."""
        p = self._read_json("pairing.json", {})
        return bool(p.get("hash")) and p.get("expires", 0) > self.clock()

    def mint_code(self, models=DEFAULT_MODELS):
        """A fresh code, replacing any outstanding one. Written to
        pairing-code for the operator; only its hash is kept for checking.
        models is the grant the key it pairs gets: only the host's own
        `pair --models write` asks for write."""
        if models not in MODELS_GRANTS:
            raise ValueError("models must be none, read or write")
        with self._locked():
            return self._mint_code(models)

    def _mint_code(self, models=DEFAULT_MODELS):
        code = new_code()
        self._write_json("pairing.json", {"hash": _hash(code), "expires": self.clock() + CODE_TTL, "models": models})
        self._write("pairing-code", format_code(code) + "\n")
        return code

    def mint_code_if_none(self):
        """On start: a fresh code when none is outstanding, else None."""
        with self._locked():
            if self.outstanding_code():
                return None
            return self._mint_code()

    # The pod's pairing token ---------------------------------------------

    def arm_token(self, token):
        """On start with COUCHY_AGENT_PAIRING_TOKEN set: remember its hash,
        valid 30 minutes from the first start that saw it. A restart with
        the same token keeps that expiry, and a spent token stays spent."""
        if not isinstance(token, str) or len(token.strip()) < TOKEN_MIN:
            return False
        h = _hash(token.strip())
        with self._locked():
            t = self._read_json("pairing-token.json", {})
            if t.get("hash") != h:
                self._write_json("pairing-token.json", {"hash": h, "expires": self.clock() + TOKEN_TTL, "used": False})
        return True

    def _spend_token(self, given):
        """True once for the armed token, within its time. Holds the lock."""
        t = self._read_json("pairing-token.json", {})
        if not t.get("hash") or t.get("used") or t.get("expires", 0) <= self.clock():
            return False
        if not hmac.compare_digest(t["hash"], _hash(given)):
            return False
        t["used"] = True
        self._write_json("pairing-token.json", t)
        return True

    def redeem(self, code, label):
        """Spend a code, or the pod's pairing token: make a namespace and
        its rw key. Answers {namespace, key, ...} or None for a wrong, used
        or expired one, the same refusal either way."""
        if not isinstance(code, str):
            return None
        given = normalize_code(code)
        with self._locked():
            models = None
            p = self._read_json("pairing.json", {})
            if (len(given) == CODE_LEN and p.get("hash") and p.get("expires", 0) > self.clock()
                    and hmac.compare_digest(p["hash"], _hash(given))):
                models = p.get("models", DEFAULT_MODELS)
                if models not in MODELS_GRANTS:
                    models = DEFAULT_MODELS
                self._write_json("pairing.json", {})
                with contextlib.suppress(FileNotFoundError):
                    os.remove(self._path("pairing-code"))
            elif code.strip() and self._spend_token(code.strip()):
                models = "write"  # Couchy created this host, so Couchy controls it
            if models is None:
                return None
            data = self._load_keys()
            ns = _new_id("ns_", 8)
            data["namespaces"][ns] = {"label": _label(label), "created": int(self.clock())}
            key = self._add_key(data, ns, "rw", _label(label), models)
            self._save_keys(data)
        return {"namespace": ns, "key": key["key"], "keyId": key["id"], "mode": "rw", "models": models}

    # Keys ------------------------------------------------------------------

    def _load_keys(self):
        data = self._read_json("keys.json", {})
        data.setdefault("namespaces", {})
        data.setdefault("keys", {})
        return data

    def _save_keys(self, data):
        self._write_json("keys.json", data)
        self._keys_cache = None

    def _add_key(self, data, ns, mode, label, models):
        secret = KEY_PREFIX + secrets.token_urlsafe(32)
        kid = _new_id("key_", 6)
        data["keys"][_hash(secret)] = {
            "id": kid, "namespace": ns, "mode": mode, "models": models, "label": label, "created": int(self.clock()),
        }
        return {"id": kid, "key": secret}

    def _keys(self):
        """keys.json, re-read only when it changes on disk."""
        try:
            st = os.stat(self._path("keys.json"))
        except FileNotFoundError:
            self._keys_cache = None
            return {"namespaces": {}, "keys": {}}
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        if self._keys_cache is None or self._keys_cache[0] != sig:
            self._keys_cache = (sig, self._load_keys())
        return self._keys_cache[1]

    def authenticate(self, secret):
        """The grant a key carries, {id, namespace, mode, models}, or None."""
        if not isinstance(secret, str) or not secret.startswith(KEY_PREFIX):
            return None
        entry = self._keys()["keys"].get(_hash(secret))
        if entry is None or entry["namespace"] not in self._keys()["namespaces"]:
            return None
        return {"id": entry["id"], "namespace": entry["namespace"], "mode": entry["mode"], "models": _models(entry)}

    def paired(self):
        return bool(self._keys()["namespaces"])

    def mint_key(self, namespace, mode, label="", models=DEFAULT_MODELS):
        """Another key for an existing namespace (an ro grant for pre-prod).
        The caller checks that models is no higher than its own grant.
        Answers {id, key, namespace, mode, models}."""
        if mode not in MODES:
            raise ValueError("mode must be rw or ro")
        if models not in MODELS_GRANTS:
            raise ValueError("models must be none, read or write")
        with self._locked():
            data = self._load_keys()
            if namespace not in data["namespaces"]:
                raise KeyError(namespace)
            key = self._add_key(data, namespace, mode, _label(label), models)
            self._save_keys(data)
        return {"id": key["id"], "key": key["key"], "namespace": namespace, "mode": mode, "models": models}

    def migrate(self):
        """Write models: read into keys from before the models grant, so
        keys.json says what every key may do. Reading them already treats a
        missing grant as read; this only makes it explicit."""
        with self._locked():
            data = self._load_keys()
            changed = False
            for e in data["keys"].values():
                if e.get("models") not in MODELS_GRANTS:
                    e["models"] = DEFAULT_MODELS
                    changed = True
            if changed:
                self._save_keys(data)
            return changed

    def list_keys(self, namespace=None):
        """Key ids, modes and labels; never the keys or their hashes."""
        data = self._load_keys()
        out = [
            {"id": e["id"], "namespace": e["namespace"], "mode": e["mode"], "models": _models(e),
             "label": e.get("label", ""),
             "created": e.get("created")}
            for e in data["keys"].values()
            if namespace is None or e["namespace"] == namespace
        ]
        return sorted(out, key=lambda e: e["namespace"])  # stable: oldest key first within each

    def namespaces(self):
        return self._load_keys()["namespaces"]

    def revoke_key(self, namespace, key_id):
        """Drop one key of the namespace. True when it existed."""
        with self._locked():
            data = self._load_keys()
            for h, e in list(data["keys"].items()):
                if e["id"] == key_id and e["namespace"] == namespace:
                    del data["keys"][h]
                    self._save_keys(data)
                    return True
        return False


def _models(entry):
    m = entry.get("models")
    return m if m in MODELS_GRANTS else DEFAULT_MODELS


def _label(label):
    if not isinstance(label, str):
        return ""
    return "".join(c for c in label if c.isprintable())[:120]


class Lockout:
    """Pairing failures per client: five within the window lock that client
    out for 15 minutes. Plus a cap on all attempts, so many clients can't
    share the guessing between them."""

    def __init__(self, max_failures=5, lock_for=15 * 60, global_per_minute=30, clock=time.monotonic):
        self.max_failures = max_failures
        self.lock_for = lock_for
        self.global_per_minute = global_per_minute
        self.clock = clock
        self._fails = {}  # client -> (count, first failure, locked until)
        self._recent = []  # times of every attempt in the last minute

    def check(self, client):
        """None when the client may try, else seconds until it may."""
        now = self.clock()
        _, _, until = self._fails.get(client, (0, 0, 0))
        if until > now:
            return int(until - now) + 1
        self._recent = [t for t in self._recent if t > now - 60]
        if len(self._recent) >= self.global_per_minute:
            return int(self._recent[0] + 60 - now) + 1
        self._recent.append(now)
        return None

    def fail(self, client):
        now = self.clock()
        count, first, _ = self._fails.get(client, (0, now, 0))
        if now - first > self.lock_for:
            count, first = 0, now
        count += 1
        if count >= self.max_failures:
            self._fails[client] = (0, now, now + self.lock_for)
        else:
            self._fails[client] = (count, first, 0)

    def succeed(self, client):
        self._fails.pop(client, None)
