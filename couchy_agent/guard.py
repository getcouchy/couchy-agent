"""What the guard on ComfyUI's own routes decides. Plain Python: the caller
reads the request and passes the folder root it names.

- GET /view of a file under couchy/<ns>/ needs a key for <ns>; ro is enough.
- POST /upload/image (and /upload/mask) into couchy/<ns>/ needs an rw key
  for <ns>.
- POST /prompt is refused when any string in the graph names
  couchy/<ns>/ for a namespace the caller holds no key for.
- Anything outside couchy/ is never touched, and costs one substring check.
"""

import os
import posixpath
import re

from .files import TOP, namespace_of

# couchy/<ns> anywhere in a string, as a path segment: not part of a longer
# folder name such as mycouchy/. Namespace ids always start ns_. Case is
# ignored, since a macOS folder answers to any case.
_NAMED = re.compile(r"(?<![A-Za-z0-9_.-])" + TOP + r"/+(ns_[A-Za-z0-9]+)", re.IGNORECASE)


def target_namespace(root, subfolder):
    """The namespace root/subfolder lands in (lowercased), "" for couchy/
    itself, or None outside couchy/. Paths without "couchy" in them are
    answered without touching the disk."""
    if not isinstance(subfolder, str) or TOP not in subfolder.lower():
        return None
    real = os.path.realpath(os.path.join(root, subfolder))
    return namespace_of(root, real)


def allows_read(grant, namespace):
    return grant is not None and namespace and grant["namespace"] == namespace


def allows_write(grant, namespace):
    return allows_read(grant, namespace) and grant["mode"] == "rw"


TOO_DEEP = "(nested too deep to check)"


def named_namespaces(value, _depth=0):
    """Every namespace a JSON value's strings name, lowercased."""
    found = set()
    if _depth > 64:
        # Too deep to check: treat it as naming a namespace no one holds.
        return {TOO_DEEP}
    if isinstance(value, str):
        if TOP in value.lower():
            text = value.replace("\\", "/")
            for s in (text, posixpath.normpath(text)):
                found.update(m.lower() for m in _NAMED.findall(s))
    elif isinstance(value, dict):
        for k, v in value.items():
            found |= named_namespaces(k, _depth + 1)
            found |= named_namespaces(v, _depth + 1)
    elif isinstance(value, list):
        for v in value:
            found |= named_namespaces(v, _depth + 1)
    return found


def prompt_refusal(prompt, grant):
    """The namespaces a graph names that the caller can't reach; empty when
    the graph may run."""
    mine = grant["namespace"] if grant else None
    return sorted(ns for ns in named_namespaces(prompt) if ns != mine)
