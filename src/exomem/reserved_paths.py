"""Closed authority for Exomem's private in-vault state paths.

Logical classification is deliberately pure and existence-independent.  Physical
classification is a second, conservative acquisition-time check; it is not a
substitute for the held-handle operation that consumes the target.
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from . import held_fs
from .kbdir import kb_dirname

REGISTRY_VERSION = 1

_SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")
_REVIEW_TEMP_RE = re.compile(r"^\.\.review-state\.json\.[a-z0-9_]{8}\.tmp$", re.ASCII)
_LEXICAL_REBUILD_RE = re.compile(
    r"^\.lexical\.sqlite\.rebuild-[0-9a-f]{32}\.tmp(?:-(?:wal|shm|journal))?$",
    re.ASCII,
)
_LEXICAL_QUARANTINE_RE = re.compile(
    r"^\.lexical\.sqlite(?:-(?:wal|shm))?\.quarantine-[0-9a-f]{32}$",
    re.ASCII,
)
_GRAPH_REBUILD_RE = re.compile(
    r"^\.graph-rebuild-[0-9a-f]{64}-[0-9a-f]{24}\.sqlite"
    r"(?:-(?:wal|shm|journal))?$",
    re.ASCII,
)
_GRAPH_RESET_RE = re.compile(r"^\.graph-reset-[0-9a-f]{24}$", re.ASCII)
_BATCH_WORKSPACE_RE = re.compile(r"^\.exomem-batch-[0-9a-f]{32}$", re.ASCII)
_HELD_PUBLICATION_RE = re.compile(
    rf"^{re.escape(held_fs.PUBLISH_TEMP_PREFIX)}[0-9a-f]{{32}}$",
    re.ASCII,
)
_DRIVE_RE = re.compile(r"^[a-zA-Z]:")


class PathDisposition(StrEnum):
    """The only public logical outcomes."""

    ORDINARY = "ordinary"
    RESERVED = "reserved"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class PathClassification:
    disposition: PathDisposition
    descriptor_id: str | None = None
    canonical: str | None = None
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.disposition is not PathDisposition.ORDINARY


@dataclass(frozen=True, slots=True)
class InternalStateDescriptor:
    """One non-overlapping private-state family and its sole owner."""

    id: str
    owner: str
    owning_command: str | None = None
    authority_enabled: bool = True
    exact: tuple[str, ...] = ()
    trees: tuple[str, ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()
    tree_patterns: tuple[re.Pattern[str], ...] = ()
    leaf_patterns: tuple[re.Pattern[str], ...] = ()
    component_tree_patterns: tuple[re.Pattern[str], ...] = ()

    def matches(self, parts: tuple[str, ...]) -> bool:
        if not parts:
            return False
        leaf_path = "/".join(parts)
        if leaf_path in self.exact:
            return True
        if any(leaf_path == tree or leaf_path.startswith(f"{tree}/") for tree in self.trees):
            return True
        if len(parts) == 1 and any(pattern.fullmatch(parts[0]) for pattern in self.patterns):
            return True
        if any(pattern.fullmatch(parts[0]) for pattern in self.tree_patterns):
            return True
        if any(pattern.fullmatch(parts[-1]) for pattern in self.leaf_patterns):
            return True
        return any(
            pattern.fullmatch(part)
            for part in parts
            for pattern in self.component_tree_patterns
        )


@dataclass(frozen=True, slots=True)
class PathRole:
    """How one public command argument can name filesystem-backed state."""

    argument: str
    role: str
    value_kind: str = "path"


@dataclass(frozen=True, slots=True)
class ReservedPathHit:
    """One content-free preflight match; caller input is intentionally absent."""

    role: PathRole
    classification: PathClassification


@dataclass(frozen=True, slots=True)
class GenericFileSnapshot:
    """Bytes and metadata read from the exact retained generic leaf."""

    data: bytes
    identity: held_fs.StableIdentity
    mtime: float


@dataclass(frozen=True, slots=True)
class GenericTreeFile:
    """One file read from a retained, recursively enumerated ordinary tree."""

    relative_path: str
    snapshot: GenericFileSnapshot


@dataclass(frozen=True, slots=True)
class GenericTreeEntry:
    """One structurally releasable entry from a retained directory walk."""

    relative_path: str
    identity: held_fs.StableIdentity
    size_bytes: int | None
    mtime: float | None
    markdown: bytes | None = None


@dataclass(slots=True)
class ReservedPathLeafError(Exception):
    """Content-free failure from the descriptor-bound generic leaf boundary."""

    code: str

    def __str__(self) -> str:
        return self.code


_OWNER_AUTHORITY_SEAL = object()


class _OwnerAuthority:
    """Opaque dispatcher-issued authority; deliberately has no wire form."""

    __slots__ = ("_seal", "_descriptor_ids")

    def __init__(self, seal: object, descriptor_ids: frozenset[str]) -> None:
        if seal is not _OWNER_AUTHORITY_SEAL:
            raise TypeError("owner authority is dispatcher-issued")
        self._seal = seal
        self._descriptor_ids = descriptor_ids

    def __reduce__(self):
        raise TypeError("owner authority cannot be serialized")

    def __reduce_ex__(self, _protocol: int):
        raise TypeError("owner authority cannot be serialized")

    def __repr__(self) -> str:
        return "<reserved owner authority>"


_ACTIVE_OWNER_AUTHORITY: ContextVar[_OwnerAuthority | None] = ContextVar(
    "exomem_reserved_owner_authority", default=None
)
_ACTIVE_IDENTITY_COORDINATION: ContextVar[
    tuple[tuple[str, frozenset[str]], ...]
] = ContextVar(
    "exomem_reserved_identity_coordination", default=()
)


def _active_owner_authority() -> _OwnerAuthority | None:
    """Return the current opaque authority for internal enforcement/tests."""

    return _ACTIVE_OWNER_AUTHORITY.get()


def _vault_identity_key(vault_root: Path) -> str:
    """Canonical process key shared by short/long spellings of one vault."""

    return os.path.normcase(str(Path(vault_root).expanduser().resolve(strict=False)))


def _identity_coordination_domains(
    descriptor_ids: Iterable[str] | None,
) -> tuple[frozenset[str], bool]:
    """Return the exact private-owner domains and whether all must be excluded."""

    authority = _ACTIVE_OWNER_AUTHORITY.get()
    if descriptor_ids is not None:
        domains = frozenset(descriptor_ids)
        if (
            not domains
            or authority is None
            or authority._seal is not _OWNER_AUTHORITY_SEAL
            or not domains <= authority._descriptor_ids
        ):
            raise RuntimeError("private identity domain lacks exact owner authority")
        return domains, False
    if authority is not None and authority._seal is _OWNER_AUTHORITY_SEAL:
        return authority._descriptor_ids, False
    return frozenset(descriptor.id for descriptor in _REGISTRY), True


def _identity_coordination_active(
    vault_root: Path,
    descriptor_id: str | None = None,
) -> bool:
    """Whether this task holds the required cooperative identity domain."""

    key = _vault_identity_key(vault_root)
    return any(
        active_key == key
        and (descriptor_id is None or descriptor_id in active_domains)
        for active_key, active_domains in _ACTIVE_IDENTITY_COORDINATION.get()
    )


@contextmanager
def _identity_coordination_scope(
    vault_root: Path,
    *,
    descriptor_ids: Iterable[str] | None = None,
) -> Iterator[None]:
    """Serialize private identity publication with generic held-leaf acquisition."""

    key = _vault_identity_key(vault_root)
    domains, exclusive = _identity_coordination_domains(descriptor_ids)
    if not domains:
        raise RuntimeError("private identity coordination has no registered domain")
    active = _ACTIVE_IDENTITY_COORDINATION.get()
    held_domains = frozenset(
        domain
        for active_key, active_set in active
        if active_key == key
        for domain in active_set
    )
    if domains <= held_domains:
        yield
        return
    if held_domains:
        raise RuntimeError("private identity coordination cannot widen while held")

    from .writer_lease import active_manager

    with active_manager().reserved_identity_guard(
        vault_root,
        domains=domains,
        exclusive=exclusive,
        operation="reserved_identity",
        holder_kind="reserved-state",
    ):
        token = _ACTIVE_IDENTITY_COORDINATION.set((*active, (key, domains)))
        try:
            yield
        finally:
            _ACTIVE_IDENTITY_COORDINATION.reset(token)


def owner_authorized(descriptor_id: str) -> bool:
    """Whether the current dispatcher scope owns exactly this descriptor."""

    authority = _ACTIVE_OWNER_AUTHORITY.get()
    return bool(
        authority is not None
        and authority._seal is _OWNER_AUTHORITY_SEAL
        and descriptor_id in authority._descriptor_ids
    )


def _sqlite_family(name: str) -> tuple[str, ...]:
    return tuple(f"{name}{suffix}" for suffix in _SQLITE_SUFFIXES)


_REGISTRY = (
    InternalStateDescriptor(
        "governance-tree",
        "governance.tool",
        "govern_memory",
        trees=("_governance",),
    ),
    InternalStateDescriptor(
        "consolidation-tree",
        "consolidation.future",
        authority_enabled=False,
        trees=("_consolidation",),
    ),
    InternalStateDescriptor(
        "governance-store", "governance.store", exact=_sqlite_family(".governance.sqlite")
    ),
    InternalStateDescriptor(
        "embeddings-store", "embedding_index", exact=_sqlite_family(".embeddings.sqlite")
    ),
    InternalStateDescriptor(
        "clip-store", "clip_index", exact=_sqlite_family(".clip.sqlite")
    ),
    InternalStateDescriptor(
        "lexical-store", "lexstore", exact=_sqlite_family(".lexical.sqlite")
    ),
    InternalStateDescriptor(
        "graph-store", "epistemic_graph", exact=_sqlite_family(".graph.sqlite")
    ),
    InternalStateDescriptor(
        "claims-store", "claims", exact=_sqlite_family(".claims.sqlite")
    ),
    InternalStateDescriptor(
        "references-store",
        "references.legacy",
        authority_enabled=False,
        exact=_sqlite_family(".references.sqlite"),
    ),
    InternalStateDescriptor(
        "refs-store", "memory_refs", exact=_sqlite_family(".refs.sqlite")
    ),
    InternalStateDescriptor(
        "freshness-store",
        "freshness.legacy",
        authority_enabled=False,
        exact=_sqlite_family(".freshness.sqlite"),
    ),
    InternalStateDescriptor(
        "deferred-index-store",
        "deferred_index",
        exact=(
            *_sqlite_family(".deferred-index.sqlite"),
            *_sqlite_family(".deferred_index.sqlite"),
            ".deferred-index.json",
        ),
    ),
    InternalStateDescriptor(
        "media-jobs-store",
        "media_jobs",
        exact=(
            *_sqlite_family(".media-jobs.sqlite"),
            *_sqlite_family(".media_jobs.sqlite"),
            ".media-jobs.json",
            ".media-worker.lock",
        ),
    ),
    InternalStateDescriptor(
        "idempotency-store",
        "idempotency",
        exact=(
            *_sqlite_family(".idempotency.sqlite"),
            ".idempotency.json",
            ".idempotency.jsonl",
        ),
    ),
    InternalStateDescriptor(
        "voice-profile-store", "voice_profiles", exact=(".voice_profiles.json",)
    ),
    InternalStateDescriptor(
        "graph-handoff",
        "graph_sync",
        exact=(".graph-sync.json", ".graph-sync-floor.json"),
    ),
    InternalStateDescriptor(
        "graph-receipts", "graph_sync", trees=(".graph-commit-receipts",)
    ),
    InternalStateDescriptor(
        "review-state",
        "review_state",
        exact=(".review-state.json",),
        patterns=(_REVIEW_TEMP_RE,),
    ),
    InternalStateDescriptor(
        "lexical-rebuild", "lexstore", patterns=(_LEXICAL_REBUILD_RE,)
    ),
    InternalStateDescriptor(
        "lexical-quarantine", "lexstore", patterns=(_LEXICAL_QUARANTINE_RE,)
    ),
    InternalStateDescriptor(
        "graph-rebuild", "epistemic_graph", patterns=(_GRAPH_REBUILD_RE,)
    ),
    InternalStateDescriptor(
        "graph-reset", "graph_sync", tree_patterns=(_GRAPH_RESET_RE,)
    ),
    InternalStateDescriptor(
        "authorization-projections",
        "governance.projections",
        authority_enabled=False,
        trees=(".authorization-projections",),
    ),
    InternalStateDescriptor(
        "batch-workspace",
        "vault.batch",
        component_tree_patterns=(_BATCH_WORKSPACE_RE,),
    ),
    InternalStateDescriptor(
        "held-publication",
        "held_fs",
        leaf_patterns=(_HELD_PUBLICATION_RE,),
    ),
)


def _roles(*values: tuple[str, str, str]) -> tuple[PathRole, ...]:
    return tuple(PathRole(*value) for value in values)


_COMMAND_PATH_ROLES = MappingProxyType(
    {
        "fetch": _roles(("id", "source", "path-or-ref")),
        "find": _roles(("relation_of", "selector", "path-or-ref")),
        "suggest_links": _roles(("path", "source", "path-or-ref")),
        "graph_context": _roles(
            ("path", "source", "path-or-ref"),
            ("unit_ref", "source", "ref"),
        ),
        "suggest_relations": _roles(("path", "source", "path-or-ref")),
        "overview": _roles(("path", "source", "path")),
        "adopt": _roles(
            ("path", "external-source", "external-path"),
            ("manifest_path", "external-selector", "external-path"),
            ("selected_paths", "external-selector", "external-path-list"),
        ),
        "provenance_report": _roles(("path", "source", "path-or-ref")),
        "propose_compilation": _roles(("sources", "source", "path-or-ref-list")),
        "get": _roles(("path", "source", "path-or-ref")),
        "edit": _roles(("path", "source-destination", "path")),
        "observe_memory": _roles(
            ("path", "source-destination", "path"),
            ("unit_ref", "source", "ref"),
        ),
        "replace": _roles(
            ("old_path", "source", "path"),
            ("sources", "source", "path-or-ref-list"),
        ),
        "note": _roles(("sources", "source", "path-or-ref-list")),
        "query_data": _roles(("path", "dataset", "path")),
        "create_file": _roles(("path", "destination", "path")),
        "list_directory": _roles(("path", "recursive-source", "path")),
        "move_file": _roles(
            ("old_path", "source", "path"),
            ("new_path", "destination", "path"),
        ),
        "delete": _roles(("path", "recursive-source", "path")),
        "append_to_file": _roles(("path", "source-destination", "path")),
        "recover_from_trash": _roles(
            ("trash_path", "recovery-source", "path"),
            ("restore_path", "recovery-destination", "path"),
        ),
        "list_inbound_links": _roles(("target", "source", "path-or-ref")),
        "record_memory": _roles(
            ("collection", "selector", "path-or-ref"),
            ("manifest_path", "destination", "path"),
        ),
        "plan_memory": _roles(
            ("collection", "selector", "path-or-ref"),
            ("manifest_path", "destination", "path"),
        ),
        "get_video_frames": _roles(("path", "media-source", "path")),
        "ask_memory": _roles(("relation_of", "selector", "path-or-ref")),
        "read_memory": _roles(
            ("path", "source", "path-or-ref"),
            ("unit_ref", "source", "ref"),
        ),
        "browse_memory": _roles(("path", "recursive-source", "path")),
        "remember": _roles(("sources", "source", "path-or-ref-list")),
        "edit_memory": _roles(("path", "source-destination", "path")),
        "replace_memory": _roles(
            ("old_path", "source", "path"),
            ("sources", "source", "path-or-ref-list"),
        ),
        "compile_source": _roles(("sources", "source", "path-or-ref-list")),
        "preserve_artifacts": _roles(("files", "external-source", "external-artifacts")),
        "process_media": _roles(("path", "media-source", "path")),
        "review_memory": _roles(
            ("path", "source", "path-or-ref"),
            ("ref", "source", "ref"),
            ("sources", "source", "path-or-ref-list"),
        ),
        "review_item_context": _roles(("ref", "source", "ref")),
        "triage_memory": _roles(("ref", "source-destination", "ref")),
        "connect_memory": _roles(
            ("path", "source", "path-or-ref"),
            ("target", "source", "path-or-ref"),
            ("unit_ref", "source", "ref"),
            ("ref", "source", "ref"),
        ),
        "adopt_vault": _roles(
            ("path", "external-source", "external-path"),
            ("manifest_path", "external-selector", "external-path"),
            ("selected_paths", "external-selector", "external-path-list"),
        ),
        "adoption_studio": _roles(
            ("path", "external-source", "external-path"),
            ("include", "external-selector", "external-path-list"),
            ("exclude", "external-selector", "external-path-list"),
            ("only_paths", "external-selector", "external-path-list"),
            ("sources", "source", "path-or-ref-list"),
            ("ref", "source", "ref"),
        ),
        "govern_memory": _roles(
            ("selector_paths", "policy-selector", "path-list"),
            ("path", "source", "path-or-ref"),
            ("paths", "source", "path-or-ref-list"),
        ),
        "manage_memory_file": _roles(
            ("path", "source-destination", "path"),
            ("old_path", "source", "path"),
            ("new_path", "destination", "path"),
            ("trash_path", "recovery-source", "path"),
            ("restore_path", "recovery-destination", "path"),
        ),
        "query_dataset": _roles(("path", "dataset", "path")),
        "read_media": _roles(("path", "media-source", "path")),
    }
)


def internal_state_registry() -> tuple[InternalStateDescriptor, ...]:
    """Return the immutable version-1 descriptor registry."""

    return _REGISTRY


@contextmanager
def _owner_authority_scope(command_name: str) -> Iterator[None]:
    """Install the closed authority owned by one dispatcher command invocation."""

    descriptor_ids = frozenset(
        descriptor.id
        for descriptor in _REGISTRY
        if descriptor.authority_enabled and descriptor.owning_command == command_name
    )
    if not descriptor_ids:
        yield
        return
    authority = _OwnerAuthority(_OWNER_AUTHORITY_SEAL, descriptor_ids)
    token = _ACTIVE_OWNER_AUTHORITY.set(authority)
    try:
        yield
    finally:
        _ACTIVE_OWNER_AUTHORITY.reset(token)


@contextmanager
def _subsystem_authority_scope(owner: str) -> Iterator[None]:
    """Install the exact closed authority for one registered private owner."""

    descriptor_ids = frozenset(
        descriptor.id
        for descriptor in _REGISTRY
        if descriptor.authority_enabled
        and descriptor.owning_command is None
        and descriptor.owner == owner
    )
    if not descriptor_ids:
        yield
        return
    authority = _OwnerAuthority(_OWNER_AUTHORITY_SEAL, descriptor_ids)
    token = _ACTIVE_OWNER_AUTHORITY.set(authority)
    try:
        yield
    finally:
        _ACTIVE_OWNER_AUTHORITY.reset(token)


def path_roles_for_command(command_name: str) -> tuple[PathRole, ...]:
    """Return the immutable path/ref roles for one canonical or product route."""

    return _COMMAND_PATH_ROLES.get(command_name, ())


def _role_values(role: PathRole, value: object) -> tuple[object, ...]:
    if value is None or role.value_kind.startswith("external-"):
        return ()
    if role.value_kind == "ref":
        return ()
    if role.value_kind.endswith("-list") or role.value_kind == "path-list":
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(value)
    return (value,)


def _reserved_after_bounded_dot_collapse(value: object) -> PathClassification | None:
    """Route a latent reserved alias without accepting a non-canonical path.

    The pure classifier continues to reject every dot segment.  Dispatcher
    routing additionally needs to recognize ``Notes/../_Governance`` before a
    leaf or lease is reached, while leaving ``../outside`` to the owning leaf's
    stable outside-vault error.  Collapse only within the KB-relative boundary;
    an attempted escape has no reserved routing result.
    """

    if not isinstance(value, (str, os.PathLike)):
        return None
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    text = unicodedata.normalize("NFKC", raw).replace("\\", "/")
    if text.startswith("/") or _DRIVE_RE.match(text) or ":" in text:
        return None
    parts = text.split("/")
    if not parts or any(not part for part in parts):
        return None
    knowledge_base = unicodedata.normalize("NFKC", kb_dirname()).casefold()
    if parts[0].casefold() == knowledge_base:
        parts = parts[1:]
    collapsed: list[str] = []
    for part in parts:
        if part == ".":
            continue
        if part == "..":
            if not collapsed:
                return None
            collapsed.pop()
            continue
        collapsed.append(part)
    if not collapsed:
        return None
    routed = classify_logical("/".join(collapsed))
    return routed if routed.disposition is PathDisposition.RESERVED else None


def reserved_preflight(command: object, kwargs: Mapping[str, object]) -> ReservedPathHit | None:
    """Run existence-independent logical routing before dispatch or lease work.

    Canonical references are classified after trusted reference resolution.  This
    function never opens the reference index and therefore cannot turn preflight
    into an existence oracle.
    """

    roles = getattr(command, "path_roles", ())
    for role in roles:
        if not isinstance(role, PathRole) or role.argument not in kwargs:
            continue
        for value in _role_values(role, kwargs[role.argument]):
            if isinstance(value, str) and value.casefold().startswith("exomem://"):
                continue
            classified = classify_logical(value)
            if classified.disposition is PathDisposition.RESERVED:
                return ReservedPathHit(role, classified)
            if classified.disposition is PathDisposition.INVALID:
                collapsed = _reserved_after_bounded_dot_collapse(value)
                if collapsed is not None:
                    return ReservedPathHit(role, collapsed)
    return None


def mutation_remediation(descriptor_id: str | None) -> str:
    descriptor = next(
        (descriptor for descriptor in _REGISTRY if descriptor.id == descriptor_id),
        None,
    )
    if descriptor is not None and descriptor.owning_command == "govern_memory":
        return "Use govern_memory's reviewed governance lifecycle."
    if descriptor is not None and descriptor.owning_command == "consolidate_memory":
        return "No public command owns consolidation state until consolidate_memory ships."
    return "Use the owning subsystem's bounded control operation."


def _leaf_spelling(value: object) -> tuple[str, str]:
    classified = classify_logical(value)
    if classified.disposition is PathDisposition.RESERVED:
        raise ReservedPathLeafError("RESERVED_PATH")
    if classified.disposition is PathDisposition.INVALID:
        raise ReservedPathLeafError("UNSAFE_PATH")
    assert isinstance(value, (str, os.PathLike))
    relative = unicodedata.normalize("NFKC", os.fspath(value)).replace("\\", "/")
    parent, separator, leaf = relative.rpartition("/")
    if not separator:
        parent, leaf = ".", relative
    if not leaf:
        raise ReservedPathLeafError("UNSAFE_PATH")
    return parent, leaf


def read_generic_bytes(
    vault_root: Path,
    value: object,
    *,
    identities: IdentityCatalogue | None = None,
) -> GenericFileSnapshot:
    """Read one ordinary file through the retained no-follow leaf.

    A generic path with multiple names is structurally ambiguous: one of those
    names may be a private-state name, so generic acquisition refuses it even
    when the caller supplied an ordinary-looking spelling. Named private
    identities published by owners are refused independently of link count.
    """

    with _generic_identity_catalogue_scope(
        vault_root, value, identities=identities
    ) as current:
        return _read_generic_bytes_held(vault_root, value, identities=current)


def _read_generic_bytes_held(
    vault_root: Path,
    value: object,
    *,
    identities: IdentityCatalogue,
) -> GenericFileSnapshot:

    parent_path, leaf = _leaf_spelling(value)
    acquired = held_fs.acquire(Path(vault_root))
    if not acquired.ok:
        raise ReservedPathLeafError("CAPABILITY_UNAVAILABLE")
    with acquired.require() as filesystem:
        parent_result = filesystem.parent(parent_path)
        if not parent_result.ok:
            code = parent_result.error.code if parent_result.error is not None else "IO_REFUSED"
            raise ReservedPathLeafError(code)
        with parent_result.require() as parent:
            _refuse_private_identity(parent.identity, identities)
            file_result = filesystem.file(parent, leaf)
            if not file_result.ok:
                code = file_result.error.code if file_result.error is not None else "IO_REFUSED"
                raise ReservedPathLeafError(code)
            with file_result.require() as file:
                _require_current_generic_directory(filesystem, parent)
                descriptor_id = identities.descriptor_for(file.identity) if identities else None
                if descriptor_id is not None or file.identity.link_count != 1:
                    raise ReservedPathLeafError("RESERVED_PATH")
                read_result = filesystem.read(file)
                if not read_result.ok:
                    code = read_result.error.code if read_result.error is not None else "IO_REFUSED"
                    raise ReservedPathLeafError(code)
                descriptor = getattr(file, "descriptor", None)
                if not isinstance(descriptor, int):
                    raise ReservedPathLeafError("IO_REFUSED")
                try:
                    mtime = os.fstat(descriptor).st_mtime
                except OSError as error:
                    raise ReservedPathLeafError("IO_REFUSED") from error
                _require_current_generic_directory(filesystem, parent)
                return GenericFileSnapshot(read_result.require(), file.identity, mtime)


def inspect_generic_file(
    vault_root: Path,
    value: object,
    *,
    identities: IdentityCatalogue | None = None,
) -> held_fs.StableIdentity:
    """Acquire and classify one generic regular file without reading its bytes."""

    with _generic_identity_catalogue_scope(
        vault_root, value, identities=identities
    ) as current:
        return _inspect_generic_file_held(vault_root, value, identities=current)


def inspect_generic_path(
    vault_root: Path,
    value: object,
    *,
    identities: IdentityCatalogue | None = None,
) -> held_fs.StableIdentity:
    """Acquire and classify one generic file or directory without following aliases."""

    with _generic_identity_catalogue_scope(
        vault_root, value, identities=identities
    ) as current:
        parent_path, leaf = _leaf_spelling(value)
        acquired = held_fs.acquire(Path(vault_root))
        if not acquired.ok:
            raise ReservedPathLeafError("CAPABILITY_UNAVAILABLE")
        with acquired.require() as filesystem:
            parent_result = filesystem.parent(parent_path)
            if not parent_result.ok:
                code = (
                    parent_result.error.code
                    if parent_result.error is not None
                    else "IO_REFUSED"
                )
                raise ReservedPathLeafError(code)
            with parent_result.require() as parent:
                _refuse_private_identity(parent.identity, current)
                _require_current_generic_directory(filesystem, parent)
                file_result = filesystem.file(parent, leaf)
                if file_result.ok:
                    with file_result.require() as file:
                        _require_current_generic_directory(filesystem, parent)
                        _refuse_private_identity(file.identity, current)
                        return file.identity

                assert isinstance(value, (str, os.PathLike))
                relative = unicodedata.normalize(
                    "NFKC", os.fspath(value)
                ).replace("\\", "/")
                directory_result = filesystem.parent(relative)
                if directory_result.ok:
                    with directory_result.require() as directory:
                        _require_current_generic_directory(filesystem, directory)
                        _refuse_private_identity(directory.identity, current)
                        return directory.identity

                file_code = (
                    file_result.error.code
                    if file_result.error is not None
                    else "IO_REFUSED"
                )
                directory_code = (
                    directory_result.error.code
                    if directory_result.error is not None
                    else "IO_REFUSED"
                )
                if file_code == directory_code == "MISSING":
                    raise ReservedPathLeafError("MISSING")
                raise ReservedPathLeafError(
                    "UNSAFE_PATH"
                    if "UNSAFE_PATH" in {file_code, directory_code}
                    else "IO_REFUSED"
                )


def publish_generic_bytes(
    vault_root: Path,
    value: object,
    data: bytes,
    *,
    expected_identity: held_fs.StableIdentity | None,
    identities: IdentityCatalogue | None = None,
) -> held_fs.StableIdentity:
    """Publish one ordinary file through a retained parent and exact leaf CAS.

    ``expected_identity=None`` is create-only.  Callers replacing a file must
    first acquire it through :func:`inspect_generic_file` and provide that
    exact identity; an appearance, disappearance, alias, or link-count change
    between planning and publication is refused.
    """

    if not isinstance(data, bytes):
        raise TypeError("generic byte publication requires bytes")
    with _generic_identity_catalogue_scope(
        vault_root, value, identities=identities
    ) as current:
        parent_path, leaf = _leaf_spelling(value)
        acquired = held_fs.acquire(Path(vault_root))
        if not acquired.ok:
            raise ReservedPathLeafError("CAPABILITY_UNAVAILABLE")
        with acquired.require() as filesystem:
            parent_result = filesystem.parent(
                parent_path,
                create=True,
                access="mutate",
            )
            if not parent_result.ok:
                code = (
                    parent_result.error.code
                    if parent_result.error is not None
                    else "IO_REFUSED"
                )
                raise ReservedPathLeafError(code)
            with parent_result.require() as parent:
                _refuse_private_identity(parent.identity, current)
                _require_current_generic_directory(filesystem, parent)
                existing = filesystem.file(parent, leaf)
                if existing.ok:
                    with existing.require() as file:
                        _refuse_private_identity(file.identity, current)
                        if (
                            expected_identity is None
                            or file.identity != expected_identity
                        ):
                            raise ReservedPathLeafError("IDENTITY_CHANGED")
                elif existing.error is None or existing.error.code != "MISSING":
                    code = (
                        existing.error.code
                        if existing.error is not None
                        else "IO_REFUSED"
                    )
                    raise ReservedPathLeafError(code)
                elif expected_identity is not None:
                    raise ReservedPathLeafError("IDENTITY_CHANGED")

                _require_current_generic_directory(filesystem, parent)
                published = held_fs.publish_bytes(
                    filesystem,
                    parent,
                    leaf,
                    data,
                    expected_identity=expected_identity,
                )
                if not published.ok:
                    code = (
                        published.error.code
                        if published.error is not None
                        else "IO_REFUSED"
                    )
                    raise ReservedPathLeafError(code)
                identity = published.require()
                _refuse_private_identity(identity, current)
                _require_current_generic_directory(filesystem, parent)
                return identity


def _inspect_generic_file_held(
    vault_root: Path,
    value: object,
    *,
    identities: IdentityCatalogue,
) -> held_fs.StableIdentity:

    parent_path, leaf = _leaf_spelling(value)
    acquired = held_fs.acquire(Path(vault_root))
    if not acquired.ok:
        raise ReservedPathLeafError("CAPABILITY_UNAVAILABLE")
    with acquired.require() as filesystem:
        parent_result = filesystem.parent(parent_path)
        if not parent_result.ok:
            code = parent_result.error.code if parent_result.error is not None else "IO_REFUSED"
            raise ReservedPathLeafError(code)
        with parent_result.require() as parent:
            _refuse_private_identity(parent.identity, identities)
            file_result = filesystem.file(parent, leaf)
            if not file_result.ok:
                code = file_result.error.code if file_result.error is not None else "IO_REFUSED"
                raise ReservedPathLeafError(code)
            with file_result.require() as file:
                _require_current_generic_directory(filesystem, parent)
                _refuse_private_identity(file.identity, identities)
                return file.identity


def unlink_generic_file(
    vault_root: Path,
    value: object,
    *,
    expected_identity: held_fs.StableIdentity | None = None,
    identities: IdentityCatalogue | None = None,
) -> None:
    """Remove one exact ordinary file through its retained parent and leaf."""

    with _generic_identity_catalogue_scope(
        vault_root, value, identities=identities
    ) as current:
        _unlink_generic_file_held(
            vault_root,
            value,
            expected_identity=expected_identity,
            identities=current,
        )


def _unlink_generic_file_held(
    vault_root: Path,
    value: object,
    *,
    expected_identity: held_fs.StableIdentity | None,
    identities: IdentityCatalogue,
) -> None:

    parent_path, leaf = _leaf_spelling(value)
    acquired = held_fs.acquire(Path(vault_root))
    if not acquired.ok:
        raise ReservedPathLeafError("CAPABILITY_UNAVAILABLE")
    with acquired.require() as filesystem:
        parent_result = filesystem.parent(parent_path)
        if not parent_result.ok:
            code = (
                parent_result.error.code
                if parent_result.error is not None
                else "IO_REFUSED"
            )
            raise ReservedPathLeafError(code)
        with parent_result.require() as parent:
            _refuse_private_identity(parent.identity, identities)
            file_result = filesystem.file(parent, leaf, access="mutate")
            if not file_result.ok:
                code = (
                    file_result.error.code
                    if file_result.error is not None
                    else "IO_REFUSED"
                )
                raise ReservedPathLeafError(code)
            with file_result.require() as file:
                _require_current_generic_directory(filesystem, parent)
                _refuse_private_identity(file.identity, identities)
                if expected_identity is not None and file.identity != expected_identity:
                    raise ReservedPathLeafError("IDENTITY_CHANGED")
                removed = filesystem.unlink(file)
                if not removed.ok:
                    code = (
                        removed.error.code
                        if removed.error is not None
                        else "IO_REFUSED"
                    )
                    raise ReservedPathLeafError(code)
            missing = filesystem.file(parent, leaf)
            if missing.ok:
                missing.require().close()
                raise ReservedPathLeafError("IDENTITY_CHANGED")
            if missing.error is None or missing.error.code != "MISSING":
                raise ReservedPathLeafError("IO_REFUSED")
            _require_current_generic_directory(filesystem, parent)


def _refuse_private_identity(
    identity: held_fs.StableIdentity,
    identities: IdentityCatalogue | None,
) -> None:
    descriptor_id = identities.descriptor_for(identity) if identities else None
    if descriptor_id is not None or (
        identity.kind == "file" and identity.link_count != 1
    ):
        raise ReservedPathLeafError("RESERVED_PATH")


def _require_current_generic_directory(
    filesystem: held_fs.HeldFilesystem,
    directory: held_fs.HeldDirectory,
) -> None:
    """Refuse an observable parent-name exchange at the generic leaf boundary."""

    current = filesystem.validate_directory(
        directory,
        require_name=bool(getattr(directory, "named", False)),
    )
    if current.ok:
        return
    code = current.error.code if current.error is not None else "IO_REFUSED"
    raise ReservedPathLeafError(code)


def move_generic_path(
    vault_root: Path,
    source: object,
    destination: object,
    *,
    source_kind: str,
    identities: IdentityCatalogue | None = None,
) -> None:
    """Move one generic file or directory through retained source/destination handles."""

    with _generic_identity_catalogue_scope(
        vault_root, source, destination, identities=identities
    ) as current:
        _move_generic_path_held(
            vault_root,
            source,
            destination,
            source_kind=source_kind,
            identities=current,
        )


def _move_generic_path_held(
    vault_root: Path,
    source: object,
    destination: object,
    *,
    source_kind: str,
    identities: IdentityCatalogue,
) -> None:

    source_parent_path, source_leaf = _leaf_spelling(source)
    destination_parent_path, destination_leaf = _leaf_spelling(destination)
    if source_kind not in {"file", "directory"}:
        raise ReservedPathLeafError("UNSAFE_PATH")
    assert isinstance(source, (str, os.PathLike))
    source_relative = unicodedata.normalize("NFKC", os.fspath(source)).replace("\\", "/")
    acquired = held_fs.acquire(Path(vault_root))
    if not acquired.ok:
        raise ReservedPathLeafError("CAPABILITY_UNAVAILABLE")
    with acquired.require() as filesystem:
        if source_kind == "file":
            source_parent_result = filesystem.parent(source_parent_path)
            if not source_parent_result.ok:
                code = (
                    source_parent_result.error.code
                    if source_parent_result.error is not None
                    else "IO_REFUSED"
                )
                raise ReservedPathLeafError(code)
            with source_parent_result.require() as source_parent:
                _refuse_private_identity(source_parent.identity, identities)
                source_result = filesystem.file(source_parent, source_leaf, access="mutate")
                if not source_result.ok:
                    code = source_result.error.code if source_result.error is not None else "IO_REFUSED"
                    raise ReservedPathLeafError(code)
                with source_result.require() as source_file:
                    _refuse_private_identity(source_file.identity, identities)
                    _require_current_generic_directory(filesystem, source_parent)
                    destination_parent_result = filesystem.parent(
                        destination_parent_path, create=True
                    )
                    if not destination_parent_result.ok:
                        code = (
                            destination_parent_result.error.code
                            if destination_parent_result.error is not None
                            else "IO_REFUSED"
                        )
                        raise ReservedPathLeafError(code)
                    with destination_parent_result.require() as destination_parent:
                        _refuse_private_identity(destination_parent.identity, identities)
                        _require_current_generic_directory(
                            filesystem, destination_parent
                        )
                        destination_result = filesystem.file(
                            destination_parent, destination_leaf
                        )
                        if destination_result.ok:
                            with destination_result.require() as destination_file:
                                _require_current_generic_directory(
                                    filesystem, destination_parent
                                )
                                _refuse_private_identity(
                                    destination_file.identity, identities
                                )
                            raise ReservedPathLeafError("DESTINATION_EXISTS")
                        if (
                            destination_result.error is None
                            or destination_result.error.code != "MISSING"
                        ):
                            code = (
                                destination_result.error.code
                                if destination_result.error is not None
                                else "IO_REFUSED"
                            )
                            raise ReservedPathLeafError(code)
                        moved = filesystem.rename(
                            source_file, destination_parent, destination_leaf
                        )
                        if not moved.ok:
                            code = moved.error.code if moved.error is not None else "IO_REFUSED"
                            raise ReservedPathLeafError(code)
                        _require_current_generic_directory(filesystem, source_parent)
                        _require_current_generic_directory(
                            filesystem, destination_parent
                        )
            return

        source_result = filesystem.parent(source_relative, access="mutate")
        if not source_result.ok:
            code = source_result.error.code if source_result.error is not None else "IO_REFUSED"
            raise ReservedPathLeafError(code)
        with source_result.require() as source_directory:
            _refuse_private_identity(source_directory.identity, identities)
            _require_current_generic_directory(filesystem, source_directory)
            enumerated = filesystem.enumerate(source_directory)
            if not enumerated.ok:
                code = enumerated.error.code if enumerated.error is not None else "IO_REFUSED"
                raise ReservedPathLeafError(code)
            for record in enumerated.require():
                child = f"{source_relative.rstrip('/')}/{record.relative_path}"
                if classify_logical(child).blocked:
                    raise ReservedPathLeafError("RESERVED_PATH")
                _refuse_private_identity(record.identity, identities)
            destination_parent_result = filesystem.parent(
                destination_parent_path, create=True
            )
            if not destination_parent_result.ok:
                code = (
                    destination_parent_result.error.code
                    if destination_parent_result.error is not None
                    else "IO_REFUSED"
                )
                raise ReservedPathLeafError(code)
            with destination_parent_result.require() as destination_parent:
                _refuse_private_identity(destination_parent.identity, identities)
                _require_current_generic_directory(filesystem, source_directory)
                _require_current_generic_directory(filesystem, destination_parent)
                moved = filesystem.rename_directory(
                    source_directory, destination_parent, destination_leaf
                )
                if not moved.ok:
                    code = moved.error.code if moved.error is not None else "IO_REFUSED"
                    raise ReservedPathLeafError(code)
                _require_current_generic_directory(filesystem, destination_parent)


def read_generic_tree(
    vault_root: Path,
    value: object,
    *,
    identities: IdentityCatalogue | None = None,
) -> tuple[GenericTreeFile, ...]:
    """Enumerate and read an ordinary directory under one retained root anchor."""

    with _generic_identity_catalogue_scope(
        vault_root, value, identities=identities
    ) as current:
        return _read_generic_tree_held(vault_root, value, identities=current)


def _read_generic_tree_held(
    vault_root: Path,
    value: object,
    *,
    identities: IdentityCatalogue,
) -> tuple[GenericTreeFile, ...]:

    classified = classify_logical(value)
    if classified.disposition is PathDisposition.RESERVED:
        raise ReservedPathLeafError("RESERVED_PATH")
    if classified.disposition is PathDisposition.INVALID:
        raise ReservedPathLeafError("UNSAFE_PATH")
    assert isinstance(value, (str, os.PathLike))
    relative = unicodedata.normalize("NFKC", os.fspath(value)).replace("\\", "/")
    acquired = held_fs.acquire(Path(vault_root))
    if not acquired.ok:
        raise ReservedPathLeafError("CAPABILITY_UNAVAILABLE")
    snapshots: list[GenericTreeFile] = []
    with acquired.require() as filesystem:
        source_result = filesystem.parent(relative)
        if not source_result.ok:
            code = source_result.error.code if source_result.error is not None else "IO_REFUSED"
            raise ReservedPathLeafError(code)
        with source_result.require() as source_directory:
            _refuse_private_identity(source_directory.identity, identities)
            _require_current_generic_directory(filesystem, source_directory)
            enumerated = filesystem.enumerate(source_directory)
            if not enumerated.ok:
                code = enumerated.error.code if enumerated.error is not None else "IO_REFUSED"
                raise ReservedPathLeafError(code)
            for record in enumerated.require():
                child = f"{relative.rstrip('/')}/{record.relative_path}"
                if classify_logical(child).blocked:
                    raise ReservedPathLeafError("RESERVED_PATH")
                _refuse_private_identity(record.identity, identities)
                if record.identity.kind != "file":
                    continue
                parent_path, leaf = _leaf_spelling(child)
                parent_result = filesystem.parent(parent_path)
                if not parent_result.ok:
                    code = (
                        parent_result.error.code
                        if parent_result.error is not None
                        else "IO_REFUSED"
                    )
                    raise ReservedPathLeafError(code)
                with parent_result.require() as parent:
                    file_result = filesystem.file(parent, leaf)
                    if not file_result.ok:
                        code = (
                            file_result.error.code
                            if file_result.error is not None
                            else "IO_REFUSED"
                        )
                        raise ReservedPathLeafError(code)
                    with file_result.require() as file:
                        _require_current_generic_directory(filesystem, parent)
                        if file.identity != record.identity:
                            raise ReservedPathLeafError("IDENTITY_CHANGED")
                        _refuse_private_identity(file.identity, identities)
                        read_result = filesystem.read(file)
                        if not read_result.ok:
                            code = (
                                read_result.error.code
                                if read_result.error is not None
                                else "IO_REFUSED"
                            )
                            raise ReservedPathLeafError(code)
                        descriptor = getattr(file, "descriptor", None)
                        if not isinstance(descriptor, int):
                            raise ReservedPathLeafError("IO_REFUSED")
                        try:
                            mtime = os.fstat(descriptor).st_mtime
                        except OSError as error:
                            raise ReservedPathLeafError("IO_REFUSED") from error
                        snapshots.append(
                            GenericTreeFile(
                                record.relative_path,
                                GenericFileSnapshot(
                                    read_result.require(), file.identity, mtime
                                ),
                            )
                        )
                        _require_current_generic_directory(filesystem, parent)
            _require_current_generic_directory(filesystem, source_directory)
    return tuple(snapshots)


def _list_candidate_records(
    filesystem: held_fs.HeldFilesystem,
    source_directory: held_fs.HeldDirectory,
    source_relative: str,
    *,
    identities: IdentityCatalogue | None,
) -> tuple[held_fs.SagaRecord, ...]:
    """Walk safe ordinary children without following or exposing aliases."""

    records: list[held_fs.SagaRecord] = []

    def walk(directory: held_fs.HeldDirectory, prefix: str) -> None:
        _require_current_generic_directory(filesystem, directory)
        children = filesystem.children(directory)
        if not children.ok:
            code = children.error.code if children.error is not None else "IO_REFUSED"
            raise ReservedPathLeafError(code)
        for record in children.require():
            relative = (
                f"{prefix}/{record.relative_path}"
                if prefix
                else record.relative_path
            )
            records.append(held_fs.SagaRecord(relative, record.identity))
            child = (
                relative
                if source_relative == "."
                else f"{source_relative.rstrip('/')}/{relative}"
            )
            classification = classify_logical(child)
            descriptor_id = identities.descriptor_for(record.identity) if identities else None
            blocked = (
                classification.blocked
                or descriptor_id is not None
                or (record.identity.kind == "file" and record.identity.link_count != 1)
            )
            if record.identity.kind != "directory" or blocked:
                continue
            retained = filesystem.parent(child)
            if not retained.ok:
                code = retained.error.code if retained.error is not None else "IO_REFUSED"
                raise ReservedPathLeafError(code)
            with retained.require() as child_directory:
                if child_directory.identity != record.identity:
                    raise ReservedPathLeafError("IDENTITY_CHANGED")
                _require_current_generic_directory(filesystem, child_directory)
                walk(child_directory, relative)
        _require_current_generic_directory(filesystem, directory)

    walk(source_directory, "")
    return tuple(records)


def list_generic_tree(
    vault_root: Path,
    value: object,
    *,
    recursive: bool = True,
    identities: IdentityCatalogue | None = None,
) -> tuple[GenericTreeEntry, ...]:
    """Enumerate an ordinary tree while structurally removing private state."""

    with _generic_identity_catalogue_scope(
        vault_root, value, identities=identities
    ) as current:
        return _list_generic_tree_held(
            vault_root,
            value,
            recursive=recursive,
            identities=current,
        )


def _list_generic_tree_held(
    vault_root: Path,
    value: object,
    *,
    recursive: bool,
    identities: IdentityCatalogue,
) -> tuple[GenericTreeEntry, ...]:

    if value == ".":
        relative = "."
    else:
        classified = classify_logical(value)
        if classified.disposition is PathDisposition.RESERVED:
            raise ReservedPathLeafError("RESERVED_PATH")
        if classified.disposition is PathDisposition.INVALID:
            raise ReservedPathLeafError("UNSAFE_PATH")
        assert isinstance(value, (str, os.PathLike))
        relative = unicodedata.normalize("NFKC", os.fspath(value)).replace("\\", "/")
    acquired = held_fs.acquire(Path(vault_root))
    if not acquired.ok:
        raise ReservedPathLeafError("CAPABILITY_UNAVAILABLE")
    visible: list[GenericTreeEntry] = []
    hidden_prefixes: list[str] = []
    with acquired.require() as filesystem:
        source_result = filesystem.parent(relative)
        if not source_result.ok:
            code = source_result.error.code if source_result.error is not None else "IO_REFUSED"
            raise ReservedPathLeafError(code)
        with source_result.require() as source_directory:
            _refuse_private_identity(source_directory.identity, identities)
            _require_current_generic_directory(filesystem, source_directory)
            if recursive:
                records = _list_candidate_records(
                    filesystem,
                    source_directory,
                    relative,
                    identities=identities,
                )
            else:
                enumerated = filesystem.children(source_directory)
                if not enumerated.ok:
                    code = (
                        enumerated.error.code
                        if enumerated.error is not None
                        else "IO_REFUSED"
                    )
                    raise ReservedPathLeafError(code)
                records = enumerated.require()
            for record in records:
                if any(
                    record.relative_path == prefix
                    or record.relative_path.startswith(f"{prefix}/")
                    for prefix in hidden_prefixes
                ):
                    continue
                child = (
                    record.relative_path
                    if relative == "."
                    else f"{relative.rstrip('/')}/{record.relative_path}"
                )
                child_classification = classify_logical(child)
                descriptor_id = identities.descriptor_for(record.identity) if identities else None
                if child_classification.blocked or descriptor_id is not None or (
                    record.identity.kind == "file" and record.identity.link_count != 1
                ):
                    if record.identity.kind == "directory":
                        hidden_prefixes.append(record.relative_path)
                    continue

                parent_path, leaf = _leaf_spelling(child)
                if record.identity.kind == "directory":
                    directory_result = filesystem.parent(child)
                    if not directory_result.ok:
                        raise ReservedPathLeafError(
                            directory_result.error.code
                            if directory_result.error is not None
                            else "IO_REFUSED"
                        )
                    with directory_result.require() as directory:
                        if directory.identity != record.identity:
                            raise ReservedPathLeafError("IDENTITY_CHANGED")
                        _require_current_generic_directory(filesystem, directory)
                        descriptor = getattr(directory, "descriptor", None)
                        try:
                            mtime = (
                                os.fstat(descriptor).st_mtime
                                if isinstance(descriptor, int)
                                else None
                            )
                        except OSError as error:
                            raise ReservedPathLeafError("IO_REFUSED") from error
                    visible.append(
                        GenericTreeEntry(
                            record.relative_path,
                            record.identity,
                            None,
                            mtime,
                        )
                    )
                    continue

                parent_result = filesystem.parent(parent_path)
                if not parent_result.ok:
                    raise ReservedPathLeafError(
                        parent_result.error.code
                        if parent_result.error is not None
                        else "IO_REFUSED"
                    )
                with parent_result.require() as parent:
                    file_result = filesystem.file(parent, leaf)
                    if not file_result.ok:
                        raise ReservedPathLeafError(
                            file_result.error.code
                            if file_result.error is not None
                            else "IO_REFUSED"
                        )
                    with file_result.require() as file:
                        _require_current_generic_directory(filesystem, parent)
                        if file.identity != record.identity:
                            raise ReservedPathLeafError("IDENTITY_CHANGED")
                        descriptor = getattr(file, "descriptor", None)
                        if not isinstance(descriptor, int):
                            raise ReservedPathLeafError("IO_REFUSED")
                        try:
                            info = os.fstat(descriptor)
                        except OSError as error:
                            raise ReservedPathLeafError("IO_REFUSED") from error
                        markdown: bytes | None = None
                        if leaf.casefold().endswith(".md"):
                            read_result = filesystem.read(file)
                            if not read_result.ok:
                                raise ReservedPathLeafError(
                                    read_result.error.code
                                    if read_result.error is not None
                                    else "IO_REFUSED"
                                )
                            markdown = read_result.require()
                        visible.append(
                            GenericTreeEntry(
                                record.relative_path,
                                record.identity,
                                info.st_size,
                                info.st_mtime,
                                markdown,
                            )
                        )
                        _require_current_generic_directory(filesystem, parent)
            _require_current_generic_directory(filesystem, source_directory)
    return tuple(visible)


def _invalid(reason: str) -> PathClassification:
    return PathClassification(PathDisposition.INVALID, reason=reason)


def _normalized_parts(value: object) -> tuple[str, ...] | PathClassification:
    if not isinstance(value, (str, os.PathLike)):
        return _invalid("path is not textual")
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return _invalid("path spelling is invalid")
    text = unicodedata.normalize("NFKC", raw)
    if text != text.strip():
        return _invalid("path spelling is non-canonical")
    if text.startswith(("/", "\\")) or _DRIVE_RE.match(text):
        return _invalid("absolute path spelling is forbidden")
    text = text.replace("\\", "/")
    if text.startswith("//") or "//" in text:
        return _invalid("ambiguous separators are forbidden")
    parts = tuple(text.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        return _invalid("dot or empty components are forbidden")
    if any(":" in part for part in parts):
        return _invalid("alternate stream or URI spelling is forbidden")
    if any(part.endswith((".", " ")) for part in parts):
        return _invalid("platform-aliased components are forbidden")
    folded = tuple(unicodedata.normalize("NFKC", part).casefold() for part in parts)
    knowledge_base = unicodedata.normalize("NFKC", kb_dirname()).casefold()
    if folded and folded[0] == knowledge_base:
        folded = folded[1:]
    return folded


def classify_logical(value: object) -> PathClassification:
    """Classify one KB-relative or vault-prefixed spelling without touching disk."""

    parts = _normalized_parts(value)
    if isinstance(parts, PathClassification):
        return parts
    canonical = kb_dirname() if not parts else f"{kb_dirname()}/{'/'.join(parts)}"
    matches = tuple(descriptor for descriptor in _REGISTRY if descriptor.matches(parts))
    if not matches:
        return PathClassification(PathDisposition.ORDINARY, canonical=canonical)
    if len(matches) != 1:
        return _invalid("internal-state registry is ambiguous")
    return PathClassification(
        PathDisposition.RESERVED,
        descriptor_id=matches[0].id,
        canonical=canonical,
        reason="path is reserved for an owning subsystem",
    )


_IdentityKey = tuple[int, int, str]


def _identity_key(identity: held_fs.StableIdentity) -> _IdentityKey:
    return identity.device, identity.inode, identity.kind


@dataclass(frozen=True, slots=True)
class IdentityCatalogue:
    """Stable identities published for currently reachable private state."""

    identities: Mapping[_IdentityKey, str]
    published_paths: Mapping[
        _IdentityKey, tuple[tuple[str, str], ...]
    ] | None = None
    vault_root: Path | None = None

    @classmethod
    def from_vault(cls, vault_root: Path) -> IdentityCatalogue:
        """Acquire current private identities through the no-follow substrate.

        This constructor is for startup/recovery and tests.  Live owners publish
        their identities while holding their own coordination primitive.
        """

        root = Path(vault_root)
        acquired = held_fs.acquire(root)
        if not acquired.ok:
            raise RuntimeError("reserved identity catalogue could not acquire the vault")
        published: dict[_IdentityKey, str] = {}
        published_paths: dict[_IdentityKey, list[tuple[str, str]]] = {}

        def add(
            identity: held_fs.StableIdentity,
            descriptor_id: str,
            logical_name: str,
        ) -> None:
            key = _identity_key(identity)
            prior = published.get(key)
            if prior is not None and prior != descriptor_id:
                raise RuntimeError("one private filesystem identity maps to multiple owners")
            published[key] = descriptor_id
            published_paths.setdefault(key, []).append(
                (descriptor_id, logical_name)
            )

        def walk(
            filesystem: held_fs.HeldFilesystem,
            directory: held_fs.HeldDirectory,
            prefix: str,
            seen_directories: set[_IdentityKey],
        ) -> None:
            children = filesystem.children(directory)
            if not children.ok:
                raise RuntimeError(
                    "reserved identity catalogue could not enumerate the KB"
                )
            for record in children.require():
                relative = (
                    f"{prefix}/{record.relative_path}"
                    if prefix
                    else record.relative_path
                )
                logical = classify_logical(relative)
                if logical.disposition is PathDisposition.RESERVED:
                    descriptor_id = logical.descriptor_id
                    assert descriptor_id is not None
                    add(
                        record.identity,
                        descriptor_id,
                        f"{kb_dirname()}/{relative}",
                    )
                if record.identity.kind != "directory":
                    continue
                key = _identity_key(record.identity)
                if key in seen_directories:
                    raise RuntimeError(
                        "reserved identity catalogue encountered a directory cycle"
                    )
                child = filesystem.parent(f"{kb_dirname()}/{relative}")
                if not child.ok:
                    raise RuntimeError(
                        "reserved identity catalogue could not retain a KB directory"
                    )
                with child.require() as retained:
                    if retained.identity != record.identity:
                        raise RuntimeError(
                            "reserved identity catalogue observed directory drift"
                        )
                    seen_directories.add(key)
                    try:
                        walk(filesystem, retained, relative, seen_directories)
                    finally:
                        seen_directories.remove(key)

        with acquired.require() as filesystem:
            kb_parent = filesystem.parent(kb_dirname())
            if not kb_parent.ok:
                if kb_parent.error is not None and kb_parent.error.code == "MISSING":
                    return cls(
                        MappingProxyType({}),
                        MappingProxyType({}),
                        Path(os.path.abspath(root)),
                    )
                raise RuntimeError("reserved identity catalogue could not acquire the KB")
            with kb_parent.require() as knowledge_base:
                walk(
                    filesystem,
                    knowledge_base,
                    "",
                    {_identity_key(knowledge_base.identity)},
                )
        return cls(
            MappingProxyType(published),
            MappingProxyType(
                {key: tuple(values) for key, values in published_paths.items()}
            ),
            Path(os.path.abspath(root)),
        )

    def descriptor_for(self, identity: held_fs.StableIdentity) -> str | None:
        key = _identity_key(identity)
        descriptor_id = self.identities.get(key)
        if descriptor_id is None or self.published_paths is None:
            return descriptor_id
        root = self.vault_root
        if root is None:
            return None
        for candidate_descriptor, logical_name in self.published_paths.get(key, ()):
            try:
                current = _lstat_identity(root / logical_name)
            except (FileNotFoundError, OSError):
                continue
            if _identity_key(current) == key:
                return candidate_descriptor
        return None


_PUBLISHED_IDENTITY_LOCK = threading.RLock()
_PUBLISHED_OWNER_IDENTITIES: dict[
    str, dict[str, dict[str, held_fs.StableIdentity]]
] = {}
_BASELINE_IDENTITY_CATALOGUES: dict[str, IdentityCatalogue] = {}


def _baseline_identity_catalogue(vault_root: Path) -> IdentityCatalogue:
    """Build one coordinated inventory of private identities per vault/process."""

    vault_key = _vault_identity_key(vault_root)
    with _PUBLISHED_IDENTITY_LOCK:
        cached = _BASELINE_IDENTITY_CATALOGUES.get(vault_key)
    if cached is not None:
        return cached

    with _identity_coordination_scope(vault_root):
        with _PUBLISHED_IDENTITY_LOCK:
            cached = _BASELINE_IDENTITY_CATALOGUES.get(vault_key)
        if cached is not None:
            return cached
        catalogue = IdentityCatalogue.from_vault(vault_root)
        with _PUBLISHED_IDENTITY_LOCK:
            _BASELINE_IDENTITY_CATALOGUES[vault_key] = catalogue
        return catalogue


def _publish_owner_identities(
    vault_root: Path,
    descriptor_id: str,
    identities: Mapping[str, held_fs.StableIdentity],
) -> None:
    """Atomically replace one owner's published stable-identity set.

    The caller must already hold both its exact opaque owner authority and the
    cooperative identity boundary.  Logical names are evidence that the
    identities belong to this descriptor; generic leaves consume only the
    stable keys and never trust these names as an authorization path.
    """

    if not owner_authorized(descriptor_id):
        raise RuntimeError("private identity publication lacks exact owner authority")
    if not _identity_coordination_active(vault_root, descriptor_id):
        raise RuntimeError("private identity publication lacks coordination")

    published: dict[str, held_fs.StableIdentity] = {}
    seen_keys: set[_IdentityKey] = set()
    for logical_name, identity in identities.items():
        classification = classify_logical(logical_name)
        if (
            classification.disposition is not PathDisposition.RESERVED
            or classification.descriptor_id != descriptor_id
        ):
            raise RuntimeError("private identity publication does not match its descriptor")
        if identity.kind not in {"file", "directory"} or (
            identity.kind == "file" and identity.link_count != 1
        ):
            raise RuntimeError("private identity publication is ambiguous")
        key = _identity_key(identity)
        if key in seen_keys:
            raise RuntimeError("private identity publication is ambiguous")
        seen_keys.add(key)
        published[logical_name] = identity

    vault_key = _vault_identity_key(vault_root)
    with _PUBLISHED_IDENTITY_LOCK:
        owners = _PUBLISHED_OWNER_IDENTITIES.setdefault(vault_key, {})
        owners[descriptor_id] = published


def _published_identity_catalogue(vault_root: Path) -> IdentityCatalogue:
    """Return currently reachable identities from process-local publications.

    WAL/SHM and rebuild files can disappear when their last owner handle closes,
    and their inode/file-id can be reused immediately.  Revalidate each
    publication at its reserved no-follow spelling before admitting the key to
    a generic-leaf snapshot; a stale ephemeral identity must never reserve an
    unrelated later file.
    """

    vault_key = _vault_identity_key(vault_root)
    with _PUBLISHED_IDENTITY_LOCK:
        by_descriptor = {
            descriptor_id: dict(identities)
            for descriptor_id, identities in _PUBLISHED_OWNER_IDENTITIES.get(
                vault_key, {}
            ).items()
        }

    root = Path(os.path.abspath(vault_root))
    merged: dict[_IdentityKey, str] = {}
    published_paths: dict[_IdentityKey, list[tuple[str, str]]] = {}
    for descriptor_id, identities in by_descriptor.items():
        for logical_name, expected in identities.items():
            key = _identity_key(expected)
            prior = merged.get(key)
            if prior is not None and prior != descriptor_id:
                # A link-count-one identity can only move between names over
                # time. Keep every candidate and let descriptor_for validate
                # which reserved spelling still owns it now.
                pass
            else:
                merged[key] = descriptor_id
            published_paths.setdefault(key, []).append(
                (descriptor_id, logical_name)
            )
    return IdentityCatalogue(
        MappingProxyType(merged),
        MappingProxyType(
            {key: tuple(values) for key, values in published_paths.items()}
        ),
        root,
    )


def _reachable_owner_publications(
    vault_root: Path,
    descriptor_id: str,
) -> dict[str, held_fs.StableIdentity]:
    """Return one owner's still-reachable canonical publications.

    SQLite WAL/SHM files disappear on last close and their filesystem identity
    can be reused immediately.  Publication updates must therefore prune stale
    logical rows before merging a newly installed identity; otherwise an inode
    reused for the new primary can appear to alias an already-gone SHM entry and
    make the owner's own atomic publish fail as ambiguous.
    """
    if not _identity_coordination_active(vault_root, descriptor_id):
        raise RuntimeError("private identity refresh lacks coordination")

    root = Path(os.path.abspath(vault_root))
    vault_key = _vault_identity_key(root)
    with _PUBLISHED_IDENTITY_LOCK:
        candidates = dict(
            _PUBLISHED_OWNER_IDENTITIES.get(vault_key, {}).get(
                descriptor_id,
                {},
            )
        )

    current: dict[str, held_fs.StableIdentity] = {}
    for logical_name, expected in candidates.items():
        try:
            observed = _lstat_identity(root / logical_name)
        except (FileNotFoundError, OSError):
            continue
        if (
            _identity_key(observed) != _identity_key(expected)
            or observed.kind not in {"file", "directory"}
            or (observed.kind == "file" and observed.link_count != 1)
        ):
            continue
        current[logical_name] = observed
    return current


def _merge_identity_catalogues(
    *catalogues: IdentityCatalogue,
) -> IdentityCatalogue:
    merged: dict[_IdentityKey, str] = {}
    for catalogue in catalogues:
        for key, descriptor_id in catalogue.identities.items():
            if catalogue.published_paths is not None:
                candidate = held_fs.StableIdentity(
                    key[0], key[1], key[2], 1
                )
                current_descriptor = catalogue.descriptor_for(candidate)
                if current_descriptor is None:
                    continue
                descriptor_id = current_descriptor
            prior = merged.get(key)
            if prior is not None and prior != descriptor_id:
                raise RuntimeError(
                    "one private filesystem identity maps to multiple owners"
                )
            merged[key] = descriptor_id
    return IdentityCatalogue(MappingProxyType(merged))


def _needs_fresh_physical_catalogue(values: tuple[object, ...]) -> bool:
    """Whether a spelling may be a Windows short-name physical alias."""

    if os.name != "nt":
        return False
    return any(
        isinstance(value, (str, os.PathLike))
        and "~" in unicodedata.normalize("NFKC", os.fspath(value))
        for value in values
    )


@contextmanager
def _generic_identity_catalogue_scope(
    vault_root: Path,
    *values: object,
    identities: IdentityCatalogue | None = None,
) -> Iterator[IdentityCatalogue]:
    """Pin the cooperative boundary and one private-identity snapshot."""

    needs_fresh = _needs_fresh_physical_catalogue(values)
    if identities is None and not needs_fresh:
        yield _merge_identity_catalogues(
            _baseline_identity_catalogue(vault_root),
            _published_identity_catalogue(vault_root),
        )
        return

    with _identity_coordination_scope(vault_root):
        current = identities or _merge_identity_catalogues(
            _baseline_identity_catalogue(vault_root),
            _published_identity_catalogue(vault_root),
        )
        if needs_fresh:
            current = _merge_identity_catalogues(
                current,
                IdentityCatalogue.from_vault(vault_root),
            )
        yield current


def _publish_sqlite_owner_family(
    vault_root: Path,
    database: Path,
    descriptor_id: str,
    connection: object,
) -> None:
    """Publish a canonical owner's reachable SQLite family before unlock."""

    if not owner_authorized(descriptor_id):
        raise RuntimeError("SQLite identity publication lacks exact owner authority")
    if not _identity_coordination_active(vault_root, descriptor_id):
        raise RuntimeError("SQLite identity publication lacks coordination")

    root = Path(os.path.abspath(vault_root))
    target = Path(os.path.abspath(database))
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise RuntimeError("SQLite identity publication target is outside the vault") from error
    classification = classify_logical(relative.as_posix())
    if (
        classification.disposition is not PathDisposition.RESERVED
        or classification.descriptor_id != descriptor_id
    ):
        raise RuntimeError("SQLite identity publication target is not owner-bound")

    if not callable(getattr(connection, "execute", None)):
        raise RuntimeError("SQLite identity publication lacks a live connection")

    acquired = held_fs.acquire(root)
    if not acquired.ok:
        raise RuntimeError("SQLite identity publication cannot acquire the vault")
    with acquired.require() as filesystem:
        parent_relative = relative.parent.as_posix()
        parent_result = filesystem.parent(
            parent_relative if parent_relative != "." else "."
        )
        if not parent_result.ok:
            raise RuntimeError("SQLite identity publication cannot retain its parent")
        with parent_result.require() as parent:
            prefix = "" if relative.parent == Path(".") else f"{relative.parent.as_posix()}/"

            def publish(family: dict[str, held_fs.StableIdentity]) -> None:
                _publish_owner_identities(
                    root,
                    descriptor_id,
                    {f"{prefix}{name}": identity for name, identity in family.items()},
                )

            wal_or_shm_reachable = False
            for suffix in ("-wal", "-shm"):
                probe = filesystem.file(parent, f"{relative.name}{suffix}")
                if probe.ok:
                    probe.require().close()
                    wal_or_shm_reachable = True
                elif probe.error is None or probe.error.code != "MISSING":
                    raise RuntimeError("SQLite identity family is unavailable")

            if wal_or_shm_reachable:
                if not _owner_directory_is_current(filesystem, parent):
                    raise RuntimeError(
                        "SQLite identity publication parent changed"
                    )
                result = held_fs.publish_sqlite_identities(
                    filesystem,
                    parent,
                    relative.name,
                    publish,
                )
                if not result.ok:
                    raise RuntimeError(
                        "SQLite WAL identity family is not completely reachable"
                    )
                if not _owner_directory_is_current(filesystem, parent):
                    raise RuntimeError(
                        "SQLite identity publication parent changed"
                    )
                return

            family: dict[str, held_fs.StableIdentity] = {}
            for suffix in ("", "-journal", "-wal", "-shm"):
                name = f"{relative.name}{suffix}"
                file_result = filesystem.file(parent, name)
                if not file_result.ok:
                    if suffix and file_result.error is not None and file_result.error.code == "MISSING":
                        continue
                    raise RuntimeError("SQLite identity family is unavailable")
                with file_result.require() as file:
                    family[name] = file.identity
            if not _owner_directory_is_current(filesystem, parent):
                raise RuntimeError("SQLite identity publication parent changed")
            publish(family)


