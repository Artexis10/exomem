"""The existing Trash placement format, shared by recovery and reference accounting."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from . import held_fs, originals, reserved_paths
from .kbdir import kb_dirname

if TYPE_CHECKING:
    from .graph_sync import GraphLifecycleTransition

# These names belong to the fixed vault placement format, not a content vocabulary.
TRASH_SUBPATH = "_trash"
METADATA_SUFFIX = ".meta.json"


def is_trash_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return len(parts) >= 2 and parts[0] == kb_dirname() and parts[1] == TRASH_SUBPATH


def destination_paths(target: Path) -> tuple[Path, Path, Path]:
    """The payload, placement metadata and canonical companion share one namespace."""
    return target, target.with_name(target.name + METADATA_SUFFIX), originals.sidecar_path(target)


def preserve_floor(source: str, name: str) -> str:
    """Keep the existing copy-stable RAW floor on a relocated Trash root."""
    from .governance.raw_protection import PREFIX, marked

    return PREFIX + name if marked(source) else name


def original_path(
    metadata: dict[str, object], *, trash_root: str, current_path: str,
    fallback_root: str | None = None,
) -> str:
    """Map a retained item through its declared original root and descendant suffix."""
    original = metadata.get("original_path") or fallback_root
    if not isinstance(original, str) or not original or reserved_paths.classify_logical(original).blocked:
        raise ValueError("Trash metadata has no valid original path")
    origin = PurePosixPath(original)
    root, current = PurePosixPath(trash_root), PurePosixPath(current_path)
    if current == root:
        return origin.as_posix()
    if current.is_relative_to(root):
        return (origin / current.relative_to(root)).as_posix()
    # A single binary's companion is adjacent, rather than a descendant of the binary.
    if current.as_posix() == originals.sidecar_path(Path(root)).as_posix():
        return originals.sidecar_path(Path(origin)).as_posix()
    raise ValueError("Trash item is outside its declared placement")


def reference_companion(vault: Path, binary_path: str) -> tuple[str, reserved_paths.GenericFileSnapshot] | None:
    """Acquire the exact reference-only companion that recovery may retire."""
    from .vault import parse_frontmatter

    path = originals.sidecar_path(Path(binary_path)).as_posix()
    try:
        snapshot = reserved_paths.read_generic_bytes(vault, path)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "MISSING":
            return None
        raise
    fields, body, _marker = parse_frontmatter(snapshot.data.decode("utf-8"), strict=True)
    if originals.bound_path(fields) != binary_path or not originals.is_reference_only_companion(fields, body):
        raise ValueError("Trash companion contains independent or unresolved content")
    return path, snapshot


def remove_unchanged(vault: Path, path: str, identity: held_fs.StableIdentity, data: bytes) -> None:
    """Retire only the acquired metadata bytes, retaining any replacement or edit."""
    try:
        current = reserved_paths.read_generic_bytes(vault, path)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "MISSING":
            return
        raise
    if current.identity != identity or current.data != data:
        raise reserved_paths.ReservedPathLeafError("IDENTITY_CHANGED")
    reserved_paths.unlink_generic_file(vault, path, expected_identity=identity)


@contextmanager
def preserve_placement(
    vault: Path, transition: GraphLifecycleTransition, files: Iterable[tuple[str, bytes]],
):
    """Sync reference custody before rename; undo only this transition's caught writes."""
    from . import graph_sync

    created: list[tuple[str, held_fs.StableIdentity, bytes]] = []

    def cleanup() -> None:
        try:
            for path, identity, data in reversed(created):
                remove_unchanged(vault, path, identity, data)
        except (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError) as error:
            raise graph_sync.GraphLifecycleRollbackError("Trash placement cleanup remains unresolved") from error

    publishing: str | None = None
    try:
        for path, data in files:
            publishing = path
            identity = reserved_paths.publish_generic_bytes(vault, path, data, expected_identity=None)
            created.append((path, identity, data))
    except Exception as error:
        transition.abort()
        if publishing is not None:
            try:
                reserved_paths.inspect_generic_path(vault, publishing)
            except reserved_paths.ReservedPathLeafError as inspection:
                if inspection.code != "MISSING":
                    raise graph_sync.GraphLifecycleRollbackError("Trash placement publication remains uncertain") from error
            else:
                # A publisher can fail after its write; no returned identity proves ownership.
                raise graph_sync.GraphLifecycleRollbackError("Trash placement publication remains uncertain") from error
        cleanup()
        raise
    try:
        yield
    except Exception:
        # An incomplete inverse rename still needs every durable placement record.
        if not transition.renamed:
            cleanup()
        raise
