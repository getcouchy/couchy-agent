"""The routes and the middleware, on a stand-in for ComfyUI's server: an
aiohttp app with fake /view, /upload/image, /prompt and /comfyform/files."""

import json
import os
import time
import unittest

from aiohttp import FormData, web
from aiohttp.test_utils import AioHTTPTestCase

from couchy_agent import __version__
from couchy_agent.routes import Agent, Folders
from couchy_agent.store import Lockout, Store
from tests.helpers import Tree


def auth(key):
    return {"Authorization": f"Couchy-Agent {key}"}


class RoutesTest(AioHTTPTestCase):
    async def get_application(self):
        self.t = Tree()
        self.store = Store(self.t.dirs["user"])
        self.agent = Agent(self.store, Folders(self.t.dirs), Lockout())
        self.reached = []
        self.bodies = []
        self.prompt_status = 200

        async def comfy(request):
            if request.path.endswith("/prompt"):
                self.bodies.append(await request.json())  # the handler can still read the body
                if self.prompt_status != 200:
                    return web.json_response({"error": "invalid prompt"}, status=self.prompt_status)
            if "upload" in request.path:
                post = await request.post()
                self.reached.append(post.get("subfolder", ""))
            self.reached.append(request.path)
            return web.json_response({"ok": True})

        app = web.Application(middlewares=[self.agent.middleware()])
        self.agent.add_routes(app.router)
        for path in ("/view", "/api/view", "/comfyform/files"):
            app.router.add_get(path, comfy)
        for path in ("/upload/image", "/api/upload/image", "/upload/mask", "/prompt", "/api/prompt",
                     "/comfyform/files/delete"):
            app.router.add_post(path, comfy)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.t.cleanup()

    async def pair(self, label="Couchy prod · Lab"):
        code = self.store.mint_code()
        r = await self.client.post("/couchy/pair", json={"code": code, "label": label})
        self.assertEqual(r.status, 200)
        return await r.json()

    async def seed(self):
        a = await self.pair()
        b = await self.pair("Couchy pre-prod · Lab")
        self.ns, self.key, self.other, self.okey = a["namespace"], a["key"], b["namespace"], b["key"]
        self.t.write(f"output/couchy/{self.ns}/u1/r1/a.png", b"aa")
        self.t.write(f"output/couchy/{self.ns}/u2/r2/b.png", b"bbb")
        self.t.write(f"input/couchy/{self.ns}/u1/up.png", b"u")
        self.t.write(f"output/couchy/{self.other}/u1/r9/theirs.png", b"t")
        self.t.write("output/ComfyUI_00001_.png", b"loose")
        r = await self.client.post("/couchy/keys", json={"mode": "ro", "label": "pre-prod"}, headers=auth(self.key))
        self.assertEqual(r.status, 200)
        self.ro = (await r.json())["key"]

    # Pairing ---------------------------------------------------------------

    async def test_pairing_and_lockout(self):
        r = await self.client.post("/couchy/pair", json={"code": "WRONGWRONG12", "label": "x"})
        self.assertEqual(r.status, 403)
        out = await self.pair()
        self.assertTrue(out["namespace"].startswith("ns_"))
        for _ in range(5):
            r = await self.client.post("/couchy/pair", json={"code": "WRONGWRONG12"})
            self.assertEqual(r.status, 403)
        r = await self.client.post("/couchy/pair", json={"code": self.store.mint_code()})
        self.assertEqual(r.status, 429)  # five failures: even a good code waits

    async def test_a_used_code_is_refused(self):
        code = self.store.mint_code()
        r = await self.client.post("/couchy/pair", json={"code": code})
        self.assertEqual(r.status, 200)
        r = await self.client.post("/couchy/pair", json={"code": code})
        self.assertEqual(r.status, 403)

    # Keys ------------------------------------------------------------------

    async def test_every_route_needs_a_key(self):
        await self.seed()
        calls = [("GET", "/couchy/usage", None), ("GET", "/couchy/files?folder=output&owner=u1", None),
                 ("POST", "/couchy/files/delete", {"folder": "output", "owner": "u1"}),
                 ("POST", "/couchy/files/adopt", {}), ("POST", "/couchy/files/move-owner", {"from": "u1", "to": "lab"}),
                 ("GET", "/couchy/keys", None), ("POST", "/couchy/keys", {}), ("POST", "/couchy/keys/revoke", {}),
                 ("GET", "/api/couchy/usage", None)]
        for headers in ({}, auth("cak_wrong"), {"Authorization": f"Bearer {self.key}"}):
            for method, path, body in calls:
                r = await self.client.request(method, path, json=body, headers=headers)
                self.assertEqual(r.status, 401, (method, path, headers))
        self.assertTrue(self.t.exists(f"output/couchy/{self.ns}/u1/r1/a.png"))

    async def test_the_key_may_come_in_its_own_header(self):
        await self.seed()
        r = await self.client.get("/couchy/usage", headers={"Authorization": "Bearer proxy-token",
                                                            "X-Couchy-Agent-Key": self.key})
        self.assertEqual(r.status, 200)

    async def test_usage_is_per_owner_without_names_and_reports_permissions(self):
        await self.seed()
        r = await self.client.get("/couchy/usage", headers=auth(self.ro))
        self.assertEqual(r.status, 200)
        out = await r.json()
        self.assertEqual(out["namespace"], self.ns)
        self.assertEqual(out["folders"]["output"]["owners"], {"u1": {"count": 1, "bytes": 2}, "u2": {"count": 1, "bytes": 3}})
        self.assertEqual(out["permissions"], "ok")
        self.assertNotIn("a.png", json.dumps(out))
        os.chmod(self.t.dirs["output"], 0o755)
        out = await (await self.client.get("/couchy/usage", headers=auth(self.key))).json()
        self.assertEqual(out["permissions"], "open")

    async def test_listing_is_scoped_to_the_key(self):
        await self.seed()
        r = await self.client.get("/couchy/files?folder=output&owner=u1", headers=auth(self.ro))
        self.assertEqual([f["filename"] for f in (await r.json())["files"]], ["a.png"])
        r = await self.client.get("/couchy/files?folder=output&owner=u1", headers=auth(self.okey))
        self.assertEqual([f["filename"] for f in (await r.json())["files"]], ["theirs.png"])
        for q in ("folder=models&owner=u1", "folder=output&owner=..", "folder=output&owner=u1/../../x",
                  "folder=output&owner=u1&limit=x"):
            r = await self.client.get("/couchy/files?" + q, headers=auth(self.key))
            self.assertEqual(r.status, 400, q)

    async def test_a_read_only_key_cant_change_anything(self):
        await self.seed()
        for path, body in [
            ("/couchy/files/delete", {"folder": "output", "owner": "u1"}),
            ("/couchy/files/delete", {"folder": "output", "olderThan": 0}),
            ("/couchy/files/adopt", {"from": {"folder": "output", "subfolder": "", "filename": "ComfyUI_00001_.png"},
                                     "owner": "u1", "runId": "r1"}),
            ("/couchy/files/move-owner", {"from": "u1", "to": "lab"}),
            ("/couchy/keys", {"mode": "rw"}),
        ]:
            r = await self.client.post(path, json=body, headers=auth(self.ro))
            self.assertEqual(r.status, 403, path)
        self.assertTrue(self.t.exists(f"output/couchy/{self.ns}/u1/r1/a.png"))
        self.assertTrue(self.t.exists("output/ComfyUI_00001_.png"))

    async def test_delete_refuses_another_namespace(self):
        await self.seed()
        r = await self.client.post("/couchy/files/delete", headers=auth(self.key), json={"folder": "output", "files": [
            {"subfolder": f"couchy/{self.ns}/u1/r1", "filename": "a.png"},
            {"subfolder": f"couchy/{self.other}/u1/r9", "filename": "theirs.png"},
            {"subfolder": "", "filename": "ComfyUI_00001_.png"},
            {"subfolder": "../models", "filename": "model.safetensors"},
        ]})
        out = await r.json()
        self.assertEqual([f["filename"] for f in out["deleted"]], ["a.png"])
        self.assertEqual(len(out["refused"]), 3)
        self.assertTrue(self.t.exists(f"output/couchy/{self.other}/u1/r9/theirs.png"))
        old = time.time() - 3600
        os.utime(self.t.path(f"output/couchy/{self.other}/u1/r9/theirs.png"), (old, old))
        r = await self.client.post("/couchy/files/delete", json={"folder": "output", "olderThan": 0}, headers=auth(self.key))
        self.assertEqual(await r.json(), {"deleted": 1, "bytes": 3})
        self.assertTrue(self.t.exists(f"output/couchy/{self.other}/u1/r9/theirs.png"))
        self.assertTrue(self.t.exists("output/ComfyUI_00001_.png"))
        r = await self.client.post("/couchy/files/delete", json={"folder": "temp", "olderThan": 0}, headers=auth(self.key))
        self.assertEqual(r.status, 400)

    async def test_adopt_twice_and_another_namespace(self):
        await self.seed()
        body = {"from": {"folder": "output", "subfolder": "", "filename": "ComfyUI_00001_.png"}, "owner": "u1", "runId": "r5"}
        r = await self.client.post("/couchy/files/adopt", json=body, headers=auth(self.key))
        self.assertEqual((await r.json())["state"], "adopted")
        self.assertTrue(self.t.exists(f"output/couchy/{self.ns}/u1/r5/ComfyUI_00001_.png"))
        r = await self.client.post("/couchy/files/adopt", json=body, headers=auth(self.key))
        self.assertEqual(await r.json(), {"state": "missing"})
        body["from"] = {"folder": "output", "subfolder": f"couchy/{self.other}/u1/r9", "filename": "theirs.png"}
        r = await self.client.post("/couchy/files/adopt", json=body, headers=auth(self.key))
        self.assertEqual(await r.json(), {"state": "refused"})

    async def test_move_owner(self):
        await self.seed()
        r = await self.client.post("/couchy/files/move-owner", json={"from": "u1", "to": "lab"}, headers=auth(self.key))
        self.assertEqual(await r.json(), {"input": {"moved": 1}, "output": {"moved": 1}})
        self.assertTrue(self.t.exists(f"output/couchy/{self.ns}/lab/r1/a.png"))
        self.assertTrue(self.t.exists(f"output/couchy/{self.other}/u1/r9/theirs.png"))

    async def test_keys_listing_and_revoking(self):
        await self.seed()
        out = await (await self.client.get("/couchy/keys", headers=auth(self.key))).json()
        self.assertEqual([k["mode"] for k in out["keys"]], ["rw", "ro"])
        self.assertNotIn(self.ro, json.dumps(out))
        ro_id = out["keys"][1]["id"]
        rw_id = out["keys"][0]["id"]
        r = await self.client.post("/couchy/keys/revoke", json={"id": rw_id}, headers=auth(self.key))
        self.assertEqual(r.status, 400)  # not itself
        r = await self.client.post("/couchy/keys/revoke", json={"id": ro_id}, headers=auth(self.okey))
        self.assertEqual(await r.json(), {"revoked": False})  # not another namespace's
        r = await self.client.post("/couchy/keys/revoke", json={"id": ro_id}, headers=auth(self.key))
        self.assertEqual(await r.json(), {"revoked": True})
        r = await self.client.get("/couchy/usage", headers=auth(self.ro))
        self.assertEqual(r.status, 401)

    # The guard on ComfyUI's routes -------------------------------------------

    async def test_view_guard(self):
        await self.seed()
        mine = {"filename": "a.png", "subfolder": f"couchy/{self.ns}/u1/r1", "type": "output"}
        for path in ("/view", "/api/view"):
            r = await self.client.get(path, params=mine)
            self.assertEqual(r.status, 403, path)
            r = await self.client.get(path, params=mine, headers=auth(self.ro))
            self.assertEqual(r.status, 200, path)
            r = await self.client.get(path, params=mine, headers=auth(self.okey))
            self.assertEqual(r.status, 403, path)
        sneaky = dict(mine, subfolder=f"x/../COUCHY/{self.ns.upper()}/u1/r1")
        r = await self.client.get("/view", params=sneaky, headers=auth(self.okey))
        self.assertEqual(r.status, 403)
        r = await self.client.get("/view", params={"filename": "ComfyUI_00001_.png", "type": "output"})
        self.assertEqual(r.status, 200)
        r = await self.client.get("/view", params={"filename": "x.png", "subfolder": "my-renders"})
        self.assertEqual(r.status, 200)

    async def test_upload_guard(self):
        await self.seed()

        def form(subfolder, filename="up.png"):
            f = FormData()
            f.add_field("image", b"png", filename=filename, content_type="image/png")
            f.add_field("subfolder", subfolder)
            f.add_field("type", "input")
            return f

        target = f"couchy/{self.ns}/u1"
        r = await self.client.post("/upload/image", data=form(target), headers=auth(self.ro))
        self.assertEqual(r.status, 403)
        r = await self.client.post("/upload/image", data=form(target), headers=auth(self.okey))
        self.assertEqual(r.status, 403)
        r = await self.client.post("/api/upload/image", data=form(target))
        self.assertEqual(r.status, 403)
        # The path hidden in the file name, sent raw as a hand-made client could.
        boundary = "b0undary"
        raw = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="couchy/{self.ns}/u1/up.png"\r\n'
               f'Content-Type: image/png\r\n\r\npng\r\n--{boundary}--\r\n').encode()
        r = await self.client.post("/upload/image", data=raw,
                                   headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        self.assertEqual(r.status, 403)
        self.reached.clear()
        r = await self.client.post("/upload/image", data=form(target), headers=auth(self.key))
        self.assertEqual(r.status, 200)
        self.assertEqual(self.reached, [target, "/upload/image"])  # the handler still read the form
        r = await self.client.post("/upload/image", data=form("pasted"))
        self.assertEqual(r.status, 200)

    async def test_mask_upload_reading_a_namespaced_original(self):
        await self.seed()
        f = FormData()
        f.add_field("image", b"png", filename="mask.png", content_type="image/png")
        f.add_field("original_ref", json.dumps({"filename": "a.png", "subfolder": f"couchy/{self.ns}/u1/r1", "type": "output"}))
        r = await self.client.post("/upload/mask", data=f, headers=auth(self.okey))
        self.assertEqual(r.status, 403)

    async def test_prompt_guard(self):
        await self.seed()
        graph = {"prompt": {"1": {"class_type": "LoadImage", "inputs": {"image": f"couchy/{self.ns}/u1/up.png"}}}}
        for path in ("/prompt", "/api/prompt"):
            r = await self.client.post(path, json=graph, headers=auth(self.okey))
            self.assertEqual(r.status, 403, path)
            self.assertIn(self.ns, (await r.json())["error"])
            r = await self.client.post(path, json=graph)
            self.assertEqual(r.status, 403, path)
        r = await self.client.post("/prompt", json=graph, headers=auth(self.ro))
        self.assertEqual(r.status, 200)
        r = await self.client.post("/prompt", json=graph, headers=auth(self.key))
        self.assertEqual(r.status, 200)
        plain = {"prompt": {"1": {"class_type": "LoadImage", "inputs": {"image": "example.png"}}}}
        r = await self.client.post("/prompt", json=plain)
        self.assertEqual(r.status, 200)

    # Run scopes on /prompt ---------------------------------------------------

    def scoped(self, **couchy):
        couchy = {"runId": "r1", "owner": "u1", "inputs": ["up"], **couchy}
        return {"prompt": {"1": {"class_type": "CouchyLoadImage", "inputs": {"upload": "up"}}},
                "extra_data": {"extra_pnginfo": {"workflow": {}}, "couchy": couchy}}

    async def test_scope_is_bound_and_stripped(self):
        await self.seed()
        r = await self.client.post("/prompt", json=self.scoped(), headers=auth(self.key))
        self.assertEqual(r.status, 200)
        body = self.bodies[-1]
        self.assertEqual(body["extra_data"], {"extra_pnginfo": {"workflow": {}}})  # ComfyUI never sees it
        pid = body["prompt_id"]  # set, since the API sent none
        sc = self.agent.scopes.get(pid)
        self.assertEqual((sc.namespace, sc.owner, sc.run_id, sc.inputs), (self.ns, "u1", "r1", {"up"}))
        self.assertEqual(sc.class_of("1"), "CouchyLoadImage")

        given = "8a3c1f9e-0d2b-4c6e-9f1a-2b3c4d5e6f70"
        r = await self.client.post("/api/prompt", json={**self.scoped(), "prompt_id": given}, headers=auth(self.key))
        self.assertEqual(r.status, 200)
        self.assertEqual(self.bodies[-1]["prompt_id"], given)
        self.assertEqual(self.agent.scopes.get(given).run_id, "r1")

    async def test_a_scope_needs_the_rw_key(self):
        await self.seed()
        for headers in ({}, auth(self.ro)):
            r = await self.client.post("/prompt", json=self.scoped(), headers=headers)
            self.assertEqual(r.status, 403)
        self.assertEqual((len(self.agent.scopes), self.bodies), (0, []))
        # okey is rw on the other namespace: the scope is that namespace's.
        r = await self.client.post("/prompt", json=self.scoped(), headers=auth(self.okey))
        self.assertEqual(r.status, 200)
        self.assertEqual(len(self.agent.scopes), 1)
        self.assertEqual(next(iter(self.agent.scopes._by_prompt.values())).namespace, self.other)
        self.assertEqual(len(self.bodies), 1)

    async def test_bad_scopes_are_refused(self):
        await self.seed()
        for bad in ({"owner": "../u2"}, {"runId": None}, {"inputs": "up"}, {"inputs": ["a/b"]}):
            r = await self.client.post("/prompt", json=self.scoped(**bad), headers=auth(self.key))
            self.assertEqual(r.status, 400, bad)
        r = await self.client.post("/prompt", json={**self.scoped(), "prompt_id": 7}, headers=auth(self.key))
        self.assertEqual(r.status, 400)
        self.assertEqual((len(self.agent.scopes), self.bodies), (0, []))

    async def test_a_scoped_prompt_id_cant_be_reused(self):
        await self.seed()
        pid = "8a3c1f9e-0d2b-4c6e-9f1a-2b3c4d5e6f70"
        r = await self.client.post("/prompt", json={**self.scoped(), "prompt_id": pid}, headers=auth(self.key))
        self.assertEqual(r.status, 200)
        # Someone in ComfyUI's own UI, sending the queued prompt's id.
        plain = {"prompt": {"1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}}}, "prompt_id": pid}
        r = await self.client.post("/prompt", json=plain)
        self.assertEqual(r.status, 409)
        r = await self.client.post("/prompt", json={**self.scoped(owner="u2"), "prompt_id": pid}, headers=auth(self.key))
        self.assertEqual(r.status, 409)
        self.assertEqual(self.agent.scopes.get(pid).owner, "u1")

    async def test_a_refused_prompt_drops_its_scope(self):
        await self.seed()
        self.prompt_status = 400
        r = await self.client.post("/prompt", json=self.scoped(), headers=auth(self.key))
        self.assertEqual(r.status, 400)
        self.assertEqual(len(self.agent.scopes), 0)

    async def test_unscoped_prompts(self):
        await self.seed()
        plain = {"prompt": {"1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}}}}
        r = await self.client.post("/prompt", json=plain)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.bodies[-1], plain)  # untouched, and no scope
        r = await self.client.post("/prompt", json=plain, headers=auth(self.ro))
        self.assertEqual(len(self.agent.scopes), 0)
        # The API before it sends scopes: its rw key's namespace.
        r = await self.client.post("/prompt", json=plain, headers=auth(self.key))
        self.assertEqual(r.status, 200)
        sc = self.agent.scopes.get(self.bodies[-1]["prompt_id"])
        self.assertEqual((sc.namespace, sc.is_run), (self.ns, False))

    async def test_usage_reports_the_hook_and_nodes(self):
        await self.seed()
        out = await (await self.client.get("/couchy/usage", headers=auth(self.ro))).json()
        self.assertEqual((out["scope_hook"], out["nodes"]), ("off", None))
        self.agent.scope_hook, self.agent.node_version = "on", 1
        out = await (await self.client.get("/couchy/usage", headers=auth(self.ro))).json()
        self.assertEqual((out["scope_hook"], out["nodes"]), ("on", 1))

    async def test_version_answers_any_paired_key(self):
        await self.seed()
        r = await self.client.post("/couchy/keys", json={"mode": "ro", "label": "no models", "models": "none"},
                                   headers=auth(self.key))
        self.assertEqual(r.status, 200)
        none = (await r.json())["key"]
        for key in (self.key, self.ro, none):
            for path in ("/couchy/version", "/api/couchy/version"):
                r = await self.client.get(path, headers=auth(key))
                self.assertEqual(r.status, 200, path)
                self.assertEqual(await r.json(), {"version": __version__, "nodes": None,
                                                  "lockdown": "off", "scope_hook": "off"})
        self.agent.scope_hook, self.agent.node_version, self.agent.lockdown = "on", 1, "on"
        out = await (await self.client.get("/couchy/version", headers=auth(self.ro))).json()
        self.assertEqual(out, {"version": __version__, "nodes": 1, "lockdown": "on", "scope_hook": "on"})

    async def test_version_needs_a_key(self):
        await self.seed()
        for headers in ({}, auth("cak_wrong")):
            r = await self.client.get("/couchy/version", headers=headers)
            self.assertEqual(r.status, 401)
            self.assertNotIn(__version__, await r.text())

    async def test_pairing_calls_on_paired(self):
        called = []
        self.agent.on_paired = lambda: called.append(1)
        await self.pair()
        self.assertEqual(called, [1])

    async def test_old_file_routes_go_once_paired(self):
        r = await self.client.get("/comfyform/files?type=output")
        self.assertEqual(r.status, 200)
        await self.pair()
        r = await self.client.get("/comfyform/files?type=output")
        self.assertEqual(r.status, 410)
        r = await self.client.post("/comfyform/files/delete", json={"type": "output", "olderThan": 0})
        self.assertEqual(r.status, 410)
        self.assertTrue(self.t.exists("models/model.safetensors"))


if __name__ == "__main__":
    unittest.main()