def _publish_owner_bytes(
    vault_root: Path,
    path: Path,
    descriptor_id: str,
    data: bytes,
) -> held_fs.StableIdentity:
    """Create or replace one exact private owner file through held handles."""

    if not owner_authorized(descriptor_id):
        raise RuntimeError("private byte publication lacks exact owner authority")
    if not isinstance(data, bytes):
        raise TypeError("private byte publication requires bytes")

    root = Path(os.path.abspath(vault_root))
    target = Path(os.path.abspath(path))
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise RuntimeError("private byte publication target is outside the vault") from error
    classification = classify_logical(relative.as_posix())
    if (
        classification.disposition is not PathDisposition.RESERVED
        or classification.descriptor_id != descriptor_id
    ):
        raise RuntimeError("private byte publication target is not owner-bound")

    # The caller-provided vault root is the trust anchor, not a descendant
    # selected by vault content.  Standalone first-write flows may not have
    # materialized it yet; create that anchor, then let held_fs reject any
    # unsafe/raced object before a private descendant is touched.
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeError("private byte publication cannot create the vault") from error

    with _identity_coordination_scope(root, descriptor_ids=(descriptor_id,)):
        acquired = held_fs.acquire(root)
        if not acquired.ok:
            raise RuntimeError("private byte publication cannot acquire the vault")
        with acquired.require() as filesystem:
            parent_text = relative.parent.as_posix()
            parent_result = filesystem.parent(
                parent_text if parent_text != "." else ".",
                create=True,
                access="mutate",
            )
            if not parent_result.ok:
                raise RuntimeError("private byte publication cannot retain its parent")
            with parent_result.require() as parent:
                expected: held_fs.StableIdentity | None = None
                current = filesystem.file(parent, relative.name)
                if current.ok:
                    with current.require() as existing:
                        if existing.identity.link_count != 1:
                            raise RuntimeError(
                                "private byte publication target is ambiguous"
                            )
                        expected = existing.identity
                elif current.error is None or current.error.code != "MISSING":
                    raise RuntimeError("private byte publication target is unsafe")

                if not _owner_directory_is_current(filesystem, parent):
                    raise RuntimeError("private byte publication parent changed")

                result = held_fs.publish_bytes(
                    filesystem,
                    parent,
                    relative.name,
                    data,
                    expected_identity=expected,
                )
                if not result.ok:
                    raise RuntimeError("private byte publication was refused")
                identity = result.require()
                if not _owner_directory_is_current(filesystem, parent):
                    raise RuntimeError("private byte publication parent changed")

        current = _reachable_owner_publications(root, descriptor_id)
        current[relative.as_posix()] = identity
        _publish_owner_identities(root, descriptor_id, current)
        return identity


