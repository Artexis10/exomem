"""Pure semantic page-state evaluation over an immutable corpus snapshot."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from . import (
    access,
    activation,
    activation_manifest,
    freshness,
    memory_refs,
    memory_schema,
    relation_registry,
    semantic_authoring,
    semantic_language_registry,
    semantic_units,
    vault,
)
from . import find as find_module
from .memory_refs import ID_FIELD, normalize_id
from .semantic_units import SemanticUnitDocument, SourceSpan

_REVIEW_KINDS = frozenset({"reviewed_none", "bootstrap"})
_DISPOSITION_KINDS = frozenset(
    {"qualifying_relation", "reviewed_none", "bootstrap", "missing", "stale"}
)
_TARGET_STATUSES = frozenset({"resolved", "unresolved", "ambiguous"})
_EXCLUDED_FAMILIES = frozenset(
    {"link", "citation", "derivation", "evidence", "mention", "observation", "provenance"}
)
_AUTHORED_SCHEMA_ORIGINS = frozenset({"markdown_relation", "semantic_relation"})
_CREATE_LIKE = frozenset({"create", "replacement", "adoption_compile", "tier2_create"})
_GRANDFATHERED_OPERATIONS = frozenset(
    {"edit", "move", "observe", "recover", "tier2_overwrite", "tier2_append"}
)
COMPILED_DESTINATIONS = MappingProxyType(
    {
        "experiment": "Notes/Experiments",
        "failure": "Notes/Failures",
        "insight": "Notes/Insights",
        "pattern": "Notes/Patterns",
        "production-log": "Notes/Productions",
        "research-note": "Notes/Research",
    }
)
COMPILED_TYPES = frozenset(COMPILED_DESTINATIONS)
_COMPILED_ROOT_TYPES = MappingProxyType(
    {
        destination.rsplit("/", 1)[-1].casefold(): page_type
        for page_type, destination in COMPILED_DESTINATIONS.items()
    }
)
_INACTIVE_STATUSES = frozenset({"archived", "draft", "dropped", "planned", "superseded"})
_SEMANTIC_UNIT_EXEMPT_PARTS = frozenset(
    {"sources", "evidence", "_trash", "trash", "_schema", "templates", "data"}
)
_SEMANTIC_UNIT_EXEMPT_TAGS = frozenset({"hub", "snapshot"})
_SEMANTIC_UNIT_EXEMPT_SUFFIXES = (
    "-architecture",
    "-snapshot",
    "-catalog-snapshot",
)
_WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")

log = logging.getLogger(__name__)
_REVIEW_FINGERPRINT_UNSET = object()


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def review_content_fingerprint(page_identity: str, source: str) -> str:
    """Return the portable review fingerprint shared by model and coordinator."""
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return _canonical_hash(
        {
            "schema_version": 1,
            "page_identity": page_identity,
            "normalized_source_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        }
    )


def _stable_value_key(value: Any) -> tuple[str, str, str]:
    value_type = type(value)
    return (value_type.__module__, value_type.__qualname__, repr(value))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: _freeze(child)
                for key, child in sorted(value.items(), key=lambda item: _stable_value_key(item[0]))
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(child) for child in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(child) for child in value]
    if isinstance(value, frozenset):
        return [_thaw(child) for child in sorted(value, key=_stable_value_key)]
    return value


def _mapping_of_tuples(
    values: Mapping[str, Iterable[Any]],
) -> Mapping[str, tuple[Any, ...]]:
    return MappingProxyType({key: tuple(values[key]) for key in sorted(values)})


@dataclass(frozen=True, slots=True)
class SemanticPageState:
    path: str
    identity_kind: str
    identity: str
    source_hash: str
    language_registry_hash: str
    relation_registry_hash: str
    review_fingerprint: str | None
    frontmatter: Mapping[Any, Any]
    page_type: str | None
    projects: tuple[str, ...]
    status: str | None
    title: str
    document: SemanticUnitDocument
    eligible_governed: bool
    eligible_compiled: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "frontmatter", _freeze(dict(self.frontmatter)))
        object.__setattr__(self, "projects", tuple(sorted(set(self.projects))))

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "identity_kind": self.identity_kind,
            "identity": self.identity,
            "source_hash": self.source_hash,
            "review_fingerprint": self.review_fingerprint,
            "frontmatter": _thaw(self.frontmatter),
            "page_type": self.page_type,
            "projects": list(self.projects),
            "status": self.status,
            "title": self.title,
            "semantic_units": [unit.to_dict() for unit in self.document.units],
            "canonical_section_present": self.document.canonical_section_present,
            "canonical_bullet_count": self.document.canonical_bullet_count,
            "eligible_governed": self.eligible_governed,
            "eligible_compiled": self.eligible_compiled,
        }


@dataclass(frozen=True, slots=True)
class StableIdentityEntry:
    path: str
    page_identity: str | None

    def __post_init__(self) -> None:
        path = PurePosixPath(str(self.path).replace("\\", "/")).as_posix()
        if (
            not path
            or path.startswith("/")
            or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)
            or PurePosixPath(path).suffix.casefold() != ".md"
        ):
            raise ValueError("stable identity path must be safe vault-relative POSIX")
        identity = self.page_identity
        if identity is not None and (
            type(identity) is not str or normalize_id(identity) != identity
        ):
            raise ValueError("stable page identity must be a canonical UUID")
        object.__setattr__(self, "path", path)


@dataclass(frozen=True, slots=True)
class StableIdentityCensus:
    entries: tuple[StableIdentityEntry, ...]
    paths_by_identity: Mapping[str, tuple[str, ...]] = field(init=False)

    def __post_init__(self) -> None:
        entries = tuple(
            sorted(
                tuple(self.entries),
                key=lambda item: (item.path, item.page_identity or ""),
            )
        )
        paths = [entry.path for entry in entries]
        if len(paths) != len(set(paths)):
            raise ValueError("stable identity census contains a duplicate path")
        grouped: dict[str, list[str]] = {}
        for entry in entries:
            if entry.page_identity is not None:
                grouped.setdefault(entry.page_identity, []).append(entry.path)
        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self,
            "paths_by_identity",
            MappingProxyType(
                {identity: tuple(sorted(values)) for identity, values in sorted(grouped.items())}
            ),
        )

    def with_page(self, page: SemanticPageState) -> StableIdentityCensus:
        entries = [entry for entry in self.entries if entry.path != page.path]
        entries.append(
            StableIdentityEntry(
                page.path,
                page.identity if page.identity_kind == "exomem_id" else None,
            )
        )
        return StableIdentityCensus(tuple(entries))

    @classmethod
    def from_states(
        cls,
        states: Iterable[SemanticPageState],
    ) -> StableIdentityCensus:
        """Derive stable UUID ownership from already-built page states."""
        entries: list[StableIdentityEntry] = []
        for state in states:
            raw_identity = state.frontmatter.get(ID_FIELD)
            identity: str | None = None
            if raw_identity is not None:
                if not isinstance(raw_identity, str) or normalize_id(raw_identity) is None:
                    raise ValueError(f"stable identity is invalid at {state.path}")
                identity = normalize_id(raw_identity)
            entries.append(StableIdentityEntry(state.path, identity))
        return cls(tuple(entries))

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_count": len(self.entries),
            "identity_count": len(self.paths_by_identity),
            "entries": [
                {"path": entry.path, "page_identity": entry.page_identity} for entry in self.entries
            ],
            "paths_by_identity": {
                identity: list(paths) for identity, paths in self.paths_by_identity.items()
            },
        }


@dataclass(frozen=True, slots=True)
class RelationReviewState:
    kind: str
    page_identity: str
    content_fingerprint: str
    reason: str | None = None
    reference: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _REVIEW_KINDS:
            raise ValueError(f"relation review kind must be one of {sorted(_REVIEW_KINDS)}")
        if not str(self.page_identity or "").strip():
            raise ValueError("relation review page_identity must be nonempty")
        if not str(self.content_fingerprint or "").strip():
            raise ValueError("relation review content_fingerprint must be nonempty")
        for label, value in (("reason", self.reason), ("reference", self.reference)):
            if value is not None and not str(value).strip():
                raise ValueError(f"relation review {label} must be nonempty when supplied")

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "page_identity": self.page_identity,
            "content_fingerprint": self.content_fingerprint,
            "reason": self.reason,
            "reference": self.reference,
        }


@dataclass(frozen=True, slots=True)
class RelationFact:
    identity: str
    logical_source_path: str
    logical_target_path: str
    raw_target: str
    resolved_target_path: str | None
    target_anchor: str | None
    target_alias: str | None
    authored_path: str
    authored_line: int | None
    authored_anchor: str | None
    authored_projects: tuple[str, ...]
    authored_page_type: str | None
    source_kind: str
    target_page_type: str | None
    raw_relation: str
    canonical_relation: str | None
    family: str | None
    registry_status: str
    origin: str
    authored: bool
    reviewer_accepted: bool
    target_status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "authored_projects", tuple(sorted(set(self.authored_projects))))
        if self.target_status not in _TARGET_STATUSES:
            raise ValueError(f"unsupported target status: {self.target_status}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "logical_source_path": self.logical_source_path,
            "logical_target_path": self.logical_target_path,
            "raw_target": self.raw_target,
            "resolved_target_path": self.resolved_target_path,
            "target_anchor": self.target_anchor,
            "target_alias": self.target_alias,
            "authored_path": self.authored_path,
            "authored_line": self.authored_line,
            "authored_anchor": self.authored_anchor,
            "authored_projects": list(self.authored_projects),
            "authored_page_type": self.authored_page_type,
            "source_kind": self.source_kind,
            "target_page_type": self.target_page_type,
            "raw_relation": self.raw_relation,
            "canonical_relation": self.canonical_relation,
            "family": self.family,
            "registry_status": self.registry_status,
            "origin": self.origin,
            "authored": self.authored,
            "reviewer_accepted": self.reviewer_accepted,
            "target_status": self.target_status,
        }


@dataclass(frozen=True, slots=True)
class RelationQualification:
    qualifies: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"qualifies": self.qualifies, "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class RejectedRelationFact:
    fact: RelationFact
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"fact": self.fact.as_dict(), "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class RelationDisposition:
    kind: str
    satisfied: bool
    current: bool
    qualifying_directions: tuple[str, ...] = ()
    qualifying_facts: tuple[RelationFact, ...] = ()
    rejected_facts: tuple[RejectedRelationFact, ...] = ()
    actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _DISPOSITION_KINDS:
            raise ValueError(f"unsupported relation disposition: {self.kind}")
        object.__setattr__(self, "qualifying_directions", tuple(self.qualifying_directions))
        object.__setattr__(self, "qualifying_facts", tuple(self.qualifying_facts))
        object.__setattr__(self, "rejected_facts", tuple(self.rejected_facts))
        object.__setattr__(self, "actions", tuple(self.actions))

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "satisfied": self.satisfied,
            "current": self.current,
            "qualifying_directions": list(self.qualifying_directions),
            "qualifying_facts": [fact.as_dict() for fact in self.qualifying_facts],
            "rejected_facts": [item.as_dict() for item in self.rejected_facts],
            "actions": list(self.actions),
        }


@dataclass(frozen=True, slots=True)
class ContractFinding:
    code: str
    severity: str
    path: str
    span: SourceSpan | None
    detail: str
    remediation: str
    governed_element_identity: tuple[str, ...]
    resolved_rule: tuple[str, str, str]
    contracts: tuple[str, ...] = ()
    provenance: tuple[tuple[str, str, str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "governed_element_identity",
            tuple(self.governed_element_identity),
        )
        object.__setattr__(self, "resolved_rule", tuple(self.resolved_rule))
        object.__setattr__(self, "contracts", tuple(self.contracts))
        object.__setattr__(
            self,
            "provenance",
            tuple(tuple(item) for item in self.provenance),
        )

    @property
    def key(self) -> tuple[str, tuple[str, ...], tuple[str, str, str]]:
        return (self.code, self.governed_element_identity, self.resolved_rule)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "span": self.span.to_dict() if self.span is not None else None,
            "detail": self.detail,
            "remediation": self.remediation,
            "governed_element_identity": list(self.governed_element_identity),
            "resolved_rule": list(self.resolved_rule),
            "contracts": list(self.contracts),
            "provenance": [
                {
                    "contract": contract,
                    "path": path,
                    "raw_element": raw_element,
                    "scope": scope,
                }
                for contract, path, raw_element, scope in self.provenance
            ],
        }


@dataclass(frozen=True, slots=True)
class SemanticContractResult:
    mode: str
    operation: str
    findings: tuple[ContractFinding, ...]
    errors: tuple[ContractFinding, ...]
    warnings: tuple[ContractFinding, ...]
    blocking_findings: tuple[ContractFinding, ...]
    should_block: bool
    semantic_unit_count: int
    kind_counts: tuple[tuple[str, int], ...]
    category_counts: tuple[tuple[str, int], ...]
    relation_disposition: RelationDisposition | None
    actions: tuple[str, ...]
    compact_unit_count: int = 0
    rich_unit_count: int = 0

    def __post_init__(self) -> None:
        for name in (
            "findings",
            "errors",
            "warnings",
            "blocking_findings",
            "kind_counts",
            "category_counts",
            "actions",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "operation": self.operation,
            "findings": [finding.as_dict() for finding in self.findings],
            "errors": [finding.as_dict() for finding in self.errors],
            "warnings": [finding.as_dict() for finding in self.warnings],
            "blocking_findings": [finding.as_dict() for finding in self.blocking_findings],
            "should_block": self.should_block,
            "semantic_unit_count": self.semantic_unit_count,
            "compact_unit_count": self.compact_unit_count,
            "rich_unit_count": self.rich_unit_count,
            "kind_counts": dict(self.kind_counts),
            "category_counts": dict(self.category_counts),
            "relation_disposition": (
                self.relation_disposition.as_dict()
                if self.relation_disposition is not None
                else None
            ),
            "actions": list(self.actions),
        }


def normalized_compiled_type(value: object) -> str | None:
    """Normalize an authored page type for exact compiled-intent checks."""
    return activation.normalized_page_type(value)


def _path_parts(path: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in PurePosixPath(path.replace("\\", "/")).parts)


def _path_excluded_from_semantic_minimum(path: str) -> bool:
    parts = _path_parts(path)
    if any(part in _SEMANTIC_UNIT_EXEMPT_PARTS for part in parts):
        return True
    name = parts[-1] if parts else ""
    if name in {"hub.md", "index.md", "log.md"}:
        return True
    stem = PurePosixPath(name).stem
    return any(stem.endswith(suffix) for suffix in _SEMANTIC_UNIT_EXEMPT_SUFFIXES)


def canonical_compiled_destination(path: str) -> str | None:
    """Return the compiled type selected by one canonical destination, if any."""
    if _path_excluded_from_semantic_minimum(path):
        return None
    parts = _path_parts(path)
    if len(parts) < 3 or parts[:2] != ("knowledge base", "notes"):
        return None
    return _COMPILED_ROOT_TYPES.get(parts[2])


def _frontmatter_tags(page: SemanticPageState) -> frozenset[str]:
    value = page.frontmatter.get("tags")
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        values = ()
    return frozenset(str(item).strip().casefold().lstrip("#") for item in values)


def _semantic_unit_explicitly_exempt(page: SemanticPageState) -> bool:
    return bool(
        _path_excluded_from_semantic_minimum(page.path)
        or (_frontmatter_tags(page) & _SEMANTIC_UNIT_EXEMPT_TAGS)
    )


def compiled_intent(page: SemanticPageState) -> bool:
    """Apply the exact route-or-type compiled-intent definition."""
    return bool(
        canonical_compiled_destination(page.path) is not None
        or normalized_compiled_type(page.page_type) in COMPILED_TYPES
    )


def compiled_structure_finding(page: SemanticPageState) -> ContractFinding | None:
    """Return a deterministic path/type mismatch before semantic applicability."""
    destination_type = canonical_compiled_destination(page.path)
    page_type = normalized_compiled_type(page.page_type)
    if destination_type is not None and page_type != destination_type:
        return ContractFinding(
            code="COMPILED_TYPE_MISMATCH",
            severity="error",
            path=page.path,
            span=None,
            detail=(
                f"canonical {COMPILED_DESTINATIONS[destination_type]} content requires "
                f"frontmatter type {destination_type!r}"
            ),
            remediation=(
                f"Set `type: {destination_type}` or move the document outside that "
                "canonical compiled destination."
            ),
            governed_element_identity=("compiled_intent", "type"),
            resolved_rule=("semantic_authoring", "compiled_intent", "structure"),
        )
    if page_type in COMPILED_TYPES and destination_type != page_type:
        return ContractFinding(
            code="COMPILED_DESTINATION_MISMATCH",
            severity="error",
            path=page.path,
            span=None,
            detail=(
                f"compiled frontmatter type {page_type!r} is outside its canonical "
                f"{COMPILED_DESTINATIONS[page_type]} destination"
            ),
            remediation=(
                f"Move the document under `{COMPILED_DESTINATIONS[page_type]}` or use "
                "a non-compiled type appropriate to this destination."
            ),
            governed_element_identity=("compiled_intent", "destination"),
            resolved_rule=("semantic_authoring", "compiled_intent", "structure"),
        )
    return None


def compiled_structure_matches(page: SemanticPageState) -> bool:
    """Return whether compiled intent exists and path/type structure is applicable."""
    return bool(
        compiled_intent(page)
        and not _semantic_unit_explicitly_exempt(page)
        and compiled_structure_finding(page) is None
    )


def requires_semantic_unit(page: SemanticPageState) -> bool:
    """Return the exact active, writable compiled minimum-unit predicate."""
    return bool(compiled_structure_matches(page) and page.eligible_compiled)


def _missing_semantic_unit_finding(page: SemanticPageState) -> ContractFinding | None:
    if page.document.units or not compiled_structure_matches(page):
        return None
    active_required = requires_semantic_unit(page)
    inactive = normalized_compiled_type(page.status) in _INACTIVE_STATUSES
    if not active_required and not inactive:
        return None
    finding = semantic_authoring.AUTHORING_CONTRACT.findings["missing_semantic_unit"]
    return ContractFinding(
        code="missing_semantic_unit",
        severity="error" if active_required else "warning",
        path=page.path,
        span=None,
        detail=str(finding["when"]),
        remediation=(f"{finding['compact_remediation']} {finding['rich_remediation']}"),
        governed_element_identity=("semantic_units", "minimum"),
        resolved_rule=("semantic_authoring", "semantic_unit", "minimum"),
    )


def _copy_definition(
    definition: relation_registry.RelationDefinition,
) -> relation_registry.RelationDefinition:
    return relation_registry.RelationDefinition(
        key=definition.key,
        description=definition.description,
        family=definition.family,
        direction=definition.direction,
        parent=definition.parent,
        inverse=definition.inverse,
        origins=frozenset(definition.origins),
        aliases=tuple(definition.aliases),
        source_kinds=frozenset(definition.source_kinds),
        target_kinds=frozenset(definition.target_kinds),
        projects=frozenset(definition.projects),
        page_types=frozenset(definition.page_types),
        status=definition.status,
        replaced_by=definition.replaced_by,
        core=definition.core,
    )


def _copy_registry(
    registry: relation_registry.RelationRegistry,
) -> relation_registry.RelationRegistry:
    return relation_registry.RelationRegistry(
        registry.core_version,
        registry.extension_hash,
        MappingProxyType({key: _copy_definition(value) for key, value in registry.core.items()}),
        MappingProxyType(
            {key: _copy_definition(value) for key, value in registry.extensions.items()}
        ),
        MappingProxyType(dict(registry.aliases)),
        tuple(MappingProxyType(dict(finding)) for finding in registry.findings),
    )


@dataclass(frozen=True, slots=True)
class SemanticCorpusContext:
    vault_root: Path
    pages: Mapping[str, SemanticPageState]
    resolver_entries: tuple[tuple[str, str], ...]
    resolver_full_paths: frozenset[str]
    resolver_kb_stripped: frozenset[str]
    resolver_stems: Mapping[str, tuple[str, ...]]
    resolver_titles: Mapping[str, tuple[str, ...]]
    raw_target_dependencies: Mapping[str, tuple[str, ...]]
    relation_facts: tuple[RelationFact, ...]
    eligible_governed_paths: frozenset[str]
    eligible_compiled_paths: frozenset[str]
    inbound: Mapping[str, tuple[RelationFact, ...]]
    outbound: Mapping[str, tuple[RelationFact, ...]]
    activation_census: activation_manifest.ActivationCensus
    identity_census: StableIdentityCensus
    registry: relation_registry.RelationRegistry = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "pages",
            MappingProxyType({key: self.pages[key] for key in sorted(self.pages)}),
        )
        object.__setattr__(self, "resolver_entries", tuple(self.resolver_entries))
        object.__setattr__(self, "resolver_full_paths", frozenset(self.resolver_full_paths))
        object.__setattr__(self, "resolver_kb_stripped", frozenset(self.resolver_kb_stripped))
        object.__setattr__(
            self,
            "resolver_stems",
            _mapping_of_tuples(self.resolver_stems),
        )
        object.__setattr__(
            self,
            "resolver_titles",
            _mapping_of_tuples(self.resolver_titles),
        )
        object.__setattr__(
            self,
            "raw_target_dependencies",
            _mapping_of_tuples(self.raw_target_dependencies),
        )
        object.__setattr__(self, "relation_facts", tuple(self.relation_facts))
        object.__setattr__(
            self,
            "eligible_governed_paths",
            frozenset(self.eligible_governed_paths),
        )
        object.__setattr__(
            self,
            "eligible_compiled_paths",
            frozenset(self.eligible_compiled_paths),
        )
        object.__setattr__(self, "inbound", _mapping_of_tuples(self.inbound))
        object.__setattr__(self, "outbound", _mapping_of_tuples(self.outbound))
        object.__setattr__(self, "registry", _copy_registry(self.registry))

    @classmethod
    def from_states(
        cls,
        vault_root: Path,
        states: Iterable[SemanticPageState],
        *,
        registry: relation_registry.RelationRegistry,
        identity_census: StableIdentityCensus,
    ) -> SemanticCorpusContext:
        by_path: dict[str, SemanticPageState] = {}
        for state in states:
            if state.path in by_path:
                raise ValueError(f"duplicate semantic page path: {state.path}")
            by_path[state.path] = state
        return _context_from_state_map(
            Path(vault_root),
            by_path,
            _copy_registry(registry),
            StableIdentityCensus(tuple(identity_census.entries)),
        )

    def with_candidate(self, state: SemanticPageState) -> SemanticCorpusContext:
        current = self.pages.get(state.path)
        if current is not None and (
            current.title,
            current.identity_kind,
            current.identity,
            current.page_type,
            current.projects,
            current.language_registry_hash,
            current.relation_registry_hash,
        ) == (
            state.title,
            state.identity_kind,
            state.identity,
            state.page_type,
            state.projects,
            state.language_registry_hash,
            state.relation_registry_hash,
        ):
            return _context_with_stable_topology_candidate(self, state)
        pages = dict(self.pages)
        pages[state.path] = state
        return _context_from_state_map(
            self.vault_root,
            pages,
            self.registry,
            self.identity_census.with_page(state),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages": [self.pages[path].as_dict() for path in sorted(self.pages)],
            "resolver_entries": [list(entry) for entry in self.resolver_entries],
            "relation_facts": [fact.as_dict() for fact in self.relation_facts],
            "eligible_governed_paths": sorted(self.eligible_governed_paths),
            "eligible_compiled_paths": sorted(self.eligible_compiled_paths),
            "identity_census": self.identity_census.as_dict(),
            "inbound": {
                path: [fact.identity for fact in facts] for path, facts in self.inbound.items()
            },
            "outbound": {
                path: [fact.identity for fact in facts] for path, facts in self.outbound.items()
            },
        }


def _normalize_path(vault_root: Path, path: Path | str) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            value = candidate.resolve().relative_to(vault_root.resolve()).as_posix()
        except ValueError as error:
            raise ValueError("page path must be inside vault_root") from error
    else:
        value = PurePosixPath(str(path).replace("\\", "/")).as_posix().lstrip("/")
    posix = PurePosixPath(value)
    if (
        not value
        or posix.is_absolute()
        or any(part in {"", ".", ".."} for part in posix.parts)
        or posix.suffix.lower() != ".md"
    ):
        raise ValueError("page path must be a safe vault-relative POSIX Markdown path")
    return value


def _page_projects(frontmatter: Mapping[Any, Any]) -> tuple[str, ...]:
    projects: set[str] = set()
    project = frontmatter.get("project")
    if project:
        projects.add(str(project))
    attached = frontmatter.get("projects")
    if isinstance(attached, (list, tuple)):
        projects.update(str(value) for value in attached if str(value))
    elif attached:
        projects.add(str(attached))
    return tuple(sorted(projects))


def build_page_state(
    vault_root: Path,
    path: Path | str,
    source: str,
    *,
    relation_registry: relation_registry.RelationRegistry | None = None,
    language_registry: semantic_language_registry.SemanticLanguageRegistry | None = None,
    review_fingerprint: str | None | object = _REVIEW_FINGERPRINT_UNSET,
) -> SemanticPageState:
    """Build detached semantic state from one already-read Markdown string."""
    root = Path(vault_root)
    rel_path = _normalize_path(root, path)
    raw_source = source
    logical_source = raw_source.replace("\r\n", "\n").replace("\r", "\n")
    frontmatter, body, _ = vault.parse_frontmatter(logical_source)
    page_type_value = frontmatter.get("type")
    page_type = str(page_type_value) if page_type_value else None
    projects = _page_projects(frontmatter)
    normalized_id = normalize_id(frontmatter.get(ID_FIELD))
    registry = relation_registry or globals()["relation_registry"].core_registry()
    language = language_registry or semantic_language_registry.core_registry()
    document = semantic_units.parse_semantic_units(
        body,
        path=rel_path,
        parent_ref=(memory_refs.memory_ref(normalized_id) if normalized_id is not None else None),
        validate=True,
        language_registry=semantic_language_registry.for_attached_projects(language, projects),
        relation_registry=registry,
        include_legacy_relations=True,
        retain_unknown_relations=True,
        project=None,
        page_type=page_type,
    )
    title = vault.resolve_display_title(frontmatter, body, rel_path)
    parsed = find_module.ParsedPage(
        path=root / rel_path,
        rel_path=rel_path,
        frontmatter=frontmatter,
        body=body,
        title=title,
        mtime=0.0,
    )
    identity_kind = "exomem_id" if normalized_id is not None else "path"
    identity = normalized_id or rel_path
    resolved_review_fingerprint = review_fingerprint
    if review_fingerprint is _REVIEW_FINGERPRINT_UNSET:
        resolved_review_fingerprint = (
            review_content_fingerprint(normalized_id, logical_source)
            if normalized_id is not None
            else None
        )
    elif review_fingerprint is not None and type(review_fingerprint) is not str:
        raise ValueError("review_fingerprint must be a string or None")
    return SemanticPageState(
        path=rel_path,
        identity_kind=identity_kind,
        identity=identity,
        source_hash=vault.content_hash(raw_source),
        language_registry_hash=(f"{language.schema_version}:{language.content_hash}"),
        relation_registry_hash=(f"{registry.core_version}:{registry.extension_hash}"),
        review_fingerprint=(
            str(resolved_review_fingerprint) if resolved_review_fingerprint is not None else None
        ),
        frontmatter=frontmatter,
        page_type=page_type,
        projects=projects,
        status=str(frontmatter["status"]) if frontmatter.get("status") else None,
        title=title,
        document=document,
        eligible_governed=activation.is_eligible_governed_page(root, parsed),
        eligible_compiled=activation.is_eligible_compiled_page(root, parsed),
    )


class _CensusUnsafe(Exception):
    """The corpus tree cannot be fingerprinted safely; do not use the cache."""


# Process cache for the fully-materialized corpus context: one bounded entry
# per vault root, captioned by a stat-level census of every filesystem input the
# build reads. Exact census matches are returned directly. Markdown-only census
# deltas are reconciled by reparsing just the changed parents and rebuilding the
# cheap derived maps from the retained page states. Configuration/registry
# changes still take the full-build oracle path.
#
# Why (path, size, mtime_ns) per file and not a max-mtime scan: Syncthing (and
# any mtime-preserving sync) materializes remote edits with the SOURCE's
# modification time, which can be older than every local timestamp — max-mtime
# would silently serve a stale corpus. The full census still catches such an
# edit because the synced file's mtime_ns (and normally size) differs from the
# entry recorded for the previous content at that path. The residual
# undetectable case — new content with byte-identical size and identical
# 100ns-resolution mtime at the same path — is not producible by the engine's
# own temp+rename writes nor by observed sync behavior.
_CORPUS_CONTEXT_CACHE: dict[tuple[str, str], tuple[tuple, SemanticCorpusContext]] = {}
_CORPUS_CONTEXT_EVENT_TOKENS: dict[tuple[str, str], tuple[int, int, str]] = {}
_CORPUS_CONTEXT_LANGUAGE_HASHES: dict[tuple[str, str], str] = {}
_CORPUS_CONTEXT_CACHE_LOCK = threading.Lock()
_CORPUS_CONTEXT_UPDATE_LOCK = threading.RLock()
_CORPUS_CONTEXT_CACHE_MAX_VAULTS = 2


@dataclass(slots=True)
class _CorpusContextFlight:
    census: tuple
    registry_identity: tuple[Any, ...]
    language_identity: tuple[Any, ...]
    done: threading.Event = field(default_factory=threading.Event)
    result: SemanticCorpusContext | None = None
    error: BaseException | None = None


_CORPUS_CONTEXT_FLIGHTS: dict[tuple[str, str], _CorpusContextFlight] = {}


def corpus_context_cache_enabled() -> bool:
    """Whether the corpus-context cache is active (EXOMEM_DISABLE_CORPUS_CACHE)."""
    return not os.environ.get("EXOMEM_DISABLE_CORPUS_CACHE")


def reset_corpus_context_cache() -> None:
    """Drop every cached corpus context; intentionally public for tests."""
    with _CORPUS_CONTEXT_CACHE_LOCK:
        _CORPUS_CONTEXT_CACHE.clear()
        _CORPUS_CONTEXT_EVENT_TOKENS.clear()
        _CORPUS_CONTEXT_LANGUAGE_HASHES.clear()


def _corpus_cache_key(root: Path) -> tuple[str, str]:
    return (
        os.path.normcase(str(root.resolve(strict=False))),
        os.path.normcase(str(vault.kb_root(root).resolve(strict=False))),
    )


def _corpus_census(root: Path) -> tuple | None:
    """Stat census of every filesystem input ``build_corpus_context`` reads.

    Mirrors both production walks — ``_build_identity_census`` (every ``.md``
    under the KB, refusing filesystem aliases) and ``vault.walk_vault_md``
    (the full vault minus skip dirs and sync-conflict copies) — and appends
    the non-Markdown inputs: ``_access.yaml`` (page eligibility via
    ``access.access_tier``) and the two ``_Schema`` registry files. Returns
    ``None`` when the tree cannot be fingerprinted safely; callers must then
    build uncached so the build path surfaces its own safety errors.
    """
    entries: set[tuple[str, str, int, int]] = set()
    kb = vault.kb_root(root)

    def strict_walk(directory: Path) -> None:
        # Mirror of _build_identity_census: every entry under KB, alias-free.
        for child in os.scandir(directory):
            path = Path(child.path)
            info = child.stat(follow_symlinks=False)
            if child.is_symlink() or vault._is_reparse(info):
                raise _CensusUnsafe
            if stat.S_ISDIR(info.st_mode):
                strict_walk(path)
                continue
            if path.suffix.casefold() != ".md":
                continue
            if not stat.S_ISREG(info.st_mode):
                raise _CensusUnsafe
            entries.add((path.relative_to(root).as_posix(), "f", info.st_size, info.st_mtime_ns))

    def loose_walk(directory: Path) -> None:
        # Mirror of vault.walk_vault_md: skip dirs and sync-conflict copies,
        # with the same is_dir()/is_file() semantics (which traverse links).
        for child in os.scandir(directory):
            path = Path(child.path)
            if child.is_dir():
                if child.name in vault.VAULT_SCAN_SKIP_DIRS:
                    continue
                loose_walk(path)
            elif (
                child.is_file()
                and path.suffix.lower() == ".md"
                and ".sync-conflict-" not in child.name
            ):
                info = child.stat()
                entries.add(
                    (path.relative_to(root).as_posix(), "f", info.st_size, info.st_mtime_ns)
                )

    try:
        try:
            kb_info = kb.lstat()
        except FileNotFoundError:
            kb_info = None
        if kb_info is not None:
            # A KB root that is an alias makes the identity census raise; the
            # census must refuse to vouch for that tree rather than mask it.
            if not stat.S_ISDIR(kb_info.st_mode) or vault._is_reparse(kb_info):
                raise _CensusUnsafe
            strict_walk(kb)
        loose_walk(root)
        for extra in (
            access.access_config_path(root),
            relation_registry.extension_registry_path(root),
            semantic_language_registry.registry_path(root),
        ):
            marker = str(extra.relative_to(root).as_posix())
            try:
                info = extra.stat()
            except FileNotFoundError:
                entries.add((marker, "absent", -1, -1))
            else:
                entries.add((marker, "cfg", info.st_size, info.st_mtime_ns))
    except (_CensusUnsafe, OSError, ValueError):
        return None
    return tuple(sorted(entries))


def _config_census(root: Path) -> tuple[tuple[str, str, int, int], ...] | None:
    """O(1) freshness stamp for non-Markdown semantic inputs."""
    entries: list[tuple[str, str, int, int]] = []
    try:
        for extra in (
            access.access_config_path(root),
            relation_registry.extension_registry_path(root),
            semantic_language_registry.registry_path(root),
        ):
            marker = extra.relative_to(root).as_posix()
            try:
                info = extra.stat()
            except FileNotFoundError:
                entries.append((marker, "absent", -1, -1))
            else:
                entries.append((marker, "cfg", info.st_size, info.st_mtime_ns))
    except (OSError, ValueError):
        return None
    return tuple(sorted(entries))


def _stored_config_census(census: tuple) -> tuple:
    return tuple(entry for entry in census if entry[1] != "f")


def _patch_markdown_census(
    root: Path,
    census: tuple,
    *,
    changed_paths: tuple[str, ...],
    deleted_paths: tuple[str, ...],
) -> tuple:
    entries = {str(entry[0]): entry for entry in census}
    for rel_path in deleted_paths:
        entries.pop(rel_path, None)
    for rel_path in changed_paths:
        path = root / rel_path
        try:
            info = path.stat()
        except FileNotFoundError:
            entries.pop(rel_path, None)
        else:
            entries[rel_path] = (rel_path, "f", info.st_size, info.st_mtime_ns)
    return tuple(sorted(entries.values()))


def _registries_match_disk(
    root: Path,
    relation_definitions: relation_registry.RelationRegistry,
    language: semantic_language_registry.SemanticLanguageRegistry,
) -> bool:
    """Whether the supplied registries are content-identical to the on-disk ones.

    The cache stores contexts derived from on-disk registry state (the
    registry files are census entries). A caller-supplied registry object is
    only compatible with that stored state when its content fingerprint equals
    a fresh disk load — synthetic/proposal registries fail this and bypass the
    cache entirely.
    """
    try:
        disk_registry = relation_registry.load_registry(root)
        disk_language = semantic_language_registry.load_registry(root)
    except Exception:  # noqa: BLE001 — unreadable registry state: never cache
        return False
    return (disk_registry.core_version, disk_registry.extension_hash) == (
        relation_definitions.core_version,
        relation_definitions.extension_hash,
    ) and (disk_language.schema_version, disk_language.content_hash) == (
        language.schema_version,
        language.content_hash,
    )


def _markdown_census_delta(before: tuple, after: tuple) -> tuple[str, ...] | None:
    """Return changed Markdown paths, or ``None`` for a non-page delta.

    Census entries are ``(path, kind, size, mtime_ns)``. A removed path only
    exists in ``before`` and an added path only in ``after``; both remain safe
    incremental page changes when their surviving entry kind is ``f``.
    """
    before_by_path = {str(entry[0]): entry for entry in before}
    after_by_path = {str(entry[0]): entry for entry in after}
    changed = tuple(
        sorted(
            path
            for path in before_by_path.keys() | after_by_path.keys()
            if before_by_path.get(path) != after_by_path.get(path)
        )
    )
    for path in changed:
        old = before_by_path.get(path)
        new = after_by_path.get(path)
        if (old is not None and old[1] != "f") or (new is not None and new[1] != "f"):
            return None
    return changed


def _identity_guarded_delta_source(root: Path, rel_path: str) -> str | None:
    """Read one KB Markdown delta under the full census' alias policy."""
    kb = vault.kb_root(root)
    try:
        kb_info = kb.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise activation_manifest.ActivationManifestError(
            "IDENTITY_CENSUS_ROOT_UNREADABLE",
            "Knowledge Base root is unavailable for identity census",
        ) from error
    if (
        not stat.S_ISDIR(kb_info.st_mode)
        or stat.S_ISLNK(kb_info.st_mode)
        or vault._is_reparse(kb_info)
    ):
        raise activation_manifest.ActivationManifestError(
            "IDENTITY_CENSUS_UNSAFE_ROOT",
            "Knowledge Base root is unsafe for identity census",
        )

    source_path = root / rel_path
    try:
        leaf_info = source_path.lstat()
    except FileNotFoundError:
        try:
            vault.PathGuard.capture(root, rel_path, leaf_policy="absent")
        except vault.PathGuardError as error:
            code = (
                "IDENTITY_CENSUS_UNSAFE_ENTRY"
                if error.code in {"PATH_GUARD_ROOT", "PATH_GUARD_UNSAFE"}
                else "IDENTITY_CENSUS_ENTRY_UNREADABLE"
            )
            raise activation_manifest.ActivationManifestError(
                code,
                f"could not safely inspect stable identity at {rel_path}",
            ) from error
        return None
    except OSError as error:
        raise activation_manifest.ActivationManifestError(
            "IDENTITY_CENSUS_ENTRY_UNREADABLE",
            "could not inspect a stable-identity census entry",
        ) from error
    if stat.S_ISLNK(leaf_info.st_mode) or vault._is_reparse(leaf_info):
        raise activation_manifest.ActivationManifestError(
            "IDENTITY_CENSUS_UNSAFE_ENTRY",
            "stable-identity census contains a filesystem alias",
        )
    if not stat.S_ISREG(leaf_info.st_mode):
        raise activation_manifest.ActivationManifestError(
            "IDENTITY_CENSUS_NONREGULAR_MARKDOWN",
            "stable-identity census contains nonregular Markdown",
        )
    try:
        guard = vault.PathGuard.capture(root, rel_path, leaf_policy="stable")
    except vault.PathGuardError as error:
        code = (
            "IDENTITY_CENSUS_UNSAFE_ENTRY"
            if error.code in {"PATH_GUARD_ROOT", "PATH_GUARD_UNSAFE"}
            else "IDENTITY_CENSUS_ENTRY_UNREADABLE"
        )
        raise activation_manifest.ActivationManifestError(
            code,
            f"could not safely inspect stable identity at {rel_path}",
        ) from error
    try:
        source = source_path.read_text(encoding="utf-8")
        guard.recheck(root)
    except (OSError, UnicodeDecodeError, vault.PathGuardError) as error:
        governed_writable = (
            activation.is_managed_governed_path(root, source_path)
            and access.access_tier(root, rel_path) == access.TIER_READ_WRITE
        )
        code = (
            "ACTIVATION_MANIFEST_PAGE_UNREADABLE"
            if governed_writable
            else "IDENTITY_CENSUS_PAGE_UNREADABLE"
        )
        raise activation_manifest.ActivationManifestError(
            code,
            f"could not safely inspect stable identity at {rel_path}",
        ) from error
    return source


