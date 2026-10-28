"""Where a run may write, and how: owner-only, immutable, never a tracked path."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import IO, Any

#: Variables that point git at a repository other than the one around the directory asked about.
_GIT_REPOSITORY_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
)


class PrivateDirError(ValueError):
    """The chosen directory could end up in a commit."""


def _git(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REPOSITORY_VARIABLES}
    # English diagnostics for the one message read below, and a repository discovery
    # that crosses a mount point, so a mount inside a work tree still counts as inside it.
    env.update(LC_ALL="C", GIT_DISCOVERY_ACROSS_FILESYSTEM="1")
    try:
        return subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    except OSError:
        raise PrivateDirError(
            "git is needed to check the private directory and did not run"
        ) from None


def _inside_work_tree(directory: Path) -> bool:
    result = _git(directory, "rev-parse", "--is-inside-work-tree")
    if result.returncode == 0:
        return result.stdout.strip() == "true"
    # Git's fixed diagnostic for "no repository here". Any other failure (for example a
    # repository owned by someone else) leaves the answer unknown, and unknown is refused.
    if "not a git repository" in result.stderr:
        return False
    raise PrivateDirError("git could not tell whether the private directory is inside a work tree")


def private_root(path: str | Path) -> Path:
    """Resolve the private directory, refusing a place a commit could reach.

    Real queries must never reach a commit. A directory in no git work tree is
    safe by construction. One inside any work tree, this checkout or another, is
    safe only where git itself reports it ignored, so the rules stay in the
    ``.gitignore`` files and are never copied here. Wrongly firing costs the
    operator one different path.
    """

    root = Path(path).expanduser().resolve()
    # The directory may not exist yet; its nearest existing ancestor sits in the same work tree.
    anchor = root
    while not anchor.exists():
        anchor = anchor.parent
    if not anchor.is_dir():
        anchor = anchor.parent
    if _inside_work_tree(anchor):
        # A trailing separator marks the target as a directory, so a rule such as
        # "private/" matches a directory that does not exist yet.
        if _git(anchor, "check-ignore", "-q", f"{root}{os.sep}").returncode != 0:
            raise PrivateDirError(
                "the private directory is inside a git work tree and is not git-ignored; "
                "choose a directory outside every repository or one that .gitignore excludes"
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
