"""models.py: locations, listing and paging, symlinks, headers and space."""

import json
import os
import struct
import unittest

from couchy_agent import models
from tests.helpers import Tree


def safetensors(tensors=None, metadata=None, pad=0):
    header = dict(tensors if tensors is not None else {"w": {"dtype": "F16", "shape": [2, 2], "data_offsets": [0, 8]}})
    if metadata is not None:
        header["__metadata__"] = metadata
    raw = json.dumps(header).encode() + b" " * pad
    return struct.pack("<Q", len(raw)) + raw + b"\0" * 8


class Models(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.nas = os.path.join(self.t.base, "nas-hot")
        os.makedirs(self.nas)
        m = self.t.dirs["models"]
        self.host = {f: [os.path.join(m, d) for d in dirs] for f, dirs in models.LAYOUT.items()}
        self.locs = models.Locations(self.host, lambda: {"nas-hot": self.nas}).all()

    def tearDown(self):
        self.t.cleanup()

    def test_lists_host_and_shares_with_their_location(self):
        self.t.write("models/checkpoints/sdxl.safetensors", b"a" * 10)
        self.t.write("models/loras/style/ink.safetensors", b"b")
        self.t.write("nas-hot/checkpoints/flux.safetensors", b"c" * 3)
        out = models.list_models(self.locs, None)
        got = [(f["location"], f["folder"], f["path"], f["size"], f["sha256"], f["verifiedAt"]) for f in out["files"]]
        self.assertEqual(got, [
            ("host", "checkpoints", "sdxl.safetensors", 10, None, None),
            ("host", "loras", "style/ink.safetensors", 1, None, None),
            ("nas-hot", "checkpoints", "flux.safetensors", 3, None, None),
        ])
        self.assertNotIn("next", out)
        only = models.list_models(self.locs, None, location="nas-hot")
        self.assertEqual([f["path"] for f in only["files"]], ["flux.safetensors"])
        ckpt = models.list_models(self.locs, None, folder="checkpoints")
        self.assertEqual([f["location"] for f in ckpt["files"]], ["host", "nas-hot"])

    def test_folder_types_with_two_directories(self):
        self.t.write("models/clip/t5.safetensors")
        self.t.write("models/text_encoders/clip_l.safetensors")
        self.t.write("models/clip/clip_l.safetensors")  # hidden by text_encoders/, as ComfyUI resolves it
        out = models.list_models(self.locs, None, folder="text_encoders")
        self.assertEqual([f["path"] for f in out["files"]], ["clip_l.safetensors", "t5.safetensors"])
        self.assertEqual(models.resolve(self.host["text_encoders"], "clip_l.safetensors"),
                         os.path.realpath(self.t.path("models/text_encoders/clip_l.safetensors")))

    def test_paging(self):
        for i in range(5):
            self.t.write(f"models/loras/l{i}.safetensors")
        self.t.write("nas-hot/loras/n.safetensors")
        seen, after = [], ""
        while True:
            page = models.list_models(self.locs, None, after=after, limit=2)
            seen += [(f["location"], f["path"]) for f in page["files"]]
            if "next" not in page:
                break
            after = page["next"]
        self.assertEqual(seen, [("host", f"l{i}.safetensors") for i in range(5)] + [("nas-hot", "n.safetensors")])
        self.assertEqual(len(models.list_models(self.locs, None, limit=5000)["files"]), 6)  # capped at 1000, not refused

    def test_symlinks_are_never_listed_or_resolved(self):
        secret = self.t.write("secret.safetensors", safetensors())
        os.makedirs(self.t.path("models/checkpoints"))
        os.symlink(secret, self.t.path("models/checkpoints/link.safetensors"))
        os.makedirs(self.t.path("elsewhere"))
        self.t.write("elsewhere/x.safetensors", safetensors())
        os.symlink(self.t.path("elsewhere"), self.t.path("models/checkpoints/dir"))
        self.t.write("models/checkpoints/real.safetensors", safetensors())
        out = models.list_models(self.locs, None)
        self.assertEqual([f["path"] for f in out["files"]], ["real.safetensors"])
        dirs = self.host["checkpoints"]
        self.assertIsNone(models.resolve(dirs, "link.safetensors"))
        self.assertIsNone(models.resolve(dirs, "dir/x.safetensors"))
        for bad in ("../../secret.safetensors", "/etc/passwd", "a/../real.safetensors", "", "a\\b", "./real.safetensors"):
            self.assertIsNone(models.resolve(dirs, bad), bad)
        self.assertIsNotNone(models.resolve(dirs, "real.safetensors"))

    def test_a_folder_directory_that_is_itself_a_symlink_is_the_operators_config(self):
        big = os.path.join(self.t.base, "big")
        os.makedirs(big)
        with open(os.path.join(big, "huge.safetensors"), "wb") as f:
            f.write(b"x")
        os.symlink(big, self.t.path("models/checkpoints"))
        out = models.list_models(self.locs, None)
        self.assertEqual([f["path"] for f in out["files"]], ["huge.safetensors"])

    def test_host_folders_from_folder_paths(self):
        m = self.t.dirs["models"]
        fnp = {
            "checkpoints": ([os.path.join(m, "checkpoints"), os.path.join(self.nas, "checkpoints")], {".safetensors"}),
            "custom_nodes": ([os.path.join(self.t.base, "custom_nodes")], set()),
            "loras": ([os.path.join(m, "loras")], {".safetensors"}),
        }
        got = models.host_folders(fnp, [self.nas])
        self.assertEqual(got, {"checkpoints": [os.path.join(m, "checkpoints")], "loras": [os.path.join(m, "loras")]})

    # Headers -----------------------------------------------------------------

    def header_of(self, data):
        return models.read_header(self.t.write("models/checkpoints/h.safetensors", data))

    def test_header_of_a_normal_file(self):
        out = self.header_of(safetensors(
            {"a.weight": {"dtype": "BF16", "shape": [4, 8], "data_offsets": [0, 64]},
             "b.bias": {"dtype": "F32", "shape": [8], "data_offsets": [64, 96]}},
            metadata={"modelspec.architecture": "stable-diffusion-xl-v1-base"}))
        self.assertEqual(out["metadata"], {"modelspec.architecture": "stable-diffusion-xl-v1-base"})
        self.assertEqual(out["tensors"], [{"name": "a.weight", "dtype": "BF16", "shape": [4, 8]},
                                          {"name": "b.bias", "dtype": "F32", "shape": [8]}])
        self.assertEqual((out["tensorCount"], out["truncated"]), (2, False))

    def test_header_over_the_cap(self):
        with self.assertRaises(models.NotSafetensors):
            self.header_of(safetensors(metadata={"big": "x" * (models.HEADER_CAP + 10)}))
        # Only the length is read: a claimed 2 GB header is refused without reading it.
        with self.assertRaises(models.NotSafetensors):
            self.header_of(struct.pack("<Q", 2 ** 31) + b"{}")

    def test_more_than_2000_tensors(self):
        many = {f"t{i:05d}": {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]} for i in range(2500)}
        out = self.header_of(safetensors(many))
        self.assertEqual(len(out["tensors"]), models.TENSOR_CAP)
        self.assertEqual((out["tensorCount"], out["truncated"]), (2500, True))

    def test_not_safetensors(self):
        for data in (b"PK\x03\x04 a pickle zip" + b"\0" * 64, b"GGUF" + b"\0" * 64, b"", b"short",
                     struct.pack("<Q", 4) + b"[12]" + b"\0", struct.pack("<Q", 5) + b"{oops" + b"\0",
                     struct.pack("<Q", 50) + b"{}"):
            with self.assertRaises(models.NotSafetensors, msg=data[:12]):
                self.header_of(data)

    # Space -------------------------------------------------------------------

    def test_space_per_volume_and_location(self):
        self.t.write("models/checkpoints/a.safetensors")
        self.t.write("models/loras/b.safetensors")
        self.t.write("nas-hot/checkpoints/c.safetensors")
        out = models.space(self.locs)["volumes"]
        self.assertEqual([(v["location"], v["folders"]) for v in out],
                         [("host", ["checkpoints", "loras"]), ("nas-hot", ["checkpoints"])])
        for v in out:
            self.assertGreater(v["totalBytes"], 0)
            self.assertGreaterEqual(v["totalBytes"], v["freeBytes"])


if __name__ == "__main__":
    unittest.main()
