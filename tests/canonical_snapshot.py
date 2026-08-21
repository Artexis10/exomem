"""Snapshot a vault's *canonical* bytes, ignoring registered internal state.

Several tests assert that some operation left the vault byte-identical. Since an
interactive write stopped joining its own graph rebuild, that rebuild is still
running when the assertion is taken, and its scratch sidecars
(`.graph-rebuild-<digest>.sqlite` and companions) are sitting in the tree.

Those are not vault content, and the distinction is not a test convenience: the
canonical directory census consumes the same closed internal-state registry. A
write is not "a mutation of the vault" because an index owner happened to be
mid-flight beside it, and a test that says otherwise is asserting something the
guard itself does not.

Deliberately one definition rather than one filter per test module. Two copies
had already diverged into two different ideas of what counts as residue, and a
third would have been added for every module that grew a snapshot.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from exomem import vault as vault_module


def is_canonical(path: Path, root: Path) -> bool:
    """Whether `path` is canonical vault content rather than derived residue."""
    return path.is_file() and not vault_module._is_registered_internal_state_artifact(
        path.relative_to(root).as_posix()
    )


def canonical_bytes(root: Path) -> dict[str, bytes]:
    """Every canonical file's bytes, keyed by POSIX-relative path."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if is_canonical(path, root)
    }


def canonical_digests(root: Path) -> dict[str, str]:
    """Every canonical file's SHA-256, keyed by POSIX-relative path.

    Preferred over `canonical_bytes` when the assertion only needs to detect a
    change: a failing byte-map comparison prints the whole vault.
    """
    return {
        relative: hashlib.sha256(payload).hexdigest()
        for relative, payload in canonical_bytes(root).items()
    }
