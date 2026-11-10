"""Store mode: the models routes, pairing and keys without ComfyUI, for a
host that holds model copies but runs nothing.

    python -m couchy_agent store --root /volume1/models --listen 0.0.0.0:8190

--root is laid out like ComfyUI's models folder (checkpoints/, loras/, ...)
and is the "host" location; --share adds more, as on a GPU host. There's
no /view, no guard and no namespaced files. It needs Python 3.10+ and
aiohttp. Put it behind TLS (a reverse proxy) when it listens beyond
localhost: its key and tickets travel on every call.
"""

import logging
import os

from aiohttp import web

from . import models, obs
from .config import Config
from .hashing import Hasher, Index
from .models_routes import ModelsRoutes
from .pulls import Pulls
from .routes import Agent, Folders
from .store import Store, format_code
from .tickets import Tickets

log = logging.getLogger("couchy_agent")

DEFAULT_LISTEN = "127.0.0.1:8190"


def parse_listen(text):
    """host:port, [v6]:port or :port (all interfaces). ValueError
    otherwise."""
    host, sep, port = (text or "").rpartition(":")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"--listen takes host:port, not {text!r}")
    host = host.strip("[]") or "0.0.0.0"
    return host, int(port)


def build(user_dir, root, shares=None):
    """The store-mode app. shares, when given, replaces the config's for
    this run."""
    store = Store(user_dir)
    store.ensure_dir()
    store.migrate()
    obs.models_logger()
    config = Config(store)
    share_source = (lambda: dict(shares)) if shares is not None else config.shares
    root = os.path.abspath(root)
    locations = models.Locations(lambda _shares: models.share_folders(root), share_source, root)
    index = Index(store)
    hasher = Hasher(locations, index)
    pulls = Pulls(locations, hasher, config)
    agent = Agent(store, Folders({}), models=ModelsRoutes(locations, index, hasher, pulls=pulls, tickets=Tickets()))
    app = web.Application()
    agent.add_routes(app.router, files=False)

    async def started(_app):
        hasher.start()

    async def stopping(_app):
        hasher.stop()
        await pulls.close()

    app.on_startup.append(started)
    app.on_cleanup.append(stopping)
    agent.hasher = hasher

    code = store.mint_code_if_none()
    if code:
        log.warning("couchy-agent: pairing code %s (valid 24 hours, also in %s/pairing-code)", format_code(code), store.dir)
    else:
        log.info("couchy-agent: a pairing code is waiting in %s/pairing-code", store.dir)
    return app, agent


def run(user_dir, root, listen=DEFAULT_LISTEN, shares=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    host, port = parse_listen(listen)
    app, _ = build(user_dir, root, shares)
    if host not in ("127.0.0.1", "::1", "localhost"):
        log.warning("couchy-agent: store mode listens on %s; put it behind TLS, since keys and tickets travel on "
                    "every call", host)
    web.run_app(app, host=host, port=port, access_log=None, print=None)
