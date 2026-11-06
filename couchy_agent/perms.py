"""The OS-permissions check on ComfyUI's folders: input, output, temp, user
and models should be private to the account ComfyUI runs as."""

import os
import stat

CHECKED = ("input", "output", "temp", "user", "models")


def open_folders(dirs):
    """[(name, path, mode)] for each folder that other accounts can reach:
    any group or world bit set. Missing folders are skipped."""
    out = []
    for name in CHECKED:
        path = dirs.get(name)
        if not path:
            continue
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            continue
        if mode & 0o077:
            out.append((name, path, mode))
    return out


def permissions(dirs):
    return "open" if open_folders(dirs) else "ok"
