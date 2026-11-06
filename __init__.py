"""couchy-agent, as a ComfyUI custom node pack. It registers no nodes: it adds
routes under /couchy/ and a guard on ComfyUI's /view, /upload and /prompt.
The code is in the couchy_agent package, which tests import without ComfyUI."""

from .couchy_agent.comfy import setup

setup()

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
