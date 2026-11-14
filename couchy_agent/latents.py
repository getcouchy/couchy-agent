"""The .latent file Couchy Save Latent writes and Couchy Load Latent reads
(nodes.py). Needs torch and safetensors, which ComfyUI installs; imported
only when a latent node runs.

It is safetensors, and a superset of stock SaveLatent's file:

    latent_tensor             the samples, when they are one tensor (stock's key)
    latent_format_version_0   an empty tensor, as stock writes (stock's key)
    latent_tensor.<i>         member i of nested samples (a NestedTensor, such
                              as LTX's joint video and audio latent), i = 0, 1 ...
    noise_mask                the latent's noise mask, when it has one
    noise_mask.<i>            member i of a nested noise mask

The safetensors metadata is stock's: "prompt" and one key per extra_pnginfo
entry, each JSON, or none when the run asks for no metadata.

A latent with one tensor and no mask is written with exactly stock's keys and
metadata, so stock LoadLatent opens a Couchy file and Couchy Load Latent opens
a stock one (applying stock's scale to a file older than
latent_format_version_0). Stock LoadLatent can't open a nested latent: it has
no latent_tensor key.
"""

import json

SAMPLES = "latent_tensor"
VERSION = "latent_format_version_0"
MASK = "noise_mask"
OLD_SCALE = 1.0 / 0.18215  # stock LoadLatent's multiplier for files without VERSION


def _members(value):
    """The tensors of a NestedTensor, or None for a plain tensor."""
    return list(value.tensors) if getattr(value, "is_nested", False) else None


def _put(out, key, value):
    members = _members(value)
    if members is None:
        out[key] = value.contiguous()
        return
    if not members:
        raise ValueError(f"The latent's {key} is nested but empty.")
    for i, t in enumerate(members):
        out[f"{key}.{i}"] = t.contiguous()


def _take(tensors, key, nest):
    """A plain tensor at key, or the members key.0, key.1 ... nested; None
    when neither is there."""
    if key in tensors:
        return tensors[key]
    members = []
    while f"{key}.{len(members)}" in tensors:
        members.append(tensors[f"{key}.{len(members)}"])
    return nest(members) if members else None


def tensors(latent):
    """The dict of tensors to save for a LATENT ({"samples", "noise_mask"?})."""
    import torch
    out = {}
    _put(out, SAMPLES, latent["samples"])
    out[VERSION] = torch.tensor([])
    if latent.get(MASK) is not None:
        _put(out, MASK, latent[MASK])
    return out


def metadata(prompt, extra_pnginfo):
    """Stock SaveLatent's metadata."""
    meta = {"prompt": json.dumps(prompt) if prompt is not None else ""}
    for k in extra_pnginfo or {}:
        meta[k] = json.dumps(extra_pnginfo[k])
    return meta


def save(path, latent, meta=None):
    import safetensors.torch
    if meta is None:
        safetensors.torch.save_file(tensors(latent), path)
    else:
        safetensors.torch.save_file(tensors(latent), path, metadata=meta)


def load(path, nest):
    """The LATENT in a .latent file. nest builds a nested tensor from a list
    of members (comfy.nested_tensor.NestedTensor)."""
    import safetensors.torch
    stored = safetensors.torch.load_file(path, device="cpu")
    samples = _take(stored, SAMPLES, nest)
    if samples is None:
        raise ValueError("This file isn't a latent.")
    if SAMPLES in stored:
        samples = samples.float() * (1.0 if VERSION in stored else OLD_SCALE)
    else:
        samples = nest([t.float() for t in samples.tensors])
    out = {"samples": samples}
    mask = _take(stored, MASK, nest)
    if mask is not None:
        out[MASK] = mask
    return out
