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
from pathlib import Path

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
def has_posix_host_paths() -> bool:
    """True where a POSIX path is a valid *host* path.

    Distinct from the guest paths a benchmark run uses inside its sandbox.
    `protocol/models.py` validates several fields as absolute host paths with
    `os.path`, correctly -- `run_export` opens them on the machine it runs on.
    The committed schema-conformance fixtures fill those fields with `/owned/...`,
    which is a real host path on Linux and not one on Windows, so it is the
    fixture that is platform-bound rather than the validator.
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


#: What `epistemic.broker` pins its sandbox to. Not merely "Linux": these are
#: Debian/Ubuntu x86-64 paths, hardcoded, and the broker refuses rather than
#: substitute -- it verifies `bwrap` *is* the trusted binary, then binds this
#: exact runtime into the namespace. Duplicated here rather than imported so a
#: skip decision never depends on importing the harness it is gating.
_SANDBOX_RUNTIME = (
    "/usr/bin/bwrap",
    "/usr/bin/python3.12",
    "/usr/lib/python3.12",
    "/lib/x86_64-linux-gnu",
    "/lib64/ld-linux-x86-64.so.2",
)


@lru_cache(maxsize=1)
def has_bwrap_sandbox() -> bool:
    """True when the exact sandbox the broker binds is present.

    `shutil.which("bwrap")` alone is not enough: the broker resolves it against
    `/usr/bin/bwrap` specifically and binds a pinned interpreter, stdlib, libc
    and loader beside it. Any one of them missing is a refusal, so all of them
    are the capability.
    """
    return all(Path(path).exists() for path in _SANDBOX_RUNTIME)


@lru_cache(maxsize=1)
def has_trusted_system_git() -> bool:
    """True where the harness's trust anchor can actually find Git.

    `benchmarks/protocol` deliberately resolves Git through `os.defpath` rather
    than `PATH`, so a user-controlled `PATH` cannot substitute the binary that
    establishes contract identity. That anchor is a POSIX idea: `os.defpath` is
    `:/bin:/usr/bin` there, while on Windows it names a drive-root `bin`
    directory that does not exist, preceded by the current directory -- which
    is the very thing the anchor exists to exclude. So the check finds nothing
    on Windows even with Git installed in the usual Program Files location.

    Giving Windows its own trusted location would mean inventing a trust policy
    for a harness that also requires `bwrap`, and therefore cannot run there
    regardless. Gate on the anchor working instead.
    """
    return shutil.which("git", path=os.defpath) is not None


def require_posix_file_modes() -> None:
    if not has_posix_file_modes():
        pytest.skip("POSIX file mode and ownership bits are not an access-control fact here")


def require_posix_executable_scripts() -> None:
    if not has_posix_executable_scripts():
        pytest.skip("a `#!/bin/sh` fixture is not an executable program here")


def require_trusted_system_git() -> None:
    if not has_trusted_system_git():
        pytest.skip("no trusted system Git on os.defpath (the harness's trust anchor)")


def require_posix_host_paths() -> None:
    if not has_posix_host_paths():
        pytest.skip("the committed fixture's POSIX host paths are not host paths here")


def require_pinned_bun() -> None:
    if not has_pinned_bun():
        pytest.skip(f"the pinned Bun {REQUIRED_BUN_VERSION} toolchain is not installed")


def require_posix_interval_timers() -> None:
    if not has_posix_interval_timers():
        pytest.skip("POSIX interval timers (setitimer/SIGALRM) are unavailable here")


def require_bwrap_sandbox() -> None:
    if not has_bwrap_sandbox():
        pytest.skip("the pinned bubblewrap sandbox runtime is unavailable here")


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


#: The broker's five distinct refusals for a sandbox it cannot build -- an
#: absent `bwrap`, one that is not the trusted binary, one it cannot stat or
#: that is group/world writable, and a pinned system runtime that is not there.
#: Each is a deliberate fail-closed declaration, which is what makes matching
#: the sentence safe.
_BROKER_SANDBOX_REFUSAL = re.compile(
    r"bwrap sandbox (is unavailable|executable (cannot be verified"
    r"|is not the trusted system binary|is group/world writable))"
    r"|sandbox system runtime or bound worker bytes are unavailable"
)


def declares_absent_sandbox(error: BaseException | None) -> bool:
    """True when *error* is the broker refusing to build its pinned sandbox.

    Walks the chain for the same reason `declares_absent_surface_timers` does:
    `pytest.raises(..., match=...)` re-raises as an `AssertionError` carrying
    the refusal, and a test that met this refusal instead of its own failed for
    the absent capability rather than for its subject.
    """
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if type(error).__name__ in {"BrokerContractError", "BrokerSurfaceTimeout"}:
            if _BROKER_SANDBOX_REFUSAL.search(str(error)):
                return True
        if isinstance(error, AssertionError) and _BROKER_SANDBOX_REFUSAL.search(str(error)):
            return True
        error = error.__cause__ or error.__context__
    return False


#: The contract harness's refusals for a Git it will not trust. Only the three
#: that mean "the trust anchor found nothing usable" -- a digest mismatch or a
#: malformed revision is a real finding and must stay a failure.
_CONTRACT_GIT_REFUSAL = re.compile(
    r"trusted Git executable (is unavailable|cannot be resolved"
    r"|is not an executable file)"
)


def declares_absent_trusted_git(error: BaseException | None) -> bool:
    """True when *error* is the contract harness failing to anchor on Git."""
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if type(error).__name__ in {"ContractIdentityError", "ManifestError"}:
            if _CONTRACT_GIT_REFUSAL.search(str(error)):
                return True
        if isinstance(error, AssertionError) and _CONTRACT_GIT_REFUSAL.search(str(error)):
            return True
        error = error.__cause__ or error.__context__
    return False
