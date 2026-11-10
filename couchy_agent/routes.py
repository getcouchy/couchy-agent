"""The agent's HTTP side: /couchy/* routes and the middleware that guards
ComfyUI's own routes. Needs aiohttp (ComfyUI ships it) but not ComfyUI: the
caller passes a Folders, which comfy.py fills from folder_paths.
"""

import asyncio
import json
import logging
import os

from aiohttp import web

from . import files, guard, perms
from .store import DEFAULT_MODELS, MODELS_GRANTS, Lockout, models_rank

log = logging.getLogger("couchy_agent")

AUTH_SCHEME = "Couchy-Agent"
# For deployments where a token proxy in front of ComfyUI already uses
# Authorization: the key may come in this header instead.
KEY_HEADER = "X-Couchy-Agent-Key"

# couchyforms-helper's file routes, switched off once this server is paired.
OLD_FILE_ROUTES = ("/comfyform/files", "/comfyform/files/delete")

UPLOAD_ROUTES = ("/upload/image", "/upload/mask")


class Folders:
    """Where ComfyUI keeps things. dirs: name -> path for input, output,
    temp, user and models. annotated(name) -> (name, dir or None) as
    folder_paths.annotated_filepath does."""

    def __init__(self, dirs, annotated=None):
        self._dirs = dirs
        self.annotated = annotated or (lambda name: (name, None))

    def all(self):
        """The folders now; read each time, since ComfyUI can change them."""
        return self._dirs() if callable(self._dirs) else self._dirs

    def by_type(self, kind, default):
        if kind not in ("input", "output", "temp"):
            kind = default
        return self.all()[kind]


def _error(status, text):
    return web.json_response({"error": text}, status=status)


def _strip_api(path):
    return path[4:] if path.startswith("/api/") else path


