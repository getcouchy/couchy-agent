"""couchy-agent, as a ComfyUI custom node pack. It adds routes under /couchy/,
a guard on ComfyUI's /view, /upload and /prompt, a scope hook on folder_paths,
and, once the server is paired, Couchy's own file nodes (V3, category couchy).
The code is in the couchy_agent package, which tests import without ComfyUI."""

from .couchy_agent.comfy import entrypoint, setup

_agent = setup()


async def comfy_entrypoint():
    return entrypoint(_agent)
