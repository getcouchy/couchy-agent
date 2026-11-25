"""The /couchy/models routes: the inventory, a .safetensors header, the
space on each volume and what ran together (read), and pulls, deletes and
tickets (write). The blob route takes a ticket instead of a key. A key
whose models grant is none gets 403 everywhere, and read gets 403 on the
write routes.

Agent.add_routes adds these when it has a ModelsRoutes, so routes.py only
grows by the call.
"""

import asyncio
import json
import logging
import os

from aiohttp import web

from . import history as history_mod
from . import models, pulls as pulls_mod
from .obs import sha12
from .tickets import DEFAULT_TTL

log = logging.getLogger("couchy_agent.models")


class ModelsRoutes:
    def __init__(self, locations, index=None, hasher=None, pulls=None, tickets=None, history=None):
        self.locations = locations  # models.Locations
        self.index = index  # hashing.Index
        self.hasher = hasher  # hashing.Hasher: listings come from its last scan
        self.pulls = pulls  # pulls.Pulls
        self.tickets = tickets  # tickets.Tickets
        self.history = history  # () -> ComfyUI's history dict; None in store mode

    def add_routes(self, router, need):
        """need(request) is the Agent's key check: the grant, or it raises
        401."""
        self._need_key = need
        for prefix in ("", "/api"):
            router.add_get(prefix + "/couchy/models", self.list)
            router.add_get(prefix + "/couchy/models/header", self.header)
            router.add_get(prefix + "/couchy/models/space", self.space)
            router.add_get(prefix + "/couchy/models/history", self.history_sets)
            if self.pulls is not None:
                router.add_post(prefix + "/couchy/models/pull", self.pull)
                router.add_get(prefix + "/couchy/models/pulls/{id}", self.pull_state)
                router.add_post(prefix + "/couchy/models/pulls/{id}/cancel", self.cancel)
            if self.hasher is not None:
                router.add_post(prefix + "/couchy/models/delete", self.delete)
            if self.tickets is not None:
                router.add_post(prefix + "/couchy/models/tickets", self.mint_ticket)
                router.add_get(prefix + "/couchy/models/blob/{sha256}", self.blob)

    def _need(self, request, write=False):
        g = self._need_key(request)
        grant = g.get("models", "read")
        if grant == "none" or (write and grant != "write"):
            log.warning("models request refused", extra={"key": g["id"], "route": request.path, "grant": grant})
            text = "this key has no models grant" if grant == "none" else "this key's models grant is read; this needs write"
            raise web.HTTPForbidden(text=json.dumps({"error": text}), content_type="application/json")
        return g

    async def _body(self, request):
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="the body must be JSON")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="the body must be an object")
        return body

    def _location(self, locs, value):
        if value is None:
            return None
        if value not in locs:
            raise web.HTTPBadRequest(text=f"location must be one of {', '.join(locs)}")
        return value

    def _folder(self, locs, value, required=False):
        if value is None and not required:
            return None
        known = sorted({f for folders in locs.values() for f in folders})
        if value not in known:
            raise web.HTTPBadRequest(text="folder must be a ComfyUI models folder, such as checkpoints or loras")
        return value

    async def list(self, request):
        self._need(request)
        q = request.query
        locs = await asyncio.to_thread(self.locations.all)
        location = self._location(locs, q.get("location"))
        folder = self._folder(locs, q.get("folder"))
        try:
            limit = int(q.get("limit", str(models.MAX_LIMIT)))
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be a number")

        def page():
            found = self.hasher.found(locs, location, folder) if self.hasher is not None else None
            return models.list_models(locs, self.index, location, folder, q.get("after", ""), limit, found)

        out = await asyncio.to_thread(page)
        return web.json_response(out)

    async def header(self, request):
        self._need(request)
        q = request.query
        locs = await asyncio.to_thread(self.locations.all)
        location = self._location(locs, q.get("location", models.HOST))
        folder = self._folder(locs, q.get("folder"), required=True)
        real = await asyncio.to_thread(models.resolve, locs[location].get(folder, []), q.get("path", ""))
        if real is None:
            return web.json_response({"error": "no such model file"}, status=404)
        try:
            out = await asyncio.to_thread(models.read_header, real)
        except models.NotSafetensors as e:
            return web.json_response({"error": str(e)}, status=422)
        except OSError:
            return web.json_response({"error": "no such model file"}, status=404)
        return web.json_response(out)

    async def space(self, request):
        self._need(request)
        locs = await asyncio.to_thread(self.locations.all)
        out = await asyncio.to_thread(models.space, locs)
        # What a pull may fetch, so the API can say so before it starts one.
        out["allowPickle"] = self.pulls is not None and self.pulls.config.allow_pickle()
        return web.json_response(out)

    # What ran together -----------------------------------------------------

    async def history_sets(self, request):
        self._need(request)
        since = request.query.get("since")
        if since is not None:
            try:
                since = float(since)
            except ValueError:
                raise web.HTTPBadRequest(text="since is seconds since the epoch")
        if self.history is None or self.index is None:
            return web.json_response({"runs": []})

        def sets():
            return history_mod.model_sets(self.history(), self.index.shas(), since)

        return web.json_response(await asyncio.to_thread(sets))

    # Pulls -------------------------------------------------------------------

    async def pull(self, request):
        self._need(request, write=True)
        body = await self._body(request)
        locs = await asyncio.to_thread(self.locations.all)
        try:
            p = self.pulls.start(body, locs)
        except pulls_mod.BadPull as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response({"id": p.id})

    async def pull_state(self, request):
        self._need(request)
        p = self.pulls.get(request.match_info["id"])
        if p is None:
            return web.json_response({"error": "no such pull"}, status=404)
        return web.json_response(p.view())

    async def cancel(self, request):
        self._need(request, write=True)
        p = await self.pulls.cancel(request.match_info["id"])
        if p is None:
            return web.json_response({"error": "no such pull"}, status=404)
        return web.json_response(p.view())

    # Delete ------------------------------------------------------------------

    async def delete(self, request):
        self._need(request, write=True)
        body = await self._body(request)
        sha = body.get("sha256")
        if not pulls_mod.valid_sha(sha):
            raise web.HTTPBadRequest(text="sha256 must be 64 lowercase hex characters")
        locs = await asyncio.to_thread(self.locations.all)
        location = self._location(locs, body.get("location", models.HOST))
        folder = body.get("folder")
        if folder not in locs[location]:
            raise web.HTTPBadRequest(text="folder must be a ComfyUI models folder, such as checkpoints or loras")
        path = body.get("path")
        out = await asyncio.to_thread(self._delete, locs[location][folder], location, folder, path, sha)
        log.info("model delete", extra={"location": location, "folder": folder, "sha": sha12(sha), **out})
        return web.json_response(out)

    def _delete(self, dirs, location, folder, path, sha):
        parts = models.split_path(path)
        if parts is None:
            return {"state": "refused", "reason": "path"}
        real = models.resolve(dirs, path)
        if real is None:
            if any(os.path.lexists(os.path.join(d, *parts)) for d in dirs):
                return {"state": "refused", "reason": "symlink"}  # or outside its folder
            return {"state": "missing"}
        st = os.lstat(real)
        known = self.index.lookup(location, folder, path, st.st_size, st.st_mtime)
        if not known or not known.get("sha256"):
            return {"state": "refused", "reason": "not_hashed"}
        if known["sha256"] != sha:
            return {"state": "mismatch"}
        os.remove(real)
        self.hasher.removed(location, folder, path)
        return {"state": "deleted"}

    # Tickets and the blob ------------------------------------------------------

    async def mint_ticket(self, request):
        g = self._need(request, write=True)
        body = await self._body(request)
        sha = body.get("sha256")
        if not pulls_mod.valid_sha(sha):
            raise web.HTTPBadRequest(text="sha256 must be 64 lowercase hex characters")
        locs = await asyncio.to_thread(self.locations.all)
        location = self._location(locs, body.get("location", models.HOST))
        ttl = body.get("ttl", DEFAULT_TTL)
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
            raise web.HTTPBadRequest(text="ttl is seconds, more than 0")
        if not self.index.with_sha(sha, location):
            return web.json_response({"error": "no file with that hash here"}, status=404)
        out = self.tickets.mint(sha, location, ttl)
        log.info("model ticket minted", extra={"key": g["id"], "location": location, "sha": sha12(sha),
                                               "ttl": int(out["expiresAt"] - self.tickets.clock())})
        return web.json_response(out)

    async def blob(self, request):
        sha = request.match_info["sha256"]
        ticket = request.headers.get(pulls_mod.TICKET_HEADER) or request.query.get("ticket")
        location = self.tickets.redeem(ticket, sha) if pulls_mod.valid_sha(sha) else None
        if location is None:
            log.warning("model blob refused", extra={"sha": sha12(sha) if pulls_mod.valid_sha(sha) else None})
            return web.json_response({"error": "that ticket is wrong, used or expired"}, status=403)
        locs = await asyncio.to_thread(self.locations.all)
        real = await asyncio.to_thread(self._find, locs.get(location, {}), location, sha)
        if real is None:
            return web.json_response({"error": "no file with that hash here"}, status=404)
        log.info("model blob served", extra={"location": location, "sha": sha12(sha), "range": "Range" in request.headers})
        return web.FileResponse(real, headers={"Content-Type": "application/octet-stream"})

    def _find(self, folders, location, sha):
        """The real path of a file in the location indexed with this hash
        and unchanged since, or None."""
        for _, folder, path, entry in self.index.with_sha(sha, location):
            real = models.resolve(folders.get(folder, []), path)
            if real is None:
                continue
            st = os.lstat(real)
            if (st.st_size, st.st_mtime) == (entry["size"], entry["mtime"]):
                return real
        return None
