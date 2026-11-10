"""Tickets: one-time, short-lived passes a peer agent presents to
GET /couchy/models/blob/{sha256} to copy one file. Minted with a models
write key, kept in memory only (a restart drops them), and stored hashed."""

import hashlib
import secrets
import threading
import time

PREFIX = "ckt_"
DEFAULT_TTL = 300
MAX_TTL = 3600


def _hash(ticket):
    return hashlib.sha256(ticket.encode()).hexdigest()


class Tickets:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._live = {}  # hash -> {sha256, location, expires}

    def mint(self, sha256, location, ttl=DEFAULT_TTL):
        ttl = max(1, min(int(ttl), MAX_TTL))
        ticket = PREFIX + secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._live = {h: t for h, t in self._live.items() if t["expires"] > now}
            self._live[_hash(ticket)] = {"sha256": sha256, "location": location, "expires": now + ttl}
        return {"ticket": ticket, "expiresAt": now + ttl}

    def redeem(self, ticket, sha256):
        """The ticket's location when it's live and for this file, else
        None. A ticket is spent the first time it's presented, whatever
        the answer."""
        if not isinstance(ticket, str) or not ticket.startswith(PREFIX):
            return None
        with self._lock:
            t = self._live.pop(_hash(ticket), None)
        if t is None or t["expires"] <= self.clock() or t["sha256"] != sha256:
            return None
        return t["location"]
