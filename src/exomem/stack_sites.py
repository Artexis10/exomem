"""Content-free observation of call sites, for refusals and diagnostics.

A site is a module, a function and a line, never a path, a local or source text.
"""

from __future__ import annotations

import re
from types import FrameType


def package_frames(
    frame: FrameType | None, *, limit: int = 64, module_prefix: str = "exomem."
) -> tuple[list[dict[str, object]], bool]:
    """Bounded frames from ``frame`` outward, as ``({module, function, line} ..., truncated)``.

    Only modules under ``module_prefix`` are named, so by default only shipped package
    symbols leave this process."""
    frames: list[dict[str, object]] = []
    try:
        for _ in range(limit):
            if frame is None:
                break
            module = frame.f_globals.get("__name__")
            if isinstance(module, str) and module.startswith(module_prefix):
                frames.append({"module": module, "function": frame.f_code.co_name,
                               "line": frame.f_lineno})
            frame = frame.f_back
        return frames, frame is not None
    finally:
        del frame


def log_label(value: object, *, limit: int = 64) -> str:
    """A log label of a fixed shape, so no path or sentence can ride in it.

    The regex checks the character shape of a label, not what it means."""
    return re.sub(r"[^A-Za-z0-9._:-]", "_", str(value))[:limit] or "unknown"


def caller_site(frame: FrameType | None, *, skip_modules: frozenset[str], limit: int = 16) -> str:
    """The nearest caller outside ``skip_modules``, as a labelled ``module:function:line``."""
    try:
        for _ in range(limit):
            if frame is None:
                break
            module = frame.f_globals.get("__name__")
            if module not in skip_modules:
                return log_label(f"{module}:{frame.f_code.co_name}:{frame.f_lineno}", limit=96)
            frame = frame.f_back
        return "unknown"
    finally:
        del frame
