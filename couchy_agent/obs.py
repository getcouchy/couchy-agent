"""Structured log lines, as the observability spec (couchy-design
features/observability, the couchy-agent line under #870) sets them: one JSON
object per line on ComfyUI's console, with service=couchy-agent, version and
server on every line, and the same denied keys as the API, dropped at any
depth.

Used by the models lines (logger couchy_agent.models). They name a file by
its location, folder and the first 12 hex characters of its SHA-256, with
sizes and durations, and never by its path.
"""

import datetime
import json
import logging
import socket

from . import __version__

SERVICE = "couchy-agent"

DENIED = frozenset((
    "token", "authorization", "cookie", "code", "password", "secret", "prompt", "negative", "values",
    "email", "ip", "path", "filename",
))
DROPPED = "[dropped]"

# Attributes every LogRecord has; anything else came in through extra.
_STANDARD = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


def scrub(value, _depth=0):
    """value with every denied key's value replaced, at any depth."""
    if _depth > 16:
        return DROPPED
    if isinstance(value, dict):
        return {k: (DROPPED if str(k).lower() in DENIED else scrub(v, _depth + 1)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, _depth + 1) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def __init__(self, server=None, version=__version__):
        super().__init__()
        self.server = server or socket.gethostname()
        self.version = version

    def format(self, record):
        line = {
            "time": datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "msg": record.getMessage(),
            "service": SERVICE,
            "version": self.version,
            "server": self.server,
        }
        fields = {k: v for k, v in vars(record).items() if k not in _STANDARD and not k.startswith("_")}
        for k, v in scrub(fields).items():
            if k not in line:
                line[k] = v
        if record.exc_info:
            line["error"] = record.exc_info[1].__class__.__name__
        return json.dumps(line, default=str)


def models_logger(server=None):
    """couchy_agent.models, writing JSON lines to the console once, and not
    also through ComfyUI's own plain format."""
    log = logging.getLogger("couchy_agent.models")
    if not any(isinstance(h.formatter, JsonFormatter) for h in log.handlers):
        h = logging.StreamHandler()
        h.setFormatter(JsonFormatter(server))
        log.addHandler(h)
        log.propagate = False
        if log.level == logging.NOTSET:
            log.setLevel(logging.INFO)
    return log


def sha12(sha256):
    return (sha256 or "")[:12] or None
