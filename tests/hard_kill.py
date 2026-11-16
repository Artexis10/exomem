"""A test child that a crash ends with no cleanup, on every platform."""

from __future__ import annotations

import os
import signal

_SIGKILL = getattr(signal, "SIGKILL", None)

#: What a parent reads as the killed child's exit code: -SIGKILL where signals exist;
#: Windows has none, so the child exits with this code and the parent reads it as it is.
KILLED = 137 if _SIGKILL is None else -_SIGKILL


def kill_self() -> None:
    """End this process at once: no finalizer, no handler and no flush; the system closes its files."""
    if _SIGKILL is not None:
        os.kill(os.getpid(), _SIGKILL)
    os._exit(KILLED)
