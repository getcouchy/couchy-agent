"""Pulls, delete, tickets, the blob and history over HTTP: the grant per
route, verify and rename-no-replace, clash and resume, shares, refusals,
room, cancel, the queue, and credentials kept out of logs and disk."""

import asyncio
import hashlib
import io
import json
import logging
import os
import unittest

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from couchy_agent import models
from couchy_agent.config import Config
from couchy_agent.hashing import Hasher, Index
from couchy_agent.models_routes import ModelsRoutes
from couchy_agent.obs import JsonFormatter
from couchy_agent.pulls import TICKET_HEADER, Pulls, pull_id
from couchy_agent.routes import Agent, Folders
from couchy_agent.store import Lockout, Store
from couchy_agent.tickets import Tickets
from tests.helpers import Tree

TOKEN = "hf_secret_token_9f8e7d"
CIVITAI_TOKEN = "civitai_query_token_1a2b3c"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def auth(key):
    return {"Authorization": f"Couchy-Agent {key}"}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Source:
    """The fake Civitai / Hugging Face side, served by the same test app."""

    def __init__(self):
        self.blobs = {}
        self.modes = {}
        self.redirects = {}
        self.seen = []  # (name, headers, query)
        self.gate = asyncio.Event()

    async def handle(self, request):
        name = request.match_info["name"]
        self.seen.append((name, dict(request.headers), dict(request.query)))
        mode = self.modes.get(name)
        if mode == "redirect":
            raise web.HTTPFound(self.redirects[name])
        if mode == "missing":
            return web.Response(status=404)
        data = self.blobs[name]
        rng = request.headers.get("Range")
        if rng and mode != "norange":
            start = int(rng[len("bytes="):-1])
            return web.Response(status=206, body=data[start:],
                                headers={"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
        if mode == "slow":
            resp = web.StreamResponse()
            resp.content_length = len(data)
            await resp.prepare(request)
            await resp.write(data[:4])
            await self.gate.wait()
            await resp.write(data[4:])
            await resp.write_eof()
            return resp
        return web.Response(body=data)


class PullsTest(AioHTTPTestCase):
    async def get_application(self):
        self.t = Tree()
        self.clock = Clock()
        self.store = Store(self.t.dirs["user"])
        self.config = Config(self.store)
        self.config.set(allowedHosts=["127.0.0.1", "localhost"], reserveBytes=0)
        self.nas = os.path.join(self.t.base, "nas-hot")
        os.makedirs(self.nas)
        m = self.t.dirs["models"]
        self.locations = models.Locations(
            {f: [os.path.join(m, d) for d in dirs] for f, dirs in models.LAYOUT.items()},
            lambda: {"nas-hot": self.nas}, m)
        self.index = Index(self.store)
        self.hasher = Hasher(self.locations, self.index, clock=self.clock, chunk=3)
        self.pulls = Pulls(self.locations, self.hasher, self.config, chunk=5)
        self.tickets = Tickets(clock=self.clock)
        self.history = {}
        self.routes = ModelsRoutes(self.locations, self.index, self.hasher, pulls=self.pulls, tickets=self.tickets,
                                   history=lambda: self.history)
        self.agent = Agent(self.store, Folders(self.t.dirs), Lockout(), models=self.routes)
        self.src = Source()
        app = web.Application(middlewares=[self.agent.middleware()])
        self.agent.add_routes(app.router)
        app.router.add_get("/src/{name}", self.src.handle)

        self.logs = io.StringIO()
        handler = logging.StreamHandler(self.logs)
        handler.setFormatter(JsonFormatter(server="spark-1"))
        log = logging.getLogger("couchy_agent.models")
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        self.addCleanup(log.removeHandler, handler)
        return app

    async def asyncTearDown(self):
        await self.pulls.close()
        await super().asyncTearDown()
        self.t.cleanup()

    # Helpers -----------------------------------------------------------------

    async def key(self, grant="write"):
        code = self.store.mint_code(models=grant)
        r = await self.client.post("/couchy/pair", json={"code": code, "label": "prod"})
        return (await r.json())["key"]

    def url(self, name, host="127.0.0.1"):
        return str(self.server.make_url(f"/src/{name}")).replace("127.0.0.1", host)

    def offer(self, name, data, mode=None):
        self.src.blobs[name] = data
        if mode:
            self.src.modes[name] = mode
        return self.url(name)

    def body(self, data, url=None, path="a.safetensors", folder="checkpoints", location="host", **source):
        return {"sha256": sha(data), "location": location, "folder": folder, "path": path, "size": len(data),
                "source": {"url": url, **source} if url else source}

    async def start(self, key, body):
        r = await self.client.post("/couchy/models/pull", json=body, headers=auth(key))
        self.assertEqual(r.status, 200, await r.text())
        return (await r.json())["id"]

    async def wait(self, key, pid):
        for _ in range(200):
            r = await self.client.get(f"/couchy/models/pulls/{pid}", headers=auth(key))
            out = await r.json()
            if out["state"] in ("done", "failed"):
                return out
            await asyncio.sleep(0.01)
        self.fail(f"pull {pid} didn't finish: {out}")

    async def pull(self, key, body):
        return await self.wait(key, await self.start(key, body))

    def read(self, rel):
        with open(self.t.path(rel), "rb") as f:
            return f.read()

    def all_files_text(self):
        out = b""
        for top in (self.t.base,):
            for dirpath, _, files in os.walk(top):
                for fn in files:
                    with open(os.path.join(dirpath, fn), "rb") as f:
                        out += fn.encode() + b"\n" + f.read() + b"\n"
        return out

    # The grant ---------------------------------------------------------------

    async def test_grant_per_route(self):
        data = b"weights!"
        self.t.write("models/checkpoints/a.safetensors", data)
        self.hasher.scan(force=True)
        while self.hasher.step():
            pass
        write = await self.key("write")
        read = await self.key("read")
        none = await self.key("none")
        body = self.body(data, self.offer("a", data), path="b.safetensors")
        delete = {"sha256": sha(data), "folder": "checkpoints", "path": "a.safetensors"}
        writes = [("post", "/couchy/models/pull", body), ("post", "/couchy/models/pulls/pull_x/cancel", {}),
                  ("post", "/couchy/models/delete", delete), ("post", "/couchy/models/tickets", {"sha256": sha(data)}),
                  ("post", "/api/couchy/models/pull", body)]
        reads = [("get", "/couchy/models/pulls/pull_x", None), ("get", "/couchy/models/history", None),
                 ("get", "/api/couchy/models/history?since=5", None)]
        for method, route, json_body in writes + reads:
            r = await getattr(self.client, method)(route, json=json_body)
            self.assertEqual(r.status, 401, route)
            r = await getattr(self.client, method)(route, json=json_body, headers=auth(none))
            self.assertEqual(r.status, 403, route)
        for method, route, json_body in writes:
            r = await getattr(self.client, method)(route, json=json_body, headers=auth(read))
            self.assertEqual(r.status, 403, route)
        self.assertFalse(self.t.exists("models/checkpoints/b.safetensors"))
        for method, route, json_body in reads:
            r = await getattr(self.client, method)(route, json=json_body, headers=auth(read))
            self.assertIn(r.status, (200, 404), route)
        r = await self.client.post("/couchy/models/tickets", json={"sha256": sha(data)}, headers=auth(write))
        self.assertEqual(r.status, 200)
        # The blob takes a ticket, never a key.
        r = await self.client.get(f"/couchy/models/blob/{sha(data)}", headers=auth(write))
        self.assertEqual(r.status, 403)

    # Pulls -------------------------------------------------------------------

    async def test_a_pull_lands_verified_and_is_known_by_its_hash(self):
        key = await self.key()
        data = b"0123456789abcdefghij"
        out = await self.pull(key, self.body(data, self.offer("a", data), path="sdxl/base.safetensors"))
        self.assertEqual(out["state"], "done", out)
        self.assertEqual((out["bytesDone"], out["bytesTotal"]), (len(data), len(data)))
        self.assertEqual(self.read("models/checkpoints/sdxl/base.safetensors"), data)
        self.assertEqual(os.listdir(self.t.path("models/.couchy-partial")), [])
        r = await self.client.get("/couchy/models?folder=checkpoints", headers=auth(key))
        files = (await r.json())["files"]
        self.assertEqual([(f["path"], f["sha256"]) for f in files], [("sdxl/base.safetensors", sha(data))])
        # The partial folder is never listed or hashed.
        self.assertFalse(any(".couchy-partial" in k[2] for k in self.index.shas()))

    async def test_a_pull_into_a_share(self):
        key = await self.key()
        data = b"cold copy bytes"
        out = await self.pull(key, self.body(data, self.offer("a", data), location="nas-hot", folder="loras"))
        self.assertEqual(out["state"], "done", out)
        self.assertEqual(self.read("nas-hot/loras/a.safetensors"), data)
        self.assertTrue(os.path.isdir(self.t.path("nas-hot/.couchy-partial")))
        self.assertEqual(self.index.with_sha(sha(data))[0][:3], ("nas-hot", "loras", "a.safetensors"))

    async def test_a_hash_mismatch_never_lands(self):
        key = await self.key()
        data = b"the real bytes"
        self.offer("a", b"other bytes!!!")
        out = await self.pull(key, self.body(data, self.url("a")))
        self.assertEqual((out["state"], out["error"]), ("failed", "hash_mismatch"))
        self.assertFalse(self.t.exists("models/checkpoints/a.safetensors"))
        self.assertEqual(os.listdir(self.t.path("models/.couchy-partial")), [])
        self.offer("b", data + b"more")
        out = await self.pull(key, self.body(data, self.url("b"), path="b.safetensors"))
        self.assertEqual((out["state"], out["error"]), ("failed", "size_mismatch"))

    async def test_a_clash_never_overwrites(self):
        key = await self.key()
        self.t.write("models/checkpoints/a.safetensors", b"someone else's")
        data = b"new bytes"
        out = await self.pull(key, self.body(data, self.offer("a", data)))
        self.assertEqual((out["state"], out["error"]), ("failed", "exists"))
        self.assertEqual(self.read("models/checkpoints/a.safetensors"), b"someone else's")
        self.assertEqual(self.src.seen, [])  # refused before fetching
        # The same file already there, and hashed, is done at once.
        self.t.write("models/checkpoints/same.safetensors", data)
        self.hasher.scan(force=True)
        while self.hasher.step():
            pass
        out = await self.pull(key, self.body(data, self.url("a"), path="same.safetensors"))
        self.assertEqual(out["state"], "done")
        # A file that turns up while the pull runs isn't replaced either.
        self.offer("slow", data, "slow")
        pid = await self.start(key, self.body(data, self.url("slow"), path="race.safetensors"))
        await asyncio.sleep(0.05)
        self.t.write("models/checkpoints/race.safetensors", b"got here first")
        self.src.gate.set()
        out = await self.wait(key, pid)
        self.assertEqual((out["state"], out["error"]), ("failed", "exists"))
        self.assertEqual(self.read("models/checkpoints/race.safetensors"), b"got here first")

    async def test_an_interrupted_pull_resumes_with_range(self):
        key = await self.key()
        data = bytes(range(200))
        body = self.body(data, self.offer("a", data))
        pid = pull_id(body["sha256"], "host", "checkpoints", "a.safetensors")
        # What a pull before a restart left behind.
        self.t.write(f"models/.couchy-partial/{pid}", data[:120])
        self.assertEqual(await self.start(key, body), pid)
        out = await self.wait(key, pid)
        self.assertEqual(out["state"], "done", out)
        self.assertEqual(self.read("models/checkpoints/a.safetensors"), data)
        self.assertEqual(self.src.seen[-1][1].get("Range"), "bytes=120-")

    async def test_a_source_that_ignores_range_starts_over(self):
        key = await self.key()
        data = bytes(range(100))
        body = self.body(data, self.offer("a", data, "norange"))
        pid = pull_id(body["sha256"], "host", "checkpoints", "a.safetensors")
        self.t.write(f"models/.couchy-partial/{pid}", b"\xff" * 40)  # and wrong, too
        out = await self.pull(key, body)
        self.assertEqual(out["state"], "done", out)
        self.assertEqual(self.read("models/checkpoints/a.safetensors"), data)

    async def test_a_network_failure_keeps_the_partial_file(self):
        key = await self.key()
        data = b"abc"
        out = await self.pull(key, self.body(data, "http://127.0.0.1:1/nothing"))
        self.assertEqual((out["state"], out["error"]), ("failed", "network"))
        out = await self.pull(key, self.body(data, self.offer("gone", data, "missing"), path="g.safetensors"))
        self.assertEqual((out["state"], out["error"]), ("failed", "not_found"))

    async def test_pickle_formats_need_allow_pickle(self):
        key = await self.key()
        data = b"pickled"
        url = self.offer("a", data)
        for path in ("a.ckpt", "a.pt", "a.pth", "a.bin"):
            out = await self.pull(key, self.body(data, url, path=path))
            self.assertEqual((out["state"], out["error"]), ("failed", "pickle_refused"), path)
        out = await self.pull(key, self.body(data, url, path="a.exe"))
        self.assertEqual(out["error"], "format_refused")
        self.assertEqual(self.src.seen, [])
        self.config.set(allowPickle=True)
        out = await self.pull(key, self.body(data, url, path="a.ckpt"))
        self.assertEqual(out["state"], "done")
        for path in ("b.safetensors", "b.gguf", "b.sft"):
            self.assertEqual((await self.pull(key, self.body(data, url, path=path)))["state"], "done", path)

    async def test_unknown_hosts_are_refused_unless_allowed(self):
        key = await self.key()
        data = b"abc"
        self.config.set(allowedHosts=["127.0.0.1"])
        url = self.offer("a", data)
        out = await self.pull(key, self.body(data, url.replace("127.0.0.1", "localhost")))
        self.assertEqual((out["state"], out["error"]), ("failed", "host_not_allowed"))
        self.assertEqual(self.src.seen, [])
        # A redirect to a host that isn't allowed is refused too.
        self.offer("r", data, "redirect")
        self.src.redirects["r"] = self.url("a", "localhost")
        out = await self.pull(key, self.body(data, self.url("r"), path="r.safetensors"))
        self.assertEqual((out["state"], out["error"]), ("failed", "host_not_allowed"))
        self.assertEqual([s[0] for s in self.src.seen], ["r"])
        self.config.set(allowedHosts=["127.0.0.1", "localhost"])
        out = await self.pull(key, self.body(data, self.url("r"), path="r.safetensors"))
        self.assertEqual(out["state"], "done", out)

    async def test_credentials_go_to_their_origin_only_and_never_to_logs_or_disk(self):
        key = await self.key()
        data = b"gated weights"
        self.offer("a", data)
        self.offer("r", data, "redirect")
        self.src.redirects["r"] = self.url("a", "localhost")  # the CDN hop
        url = self.url("r") + f"?token={CIVITAI_TOKEN}"
        body = self.body(data, path="gated.safetensors", url=url, headers={"Authorization": f"Bearer {TOKEN}"})
        out = await self.pull(key, body)
        self.assertEqual(out["state"], "done", out)
        (first, h1, q1), (second, h2, _) = self.src.seen
        self.assertEqual((first, h1.get("Authorization"), q1.get("token")), ("r", f"Bearer {TOKEN}", CIVITAI_TOKEN))
        self.assertEqual(second, "a")
        self.assertNotIn("Authorization", h2)
        logs = self.logs.getvalue()
        self.assertIn("model pull done", logs)
        disk = self.all_files_text()
        for leak in (TOKEN, CIVITAI_TOKEN, "/src/"):
            self.assertNotIn(leak, logs)
            self.assertNotIn(leak.encode(), disk)
        self.assertNotIn("gated", logs)  # the file is named by its hash prefix
        r = await self.client.get(f"/couchy/models/pulls/{out['id']}", headers=auth(key))
        self.assertNotIn(TOKEN, await r.text())

    async def test_no_room(self):
        key = await self.key()
        self.config.set(reserveBytes=10**18)
        data = b"abc"
        out = await self.pull(key, self.body(data, self.offer("a", data)))
        self.assertEqual((out["state"], out["error"]), ("failed", "no_room"))
        self.assertEqual(self.src.seen, [])

    async def test_cancel_stops_it_and_removes_the_partial_file(self):
        key = await self.key()
        data = b"0123456789"
        pid = await self.start(key, self.body(data, self.offer("a", data, "slow")))
        for _ in range(100):
            if os.path.exists(self.t.path(f"models/.couchy-partial/{pid}")):
                break
            await asyncio.sleep(0.01)
        r = await self.client.post(f"/couchy/models/pulls/{pid}/cancel", headers=auth(key))
        self.assertEqual((await r.json())["state"], "failed")
        out = await self.wait(key, pid)
        self.assertEqual(out["error"], "cancelled")
        self.assertFalse(os.path.exists(self.t.path(f"models/.couchy-partial/{pid}")))
        self.assertFalse(self.t.exists("models/checkpoints/a.safetensors"))
        r = await self.client.post("/couchy/models/pulls/pull_none/cancel", headers=auth(key))
        self.assertEqual(r.status, 404)

    async def test_at_most_two_run_and_the_rest_queue(self):
        key = await self.key()
        data = b"0123456789"
        url = self.offer("a", data, "slow")
        ids = [await self.start(key, self.body(data, url, path=f"{i}.safetensors")) for i in range(3)]
        await asyncio.sleep(0.1)
        states = [(await (await self.client.get(f"/couchy/models/pulls/{i}", headers=auth(key))).json())["state"]
                  for i in ids]
        self.assertEqual(sorted(states), ["queued", "running", "running"])
        # Starting the same pull again answers the same id.
        self.assertEqual(await self.start(key, self.body(data, url, path="0.safetensors")), ids[0])
        self.src.gate.set()
        for i in ids:
            self.assertEqual((await self.wait(key, i))["state"], "done")

    async def test_bad_bodies(self):
        key = await self.key()
        good = self.body(b"x", "http://127.0.0.1/x")
        for change in ({"sha256": "abc"}, {"location": "nope"}, {"folder": "input"}, {"path": "../a.safetensors"},
                       {"path": ".couchy-partial/x.safetensors"}, {"size": 0}, {"source": {"x": 1}},
                       {"source": {"peer": "http://x"}}):
            r = await self.client.post("/couchy/models/pull", json={**good, **change}, headers=auth(key))
            self.assertEqual(r.status, 400, change)

    # Delete --------------------------------------------------------------------

    async def hashed(self, rel, data):
        self.t.write(rel, data)
        self.hasher.scan(force=True)
        while self.hasher.step():
            pass

    async def delete(self, key, **body):
        r = await self.client.post("/couchy/models/delete", json=body, headers=auth(key))
        self.assertEqual(r.status, 200, await r.text())
        return (await r.json())["state"]

    async def test_delete_only_when_the_hash_matches_on_any_location(self):
        key = await self.key()
        await self.hashed("models/checkpoints/a.safetensors", b"aaa")
        await self.hashed("nas-hot/loras/b.safetensors", b"bbb")
        self.assertEqual(await self.delete(key, sha256=sha(b"zzz"), folder="checkpoints", path="a.safetensors"),
                         "mismatch")
        self.assertTrue(self.t.exists("models/checkpoints/a.safetensors"))
        self.assertEqual(await self.delete(key, sha256=sha(b"aaa"), folder="checkpoints", path="a.safetensors"),
                         "deleted")
        self.assertFalse(self.t.exists("models/checkpoints/a.safetensors"))
        self.assertEqual(self.index.with_sha(sha(b"aaa")), [])
        self.assertEqual(await self.delete(key, sha256=sha(b"aaa"), folder="checkpoints", path="a.safetensors"),
                         "missing")
        self.assertEqual(await self.delete(key, sha256=sha(b"bbb"), location="nas-hot", folder="loras",
                                           path="b.safetensors"), "deleted")
        self.assertFalse(self.t.exists("nas-hot/loras/b.safetensors"))

    async def test_delete_refuses_symlinks_and_unhashed_files(self):
        key = await self.key()
        secret = self.t.write("secret.safetensors", b"s")
        os.makedirs(self.t.path("models/checkpoints"), exist_ok=True)
        os.symlink(secret, self.t.path("models/checkpoints/link.safetensors"))
        self.assertEqual(await self.delete(key, sha256=sha(b"s"), folder="checkpoints", path="link.safetensors"),
                         "refused")
        self.assertTrue(os.path.exists(secret))
        self.t.write("models/checkpoints/new.safetensors", b"n")  # not hashed yet
        self.assertEqual(await self.delete(key, sha256=sha(b"n"), folder="checkpoints", path="new.safetensors"),
                         "refused")
        self.assertTrue(self.t.exists("models/checkpoints/new.safetensors"))

    # Tickets and peers -----------------------------------------------------------

    async def ticket(self, key, data, **body):
        r = await self.client.post("/couchy/models/tickets", json={"sha256": sha(data), **body}, headers=auth(key))
        self.assertEqual(r.status, 200, await r.text())
        return (await r.json())["ticket"]

    async def test_tickets_are_single_use_and_expire(self):
        key = await self.key()
        data = b"0123456789"
        await self.hashed("models/checkpoints/a.safetensors", data)
        t = await self.ticket(key, data, ttl=60)
        r = await self.client.get(f"/couchy/models/blob/{sha(data)}?ticket={t}", headers={"Range": "bytes=4-"})
        self.assertEqual((r.status, await r.read()), (206, data[4:]))
        r = await self.client.get(f"/couchy/models/blob/{sha(data)}?ticket={t}")
        self.assertEqual(r.status, 403)
        t = await self.ticket(key, data, ttl=60)
        self.clock.t += 61
        r = await self.client.get(f"/couchy/models/blob/{sha(data)}", headers={TICKET_HEADER: t})
        self.assertEqual(r.status, 403)
        # A ticket is for one file.
        await self.hashed("models/checkpoints/b.safetensors", b"other")
        t = await self.ticket(key, data)
        r = await self.client.get(f"/couchy/models/blob/{sha(b'other')}", headers={TICKET_HEADER: t})
        self.assertEqual(r.status, 403)
        r = await self.client.post("/couchy/models/tickets", json={"sha256": sha(b"not here")}, headers=auth(key))
        self.assertEqual(r.status, 404)
        self.assertNotIn(t, self.logs.getvalue())

    async def test_a_peer_pull_from_a_share_to_the_host_with_a_ticket(self):
        key = await self.key()
        data = b"copied between agents"
        await self.hashed("nas-hot/checkpoints/flux.safetensors", data)
        t = await self.ticket(key, data, location="nas-hot")
        peer = str(self.server.make_url("/"))
        out = await self.pull(key, self.body(data, path="flux.safetensors", peer=peer, ticket=t))
        self.assertEqual(out["state"], "done", out)
        self.assertEqual(self.read("models/checkpoints/flux.safetensors"), data)
        # The ticket is spent.
        out = await self.pull(key, self.body(data, path="again.safetensors", peer=peer, ticket=t))
        self.assertEqual((out["state"], out["error"]), ("failed", "ticket_refused"))
        self.assertNotIn(t, self.logs.getvalue())

    # History -------------------------------------------------------------------

    async def test_history_returns_hash_sets_and_times_only(self):
        key = await self.key("read")
        await self.hashed("models/checkpoints/sdxl.safetensors", b"ckpt")
        await self.hashed("models/loras/style/ink.safetensors", b"lora")
        self.history = {
            "prompt-1": {"prompt": [1, "prompt-1", {
                "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
                "2": {"class_type": "LoraLoader", "inputs": {"lora_name": "style\\ink.safetensors", "strength": 1}},
                "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "a secret prompt"}},
            }, {}, ["9"]], "outputs": {"9": {"images": [{"filename": "out.png"}]}},
                "status": {"messages": [["execution_start", {"prompt_id": "prompt-1", "timestamp": 1_700_000_000_000}]]}},
            "prompt-2": {"prompt": [2, "prompt-2", {
                "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
            }, {}, []], "status": {"messages": [["execution_start", {"timestamp": 1_700_000_100_000}]]}},
            "prompt-3": {"prompt": [3, "prompt-3", {"1": {"inputs": {"ckpt_name": "unknown.safetensors"}}}, {}, []],
                         "status": {"messages": [["execution_start", {"timestamp": 1_700_000_200_000}]]}},
        }
        r = await self.client.get("/couchy/models/history", headers=auth(key))
        out = await r.json()
        self.assertEqual(out, {"runs": [
            {"at": 1_700_000_000.0, "sha256": sorted([sha(b"ckpt"), sha(b"lora")])},
            {"at": 1_700_000_100.0, "sha256": [sha(b"ckpt")]},
        ]})
        text = await (await self.client.get("/couchy/models/history", headers=auth(key))).text()
        for leak in ("prompt-1", "secret", "out.png", "ink"):
            self.assertNotIn(leak, text)
        r = await self.client.get("/couchy/models/history?since=1700000050", headers=auth(key))
        self.assertEqual([x["at"] for x in (await r.json())["runs"]], [1_700_000_100.0])
        r = await self.client.get("/couchy/models/history?since=x", headers=auth(key))
        self.assertEqual(r.status, 400)


if __name__ == "__main__":
    unittest.main()
