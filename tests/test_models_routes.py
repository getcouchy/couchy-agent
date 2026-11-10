"""The /couchy/models routes and the models grant: per-route enforcement,
the mint ceiling, pairing with --models write, and the routes' answers."""

import os
import unittest

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from couchy_agent import models
from couchy_agent.hashing import Hasher, Index
from couchy_agent.models_routes import ModelsRoutes
from couchy_agent.routes import Agent, Folders
from couchy_agent.store import Lockout, Store
from tests.helpers import Tree
from tests.test_models import safetensors

ROUTES = ("/couchy/models", "/api/couchy/models", "/couchy/models/header?folder=checkpoints&path=a.safetensors",
          "/api/couchy/models/header?folder=checkpoints&path=a.safetensors", "/couchy/models/space",
          "/api/couchy/models/space")


def auth(key):
    return {"Authorization": f"Couchy-Agent {key}"}


class ModelsRoutesTest(AioHTTPTestCase):
    async def get_application(self):
        self.t = Tree()
        self.store = Store(self.t.dirs["user"])
        self.nas = os.path.join(self.t.base, "nas-hot")
        os.makedirs(self.nas)
        m = self.t.dirs["models"]
        self.locations = models.Locations(
            {f: [os.path.join(m, d) for d in dirs] for f, dirs in models.LAYOUT.items()},
            lambda: {"nas-hot": self.nas})
        self.index = Index(self.store)
        self.agent = Agent(self.store, Folders(self.t.dirs), Lockout(), models=ModelsRoutes(self.locations, self.index))
        app = web.Application(middlewares=[self.agent.middleware()])
        self.agent.add_routes(app.router)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.t.cleanup()

    async def pair(self, models_grant="read"):
        code = self.store.mint_code(models=models_grant)
        r = await self.client.post("/couchy/pair", json={"code": code, "label": "Couchy prod · Lab"})
        self.assertEqual(r.status, 200)
        return await r.json()

    async def mint(self, key, **body):
        return await self.client.post("/couchy/keys", json=body, headers=auth(key))

    # The grant -------------------------------------------------------------

    async def test_a_new_pairing_grants_read_and_only_the_host_flag_grants_write(self):
        out = await self.pair()
        self.assertEqual(out["models"], "read")
        self.assertEqual(self.store.authenticate(out["key"])["models"], "read")
        w = await self.pair("write")
        self.assertEqual(w["models"], "write")
        # The pairing route can't ask for more: the code carries the grant.
        code = self.store.mint_code()
        r = await self.client.post("/couchy/pair", json={"code": code, "label": "x", "models": "write"})
        self.assertEqual((await r.json())["models"], "read")

    async def test_grant_enforcement_per_route(self):
        self.t.write("models/checkpoints/a.safetensors", safetensors())
        w = await self.pair("write")
        r = await self.mint(w["key"], mode="ro", models="none")
        none = (await r.json())["key"]
        read = (await (await self.mint(w["key"], mode="ro", models="read")).json())["key"]
        for route in ROUTES:
            r = await self.client.get(route)
            self.assertEqual(r.status, 401, route)
            r = await self.client.get(route, headers=auth(none))
            self.assertEqual(r.status, 403, route)
            for key in (read, w["key"]):
                r = await self.client.get(route, headers=auth(key))
                self.assertEqual(r.status, 200, route)
        # none still reaches the namespaced routes its mode allows.
        r = await self.client.get("/couchy/usage", headers=auth(none))
        self.assertEqual(r.status, 200)

    async def test_the_mint_ceiling(self):
        read = await self.pair("read")
        r = await self.mint(read["key"], mode="ro", models="write")
        self.assertEqual(r.status, 403)
        r = await self.mint(read["key"], mode="ro")
        self.assertEqual((r.status, (await r.json())["models"]), (200, "read"))  # prod mints pre-prod ro + read
        r = await self.mint(read["key"], mode="ro", models="none")
        self.assertEqual((await r.json())["models"], "none")
        r = await self.mint(read["key"], mode="ro", models="admin")
        self.assertEqual(r.status, 400)
        w = await self.pair("write")
        r = await self.mint(w["key"], mode="rw", models="write")
        self.assertEqual((r.status, (await r.json())["models"]), (200, "write"))
        r = await self.mint(w["key"], mode="ro")
        self.assertEqual((await r.json())["models"], "read")  # write is never the default
        # A key minted with none can't mint more (and as rw, defaults to none).
        rw_none = (await (await self.mint(w["key"], mode="rw", models="none")).json())["key"]
        r = await self.mint(rw_none, mode="ro", models="read")
        self.assertEqual(r.status, 403)
        r = await self.mint(rw_none, mode="ro")
        self.assertEqual((await r.json())["models"], "none")
        keys = (await (await self.client.get("/couchy/keys", headers=auth(w["key"]))).json())["keys"]
        self.assertEqual([k["models"] for k in keys], ["write", "write", "read", "none", "none"])

    # The routes ------------------------------------------------------------

    async def test_listing_with_locations_hashes_and_paging(self):
        self.t.write("models/checkpoints/a.safetensors", b"aa")
        self.t.write("models/loras/b.safetensors", b"b")
        self.t.write("nas-hot/checkpoints/c.safetensors", b"ccc")
        key = (await self.pair())["key"]
        h = Hasher(self.locations, self.index)
        h.scan(force=True)
        h.hash_one("host", "checkpoints", "a.safetensors", "new")
        r = await self.client.get("/couchy/models?limit=2", headers=auth(key))
        page = await r.json()
        self.assertEqual([(f["location"], f["folder"], f["path"]) for f in page["files"]],
                         [("host", "checkpoints", "a.safetensors"), ("host", "loras", "b.safetensors")])
        self.assertEqual(len(page["files"][0]["sha256"]), 64)
        self.assertIsNotNone(page["files"][0]["verifiedAt"])
        self.assertIsNone(page["files"][1]["sha256"])
        r = await self.client.get("/couchy/models", params={"limit": "2", "after": page["next"]}, headers=auth(key))
        rest = await r.json()
        self.assertEqual([(f["location"], f["size"]) for f in rest["files"]], [("nas-hot", 3)])
        self.assertNotIn("next", rest)
        r = await self.client.get("/couchy/models?location=nas-hot&folder=checkpoints", headers=auth(key))
        self.assertEqual(len((await r.json())["files"]), 1)
        for bad in ("location=nowhere", "folder=../secrets", "limit=lots"):
            r = await self.client.get("/couchy/models?" + bad, headers=auth(key))
            self.assertEqual(r.status, 400, bad)

    async def test_header_route(self):
        self.t.write("models/checkpoints/a.safetensors", safetensors(metadata={"ss_base_model_version": "sdxl_base_v1-0"}))
        self.t.write("models/checkpoints/old.ckpt", b"PK\x03\x04" + b"\0" * 64)
        self.t.write("nas-hot/loras/n.safetensors", safetensors())
        secret = self.t.write("secret.safetensors", safetensors())
        os.symlink(secret, self.t.path("models/checkpoints/link.safetensors"))
        key = (await self.pair())["key"]

        async def get(**q):
            return await self.client.get("/couchy/models/header", params=q, headers=auth(key))

        r = await get(folder="checkpoints", path="a.safetensors")
        out = await r.json()
        self.assertEqual(out["metadata"], {"ss_base_model_version": "sdxl_base_v1-0"})
        self.assertEqual(out["tensors"], [{"name": "w", "dtype": "F16", "shape": [2, 2]}])
        self.assertEqual((await get(folder="checkpoints", path="old.ckpt")).status, 422)
        self.assertEqual((await get(folder="checkpoints", path="link.safetensors")).status, 404)
        self.assertEqual((await get(folder="checkpoints", path="../../secret.safetensors")).status, 404)
        self.assertEqual((await get(folder="loras", path="n.safetensors")).status, 404)  # it's on the share
        self.assertEqual((await get(folder="loras", path="n.safetensors", location="nas-hot")).status, 200)
        self.assertEqual((await get(path="a.safetensors")).status, 400)

    async def test_space_route(self):
        self.t.write("models/checkpoints/a.safetensors")
        self.t.write("nas-hot/loras/b.safetensors")
        key = (await self.pair())["key"]
        r = await self.client.get("/couchy/models/space", headers=auth(key))
        vols = (await r.json())["volumes"]
        self.assertEqual([(v["location"], v["folders"]) for v in vols], [("host", ["checkpoints"]), ("nas-hot", ["loras"])])
        self.assertTrue(all(v["totalBytes"] >= v["freeBytes"] >= 0 for v in vols))


if __name__ == "__main__":
    unittest.main()
