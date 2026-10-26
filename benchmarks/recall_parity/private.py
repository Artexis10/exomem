"""Where a run may write, and how: owner-only, immutable, never a tracked path."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import IO, Any

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The two directories under the checkout that ``.gitignore`` already excludes.
_IGNORED_UNDER_REPO = (REPO_ROOT / "benchmarks" / "private", REPO_ROOT / "benchmarks" / "runs")


class PrivateDirError(ValueError):
    """The chosen directory could end up in a commit."""


def private_root(path: str | Path) -> Path:
    """Resolve the private directory, refusing a tracked place inside the checkout.

    Real queries must never reach a commit. A directory outside the checkout is
    safe by construction; one inside it is safe only where ``.gitignore``
    already excludes it. Wrongly firing costs the operator one different path.
    """

    root = Path(path).expanduser().resolve()
    inside = root == REPO_ROOT or root.is_relative_to(REPO_ROOT)
    if inside and not any(root.is_relative_to(ignored) for ignored in _IGNORED_UNDER_REPO):
        raise PrivateDirError(
            "the private directory must be outside the repository or under "
            "benchmarks/private/ or benchmarks/runs/ (both git-ignored)"
        )
    return root


def open_new(path: Path, *, overwrite: bool = False) -> IO[str]:
    """Create ``path`` owner-only. Artifacts are immutable unless ``overwrite`` is set."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    return os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8", newline="\n")


def write_json(path: Path, value: Any, *, overwrite: bool = False) -> None:
    with open_new(path, overwrite=overwrite) as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with open_new(path) as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
