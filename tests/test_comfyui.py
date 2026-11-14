"""The nodes, the scope hook and the queue wrap inside a real ComfyUI: its
folder_paths, stock nodes, validate_prompt, PromptExecutor and PromptQueue.

Runs only with COMFYUI_PATH set to a ComfyUI checkout at the commit in
Couchy's runtime manifest (api/internal/runtime/manifest.json, v0.42.5,
483dae4) with its requirements installed; skipped otherwise:

    COMFYUI_PATH=~/ComfyUI python -m unittest tests.test_comfyui
"""

import asyncio
import json
import os
import shutil
import sys
import unittest
import uuid

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from couchy_agent import hook
from couchy_agent import scope as s
from couchy_agent.routes import Agent, Folders
from couchy_agent.store import Store
from tests.helpers import Tree

COMFY = os.environ.get("COMFYUI_PATH")
NS = "ns_aa11"
WORKFLOW = {"workflow": {"nodes": []}}


def setUpModule():
    if not COMFY:
        raise unittest.SkipTest("set COMFYUI_PATH to a ComfyUI checkout at the runtime manifest's commit")
    sys.path.insert(0, os.path.abspath(os.path.expanduser(COMFY)))
    global folder_paths, comfy_nodes, execution, utils, couchy_nodes, np, torch, Image
    from comfy.cli_args import args
    args.cpu = True  # as --cpu, before model_management picks a device
    import folder_paths
    import numpy as np
    import torch
    from PIL import Image
    import nodes as comfy_nodes
    import execution
    from comfy_execution import utils
    from couchy_agent import nodes as couchy_nodes
    T.tree = Tree()
    folder_paths.set_input_directory(T.tree.dirs["input"])
    folder_paths.set_output_directory(T.tree.dirs["output"])
    folder_paths.set_temp_directory(T.tree.dirs["temp"])
    T.scopes = s.Scopes()
    roots = lambda: {"input": folder_paths.get_input_directory(), "output": folder_paths.get_output_directory(),
                     "temp": folder_paths.get_temp_directory()}
    T.hook_on = hook.install(folder_paths, hook.Hook(T.scopes, utils.get_executing_context, roots,
                                                    lambda c: comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS.get(c, c)))
    couchy_nodes.configure(T.scopes)
    couchy_nodes.register("custom_nodes.couchy-agent")
    comfy_nodes.NODE_CLASS_MAPPINGS["CouchyTestExpand"] = Expand
    comfy_nodes.NODE_CLASS_MAPPINGS["CouchyTestLatent"] = MakeLatent
    write_fixtures(T.tree)


def tearDownModule():
    T.tree.cleanup()


class T:
    tree = scopes = hook_on = None


