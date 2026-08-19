"""What a built-from-scratch environment must still carry to start a process.

A benchmark subprocess is handed an environment assembled from nothing, so a run
is reproducible and cannot read the developer's state. On Windows that ambition
overshoots: a few variables name the OS install rather than the user, and
without them the interpreter cannot start the program at all -- the run fails
before any exomem code executes, which reads as a product failure and is not
one.
"""

from __future__ import annotations

import os


def apply_os_requirements(env: dict[str, str], home: os.PathLike[str] | str) -> dict[str, str]:
    """Add what Windows needs to run a Python child, in place; return *env*.

    `SystemRoot`: Windows resolves Winsock through it, and `exomem.__main__`
    imports `asyncio`, whose Windows event loop pulls that in at import time.
    Omitting it does not merely lose a convenience -- the interpreter exits with
    `OSError [WinError 10106] The requested service provider could not be loaded
    or initialized` before reaching `main`.

    `USERPROFILE`, and `HOMEDRIVE` + `HOMEPATH` as its fallback: `HOME` alone
    redirects the home directory on POSIX only. Windows resolves `Path.home()`
    through these, and `install_hook` computes a default hook directory from
    `Path.home()` at import scope, so without them the child dies with "Could
    not determine home directory" rather than merely leaking state.

    Isolation is unaffected. `SystemRoot` names the OS install, not the user,
    and the home variables point at the same redirected *home* the caller chose.
    """
    if os.name != "nt":
        return env
    system_root = os.environ.get("SystemRoot")
    if system_root:
        env["SystemRoot"] = system_root
    env["USERPROFILE"] = str(home)
    drive, tail = os.path.splitdrive(str(home))
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail
    return env