def _reconcile_markdown_delta(
    root: Path,
    context: SemanticCorpusContext,
    changed_paths: tuple[str, ...],
    *,
    relation_definitions: relation_registry.RelationRegistry,
    language: semantic_language_registry.SemanticLanguageRegistry,
) -> SemanticCorpusContext:
    """Apply current Markdown bytes to an already-materialized context.

    The stable-identity census intentionally covers every Markdown file under
    the KB, including scan-excluded paths. The semantic page map mirrors
    ``vault.walk_vault_md`` and therefore excludes trash/schema/sync-conflict
    paths. Any read or identity ambiguity raises through the same full-build
    validation types instead of blessing partial state.
    """
    states = dict(context.pages)
    identities = {entry.path: entry for entry in context.identity_census.entries}
    kb_rel = vault.kb_root(root).relative_to(root).as_posix().rstrip("/")
    for rel_path in changed_paths:
        source_path = root / rel_path
        in_kb = rel_path == kb_rel or rel_path.startswith(kb_rel + "/")
        included = not vault.in_excluded_scan_dir(rel_path) and (
            ".sync-conflict-" not in PurePosixPath(rel_path).name
        )
        if in_kb:
            source = _identity_guarded_delta_source(root, rel_path)
        else:
            try:
                source = source_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                source = None
        if source is None:
            states.pop(rel_path, None)
            identities.pop(rel_path, None)
            continue

        state = build_page_state(
            root,
            rel_path,
            source,
            relation_registry=relation_definitions,
            language_registry=language,
        )
        if included:
            states[rel_path] = state
        else:
            states.pop(rel_path, None)
        if in_kb:
            # ``from_states`` preserves the strict invalid-ID failure that the
            # full stable-identity census would raise for governed Markdown.
            identities[rel_path] = StableIdentityCensus.from_states((state,)).entries[0]
        else:
            identities.pop(rel_path, None)

    return _context_from_state_map(
        root,
        states,
        _copy_registry(relation_definitions),
        StableIdentityCensus(tuple(identities.values())),
    )


