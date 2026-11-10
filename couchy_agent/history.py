"""Which model files ran together, from ComfyUI's history: sets of SHA-256s
with a time each. Nothing else from a history item leaves this module (no
prompt, prompt id, image or other input).

A model is found by its name: any string input of a node that names a
hashed model file (its path in a folder), with \\ read as /. A name that
matches files with different hashes (the same path in two folders or
locations, holding different files) is left out rather than guessed.
"""

TIME_EVENTS = ("execution_start", "execution_success", "execution_cached")


def _when(item):
    """The run's time in seconds, from its status messages."""
    status = item.get("status") if isinstance(item, dict) else None
    messages = status.get("messages") if isinstance(status, dict) else None
    if not isinstance(messages, list):
        return None
    for event in TIME_EVENTS:
        for m in messages:
            if isinstance(m, (list, tuple)) and len(m) == 2 and m[0] == event and isinstance(m[1], dict):
                ts = m[1].get("timestamp")
                if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                    return ts / 1000
    return None


def _graph(item):
    p = item.get("prompt") if isinstance(item, dict) else None
    if isinstance(p, (list, tuple)) and len(p) > 2 and isinstance(p[2], dict):
        return p[2]
    return None


def by_name(shas):
    """{path: sha256} from the index's {(location, folder, path): sha256},
    without the paths that name more than one hash."""
    seen = {}
    for (_, _, path), sha in shas.items():
        seen.setdefault(path, set()).add(sha)
    return {path: next(iter(s)) for path, s in seen.items() if len(s) == 1}


def model_sets(history, shas, since=None):
    """{runs: [{at, sha256: [...]}]}, oldest first, for each run after
    since that used a known model."""
    names = by_name(shas)
    runs = []
    for item in (history or {}).values():
        at = _when(item)
        graph = _graph(item)
        if at is None or graph is None or (since is not None and at <= since):
            continue
        found = set()
        for node in graph.values():
            inputs = node.get("inputs") if isinstance(node, dict) else None
            if not isinstance(inputs, dict):
                continue
            for v in inputs.values():
                if isinstance(v, str):
                    sha = names.get(v.replace("\\", "/"))
                    if sha:
                        found.add(sha)
        if found:
            runs.append({"at": at, "sha256": sorted(found)})
    runs.sort(key=lambda r: r["at"])
    return {"runs": runs}
