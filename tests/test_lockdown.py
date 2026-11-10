"""Lockdown mode, the pod's self-pairing token and allowed hosts from the
environment, on a stand-in for ComfyUI's server with its open routes."""

import logging
import os
import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from couchy_agent import lockdown
from couchy_agent.config import Config
from couchy_agent.routes import Agent, Folders
from couchy_agent.store import Lockout, Store
from tests.helpers import Tree

TOKEN = "pod-token-6f1c0e9a2b7d4c3e8f5a1b2c3d4e5f60"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class LockdownTest(AioHTTPTestCase):
    async def get_application(self):
        self.t = Tree()
        self.clock = Clock()
        self.store = Store(self.t.dirs["user"], clock=self.clock)
        self.agent = Agent(self.store, Folders(self.t.dirs), Lockout())
        self.agent.lockdown = "on"

        async def comfy(request):
            return web.json_response({"ok": True})

        async def ws(request):
            sock = web.WebSocketResponse()
            await sock.prepare(request)
            await sock.send_json({"type": "status"})
            await sock.close()
            return sock

        app = web.Application(middlewares=[lockdown.middleware(self.agent), self.agent.middleware()])
        self.agent.add_routes(app.router)
        for path in ("/", "/system_stats", "/object_info", "/api/object_info", "/history", "/view", "/queue",
                     "/manager/version", "/comfyform/packs"):
            app.router.add_get(path, comfy)
        app.router.add_post("/prompt", comfy)
        app.router.add_get("/ws", ws)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.t.cleanup()

    async def pair(self, code):
        return await self.client.post("/couchy/pair", json={"code": code, "label": "Couchy prod · cloud"})

    # Lockdown --------------------------------------------------------------

    async def test_every_comfyui_route_needs_a_key(self):
        for path in ("/", "/system_stats", "/object_info", "/api/object_info", "/history", "/view", "/queue",
                     "/manager/version", "/comfyform/packs", "/couchy/usage", "/couchy/keys"):
            r = await self.client.get(path)
            self.assertEqual(r.status, 401, path)
        r = await self.client.post("/prompt", json={"prompt": {}})
        self.assertEqual(r.status, 401)
        # Couchy's open-network check: no credentials, no answer.
        r = await self.client.get("/system_stats")
        self.assertEqual(r.status, 401)
        self.assertNotIn("ok", await r.text())
        r = await self.client.get("/system_stats", headers={"Authorization": "Couchy-Agent cak_wrong"})
        self.assertEqual(r.status, 401)

    async def test_the_websocket_needs_a_key(self):
        with self.assertRaises(aiohttp.WSServerHandshakeError) as e:
            await self.client.ws_connect("/ws?clientId=x")
        self.assertEqual(e.exception.status, 401)
        code = self.store.mint_code()
        key = (await (await self.pair(code)).json())["key"]
        sock = await self.client.ws_connect("/ws?clientId=x", headers={"X-Couchy-Agent-Key": key})
        self.assertEqual((await sock.receive_json())["type"], "status")
        await sock.close()

    async def test_a_key_opens_every_route(self):
        code = self.store.mint_code()
        key = (await (await self.pair(code)).json())["key"]
        for headers in ({"Authorization": f"Couchy-Agent {key}"},
                        {"Authorization": "Bearer proxy-token", "X-Couchy-Agent-Key": key}):
            for path in ("/", "/system_stats", "/object_info", "/couchy/usage"):
                r = await self.client.get(path, headers=headers)
                self.assertEqual(r.status, 200, path)
        r = await self.client.get("/couchy/usage", headers={"X-Couchy-Agent-Key": key})
        self.assertEqual((await r.json())["lockdown"], "on")

    async def test_pairing_and_the_blob_stay_open(self):
        r = await self.pair("WRONGWRONG12")
        self.assertEqual(r.status, 403)  # the pairing route answered, not lockdown
        r = await self.client.get("/api/couchy/models/blob/" + "0" * 64)
        self.assertEqual(r.status, 404)  # no models routes here; lockdown let it through

    # Self-pairing ------------------------------------------------------------

    async def test_the_token_pairs_once_with_models_write(self):
        self.assertTrue(self.store.arm_token(TOKEN))
        r = await self.pair(TOKEN)
        self.assertEqual(r.status, 200)
        out = await r.json()
        self.assertEqual((out["mode"], out["models"]), ("rw", "write"))
        self.assertEqual(self.store.authenticate(out["key"])["models"], "write")
        r = await self.pair(TOKEN)
        self.assertEqual(r.status, 403)
        self.assertEqual(await r.json(), {"error": "that code is wrong, used or expired"})
        # A restart with the same token in the environment doesn't re-arm it.
        self.store.arm_token(TOKEN)
        self.assertEqual((await self.pair(TOKEN)).status, 403)

    async def test_the_token_expires_30_minutes_after_start(self):
        self.store.arm_token(TOKEN)
        self.clock.t += 29 * 60
        self.store.arm_token(TOKEN)  # a restart keeps the first expiry
        self.clock.t += 61
        r = await self.pair(TOKEN)
        self.assertEqual(r.status, 403)
        self.assertEqual(await r.json(), {"error": "that code is wrong, used or expired"})

    async def test_a_printed_code_still_pairs_with_read_and_the_token_is_never_logged_or_kept(self):
        self.store.arm_token(TOKEN)
        with self.assertLogs("couchy_agent", level=logging.DEBUG) as logs:
            out = await (await self.pair(self.store.mint_code())).json()
            self.assertEqual(out["models"], "read")
            await self.pair(TOKEN)
            await self.pair("not-the-token")
        self.assertNotIn(TOKEN, "\n".join(logs.output))
        for dirpath, _, files in os.walk(self.store.dir):
            for fn in files:
                with open(os.path.join(dirpath, fn), "rb") as f:
                    self.assertNotIn(TOKEN.encode(), f.read(), fn)

    async def test_no_token_means_no_token_pairing(self):
        self.assertFalse(self.store.arm_token(""))
        self.assertEqual((await self.pair("")).status, 403)


class EnvTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.store = Store(self.t.dirs["user"])

    def tearDown(self):
        self.t.cleanup()

    def test_enabled(self):
        for v in ("1", "true", "YES", "on"):
            self.assertTrue(lockdown.enabled({"COUCHY_AGENT_LOCKDOWN": v}))
        for v in ("", "0", "false", "off"):
            self.assertFalse(lockdown.enabled({"COUCHY_AGENT_LOCKDOWN": v}))
        self.assertFalse(lockdown.enabled({}))

    def test_env_allowed_hosts_merge_with_the_configs(self):
        Config(self.store).set(allowedHosts=["models.example.com"])
        env = {"COUCHY_AGENT_ALLOWED_HOSTS": " Acct.R2.CloudflareStorage.com, models.example.com,,bad host,s3.us-east-1.amazonaws.com"}
        c = Config(self.store, env=env)
        self.assertEqual(c.allowed_hosts(), ["models.example.com", "acct.r2.cloudflarestorage.com",
                                             "s3.us-east-1.amazonaws.com"])
        self.assertEqual(c.load()["allowedHosts"], ["models.example.com"])  # never written to the file
        self.assertEqual(Config(self.store, env={}).allowed_hosts(), ["models.example.com"])

    def test_env_index_dir(self):
        Config(self.store).set(indexDir="/from/config")
        self.assertEqual(Config(self.store, env={"COUCHY_AGENT_INDEX_DIR": "/workspace/models/.couchy-index"}).index_dir(),
                         "/workspace/models/.couchy-index")
        self.assertEqual(Config(self.store, env={"COUCHY_AGENT_INDEX_DIR": "relative"}).index_dir(), "/from/config")


if __name__ == "__main__":
    unittest.main()
