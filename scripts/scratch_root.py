"""One scratch root per tool run, reclaimed even when a run does not finish.

Every tool here builds a throwaway tree under the system temp directory: a
synthetic vault, an isolated home, an installed wheel. Each is hundreds of
megabytes, and each removed itself with cleanup errors suppressed --
`shutil.rmtree(..., ignore_errors=True)` or
`TemporaryDirectory(ignore_cleanup_errors=True)`. Suppression was chosen for a
real reason: on Windows a SQLite or child-process handle can outlive the
shutdown that released it, and a capture that already succeeded must not fail
on a slow unlink. But suppression answers "do not raise" by never retrying and
never saying anything, so the tree simply stays -- and the next run makes
another one.

That is not hypothetical. A developer laptop reached 58 GB of temp with no
single large item: hundreds of directories from these tools, spread thin
enough that ordinary disk-usage inspection looks past them (#579).

Two changes close it, and both are needed:

* Retry the removal against a deadline instead of giving up on the first
  error, and if it still fails, *say so* naming the path. A transient handle
  loses to a few hundred milliseconds of patience; a real leak stops being
  silent.
* Sweep this tool's own stale roots on the way in. Retrying cannot help a run
  that was killed -- Ctrl-C, a cancelled CI job -- and that is precisely the
  case that accumulates. The next run reclaims what the last one could not.

And when a removal genuinely cannot finish, take everything that will go
rather than abandoning the tree at the first refusal. Some holders are
permanent by design -- `writer_lease` keeps its owner lock open for the life
of the process so a liveness probe can see it, which means a tool that builds
a lease directory can never remove that directory before exiting. The choice
there is between stranding a few hundred bytes and stranding the vault beside
it.

The sweep is deliberately narrow: same prefix, directories only, directly
inside the temp directory, and older than a threshold no live run can reach.
Anything it cannot remove it leaves alone.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: How long to keep retrying one removal. Sized for a handle that is already
#: being released, not for a process that still holds the tree open: on
#: Windows the observed window after lifecycle shutdown is well under a
#: second, and a tool that waits longer than this is waiting on something a
#: retry will never win.
REMOVE_DEADLINE_SECONDS = 5.0

#: First backoff step; doubles up to the deadline.
_FIRST_DELAY_SECONDS = 0.05

#: A root older than this cannot belong to a live run -- these tools take
#: minutes, not hours -- so it is residue from one that was killed. Generous
#: on purpose: sweeping is a convenience, and never removing a concurrent
#: run's tree matters far more than reclaiming its bytes promptly.
STALE_AGE_SECONDS = 6 * 60 * 60


def remove_scratch_tree(path: Path, *, deadline: float = REMOVE_DEADLINE_SECONDS) -> bool:
    """Remove *path*, retrying a transient failure; report rather than raise.

    Returns whether the tree is gone. A caller that already produced its
    output must not fail on cleanup, so this never raises -- but it also
    never stays quiet about a tree it could not remove, which is the whole
    difference from `ignore_errors=True`.
    """
    if not path.exists():
        return True
    delay = _FIRST_DELAY_SECONDS
    give_up_at = time.monotonic() + deadline
    while True:
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError as error:
            if time.monotonic() >= give_up_at:
                # `rmtree` stops at the first entry it cannot remove, so one
                # held handle strands the whole tree -- and the tree is the
                # hundreds of megabytes, while the handle is on a lock file of
                # a few hundred bytes. Some of these holders are permanent by
                # design: `writer_lease` keeps its owner lock open for the
                # life of the process precisely so a liveness probe can see
                # it, so a tool that builds a lease directory can never remove
                # that directory before exiting. Take everything else.
                survivors = _remove_what_we_can(path)
                if not survivors:
                    return True
                print(
                    f"scratch: could not fully remove {path}: {error}. "
                    f"{len(survivors)} item(s) left, first {survivors[0]}. "
                    "A later run of this tool will sweep it.",
                    flush=True,
                )
                return False
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


def _remove_what_we_can(path: Path) -> list[Path]:
    """Delete every entry under *path* that will go; return the ones that stayed.

    Bottom-up and per-entry, because `shutil.rmtree` abandons the whole walk
    at its first failure. Written against `os.walk` rather than `rmtree`'s
    error hook so it reads the same on 3.11 and 3.13 -- the hook was renamed
    `onerror` to `onexc` in between.
    """
    survivors: list[Path] = []
    for current, directory_names, file_names in os.walk(path, topdown=False):
        here = Path(current)
        for name in file_names:
            try:
                (here / name).unlink()
            except OSError:
                survivors.append(here / name)
        for name in directory_names:
            try:
                (here / name).rmdir()
            except OSError:
                survivors.append(here / name)
    try:
        path.rmdir()
    except OSError:
        survivors.append(path)
    return survivors


def sweep_stale_scratch_roots(
    prefix: str, *, older_than: float = STALE_AGE_SECONDS
) -> list[Path]:
    """Remove this tool's abandoned roots from a previous, killed run.

    Scoped to directories named with *prefix* directly inside the system temp
    directory, and only those whose mtime is older than *older_than* -- a live
    run's root can never be. Whatever cannot be removed (another user's tree,
    a handle still held) is left where it is.
    """
    root = Path(tempfile.gettempdir())
    cutoff = time.time() - older_than
    swept: list[Path] = []
    try:
        candidates = sorted(root.glob(f"{prefix}*"))
    except OSError:
        return swept
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if not os.path.isdir(candidate) or candidate.is_symlink():
            continue
        if info.st_mtime > cutoff:
            continue
        if remove_scratch_tree(candidate, deadline=0.5):
            swept.append(candidate)
    return swept


@contextmanager
def scratch_root(prefix: str, *, keep: bool = False) -> Iterator[Path]:
    """A fresh scratch root for one run, swept in and removed on the way out.

    `keep=True` retains it for a post-mortem and says where it is, so a
    retained tree is a decision someone can see rather than a leak.
    """
    swept = sweep_stale_scratch_roots(prefix)
    if swept:
        print(
            f"scratch: reclaimed {len(swept)} abandoned root(s) from earlier runs",
            flush=True,
        )
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        if keep:
            print(f"scratch: kept working directory {path}", flush=True)
        else:
            remove_scratch_tree(path)
