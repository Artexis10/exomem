"""Account for canonical original references without reading provider or binary bytes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from . import held_fs, originals, reserved_paths, trash_metadata
from .kbdir import kb_dirname
from .vault import parse_frontmatter


@dataclass(frozen=True, slots=True)
class ReferenceUsage:
    versions: dict[tuple[str, str, str], tuple[originals.BinaryBinding, originals.StoredObject]] | None
    observed_at: str

    @property
    def used_bytes(self) -> int | None:
        if self.versions is None:
            return None
        return sum(binary.size for binary, _reference in self.versions.values())


def _key(reference: originals.StoredObject) -> tuple[str, str, str]:
    return reference.store, reference.object, reference.version


def _consistent(
    first: tuple[originals.BinaryBinding, originals.StoredObject],
    second: tuple[originals.BinaryBinding, originals.StoredObject],
) -> None:
    before, before_reference = first
    after, after_reference = second
    if before.sha256 != after.sha256 or before.size != after.size or before_reference != after_reference:
        raise ValueError("one stored version has conflicting bindings")


def projected_bytes(
    usage: ReferenceUsage, binary: originals.BinaryBinding, reference: originals.StoredObject | None,
) -> int | None:
    """Count a proposed version once; None names a new version not uploaded yet."""
    used = usage.used_bytes
    if used is None or usage.versions is None:
        return None
    if reference is not None and (existing := usage.versions.get(_key(reference))) is not None:
        _consistent(existing, (binary, reference))
        return used
    return used + binary.size


def _binding(
    fields: dict[str, object], path: str, store: originals.StoreBinding,
    metadata: dict[str, str], vault: Path, placements: dict[str, dict[str, object] | None],
) -> tuple[originals.BinaryBinding, originals.StoredObject] | None:
    try:
        return originals.referenced_binding(fields, sidecar_path=path, store=store)
    except ValueError:
        if not PurePosixPath(path).is_relative_to(PurePosixPath(kb_dirname()) / trash_metadata.TRASH_SUBPATH):
            raise
    from .relation_review import parse_exact_json_object

    current = PurePosixPath(path)
    # A canonical companion is adjacent to its binary; directory entries use their ancestor root.
    candidates = (current, current.with_suffix(""), *current.parents)
    for root in candidates:
        key = root.as_posix()
        metadata_path = metadata.get(key)
        if metadata_path is None:
            continue
        try:
            if key not in placements:
                try:
                    raw = reserved_paths.read_generic_bytes(vault, metadata_path).data
                    placements[key] = parse_exact_json_object(raw.decode("utf-8"))
                except (OSError, ValueError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
                    placements[key] = None
            meta = placements[key]
            if meta is None:
                continue
            historical = trash_metadata.original_path(meta, trash_root=root.as_posix(), current_path=path)
            return originals.referenced_binding(fields, sidecar_path=historical, store=store)
        except (ValueError, UnicodeError):
            continue
    raise ValueError("stored Trash reference has no trustworthy placement")


def reference_usage(vault_root: Path) -> ReferenceUsage:
    """Read complete current custody; a failed read never becomes an empty total."""
    from .writer_lease import get_manager

    vault = Path(vault_root).resolve()
    versions: dict[tuple[str, str, str], tuple[originals.BinaryBinding, originals.StoredObject]] = {}
    try:
        store = originals.binding_for(vault)
        if store is None:
            raise ValueError("no selected media store")
        with get_manager().consistency_guard(vault, operation="media_reference_usage", holder_kind="background"):
            metadata: dict[str, str] = {}
            placements: dict[str, dict[str, object] | None] = {}

            def select(candidates: list[tuple[str, str]]):
                for relative, kind in candidates:
                    if kind != "file":
                        continue
                    # Markdown and Trash metadata extensions belong to the fixed vault format.
                    if (PurePosixPath(relative).is_relative_to(trash_metadata.TRASH_SUBPATH)
                            and relative.endswith(trash_metadata.METADATA_SUFFIX)):
                        metadata[f"{kb_dirname()}/{relative[:-len(trash_metadata.METADATA_SUFFIX)]}"] = f"{kb_dirname()}/{relative}"
                    if relative.lower().endswith(".md"):
                        yield relative

            entries = reserved_paths.list_generic_tree(vault, kb_dirname(), select=select, require_complete=True)
            for entry in entries:
                if not entry.relative_path.lower().endswith(".md"):
                    continue
                if entry.markdown is None:
                    raise ValueError("companion bytes were not read")
                fields, _body, _marker = parse_frontmatter(entry.markdown.decode("utf-8"), strict=True)
                path = f"{kb_dirname()}/{entry.relative_path}"
                bound = _binding(fields, path, store, metadata, vault, placements)
                if bound is None:
                    continue
                binary, reference = bound
                key = _key(reference)
                if key in versions:
                    _consistent(versions[key], bound)
                versions[key] = (binary, reference)
    except (OSError, ValueError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
        return ReferenceUsage(None, datetime.now(UTC).isoformat())
    return ReferenceUsage(versions, datetime.now(UTC).isoformat())
