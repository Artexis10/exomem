"""Account for canonical original references without reading provider or binary bytes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import held_fs, originals, reserved_paths, runtime_resources, trash_metadata
from .kbdir import managed_join, managed_root_rel
from .vault import parse_frontmatter

# These refusal codes belong to the closed media protocol, not a content vocabulary.
MEDIA_ALLOWANCE_UNKNOWN = "MEDIA_ALLOWANCE_UNKNOWN"
MEDIA_ALLOWANCE_EXCEEDED = "MEDIA_ALLOWANCE_EXCEEDED"
ALLOWANCE_REFUSAL_CODES = (MEDIA_ALLOWANCE_UNKNOWN, MEDIA_ALLOWANCE_EXCEEDED)


# An unknown census names this many blocking notes at most, in path order: enough for the owner to start on,
# small enough that a vault with many damaged notes grows neither the ledger row nor the status. The owner
# census and the retained-store census of media_store share it.
BLOCKERS_SHOWN = 5
# A reason is the walk's own message about a note; the cap keeps a long parser message off the status.
REASON_MAX_CHARS = 160


@dataclass(frozen=True, slots=True)
class Blocker:
    """One note that stopped the census: its vault-relative path and why it could not be read."""

    path: str
    reason: str


def blocked_fields(blockers: tuple[Blocker, ...]) -> dict:
    """The status field that names the notes behind an unknown census, or none when no note explains it.

    The notes stay out of every message: each is its own entry with a path, which egress filters one by one.
    No count goes with them, because a count would tell a caller about notes that egress withholds from it.
    """
    if not blockers:
        return {}
    return {"blocked_by": [{"path": item.path, "reason": item.reason} for item in blockers]}


@dataclass(frozen=True, slots=True)
class ReferenceUsage:
    versions: dict[tuple[str, str, str], tuple[originals.BinaryBinding, originals.StoredObject]] | None
    observed_at: str
    live_paths: dict[tuple[str, str, str], str] = field(default_factory=dict)
    # Why the census is unknown, when notes explain it: the first few by path.
    blockers: tuple[Blocker, ...] = ()

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


def decide(
    used_bytes: int | None, projected: int | None, allowance_bytes: object, observed_at: str,
) -> AllowanceDecision:
    """Judge a projected total against the allowance; an unknown total or allowance is unknown, never 0."""
    allowance = allowance_bytes if type(allowance_bytes) is int and allowance_bytes >= 0 else None
    code = None
    if allowance is None or projected is None:
        code = MEDIA_ALLOWANCE_UNKNOWN
    elif projected > allowance:
        code = MEDIA_ALLOWANCE_EXCEEDED
    return AllowanceDecision(used_bytes, projected, allowance, observed_at, code)


def admission(
    usage: ReferenceUsage, binary: originals.BinaryBinding,
    reference: originals.StoredObject | None, allowance_bytes: int | None,
) -> AllowanceDecision:
    """Admit one proposed plaintext version against the current reference union."""
    try:
        projected = projected_bytes(usage, binary, reference)
    except ValueError:
        projected = None
    return decide(usage.used_bytes, projected, allowance_bytes, usage.observed_at)


def bound_references(
    vault: Path, store: originals.StoreBinding, *, unreadable: Callable[[str, Exception], None] | None = None,
) -> Iterator[tuple[str, originals.BinaryBinding, originals.StoredObject, bool]]:
    """Every reference to `store` under the Knowledge Base: the companion's path, what it binds, and if displaced.

    The caller holds the consistency guard around the whole iteration, and takes any read failure as an
    unknown census: a failed read never ends the walk early and reads as complete. A caller that passes
    `unreadable` is told of each note that fails on its own: a header that the tree cannot read, decode or
    prepare within the note's own budget, or one that does not parse or bind. The walk then goes on to the next note, and that caller must treat
    the census as unknown once told. Without it, the first such note raises.

    A page that binds a reference away from its original's companion path, as a confined move leaves
    one, counts only when the media ledger records that exact version at the original's path; those pages
    come after the walk, from one batched ledger read, with True last. Such a page holds no original
    beside it: its original stays at `binary.path`, and several pages may name the same one.
    """
    placements = trash_metadata.PlacementCensus(vault)
    displaced: list[tuple[str, originals.DisplacedReference]] = []

    def select(candidates: list[tuple[str, str]]):
        for relative, kind in candidates:
            if kind != "file":
                continue
            if relative.lower().endswith(".md"):
                yield relative

    def header_failed(relative: str, error: Exception) -> None:
        assert unreadable is not None
        unreadable(managed_join(relative), error)

    with reserved_paths.open_generic_tree(
        vault, managed_root_rel(), select=select, require_complete=True,
        markdown_header_limit=runtime_resources.SEMANTIC_SOURCE_MAX_BYTES,
        markdown_header_failed=None if unreadable is None else header_failed,
    ) as entries:
        for entry in entries:
            path = managed_join(entry.relative_path)
            try:
                if entry.markdown is None:
                    raise ValueError("companion header was not read")
                fields = parse_frontmatter(entry.markdown.decode("utf-8"), custody=True)[0]
                bound = None
                if "stored_object" in fields:
                    reference = originals.stored_reference(fields["stored_object"])
                    if reference is None:
                        raise ValueError("stored reference is malformed")
                    if reference.store == store.store:
                        # A live page's `DisplacedReference` comes here, and so does a Trash page that sat
                        # away from its companion path before its deletion: the ledger confirms both below.
                        binary = placements.resolve(path, fields)
                        if binary is None:
                            raise ValueError("stored reference has no valid binary binding")
                        bound = binary, reference
            except originals.DisplacedReference as error:
                displaced.append((path, error))
                continue
            except ValueError as error:  # This note's YAML or binding. The tree reports a header it cannot read; a walk or I/O failure is no ValueError.
                if unreadable is None:
                    raise
                unreadable(path, error)
                continue
            finally:
                del entry
            if bound is not None:
                yield path, bound[0], bound[1], False
    if displaced:
        from . import media_store

        try:
            recorded = media_store.recorded_versions(vault, [(item.binary, item.reference) for _path, item in displaced])
        except sqlite3.Error as error:
            raise OSError("the media ledger that confirms a moved page's reference cannot be read") from error
        for (path, item), confirmed in zip(displaced, recorded, strict=True):
            if confirmed:
                yield path, item.binary, item.reference, True
            elif unreadable is None:
                raise item
            else:
                unreadable(path, item)
    placements.require_current()


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

    def blocked_by(path: str, error: Exception) -> None:
        blockers.append(Blocker(path, str(error)[:REASON_MAX_CHARS] or type(error).__name__))
        blockers.sort(key=lambda item: item.path)
        del blockers[BLOCKERS_SHOWN:]

    try:
        store = originals.binding_for(vault)
        if store is None:
            raise ValueError("no selected media store")
        with get_manager().consistency_guard(vault, operation="media_reference_usage", holder_kind="background"):
            for path, binary, reference, displaced in bound_references(vault, store, unreadable=blocked_by):
                key = _key(reference)
                if key in versions:
                    try:
                        _consistent(versions[key], (binary, reference))
                    except ValueError as error:
                        blocked_by(path, error)
                        continue
                versions[key] = (binary, reference)
                # A displaced page's original reads missing at its path, so it is no candidate for reuse.
                if not displaced and not trash_metadata.is_trash_path(path):
                    live_paths.setdefault(key, binary.path)
    except UNREADABLE:  # The walk itself failed, so any note named so far is not the whole reason: name none.
        return ReferenceUsage(None, datetime.now(UTC).isoformat())
    if blockers:  # Notes that failed one by one: the census is unknown, and says which notes and why.
        return ReferenceUsage(None, datetime.now(UTC).isoformat(), {}, tuple(blockers))
    return ReferenceUsage(versions, datetime.now(UTC).isoformat(), live_paths)