def on_corpus_files_changed(
    vault_root: Path,
    *,
    changed: Iterable[Path] = (),
    deleted: Iterable[Path | str] = (),
) -> None:
    """Patch a warm semantic corpus after a watcher or canonical write event.

    Callers that also publish freshness MUST use
    :func:`publish_corpus_files_changed` so both derived states advance under
    one boundary. This lower-level hook remains useful for tests and repair.
    """
    with _CORPUS_CONTEXT_UPDATE_LOCK:
        _patch_corpus_files_changed_locked(
            vault_root,
            changed=changed,
            deleted=deleted,
            event_token=freshness.triple(Path(vault_root), "vault"),
        )


def publish_corpus_files_changed(
    vault_root: Path,
    *,
    changed: Iterable[Path] = (),
    deleted: Iterable[Path | str] = (),
) -> None:
    """Atomically advance freshness and the warm semantic corpus context.

    Serializing these two derived-state publications prevents concurrent file
    events from stamping a context that contains only one event with a
    freshness token that already represents both.
    """
    root = Path(vault_root)
    changed_values = tuple(Path(value) for value in changed)
    deleted_values = tuple(Path(value) for value in deleted)
    changed_paths = tuple(
        value if value.is_absolute() else root / value for value in changed_values
    )
    deleted_paths = tuple(
        value if value.is_absolute() else root / value for value in deleted_values
    )
    with _CORPUS_CONTEXT_UPDATE_LOCK:
        freshness.on_files_changed(root, changed=changed_paths, deleted=deleted_paths)
        try:
            _patch_corpus_files_changed_locked(
                root,
                changed=changed_values,
                deleted=deleted_values,
                event_token=freshness.triple(root, "vault"),
            )
        except BaseException:
            # Freshness already advanced, so the previous context can no
            # longer prove continuity. Evict every caption before propagating
            # the safety/parse failure; a later good event must not leapfrog
            # this missed delta and stamp stale state as current.
            cache_key = _corpus_cache_key(root)
            with _CORPUS_CONTEXT_CACHE_LOCK:
                _CORPUS_CONTEXT_CACHE.pop(cache_key, None)
                _CORPUS_CONTEXT_EVENT_TOKENS.pop(cache_key, None)
                _CORPUS_CONTEXT_LANGUAGE_HASHES.pop(cache_key, None)
            raise


