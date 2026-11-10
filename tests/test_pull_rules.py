"""The pure rules behind pulls: known and allowed hosts, formats, ids,
rename-no-replace and its fallbacks, tickets, and history's name matching."""

import errno
import os
import unittest
from unittest import mock

from couchy_agent import history, pulls
from couchy_agent.tickets import Tickets
from tests.helpers import Tree


class Hosts(unittest.TestCase):
    def test_known_hosts_over_https_only(self):
        for url in ("https://civitai.com/api/download/models/1?token=x", "https://huggingface.co/a/b/resolve/main/c",
                    "https://cdn-lfs.huggingface.co/x", "https://cas-bridge.xethub.hf.co/x", "https://b2.civitai.com/x",
                    "https://civitai-delivery-worker-prod.0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/x"):
            self.assertTrue(pulls.host_allowed(url), url)
        for url in ("http://huggingface.co/x", "https://evilhuggingface.co/x", "https://huggingface.co.evil.com/x",
                    "https://someone.0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/x",
                    "https://user:pw@huggingface.co/x", "ftp://huggingface.co/x", "file:///etc/passwd",
                    "https://192.168.1.10/x", "not a url"):
            self.assertFalse(pulls.host_allowed(url), url)

    def test_presigned_urls_need_allowed_hosts(self):
        allowed = ["r2.cloudflarestorage.com"]
        self.assertTrue(pulls.in_allowed_hosts("https://bucket.acct.r2.cloudflarestorage.com/k?X-Amz-Signature=s", allowed))
        self.assertFalse(pulls.in_allowed_hosts("https://huggingface.co/x", allowed))
        self.assertFalse(pulls.in_allowed_hosts("https://huggingface.co/x", []))
        self.assertFalse(pulls.in_allowed_hosts("https://u:p@bucket.r2.cloudflarestorage.com/k", allowed))

    def test_allowed_hosts_and_their_subdomains(self):
        allowed = ["models.example.com", "10.0.0.5"]
        self.assertTrue(pulls.host_allowed("http://models.example.com:8080/x", allowed))
        self.assertTrue(pulls.host_allowed("https://eu.models.example.com/x", allowed))
        self.assertTrue(pulls.host_allowed("http://10.0.0.5/x", allowed))
        self.assertFalse(pulls.host_allowed("https://example.com/x", allowed))
        self.assertFalse(pulls.host_allowed("https://xmodels.example.com/x", allowed))


class Formats(unittest.TestCase):
    def test_formats(self):
        self.assertEqual([pulls.file_format(p) for p in ("a.safetensors", "b.GGUF", "c.sft")], ["safe"] * 3)
        self.assertEqual([pulls.file_format(p) for p in ("a.ckpt", "a.pt", "a.pth", "a.bin")], ["pickle"] * 4)
        self.assertIsNone(pulls.file_format("a.zip"))

    def test_ids_are_stable_per_file_and_target(self):
        a = pulls.pull_id("0" * 64, "host", "loras", "a.safetensors")
        self.assertEqual(a, pulls.pull_id("0" * 64, "host", "loras", "a.safetensors"))
        self.assertNotEqual(a, pulls.pull_id("0" * 64, "nas-hot", "loras", "a.safetensors"))
        self.assertRegex(a, r"^pull_[0-9a-f]{24}$")


class RenameNoReplace(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.src = self.t.write("src", b"new")
        self.dst = self.t.path("dst")

    def tearDown(self):
        self.t.cleanup()

    def check(self):
        with open(self.t.write("dst2", b"old")):
            pass
        with self.assertRaises(FileExistsError):
            pulls.rename_noreplace(self.src, self.t.path("dst2"))
        with open(self.t.path("dst2"), "rb") as f:
            self.assertEqual(f.read(), b"old")
        pulls.rename_noreplace(self.src, self.dst)
        self.assertFalse(os.path.exists(self.src))
        with open(self.dst, "rb") as f:
            self.assertEqual(f.read(), b"new")

    def test_renameat2(self):
        self.check()

    def test_link_then_unlink(self):
        with mock.patch.object(pulls, "_renameat2", None):
            self.check()

    def test_no_hard_links(self):
        def no_link(*_):
            raise OSError(errno.EPERM, "no hard links here")
        with mock.patch.object(pulls, "_renameat2", None), mock.patch.object(pulls.os, "link", no_link):
            self.check()


class TicketsTest(unittest.TestCase):
    def test_single_use_expiring_and_capped(self):
        now = [100.0]
        t = Tickets(clock=lambda: now[0])
        a = t.mint("a" * 64, "host", ttl=10)
        self.assertEqual(t.redeem(a["ticket"], "a" * 64), "host")
        self.assertIsNone(t.redeem(a["ticket"], "a" * 64))
        b = t.mint("a" * 64, "nas-hot", ttl=10)
        now[0] += 11
        self.assertIsNone(t.redeem(b["ticket"], "a" * 64))
        c = t.mint("a" * 64, "host", ttl=10**9)
        self.assertEqual(c["expiresAt"], now[0] + 3600)
        self.assertIsNone(t.redeem(c["ticket"], "b" * 64))
        self.assertIsNone(t.redeem(c["ticket"], "a" * 64))  # spent by the wrong file


class History(unittest.TestCase):
    def test_a_name_with_two_hashes_is_left_out(self):
        shas = {("host", "checkpoints", "a.safetensors"): "1" * 64, ("nas-hot", "checkpoints", "a.safetensors"): "1" * 64,
                ("host", "loras", "b.safetensors"): "2" * 64, ("host", "vae", "b.safetensors"): "3" * 64}
        self.assertEqual(history.by_name(shas), {"a.safetensors": "1" * 64})

    def test_items_without_a_time_or_graph_are_skipped(self):
        shas = {("host", "checkpoints", "a.safetensors"): "1" * 64}
        graph = {"1": {"inputs": {"ckpt_name": "a.safetensors"}}}
        h = {"x": {"prompt": [1, "x", graph, {}, []], "status": {"messages": []}},
             "y": {"prompt": "garbage", "status": {"messages": [["execution_start", {"timestamp": 5000}]]}},
             "z": {"prompt": [1, "z", graph, {}, []],
                   "status": {"messages": [["execution_cached", {"timestamp": 9000}]]}}}
        self.assertEqual(history.model_sets(h, shas), {"runs": [{"at": 9.0, "sha256": ["1" * 64]}]})


if __name__ == "__main__":
    unittest.main()