def request_key(request):
    auth = request.headers.get("Authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme == AUTH_SCHEME and value.strip():
        return value.strip()
    return request.headers.get(KEY_HEADER, "").strip() or None


def _client(request):
    return request.remote or "unknown"


class Agent:
    def __init__(self, store, folders, lockout=None, models=None):
        self.store = store
        self.folders = folders
        self.lockout = lockout or Lockout()
        self.models = models  # models_routes.ModelsRoutes, or None

    def grant(self, request):
        return self.store.authenticate(request_key(request))

    # Routes ----------------------------------------------------------------

    def add_routes(self, router):
        for prefix in ("", "/api"):
            router.add_post(prefix + "/couchy/pair", self.pair)
            router.add_get(prefix + "/couchy/usage", self.usage)
            router.add_get(prefix + "/couchy/files", self.list_files)
            router.add_post(prefix + "/couchy/files/delete", self.delete)
            router.add_post(prefix + "/couchy/files/adopt", self.adopt)
            router.add_post(prefix + "/couchy/files/move-owner", self.move_owner)
            router.add_get(prefix + "/couchy/keys", self.list_keys)
            router.add_post(prefix + "/couchy/keys", self.mint_key)
            router.add_post(prefix + "/couchy/keys/revoke", self.revoke_key)
        if self.models is not None:
            self.models.add_routes(router, self._need)

    def _need(self, request, write=False):
        g = self.grant(request)
        if g is None:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "send Authorization: Couchy-Agent <key>"}),
                content_type="application/json",
                headers={"WWW-Authenticate": AUTH_SCHEME},
            )
        if write and g["mode"] != "rw":
            log.warning("couchy-agent: refused a write with read-only key %s (%s %s)", g["id"], request.method, request.path)
            raise web.HTTPForbidden(text=json.dumps({"error": "this key is read-only"}), content_type="application/json")
        return g

    async def _body(self, request):
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="the body must be JSON")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="the body must be an object")
        return body

    def _folder(self, kind):
        if kind not in files.NS_FOLDERS:
            raise web.HTTPBadRequest(text=f"folder must be one of {', '.join(files.NS_FOLDERS)}")
        return self.folders.all()[kind]

    def _owner(self, owner, name="owner"):
        if not files.valid_id(owner):
            raise web.HTTPBadRequest(text=f"{name} must be a Couchy user id or lab")
        return owner

    async def pair(self, request):
        client = _client(request)
        wait = self.lockout.check(client)
        if wait is not None:
            return _error(429, f"too many tries; wait {wait} seconds")
        body = await self._body(request)
        result = await asyncio.to_thread(self.store.redeem, body.get("code"), body.get("label", ""))
        if result is None:
            self.lockout.fail(client)
            return _error(403, "that code is wrong, used or expired")
        self.lockout.succeed(client)
        log.info("couchy-agent: paired namespace %s (%s)", result["namespace"], body.get("label", ""))
        return web.json_response(result)

    async def usage(self, request):
        g = self._need(request)
        dirs = self.folders.all()
        out = await asyncio.to_thread(files.usage, dirs, g["namespace"])
        out = {"namespace": g["namespace"], "folders": out, "permissions": perms.permissions(dirs)}
        return web.json_response(out)

    async def list_files(self, request):
        g = self._need(request)
        q = request.query
        root = self._folder(q.get("folder", ""))
        owner = self._owner(q.get("owner", ""))
        try:
            limit = int(q.get("limit", str(files.MAX_LIMIT)))
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be a number")
        out = await asyncio.to_thread(files.list_files, root, g["namespace"], owner, q.get("after", ""), limit)
        return web.json_response(out)

    async def delete(self, request):
        g = self._need(request, write=True)
        body = await self._body(request)
        root = self._folder(body.get("folder", ""))
        ns = g["namespace"]
        if "olderThan" in body:
            older = body["olderThan"]
            if not isinstance(older, (int, float)) or isinstance(older, bool) or older < 0:
                raise web.HTTPBadRequest(text="olderThan is seconds, 0 or more")
            return web.json_response(await asyncio.to_thread(files.delete_older, root, ns, older))
        if "owner" in body:
            owner = self._owner(body["owner"])
            return web.json_response(await asyncio.to_thread(files.delete_owner, root, ns, owner))
        listed = body.get("files")
        if not isinstance(listed, list) or not all(isinstance(f, dict) for f in listed):
            raise web.HTTPBadRequest(text="send files, a list of {subfolder, filename}; owner; or olderThan")
        return web.json_response(await asyncio.to_thread(files.delete_files, root, ns, listed))

    async def adopt(self, request):
        g = self._need(request, write=True)
        body = await self._body(request)
        out = await asyncio.to_thread(
            files.adopt, self.folders.all(), g["namespace"], body.get("from"), body.get("owner"), body.get("runId"))
        return web.json_response(out)

    async def move_owner(self, request):
        g = self._need(request, write=True)
        body = await self._body(request)
        src = self._owner(body.get("from"), "from")
        dst = self._owner(body.get("to"), "to")
        if src == dst:
            raise web.HTTPBadRequest(text="from and to are the same owner")
        out = await asyncio.to_thread(files.move_owner, self.folders.all(), g["namespace"], src, dst)
        return web.json_response(out)

    async def list_keys(self, request):
        g = self._need(request)
        return web.json_response({"keys": self.store.list_keys(g["namespace"])})

    async def mint_key(self, request):
        g = self._need(request, write=True)
        body = await self._body(request)
        mode = body.get("mode", "ro")
        if mode not in ("rw", "ro"):
            raise web.HTTPBadRequest(text="mode must be rw or ro")
        # The models grant is no higher than the minter's: by default read,
        # or the minter's own when that is lower.
        mine = g.get("models", DEFAULT_MODELS)
        models = body.get("models", min(DEFAULT_MODELS, mine, key=models_rank))
        if models not in MODELS_GRANTS:
            raise web.HTTPBadRequest(text="models must be none, read or write")
        if models_rank(models) > models_rank(mine):
            log.warning("couchy-agent: refused minting a models %s key with key %s (models %s)", models, g["id"], mine)
            raise web.HTTPForbidden(text=json.dumps({"error": f"this key's models grant is {mine}; a key it mints can't have more"}),
                                    content_type="application/json")
        out = await asyncio.to_thread(self.store.mint_key, g["namespace"], mode, body.get("label", ""), models)
        log.info("couchy-agent: minted %s key %s (models %s) for %s", mode, out["id"], models, g["namespace"])
        return web.json_response(out)

    async def revoke_key(self, request):
        g = self._need(request, write=True)
        body = await self._body(request)
        kid = body.get("id")
        if kid == g["id"]:
            raise web.HTTPBadRequest(text="a key can't revoke itself")
        ok = await asyncio.to_thread(self.store.revoke_key, g["namespace"], kid)
        return web.json_response({"revoked": ok})

    # The guard on ComfyUI's routes ------------------------------------------

    def middleware(self):
        @web.middleware
        async def couchy_agent_guard(request, handler):
            refusal = await self.check(request)
            if refusal is not None:
                return refusal
            return await handler(request)

        return couchy_agent_guard

    def _refuse(self, request, status, text):
        g = self.grant(request)
        log.warning("couchy-agent: refused %s %s (%s, key %s)", request.method, request.path, text,
                    g["id"] if g else "none")
        return _error(status, text)

    async def check(self, request):
        """None to let the request through, else the response refusing it."""
        path = _strip_api(request.path)
        method = request.method
        if method == "GET" and path == "/view":
            return self._check_view(request)
        if method == "POST" and path in UPLOAD_ROUTES:
            return await self._check_upload(request, path)
        if method == "POST" and path == "/prompt":
            return await self._check_prompt(request)
        if path in OLD_FILE_ROUTES and self.store.paired():
            return _error(410, "this server is paired with Couchy: its files are reached through /couchy/ now")
        return None

    def _read_target(self, filename, subfolder, kind):
        """The namespace a /view-style reference lands in, or None."""
        name, base = self.folders.annotated(filename or "")
        root = base or self.folders.by_type(kind, "output")
        return guard.target_namespace(root, subfolder or "")

    def _check_view(self, request):
        q = request.rel_url.query
        if files.TOP not in q.get("subfolder", "").lower():
            return None  # the fast path: nothing outside couchy/ is slowed
        ns = self._read_target(q.get("filename", ""), q.get("subfolder", ""), q.get("type", "output"))
        if ns is None:
            return None
        if not guard.allows_read(self.grant(request), ns):
            return self._refuse(request, 403, "that file is in a Couchy namespace this key can't read")
        return None

    async def _check_upload(self, request, path):
        try:
            post = await request.post()  # aiohttp keeps it for the handler
        except Exception:
            return None  # let ComfyUI answer a bad body itself
        image = post.get("image")
        filename = getattr(image, "filename", "") or ""
        subfolder = post.get("subfolder", "")
        if not isinstance(subfolder, str):
            subfolder = ""
        target = os.path.dirname(os.path.join(os.path.normpath(subfolder) if subfolder else "", filename))
        if files.TOP in (subfolder + filename).lower():
            root = self.folders.by_type(post.get("type"), "input")
            ns = guard.target_namespace(root, target)
            if ns is not None and not guard.allows_write(self.grant(request), ns):
                return self._refuse(request, 403, "uploads into a Couchy namespace need its read-write key")
        if path == "/upload/mask":
            try:
                ref = json.loads(post.get("original_ref") or "{}")
            except (TypeError, ValueError):
                return None
            if isinstance(ref, dict):
                ns = self._read_target(ref.get("filename", ""), ref.get("subfolder", ""), ref.get("type", "output"))
                if ns is not None and not guard.allows_read(self.grant(request), ns):
                    return self._refuse(request, 403, "that file is in a Couchy namespace this key can't read")
        return None

    async def _check_prompt(self, request):
        try:
            body = await request.json()  # aiohttp keeps the body for the handler
        except Exception:
            return None
        if not isinstance(body, dict):
            return None
        refused = guard.prompt_refusal(body.get("prompt"), self.grant(request))
        if refused:
            return self._refuse(request, 403, "this graph names a Couchy namespace this key can't reach: " + ", ".join(refused))
        return None