def _read_owner_bytes(
    vault_root: Path,
    path: Path,
    descriptor_id: str,
    *,
    limit: int,
) -> bytes:
    """Read one exact private owner file through a retained no-follow handle."""

    if not owner_authorized(descriptor_id):
        raise RuntimeError("private byte read lacks exact owner authority")
    if type(limit) is not int or limit < 0:
        raise TypeError("private byte read requires a non-negative integer limit")

    root = Path(os.path.abspath(vault_root))
    target = Path(os.path.abspath(path))
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise RuntimeError("private byte read target is outside the vault") from error
    classification = classify_logical(relative.as_posix())
    if (
        classification.disposition is not PathDisposition.RESERVED
        or classification.descriptor_id != descriptor_id
    ):
        raise RuntimeError("private byte read target is not owner-bound")

    try:
        root.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(target) from None
    except OSError as error:
        raise OSError("private byte read cannot inspect the vault") from error

    with _identity_coordination_scope(root, descriptor_ids=(descriptor_id,)):
        acquired = held_fs.acquire(root)
        if not acquired.ok:
            raise OSError("private byte read cannot acquire the vault")
        with acquired.require() as filesystem:
            parent_text = relative.parent.as_posix()
            parent_result = filesystem.parent(
                parent_text if parent_text != "." else "."
            )
            if not parent_result.ok:
                if parent_result.error is not None and parent_result.error.code == "MISSING":
                    raise FileNotFoundError(target)
                raise OSError("private byte read cannot retain its parent")
            with parent_result.require() as parent:
                file_result = filesystem.file(parent, relative.name)
                if not file_result.ok:
                    if file_result.error is not None and file_result.error.code == "MISSING":
                        raise FileNotFoundError(target)
                    raise OSError("private byte read target is unsafe")
                with file_result.require() as file:
                    if file.identity.link_count != 1:
                        raise OSError("private byte read target is ambiguous")
                    if not _owner_directory_is_current(filesystem, parent):
                        raise OSError("private byte read parent changed")
                    descriptor = getattr(file, "descriptor", None)
                    if not isinstance(descriptor, int):
                        raise OSError("private byte read target is unavailable")
                    try:
                        size = os.fstat(descriptor).st_size
                    except OSError as error:
                        raise OSError("private byte read target is unavailable") from error
                    if size > limit:
                        raise OSError("private byte read exceeds its limit")
                    result = filesystem.read(file)
                    if not result.ok:
                        raise OSError("private byte read was refused")
                    data = result.require()
                    if len(data) > limit:
                        raise OSError("private byte read exceeds its limit")
                    identity = file.identity
                    current = filesystem.file(parent, relative.name)
                    if not current.ok:
                        raise OSError("private byte read target changed")
                    with current.require() as current_file:
                        if current_file.identity != identity:
                            raise OSError("private byte read target changed")
                    if not _owner_directory_is_current(filesystem, parent):
                        raise OSError("private byte read parent changed")

        current = _reachable_owner_publications(root, descriptor_id)
        current[relative.as_posix()] = identity
        _publish_owner_identities(root, descriptor_id, current)
        return data