class Expand:
    """Expands into a stock LoadImage on a path it builds at run time."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"owner": ("STRING", {"default": ""})}}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "go"
    CATEGORY = "test"

    def go(self, owner):
        from comfy_execution.graph_utils import GraphBuilder
        g = GraphBuilder()
        load = g.node("LoadImage", image="/".join(["couchy", NS, owner, "r0", "old.png"]) + " [output]")
        return {"result": (load.out(0),), "expand": g.finalize()}


def make_latent(kind):
    """A latent with fixed values: one tensor, a nested video and audio pair
    (LTX's shapes), or one tensor with a noise mask."""
    from comfy.nested_tensor import NestedTensor
    g = torch.Generator().manual_seed(11)
    if kind == "nested":
        return {"samples": NestedTensor([torch.randn((1, 128, 16, 32, 24), generator=g),
                                         torch.randn((1, 8, 126, 16), generator=g)])}
    out = {"samples": torch.randn((1, 4, 8, 8), generator=g)}
    if kind == "masked":
        out["noise_mask"] = (torch.rand((1, 1, 64, 64), generator=g) > 0.5).float()
    return out


class MakeLatent:
    """Outputs make_latent(kind)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"kind": (["plain", "nested", "masked"],)}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "go"
    CATEGORY = "test"

    def go(self, kind):
        return (make_latent(kind),)


def write_fixtures(t):
    rng = np.random.default_rng(7)
    img = Image.fromarray(rng.integers(0, 255, (6, 8, 4), dtype=np.uint8), "RGBA")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotated: LoadImage turns it upright
    for rel in (f"input/couchy/{NS}/u1/up1.png", f"input/couchy/{NS}/lab/shared.png",
                f"input/couchy/{NS}/u2/theirs.png", f"output/couchy/{NS}/u1/r0/old.png",
                f"output/couchy/{NS}/u2/r0/old.png", "input/plain.png"):
        os.makedirs(os.path.dirname(t.path(rel)), exist_ok=True)
        img.save(t.path(rel), exif=exif.tobytes())
    import av
    path = t.path(f"input/couchy/{NS}/u1/clip.mp4")
    with av.open(path, "w") as c:
        st = c.add_stream("mpeg4", rate=8)
        st.width, st.height, st.pix_fmt = 16, 16, "yuv420p"
        for i in range(4):
            frame = av.VideoFrame.from_ndarray(np.full((16, 16, 3), i * 40, np.uint8), format="rgb24")
            for p in st.encode(frame):
                c.mux(p)
        for p in st.encode():
            c.mux(p)


class Server:
    """What PromptExecutor and PromptQueue ask of PromptServer."""
    client_id = None
    last_node_id = None

    def send_sync(self, *args, **kwargs):
        pass

    def queue_updated(self):
        pass


def scope(owner="u1", run="r1", inputs=("up1", "shared", "clip"), metadata=True, graph=None):
    return s.parse({"runId": run, "owner": owner, "inputs": list(inputs), "metadata": metadata}, NS, graph or {})


def run(prompt, sc=None):
    """Validate and execute a prompt as ComfyUI's worker does. Returns
    (ok, the error line or None)."""
    pid = str(uuid.uuid4())
    if sc is not None:
        T.scopes.bind(pid, s.Scope(sc.namespace, sc.owner, sc.run_id, sc.inputs, sc.metadata, s._classes(prompt)))
    try:
        valid = asyncio.run(execution.validate_prompt(pid, prompt, None))
        if not valid[0]:
            return False, json.dumps(valid[3]) + json.dumps(valid[1])
        e = execution.PromptExecutor(Server(), cache_type=execution.CacheType.CLASSIC,
                                     cache_args={"lru": 0, "ram": 0, "ram_inactive": 0})
        e.execute(prompt, pid, {"extra_pnginfo": WORKFLOW}, valid[2])
        errors = [m for ev, m in e.status_messages if ev == "execution_error"]
        return e.success, errors[0]["exception_message"] if errors else None
    finally:
        T.scopes.drop(pid)


def in_context(sc, fn):
    pid = str(uuid.uuid4())
    T.scopes.bind(pid, sc)
    try:
        with utils.CurrentNodeContext(pid, "1"):
            return fn()
    finally:
        T.scopes.drop(pid)


class NodesTest(unittest.TestCase):
    def setUp(self):
        for d in ("output", "temp"):
            shutil.rmtree(T.tree.path(f"{d}/couchy/{NS}/u1/r1"), ignore_errors=True)

    def test_the_hook_is_on_for_this_comfyui(self):
        self.assertIsNone(hook.mismatch(folder_paths))
        self.assertEqual(T.hook_on, (True, None))

    def test_nodes_register_under_couchy(self):
        for cls in couchy_nodes.NODES:
            schema = cls.GET_SCHEMA()
            self.assertIs(comfy_nodes.NODE_CLASS_MAPPINGS[schema.node_id], cls)
            self.assertEqual(schema.category, "couchy")
            self.assertTrue(comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS[schema.node_id].startswith("Couchy "))

    def test_load_image_matches_stock(self):
        rel = f"couchy/{NS}/u1/up1.png"
        image, mask = in_context(scope(), lambda: couchy_nodes.CouchyLoadImage.execute("up1").args)
        want = in_context(scope(), lambda: comfy_nodes.LoadImage().load_image(rel))
        self.assertTrue(torch.equal(image, want[0]) and torch.equal(mask, want[1]))
        self.assertEqual(tuple(image.shape), (1, 8, 6, 3))  # EXIF turned it upright
        self.assertGreater(float(mask.max()), 0)  # alpha came through
        # An input in scope that someone else owns (shared with Lab).
        in_context(scope(), lambda: couchy_nodes.CouchyLoadImage.execute("shared"))

    def test_load_mask_matches_stock(self):
        rel = f"couchy/{NS}/u1/up1.png"
        for channel in couchy_nodes.CHANNELS:
            (got,) = in_context(scope(), lambda: couchy_nodes.CouchyLoadMask.execute("up1", channel).args)
            (want,) = in_context(scope(), lambda: comfy_nodes.LoadImageMask().load_image_mask(rel, channel))
            self.assertTrue(torch.equal(got, want), channel)

    def test_load_video_matches_stock(self):
        from comfy_extras.nodes_video import LoadVideo
        got = in_context(scope(), lambda: couchy_nodes.CouchyLoadVideo.execute("clip"))
        want = in_context(scope(), lambda: LoadVideo.execute(f"couchy/{NS}/u1/clip.mp4"))
        a, b = got.args[0].get_components(), want.args[0].get_components()
        self.assertTrue(torch.equal(a.images, b.images))
        self.assertEqual(a.images.shape[0], 4)

    def test_out_of_scope_ids_fail(self):
        with self.assertRaises(s.NotInRun) as e:
            in_context(scope(), lambda: couchy_nodes.CouchyLoadImage.execute("theirs"))
        self.assertEqual(str(e.exception), "This file isn't part of this run. (Couchy Load Image)")
        for upload in ("../u2/theirs", "theirs"):
            ok, line = run({"1": {"class_type": "CouchyLoadImage", "inputs": {"upload": upload}},
                            "2": {"class_type": "CouchySaveImage", "inputs": {"images": ["1", 0]}}}, scope())
            self.assertFalse(ok)
            self.assertIn("This file isn't part of this run. (Couchy Load Image)", line)

    def test_unscoped_prompts_fail_with_the_line(self):
        ok, line = run({"1": {"class_type": "CouchyLoadImage", "inputs": {"upload": "up1"}},
                        "2": {"class_type": "PreviewImage", "inputs": {"images": ["1", 0]}}})
        self.assertFalse(ok)
        self.assertIn("This node only runs in runs Couchy starts. (Couchy Load Image)", line)
        ok, line = run({"1": {"class_type": "LoadImage", "inputs": {"image": "plain.png"}},
                        "2": {"class_type": "CouchySaveImage", "inputs": {"images": ["1", 0]}}})
        self.assertFalse(ok)
        self.assertIn("This node only runs in runs Couchy starts. (Couchy Save Image)", line)

    def test_saves_land_in_the_run_folder_as_stock_does(self):
        graph = {"1": {"class_type": "CouchyLoadImage", "inputs": {"upload": "up1"}},
                 "2": {"class_type": "CouchySaveImage", "inputs": {"images": ["1", 0]}},
                 "3": {"class_type": "CouchyPreviewImage", "inputs": {"images": ["1", 0]}}}
        self.assertEqual(run(graph, scope()), (True, None))
        self.assertEqual(run(graph, scope()), (True, None))  # not answered from the cache
        run_dir = T.tree.path(f"output/couchy/{NS}/u1/r1")
        self.assertEqual(sorted(os.listdir(run_dir)), ["ComfyUI_00001_.png", "ComfyUI_00002_.png"])
        temp = os.listdir(T.tree.path(f"temp/couchy/{NS}/u1/r1"))
        self.assertEqual(len(temp), 2)
        self.assertTrue(all(n.startswith("ComfyUI_temp_") for n in temp))

        stock = {"1": {"class_type": "LoadImage", "inputs": {"image": "plain.png"}},
                 "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "stock"}}}
        self.assertEqual(run(stock), (True, None))
        mine, theirs = Image.open(os.path.join(run_dir, "ComfyUI_00001_.png")), Image.open(T.tree.path("output/stock_00001_.png"))
        self.assertEqual(mine.tobytes(), theirs.tobytes())
        self.assertEqual(set(mine.text), {"prompt", "workflow"})
        self.assertEqual(set(mine.text), set(theirs.text))
        self.assertEqual(json.loads(mine.text["workflow"]), WORKFLOW["workflow"])

    def test_a_run_can_ask_for_no_metadata(self):
        graph = {"1": {"class_type": "CouchyLoadImage", "inputs": {"upload": "up1"}},
                 "2": {"class_type": "CouchySaveImage", "inputs": {"images": ["1", 0]}}}
        self.assertEqual(run(graph, scope(metadata=False)), (True, None))
        img = Image.open(T.tree.path(f"output/couchy/{NS}/u1/r1/ComfyUI_00001_.png"))
        self.assertEqual(img.text, {})


class LatentTest(unittest.TestCase):
    RUN = f"output/couchy/{NS}/u1/r1"

    def setUp(self):
        shutil.rmtree(T.tree.path(self.RUN), ignore_errors=True)

    def save(self, kind):
        """Runs MakeLatent into Couchy Save Latent; the file it wrote, relative to output/."""
        graph = {"1": {"class_type": "CouchyTestLatent", "inputs": {"kind": kind}},
                 "2": {"class_type": "CouchySaveLatent", "inputs": {"samples": ["1", 0]}}}
        self.assertEqual(run(graph, scope()), (True, None))
        names = sorted(os.listdir(T.tree.path(self.RUN)))
        self.assertTrue(names and names[-1].endswith("_.latent"), names)
        return f"couchy/{NS}/u1/r1/{names[-1]}"

    def as_upload(self, rel, upload):
        """Copies an output file into u1's uploads as <upload>.latent."""
        shutil.copy(T.tree.path(f"output/{rel}"), T.tree.path(f"input/couchy/{NS}/u1/{upload}.latent"))

    def load(self, upload):
        sc = scope(inputs=(upload,))
        return in_context(sc, lambda: couchy_nodes.CouchyLoadLatent.execute(upload).args[0])

    def test_the_latent_nodes_register(self):
        for node_id, display in (("CouchySaveLatent", "Couchy Save Latent"), ("CouchyLoadLatent", "Couchy Load Latent")):
            self.assertEqual(comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS[node_id], display)
            self.assertEqual(comfy_nodes.NODE_CLASS_MAPPINGS[node_id].GET_SCHEMA().category, "couchy")

    def test_couchy_save_then_stock_load(self):
        rel = self.save("plain")
        self.assertEqual(rel, f"couchy/{NS}/u1/r1/ComfyUI_00001_.latent")  # the run's folder, stock's numbering
        (got,) = in_context(scope(), lambda: comfy_nodes.LoadLatent().load(rel + " [output]"))
        self.assertTrue(torch.equal(got["samples"], make_latent("plain")["samples"]))

    def test_stock_save_then_couchy_load(self):
        want = make_latent("plain")
        out = comfy_nodes.SaveLatent().save(want, filename_prefix="stocklatent", prompt={"p": 1}, extra_pnginfo=WORKFLOW)
        rel = out["ui"]["latents"][0]["filename"]
        self.as_upload(rel, "stocklat")
        self.assertTrue(torch.equal(self.load("stocklat")["samples"], want["samples"]))

    def test_one_tensor_is_written_as_stock_writes_it(self):
        from safetensors import safe_open
        mine = T.tree.path(f"output/{self.save('plain')}")
        out = comfy_nodes.SaveLatent().save(make_latent("plain"), filename_prefix="stockkeys", prompt={"p": 1},
                                            extra_pnginfo=WORKFLOW)
        theirs = T.tree.path(f"output/{out['ui']['latents'][0]['filename']}")
        with safe_open(mine, "pt") as a, safe_open(theirs, "pt") as b:
            self.assertEqual(sorted(a.keys()), sorted(b.keys()))
            self.assertEqual(sorted(a.metadata()), sorted(b.metadata()))
            self.assertEqual(json.loads(a.metadata()["workflow"]), WORKFLOW["workflow"])
            for k in a.keys():
                self.assertTrue(torch.equal(a.get_tensor(k), b.get_tensor(k)), k)

    def test_a_nested_latent_round_trips(self):
        self.as_upload(self.save("nested"), "nested")
        got, want = self.load("nested")["samples"], make_latent("nested")["samples"]
        self.assertTrue(got.is_nested)
        self.assertEqual(len(got.tensors), 2)
        for a, b in zip(got.tensors, want.tensors):
            self.assertTrue(torch.equal(a, b))

    def test_the_noise_mask_round_trips(self):
        self.as_upload(self.save("masked"), "masked")
        got, want = self.load("masked"), make_latent("masked")
        self.assertTrue(torch.equal(got["samples"], want["samples"]))
        self.assertTrue(torch.equal(got["noise_mask"], want["noise_mask"]))

    def test_load_refuses_an_upload_outside_the_run(self):
        self.as_upload(self.save("plain"), "mine")
        with self.assertRaises(s.NotInRun) as e:
            in_context(scope(inputs=("up1",)), lambda: couchy_nodes.CouchyLoadLatent.execute("mine"))
        self.assertEqual(str(e.exception), "This file isn't part of this run. (Couchy Load Latent)")
        ok, line = run({"1": {"class_type": "CouchyLoadLatent", "inputs": {"upload": "mine"}},
                        "2": {"class_type": "CouchySaveLatent", "inputs": {"samples": ["1", 0]}}}, scope(inputs=("up1",)))
        self.assertFalse(ok)
        self.assertIn("This file isn't part of this run. (Couchy Load Latent)", line)

    def test_save_ignores_a_prefix_and_needs_a_run(self):
        # A graph carried over from stock SaveLatent keeps its filename_prefix; it is dropped.
        graph = {"1": {"class_type": "CouchyTestLatent", "inputs": {"kind": "plain"}},
                 "2": {"class_type": "CouchySaveLatent", "inputs": {"samples": ["1", 0], "filename_prefix": "../../x"}}}
        self.assertEqual(run(graph, scope()), (True, None))
        self.assertEqual(os.listdir(T.tree.path(self.RUN)), ["ComfyUI_00001_.latent"])
        ok, line = run(graph)
        self.assertFalse(ok)
        self.assertIn("This node only runs in runs Couchy starts. (Couchy Save Latent)", line)


class HookTest(unittest.TestCase):
    def save(self, prefix):
        return {"1": {"class_type": "LoadImage", "inputs": {"image": "plain.png"}},
                "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": prefix}}}

    def test_stock_nodes_in_a_scoped_run(self):
        own = {"1": {"class_type": "LoadImage", "inputs": {"image": f"couchy/{NS}/u1/r0/old.png [output]"}},
               "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": f"couchy/{NS}/u1/r1/x"}}}
        self.assertEqual(run(own, scope()), (True, None))
        theirs = {"1": {"class_type": "LoadImage", "inputs": {"image": f"couchy/{NS}/u2/r0/old.png [output]"}},
                  "2": {"class_type": "PreviewImage", "inputs": {"images": ["1", 0]}}}
        ok, line = run(theirs, scope())
        self.assertFalse(ok)
        self.assertIn("This file isn't part of this run. (Load Image)", line)

    def test_saves_outside_the_run_folder_are_refused(self):
        for prefix in (f"couchy/{NS}/u1/r0/x", f"couchy/{NS}/u2/r1/x", f"couchy/{NS}/x"):
            ok, line = run(self.save(prefix), scope())
            self.assertFalse(ok, prefix)
            self.assertIn("This file isn't part of this run. (Save Image)", line)
        self.assertFalse(os.path.exists(T.tree.path(f"output/couchy/{NS}/u2/r1")))
        self.assertEqual(run(self.save("plain"), scope()), (True, None))  # outside couchy/

    def test_unscoped_prompts_get_nothing_inside_couchy(self):
        ok, line = run({"1": {"class_type": "LoadImage", "inputs": {"image": f"couchy/{NS}/u1/up1.png"}},
                        "2": {"class_type": "PreviewImage", "inputs": {"images": ["1", 0]}}})
        self.assertFalse(ok)
        self.assertIn("This file isn't part of this run.", line)
        self.assertFalse(run(self.save(f"couchy/{NS}/u1/r1/x"))[0])
        self.assertEqual(run(self.save("unscoped")), (True, None))
        self.assertTrue(os.path.exists(folder_paths.get_annotated_filepath("plain.png")))

    def test_expanded_nodes_stay_scoped(self):
        graph = {"1": {"class_type": "CouchyTestExpand", "inputs": {"owner": "u1"}},
                 "2": {"class_type": "CouchySaveImage", "inputs": {"images": ["1", 0]}}}
        self.assertEqual(run(graph, scope()), (True, None))
        graph["1"]["inputs"]["owner"] = "u2"
        ok, line = run(graph, scope())
        self.assertFalse(ok)
        self.assertIn("This file isn't part of this run. (CouchyTestExpand)", line)


