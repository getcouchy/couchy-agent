"""Loading the pack the way ComfyUI does, with stand-ins for folder_paths and
server: it adds its middleware and routes to PromptServer.instance.app and
writes a pairing code."""

import importlib.util
import os
import sys
import types
import unittest

from aiohttp import web

from tests.helpers import Tree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        os.chmod(self.t.dirs["models"], 0o755)
        fp = types.ModuleType("folder_paths")
        fp.get_input_directory = lambda: self.t.dirs["input"]
        fp.get_output_directory = lambda: self.t.dirs["output"]
        fp.get_temp_directory = lambda: self.t.dirs["temp"]
        fp.get_user_directory = lambda: self.t.dirs["user"]
        fp.models_dir = self.t.dirs["models"]
        fp.annotated_filepath = lambda name: (name, None)
        srv = types.ModuleType("server")
        self.app = web.Application(middlewares=[])
        srv.PromptServer = types.SimpleNamespace(instance=types.SimpleNamespace(app=self.app))
        self.saved = {k: sys.modules.get(k) for k in ("folder_paths", "server")}
        sys.modules.update(folder_paths=fp, server=srv)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        for k in [k for k in sys.modules if k.startswith("couchy_agent_pack")]:
            del sys.modules[k]
        self.t.cleanup()

    def test_loads_as_a_custom_node(self):
        spec = importlib.util.spec_from_file_location(
            "couchy_agent_pack", os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["couchy_agent_pack"] = mod
        with self.assertLogs("couchy_agent", "WARNING") as logs:
            spec.loader.exec_module(mod)
        self.assertFalse(hasattr(mod, "NODE_CLASS_MAPPINGS"))  # V3: nodes come from comfy_entrypoint
        self.assertTrue(callable(mod.comfy_entrypoint))
        self.assertEqual(mod._agent.scope_hook, "off")  # this folder_paths has none of the three functions
        self.assertEqual(len(self.app.middlewares), 1)
        paths = {r.resource.canonical for r in self.app.router.routes()}
        self.assertTrue({"/couchy/pair", "/couchy/usage", "/api/couchy/files/adopt"} <= paths)
        self.assertTrue(os.path.exists(os.path.join(self.t.dirs["user"], "couchy-agent", "pairing-code")))
        text = "\n".join(logs.output)
        self.assertIn("pairing code", text)
        self.assertIn("models folder", text)  # 0755: open to other accounts
        self.assertNotIn("output folder", text)
        self.assertIn("scope hook is off", text)


if __name__ == "__main__":
    unittest.main()