def _owner_relative_path(
    vault_root: Path,
    path: Path,
    descriptor_id: str,
    *,
    operation: str,
) -> tuple[Path, Path]:
    if not owner_authorized(descriptor_id):
        raise RuntimeError(f"private {operation} lacks exact owner authority")
    root = Path(os.path.abspath(vault_root))
    target = Path(os.path.abspath(path))
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise RuntimeError(f"private {operation} target is outside the vault") from error
    classification = classify_logical(relative.as_posix())
    if (
        classification.disposition is not PathDisposition.RESERVED
        or classification.descriptor_id != descriptor_id
    ):
        raise RuntimeError(f"private {operation} target is not owner-bound")
    return root, relative


def _owner_directory_is_current(
    filesystem: held_fs.HeldFilesystem,
    directory: held_fs.HeldDirectory,
) -> bool:
    current = filesystem.validate_directory(
        directory,
        require_name=bool(getattr(directory, "named", False)),
    )
    return current.ok


@contextmanager
def _sqlite_owner_target_scope(
    vault_root: Path,
    database: Path,
    descriptor_id: str,
    *,
    create: bool,
) -> Iterator[Path]:
    """Retain one exact private SQLite leaf across open, setup, and publication.

    SQLite's stdlib API accepts a pathname rather than an already-open file.
    The cooperating command boundary therefore holds the no-follow vault and
    parent handles, plus the existing leaf when present, while SQLite resolves
    that pathname.  The same identity is re-opened and checked before the
    coordination guard may be released.  No pathname-only fallback exists.
    """

    if not _identity_coordination_active(vault_root, descriptor_id):
        raise RuntimeError("private SQLite target lacks coordination")
    root, relative = _owner_relative_path(
        vault_root,
        database,
        descriptor_id,
        operation="SQLite target",
    )
    if create:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RuntimeError("private SQLite target cannot create the vault") from error
    else:
        try:
            root.lstat()
        except FileNotFoundError:
            raise FileNotFoundError(database) from None
        except OSError as error:
            raise RuntimeError("private SQLite target cannot inspect the vault") from error

    acquired = held_fs.acquire(root)
    if not acquired.ok:
        raise RuntimeError("private SQLite target cannot acquire the vault")
    with acquired.require() as filesystem:
        parent_text = relative.parent.as_posix()
        parent_result = filesystem.parent(
            parent_text if parent_text != "." else ".",
            create=create,
            access="read",
        )
        if not parent_result.ok:
            if (
                not create
                and parent_result.error is not None
                and parent_result.error.code == "MISSING"
            ):
                raise FileNotFoundError(database)
            raise RuntimeError("private SQLite target cannot retain its parent")
        with parent_result.require() as parent:
            # Retain identity without DELETE access: a live Windows SQLite
            # handle does not necessarily share DELETE with another opener.
            existing_result = filesystem.file(
                parent,
                relative.name,
                access="read",
            )
            existing: held_fs.HeldFile | None = None
            expected: held_fs.StableIdentity | None = None
            if existing_result.ok:
                existing = existing_result.require()
                expected = existing.identity
                if expected.kind != "file" or expected.link_count != 1:
                    existing.close()
                    raise RuntimeError("private SQLite target is ambiguous")
            elif (
                existing_result.error is None
                or existing_result.error.code != "MISSING"
                or not create
            ):
                if (
                    not create
                    and existing_result.error is not None
                    and existing_result.error.code == "MISSING"
                ):
                    raise FileNotFoundError(database)
                raise RuntimeError("private SQLite target is unsafe")

            try:
                if not _owner_directory_is_current(filesystem, parent):
                    raise RuntimeError("private SQLite target parent changed")
                try:
                    yield root / relative
                except BaseException:
                    raise
                else:
                    if not _owner_directory_is_current(filesystem, parent):
                        raise RuntimeError("private SQLite target parent changed")
                    current_result = filesystem.file(
                        parent,
                        relative.name,
                        access="read",
                    )
                    if not current_result.ok:
                        raise RuntimeError("private SQLite target changed during open")
                    with current_result.require() as current:
                        if current.identity.kind != "file" or current.identity.link_count != 1:
                            raise RuntimeError("private SQLite target is ambiguous")
                        if expected is not None and _identity_key(current.identity) != _identity_key(
                            expected
                        ):
                            raise RuntimeError("private SQLite target changed during open")
            finally:
                if existing is not None:
                    existing.close()


