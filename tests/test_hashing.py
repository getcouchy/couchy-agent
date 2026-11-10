"""The models index and the background hasher: hashing, restarts, weekly
verify, mismatches, shares that don't mount, and log lines without paths."""

import hashlib
import io
import json
import logging
import os
import stat
import unittest

from couchy_agent import models
from couchy_agent.hashing import VERIFY_EVERY, Hasher, Index
from couchy_agent.obs import JsonFormatter
from couchy_agent.store import Store
from tests.helpers import Tree


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def sha(data):
    return hashlib.sha256(data).hexdigest()


class HashingTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.clock = Clock()
        self.store = Store(self.t.dirs["user"])
        self.nas = os.path.join(self.t.base, "nas-cold")
        os.makedirs(self.nas)
        m = self.t.dirs["models"]
        self.locations = models.Locations(
            {f: [os.path.join(m, d) for d in dirs] for f, dirs in models.LAYOUT.items()},
            lambda: {"nas-cold": self.nas})
        self.logs = io.StringIO()
        self.handler = logging.StreamHandler(self.logs)
        self.handler.setFormatter(JsonFormatter(server="spark-1"))
        log = logging.getLogger("couchy_agent.models")
        log.addHandler(self.handler)
        log.setLevel(logging.INFO)
        self.addCleanup(log.removeHandler, self.handler)

    def tearDown(self):
        self.t.cleanup()

    def hasher(self):
        return Hasher(self.locations, Index(self.store), clock=self.clock, chunk=3)

    def drain(self, h):
        h.scan(force=True)
        n = 0
        while h.step():
            n += 1
        return n

    def listing(self, index):
        return {(f["location"], f["folder"], f["path"]): (f["sha256"], f["verifiedAt"])
                for f in models.list_models(self.locations.all(), index)["files"]}

    def lines(self):
        return [json.loads(line) for line in self.logs.getvalue().splitlines()]

    def test_hashes_host_and_shares_in_the_background_and_persists(self):
        self.t.write("models/checkpoints/a.safetensors", b"aaaa")
        self.t.write("nas-cold/loras/b.safetensors", b"bb")
        h = self.hasher()
        h.scan(force=True)
        self.assertEqual(self.listing(h.index)[("host", "checkpoints", "a.safetensors")], (None, None))  # nothing on request
        self.assertEqual(self.drain(h), 2)
        got = self.listing(h.index)
        self.assertEqual(got[("host", "checkpoints", "a.safetensors")], (sha(b"aaaa"), self.clock.t))
        self.assertEqual(got[("nas-cold", "loras", "b.safetensors")], (sha(b"bb"), self.clock.t))
        path = os.path.join(self.store.dir, "models-index.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_a_restart_rehashes_only_what_changed(self):
        self.t.write("models/checkpoints/a.safetensors", b"aaaa")
        self.t.write("models/checkpoints/b.safetensors", b"bbbb")
        self.drain(self.hasher())
        b = self.t.path("models/checkpoints/b.safetensors")
        with open(b, "wb") as f:
            f.write(b"changed")
        os.utime(b, (5, 5))
        self.t.write("models/vae/new.safetensors", b"v")
        os.remove(self.t.path("models/checkpoints/a.safetensors"))
        self.t.write("models/checkpoints/c.safetensors", b"c")
        before = len(self.lines())
        h = self.hasher()  # a restart: a new process reads the index from disk
        h.scan(force=True)
        self.assertEqual(sorted((p[1], p[2]) for p in h.pending()),
                         [("checkpoints", "b.safetensors"), ("checkpoints", "c.safetensors"), ("vae", "new.safetensors")])
        self.assertIsNone(h.index.get("host", "checkpoints", "a.safetensors"))  # gone from disk, gone from the index
        self.assertEqual(self.drain(h), 3)
        hashed = [l for l in self.lines()[before:] if l["msg"] == "model hashed"]
        self.assertEqual(len(hashed), 3)
        self.assertEqual(self.listing(h.index)[("host", "checkpoints", "b.safetensors")][0], sha(b"changed"))

    def test_weekly_verify_sets_verified_at(self):
        self.t.write("nas-cold/checkpoints/a.safetensors", b"aaaa")
        h = self.hasher()
        self.drain(h)
        first = self.clock.t
        self.clock.t += VERIFY_EVERY - 60
        self.assertEqual(self.drain(h), 0)  # not due yet
        self.clock.t += 120
        self.assertEqual([p[3] for p in h.pending()], ["verify"])
        self.assertEqual(self.drain(h), 1)
        self.assertEqual(self.listing(h.index)[("nas-cold", "checkpoints", "a.safetensors")],
                         (sha(b"aaaa"), first + VERIFY_EVERY + 60))
        self.assertIn("model verified", [l["msg"] for l in self.lines()])

    def test_a_mismatch_clears_the_hash_and_is_logged(self):
        p = self.t.write("nas-cold/checkpoints/a.safetensors", b"aaaa")
        h = self.hasher()
        self.drain(h)
        st = os.stat(p)
        with open(p, "r+b") as f:
            f.write(b"rot!")  # bit rot: same size, mtime put back
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.clock.t += VERIFY_EVERY + 1
        self.assertEqual(self.drain(h), 1)
        self.assertEqual(self.listing(h.index)[("nas-cold", "checkpoints", "a.safetensors")], (None, None))
        bad = [l for l in self.lines() if l["msg"] == "model verify mismatch"]
        self.assertEqual(len(bad), 1)
        self.assertEqual((bad[0]["level"], bad[0]["location"], bad[0]["sha"]), ("WARNING", "nas-cold", sha(b"aaaa")[:12]))
        self.clock.t += VERIFY_EVERY + 1
        self.assertEqual(self.drain(h), 0)  # stays unverified until the file changes
        Hasher(self.locations, Index(self.store), clock=self.clock).scan(force=True)
        self.assertIsNone(Index(self.store).get("nas-cold", "checkpoints", "a.safetensors")["sha256"])

    def test_a_share_that_did_not_mount_keeps_its_hashes(self):
        self.t.write("nas-cold/checkpoints/a.safetensors", b"aaaa")
        h = self.hasher()
        self.drain(h)
        os.rename(self.nas, self.nas + ".away")
        os.makedirs(self.nas)  # an empty mount point
        self.drain(h)
        self.assertIsNotNone(h.index.get("nas-cold", "checkpoints", "a.safetensors"))
        os.rmdir(self.nas)
        os.rename(self.nas + ".away", self.nas)
        self.assertEqual(self.drain(h), 0)
        self.assertEqual(self.listing(h.index)[("nas-cold", "checkpoints", "a.safetensors")][0], sha(b"aaaa"))

    def test_shares_scan_less_often_than_the_host(self):
        h = self.hasher()
        h.scan()
        self.t.write("models/loras/a.safetensors")
        self.t.write("nas-cold/loras/b.safetensors")
        self.clock.t += 61
        h.scan()
        self.assertEqual([p[0] for p in h.pending()], ["host"])
        self.clock.t += 240
        h.scan()
        self.assertEqual(sorted(p[0] for p in h.pending()), ["host", "nas-cold"])

    def test_listings_come_from_the_last_scan(self):
        self.t.write("nas-cold/loras/a.safetensors")
        h = self.hasher()
        locs = self.locations.all()
        self.assertEqual([e[2] for e in h.found(locs)], ["a.safetensors"])  # not scanned yet: walked now
        h.scan(force=True)
        self.t.write("nas-cold/loras/b.safetensors")
        page = models.list_models(locs, h.index, found=h.found(locs, "nas-cold", "loras"))
        self.assertEqual([f["path"] for f in page["files"]], ["a.safetensors"])  # the NAS isn't walked per page
        self.clock.t += 301
        h.scan()
        page = models.list_models(locs, h.index, found=h.found(locs, folder="loras"))
        self.assertEqual([f["path"] for f in page["files"]], ["a.safetensors", "b.safetensors"])

    def test_symlinks_are_never_hashed(self):
        secret = self.t.write("secret.bin", b"s")
        os.makedirs(self.t.path("models/checkpoints"))
        os.symlink(secret, self.t.path("models/checkpoints/link.safetensors"))
        h = self.hasher()
        self.assertEqual(self.drain(h), 0)
        self.assertEqual(h.index.keys("host"), [])

    def test_log_lines_carry_the_process_fields_and_never_a_path(self):
        self.t.write("models/checkpoints/my-secret-project/flux-dev.safetensors", b"x")
        self.drain(self.hasher())
        lines = self.lines()
        self.assertTrue(lines)
        for line in lines:
            self.assertEqual((line["service"], line["server"]), ("couchy-agent", "spark-1"))
            self.assertIn("version", line)
        text = self.logs.getvalue()
        for leak in ("my-secret-project", "flux-dev", self.t.base):
            self.assertNotIn(leak, text)
        hashed = [l for l in lines if l["msg"] == "model hashed"][0]
        self.assertEqual((hashed["folder"], hashed["sha"], hashed["bytes"]), ("checkpoints", sha(b"x")[:12], 1))
        self.assertIn("ms", hashed)

    def test_denied_keys_are_dropped(self):
        log = logging.getLogger("couchy_agent.models")
        log.info("x", extra={"path": "/models/a.safetensors", "token": "t", "nested": {"filename": "a"}})
        line = self.lines()[-1]
        self.assertEqual((line["path"], line["token"], line["nested"]), ("[dropped]", "[dropped]", {"filename": "[dropped]"}))


if __name__ == "__main__":
    unittest.main()