class PromptTest(unittest.IsolatedAsyncioTestCase):
    """POST /prompt through the agent's middleware into a real PromptQueue,
    with the handler doing what ComfyUI's post_prompt does with the body."""

    async def asyncSetUp(self):
        self.store = Store(T.tree.dirs["user"])
        self.agent = Agent(self.store, Folders(T.tree.dirs))
        self.agent.scopes = T.scopes  # as comfy.py shares them with the nodes and the hook
        self.queue = execution.PromptQueue(Server())
        s.watch_queue(self.queue, self.agent.scopes)
        app = web.Application(middlewares=[self.agent.middleware()])
        self.agent.add_routes(app.router)
        app.router.add_post("/prompt", self.post_prompt)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        code = self.store.mint_code()
        out = await (await self.client.post("/couchy/pair", json={"code": code})).json()
        self.ns, self.key = out["namespace"], out["key"]

    async def asyncTearDown(self):
        await self.client.close()

    async def post_prompt(self, request):
        json_data = await request.json()
        prompt_id = json_data.get("prompt_id") or str(uuid.uuid4())
        valid = await execution.validate_prompt(prompt_id, json_data["prompt"], None)
        if not valid[0]:
            return web.json_response({"error": valid[1], "node_errors": valid[3]}, status=400)
        extra_data = json_data.get("extra_data", {})
        self.queue.put((0, prompt_id, json_data["prompt"], extra_data, valid[2], {}))
        return web.json_response({"prompt_id": prompt_id})

    def work(self):
        item, item_id = self.queue.get(timeout=1)
        e = execution.PromptExecutor(Server(), cache_type=execution.CacheType.CLASSIC,
                                     cache_args={"lru": 0, "ram": 0, "ram_inactive": 0})
        e.execute(item[2], item[1], item[3], item[4])
        self.queue.task_done(item_id, e.history_result, status=None, process_item=lambda p: p[:5] + p[6:])
        return item[1], e.success

    def body(self, upload="up1"):
        return {"prompt": {"1": {"class_type": "PreviewImage", "inputs": {"images": ["2", 0]}},
                           "2": {"class_type": "CouchyLoadImage", "inputs": {"upload": upload}}},
                "extra_data": {"couchy": {"runId": "r1", "owner": "u1", "inputs": ["up1"]}}}

    def headers(self):
        return {"Authorization": f"Couchy-Agent {self.key}"}

    async def test_the_scope_never_reaches_history_and_goes_when_done(self):
        os.makedirs(T.tree.path(f"input/couchy/{self.ns}/u1"), exist_ok=True)
        shutil.copy(T.tree.path("input/plain.png"), T.tree.path(f"input/couchy/{self.ns}/u1/up1.png"))
        r = await self.client.post("/prompt", json=self.body(), headers=self.headers())
        self.assertEqual(r.status, 200, await r.text())
        pid = (await r.json())["prompt_id"]
        self.assertIsNotNone(self.agent.scopes.get(pid))
        done, ok = await asyncio.to_thread(self.work)
        self.assertEqual((done, ok), (pid, True))
        history = self.queue.get_history(prompt_id=pid)[pid]
        self.assertNotIn("couchy", json.dumps(history["prompt"][3]))
        self.assertIsNone(self.agent.scopes.get(pid))

    async def test_a_refused_or_deleted_prompt_drops_its_scope(self):
        r = await self.client.post("/prompt", json=self.body("someone-else"), headers=self.headers())
        self.assertEqual(r.status, 400)  # validation: not in scope
        self.assertIn("This file isn't part of this run.", await r.text())
        self.assertEqual(len(self.agent.scopes), 0)
        os.makedirs(T.tree.path(f"input/couchy/{self.ns}/u1"), exist_ok=True)
        shutil.copy(T.tree.path("input/plain.png"), T.tree.path(f"input/couchy/{self.ns}/u1/up1.png"))
        pids = []
        for _ in range(2):
            r = await self.client.post("/prompt", json=self.body(), headers=self.headers())
            pids.append((await r.json())["prompt_id"])
        self.assertTrue(self.queue.delete_queue_item(lambda item: item[1] == pids[0]))
        self.assertEqual([p for p in pids if self.agent.scopes.has(p)], [pids[1]])
        self.queue.wipe_queue()
        self.assertEqual(len(self.agent.scopes), 0)


if __name__ == "__main__":
    unittest.main()