def _replace_published_owner_path(
    vault_root: Path,
    descriptor_id: str,
    relative: Path,
    identity: held_fs.StableIdentity | None,
) -> None:
    current = _reachable_owner_publications(vault_root, descriptor_id)
    if identity is None:
        current.pop(relative.as_posix(), None)
    else:
        current[relative.as_posix()] = identity
    _publish_owner_identities(vault_root, descriptor_id, current)


def _move_owner_file(
    vault_root: Path,
    source: Path,
    source_descriptor_id: str,
    destination: Path,
    destination_descriptor_id: str,
    *,
    replace: bool,
) -> held_fs.StableIdentity:
    """Rename one exact private file between owner-bound names."""

    root, source_relative = _owner_relative_path(
        vault_root,
        source,
        source_descriptor_id,
        operation="move",
    )
    destination_root, destination_relative = _owner_relative_path(
        vault_root,
        destination,
        destination_descriptor_id,
        operation="move",
    )
    if destination_root != root:
        raise RuntimeError("private move targets different vault roots")

    with _identity_coordination_scope(
        root,
        descriptor_ids=(source_descriptor_id, destination_descriptor_id),
    ):
        acquired = held_fs.acquire(root)
        if not acquired.ok:
            raise OSError("private move cannot acquire the vault")
        with acquired.require() as filesystem:
            source_parent_text = source_relative.parent.as_posix()
            destination_parent_text = destination_relative.parent.as_posix()
            source_parent_result = filesystem.parent(
                source_parent_text if source_parent_text != "." else ".",
                access="mutate",
            )
            if not source_parent_result.ok:
                if (
                    source_parent_result.error is not None
                    and source_parent_result.error.code == "MISSING"
                ):
                    raise FileNotFoundError(source)
                raise OSError("private move source parent is unsafe")
            with source_parent_result.require() as source_parent:
                destination_parent_result = filesystem.parent(
                    destination_parent_text
                    if destination_parent_text != "."
                    else ".",
                    create=True,
                    access="mutate",
                )
                if not destination_parent_result.ok:
                    raise OSError("private move destination parent is unsafe")
                with destination_parent_result.require() as destination_parent:
                    source_result = filesystem.file(
                        source_parent,
                        source_relative.name,
                        access="mutate",
                    )
                    if not source_result.ok:
                        if (
                            source_result.error is not None
                            and source_result.error.code == "MISSING"
                        ):
                            raise FileNotFoundError(source)
                        raise OSError("private move source is unsafe")
                    with source_result.require() as source_file:
                        if source_file.identity.link_count != 1:
                            raise OSError("private move source is ambiguous")
                        destination_result = filesystem.file(
                            destination_parent,
                            destination_relative.name,
                        )
                        if destination_result.ok:
                            with destination_result.require() as destination_file:
                                if destination_file.identity.link_count != 1:
                                    raise OSError(
                                        "private move destination is ambiguous"
                                    )
                            if not replace:
                                raise FileExistsError(destination)
                        elif (
                            destination_result.error is None
                            or destination_result.error.code != "MISSING"
                        ):
                            raise OSError("private move destination is unsafe")

                        if not _owner_directory_is_current(
                            filesystem, source_parent
                        ) or not _owner_directory_is_current(
                            filesystem, destination_parent
                        ):
                            raise OSError("private move parent changed")

                        moved = filesystem.rename(
                            source_file,
                            destination_parent,
                            destination_relative.name,
                            replace=replace,
                        )
                        if not moved.ok:
                            raise OSError("private move was refused")
                        expected = source_file.identity

                    if not _owner_directory_is_current(
                        filesystem, source_parent
                    ) or not _owner_directory_is_current(
                        filesystem, destination_parent
                    ):
                        raise OSError("private move parent changed")

                    installed = filesystem.file(
                        destination_parent,
                        destination_relative.name,
                    )
                    if not installed.ok:
                        raise OSError("private move destination is unavailable")
                    with installed.require() as installed_file:
                        if installed_file.identity != expected:
                            raise OSError("private move destination changed")
                        identity = installed_file.identity

        if source_descriptor_id == destination_descriptor_id:
            current = _reachable_owner_publications(root, source_descriptor_id)
            current.pop(source_relative.as_posix(), None)
            current[destination_relative.as_posix()] = identity
            _publish_owner_identities(root, source_descriptor_id, current)
        else:
            _replace_published_owner_path(
                root,
                source_descriptor_id,
                source_relative,
                None,
            )
            _replace_published_owner_path(
                root,
                destination_descriptor_id,
                destination_relative,
                identity,
            )
        return identity


