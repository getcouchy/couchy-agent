"""python -m couchy_agent: pair --models write, keys with their grant, and
config --share."""

import contextlib
import io
import json
import os
import stat
import unittest

from couchy_agent.__main__ import main
from couchy_agent.config import Config, parse_share
from couchy_agent.store import Store
from tests.helpers import Tree


class MainTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.user = self.t.dirs["user"]
        self.store = Store(self.user)

    def tearDown(self):
        self.t.cleanup()

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--user-directory", self.user, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_pair_with_models_write_and_keys_shows_the_grant(self):
        code, out, err = self.run_main("pair", "--models", "write")
        self.assertEqual(code, 0)
        self.assertIn("models: write", err)
        paired = self.store.redeem(out.strip(), "prod")
        self.assertEqual(paired["models"], "write")
        code, out, _ = self.run_main("pair")
        self.assertEqual(self.store.redeem(out.strip(), "pre-prod")["models"], "read")
        _, out, _ = self.run_main("keys")
        self.assertIn("models:write", out)
        self.assertIn("models:read", out)

    def test_config_shares(self):
        hot = os.path.join(self.t.base, "hot")
        os.makedirs(hot)
        code, out, _ = self.run_main("config", "--share", f"nas-hot={hot}", "--share", "nas-cold=/mnt/models-cold")
        self.assertEqual(code, 0)
        self.assertIn(f"share  nas-hot={hot}", out)
        self.assertEqual(Config(self.store).shares(), {"nas-hot": hot, "nas-cold": "/mnt/models-cold"})
        path = os.path.join(self.store.dir, "config.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(self.run_main("config", "--share", "host=/x")[0], 2)
        self.assertEqual(self.run_main("config", "--share", "nas=relative/path")[0], 2)
        self.assertEqual(self.run_main("config", "--share", f"a={hot}", "--share", f"a={hot}")[0], 2)
        _, out, _ = self.run_main("config")
        self.assertIn("nas-cold=/mnt/models-cold", out)
        self.run_main("config", "--clear-shares")
        self.assertEqual(Config(self.store).shares(), {})

    def test_config_allowed_hosts_pickle_and_reserve(self):
        code, out, _ = self.run_main("config")
        self.assertIn("allowed-hosts: none", out)
        self.assertIn("allow-pickle: no", out)
        self.assertIn("reserve: 20 GB", out)
        hot = os.path.join(self.t.base, "hot")
        os.makedirs(hot)
        self.run_main("config", "--share", f"nas-hot={hot}")
        code, out, _ = self.run_main("config", "--allowed-host", "Models.Example.com", "--allowed-host", "10.0.0.5",
                                     "--allow-pickle", "--reserve-gb", "50")
        self.assertEqual(code, 0)
        c = Config(self.store).load()
        self.assertEqual((c["allowedHosts"], c["allowPickle"], c["reserveBytes"]),
                         (["models.example.com", "10.0.0.5"], True, 50 * 10**9))
        self.assertEqual(c["shares"], {"nas-hot": hot})  # the rest is kept
        self.assertEqual(self.run_main("config", "--allowed-host", "https://x.com/path")[0], 2)
        self.assertEqual(self.run_main("config", "--reserve-gb", "-1")[0], 2)
        self.run_main("config", "--no-allow-pickle", "--clear-allowed-hosts")
        c = Config(self.store).load()
        self.assertEqual((c["allowedHosts"], c["allowPickle"]), ([], False))

    def test_a_hand_edited_config_cannot_add_host_or_a_relative_path(self):
        self.store.ensure_dir()
        with open(os.path.join(self.store.dir, "config.json"), "w") as f:
            json.dump({"shares": {"host": "/x", "ok": "/mnt/ok", "rel": "a/b", "Bad Name": "/y"}}, f)
        self.assertEqual(Config(self.store).shares(), {"ok": "/mnt/ok"})

    def test_parse_share(self):
        self.assertEqual(parse_share("nas-hot=/mnt/models-hot/"), ("nas-hot", "/mnt/models-hot"))
        for bad in ("nas-hot", "=/x", "nas-hot=", "NAS=/x", "a/b=/x"):
            with self.assertRaises(ValueError):
                parse_share(bad)


if __name__ == "__main__":
    unittest.main()
