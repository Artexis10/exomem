"""Where the outcome suites keep their data, and which upstream commits they run against.

One definition of each: the run root (``EXOMEM_OUTCOME_ROOT``) and the pinned upstream
checkouts (``LOCKFILE.json``). Every suite module reads them from here.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RUN_ROOT = "/var/tmp/exomem-outcome-evals"
RUN_ROOT = Path(os.environ.get("EXOMEM_OUTCOME_ROOT", DEFAULT_RUN_ROOT))
LOCKFILE = Path(__file__).with_name("LOCKFILE.json")


@dataclass(frozen=True)
class Pin:
    """One upstream project, at the commit the suites were built against."""

    key: str
    directory: str
    repository: str
    commit: str
    license: str

    def checkout(self, root: Path = RUN_ROOT) -> Path:
        return root / "upstream" / self.directory


def pins() -> dict[str, Pin]:
    return {key: Pin(key=key, **entry) for key, entry in json.loads(LOCKFILE.read_text()).items()}


def pin(key: str) -> Pin:
    return pins()[key]


def sha256_of(path: Path) -> str:
    """The SHA-256 of a file; a database of hundreds of megabytes is read in blocks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def head_of(checkout: Path) -> str | None:
    """The commit a checkout is at, or None when ``checkout`` is not a Git checkout."""
    done = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return done.stdout.strip() if done.returncode == 0 else None


def edited_files(checkout: Path) -> list[str]:
    """The tracked files of a Git checkout that differ from its commit (untracked files do not count)."""
    return subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()


def edited_fix(checkout: Path, edited: list[str]) -> str:
    return (
        f"{checkout} has {len(edited)} edited tracked file(s), so it is not the pinned code. "
        f"Run `git -C {checkout} checkout -- .`, then `membench outcome fetch`."
    )


def wrong_commit_fix(upstream: Pin, checkout: Path, head: str) -> str:
    return (
        f"{checkout} is at {head}, not the pinned {upstream.commit}. "
        f"Run `git -C {checkout} checkout --detach {upstream.commit}` "
        "(or move the directory aside), then `membench outcome fetch`."
    )


def require_pinned_checkout(key: str, root: Path = RUN_ROOT, *, unedited: bool = False) -> Path:
    """The upstream checkout, refused unless it sits at its pinned commit.

    ``unedited`` also refuses local changes to tracked files: a decision run must play the pinned code.
    """
    upstream = pin(key)
    checkout = upstream.checkout(root)
    head = head_of(checkout)
    if head is None:
        raise SystemExit(f"no {key} checkout at {checkout}. Run `membench outcome fetch`.")
    if head != upstream.commit:
        raise SystemExit(wrong_commit_fix(upstream, checkout, head))
    if unedited and (edited := edited_files(checkout)):
        raise SystemExit(edited_fix(checkout, edited))
    return checkout