def _remove_owner_file(
    vault_root: Path,
    path: Path,
    descriptor_id: str,
    *,
    missing_ok: bool = False,
) -> bool:
    """Unlink one exact private file through its retained owner-bound handle."""

    root, relative = _owner_relative_path(
        vault_root,
        path,
        descriptor_id,
        operation="remove",
    )
    with _identity_coordination_scope(root, descriptor_ids=(descriptor_id,)):
        acquired = held_fs.acquire(root)
        if not acquired.ok:
            if missing_ok:
                return False
            raise OSError("private remove cannot acquire the vault")
        with acquired.require() as filesystem:
            parent_text = relative.parent.as_posix()
            parent_result = filesystem.parent(
                parent_text if parent_text != "." else ".",
                access="mutate",
            )
            if not parent_result.ok:
                if (
                    missing_ok
                    and parent_result.error is not None
                    and parent_result.error.code == "MISSING"
                ):
                    _replace_published_owner_path(root, descriptor_id, relative, None)
                    return False
                raise OSError("private remove parent is unsafe")
            with parent_result.require() as parent:
                file_result = filesystem.file(parent, relative.name, access="mutate")
                if not file_result.ok:
                    if (
                        file_result.error is not None
                        and file_result.error.code == "MISSING"
                    ):
                        if not missing_ok:
                            raise FileNotFoundError(path)
                        _replace_published_owner_path(root, descriptor_id, relative, None)
                        return False
                    raise OSError("private remove target is unsafe")
                with file_result.require() as file:
                    if file.identity.link_count != 1:
                        raise OSError("private remove target is ambiguous")
                    if not _owner_directory_is_current(filesystem, parent):
                        raise OSError("private remove parent changed")
                    removed = filesystem.unlink(file)
                    if not removed.ok:
                        raise OSError("private remove was refused")
                current = filesystem.file(parent, relative.name)
                if current.ok:
                    current.require().close()
                    raise OSError("private remove target changed")
                if current.error is None or current.error.code != "MISSING":
                    raise OSError("private remove target is unavailable")
                if not _owner_directory_is_current(filesystem, parent):
                    raise OSError("private remove parent changed")

        _replace_published_owner_path(root, descriptor_id, relative, None)
        return True


