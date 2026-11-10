"""The /couchy/models routes. Read-only in this step: the inventory, a
.safetensors header and the space on each volume. Every route needs a key
whose models grant is read or write; none answers 403.

Agent.add_routes adds these when it has a ModelsRoutes, so routes.py only
grows by the call.
"""

import asyncio
import json
import logging

from aiohttp import web

from . import models

log = logging.getLogger("couchy_agent.models")


class ModelsRoutes:
    def __init__(self, locations, index=None, hasher=None):
        self.locations = locations  # models.Locations
        self.index = index  # hashing.Index
        self.hasher = hasher  # hashing.Hasher: listings come from its last scan

    def add_routes(self, router, need):
        """need(request) is the Agent's key check: the grant, or it raises
        401."""
        self._need_key = need
        for prefix in ("", "/api"):
            router.add_get(prefix + "/couchy/models", self.list)
            router.add_get(prefix + "/couchy/models/header", self.header)
            router.add_get(prefix + "/couchy/models/space", self.space)

    def _need(self, request):
        g = self._need_key(request)
        if g.get("models", "read") == "none":
            log.warning("models request refused", extra={"key": g["id"], "route": request.path, "grant": "none"})
            raise web.HTTPForbidden(text=json.dumps({"error": "this key has no models grant"}),
                                    content_type="application/json")
        return g

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
        return web.json_response(await asyncio.to_thread(models.space, locs))
