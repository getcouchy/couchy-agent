"""Wiring into a running ComfyUI: the folders from folder_paths, the routes
and middleware on PromptServer.instance.app, and the start-up checks.

The hook point, checked on ComfyUI 0.44.0 (master c85957a, 2026-11-06):
PromptServer.__init__ builds self.app = web.Application(middlewares=[...]),
then main.py loads custom nodes (nodes.init_extra_nodes) and only later calls
start_multi_address, which freezes the app. So a custom node can still
append to app.middlewares and add routes while it is imported, as
ComfyUI-Login does. Appended last, this middleware runs after ComfyUI's own
(cache control, CORS, origin checks) and before every handler.
"""

import logging

import folder_paths
from server import PromptServer

from . import perms
from .routes import Agent, Folders
from .store import Store, format_code

log = logging.getLogger("couchy_agent")


def _dirs():
    return {
        "input": folder_paths.get_input_directory(),
        "output": folder_paths.get_output_directory(),
        "temp": folder_paths.get_temp_directory(),
        "user": folder_paths.get_user_directory(),
        "models": folder_paths.models_dir,
    }


def setup():
    store = Store(folder_paths.get_user_directory())
    store.ensure_dir()
    agent = Agent(store, Folders(_dirs, folder_paths.annotated_filepath))

    app = PromptServer.instance.app
    app.middlewares.append(agent.middleware())
    agent.add_routes(app.router)

    for name, path, mode in perms.open_folders(_dirs()):
        log.warning(
            "couchy-agent: ComfyUI's %s folder (%s) is open to other accounts on this machine (mode %o). "
            "Run ComfyUI as its own account with umask 077 and chmod -R go-rwx it; see couchy-agent's README.",
            name, path, mode)

    code = store.mint_code_if_none()
    if code:
        log.warning("couchy-agent: pairing code %s (valid 24 hours, also in %s/pairing-code)", format_code(code), store.dir)
    else:
        log.info("couchy-agent: a pairing code is waiting in %s/pairing-code", store.dir)
    return agent