def _lstat_identity(path: Path) -> held_fs.StableIdentity:
    info = path.lstat()
    if path.is_dir() and not path.is_symlink():
        kind = "directory"
    elif path.is_file() and not path.is_symlink():
        kind = "file"
    else:
        kind = "other"
    return held_fs.StableIdentity(info.st_dev, info.st_ino, kind, info.st_nlink)


def classify_physical(
    vault_root: Path,
    value: object,
    *,
    identities: IdentityCatalogue,
) -> PathClassification:
    """Conservatively classify a stable pre-existing physical target.

    This observes aliases for routing only.  The consuming leaf must still use
    the same held handle for its operation; no result here authorizes a pathname
    reopen.
    """

    logical = classify_logical(value)
    if logical.blocked:
        return logical
    root = Path(vault_root)
    assert isinstance(value, (str, os.PathLike))
    physical_text = unicodedata.normalize("NFKC", os.fspath(value)).replace("\\", "/")
    physical_parts = tuple(physical_text.split("/"))
    if physical_parts[0].casefold() == unicodedata.normalize("NFKC", kb_dirname()).casefold():
        physical_parts = (kb_dirname(), *physical_parts[1:])
    else:
        physical_parts = (kb_dirname(), *physical_parts)
    candidate = root.joinpath(*physical_parts)

    try:
        relative_parts = candidate.relative_to(root).parts
    except ValueError:
        return _invalid("physical target is outside the vault")

    current = root
    for part in relative_parts:
        current = current / part
        try:
            is_alias = current.is_symlink() or bool(
                getattr(os.path, "isjunction", lambda _path: False)(current)
            )
        except OSError:
            return _invalid("physical target could not be classified")
        if not is_alias:
            continue
        try:
            resolved = current.resolve(strict=True)
            resolved_relative = resolved.relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            return _invalid("physical alias is unsafe")
        resolved_result = classify_logical(resolved_relative)
        if resolved_result.disposition is PathDisposition.RESERVED:
            return resolved_result
        return _invalid("physical alias is unsupported by held traversal")

    try:
        resolved = candidate.resolve(strict=True)
        resolved_relative = resolved.relative_to(root.resolve()).as_posix()
    except FileNotFoundError:
        return logical
    except (OSError, ValueError):
        return _invalid("physical target could not be resolved safely")
    resolved_result = classify_logical(resolved_relative)
    if resolved_result.disposition is PathDisposition.RESERVED:
        return resolved_result

    try:
        identity = _lstat_identity(candidate)
    except FileNotFoundError:
        return logical
    except OSError:
        return _invalid("physical target identity is unavailable")
    descriptor_id = identities.descriptor_for(identity)
    if descriptor_id is not None:
        return PathClassification(
            PathDisposition.RESERVED,
            descriptor_id=descriptor_id,
            canonical=logical.canonical,
            reason="filesystem identity belongs to private state",
        )
    return logical


def _validate_registry() -> None:
    ids = [descriptor.id for descriptor in _REGISTRY]
    if len(ids) != len(set(ids)) or any(not descriptor.owner for descriptor in _REGISTRY):
        raise RuntimeError("internal-state registry is invalid")


_validate_registry()
