"""Vault path resolution + safe-write helpers used by the add tool."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from slugify import slugify as _slugify


DESKTOP_VAULT = Path(r"D:\Archive\Personal Archive\50 Notes\Obsidian")
LAPTOP_VAULT = Path(r"C:\Users\win-laptop\Documents\Obsidian")

SLUG_MAX_LENGTH = 60


def resolve_vault(env_var: str = "KB_MCP_VAULT_PATH") -> Path:
    """Return the Obsidian vault root that contains Knowledge Base/.

    Priority: env override → desktop → laptop. Raises if none resolve.
    """
    override = os.environ.get(env_var)
    if override:
        path = Path(override)
        if not _is_vault(path):
            raise RuntimeError(
                f"{env_var}={override!r} does not look like a vault "
                f"(no Knowledge Base/_Schema/SKILL.md found)"
            )
        return path
    for candidate in (DESKTOP_VAULT, LAPTOP_VAULT):
        if _is_vault(candidate):
            return candidate
    raise RuntimeError(
        "Knowledge Base vault not found at the desktop or laptop path. "
        f"Set {env_var} to override."
    )


def _is_vault(path: Path) -> bool:
    return (path / "Knowledge Base" / "_Schema" / "SKILL.md").exists()


def kb_root(vault: Path) -> Path:
    return vault / "Knowledge Base"


def slugify_title(title: str, max_length: int = SLUG_MAX_LENGTH) -> str:
    """Lowercase, dash-separated, alphanumeric-only, length-capped."""
    slug = _slugify(title, max_length=max_length, word_boundary=True, lowercase=True)
    return slug or "untitled"


def unique_path(directory: Path, stem: str, suffix: str = ".md") -> Path:
    """Return a path that doesn't exist yet, appending -2, -3, ... on collision."""
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    i = 2
    while True:
        candidate = directory / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


@dataclass
class PlannedWrite:
    """One target file in a batch write: destination path + final content."""

    path: Path
    content: str


def batch_atomic_write(writes: Iterable[PlannedWrite]) -> list[Path]:
    """Stage each write as a sibling .tmp file, then os.replace() them into place.

    On any exception during staging, no replacements happen — temps are cleaned.
    Once replacement starts, files are flipped one at a time. A mid-flip failure
    leaves a partially-updated tree: already-replaced files stand, remaining
    temps are cleaned, the exception re-raises so the caller can warn.
    """
    writes = list(writes)
    staged: list[tuple[Path, Path]] = []  # (final, tmp)
    try:
        for w in writes:
            w.path.parent.mkdir(parents=True, exist_ok=True)
            # NamedTemporaryFile would need delete=False + cross-platform care;
            # explicit tmp sibling is simpler and survives os.replace.
            fd, tmp_str = tempfile.mkstemp(
                prefix=f".{w.path.name}.", suffix=".tmp", dir=str(w.path.parent)
            )
            os.close(fd)
            tmp = Path(tmp_str)
            tmp.write_text(w.content, encoding="utf-8", newline="\n")
            staged.append((w.path, tmp))
    except Exception:
        for _, tmp in staged:
            tmp.unlink(missing_ok=True)
        raise

    replaced: list[Path] = []
    try:
        for final, tmp in staged:
            os.replace(tmp, final)
            replaced.append(final)
    except Exception:
        # Replace failed mid-batch. Clean up remaining temps; replaced files stay.
        replaced_paths = {s[0] for s, _ in zip(staged, staged) if s[0] in replaced}
        for final, tmp in staged:
            if final not in replaced_paths and tmp.exists():
                tmp.unlink(missing_ok=True)
        raise
    return replaced


@contextmanager
def chdir(path: Path):
    """Temporary cwd switch — used in tests."""
    prev = Path.cwd()
    os.chdir(path)
    try:
        yield path
    finally:
        os.chdir(prev)
