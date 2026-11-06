import os
import time
import unittest

from couchy_agent import files
from tests.helpers import Tree

NS, OTHER = "ns_aaaa", "ns_bbbb"


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.out = self.t.dirs["output"]
        self.t.write(f"output/couchy/{NS}/u1/run1/a.png", b"aa")
        self.t.write(f"output/couchy/{NS}/u1/run1/b.png", b"bbb")
        self.t.write(f"output/couchy/{NS}/u2/run2/c.png", b"c")
        self.t.write(f"output/couchy/{OTHER}/u1/run9/theirs.png", b"t")
        self.t.write(f"input/couchy/{NS}/u1/up.png", b"up")
        self.t.write("output/ComfyUI_00001_.png", b"loose")
        os.symlink(self.t.path("models/model.safetensors"), self.t.path(f"output/couchy/{NS}/u1/run1/link.png"))
        os.symlink(self.t.path(f"output/couchy/{OTHER}"), self.t.path(f"output/couchy/{NS}/u1/sneak"))

    def tearDown(self):
        self.t.cleanup()

    def test_resolve_keeps_the_helper_rules(self):
        self.assertIsNotNone(files.resolve(self.out, "", "ComfyUI_00001_.png"))
        for sub, name in [("..", "models/model.safetensors"), ("../models", "model.safetensors"), ("", "../x"),
                          ("", ""), ("", ".."), ("/etc", "passwd")]:
            self.assertIsNone(files.resolve(self.out, sub, name), (sub, name))

    def test_resolve_in_reaches_only_its_namespace(self):
        self.assertIsNotNone(files.resolve_in(self.out, NS, f"couchy/{NS}/u1/run1", "a.png"))
        refused = [
            (f"couchy/{OTHER}/u1/run9", "theirs.png"),
            (f"couchy/{NS}/u1/sneak/u1/run9", "theirs.png"),   # through a symlink
            (f"couchy/{NS}/../{OTHER}/u1/run9", "theirs.png"),  # through ..
            ("", "ComfyUI_00001_.png"),                         # outside couchy/
            (f"couchy/{NS}/u1/run1", "link.png"),               # a symlink out
            ("couchy", NS),                                     # the namespace folder itself
        ]
        for sub, name in refused:
            self.assertIsNone(files.resolve_in(self.out, NS, sub, name), (sub, name))

    def test_namespace_match_ignores_case(self):
        path = os.path.realpath(self.t.path(f"output/couchy/{NS}/u1/run1/a.png"))
        self.assertEqual(files.namespace_of(self.out, path), NS)
        self.assertEqual(files.namespace_of(self.out, os.path.realpath(self.t.path("output/COUCHY/NS_AAAA/x"))), NS)
        self.assertIsNone(files.namespace_of(self.out, os.path.realpath(self.t.path("output/ComfyUI_00001_.png"))))

    def test_usage_counts_per_folder_and_owner_without_names(self):
        u = files.usage(self.t.dirs, NS)
        self.assertEqual(u["output"]["owners"], {"u1": {"count": 2, "bytes": 5}, "u2": {"count": 1, "bytes": 1}})
        self.assertEqual(u["output"]["count"], 3)
        self.assertEqual(u["input"], {"count": 1, "bytes": 2, "owners": {"u1": {"count": 1, "bytes": 2}}})
        self.assertNotIn("a.png", repr(u))

    def test_lists_one_owner_in_pages(self):
        first = files.list_files(self.out, NS, "u1", limit=1)
        self.assertEqual([f["filename"] for f in first["files"]], ["a.png"])
        self.assertEqual(first["files"][0]["subfolder"], f"couchy/{NS}/u1/run1")
        self.assertEqual(first["total"], {"count": 2, "bytes": 5})
        rest = files.list_files(self.out, NS, "u1", after=first["next"])
        self.assertEqual([f["filename"] for f in rest["files"]], ["b.png"])
        self.assertNotIn("next", rest)
        self.assertEqual(files.list_files(self.out, NS, "nobody")["files"], [])

    def test_deletes_listed_files_inside_the_namespace_only(self):
        out = files.delete_files(self.out, NS, [
            {"subfolder": f"couchy/{NS}/u1/run1", "filename": "a.png"},
            {"subfolder": f"couchy/{NS}/u1/run1", "filename": "gone.png"},
            {"subfolder": f"couchy/{OTHER}/u1/run9", "filename": "theirs.png"},
            {"subfolder": "", "filename": "ComfyUI_00001_.png"},
            {"subfolder": f"couchy/{NS}/u1/run1", "filename": "link.png"},
            {"subfolder": f"couchy/{NS}/u1", "filename": "run1"},
        ])
        self.assertEqual([f["filename"] for f in out["deleted"]], ["a.png"])
        self.assertEqual([f["filename"] for f in out["missing"]], ["gone.png"])
        self.assertEqual([f["filename"] for f in out["refused"]], ["theirs.png", "ComfyUI_00001_.png", "link.png", "run1"])
        self.assertTrue(self.t.exists(f"output/couchy/{OTHER}/u1/run9/theirs.png"))
        self.assertTrue(self.t.exists("output/ComfyUI_00001_.png"))
        self.assertTrue(self.t.exists("models/model.safetensors"))
        self.assertTrue(os.path.isdir(self.t.path(f"output/couchy/{NS}/u1/run1")))

    def test_deletes_one_owner(self):
        self.assertEqual(files.delete_owner(self.out, NS, "u1"), {"deleted": 2, "bytes": 5})
        self.assertTrue(self.t.exists(f"output/couchy/{NS}/u2/run2/c.png"))
        self.assertTrue(self.t.exists(f"output/couchy/{OTHER}/u1/run9/theirs.png"))
        self.assertTrue(self.t.exists("models/model.safetensors"))  # the symlink was skipped

    def test_deletes_older_inside_the_namespace(self):
        old = time.time() - 3600
        for rel in (f"output/couchy/{NS}/u1/run1/a.png", f"output/couchy/{OTHER}/u1/run9/theirs.png",
                    "output/ComfyUI_00001_.png"):
            os.utime(self.t.path(rel), (old, old))
        self.assertEqual(files.delete_older(self.out, NS, 60), {"deleted": 1, "bytes": 2})
        self.assertTrue(self.t.exists(f"output/couchy/{OTHER}/u1/run9/theirs.png"))
        self.assertTrue(self.t.exists("output/ComfyUI_00001_.png"))

    def test_adopt_moves_once_then_answers_missing(self):
        src = {"folder": "output", "subfolder": "", "filename": "ComfyUI_00001_.png"}
        out = files.adopt(self.t.dirs, NS, src, "u1", "run7")
        self.assertEqual(out, {"state": "adopted", "to": {
            "folder": "output", "subfolder": f"couchy/{NS}/u1/run7", "filename": "ComfyUI_00001_.png"}})
        self.assertTrue(self.t.exists(f"output/couchy/{NS}/u1/run7/ComfyUI_00001_.png"))
        self.assertEqual(files.adopt(self.t.dirs, NS, src, "u1", "run7"), {"state": "missing"})

    def test_adopt_keeps_both_on_a_name_clash(self):
        self.t.write(f"output/couchy/{NS}/u1/run7/ComfyUI_00001_.png", b"first")
        out = files.adopt(self.t.dirs, NS, {"folder": "output", "subfolder": "", "filename": "ComfyUI_00001_.png"}, "u1", "run7")
        self.assertEqual(out["to"]["filename"], "ComfyUI_00001__1.png")

    def test_adopt_takes_from_temp(self):
        self.t.write("temp/preview.png")
        out = files.adopt(self.t.dirs, NS, {"folder": "temp", "subfolder": "", "filename": "preview.png"}, "lab", "r1")
        self.assertEqual(out["state"], "adopted")
        self.assertTrue(self.t.exists(f"output/couchy/{NS}/lab/r1/preview.png"))

    def test_adopt_refuses(self):
        cases = [
            ({"folder": "output", "subfolder": f"couchy/{OTHER}/u1/run9", "filename": "theirs.png"}, "u1", "r"),
            ({"folder": "output", "subfolder": f"couchy/{NS}/u1/run1", "filename": "a.png"}, "u1", "r"),
            ({"folder": "output", "subfolder": "../models", "filename": "model.safetensors"}, "u1", "r"),
            ({"folder": "input", "subfolder": "", "filename": "x.png"}, "u1", "r"),
            ({"folder": "models", "subfolder": "", "filename": "model.safetensors"}, "u1", "r"),
            ({"folder": "output", "subfolder": "", "filename": "ComfyUI_00001_.png"}, "../u1", "r"),
            ({"folder": "output", "subfolder": "", "filename": "ComfyUI_00001_.png"}, "u1", ".."),
            ("not an object", "u1", "r"),
        ]
        for src, owner, run in cases:
            self.assertEqual(files.adopt(self.t.dirs, NS, src, owner, run), {"state": "refused"}, (src, owner, run))
        self.assertTrue(self.t.exists(f"output/couchy/{OTHER}/u1/run9/theirs.png"))
        self.assertTrue(self.t.exists("models/model.safetensors"))

    def test_move_owner_rehomes_both_folders(self):
        self.t.write(f"output/couchy/{NS}/lab/run1/a.png", b"lab's")
        out = files.move_owner(self.t.dirs, NS, "u1", "lab")
        self.assertEqual(out, {"input": {"moved": 1}, "output": {"moved": 2}})
        self.assertTrue(self.t.exists(f"input/couchy/{NS}/lab/up.png"))
        self.assertTrue(self.t.exists(f"output/couchy/{NS}/lab/run1/a_1.png"))
        self.assertTrue(self.t.exists(f"output/couchy/{NS}/lab/run1/b.png"))
        self.assertTrue(os.path.isdir(self.t.path(f"output/couchy/{NS}/u1")))  # folders stay
        self.assertTrue(self.t.exists(f"output/couchy/{OTHER}/u1/run9/theirs.png"))  # the sneak link wasn't followed
        self.assertIsNone(files.move_owner(self.t.dirs, NS, "u1", "../x"))


if __name__ == "__main__":
    unittest.main()