def _patch_corpus_files_changed_locked(
    vault_root: Path,
    *,
    changed: Iterable[Path],
    deleted: Iterable[Path | str],
    event_token: tuple[int, int, str] | None,
) -> None:
    """Patch one cache entry while ``_CORPUS_CONTEXT_UPDATE_LOCK`` is held."""
    root = Path(vault_root)
    cache_key = _corpus_cache_key(root)
    with _CORPUS_CONTEXT_CACHE_LOCK:
        entry = _CORPUS_CONTEXT_CACHE.get(cache_key)
    if entry is None:
        return
    if _config_census(root) != _stored_config_census(entry[0]):
        with _CORPUS_CONTEXT_CACHE_LOCK:
            if _CORPUS_CONTEXT_CACHE.get(cache_key) is entry:
                _CORPUS_CONTEXT_CACHE.pop(cache_key, None)
                _CORPUS_CONTEXT_EVENT_TOKENS.pop(cache_key, None)
                _CORPUS_CONTEXT_LANGUAGE_HASHES.pop(cache_key, None)
        return

    def relative(value: Path | str) -> str | None:
        path = Path(value)
        try:
            return (
                path.resolve().relative_to(root.resolve()).as_posix()
                if path.is_absolute()
                else _normalize_path(root, path)
            )
        except (OSError, ValueError):
            return None

    changed_paths = tuple(
        sorted({rel for value in changed if (rel := relative(value)) is not None})
    )
    deleted_paths = tuple(
        sorted({rel for value in deleted if (rel := relative(value)) is not None})
    )
    reconcile_paths = tuple(sorted(set(changed_paths) | set(deleted_paths)))
    if not reconcile_paths:
        return
    relation_definitions = relation_registry.load_registry(root)
    language = semantic_language_registry.load_registry(root)
    context = _reconcile_markdown_delta(
        root,
        entry[1],
        reconcile_paths,
        relation_definitions=relation_definitions,
        language=language,
    )
    census = _patch_markdown_census(
        root,
        entry[0],
        changed_paths=changed_paths,
        deleted_paths=deleted_paths,
    )
    with _CORPUS_CONTEXT_CACHE_LOCK:
        if _CORPUS_CONTEXT_CACHE.get(cache_key) is entry:
            _CORPUS_CONTEXT_CACHE[cache_key] = (census, context)
            _CORPUS_CONTEXT_LANGUAGE_HASHES[cache_key] = (
                f"{language.schema_version}:{language.content_hash}"
            )
            if event_token is None:
                _CORPUS_CONTEXT_EVENT_TOKENS.pop(cache_key, None)
            else:
                _CORPUS_CONTEXT_EVENT_TOKENS[cache_key] = event_token


