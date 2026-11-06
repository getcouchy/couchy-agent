import os
import tempfile


class Tree:
    """A throwaway ComfyUI base folder: input, output, temp, user, models,
    plus a secret outside them all."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.dirs = {}
        for name in ("input", "output", "temp", "user", "models"):
            path = os.path.join(self.base, name)
            os.makedirs(path)
            os.chmod(path, 0o700)
            self.dirs[name] = path
        self.write("models/model.safetensors", b"weights")

    def path(self, rel):
        return os.path.join(self.base, rel)

    def write(self, rel, data=b"x"):
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        return full

    def exists(self, rel):
        return os.path.exists(self.path(rel))

    def cleanup(self):
        self.tmp.cleanup()
