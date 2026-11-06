import os
import unittest

from couchy_agent import guard
from tests.helpers import Tree

NS, OTHER = "ns_aaaa", "ns_bbbb"
RW = {"id": "key_1", "namespace": NS, "mode": "rw"}
RO = {"id": "key_2", "namespace": NS, "mode": "ro"}


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.out = self.t.dirs["output"]
        self.t.write(f"output/couchy/{NS}/u1/r1/a.png")

    def tearDown(self):
        self.t.cleanup()

    def test_target_namespace(self):
        self.assertEqual(guard.target_namespace(self.out, f"couchy/{NS}/u1/r1"), NS)
        self.assertEqual(guard.target_namespace(self.out, f"./couchy//{NS}/u1"), NS)
        self.assertEqual(guard.target_namespace(self.out, f"x/../couchy/{OTHER}"), OTHER)
        self.assertEqual(guard.target_namespace(self.out, "COUCHY/NS_AAAA/u1"), NS)
        self.assertEqual(guard.target_namespace(self.out, "couchy"), "")
        self.assertIsNone(guard.target_namespace(self.out, ""))
        self.assertIsNone(guard.target_namespace(self.out, "mine/couchy-ish"))

    def test_a_symlink_into_a_namespace_is_still_a_namespace(self):
        os.symlink(self.t.path(f"output/couchy/{NS}"), self.t.path("output/shortcut-couchy"))
        self.assertEqual(guard.target_namespace(self.out, "shortcut-couchy/u1"), NS)

    def test_grants(self):
        self.assertTrue(guard.allows_read(RO, NS))
        self.assertTrue(guard.allows_write(RW, NS))
        self.assertFalse(guard.allows_write(RO, NS))
        self.assertFalse(guard.allows_read(RW, OTHER))
        self.assertFalse(guard.allows_read(None, NS))
        self.assertFalse(guard.allows_read(RW, ""))

    def test_prompt_naming_another_namespace_is_refused(self):
        graph = {
            "1": {"class_type": "LoadImage", "inputs": {"image": f"couchy/{OTHER}/u1/up.png"}},
            "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": f"couchy/{NS}/u1/r1/out", "images": ["1", 0]}},
        }
        self.assertEqual(guard.prompt_refusal(graph, RW), [OTHER])
        self.assertEqual(guard.prompt_refusal(graph, None), [NS, OTHER])
        graph["1"]["inputs"]["image"] = f"couchy/{NS}/u1/up.png"
        self.assertEqual(guard.prompt_refusal(graph, RW), [])
        self.assertEqual(guard.prompt_refusal(graph, RO), [])

    def test_prompt_tricks_are_caught(self):
        for s in (f"COUCHY/{OTHER.upper()}/x.png", f"couchy\\{OTHER}\\x.png", f"couchy//{OTHER}/x",
                  f"couchy/./{OTHER}/x", f"couchy/{NS}/../{OTHER}/x", f"../output/couchy/{OTHER}/x",
                  f"/abs/ComfyUI/input/couchy/{OTHER}/x"):
            self.assertEqual(guard.prompt_refusal({"1": {"inputs": {"p": s}}}, RW), [OTHER], s)

    def test_prompt_text_that_only_mentions_couchy_runs(self):
        for s in ("a cat on a couchy/sofa", "mycouchy/ns_bbbb/x", "couchy", "a couch", f"ns_bbbb"):
            self.assertEqual(guard.prompt_refusal({"1": {"inputs": {"text": s}}}, RW), [], s)

    def test_a_too_deep_graph_is_refused(self):
        v = "x"
        for _ in range(100):
            v = [v]
        self.assertEqual(guard.prompt_refusal({"1": {"inputs": {"deep": v}}}, RW), [guard.TOO_DEEP])


if __name__ == "__main__":
    unittest.main()
