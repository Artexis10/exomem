"""What the benchmark harnesses require of the machine underneath them.

`benchmarks/memorybench` and `benchmarks/epistemic` are deliberately Linux-only.
They are not product code and were never meant to be portable: their contracts
are written *in terms of* Linux facilities, because reproducible benchmark
evidence is the whole point of them.

* `memorybench.export._secure_read` proves a private file is unreadable by
  anyone else using POSIX mode bits and `st_uid`.
* `memorybench` pins Bun to an exact version so a run is byte-reproducible.
* `epistemic.broker` bounds provider work with `setitimer`/`SIGALRM` and isolates
  it with `bwrap`, which is Linux user namespaces and exists nowhere else.

`ci.yml` provisions all three; every job in it is `ubuntu-latest`.
`cross-platform.yml` cannot, so it collected ~135 failures that say nothing about
the code -- on Windows `Path.chmod(0o600)` only toggles the read-only bit and the
file still reports `0o666`, so the very first `_secure_read` of a test's own plan
raises before the test reaches its subject.

A missing prerequisite is a skip, not a failure. Gate on the *capability* rather
than on `sys.platform`, so a Linux box without Bun installed also reports the
truth, and so the reason names what is actually absent.

The gates go on the shared helpers that need the capability, not on whole
modules: a good share of these tests are pure logic -- digest vectors, plan
validation, canonical selection -- and those keep running everywhere, which is
the cross-platform signal worth having.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from functools import lru_cache

import pytest

#: The Bun release `memorybench` pins. Kept here rather than imported so a skip
#: decision never depends on importing the harness it is gating.
REQUIRED_BUN_VERSION = "1.3.14"

_TIMER_PRIMITIVES = ("SIGALRM", "ITIMER_REAL", "getitimer", "setitimer")


@lru_cache(maxsize=1)
def has_posix_file_modes() -> bool:
    """True where `st_mode` and `st_uid` are an access-control fact.

    Windows synthesizes both. A directory reports `0o777` and a file `0o666`
    whatever `chmod` was asked for, so a mode check there does not read a
    permission -- it reads a placeholder, and rejects it.
    """
    return os.name == "posix" and hasattr(os, "getuid")


@lru_cache(maxsize=1)
def has_posix_executable_scripts() -> bool:
    """True where a `#!/bin/sh` file marked 0755 is a runnable program.

    Several fixtures stand in for a toolchain by writing exactly that. Windows
    has no shebang: a file named `bun` with no extension is data, and the
    executable bit `chmod` was asked for was never set either.
    """
    return os.name == "posix"


@lru_cache(maxsize=1)
def has_pinned_bun() -> bool:
    """True when the exact pinned Bun is on PATH; version drift is not enough."""
    executable = shutil.which("bun")
    if executable is None:
        return False
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and completed.stdout.strip() == REQUIRED_BUN_VERSION


@lru_cache(maxsize=1)
def has_posix_interval_timers() -> bool:
    """True where a process can own a real-time timer, as the broker requires."""
    return all(hasattr(signal, name) for name in _TIMER_PRIMITIVES)


@lru_cache(maxsize=1)
def has_bwrap() -> bool:
    """True when a bubblewrap sandbox can be spawned at all."""
    return shutil.which("bwrap") is not None


def require_posix_file_modes() -> None:
    if not has_posix_file_modes():
        pytest.skip("POSIX file mode and ownership bits are not an access-control fact here")


def require_posix_executable_scripts() -> None:
    if not has_posix_executable_scripts():
        pytest.skip("a `#!/bin/sh` fixture is not an executable program here")


def require_pinned_bun() -> None:
    if not has_pinned_bun():
        pytest.skip(f"the pinned Bun {REQUIRED_BUN_VERSION} toolchain is not installed")


def require_posix_interval_timers() -> None:
    if not has_posix_interval_timers():
        pytest.skip("POSIX interval timers (setitimer/SIGALRM) are unavailable here")


def require_bwrap() -> None:
    if not has_bwrap():
        pytest.skip("the bubblewrap sandbox is unavailable here")


#: The broker's own words for the capability it needs. It says this deliberately
#: -- `_require_surface_timer_ownership` exists to refuse provider work up front
#: rather than let a surface run unbounded -- so matching the sentence matches a
#: declared contract, not an incidental traceback.
_BROKER_TIMER_REFUSAL = re.compile(
    r"POSIX provider surface (deadline primitives are unavailable"
    r"|timer state is unavailable)"
    r"|provider surfaces require POSIX timer ownership"
)


def declares_absent_surface_timers(error: BaseException | None) -> bool:
    """True when *error* is the broker refusing to run without POSIX timers.

    Walks the chain because `pytest.raises(..., match=...)` re-raises as an
    `AssertionError` holding the refusal as `__context__`: a test that expected
    one contract error and met this one failed for the same absent capability,
    not for its own reason.
    """
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if type(error).__name__ in {"BrokerContractError", "BrokerSurfaceTimeout"}:
            if _BROKER_TIMER_REFUSAL.search(str(error)):
                return True
        if isinstance(error, AssertionError) and _BROKER_TIMER_REFUSAL.search(str(error)):
            return True
        error = error.__cause__ or error.__context__
    return False
