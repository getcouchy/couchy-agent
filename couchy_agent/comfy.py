"""Wiring into a running ComfyUI: the folders from folder_paths, the routes
and middleware on PromptServer.instance.app, the scope hook on folder_paths,
Couchy's nodes, and the start-up checks.

The hook point, checked on ComfyUI 0.44.0 (master c85957a, 2026-11-06):
PromptServer.__init__ builds self.app = web.Application(middlewares=[...]),
then main.py loads custom nodes (nodes.init_extra_nodes) and only later calls
start_multi_address, which freezes the app. So a custom node can still
append to app.middlewares and add routes while it is imported, as
ComfyUI-Login does. Appended last, this middleware runs after ComfyUI's own
(cache control, CORS, origin checks) and before every handler.

The scope hook and the queue wrap are checked on v0.42.5 (483dae4), the
ComfyUI commit in Couchy's runtime manifest.
"""

import logging
import os

import folder_paths
from server import PromptServer

from . import hook, perms
from . import scope as scope_mod
from .routes import Agent, Folders
from .store import Store, format_code

log = logging.getLogger("couchy_agent")

# The pack's folder in custom_nodes, for nodes registered after start-up.
PACK = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _dirs():
    return {
        "input": folder_paths.get_input_directory(),
        "output": folder_paths.get_output_directory(),
        "temp": folder_paths.get_temp_directory(),
        "user": folder_paths.get_user_directory(),
        "models": folder_paths.models_dir,
    }


def _display_name(class_type):
    try:
        import nodes
        return nodes.NODE_DISPLAY_NAME_MAPPINGS.get(class_type) or class_type
    except Exception:
        return class_type


def setup():
    store = Store(folder_paths.get_user_directory())
    store.ensure_dir()
    agent = Agent(store, Folders(_dirs, folder_paths.annotated_filepath))

    app = PromptServer.instance.app
    app.middlewares.append(agent.middleware())
    agent.add_routes(app.router)
    guard_files(agent)

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


def guard_files(agent):
    """Drop scopes as the queue finishes with prompts, and install the scope
    hook on folder_paths when its signatures are the ones it was checked
    against."""
    queue = getattr(PromptServer.instance, "prompt_queue", None)
    if queue is not None:
        scope_mod.watch_queue(queue, agent.scopes)
    else:
        log.warning("couchy-agent: ComfyUI has no prompt queue to watch; run scopes are kept until restart")
    try:
        from comfy_execution.utils import get_executing_context
    except ImportError:
        ok, why = False, "comfy_execution.utils.get_executing_context is missing"
    else:
        ok, why = hook.install(folder_paths, hook.Hook(agent.scopes, get_executing_context, _dirs, _display_name))
    if ok:
        agent.scope_hook = "on"
        log.info("couchy-agent: the scope hook holds folder_paths to each run's files")
    else:
        agent.scope_hook = "off"
        log.warning("couchy-agent: the scope hook is off: %s. Update couchy-agent for this ComfyUI version.", why)


def entrypoint(agent):
    """The V3 extension with Couchy's nodes: listed at load on a paired
    server, and registered on the spot when a server pairs later."""
    from . import nodes

    nodes.configure(agent.scopes)

    def register():
        nodes.register(f"custom_nodes.{PACK}")
        agent.node_version = scope_mod.NODES_VERSION
        log.info("couchy-agent: Couchy's nodes are registered")

    agent.on_paired = register

    def paired():
        if agent.store.paired():
            agent.node_version = scope_mod.NODES_VERSION
            return True
        return False

    return nodes.CouchyExtension(paired)
