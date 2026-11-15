"""The existing Trash placement format, shared by recovery and reference accounting."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from . import held_fs, originals, reserved_paths
from .kbdir import managed_split, managed_split_parts

if TYPE_CHECKING:
    from .graph_sync import GraphLifecycleTransition

# These names belong to the fixed vault placement format, not a content vocabulary.
TRASH_SUBPATH = "_trash"
METADATA_SUFFIX = ".meta.json"


def is_trash_path(path: str) -> bool:
    below = managed_split_parts(PurePosixPath(path).parts)
    return below is not None and below[:1] == (TRASH_SUBPATH,)


def is_inside_trash(path: str, *, key: Callable[[str], str] | None = None) -> bool:
    """Whether the vault-relative ``path`` lies below the managed root's trash folder.

    The folder's own path is not below it, whereas ``is_trash_path`` accepts that path too. A ``key``
    (such as ``str.casefold``) compares the managed root's name and the trash folder's name
    through it, as ``managed_split`` does, for callers whose own rule is looser than byte equality.
    """
    below = managed_split(path, key=key)
    if below is None:
        return False
    if key is None:
        return below.startswith(TRASH_SUBPATH + "/")
    return key(below).startswith(key(TRASH_SUBPATH) + "/")


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


class PlacementCensus:
    """Reuse bounded placement parsing and recheck every acquired metadata version."""

    def __init__(self, vault: Path) -> None:
        self.vault = vault
        self.placements: dict[str, dict[str, object] | None] = {}
        self.versions: dict[str, held_fs.FileVersion] = {}

    def _placement(self, root: str) -> dict[str, object] | None:
        from . import runtime_resources
        from .relation_review import parse_exact_json_object

        if root not in self.placements:
            path = root + METADATA_SUFFIX
            try:
                with reserved_paths.open_generic_binary(self.vault, path) as source:
                    size = source.version.size
                    with runtime_resources.parser_preparation(size) as preparation:
                        preparation.reserve_source(size)
                        raw = source.stream.read(size + 1)
                        if len(raw) != size:
                            raise ValueError("Trash metadata changed during read")
                        text = raw.decode("utf-8")
                        preparation.reserve_parser(text, json_bytes=len(raw))
                        parsed = parse_exact_json_object(text)
                        placement = {"original_path": parsed.get("original_path")}
                        original_path(placement, trash_root=root, current_path=root)
                        source.require_current()
                        self.versions[path] = source.version
                        self.placements[root] = placement
            except reserved_paths.ReservedPathLeafError as error:
                if error.code != "MISSING":
                    raise
                self.placements[root] = None
        return self.placements[root]

    def resolve(self, path: str, fields: dict[str, object]) -> originals.BinaryBinding | None:
        """Return the current payload path without rewriting the canonical companion."""
        try:
            return originals.companion_binding(fields, sidecar_path=path)
        except ValueError:
            if not is_trash_path(path):
                raise
        binary = originals.binary_binding(fields)
        if binary is None:
            return None
        # A Trash page that sat away from its companion path before its deletion raises the
        # `DisplacedReference` of that placement, for the census to confirm from the media ledger.
        displaced: originals.DisplacedReference | None = None
        current = PurePosixPath(path)
        for root in (current, current.with_suffix(""), *current.parents):
            if not is_trash_path(root.as_posix()):
                continue
            placement = self._placement(root.as_posix())
            if placement is None:
                continue
            try:
                historical = original_path(placement, trash_root=root.as_posix(), current_path=path)
                admitted = originals.companion_binding(fields, sidecar_path=historical)
                if admitted is None:
                    continue
                origin = PurePosixPath(placement["original_path"])
                payload = root / PurePosixPath(admitted.path).relative_to(origin)
                return originals.BinaryBinding(payload.as_posix(), admitted.sha256, admitted.size)
            except originals.DisplacedReference as error:
                displaced = error
            except ValueError:
                continue
        if displaced is not None:
            raise displaced
        raise ValueError("original in Trash has no trustworthy placement")

    def require_current(self) -> None:
        for path, version in self.versions.items():
            with reserved_paths.open_generic_binary(self.vault, path) as source:
                if source.version != version:
                    raise ValueError("Trash placement changed during the census")
                source.require_current()


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
    fields, body, _marker = parse_frontmatter(snapshot.data.decode("utf-8"), custody=True)
    if originals.bound_path(fields) != binary_path or not originals.is_reference_only_companion(fields, body):
        raise ValueError("Trash companion contains independent or unresolved content")
    return path, snapshot


def remove_unchanged(
    vault: Path, path: str, identity: held_fs.StableIdentity | None, data: bytes
) -> None:
    """Retire only the acquired metadata bytes, retaining any replacement or edit.

    ``identity`` is None for a companion that the vault's store holds; a stored page has no file to
    retire.
    """
    try:
        current = reserved_paths.read_generic_bytes(vault, path)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "MISSING":
            return
        raise
    if current.identity != identity or current.data != data:
        raise reserved_paths.ReservedPathLeafError("IDENTITY_CHANGED")
    if identity is None:
        # Retiring a stored companion is a store delete (the write path); an unlink here would drop
        # only its projection.
        raise reserved_paths.ReservedPathLeafError("UNSAFE_PATH")
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
