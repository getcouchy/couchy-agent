import json
import os
import stat
import unittest

from couchy_agent.store import CODE_LEN, Lockout, Store, normalize_code
from tests.helpers import Tree


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.clock = Clock()
        self.s = Store(self.t.dirs["user"], clock=self.clock)

    def tearDown(self):
        self.t.cleanup()

    def mode(self, rel):
        return stat.S_IMODE(os.stat(os.path.join(self.s.dir, rel)).st_mode)

    def test_a_fresh_start_writes_a_code_and_a_restart_keeps_it(self):
        code = self.s.mint_code_if_none()
        self.assertEqual(len(code), CODE_LEN)
        with open(os.path.join(self.s.dir, "pairing-code")) as f:
            self.assertEqual(normalize_code(f.read().strip()), code)
        self.assertIsNone(self.s.mint_code_if_none())
        self.assertEqual(self.mode(""), 0o700)
        self.assertEqual(self.mode("pairing-code"), 0o600)
        self.assertEqual(self.mode("pairing.json"), 0o600)
        with open(os.path.join(self.s.dir, "pairing.json")) as f:
            self.assertNotIn(code, f.read())  # only the hash is kept

    def test_pairing_makes_a_namespace_and_an_rw_key(self):
        code = self.s.mint_code()
        out = self.s.redeem(code.lower()[:4] + "-" + code[4:], "Couchy prod · Lab")
        self.assertTrue(out["namespace"].startswith("ns_"))
        self.assertEqual(out["mode"], "rw")
        self.assertEqual(self.s.authenticate(out["key"]),
                         {"id": out["keyId"], "namespace": out["namespace"], "mode": "rw", "models": "read"})
        self.assertTrue(self.s.paired())
        self.assertEqual(self.mode("keys.json"), 0o600)
        with open(os.path.join(self.s.dir, "keys.json")) as f:
            text = f.read()
        self.assertNotIn(out["key"], text)
        self.assertIn("Couchy prod · Lab", json.loads(text)["namespaces"][out["namespace"]]["label"])
        self.assertFalse(os.path.exists(os.path.join(self.s.dir, "pairing-code")))

    def test_a_code_works_once(self):
        code = self.s.mint_code()
        self.assertIsNotNone(self.s.redeem(code, "a"))
        self.assertIsNone(self.s.redeem(code, "b"))

    def test_wrong_and_expired_codes_are_refused(self):
        code = self.s.mint_code()
        self.assertIsNone(self.s.redeem("0" * CODE_LEN if code != "0" * CODE_LEN else "1" * CODE_LEN, "x"))
        self.assertIsNone(self.s.redeem("short", "x"))
        self.assertIsNone(self.s.redeem(None, "x"))
        self.clock.t += 24 * 3600 + 1
        self.assertIsNone(self.s.redeem(code, "x"))
        self.assertIsNotNone(self.s.mint_code_if_none())  # expired, so a start writes a new one

    def test_pair_command_replaces_the_waiting_code(self):
        old = self.s.mint_code()
        new = self.s.mint_code()
        self.assertIsNone(self.s.redeem(old, "x"))
        self.assertIsNotNone(self.s.redeem(new, "x"))

    def test_two_pairings_get_two_namespaces(self):
        a = self.s.redeem(self.s.mint_code(), "prod")
        b = self.s.redeem(self.s.mint_code(), "preprod")
        self.assertNotEqual(a["namespace"], b["namespace"])
        self.assertEqual(self.s.authenticate(b["key"])["namespace"], b["namespace"])

    def test_read_only_keys_and_revoking(self):
        a = self.s.redeem(self.s.mint_code(), "prod")
        ro = self.s.mint_key(a["namespace"], "ro", "pre-prod reads prod")
        self.assertEqual(self.s.authenticate(ro["key"]), {"id": ro["id"], "namespace": a["namespace"], "mode": "ro", "models": "read"})
        self.assertEqual([k["mode"] for k in self.s.list_keys(a["namespace"])], ["rw", "ro"])
        self.assertNotIn(ro["key"], repr(self.s.list_keys()))
        self.assertTrue(self.s.revoke_key(a["namespace"], ro["id"]))
        self.assertIsNone(self.s.authenticate(ro["key"]))
        self.assertFalse(self.s.revoke_key(a["namespace"], ro["id"]))
        with self.assertRaises(KeyError):
            self.s.mint_key("ns_nope", "ro")

    def test_unknown_keys_are_refused(self):
        self.s.redeem(self.s.mint_code(), "prod")
        for key in (None, "", "cak_wrong", "Bearer x", 42):
            self.assertIsNone(self.s.authenticate(key))

    def test_a_key_minted_by_another_process_is_seen(self):
        a = self.s.redeem(self.s.mint_code(), "prod")
        other = Store(self.t.dirs["user"], clock=self.clock)
        self.assertIsNotNone(other.authenticate(a["key"]))  # warm its cache
        ro = self.s.mint_key(a["namespace"], "ro")
        self.assertIsNotNone(other.authenticate(ro["key"]))


    def test_pair_models_write_grants_write_and_the_next_code_reads(self):
        w = self.s.redeem(self.s.mint_code(models="write"), "prod")
        self.assertEqual((w["models"], self.s.authenticate(w["key"])["models"]), ("write", "write"))
        r = self.s.redeem(self.s.mint_code(), "pre-prod")
        self.assertEqual(r["models"], "read")
        self.assertEqual(self.s.redeem(self.s.mint_code_if_none(), "x")["models"], "read")
        with self.assertRaises(ValueError):
            self.s.mint_code(models="admin")

    def test_existing_keys_migrate_to_read(self):
        out = self.s.redeem(self.s.mint_code(), "prod")
        path = os.path.join(self.s.dir, "keys.json")
        with open(path) as f:
            data = json.load(f)
        for e in data["keys"].values():
            del e["models"]  # a keys.json from before the models grant
        with open(path, "w") as f:
            json.dump(data, f)
        self.assertEqual(self.s.authenticate(out["key"])["models"], "read")
        self.assertEqual([k["models"] for k in self.s.list_keys()], ["read"])
        self.assertTrue(self.s.migrate())
        with open(path) as f:
            self.assertEqual([e["models"] for e in json.load(f)["keys"].values()], ["read"])
        self.assertFalse(self.s.migrate())
        self.assertEqual(self.mode("keys.json"), 0o600)


class LockoutTest(unittest.TestCase):
    def test_five_failures_lock_that_client_out_for_fifteen_minutes(self):
        clock = Clock(0.0)
        lo = Lockout(clock=clock)
        for _ in range(5):
            self.assertIsNone(lo.check("a"))
            lo.fail("a")
        self.assertIsNotNone(lo.check("a"))
        self.assertIsNone(lo.check("b"))
        clock.t += 15 * 60 - 1
        self.assertIsNotNone(lo.check("a"))
        clock.t += 2
        self.assertIsNone(lo.check("a"))

    def test_success_clears_failures(self):
        clock = Clock(0.0)
        lo = Lockout(clock=clock)
        for _ in range(4):
            lo.fail("a")
        lo.succeed("a")
        lo.fail("a")
        self.assertIsNone(lo.check("a"))

    def test_all_clients_share_a_cap(self):
        clock = Clock(0.0)
        lo = Lockout(clock=clock, global_per_minute=3)
        for c in "abc":
            self.assertIsNone(lo.check(c))
        self.assertIsNotNone(lo.check("d"))
        clock.t += 61
        self.assertIsNone(lo.check("d"))


if __name__ == "__main__":
    unittest.main()
