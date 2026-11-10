"""Store mode: pairing and the models routes without ComfyUI, and no
namespaced file routes."""

import hashlib
import os
import unittest

from aiohttp.test_utils import AioHTTPTestCase

from couchy_agent import standalone
from couchy_agent.config import Config
from couchy_agent.store import Store
from tests.helpers import Tree


def auth(key):
    return {"Authorization": f"Couchy-Agent {key}"}


class StoreMode(AioHTTPTestCase):
    async def get_application(self):
        self.t = Tree()
        self.root = self.t.path("nas")
        os.makedirs(os.path.join(self.root, "checkpoints"))
        self.t.write("nas/checkpoints/a.safetensors", b"cold")
        self.state = self.t.path("state")
        os.makedirs(self.state)
        Config(Store(self.state)).set(reserveBytes=0)
        app, self.agent = standalone.build(self.state, self.root)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.agent.hasher.stop(wait=10)  # before its folders go
        self.t.cleanup()

    async def test_pairs_and_serves_the_same_models_routes(self):
        store = Store(self.state)
        self.assertTrue(os.path.exists(os.path.join(store.dir, "pairing-code")))  # minted on start
        code = store.mint_code(models="write")
        r = await self.client.post("/couchy/pair", json={"code": code, "label": "prod"})
        self.assertEqual(r.status, 200)
        key = (await r.json())["key"]
        r = await self.client.get("/couchy/models", headers=auth(key))
        files = (await r.json())["files"]
        self.assertEqual([(f["location"], f["folder"], f["path"]) for f in files], [("host", "checkpoints", "a.safetensors")])
        for route in ("/couchy/models/space", "/couchy/models/history", "/couchy/keys"):
            r = await self.client.get(route, headers=auth(key))
            self.assertEqual(r.status, 200, route)
        self.assertEqual(await (await self.client.get("/couchy/models/history", headers=auth(key))).json(), {"runs": []})
        r = await self.client.get("/couchy/models/pulls/pull_x", headers=auth(key))
        self.assertEqual(r.status, 404)
        # A delete needs the file hashed; the hasher runs in store mode too.
        h = self.agent.hasher
        h.scan(force=True)
        while h.step():
            pass
        sha = hashlib.sha256(b"cold").hexdigest()
        r = await self.client.post("/couchy/models/tickets", json={"sha256": sha}, headers=auth(key))
        self.assertEqual(r.status, 200)
        r = await self.client.post("/couchy/models/delete", json={"sha256": sha, "folder": "checkpoints",
                                                                  "path": "a.safetensors"}, headers=auth(key))
        self.assertEqual((await r.json())["state"], "deleted")

    async def test_no_namespaced_files_or_guard(self):
        code = Store(self.state).mint_code()
        key = (await (await self.client.post("/couchy/pair", json={"code": code, "label": "x"})).json())["key"]
        for route in ("/couchy/usage", "/couchy/files?folder=input&owner=lab", "/view?filename=a.png"):
            r = await self.client.get(route, headers=auth(key))
            self.assertEqual(r.status, 404, route)

    def test_index_dir(self):
        vol = self.t.path("nas/.couchy-index")
        app, agent = standalone.build(self.state, self.root, index_dir=vol)
        agent.hasher.scan(force=True)
        while agent.hasher.step():
            pass
        self.assertTrue(os.path.exists(os.path.join(vol, "models-index.json")))

    def test_parse_listen(self):
        self.assertEqual(standalone.parse_listen("0.0.0.0:8190"), ("0.0.0.0", 8190))
        self.assertEqual(standalone.parse_listen("[::1]:8190"), ("::1", 8190))
        self.assertEqual(standalone.parse_listen(":9000"), ("0.0.0.0", 9000))
        for bad in ("8190", "host:", "host:99999", "host:x"):
            with self.assertRaises(ValueError):
                standalone.parse_listen(bad)


if __name__ == "__main__":
    unittest.main()
