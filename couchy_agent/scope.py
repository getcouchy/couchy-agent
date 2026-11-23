"""Run scopes: which person a prompt runs for and which uploads it may read.
Plain Python, no ComfyUI imports.

The API sends extra_data.couchy = {runId, owner, inputs: [uploadId, ...]} on
POST /prompt with its rw key. The /prompt guard (routes.py) checks it, binds
it here against the prompt id and strips it from the body, so ComfyUI never
stores it in history. Couchy's nodes (nodes.py) and the folder_paths hook
(hook.py) look the scope up by the prompt id of the node that is running.

Scopes live in memory. They go when the prompt finishes, is deleted from the
queue or is refused by ComfyUI; a restart loses the queue anyway.

A prompt sent with an rw key but no extra_data.couchy (the API before it sends
scopes) gets a namespace scope: it reaches all of its key's namespace, as
the key already does through /view, and nothing else inside couchy/.
"""

import threading
from dataclasses import dataclass, field

from .files import valid_id

NOT_IN_RUN = "This file isn't part of this run."
OUTSIDE_COUCHY = "This node only runs in runs Couchy starts."

MAX_INPUTS = 10000

# Reported as "nodes" in GET /couchy/usage once Couchy's nodes are
# registered, so the API knows it can swap them in. Bump it when a node's
# inputs or outputs change.
NODES_VERSION = 2  # 2: Couchy Save Latent and Couchy Load Latent


class NotInRun(Exception):
    """A node asked for a file its run wasn't given. The message is the
    line people see, with the node's name."""

    def __init__(self, node=None):
        self.node = node
        super().__init__(f"{NOT_IN_RUN} ({node})" if node else NOT_IN_RUN)


class OutsideCouchy(Exception):
    def __init__(self, node=None):
        self.node = node
        super().__init__(f"{OUTSIDE_COUCHY} ({node})" if node else OUTSIDE_COUCHY)


@dataclass(frozen=True)
class Scope:
    namespace: str
    owner: str | None  # None: a namespace scope (no extra_data.couchy)
    run_id: str | None
    inputs: frozenset = frozenset()
    metadata: bool = True
    # node id -> class_type, from the graph as sent, to name a refused node.
    classes: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def is_run(self):
        return self.owner is not None and self.run_id is not None

    def class_of(self, node_id):
        """The class of a node in the graph. A node made by expansion has an
        id built on the one it came from ("12.0.3"), so fall back to that."""
        if node_id is None:
            return None
        node_id = str(node_id)
        if node_id in self.classes:
            return self.classes[node_id]
        return self.classes.get(node_id.split(".")[0])


class BadScope(ValueError):
    pass


def parse(value, namespace, prompt):
    """The Scope extra_data.couchy describes, or BadScope saying what's
    wrong with it."""
    if not isinstance(value, dict):
        raise BadScope("extra_data.couchy must be an object")
    run_id, owner, inputs = value.get("runId"), value.get("owner"), value.get("inputs", [])
    if not valid_id(run_id):
        raise BadScope("extra_data.couchy.runId must be a Couchy run id")
    if not valid_id(owner):
        raise BadScope("extra_data.couchy.owner must be a Couchy user id or lab")
    if not isinstance(inputs, list) or len(inputs) > MAX_INPUTS or not all(valid_id(i) for i in inputs):
        raise BadScope("extra_data.couchy.inputs must be a list of upload ids")
    metadata = value.get("metadata", True)
    if not isinstance(metadata, bool):
        raise BadScope("extra_data.couchy.metadata must be true or false")
    return Scope(namespace.lower(), owner, run_id, frozenset(inputs), metadata, _classes(prompt))


def namespace_scope(namespace, prompt):
    return Scope(namespace.lower(), None, None, classes=_classes(prompt))


def _classes(prompt):
    if not isinstance(prompt, dict):
        return {}
    return {str(k): v.get("class_type") for k, v in prompt.items()
            if isinstance(v, dict) and isinstance(v.get("class_type"), str)}


class Scopes:
    """Prompt id -> Scope, shared by the HTTP side and the worker thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_prompt = {}

    def bind(self, prompt_id, scope):
        """False when the prompt id already has a scope."""
        with self._lock:
            if prompt_id in self._by_prompt:
                return False
            self._by_prompt[prompt_id] = scope
            return True

    def get(self, prompt_id):
        return self._by_prompt.get(prompt_id)

    def has(self, prompt_id):
        return prompt_id in self._by_prompt

    def drop(self, *prompt_ids):
        with self._lock:
            for p in prompt_ids:
                self._by_prompt.pop(p, None)

    def __len__(self):
        return len(self._by_prompt)


def current(scopes, get_context):
    """(scope or None, node id or None) for the node running now: one
    context lookup, and one dict lookup when a prompt is running."""
    ctx = get_context()
    if ctx is None:
        return None, None
    return scopes.get(ctx.prompt_id), ctx.node_id


def watch_queue(queue, scopes):
    """Drop a prompt's scope when ComfyUI's PromptQueue is done with it:
    task_done (finished, failed or interrupted), delete_queue_item and
    wipe_queue (deleted before it ran). Wraps the instance's methods."""
    task_done, delete_item, wipe = queue.task_done, queue.delete_queue_item, queue.wipe_queue

    def task_done_and_drop(item_id, *args, **kwargs):
        with queue.mutex:
            item = queue.currently_running.get(item_id)
        try:
            return task_done(item_id, *args, **kwargs)
        finally:
            if item is not None:
                scopes.drop(item[1])

    def delete_and_drop(function):
        hit = []

        def matches(item):
            if function(item):
                hit.append(item[1])
                return True
            return False

        try:
            return delete_item(matches)
        finally:
            scopes.drop(*hit)

    def wipe_and_drop():
        with queue.mutex:
            ids = [item[1] for item in queue.queue]
            try:
                return wipe()
            finally:
                scopes.drop(*ids)

    queue.task_done = task_done_and_drop
    queue.delete_queue_item = delete_and_drop
    queue.wipe_queue = wipe_and_drop