def build_corpus_context(
    vault_root: Path,
    *,
    candidate: SemanticPageState | None = None,
    registry: relation_registry.RelationRegistry | None = None,
    language_registry: semantic_language_registry.SemanticLanguageRegistry | None = None,
) -> SemanticCorpusContext:
    """Read and parse the corpus once, then resolve every fact in memory.

    Warm calls are served from a bounded process cache captioned by a stat
    census of every filesystem input (see ``_corpus_census``). Exact matches
    return directly; Markdown-only changes reparse only the changed parents.
    Candidate-bearing builds always take the uncached path.
    ``EXOMEM_DISABLE_CORPUS_CACHE=1`` restores the always-rebuild behavior.
    """
    root = Path(vault_root)
    relation_definitions = registry or relation_registry.load_registry(root)
    language = language_registry or semantic_language_registry.load_registry(root)

    census: tuple | None = None
    cache_key: tuple[str, str] | None = None
    if candidate is None and corpus_context_cache_enabled():
        cache_key = _corpus_cache_key(root)
        event_token = freshness.triple(root, "vault")
        if event_token is not None:
            with _CORPUS_CONTEXT_CACHE_LOCK:
                event_entry = _CORPUS_CONTEXT_CACHE.get(cache_key)
                cached_token = _CORPUS_CONTEXT_EVENT_TOKENS.get(cache_key)
                cached_language_hash = _CORPUS_CONTEXT_LANGUAGE_HASHES.get(cache_key)
            if (
                event_entry is not None
                and cached_token == event_token
                and cached_language_hash == f"{language.schema_version}:{language.content_hash}"
                and _config_census(root) == _stored_config_census(event_entry[0])
                and (
                    event_entry[1].registry.core_version,
                    event_entry[1].registry.extension_hash,
                )
                == (
                    relation_definitions.core_version,
                    relation_definitions.extension_hash,
                )
            ):
                log.info(
                    "semantic corpus context event-hit pages=%d",
                    len(event_entry[1].pages),
                )
                return event_entry[1]
        started = time.perf_counter()
        census = _corpus_census(root)
        census_ms = (time.perf_counter() - started) * 1000.0
        if census is not None and _registries_match_disk(root, relation_definitions, language):
            with _CORPUS_CONTEXT_CACHE_LOCK:
                entry = _CORPUS_CONTEXT_CACHE.get(cache_key)
            if entry is not None and entry[0] == census:
                with _CORPUS_CONTEXT_UPDATE_LOCK, _CORPUS_CONTEXT_CACHE_LOCK:
                    current = _CORPUS_CONTEXT_CACHE.get(cache_key)
                    if current is not entry:
                        entry = current
                    elif entry[0] == census:
                        current_token = freshness.triple(root, "vault")
                        if current_token is not None:
                            _CORPUS_CONTEXT_EVENT_TOKENS[cache_key] = current_token
                        log.info(
                            "semantic corpus context reused pages=%d census_ms=%.1f",
                            len(entry[1].pages),
                            census_ms,
                        )
                        return entry[1]
                if entry is not None and entry[0] == census:
                    with _CORPUS_CONTEXT_CACHE_LOCK:
                        if _CORPUS_CONTEXT_CACHE.get(cache_key) is entry:
                            return entry[1]
            if entry is not None:
                changed_paths = _markdown_census_delta(entry[0], census)
                if changed_paths is not None:
                    started = time.perf_counter()
                    context = _reconcile_markdown_delta(
                        root,
                        entry[1],
                        changed_paths,
                        relation_definitions=relation_definitions,
                        language=language,
                    )
                    with _CORPUS_CONTEXT_UPDATE_LOCK:
                        confirmed = _corpus_census(root)
                        with _CORPUS_CONTEXT_CACHE_LOCK:
                            current = _CORPUS_CONTEXT_CACHE.get(cache_key)
                            if confirmed == census and current is entry:
                                _CORPUS_CONTEXT_CACHE[cache_key] = (census, context)
                                _CORPUS_CONTEXT_LANGUAGE_HASHES[cache_key] = (
                                    f"{language.schema_version}:{language.content_hash}"
                                )
                                current_token = freshness.triple(root, "vault")
                                if current_token is not None:
                                    _CORPUS_CONTEXT_EVENT_TOKENS[cache_key] = current_token
                                log.info(
                                    "semantic corpus context reconciled pages=%d "
                                    "changed=%d reconcile_ms=%.1f census_ms=%.1f",
                                    len(context.pages),
                                    len(changed_paths),
                                    (time.perf_counter() - started) * 1000.0,
                                    census_ms,
                                )
                                return context
                            if current is not None and current is not entry:
                                return current[1]
        else:
            census = None

    flight: _CorpusContextFlight | None = None
    if census is not None and cache_key is not None:
        registry_identity = (
            relation_definitions.core_version,
            relation_definitions.extension_hash,
        )
        language_identity = (language.schema_version, language.content_hash)
        with _CORPUS_CONTEXT_CACHE_LOCK:
            flight = _CORPUS_CONTEXT_FLIGHTS.get(cache_key)
            if flight is None:
                flight = _CorpusContextFlight(
                    census,
                    registry_identity,
                    language_identity,
                )
                _CORPUS_CONTEXT_FLIGHTS[cache_key] = flight
                owns_flight = True
            else:
                owns_flight = False
        if not owns_flight:
            same_inputs = (
                flight.census == census
                and flight.registry_identity == registry_identity
                and flight.language_identity == language_identity
            )
            flight.done.wait()
            if not same_inputs:
                return build_corpus_context(
                    root,
                    candidate=candidate,
                    registry=registry,
                    language_registry=language_registry,
                )
            if flight.error is not None:
                raise flight.error
            assert flight.result is not None
            return flight.result

    try:
        started = time.perf_counter()
        context = _build_corpus_context_uncached(
            root,
            candidate=candidate,
            relation_definitions=relation_definitions,
            language=language,
        )
        build_ms = (time.perf_counter() - started) * 1000.0
        stored = False
        if census is not None and cache_key is not None:
            with _CORPUS_CONTEXT_UPDATE_LOCK:
                stable_census = census
                # A busy watcher/media worker can change Markdown while the cold
                # build runs. Once publication begins, serialize with event
                # freshness+cache updates, absorb exact deltas, then stamp the same
                # state atomically so a newer event context cannot be overwritten.
                for _ in range(3):
                    confirmed = _corpus_census(root)
                    if confirmed == stable_census:
                        break
                    if confirmed is None:
                        stable_census = None
                        break
                    changed_paths = _markdown_census_delta(stable_census, confirmed)
                    if changed_paths is None:
                        stable_census = None
                        break
                    context = _reconcile_markdown_delta(
                        root,
                        context,
                        changed_paths,
                        relation_definitions=relation_definitions,
                        language=language,
                    )
                    stable_census = confirmed
                else:
                    confirmed = None
                if confirmed == stable_census and stable_census is not None:
                    census = stable_census
                    with _CORPUS_CONTEXT_CACHE_LOCK:
                        _CORPUS_CONTEXT_CACHE[cache_key] = (census, context)
                        _CORPUS_CONTEXT_LANGUAGE_HASHES[cache_key] = (
                            f"{language.schema_version}:{language.content_hash}"
                        )
                        current_token = freshness.triple(root, "vault")
                        if current_token is None:
                            _CORPUS_CONTEXT_EVENT_TOKENS.pop(cache_key, None)
                        else:
                            _CORPUS_CONTEXT_EVENT_TOKENS[cache_key] = current_token
                        while len(_CORPUS_CONTEXT_CACHE) > _CORPUS_CONTEXT_CACHE_MAX_VAULTS:
                            for stale_key in list(_CORPUS_CONTEXT_CACHE):
                                if stale_key != cache_key:
                                    del _CORPUS_CONTEXT_CACHE[stale_key]
                                    _CORPUS_CONTEXT_EVENT_TOKENS.pop(stale_key, None)
                                    _CORPUS_CONTEXT_LANGUAGE_HASHES.pop(stale_key, None)
                                    break
                            else:
                                break
                    stored = True
        log.info(
            "semantic corpus context built pages=%d build_ms=%.1f cached=%s",
            len(context.pages),
            build_ms,
            stored,
        )
    except BaseException as error:
        if flight is not None:
            flight.error = error
            with _CORPUS_CONTEXT_CACHE_LOCK:
                if _CORPUS_CONTEXT_FLIGHTS.get(cache_key) is flight:
                    del _CORPUS_CONTEXT_FLIGHTS[cache_key]
            flight.done.set()
        raise
    if flight is not None:
        flight.result = context
        with _CORPUS_CONTEXT_CACHE_LOCK:
            if _CORPUS_CONTEXT_FLIGHTS.get(cache_key) is flight:
                del _CORPUS_CONTEXT_FLIGHTS[cache_key]
        flight.done.set()
    return context


def _build_corpus_context_uncached(
    root: Path,
    *,
    candidate: SemanticPageState | None,
    relation_definitions: relation_registry.RelationRegistry,
    language: semantic_language_registry.SemanticLanguageRegistry,
) -> SemanticCorpusContext:
    identity_census, census_sources = _build_identity_census(root)
    states: dict[str, SemanticPageState] = {}
    candidate_path = candidate.path if candidate is not None else None
    for disk_path in sorted(vault.walk_vault_md(root), key=lambda item: item.as_posix()):
        try:
            rel_path = disk_path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        if rel_path == candidate_path:
            continue
        try:
            source = census_sources.get(rel_path)
            if source is None:
                source = disk_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            if (
                activation.is_managed_governed_path(root, disk_path)
                and access.access_tier(root, rel_path) == access.TIER_READ_WRITE
            ):
                raise activation_manifest.ActivationManifestError(
                    "ACTIVATION_MANIFEST_PAGE_UNREADABLE",
                    f"could not read governed semantic page {disk_path}: {error}",
                ) from error
            continue
        states[rel_path] = build_page_state(
            root,
            rel_path,
            source,
            relation_registry=relation_definitions,
            language_registry=language,
        )
    if candidate is not None:
        states[candidate.path] = candidate
        identity_census = identity_census.with_page(candidate)
    return _context_from_state_map(
        root,
        states,
        _copy_registry(relation_definitions),
        identity_census,
    )


def _build_identity_census(
    root: Path,
) -> tuple[StableIdentityCensus, Mapping[str, str]]:
    """Read every Markdown file below KB once without following aliases."""
    kb = vault.kb_root(root)
    entries: list[StableIdentityEntry] = []
    sources: dict[str, str] = {}

    def walk(directory: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as error:
            raise activation_manifest.ActivationManifestError(
                "IDENTITY_CENSUS_ENUMERATION_FAILED",
                "could not enumerate the stable-identity census",
            ) from error
        for child in children:
            path = Path(child.path)
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as error:
                raise activation_manifest.ActivationManifestError(
                    "IDENTITY_CENSUS_ENTRY_UNREADABLE",
                    "could not inspect a stable-identity census entry",
                ) from error
            if child.is_symlink() or vault._is_reparse(info):
                raise activation_manifest.ActivationManifestError(
                    "IDENTITY_CENSUS_UNSAFE_ENTRY",
                    "stable-identity census contains a filesystem alias",
                )
            if stat.S_ISDIR(info.st_mode):
                walk(path)
                continue
            if path.suffix.casefold() != ".md":
                continue
            if not stat.S_ISREG(info.st_mode):
                raise activation_manifest.ActivationManifestError(
                    "IDENTITY_CENSUS_NONREGULAR_MARKDOWN",
                    "stable-identity census contains nonregular Markdown",
                )
            rel = path.relative_to(root).as_posix()
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                code = (
                    "ACTIVATION_MANIFEST_PAGE_UNREADABLE"
                    if activation.is_managed_governed_path(root, path)
                    and access.access_tier(root, rel) == access.TIER_READ_WRITE
                    else "IDENTITY_CENSUS_PAGE_UNREADABLE"
                )
                raise activation_manifest.ActivationManifestError(
                    code,
                    f"could not safely inspect stable identity at {rel}",
                ) from error
            try:
                frontmatter, _, _ = vault.parse_frontmatter(source, strict=True)
            except vault.FrontmatterError as error:
                raise activation_manifest.ActivationManifestError(
                    error.code,
                    f"could not safely inspect stable identity at {rel}",
                ) from error
            raw_identity = frontmatter.get(ID_FIELD)
            identity: str | None = None
            if raw_identity is not None:
                if not isinstance(raw_identity, str) or normalize_id(raw_identity) is None:
                    raise activation_manifest.ActivationManifestError(
                        "IDENTITY_CENSUS_INVALID_ID",
                        f"stable identity is invalid at {rel}",
                    )
                identity = normalize_id(raw_identity)
            entries.append(StableIdentityEntry(rel, identity))
            sources[rel] = source

    try:
        root_info = kb.lstat()
    except FileNotFoundError:
        return StableIdentityCensus(()), MappingProxyType({})
    except OSError as error:
        raise activation_manifest.ActivationManifestError(
            "IDENTITY_CENSUS_ROOT_UNREADABLE",
            "Knowledge Base root is unavailable for identity census",
        ) from error
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or vault._is_reparse(root_info)
    ):
        raise activation_manifest.ActivationManifestError(
            "IDENTITY_CENSUS_UNSAFE_ROOT",
            "Knowledge Base root is unsafe for identity census",
        )
    walk(kb)
    return StableIdentityCensus(tuple(entries)), MappingProxyType(dict(sources))


def build_stable_identity_census(vault_root: Path) -> StableIdentityCensus:
    """Re-derive UUID ownership without building or parsing a semantic corpus."""
    census, _ = _build_identity_census(Path(vault_root))
    return census


