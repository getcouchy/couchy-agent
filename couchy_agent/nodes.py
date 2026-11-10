"""Couchy's own file nodes, as V3 nodes (comfy_api.latest), under the category
couchy. Needs ComfyUI; the rest of the package doesn't.

They take an upload id, not a path, and open only what the run's scope
(scope.py) lists. The work itself is the stock node's, called on the
resolved file, so the outputs, EXIF and alpha handling, numbering and PNG
metadata are ComfyUI's own:

    CouchyLoadImage     LoadImage       upload             IMAGE, MASK
    CouchyLoadMask      LoadImageMask   upload, channel    MASK
    CouchyLoadVideo     LoadVideo       upload             VIDEO
    CouchySaveImage     SaveImage       images             to output/couchy/<ns>/<owner>/<runId>/
    CouchyPreviewImage  PreviewImage    images             to temp/couchy/<ns>/<owner>/<runId>/

Without a run scope each fails with "This node only runs in runs Couchy
starts."; an upload that isn't in the scope fails with "This file isn't part
of this run.", naming the node. The stock calls still go through the scope
hook (hook.py), which allows exactly these files.
"""

import logging
import os

import folder_paths
import nodes as comfy_nodes
from comfy_api.latest import ComfyExtension, io
from comfy_execution.utils import get_executing_context

from . import scope as scope_mod
from .files import TOP, valid_id

log = logging.getLogger("couchy_agent")

CATEGORY = "couchy"
SAVE_NAME = "ComfyUI"
CHANNELS = ["alpha", "red", "green", "blue"]

_scopes = None  # set by configure()


def configure(scopes):
    global _scopes
    _scopes = scopes


def _scope(display):
    """The running prompt's run scope, or OutsideCouchy."""
    scope, _ = scope_mod.current(_scopes, get_executing_context) if _scopes is not None else (None, None)
    if scope is None or not scope.is_run:
        raise scope_mod.OutsideCouchy(display)
    return scope


def _refuse(scope, display, node_id):
    ctx = get_executing_context()
    log.warning("couchy-agent: refused an upload outside the run (prompt %s, run %s, node %s)",
                ctx.prompt_id if ctx else "none", scope.run_id, node_id)
    raise scope_mod.NotInRun(display)


def find_upload(input_dir, namespace, upload):
    """input/couchy/<ns>/<owner>/<upload>.<ext> as a name relative to the
    input folder, or None. Owners and files that are symlinks are skipped."""
    base = os.path.join(input_dir, TOP, namespace)
    try:
        owners = sorted(os.listdir(base))
    except OSError:
        return None
    found = []
    for owner in owners:
        d = os.path.join(base, owner)
        if not valid_id(owner) or os.path.islink(d) or not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            full = os.path.join(d, name)
            if name.split(".", 1)[0] == upload and not os.path.islink(full) and os.path.isfile(full):
                found.append(f"{TOP}/{namespace}/{owner}/{name}")
    return found[0] if len(found) == 1 else None


def _resolve(cls, upload):
    """The file a load node opens, as a name relative to the input folder."""
    display = cls.DISPLAY
    scope = _scope(display)
    ctx = get_executing_context()
    if not valid_id(upload) or upload not in scope.inputs:
        _refuse(scope, display, ctx.node_id if ctx else None)
    name = find_upload(folder_paths.get_input_directory(), scope.namespace, upload)
    if name is None:
        raise FileNotFoundError(f"This upload isn't on this server. ({display})")
    return name


def _load_fingerprint(cls, upload):
    """Never reuse a cached load for a prompt whose scope doesn't list the
    upload: NaN never equals itself, so the node runs, and refuses."""
    try:
        name = _resolve(cls, upload)
        st = os.stat(folder_paths.get_annotated_filepath(name))
    except Exception:
        return float("nan")
    return f"{upload}:{st.st_mtime_ns}:{st.st_size}"


def _load_validate(cls, upload):
    if not isinstance(upload, str):
        return True  # wired from another node: checked when it runs
    display = cls.DISPLAY
    scope, _ = scope_mod.current(_scopes, get_executing_context) if _scopes is not None else (None, None)
    if scope is None or not scope.is_run:
        return str(scope_mod.OutsideCouchy(display))
    if upload not in scope.inputs:
        return str(scope_mod.NotInRun(display))
    return True


def _upload_input():
    return io.String.Input("upload", tooltip="The Couchy upload id. Couchy fills it in.")


class CouchyLoadImage(io.ComfyNode):
    DISPLAY = "Couchy Load Image"  # read by the node's own methods, which can't ask for the schema

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="CouchyLoadImage",
            display_name=cls.DISPLAY,
            category=CATEGORY,
            description="Loads an image this run was given, by its Couchy upload id.",
            inputs=[_upload_input()],
            outputs=[io.Image.Output(), io.Mask.Output()],
        )

    @classmethod
    def execute(cls, upload) -> io.NodeOutput:
        image, mask = comfy_nodes.LoadImage().load_image(_resolve(cls, upload))
        return io.NodeOutput(image, mask)

    @classmethod
    def fingerprint_inputs(cls, upload):
        return _load_fingerprint(cls, upload)

    @classmethod
    def validate_inputs(cls, upload=None):
        return _load_validate(cls, upload)


