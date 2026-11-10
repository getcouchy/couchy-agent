"""The scope hook: holds ComfyUI's own file resolver to each run's scope.

It wraps three folder_paths functions once at load, the calls stock nodes and
most packs use to turn a value into a file:

- get_annotated_filepath and exists_annotated_filepath (loads). Inside
  couchy/, a path is allowed only under couchy/<ns>/<owner>/ for the run's
  owner, or when it is an upload in the run's inputs
  (input/couchy/<ns>/<anyone>/<id>.*).
- get_save_image_path (saves). Inside couchy/, the folder must be the run's:
  output/ or temp/ couchy/<ns>/<owner>/<runId>/.

Anything else inside couchy/ raises NotInRun, which fails the run with the
node named. Paths outside couchy/ (models, files made outside Couchy) are
untouched, and a prompt without a scope gets nothing inside couchy/.

Every path is resolved, symlinks included, before it is judged. A wrap of
ComfyUI's internals is pinned: install() refuses when the functions'
signatures differ from the ones checked here, and the agent reports
scope_hook "off". Plain Python: the caller passes folder_paths and
get_executing_context.
"""

import functools
import inspect
import logging
import os

from . import scope as scopes_mod
from .files import TOP

log = logging.getLogger("couchy_agent")

_REQUIRED = inspect.Parameter.empty

# The signatures checked on ComfyUI v0.42.5 (483dae4, the runtime manifest's
# pin): parameter names and defaults, in order.
EXPECTED = {
    "get_annotated_filepath": (("name", _REQUIRED), ("default_dir", None)),
    "exists_annotated_filepath": (("name", _REQUIRED),),
    "get_save_image_path": (("filename_prefix", _REQUIRED), ("output_dir", _REQUIRED),
                            ("image_width", 0), ("image_height", 0)),
}

ROOTS = ("input", "output", "temp")

_MARK = "_couchy_scope_hook"


def mismatch(fp):
    """Why the hook can't go on this folder_paths, or None when it can."""
    for name, want in EXPECTED.items():
        fn = getattr(fp, name, None)
        if fn is None:
            return f"folder_paths.{name} is missing"
        try:
            params = inspect.signature(fn).parameters.values()
        except (TypeError, ValueError):
            return f"folder_paths.{name} has no signature"
        got = tuple((p.name, p.default) for p in params)
        kinds = {p.kind for p in params}
        if got != want or kinds - {inspect.Parameter.POSITIONAL_OR_KEYWORD}:
            return f"folder_paths.{name}{inspect.signature(fn)} isn't the signature the hook was checked against"
    if not callable(getattr(fp, "annotated_filepath", None)) or not callable(getattr(fp, "get_input_directory", None)):
        return "folder_paths.annotated_filepath or get_input_directory is missing"
    return None


def couchy_parts(roots, path):
    """[(root kind, [ns, owner, ...])] for each of input, output and temp
    whose couchy/ the real path is inside; empty outside couchy/. The parts
    after couchy/ keep their case; couchy/ itself matches any case, as a
    macOS folder does."""
    real = os.path.realpath(path)
    hits = []
    for kind in ROOTS:
        root = roots.get(kind)
        if not root:
            continue
        rel = os.path.relpath(real, os.path.realpath(root))
        if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
            continue
        parts = rel.split(os.sep)
        if parts[0].lower() == TOP:
            hits.append((kind, parts[1:]))
    return hits


def allows_read(scope, kind, parts):
    if scope is None or not parts or parts[0].lower() != scope.namespace:
        return False
    if not scope.is_run:
        return True  # a namespace scope: the key's whole namespace
    if len(parts) >= 3 and parts[1] == scope.owner:
        return True
    return kind == "input" and len(parts) == 3 and parts[2].split(".", 1)[0] in scope.inputs


def allows_save(scope, kind, parts):
    if scope is None or not parts or parts[0].lower() != scope.namespace:
        return False
    if not scope.is_run:
        return True
    return kind in ("output", "temp") and len(parts) >= 3 and parts[1] == scope.owner and parts[2] == scope.run_id


class Hook:
    """The checks the wraps make. roots() -> {input, output, temp: path};
    get_context() -> ComfyUI's ExecutionContext or None; name_of(class_type)
    -> the node's display name."""

    def __init__(self, scopes, get_context, roots, name_of=None):
        self.scopes = scopes
        self.get_context = get_context
        self.roots = roots
        self.name_of = name_of or (lambda c: c)

    def _judge(self, path, allows):
        hits = couchy_parts(self.roots(), path)
        if not hits:
            return
        scope, node_id = scopes_mod.current(self.scopes, self.get_context)
        if all(allows(scope, kind, parts) for kind, parts in hits):
            return
        self.refuse(scope, node_id)

    def refuse(self, scope, node_id, node_class=None):
        if node_class is None and scope is not None:
            node_class = scope.class_of(node_id)
        ctx = self.get_context()
        log.warning("couchy-agent: refused a file outside the run (prompt %s, run %s, node %s %s)",
                    ctx.prompt_id if ctx else "none", scope.run_id if scope else "none",
                    node_id or "none", node_class or "unknown")
        name = self.name_of(node_class) if node_class else (f"node {node_id}" if node_id else None)
        raise scopes_mod.NotInRun(name)

    def check_read(self, path):
        self._judge(path, allows_read)

    def check_save(self, folder):
        self._judge(folder, allows_save)


def install(fp, hook):
    """Wrap fp's three functions with hook's checks. Returns (True, None),
    or (False, why) when the signatures don't match; nothing is changed
    then."""
    if getattr(fp, _MARK, False):
        return True, None
    why = mismatch(fp)
    if why:
        return False, why

    get_annotated, exists_annotated, save_path = (
        fp.get_annotated_filepath, fp.exists_annotated_filepath, fp.get_save_image_path)

    @functools.wraps(get_annotated)
    def get_annotated_filepath(name, default_dir=None):
        path = get_annotated(name, default_dir)
        hook.check_read(path)
        return path

    @functools.wraps(exists_annotated)
    def exists_annotated_filepath(name):
        n, base = fp.annotated_filepath(name)
        path = os.path.abspath(os.path.join(base or fp.get_input_directory(), n))
        hook.check_read(path)
        return exists_annotated(name)

    @functools.wraps(save_path)
    def get_save_image_path(filename_prefix, output_dir, image_width=0, image_height=0):
        # Checked before ComfyUI makes the folder, and again on the folder
        # it answers, after %width% and the other variables.
        if isinstance(filename_prefix, str) and isinstance(output_dir, str):
            hook.check_save(os.path.join(output_dir, os.path.dirname(os.path.normpath(filename_prefix))))
        out = save_path(filename_prefix, output_dir, image_width, image_height)
        hook.check_save(out[0])
        return out

    fp.get_annotated_filepath = get_annotated_filepath
    fp.exists_annotated_filepath = exists_annotated_filepath
    fp.get_save_image_path = get_save_image_path
    setattr(fp, _MARK, True)
    return True, None