def _resolver_snapshot(
    resolver: vault.WikilinkResolver,
) -> tuple[
    frozenset[str],
    frozenset[str],
    Mapping[str, tuple[str, ...]],
    Mapping[str, tuple[str, ...]],
]:
    return (
        frozenset(resolver.full_paths),
        frozenset(resolver.kb_stripped),
        _mapping_of_tuples({key: tuple(values) for key, values in resolver.stems.items()}),
        _mapping_of_tuples({key: tuple(values) for key, values in resolver.titles.items()}),
    )


def _context_from_state_map(
    root: Path,
    states: Mapping[str, SemanticPageState],
    registry: relation_registry.RelationRegistry,
    identity_census: StableIdentityCensus,
) -> SemanticCorpusContext:
    ordered_pages = {path: states[path] for path in sorted(states)}
    entries = tuple((path, ordered_pages[path].title) for path in ordered_pages)
    resolver = vault.WikilinkResolver.from_entries(root, entries)
    facts = _derive_relation_facts(root, ordered_pages, resolver, registry)
    return _context_from_resolved_state(
        root,
        ordered_pages,
        registry,
        identity_census,
        entries=entries,
        resolver=resolver,
        facts=facts,
    )


def _context_with_stable_topology_candidate(
    context: SemanticCorpusContext,
    state: SemanticPageState,
) -> SemanticCorpusContext:
    """Replace one page without re-resolving every authored corpus fact."""
    pages = dict(context.pages)
    pages[state.path] = state
    resolver = vault.WikilinkResolver.from_entries(context.vault_root, context.resolver_entries)
    candidate_facts = _derive_relation_facts(
        context.vault_root,
        {state.path: state},
        resolver,
        context.registry,
        target_states=pages,
    )
    retained_facts = (fact for fact in context.relation_facts if fact.authored_path != state.path)
    facts = tuple(sorted((*retained_facts, *candidate_facts), key=lambda item: item.identity))
    return _context_from_resolved_state(
        context.vault_root,
        pages,
        context.registry,
        context.identity_census,
        entries=context.resolver_entries,
        resolver=resolver,
        facts=facts,
    )


def _context_from_resolved_state(
    root: Path,
    ordered_pages: Mapping[str, SemanticPageState],
    registry: relation_registry.RelationRegistry,
    identity_census: StableIdentityCensus,
    *,
    entries: tuple[tuple[str, str], ...],
    resolver: vault.WikilinkResolver,
    facts: tuple[RelationFact, ...],
) -> SemanticCorpusContext:
    inbound: dict[str, list[RelationFact]] = {}
    outbound: dict[str, list[RelationFact]] = {}
    dependencies: dict[str, list[str]] = {}
    for fact in facts:
        dependencies.setdefault(_dependency_key(fact.raw_target), []).append(fact.identity)
        if fact.logical_source_path in ordered_pages:
            outbound.setdefault(fact.logical_source_path, []).append(fact)
        if fact.logical_target_path in ordered_pages:
            inbound.setdefault(fact.logical_target_path, []).append(fact)
    candidates = tuple(
        activation_manifest.ActivationCandidate(
            state.path,
            state.source_hash,
            state.identity if state.identity_kind == "exomem_id" else None,
        )
        for state in ordered_pages.values()
        if state.eligible_compiled
    )
    full_paths, kb_stripped, stems, titles = _resolver_snapshot(resolver)
    return SemanticCorpusContext(
        vault_root=root,
        pages=MappingProxyType(ordered_pages),
        resolver_entries=entries,
        resolver_full_paths=full_paths,
        resolver_kb_stripped=kb_stripped,
        resolver_stems=stems,
        resolver_titles=titles,
        raw_target_dependencies=_mapping_of_tuples(
            {key: tuple(sorted(values)) for key, values in dependencies.items()}
        ),
        relation_facts=facts,
        eligible_governed_paths=frozenset(
            state.path for state in ordered_pages.values() if state.eligible_governed
        ),
        eligible_compiled_paths=frozenset(
            state.path for state in ordered_pages.values() if state.eligible_compiled
        ),
        inbound=_mapping_of_tuples(
            {
                key: tuple(sorted(values, key=lambda item: item.identity))
                for key, values in inbound.items()
            }
        ),
        outbound=_mapping_of_tuples(
            {
                key: tuple(sorted(values, key=lambda item: item.identity))
                for key, values in outbound.items()
            }
        ),
        activation_census=activation_manifest.ActivationCensus.from_candidates(candidates),
        identity_census=identity_census,
        registry=registry,
    )


