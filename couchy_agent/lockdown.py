"""Lockdown mode (COUCHY_AGENT_LOCKDOWN=1), for a server Couchy created,
such as a cloud GPU: every route ComfyUI serves needs an agent key, not
only the ones the guard watches. That includes the websocket, /object_info,
/system_stats and ComfyUI's own UI, which isn't supported on such a server.

Only two routes stay open, each with its own credential: pairing (a code or
the pod's pairing token) and the models blob (a ticket). It's a second
middleware beside the guard, appended before it so a request without a key
never reaches the guard's body parsing.
"""

import logging
import os

from aiohttp import web

log = logging.getLogger("couchy_agent")

ENV = "COUCHY_AGENT_LOCKDOWN"

OPEN = ("/couchy/pair",)
OPEN_PREFIXES = ("/couchy/models/blob/",)


def enabled(env=None):
    return (env if env is not None else os.environ).get(ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _open(path):
    if path.startswith("/api/"):
        path = path[4:]
    return path in OPEN or path.startswith(OPEN_PREFIXES)


def middleware(agent):
    @web.middleware
    async def couchy_agent_lockdown(request, handler):
        if _open(request.path) or agent.grant(request) is not None:
            return await handler(request)
        log.warning("couchy-agent: lockdown refused %s %s (no key)", request.method, request.path)
        return web.json_response({"error": "this server needs a couchy-agent key on every request"}, status=401,
                                 headers={"WWW-Authenticate": "Couchy-Agent"})

    return couchy_agent_lockdown
