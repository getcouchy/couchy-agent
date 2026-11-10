"""The scope hook on a stand-in for folder_paths that has ComfyUI v0.42.5's
signatures and semantics for the three wrapped functions. test_comfyui.py
runs the same checks on the real module."""

import os
import types
import unittest

from couchy_agent import hook
from couchy_agent import scope as s
from tests.helpers import Tree

NS = "ns_aa11"


def fake_folder_paths(t):
    fp = types.ModuleType("folder_paths")
    fp.get_input_directory = lambda: t.dirs["input"]
    fp.get_output_directory = lambda: t.dirs["output"]
    fp.get_temp_directory = lambda: t.dirs["temp"]

    def annotated_filepath(name):
        for tag, d in (("[output]", "output"), ("[input]", "input"), ("[temp]", "temp")):
            if name.endswith(tag):
                return name[:-len(tag) - 1], t.dirs[d]
        return name, None

    def get_annotated_filepath(name, default_dir=None):
        name, base = annotated_filepath(name)
        return os.path.abspath(os.path.join(base or default_dir or t.dirs["input"], name))

    def exists_annotated_filepath(name):
        name, base = annotated_filepath(name)
        return os.path.exists(os.path.abspath(os.path.join(base or t.dirs["input"], name)))

    def get_save_image_path(filename_prefix, output_dir, image_width=0, image_height=0):
        sub = os.path.dirname(os.path.normpath(filename_prefix))
        full = os.path.join(output_dir, sub)
        os.makedirs(full, exist_ok=True)
        return full, os.path.basename(filename_prefix), 1, sub, filename_prefix

    fp.annotated_filepath = annotated_filepath
    fp.get_annotated_filepath = get_annotated_filepath
    fp.exists_annotated_filepath = exists_annotated_filepath
    fp.get_save_image_path = get_save_image_path
    return fp


class HookTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.fp = fake_folder_paths(self.t)
        self.scopes = s.Scopes()
        self.ctx = None
        self.hook = hook.Hook(self.scopes, lambda: self.ctx, lambda: self.t.dirs, lambda c: f"<{c}>")
        ok, why = hook.install(self.fp, self.hook)
        self.assertTrue(ok, why)
        self.t.write(f"input/couchy/{NS}/u1/up1.png")
        self.t.write(f"input/couchy/{NS}/lab/shared.png")
        self.t.write(f"input/couchy/{NS}/u2/theirs.png")
        self.t.write(f"output/couchy/{NS}/u1/r0/old.png")
        self.t.write(f"output/couchy/{NS}/u2/r2/b.png")
        self.t.write("input/plain.png")
        graph = {"5": {"class_type": "LoadImage", "inputs": {}}}
        self.scopes.bind("p1", s.parse({"runId": "r1", "owner": "u1", "inputs": ["shared"]}, NS, graph))
        self.scopes.bind("pn", s.namespace_scope(NS, graph))

    def tearDown(self):
        self.t.cleanup()

    def run_as(self, prompt_id, node="5"):
        self.ctx = types.SimpleNamespace(prompt_id=prompt_id, node_id=node) if prompt_id else None

    def read(self, name):
        return self.fp.get_annotated_filepath(name)

    def refused(self, fn, *args):
        with self.assertRaises(s.NotInRun) as e:
            fn(*args)
        return str(e.exception)

    def test_reads_in_scope(self):
        self.run_as("p1")
        self.read(f"couchy/{NS}/u1/up1.png")  # the owner's own upload
        self.read(f"couchy/{NS}/u1/r0/old.png [output]")  # the owner's own output
        self.read(f"couchy/{NS}/lab/shared.png")  # an input in scope
        self.assertTrue(self.fp.exists_annotated_filepath(f"couchy/{NS}/lab/shared.png"))

    def test_refuses_another_owner(self):
        self.run_as("p1")
        line = self.refused(self.read, f"couchy/{NS}/u2/theirs.png")
        self.assertEqual(line, "This file isn't part of this run. (<LoadImage>)")
        self.refused(self.read, f"couchy/{NS}/u2/r2/b.png [output]")  # the [output] suffix
        self.refused(self.fp.exists_annotated_filepath, f"couchy/{NS}/u2/r2/b.png [output]")
        # A path built at run time, with dots and doubled slashes.
        self.refused(self.read, f"couchy/{NS}/u1/../u2//theirs.png")
        self.refused(self.read, f"x/../couchy/{NS}/u2/theirs.png")
        self.refused(self.read, f"COUCHY/{NS}/u2/theirs.png")
        # An input id only counts at the upload's own place.
        self.refused(self.read, f"couchy/{NS}/lab/sub/shared.png")
        self.refused(self.read, f"couchy/{NS}/lab/shared.png [output]")
        # Another namespace, and couchy/ itself.
        self.refused(self.read, "couchy/ns_other/u1/up1.png")
        self.refused(self.read, f"couchy/{NS}")

    def test_symlinks_are_resolved(self):
        self.run_as("p1")
        os.symlink(self.t.path(f"input/couchy/{NS}/u2/theirs.png"), self.t.path("input/innocent.png"))
        self.refused(self.read, "innocent.png")
        os.symlink(self.t.path(f"input/couchy/{NS}/u2"), self.t.path(f"input/couchy/{NS}/u1/link"))
        self.refused(self.read, f"couchy/{NS}/u1/link/theirs.png")

    def test_outside_couchy_is_untouched(self):
        for pid in ("p1", None):
            self.run_as(pid)
            self.read("plain.png")
            self.read("anything [output]")
            self.assertTrue(self.fp.exists_annotated_filepath("plain.png"))
            self.fp.get_save_image_path("ComfyUI", self.t.dirs["output"])
            self.hook.check_read(self.t.path("models/model.safetensors"))

    def test_unscoped_gets_nothing_inside_couchy(self):
        for pid in (None, "unknown-prompt"):
            self.run_as(pid)
            line = self.refused(self.read, f"couchy/{NS}/u1/up1.png")
            self.assertEqual(line, "This file isn't part of this run. (node 5)" if pid else "This file isn't part of this run.")
            self.refused(self.fp.get_save_image_path, f"couchy/{NS}/u1/r1/x", self.t.dirs["output"])

    def test_namespace_scope(self):
        self.run_as("pn")
        self.read(f"couchy/{NS}/u2/theirs.png")
        self.fp.get_save_image_path(f"couchy/{NS}/u2/r9/x", self.t.dirs["output"])
        self.refused(self.read, "couchy/ns_other/u1/up1.png")

    def test_saves(self):
        self.run_as("p1")
        out = self.fp.get_save_image_path(f"couchy/{NS}/u1/r1/ComfyUI", self.t.dirs["output"])
        self.assertTrue(out[0].endswith(f"couchy/{NS}/u1/r1"))
        self.fp.get_save_image_path(f"couchy/{NS}/u1/r1/ComfyUI_temp_x", self.t.dirs["temp"])
        for prefix in (f"couchy/{NS}/u1/r0/x", f"couchy/{NS}/u2/r1/x", f"couchy/{NS}/u1/x",
                       f"couchy/{NS}/x", "couchy/x", f"couchy/{NS}/u1/r1/../r0/x"):
            self.refused(self.fp.get_save_image_path, prefix, self.t.dirs["output"])
        # Refused before ComfyUI makes the folder.
        self.assertFalse(self.t.exists(f"output/couchy/{NS}/u2/r1"))
        self.refused(self.fp.get_save_image_path, f"couchy/{NS}/u1/r1/x", self.t.dirs["input"])

    def test_expanded_nodes_are_named_by_their_parent(self):
        self.run_as("p1", node="5.0.0.2")
        line = self.refused(self.read, f"couchy/{NS}/u2/theirs.png")
        self.assertIn("(<LoadImage>)", line)

    def test_installs_once(self):
        wrapped = self.fp.get_annotated_filepath
        self.assertEqual(hook.install(self.fp, self.hook), (True, None))
        self.assertIs(self.fp.get_annotated_filepath, wrapped)


class MismatchTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()

    def tearDown(self):
        self.t.cleanup()

    def test_fake_matches(self):
        self.assertIsNone(hook.mismatch(fake_folder_paths(self.t)))

    def test_signature_mismatch_leaves_folder_paths_alone(self):
        for name, fn in (("get_annotated_filepath", lambda name, default_dir=None, strict=True: name),
                         ("exists_annotated_filepath", lambda path: True),
                         ("get_save_image_path", lambda filename_prefix, output_dir, image_width=0: None)):
            fp = fake_folder_paths(self.t)
            setattr(fp, name, fn)
            before = (fp.get_annotated_filepath, fp.exists_annotated_filepath, fp.get_save_image_path)
            ok, why = hook.install(fp, hook.Hook(s.Scopes(), lambda: None, lambda: self.t.dirs))
            self.assertFalse(ok)
            self.assertIn(name, why)
            self.assertEqual(before, (fp.get_annotated_filepath, fp.exists_annotated_filepath, fp.get_save_image_path))

    def test_missing_function(self):
        fp = fake_folder_paths(self.t)
        del fp.get_save_image_path
        self.assertIn("missing", hook.mismatch(fp))


if __name__ == "__main__":
    unittest.main()