def _frontmatter_targets(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        matches = tuple(match.group(1).strip() for match in _WIKILINK_RE.finditer(value))
        return matches or ((value.strip(),) if value.strip() else ())
    if isinstance(value, (list, tuple)):
        return tuple(target for child in value for target in _frontmatter_targets(child))
    if isinstance(value, Mapping):
        return tuple(target for child in value.values() for target in _frontmatter_targets(child))
    return ()


def _raw_note_target(raw: str, fallback: str) -> str:
    matches = tuple(_WIKILINK_RE.finditer(raw))
    return matches[-1].group(1).strip() if matches else fallback


def _raw_note_relation(raw: str, fallback: str) -> str:
    content = re.sub(r"^\s*[-*+]\s+", "", str(raw), count=1)
    match = re.match(r"(?P<label>[^\s:]+)\s*:?[\s]", content)
    return match.group("label") if match is not None else fallback


def _raw_rich_relation(raw: str, fallback: str) -> str:
    label, separator, _ = str(raw).partition(":")
    return label.strip() if separator and label.strip() else fallback


def _target_parts(raw_target: str) -> tuple[str, str | None, str | None]:
    cleaned = str(raw_target).strip()
    if cleaned.startswith("[[") and cleaned.endswith("]]"):
        cleaned = cleaned[2:-2].strip()
    path_anchor, separator, alias = cleaned.partition("|")
    path, anchor_separator, anchor = path_anchor.partition("#")
    return (
        path.strip(),
        anchor.strip() if anchor_separator and anchor.strip() else None,
        alias.strip() if separator and alias.strip() else None,
    )


def _resolve_target(
    root: Path,
    raw_target: str,
    resolver: vault.WikilinkResolver,
) -> tuple[str, str | None, str | None, str | None]:
    _, authored_anchor, alias = _target_parts(raw_target)
    try:
        normalized, _ = vault.normalize_wikilink(raw_target, root, resolver=resolver, strict=True)
    except vault.AmbiguousWikilinkError:
        return "ambiguous", None, authored_anchor, alias
    except vault.UnresolvedWikilinkError:
        return "unresolved", None, authored_anchor, alias
    path, separator, anchor = normalized.partition("#")
    resolved = path if path.lower().endswith(".md") else f"{path}.md"
    if separator:
        resolved = f"{resolved}#{anchor}"
    return "resolved", resolved, anchor or authored_anchor, alias


def _registry_resolution(
    registry: relation_registry.RelationRegistry,
    raw_relation: str,
    *,
    projects: tuple[str, ...],
    page_type: str | None,
    source_kind: str,
    target_kind: str | None,
    origin: str,
) -> relation_registry.RelationResolution:
    origins = (origin, "semantic_relation") if origin == "markdown_relation" else (origin,)
    project_values: tuple[str | None, ...] = projects or (None,)
    resolutions = tuple(
        registry.resolve(
            raw_relation,
            project=project,
            page_type=page_type,
            source_kind=source_kind,
            target_kind=target_kind,
            origin=registry_origin,
        )
        for registry_origin in origins
        for project in project_values
    )
    selected = next(
        (
            item
            for item in resolutions
            if item.canonical is not None and item.status != "scope_violation"
        ),
        resolutions[0],
    )
    if selected.definition is not None and selected.definition.projects and not projects:
        return replace(
            selected,
            status="scope_violation",
            findings=(
                *selected.findings,
                {
                    "code": "scope_violation",
                    "path": f"relations.{selected.canonical}.project",
                    "detail": "an attached project is required by the relation scope",
                },
            ),
        )
    return selected


def _fact_identity(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _derive_relation_facts(
    root: Path,
    states: Mapping[str, SemanticPageState],
    resolver: vault.WikilinkResolver,
    registry: relation_registry.RelationRegistry,
    *,
    target_states: Mapping[str, SemanticPageState] | None = None,
) -> tuple[RelationFact, ...]:
    resolved_states = target_states if target_states is not None else states
    raw_facts: list[dict[str, Any]] = []
    for state in states.values():
        for relation in state.document.note_relations:
            raw_facts.append(
                {
                    "authored": state,
                    "raw_relation": _raw_note_relation(relation.raw, relation.kind),
                    "raw_target": _raw_note_target(relation.raw, relation.target),
                    "line": relation.line,
                    "anchor": None,
                    "element_identity": None,
                    "source_kind": "file",
                    "origin": "markdown_relation" if relation.canonical else "semantic_relation",
                    "reverse": False,
                }
            )
        for unit in state.document.rich_units:
            for relation in unit.relations:
                raw_facts.append(
                    {
                        "authored": state,
                        "raw_relation": _raw_rich_relation(relation.raw, relation.kind),
                        "raw_target": relation.target,
                        "line": relation.line,
                        "anchor": unit.anchor,
                        "element_identity": unit.unit_ref or unit.fingerprint,
                        "source_kind": unit.kind,
                        "origin": "semantic_relation",
                        "reverse": False,
                    }
                )
        frontmatter_relations = (
            ("sources", "derived_from", False),
            ("evidence", "evidenced_by", False),
            ("evidences", "evidenced_by", False),
            ("evidence_paths", "evidenced_by", False),
            ("related", "links_to", False),
            ("supersedes", "supersedes", False),
            ("superseded_by", "supersedes", True),
        )
        for field_name, raw_relation, reverse in frontmatter_relations:
            for raw_target in _frontmatter_targets(state.frontmatter.get(field_name)):
                raw_facts.append(
                    {
                        "authored": state,
                        "raw_relation": raw_relation,
                        "raw_target": raw_target,
                        "line": None,
                        "anchor": field_name,
                        "element_identity": field_name,
                        "source_kind": "file",
                        "origin": "frontmatter",
                        "reverse": reverse,
                    }
                )

    occurrences: Counter[tuple[str, str, str, str, str, str]] = Counter()
    facts: list[RelationFact] = []
    for raw in raw_facts:
        state = raw["authored"]
        target_status, resolved_target, target_anchor, target_alias = _resolve_target(
            root, raw["raw_target"], resolver
        )
        resolved_base = resolved_target.split("#", 1)[0] if resolved_target else None
        target_state = resolved_states.get(resolved_base or "")
        target_kind = "file" if target_state is not None else "unresolved"
        resolution = _registry_resolution(
            registry,
            raw["raw_relation"],
            projects=state.projects,
            page_type=state.page_type,
            source_kind=raw["source_kind"],
            target_kind=target_kind,
            origin=raw["origin"],
        )
        definition = resolution.definition
        if raw["reverse"]:
            logical_source = resolved_base or _target_parts(raw["raw_target"])[0]
            logical_target = state.path
        else:
            logical_source = state.path
            logical_target = resolved_base or _target_parts(raw["raw_target"])[0]
        occurrence_key = (
            f"{state.identity_kind}:{state.identity}",
            raw["origin"],
            relation_registry.normalize_relation(raw["raw_relation"]),
            str(raw["raw_target"]),
            str(raw["anchor"] or ""),
            str(raw["element_identity"] or ""),
        )
        occurrences[occurrence_key] += 1
        identity_payload = {
            "authored_identity_kind": state.identity_kind,
            "authored_identity": state.identity,
            "origin": raw["origin"],
            "relation": relation_registry.normalize_relation(raw["raw_relation"]),
            "raw_target": raw["raw_target"],
            "authored_anchor": raw["anchor"],
            "authored_element_identity": raw["element_identity"],
            "source_kind": raw["source_kind"],
            "reverse": raw["reverse"],
            "occurrence": occurrences[occurrence_key],
        }
        facts.append(
            RelationFact(
                identity=_fact_identity(identity_payload),
                logical_source_path=logical_source,
                logical_target_path=logical_target,
                raw_target=str(raw["raw_target"]),
                resolved_target_path=resolved_target,
                target_anchor=target_anchor,
                target_alias=target_alias,
                authored_path=state.path,
                authored_line=raw["line"],
                authored_anchor=raw["anchor"],
                authored_projects=state.projects,
                authored_page_type=state.page_type,
                source_kind=raw["source_kind"],
                target_page_type=target_state.page_type if target_state is not None else None,
                raw_relation=raw["raw_relation"],
                canonical_relation=resolution.canonical,
                family=definition.family if definition is not None else None,
                registry_status=resolution.status,
                origin=raw["origin"],
                authored=True,
                reviewer_accepted=False,
                target_status=target_status,
            )
        )
    return tuple(sorted(facts, key=lambda item: item.identity))


def _dependency_key(raw_target: str) -> str:
    path, _, _ = _target_parts(raw_target)
    return path.removesuffix(".md").strip("/").casefold()


def qualify_relation(
    fact: RelationFact,
    *,
    registry: relation_registry.RelationRegistry,
    corpus: SemanticCorpusContext,
) -> RelationQualification:
    reasons: list[str] = []
    definition = registry.definition(fact.canonical_relation or "")
    target_page = corpus.pages.get(fact.logical_target_path)
    expected_successor = fact.logical_source_path.removesuffix(".md").casefold()
    mutual_supersession = bool(
        definition is not None
        and definition.family == "supersession"
        and target_page is not None
        and target_page.status == "superseded"
        and any(
            _dependency_key(raw) == expected_successor
            for raw in _frontmatter_targets(target_page.frontmatter.get("superseded_by"))
        )
    )
    if not (fact.authored or fact.reviewer_accepted):
        reasons.append("not_authored_or_accepted")
    if fact.target_status == "unresolved":
        reasons.append("unresolved_target")
    elif fact.target_status == "ambiguous":
        reasons.append("ambiguous_target")
    resolved_base = (
        fact.resolved_target_path.split("#", 1)[0]
        if fact.resolved_target_path is not None
        else None
    )
    if fact.target_status == "resolved":
        if fact.logical_source_path not in corpus.eligible_governed_paths:
            reasons.append("ineligible_target")
        if (
            fact.logical_target_path not in corpus.eligible_governed_paths
            and not mutual_supersession
        ):
            reasons.append("ineligible_target")
        if fact.logical_source_path == fact.logical_target_path:
            reasons.append("self_target")
    if definition is None:
        reasons.append("unregistered_relation")
    elif definition.status != "active":
        reasons.append("inactive_relation")
    if definition is not None:
        resolution = _registry_resolution(
            registry,
            fact.raw_relation,
            projects=fact.authored_projects,
            page_type=fact.authored_page_type,
            source_kind=fact.source_kind,
            target_kind=(
                "file"
                if fact.target_status == "resolved" and resolved_base in corpus.pages
                else "unresolved"
            ),
            origin=fact.origin,
        )
        if resolution.status == "scope_violation":
            reasons.append("scope_violation")
        if definition.family in _EXCLUDED_FAMILIES:
            reasons.append("excluded_family")
    if fact.origin == "frontmatter":
        if definition is None or definition.family != "supersession":
            reasons.append("frontmatter_not_supersession")
    elif fact.origin not in {"markdown_relation", "semantic_relation", "semantic_block"}:
        reasons.append("unsupported_origin")
    ordered = tuple(dict.fromkeys(reasons))
    return RelationQualification(not ordered, ordered)


def is_relation_review_current(
    review: RelationReviewState,
    page: SemanticPageState,
    corpus: SemanticCorpusContext,
) -> bool:
    """Return whether stable identity and exact review fingerprint are current."""
    return (
        page.identity_kind == "exomem_id"
        and review.page_identity == page.identity
        and corpus.identity_census.paths_by_identity.get(page.identity) == (page.path,)
        and page.review_fingerprint is not None
        and review.content_fingerprint == page.review_fingerprint
    )


def _relation_disposition(
    page: SemanticPageState,
    corpus: SemanticCorpusContext,
    *,
    review: RelationReviewState | None,
    operation: str,
    before: SemanticPageState | None,
    before_corpus: SemanticCorpusContext,
    mode: str,
) -> RelationDisposition:
    directional: list[tuple[str, RelationFact]] = []
    directional.extend(("outbound", fact) for fact in corpus.outbound.get(page.path, ()))
    directional.extend(("inbound", fact) for fact in corpus.inbound.get(page.path, ()))
    rejected: list[RejectedRelationFact] = []
    qualifying: list[tuple[str, RelationFact]] = []
    for direction, fact in sorted(directional, key=lambda item: (item[0], item[1].identity)):
        result = qualify_relation(fact, registry=corpus.registry, corpus=corpus)
        if result.qualifies:
            qualifying.append((direction, fact))
        else:
            rejected.append(RejectedRelationFact(fact, result.reasons))
    review_is_current = review is not None and is_relation_review_current(review, page, corpus)
    stale_review = review is not None and not review_is_current
    other_governed = corpus.eligible_governed_paths - {page.path}
    if qualifying:
        if stale_review:
            actions = ("cleanup_stale_review",)
        elif review is not None:
            actions = ("cleanup_relation_review",)
        else:
            actions = ()
        return RelationDisposition(
            kind="qualifying_relation",
            satisfied=True,
            current=True,
            qualifying_directions=tuple(direction for direction, _ in qualifying),
            qualifying_facts=tuple(fact for _, fact in qualifying),
            rejected_facts=tuple(rejected),
            actions=actions,
        )
    if review_is_current and review is not None and review.kind == "reviewed_none":
        return RelationDisposition("reviewed_none", True, True, rejected_facts=tuple(rejected))
    if (
        review_is_current
        and review is not None
        and review.kind == "bootstrap"
        and not other_governed
    ):
        return RelationDisposition("bootstrap", True, True, rejected_facts=tuple(rejected))
    automatic_bootstrap = (
        review is None
        and mode == "precommit"
        and operation in _CREATE_LIKE
        and before is None
        and not before_corpus.eligible_governed_paths
        and corpus.eligible_governed_paths == frozenset({page.path})
    )
    if automatic_bootstrap:
        return RelationDisposition(
            "bootstrap",
            True,
            True,
            rejected_facts=tuple(rejected),
            actions=("record_bootstrap_review",),
        )
    stale = review is not None
    return RelationDisposition(
        "stale" if stale else "missing",
        False,
        False,
        rejected_facts=tuple(rejected),
        actions=("replace_relation_review",) if stale else ("review_relations",),
    )


def _stable_fragment_identity(code: str, raw: str, occurrence: int) -> tuple[str, ...]:
    normalized = " ".join(str(raw).casefold().split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return ("syntax", f"{code}:{digest}:{occurrence}")


def _diagnostic_findings(page: SemanticPageState) -> list[ContractFinding]:
    findings: list[ContractFinding] = []
    occurrences: Counter[tuple[str, str]] = Counter()
    diagnostics = tuple(page.document.errors) + tuple(page.document.warnings)
    for diagnostic in diagnostics:
        if diagnostic.code == "unsupported_relation" and diagnostic.registry_namespace is None:
            continue
        raw_key = " ".join(diagnostic.raw.casefold().split())
        occurrences[(diagnostic.code, raw_key)] += 1
        registry_diagnostic = (
            diagnostic.registry_namespace is not None and diagnostic.registry_key is not None
        )
        findings.append(
            ContractFinding(
                code=diagnostic.code,
                severity=diagnostic.severity,
                path=page.path,
                span=diagnostic.span,
                detail=diagnostic.message,
                remediation=diagnostic.remediation,
                governed_element_identity=_stable_fragment_identity(
                    diagnostic.code,
                    diagnostic.raw,
                    occurrences[(diagnostic.code, raw_key)],
                ),
                resolved_rule=(
                    (
                        diagnostic.registry_namespace,
                        diagnostic.registry_key,
                        "registry",
                    )
                    if registry_diagnostic
                    else ("semantic_units", "*", "syntax")
                ),
            )
        )
    for diagnostic in page.document.note_relation_errors:
        if diagnostic.code == "unsupported_relation":
            continue
        raw_key = " ".join(diagnostic.raw.casefold().split())
        occurrences[(diagnostic.code, raw_key)] += 1
        findings.append(
            ContractFinding(
                code=diagnostic.code,
                severity="error",
                path=page.path,
                span=None,
                detail=diagnostic.message,
                remediation="Use one registered lower-snake-case relation and one wikilink target.",
                governed_element_identity=_stable_fragment_identity(
                    diagnostic.code,
                    diagnostic.raw,
                    occurrences[(diagnostic.code, raw_key)],
                ),
                resolved_rule=("relations", "*", "syntax"),
            )
        )
    return findings


def _conflict_findings(
    page: SemanticPageState,
    contracts: memory_schema.ResolvedMemoryContracts,
) -> list[ContractFinding]:
    return [
        ContractFinding(
            code=conflict.code,
            severity="error",
            path=page.path,
            span=None,
            detail=conflict.detail,
            remediation="Resolve the conflicting saved contract declarations.",
            governed_element_identity=conflict.resolved_rule[:2],
            resolved_rule=conflict.resolved_rule,
            contracts=conflict.contracts,
            provenance=conflict.provenance,
        )
        for conflict in contracts.conflicts
    ]


def _registry_findings(
    page: SemanticPageState,
    corpus: SemanticCorpusContext,
) -> list[ContractFinding]:
    findings: list[ContractFinding] = []
    for registry_finding in corpus.registry.findings:
        raw = str(registry_finding.get("path", "registry"))
        relation = registry_finding.get("relation")
        if relation is not None:
            relation = str(relation)
            governed_element_identity = ("relations", relation)
            resolved_rule = ("relations", relation, "registry")
        else:
            governed_element_identity = ("relations", "registry", raw)
            resolved_rule = ("relations", "*", "registry")
        findings.append(
            ContractFinding(
                code=str(registry_finding.get("code", "invalid_relation_registry")),
                severity="error",
                path=page.path,
                span=None,
                detail=str(registry_finding.get("detail", "invalid relation registry")),
                remediation="Repair the relation registry before validating pages.",
                governed_element_identity=governed_element_identity,
                resolved_rule=resolved_rule,
            )
        )
    for fact in corpus.relation_facts:
        if fact.authored_path != page.path:
            continue
        code: str | None = None
        detail: str | None = None
        if fact.canonical_relation is None:
            code = "unregistered_relation"
            detail = f"relation {fact.raw_relation!r} is not registered"
        elif fact.registry_status == "deprecated":
            code = "inactive_relation"
            detail = f"relation {fact.canonical_relation!r} is deprecated"
        elif fact.registry_status == "scope_violation":
            code = "scope_violation"
            detail = f"relation {fact.canonical_relation!r} is outside its registry scope"
        if code is None or detail is None:
            continue
        relation = fact.canonical_relation or relation_registry.normalize_relation(
            fact.raw_relation
        )
        findings.append(
            ContractFinding(
                code=code,
                severity="error",
                path=page.path,
                span=None,
                detail=detail,
                remediation="Use an active registered relation valid for the authored page scope.",
                governed_element_identity=("relations", fact.identity),
                resolved_rule=("relations", relation, "registry"),
            )
        )
    return findings


def _observed_namespaces(
    page: SemanticPageState,
    corpus: SemanticCorpusContext,
) -> dict[str, set[Any]]:
    required_relations: set[str] = set()
    allowed_relations: set[str] = set()
    for fact in corpus.relation_facts:
        if fact.authored_path != page.path or fact.origin not in _AUTHORED_SCHEMA_ORIGINS:
            continue
        if fact.canonical_relation is not None:
            required_relations.add(fact.canonical_relation)
            allowed_relations.add(fact.canonical_relation)
        else:
            allowed_relations.add(relation_registry.normalize_relation(fact.raw_relation))
    return {
        "fields": set(page.frontmatter),
        "blocks": {unit.kind for unit in page.document.rich_units},
        "kinds": {unit.kind for unit in page.document.units},
        "categories": {unit.category for unit in page.document.units},
        "relations": allowed_relations,
        "required_relations": required_relations,
    }


def _rule_finding(
    page: SemanticPageState,
    constraint: memory_schema.ResolvedContractConstraint,
    *,
    code: str,
    detail: str,
    severity: str,
    element: str | None = None,
) -> ContractFinding:
    governed_element = element or constraint.element
    return ContractFinding(
        code=code,
        severity=severity,
        path=page.path,
        span=None,
        detail=detail,
        remediation="Update the page or revise the applicable saved contract.",
        governed_element_identity=(constraint.namespace, governed_element),
        resolved_rule=constraint.identity,
        contracts=constraint.contracts,
        provenance=constraint.provenance,
    )


def _required_code(namespace: str) -> str:
    return {
        "fields": "CONTRACT_REQUIRED_FIELD",
        "blocks": "CONTRACT_REQUIRED_BLOCK",
        "kinds": "CONTRACT_REQUIRED_KIND",
        "categories": "CONTRACT_REQUIRED_CATEGORY",
        "relations": "CONTRACT_REQUIRED_RELATION",
    }[namespace]


def _unknown_code(namespace: str) -> str:
    return {
        "fields": "CONTRACT_UNKNOWN_FIELD",
        "blocks": "CONTRACT_UNKNOWN_BLOCK",
        "kinds": "CONTRACT_UNKNOWN_KIND",
        "categories": "CONTRACT_UNKNOWN_CATEGORY",
        "relations": "CONTRACT_UNKNOWN_RELATION",
    }[namespace]


def _governed_element(value: Any) -> str:
    if isinstance(value, str):
        return value
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}:{value!r}"


def _page_rule_findings(
    page: SemanticPageState,
    contracts: memory_schema.ResolvedMemoryContracts,
    corpus: SemanticCorpusContext,
) -> list[ContractFinding]:
    if contracts.validation == "off" or contracts.validation is None:
        return []
    severity = "error" if contracts.validation == "strict" else "warning"
    observed = _observed_namespaces(page, corpus)
    findings: list[ContractFinding] = []
    for constraint in contracts.constraints:
        namespace, element, rule = constraint.identity
        if rule == "required" and constraint.value is True:
            values = (
                observed["required_relations"]
                if namespace == "relations"
                else observed.get(namespace, set())
            )
            if element not in values:
                findings.append(
                    _rule_finding(
                        page,
                        constraint,
                        code=_required_code(namespace),
                        detail=f"required {namespace} element {element!r} is missing",
                        severity=severity,
                    )
                )
        elif namespace == "fields" and rule == "types" and element in page.frontmatter:
            actual = memory_schema.value_type(page.frontmatter[element])
            if actual not in constraint.value:
                findings.append(
                    _rule_finding(
                        page,
                        constraint,
                        code="CONTRACT_FIELD_TYPE",
                        detail=(
                            f"field {element!r} has type {actual!r}, expected "
                            f"one of {constraint.value!r}"
                        ),
                        severity=severity,
                    )
                )
        elif namespace == "fields" and rule == "enum" and element in page.frontmatter:
            try:
                actual_identity = memory_schema.typed_scalar_identity(
                    page.frontmatter[element], label=f"field {element}"
                )
            except ValueError:
                actual_identity = None
            allowed = {
                memory_schema.typed_scalar_identity(value, label=f"field {element} enum")
                for value in constraint.value
            }
            if actual_identity not in allowed:
                findings.append(
                    _rule_finding(
                        page,
                        constraint,
                        code="CONTRACT_FIELD_ENUM",
                        detail=f"field {element!r} is outside its exact typed enum",
                        severity=severity,
                    )
                )
        elif rule == "allowed" and constraint.value is not None:
            values = observed.get(namespace, set())
            for unknown in sorted(values - set(constraint.value), key=_stable_value_key):
                findings.append(
                    _rule_finding(
                        page,
                        constraint,
                        code=_unknown_code(namespace),
                        detail=f"{namespace} element {unknown!r} is outside the finite allowed set",
                        severity=severity,
                        element=_governed_element(unknown),
                    )
                )
    return findings


def _disposition_finding(
    page: SemanticPageState, disposition: RelationDisposition
) -> ContractFinding | None:
    if disposition.satisfied or not page.eligible_compiled:
        return None
    return ContractFinding(
        code=(
            "RELATION_DISPOSITION_STALE"
            if disposition.kind == "stale"
            else "RELATION_DISPOSITION_MISSING"
        ),
        severity="error",
        path=page.path,
        span=None,
        detail=_disposition_detail(page, disposition),
        remediation=(
            "Add a qualifying typed relation or record a current reviewed-none disposition."
        ),
        governed_element_identity=("relations", "disposition"),
        resolved_rule=("relations", "*", "disposition"),
    )


def _disposition_detail(page: SemanticPageState, disposition: RelationDisposition) -> str:
    if disposition.kind == "stale":
        return "the saved non-edge relation disposition is no longer current"
    if page.document.canonical_section_present and page.document.canonical_bullet_count == 0:
        return (
            "the empty canonical Relations section requires a current "
            "reviewed-none or bootstrap disposition"
        )
    return "the compiled page needs a qualifying relation or explicit current review"


def _finding_sort_key(finding: ContractFinding) -> tuple[Any, ...]:
    span = finding.span
    return (
        finding.key,
        finding.path,
        span.start_offset if span is not None else -1,
        finding.detail,
    )


def _raw_findings(
    page: SemanticPageState,
    contracts: memory_schema.ResolvedMemoryContracts,
    corpus: SemanticCorpusContext,
    *,
    review: RelationReviewState | None,
    operation: str,
    before: SemanticPageState | None,
    before_corpus: SemanticCorpusContext,
    mode: str,
    include_relation_disposition: bool,
) -> tuple[list[ContractFinding], RelationDisposition | None]:
    disposition = None
    if include_relation_disposition:
        disposition = _relation_disposition(
            page,
            corpus,
            review=review,
            operation=operation,
            before=before,
            before_corpus=before_corpus,
            mode=mode,
        )
    findings: list[ContractFinding] = []
    structure_finding = compiled_structure_finding(page)
    if structure_finding is not None:
        findings.append(structure_finding)
    findings.extend(_diagnostic_findings(page))
    minimum_finding = _missing_semantic_unit_finding(page)
    if minimum_finding is not None:
        findings.append(minimum_finding)
    if page.identity_kind == "exomem_id" and corpus.identity_census.paths_by_identity.get(
        page.identity
    ) != (page.path,):
        findings.append(
            ContractFinding(
                code="SEMANTIC_IDENTITY_DUPLICATE",
                severity="error",
                path=page.path,
                span=None,
                detail="the stable page identity has another corpus owner",
                remediation="Resolve the duplicate stable identity before writing.",
                governed_element_identity=("identity", page.identity),
                resolved_rule=("semantic_contract", "identity", "unique"),
            )
        )
    findings.extend(_conflict_findings(page, contracts))
    findings.extend(_registry_findings(page, corpus))
    findings.extend(_page_rule_findings(page, contracts, corpus))
    if disposition is not None:
        disposition_finding = _disposition_finding(page, disposition)
        if disposition_finding is not None:
            findings.append(disposition_finding)
    return findings, disposition


def _corpus_mismatch_finding(
    page: SemanticPageState,
    *,
    phase: str,
) -> ContractFinding:
    return ContractFinding(
        code="SEMANTIC_CORPUS_STATE_MISMATCH",
        severity="error",
        path=page.path,
        span=None,
        detail=(f"the supplied {phase}-corpus snapshot does not contain the evaluated page state"),
        remediation="Rebuild the immutable corpus context with the exact pending candidate.",
        governed_element_identity=(
            "corpus",
            phase,
            page.identity_kind,
            page.identity,
        ),
        resolved_rule=("semantic_contract", "corpus", "context"),
    )


def _migration_warning(finding: ContractFinding) -> ContractFinding:
    return replace(
        finding,
        severity="warning",
        detail=f"Pre-existing semantic debt retained: {finding.detail}",
    )


def evaluate(
    *,
    before: SemanticPageState | None,
    after: SemanticPageState,
    operation: str,
    mode: str,
    before_contracts: memory_schema.ResolvedMemoryContracts,
    after_contracts: memory_schema.ResolvedMemoryContracts,
    before_corpus: SemanticCorpusContext,
    after_corpus: SemanticCorpusContext,
    before_review: RelationReviewState | None = None,
    after_review: RelationReviewState | None = None,
    grandfathered: bool = False,
    include_relation_disposition: bool = True,
) -> SemanticContractResult:
    """Evaluate supplied immutable state without crossing any adapter boundary."""
    if mode not in {"precommit", "posthoc"}:
        raise ValueError("mode must be precommit or posthoc")
    before_context_matches = before is None or before_corpus.pages.get(before.path) == before
    effective_before_corpus = (
        before_corpus
        if before_context_matches or before is None
        else before_corpus.with_candidate(before)
    )
    after_context_matches = after_corpus.pages.get(after.path) == after
    effective_after_corpus = (
        after_corpus if after_context_matches else after_corpus.with_candidate(after)
    )
    after_findings, disposition = _raw_findings(
        after,
        after_contracts,
        effective_after_corpus,
        review=after_review,
        operation=operation,
        before=before,
        before_corpus=effective_before_corpus,
        mode=mode,
        include_relation_disposition=include_relation_disposition,
    )
    if not after_context_matches:
        after_findings.append(_corpus_mismatch_finding(after, phase="after"))
    if before is not None and not before_context_matches:
        after_findings.append(_corpus_mismatch_finding(before, phase="before"))
    before_findings: list[ContractFinding] = []
    before_disposition: RelationDisposition | None = None
    if before is not None:
        before_findings, before_disposition = _raw_findings(
            before,
            before_contracts,
            effective_before_corpus,
            review=before_review,
            operation=operation,
            before=before,
            before_corpus=effective_before_corpus,
            mode=mode,
            include_relation_disposition=include_relation_disposition,
        )

    raw_before_error_keys = {
        finding.key for finding in before_findings if finding.severity == "error"
    }
    raw_after_error_keys = {
        finding.key for finding in after_findings if finding.severity == "error"
    }
    use_subset_exception = (
        grandfathered and operation in _GRANDFATHERED_OPERATIONS and before is not None
    )
    normalized: list[ContractFinding] = []
    for finding in after_findings:
        if (
            use_subset_exception
            and finding.severity == "error"
            and finding.key in raw_before_error_keys
        ):
            normalized.append(_migration_warning(finding))
        else:
            normalized.append(finding)
    invalidated_disposition = bool(
        use_subset_exception
        and before_disposition is not None
        and disposition is not None
        and before_disposition.satisfied
        and not disposition.satisfied
    )
    new_error_keys = raw_after_error_keys - raw_before_error_keys
    errors = tuple(
        sorted(
            (item for item in normalized if item.severity == "error"),
            key=_finding_sort_key,
        )
    )
    warnings = tuple(
        sorted(
            (item for item in normalized if item.severity == "warning"),
            key=_finding_sort_key,
        )
    )
    findings = tuple(sorted((*errors, *warnings), key=_finding_sort_key))
    subset_allowed = use_subset_exception and raw_after_error_keys <= raw_before_error_keys
    blocking = () if subset_allowed and not invalidated_disposition else errors
    if new_error_keys and use_subset_exception:
        blocking = tuple(item for item in errors if item.key in new_error_keys)
    if invalidated_disposition:
        disposition_errors = tuple(
            item for item in errors if item.resolved_rule == ("relations", "*", "disposition")
        )
        blocking = tuple(sorted({*blocking, *disposition_errors}, key=_finding_sort_key))
    if mode == "posthoc":
        blocking = ()
    should_block = bool(blocking) and mode == "precommit"
    kind_counts = tuple(sorted(Counter(unit.kind for unit in after.document.units).items()))
    category_counts = tuple(sorted(Counter(unit.category for unit in after.document.units).items()))
    compact_unit_count = sum(unit.form == "compact" for unit in after.document.units)
    rich_unit_count = sum(unit.form == "rich" for unit in after.document.units)
    return SemanticContractResult(
        mode=mode,
        operation=operation,
        findings=findings,
        errors=errors,
        warnings=warnings,
        blocking_findings=blocking,
        should_block=should_block,
        semantic_unit_count=len(after.document.units),
        kind_counts=kind_counts,
        category_counts=category_counts,
        relation_disposition=disposition,
        actions=disposition.actions if disposition is not None else (),
        compact_unit_count=compact_unit_count,
        rich_unit_count=rich_unit_count,
    )
