"""Account for canonical original references without reading provider or binary bytes."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from . import held_fs, originals, reserved_paths, runtime_resources, trash_metadata
from .kbdir import kb_dirname
from .vault import parse_frontmatter

# These refusal codes belong to the closed media protocol, not a content vocabulary.
MEDIA_ALLOWANCE_UNKNOWN = "MEDIA_ALLOWANCE_UNKNOWN"
MEDIA_ALLOWANCE_EXCEEDED = "MEDIA_ALLOWANCE_EXCEEDED"
ALLOWANCE_REFUSAL_CODES = (MEDIA_ALLOWANCE_UNKNOWN, MEDIA_ALLOWANCE_EXCEEDED)


# An unknown census names this many unreadable notes at most, in path order: enough for the owner to start
# on, small enough that a vault with many damaged notes grows neither the ledger row nor the status.
BLOCKERS_SHOWN = 5
# A reason is the walk's own message about a note; the cap keeps a long parser message off the status.
REASON_MAX_CHARS = 160


@dataclass(frozen=True, slots=True)
class Blocker:
    """One note that stopped the census: its vault-relative path and why it could not be read."""

    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReferenceUsage:
    versions: dict[tuple[str, str, str], tuple[originals.BinaryBinding, originals.StoredObject]] | None
    observed_at: str
    live_paths: dict[tuple[str, str, str], str] = field(default_factory=dict)
    # Why the census is unknown, when notes explain it: the first few by path, and how many there were in all.
    blockers: tuple[Blocker, ...] = ()
    blockers_total: int = 0

    @property
    def used_bytes(self) -> int | None:
        if self.versions is None:
            return None
        return sum(binary.size for binary, _reference in self.versions.values())

    def charged(
        self, binary: originals.BinaryBinding, reference: originals.StoredObject,
    ) -> ReferenceUsage:
        """This census plus a version that a writer is about to bind; an unknown census stays unknown."""
        key = _key(reference)
        if self.versions is None or key in self.versions:
            return self
        return ReferenceUsage({**self.versions, key: (binary, reference)}, self.observed_at,
                              {**self.live_paths, key: binary.path})


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


@dataclass(frozen=True, slots=True)
class AllowanceDecision:
    used_bytes: int | None
    projected_bytes: int | None
    allowance_bytes: int | None
    observed_at: str
    code: str | None


def admission(
    usage: ReferenceUsage, binary: originals.BinaryBinding,
    reference: originals.StoredObject | None, allowance_bytes: int | None,
) -> AllowanceDecision:
    """Admit one proposed plaintext version against the current reference union."""
    allowance = allowance_bytes if type(allowance_bytes) is int and allowance_bytes >= 0 else None
    try:
        projected = projected_bytes(usage, binary, reference)
    except ValueError:
        projected = None
    code = None
    if allowance is None or projected is None:
        code = MEDIA_ALLOWANCE_UNKNOWN
    elif projected > allowance:
        code = MEDIA_ALLOWANCE_EXCEEDED
    return AllowanceDecision(usage.used_bytes, projected, allowance, usage.observed_at, code)


def _binding(
    fields: dict[str, object], path: str, store: originals.StoreBinding,
    metadata: dict[str, str], vault: Path, placements: dict[str, dict[str, object]],
    placement_versions: dict[str, held_fs.FileVersion],
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
        if key not in placements:
            with reserved_paths.open_generic_binary(vault, metadata_path) as source:
                size = source.version.size
                with runtime_resources.parser_preparation(size) as preparation:
                    preparation.reserve_source(size)
                    raw = source.stream.read(size + 1)
                    if len(raw) != size:
                        raise ValueError("Trash metadata changed during read")
                    text = raw.decode("utf-8")
                    preparation.reserve_parser(text, json_bytes=len(raw))
                    parsed = parse_exact_json_object(text)
                    # Only the fixed placement field survives the parser reservation.
                    placement = {"original_path": parsed.get("original_path")}
                    trash_metadata.original_path(placement, trash_root=key, current_path=path)
                    source.require_current()
                    placement_versions[metadata_path] = source.version
                    placements[key] = placement
                    del parsed, text, raw
        try:
            meta = placements[key]
            historical = trash_metadata.original_path(meta, trash_root=root.as_posix(), current_path=path)
            return originals.referenced_binding(fields, sidecar_path=historical, store=store)
        except (ValueError, UnicodeError):
            continue
    raise ValueError("stored Trash reference has no trustworthy placement")


def bound_references(
    vault: Path, store: originals.StoreBinding, *, unreadable: Callable[[str, Exception], None] | None = None,
) -> Iterator[tuple[str, originals.BinaryBinding, originals.StoredObject]]:
    """Every reference to `store` under the Knowledge Base, as the companion's path and what it binds.

    The caller holds the consistency guard around the whole iteration, and takes any read failure as an
    unknown census: a failed read never ends the walk early and reads as complete. A caller that passes
    `unreadable` is told of each note that fails on its own: a header that the tree cannot read, decode or
    prepare within the note's own budget, or one that does not parse or bind. The walk then goes on to the next note, and that caller must treat
    the census as unknown once told. Without it, the first such note raises.
    """
    metadata: dict[str, str] = {}
    placements: dict[str, dict[str, object]] = {}
    placement_versions: dict[str, held_fs.FileVersion] = {}

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

    def header_failed(relative: str, error: Exception) -> None:
        assert unreadable is not None
        unreadable(f"{kb_dirname()}/{relative}", error)

    with reserved_paths.open_generic_tree(
        vault, kb_dirname(), select=select, require_complete=True,
        markdown_header_limit=runtime_resources.SEMANTIC_SOURCE_MAX_BYTES,
        markdown_header_failed=None if unreadable is None else header_failed,
    ) as entries:
        for entry in entries:
            path = f"{kb_dirname()}/{entry.relative_path}"
            try:
                if entry.markdown is None:
                    raise ValueError("companion header was not read")
                bound = _binding(
                    parse_frontmatter(entry.markdown.decode("utf-8"), custody=True)[0],
                    path, store, metadata, vault, placements, placement_versions,
                )
            except ValueError as error:  # This note's header or binding; a tree, budget or I/O failure is not a ValueError.
                if unreadable is None:
                    raise
                unreadable(path, error)
                continue
            finally:
                del entry
            if bound is not None:
                yield path, bound[0], bound[1]
    for metadata_path, version in placement_versions.items():
        with reserved_paths.open_generic_binary(vault, metadata_path) as source:
            if source.version != version:
                raise ValueError("Trash placement changed during the census")
            source.require_current()


# What a failed read of the Knowledge Base tree can raise; the census that hit one is unknown.
UNREADABLE = (
    OSError, ValueError, MemoryError, RecursionError, held_fs.HeldFsError,
    reserved_paths.ReservedPathLeafError, runtime_resources.PreparationBudgetExceeded,
    runtime_resources.PreparationCapacityBusy,
)


def reference_usage(vault_root: Path) -> ReferenceUsage:
    """Read complete current custody; a failed read never becomes an empty total."""
    from .writer_lease import get_manager

    vault = Path(vault_root).resolve()
    versions: dict[tuple[str, str, str], tuple[originals.BinaryBinding, originals.StoredObject]] = {}
    live_paths: dict[tuple[str, str, str], str] = {}
    blockers: list[Blocker] = []
    blocked = 0

    def blocked_by(path: str, error: Exception) -> None:
        nonlocal blocked
        blocked += 1
        blockers.append(Blocker(path, str(error)[:REASON_MAX_CHARS] or type(error).__name__))
        blockers.sort(key=lambda item: item.path)
        del blockers[BLOCKERS_SHOWN:]

    try:
        store = originals.binding_for(vault)
        if store is None:
            raise ValueError("no selected media store")
        with get_manager().consistency_guard(vault, operation="media_reference_usage", holder_kind="background"):
            for path, binary, reference in bound_references(vault, store, unreadable=blocked_by):
                key = _key(reference)
                if key in versions:
                    try:
                        _consistent(versions[key], (binary, reference))
                    except ValueError as error:
                        blocked_by(path, error)
                        continue
                versions[key] = (binary, reference)
                if not trash_metadata.is_trash_path(path):
                    live_paths.setdefault(key, binary.path)
    except UNREADABLE:  # The walk itself failed, so any note named so far is not the whole reason: name none.
        return ReferenceUsage(None, datetime.now(UTC).isoformat())
    if blocked:  # Notes that failed one by one: the census is unknown, and says which notes and why.
        return ReferenceUsage(None, datetime.now(UTC).isoformat(), {}, tuple(blockers), blocked)
    return ReferenceUsage(versions, datetime.now(UTC).isoformat(), live_paths)
