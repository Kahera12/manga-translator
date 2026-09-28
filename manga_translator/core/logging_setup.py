"""Logging setup: rotating file log + secret masking. No tracebacks to the user."""
from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key['\"]?\s*[:=]\s*)([^\s,&'\"]+)"),
    re.compile(r"(?i)(key=)([A-Za-z0-9_\-]{6,})"),
    re.compile(r"(AIza[0-9A-Za-z_\-]{20,})"),
]

_CONFIGURED = False


def _mask(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + "***redacted***", text)
        else:
            text = pattern.sub("***redacted***", text)
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        red = _mask(msg)
        if red != msg:
            record.msg = red
            record.args = ()
        return True


def setup_logging(log_file: Path, debug: bool = False, console: bool = True) -> logging.Logger:
    """Configure the root logger once; safe to call repeatedly."""
    global _CONFIGURED
    root = logging.getLogger()
    level = logging.DEBUG if debug else logging.INFO
    root.setLevel(level)
    if _CONFIGURED:
        for handler in root.handlers:
            handler.setLevel(logging.DEBUG if debug else logging.INFO)
        return root

    root.addFilter(_RedactFilter())
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-28s %(message)s", datefmt="%H:%M:%S"
    )
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG if debug else logging.INFO)
        root.addHandler(fh)
    except OSError:
        pass

    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.setLevel(logging.DEBUG if debug else logging.WARNING)
        root.addHandler(sh)

    # Third-party chatter that is never actionable for the user.
    for noisy in ("onnxruntime", "PIL", "asyncio", "httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"mt.{name}")