class CouchyLoadMask(io.ComfyNode):
    DISPLAY = "Couchy Load Mask"  # read by the node's own methods, which can't ask for the schema

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="CouchyLoadMask",
            display_name=cls.DISPLAY,
            category=CATEGORY,
            description="Loads a mask from an image this run was given, by its Couchy upload id.",
            inputs=[_upload_input(), io.Combo.Input("channel", options=CHANNELS)],
            outputs=[io.Mask.Output()],
        )

    @classmethod
    def execute(cls, upload, channel) -> io.NodeOutput:
        (mask,) = comfy_nodes.LoadImageMask().load_image_mask(_resolve(cls, upload), channel)
        return io.NodeOutput(mask)

    @classmethod
    def fingerprint_inputs(cls, upload, channel=None):
        return _load_fingerprint(cls, upload)

    @classmethod
    def validate_inputs(cls, upload=None, channel=None):
        return _load_validate(cls, upload)


class CouchyLoadVideo(io.ComfyNode):
    DISPLAY = "Couchy Load Video"  # read by the node's own methods, which can't ask for the schema

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="CouchyLoadVideo",
            display_name=cls.DISPLAY,
            category=CATEGORY,
            description="Loads a video this run was given, by its Couchy upload id.",
            inputs=[_upload_input()],
            outputs=[io.Video.Output()],
        )

    @classmethod
    def execute(cls, upload) -> io.NodeOutput:
        from comfy_extras.nodes_video import LoadVideo  # core's, imported when first used
        return LoadVideo.execute(_resolve(cls, upload))

    @classmethod
    def fingerprint_inputs(cls, upload):
        return _load_fingerprint(cls, upload)

    @classmethod
    def validate_inputs(cls, upload=None):
        return _load_validate(cls, upload)


def _save(cls, stock, images):
    scope = _scope(cls.DISPLAY)
    prefix = f"{TOP}/{scope.namespace}/{scope.owner}/{scope.run_id}/{SAVE_NAME}"
    prompt, extra = (cls.hidden.prompt, cls.hidden.extra_pnginfo) if scope.metadata else (None, None)
    out = stock.save_images(images, filename_prefix=prefix, prompt=prompt, extra_pnginfo=extra)
    return io.NodeOutput(*out["result"], ui=out["ui"])


def _save_fingerprint():
    """A save is never answered from another run's cache: it must write to
    this run's folder."""
    ctx = get_executing_context()
    scope = _scopes.get(ctx.prompt_id) if (ctx is not None and _scopes is not None) else None
    if scope is None or not scope.is_run:
        return float("nan")
    return f"{scope.namespace}/{scope.owner}/{scope.run_id}"


class CouchySaveImage(io.ComfyNode):
    DISPLAY = "Couchy Save Image"  # read by the node's own methods, which can't ask for the schema

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="CouchySaveImage",
            display_name=cls.DISPLAY,
            category=CATEGORY,
            description="Saves the images to this run's folder.",
            inputs=[io.Image.Input("images", tooltip="The images to save.")],
            outputs=[io.Image.Output("images")],
            hidden=[io.Hidden.prompt, io.Hidden.extra_pnginfo],
            is_output_node=True,
        )

    @classmethod
    def execute(cls, images) -> io.NodeOutput:
        return _save(cls, comfy_nodes.SaveImage(), images)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return _save_fingerprint()


class CouchyPreviewImage(io.ComfyNode):
    DISPLAY = "Couchy Preview Image"  # read by the node's own methods, which can't ask for the schema

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="CouchyPreviewImage",
            display_name=cls.DISPLAY,
            category=CATEGORY,
            description="Previews the images, keeping the files in this run's temp folder.",
            inputs=[io.Image.Input("images")],
            outputs=[io.Image.Output("images")],
            hidden=[io.Hidden.prompt, io.Hidden.extra_pnginfo],
            is_output_node=True,
        )

    @classmethod
    def execute(cls, images) -> io.NodeOutput:
        return _save(cls, comfy_nodes.PreviewImage(), images)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return _save_fingerprint()


NODES = [CouchyLoadImage, CouchyLoadMask, CouchyLoadVideo, CouchySaveImage, CouchyPreviewImage]


class CouchyExtension(ComfyExtension):
    """Lists the nodes only on a paired server. paired() is asked at load;
    a server paired later gets them from register() instead."""

    def __init__(self, paired):
        self.paired = paired

    async def get_node_list(self):
        return list(NODES) if self.paired() else []


def register(relative_module):
    """Add the nodes to a running ComfyUI, as its loader would: for a server
    paired after start-up."""
    for node in NODES:
        schema = node.GET_SCHEMA()
        if schema.node_id in comfy_nodes.NODE_CLASS_MAPPINGS:
            continue
        comfy_nodes.NODE_CLASS_MAPPINGS[schema.node_id] = node
        node.RELATIVE_PYTHON_MODULE = relative_module
        comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS[schema.node_id] = schema.display_name
