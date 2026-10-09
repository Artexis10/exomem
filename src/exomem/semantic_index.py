"""Shared deterministic generation state for derived semantic-unit indexes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

import yaml

from . import memory_refs, relation_registry, semantic_language_registry, semantic_units, vault
from .vocabulary import instances

PARSER_VERSION = 5
_GENERATION_SCHEMA = "exomem.semantic-unit.parent-generation.v5"
_ACTIVE_PARENT_STATES: ContextVar[Mapping[str, SemanticParentIndexState] | None] = ContextVar(
    "exomem_semantic_parent_states", default=None
)


@dataclass(frozen=True, slots=True)
class SemanticParentIndexState:
    """One already-read parent and the parse shared by every derived sidecar."""

    path: str
    parent_ref: str | None
    parent_source_hash: str
    language_registry_hash: str
    relation_registry_hash: str
    parent_generation: str
    parser_version: int
    document: semantic_units.SemanticUnitDocument
    body: str = field(default="", repr=False)
    frontmatter: Mapping[str, Any] = field(default_factory=dict, repr=False)
    candidates: semantic_units.SemanticUnitCandidates | None = field(default=None, repr=False)
    definitions: instances.PageDefinitions | None = field(default=None, repr=False)
    definitions_unavailable: bool = False

    @property
    def occurrences(self) -> tuple[semantic_units.StructuralOccurrence, ...]:
        return semantic_units.structural_occurrences(self.candidates) if self.candidates else ()

    @property
    def instance_id(self) -> str | None:
        """The vocabulary instance this interpretation selected; None without definitions."""
        return self.definitions.instance_id if self.definitions is not None else None

    def __post_init__(self) -> None:
        object.__setattr__(self, "frontmatter", MappingProxyType(dict(self.frontmatter)))


@dataclass(frozen=True, slots=True)
class SemanticIndexFreshness:
    current: bool
    code: str
    parent_path: str
    current_parent_source_hash: str | None = None
    current_parent_generation: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticUnitSidecarDrift:
    """One deterministic parent/sidecar semantic-unit parity failure."""

    sidecar: str
    parent_path: str
    reasons: tuple[str, ...]
    expected_generation: str | None = None
    actual_generations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "sidecar": self.sidecar,
            "parent_path": self.parent_path,
            "reasons": list(self.reasons),
            "expected_generation": self.expected_generation,
            "actual_generations": list(self.actual_generations),
        }


@dataclass(frozen=True, slots=True)
class _SidecarParentRows:
    unit_refs: frozenset[str]
    parent_refs: frozenset[str]
    stamps: frozenset[tuple[str, str, int]]
    derived_refs: frozenset[str] | None = None


def set_parent_states(
    states: Mapping[str, SemanticParentIndexState],
) -> Token[Mapping[str, SemanticParentIndexState] | None]:
    """Bind one coordinator-owned parse set for the current dispatch context."""
    return _ACTIVE_PARENT_STATES.set(dict(states))


def reset_parent_states(
    token: Token[Mapping[str, SemanticParentIndexState] | None],
) -> None:
    _ACTIVE_PARENT_STATES.reset(token)


def parent_state_for_path(vault_root: Path, path: Path | str) -> SemanticParentIndexState | None:
    states = _ACTIVE_PARENT_STATES.get()
    if not states:
        return None
    root = Path(vault_root)
    candidate = Path(path)
    try:
        rel_path = (
            candidate.resolve().relative_to(root.resolve()).as_posix()
            if candidate.is_absolute()
            else PurePosixPath(str(path).replace("\\", "/")).as_posix().lstrip("/")
        )
    except (OSError, ValueError):
        return None
    return states.get(rel_path)


def parent_generation(
    *,
    parent_path: str,
    parent_ref: str | None,
    parent_source_hash: str,
    language_registry_hash: str,
    relation_registry_hash: str,
    parser_version: int = PARSER_VERSION,
) -> str:
    """Return a portable generation shared by lexical, vector, and graph rows.

    Stable parents are path-independent, so an unchanged move retains its unit
    generation. Legacy parents deliberately bind the generation to their path.
    """
    identity = parent_ref or f"path:{parent_path}"
    payload = json.dumps(
        {
            "schema": _GENERATION_SCHEMA,
            "identity": identity,
            "language_registry_hash": language_registry_hash,
            "parent_source_hash": parent_source_hash,
            "parser_version": parser_version,
            "relation_registry_hash": relation_registry_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_parent_record(
    vault_root: Path,
    *,
    parent_path: str,
    parent_generation_value: str,
    parent_source_hash: str,
    parser_version: int,
) -> SemanticIndexFreshness:
    """Validate one derived record against current canonical Markdown bytes."""
    root = Path(vault_root)
    # This seam is ordinary semantic recall validation, not structured-record
    # parsing.  Refuse raw Records before ``read_text`` can hydrate a stale
    # vector/lexical parent into the semantic path.
    from . import recall_policy

    if not recall_policy.is_recall_candidate(root, root / parent_path):
        return SemanticIndexFreshness(False, "recall_parent_not_admitted", parent_path)
    try:
        path = (root / parent_path).resolve()
        path.relative_to(root.resolve())
    except (OSError, ValueError):
        return SemanticIndexFreshness(False, "invalid_parent_path", parent_path)
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SemanticIndexFreshness(False, "missing_parent", parent_path)
    except (OSError, UnicodeError):
        return SemanticIndexFreshness(False, "parent_unavailable", parent_path)
    current_source_hash = vault.content_hash(source)
    current_ref = memory_refs.ref_from_markdown(source)
    language = semantic_language_registry.core_registry()
    relations = relation_registry.core_registry()
    language_hash, relation_hash = _registry_hashes(language, relations)
    current_generation = parent_generation(
        parent_path=parent_path,
        parent_ref=current_ref,
        parent_source_hash=current_source_hash,
        language_registry_hash=language_hash,
        relation_registry_hash=relation_hash,
    )
    common = {
        "parent_path": parent_path,
        "current_parent_source_hash": current_source_hash,
        "current_parent_generation": current_generation,
    }
    if parent_source_hash != current_source_hash:
        return SemanticIndexFreshness(False, "parent_source_hash_mismatch", **common)
    if parser_version != PARSER_VERSION:
        return SemanticIndexFreshness(False, "parser_version_mismatch", **common)
    if parent_generation_value != current_generation:
        return SemanticIndexFreshness(False, "parent_generation_mismatch", **common)
    return SemanticIndexFreshness(True, "current", **common)


def build_parent_index_state(
    vault_root: Path,
    path: Path | str,
    *,
    source: str | None = None,
) -> SemanticParentIndexState:
    """Parse one parent source for indexing without mutating canonical Markdown."""
    root = Path(vault_root)
    candidate = Path(path)
    if candidate.is_absolute():
        rel_path = candidate.resolve().relative_to(root.resolve()).as_posix()
        source_path = candidate
    else:
        rel_path = PurePosixPath(str(path).replace("\\", "/")).as_posix().lstrip("/")
        source_path = root / rel_path
    if source is None:
        source = source_path.read_text(encoding="utf-8")
    else:
        # ``Path.read_text`` uses universal-newline translation, while callers
        # that share an exact byte snapshot may pass decoded CRLF content.
        # Normalize both routes before frontmatter/ref parsing so Windows deep
        # packs retain the same stable memory identity as indexed retrieval.
        source = source.replace("\r\n", "\n").replace("\r", "\n")
    frontmatter, body, _ = vault.parse_frontmatter(source)
    parent_ref = memory_refs.ref_from_markdown(source)
    language = semantic_language_registry.core_registry()
    relations = relation_registry.core_registry()
    language_hash, relation_hash = _registry_hashes(language, relations)
    # Reuse the contract owner's position-preserving provenance processing.
    from . import semantic_contract

    neutral = semantic_contract._parse_page_state(root, rel_path, source)
    body, candidates = neutral.body, neutral.candidates
    document = neutral.document
    source_hash = vault.content_hash(source)
    return SemanticParentIndexState(
        path=rel_path,
        parent_ref=parent_ref,
        parent_source_hash=source_hash,
        language_registry_hash=language_hash,
        relation_registry_hash=relation_hash,
        parent_generation=parent_generation(
            parent_path=rel_path, parent_ref=parent_ref, parent_source_hash=source_hash,
            language_registry_hash=language_hash, relation_registry_hash=relation_hash,
        ),
        parser_version=PARSER_VERSION,
        document=document, body=body, frontmatter=frontmatter, candidates=candidates,
    )


def selected_parent_index_state(
    vault_root: Path, path: Path | str, *, source: str | None = None,
    state: SemanticParentIndexState | None = None,
) -> SemanticParentIndexState:
    """Interpret one admitted parent without publishing selected facts to sidecars."""
    from .governance import egress

    root = Path(vault_root)
    candidate = Path(path)
    rel_path = candidate.resolve().relative_to(root.resolve()).as_posix() if candidate.is_absolute() else candidate.as_posix()
    # A stale sidecar is no authority to read a parent or its private definitions.
    if not egress.quick_page_visible(root, rel_path):
        raise ValueError("REGISTRY_UNAVAILABLE: parent is unavailable")
    neutral = state or current_parent_index_state(root, path, source=source)
    try:
        definitions = instances.page_definitions(root, neutral.path, dict(neutral.frontmatter), ("categories", "relations"))
    except (ValueError, OSError):
        # Raw reads stay served; only units no hidden heading could change remain.
        safe = semantic_units.core_safe_occurrences(neutral.candidates) if neutral.candidates else frozenset()
        units = tuple(
            unit for unit in neutral.document.units
            if semantic_units.occurrence_key(unit.form, unit.span, unit.source_hash) in safe
        )
        return replace(neutral, document=replace(neutral.document, units=units), definitions_unavailable=True)
    language = definitions.snapshots["categories"].typed
    relations = definitions.snapshots["relations"].typed
    language_hash, relation_hash = _registry_hashes(language, relations)
    if (language_hash, relation_hash) == (neutral.language_registry_hash, neutral.relation_registry_hash):
        return replace(neutral, definitions=definitions)
    document = semantic_units.parse_semantic_units(
        neutral.body,
        candidates=neutral.candidates,
        path=neutral.path, parent_ref=neutral.parent_ref, validate=True,
        language_registry=semantic_language_registry.for_attached_projects(language, page_projects(neutral.frontmatter)),
        relation_registry=relations, include_legacy_relations=True, retain_unknown_relations=True,
        project=None, page_type=str(neutral.frontmatter.get("type") or "") or None,
    )
    return replace(neutral, document=document, definitions=definitions)


def current_parent_index_state(
    vault_root: Path,
    path: Path | str,
    *,
    source: str | None = None,
) -> SemanticParentIndexState:
    """Reuse an active parse only when it exactly matches committed bytes."""
    root = Path(vault_root)
    candidate = Path(path)
    if source is None:
        source_path = candidate if candidate.is_absolute() else root / candidate
        source = source_path.read_text(encoding="utf-8")
    else:
        # `build_parent_index_state` stores the hash of the newline-normalized
        # source, so a caller sharing an exact byte snapshot has to be folded the
        # same way before the comparison below. Hashing CRLF bytes against an LF
        # hash never matches, and the answer to that miss is a silent reparse of a
        # page whose parse is already in hand.
        source = source.replace("\r\n", "\n").replace("\r", "\n")
    active = parent_state_for_path(root, path)
    language = semantic_language_registry.core_registry()
    relations = relation_registry.core_registry()
    language_hash, relation_hash = _registry_hashes(language, relations)
    if (
        active is not None
        and active.definitions is None
        and not active.definitions_unavailable
        and active.parent_source_hash == vault.content_hash(source)
        and active.parser_version == PARSER_VERSION
        and active.language_registry_hash == language_hash
        and active.relation_registry_hash == relation_hash
    ):
        return active
    return build_parent_index_state(root, path, source=source)


def from_semantic_page_state(state: Any) -> SemanticParentIndexState:
    """Adapt an already-evaluated contract page without reparsing its Markdown."""
    path = str(state.path)
    source_hash = str(state.source_hash)
    parent_ref = (
        memory_refs.memory_ref(str(state.identity)) if state.identity_kind == "exomem_id" else None
    )
    language_hash, relation_hash = _registry_hashes(semantic_language_registry.core_registry(), relation_registry.core_registry())
    return SemanticParentIndexState(
        path=path,
        parent_ref=parent_ref,
        parent_source_hash=source_hash,
        language_registry_hash=language_hash,
        relation_registry_hash=relation_hash,
        parent_generation=parent_generation(
            parent_path=path,
            parent_ref=parent_ref,
            parent_source_hash=source_hash,
            language_registry_hash=language_hash,
            relation_registry_hash=relation_hash,
        ),
        parser_version=PARSER_VERSION,
        document=state.neutral_document or state.document,
        body=state.body, frontmatter=state.frontmatter, candidates=state.candidates,
    )


def structural_metadata(state: SemanticParentIndexState) -> dict[str, Any]:
    """Portable parser coverage and inputs for body-free selected interpretation."""
    if state.candidates is None:
        raise ValueError("SEMANTIC_STRUCTURE_UNAVAILABLE")
    # These fields are the canonical membership and interpretation input protocol.
    frontmatter = {key: value for key, value in state.frontmatter.items() if key in {
        "type", "status", "project", "projects", "entity_type", "registry_scope",
        "tags", "classes", memory_refs.ID_FIELD,
    }}
    return {
        "structure": semantic_units.structural_summary(state.candidates),
        "frontmatter_yaml": yaml.safe_dump(dict(frontmatter)),
        "parent_ref": state.parent_ref,
        "parent_generation": state.parent_generation,
        "parent_source_hash": state.parent_source_hash,
        "parser_version": state.parser_version,
        "structural_complete": True,
    }


#: Registry subjects one page interpretation admits together. The parser reads
#: exactly these adapters, so a new subject needs parser code, not data.
INTERPRETATION_SUBJECTS = ("categories", "relations", "entity-types")  # nosemgrep: ep-word-set -- Registry adapter names the parser implements.


@dataclass(frozen=True, slots=True)
class SelectedParent:
    """One stored parent interpreted for one operation's admitted instance.

    `definitions` is None when the page's selected instance is unavailable to
    this operation; `structure` then holds only core-safe units and is marked
    incomplete. Shared sidecars never store this object.
    """

    path: str
    metadata: Mapping[str, Any]
    frontmatter: Mapping[str, Any]
    structure: semantic_units.SelectedStructure
    definitions: instances.PageDefinitions | None

    @property
    def instance_id(self) -> str | None:
        return self.definitions.instance_id if self.definitions is not None else None

    @property
    def relations(self) -> relation_registry.RelationRegistry:
        if self.definitions is None:
            return relation_registry.core_registry()
        return self.definitions.snapshots["relations"].typed

    @property
    def language(self) -> semantic_language_registry.SemanticLanguageRegistry:
        if self.definitions is None:
            return semantic_language_registry.core_registry()
        return self.definitions.snapshots["categories"].typed

    @property
    def entity_types(self):
        from . import entity_types

        if self.definitions is None:
            return entity_types.core_registry()
        return self.definitions.snapshots["entity-types"].typed

    @property
    def page_type(self) -> str | None:
        return str(self.frontmatter.get("type") or "") or None


class Interpretations:
    """One operation's admitted page interpretations, keyed by stored source.

    The operation owns this map and discards it with the request: admitted
    snapshots are memoized per selected instance, and each parent summary is
    interpreted at most once. No caller-independent cache retains a result.
    """

    def __init__(self, vault_root: Path) -> None:
        self.root = Path(vault_root)
        self._snapshots: dict[str | None, Mapping[str, Any] | None] = {}
        self._parents: dict[tuple[str, str], SelectedParent] = {}

    def snapshots(self, scope: str | None) -> Mapping[str, Any] | None:
        """One instance's admitted snapshots, or None when this caller cannot use it."""
        from .vocabulary import registry, registry_spec

        scope = None if scope == instances.PUBLIC_INSTANCE else scope
        if scope not in self._snapshots:
            try:
                specs = [instances.select(self.root, registry_spec(subject), scope)
                         for subject in INTERPRETATION_SUBJECTS]
                self._snapshots[scope] = {
                    "binding": specs[-1].binding_revision,
                    "snapshots": {spec.name: registry.load(spec, self.root) for spec in specs},
                }
            except (ValueError, OSError):
                self._snapshots[scope] = None
        return self._snapshots[scope]

    def admitted_instances(self) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """Every configured instance this caller may interpret, public first.

        Readers use it only to widen conservative candidate discovery for a
        shared core meaning; each page still keeps its own selected meaning.
        """
        try:
            document = instances.configuration(self.root)
        except (ValueError, OSError):
            return ()
        scopes = [None, *sorted((document or {}).get("private", {}))]
        return tuple(
            (scope or instances.PUBLIC_INSTANCE, selected["snapshots"])
            for scope in scopes
            if (selected := self.snapshots(scope)) is not None
        )

    def definitions(self, path: str, frontmatter: Mapping[str, Any]) -> instances.PageDefinitions | None:
        """The page's admitted definitions, or None when its instance is unavailable."""
        try:
            scope = instances.page_scope(self.root, path, dict(frontmatter))
        except (ValueError, OSError):
            return None
        selected = self.snapshots(scope)
        if selected is None:
            return None
        return instances.PageDefinitions(
            scope or instances.PUBLIC_INSTANCE, selected["binding"], selected["snapshots"],
            path, dict(frontmatter),
        )

    def parent(self, path: str, metadata: Mapping[str, Any]) -> SelectedParent:
        """Interpret one stored parent summary; incomplete coverage raises ValueError."""
        if not metadata.get("structural_complete") or metadata.get("parser_version") != PARSER_VERSION:
            raise ValueError("SEMANTIC_STRUCTURE_UNAVAILABLE")
        key = (path, str(metadata.get("parent_source_hash") or ""))
        cached = self._parents.get(key)
        if cached is not None:
            return cached
        frontmatter = yaml.safe_load(metadata["frontmatter_yaml"]) or {}
        definitions = self.definitions(path, frontmatter)
        language = (
            definitions.snapshots["categories"].typed if definitions is not None
            else semantic_language_registry.core_registry()
        )
        structure = semantic_units.interpret_structural_summary(
            metadata["structure"],
            language_registry=semantic_language_registry.for_attached_projects(
                language, page_projects(frontmatter),
            ),
            relation_registry=(
                definitions.snapshots["relations"].typed if definitions is not None
                else relation_registry.core_registry()
            ),
            page_type=str(frontmatter.get("type") or "") or None,
            parent_ref=metadata.get("parent_ref"),
            path=path,
            definitions_available=definitions is not None,
        )
        selected = SelectedParent(path, metadata, frontmatter, structure, definitions)
        self._parents[key] = selected
        return selected


class AdmittedParents:
    """One operation's admitted parents over one sidecar's stored summaries.

    `load(paths)` reads many pages' stored structural summaries in one query:
    `{path: summary}` for each stored page, so serving rows from many pages
    never costs one query per page. A page `allowed` refuses is never read or
    interpreted for this operation. `unavailable` holds the admitted pages
    whose selected coverage is incomplete here.
    """

    def __init__(
        self,
        load: Callable[[list[str]], Mapping[str, Mapping[str, Any]]],
        *,
        allowed: Callable[[str], bool],
        interpretations: Interpretations,
        admitted: dict[str, bool] | None = None,
    ) -> None:
        self.interpretations = interpretations
        self.unavailable: set[str] = set()
        self._load = load
        self._allowed = allowed
        #: Admission verdicts per page. A caller that already checked pages by
        #: the same rule shares its map, so no page is checked twice.
        self._admitted = admitted if admitted is not None else {}
        self._parents: dict[str, SelectedParent | None] = {}
        self._stored: dict[str, Mapping[str, Any] | None] = {}

    def allowed(self, path: str) -> bool:
        verdict = self._admitted.get(path)
        if verdict is None:
            verdict = bool(self._allowed(path))
            self._admitted[path] = verdict
        return verdict

    def hold(self, path: str, summary: Mapping[str, Any] | None) -> None:
        """Keep a summary a caller already read with its page row."""
        if path not in self._parents:
            self._stored[path] = summary

    def prefetch(self, paths: Iterable[str]) -> None:
        wanted = sorted({
            str(path) for path in paths
            if path and path not in self._parents and path not in self._stored
            and self.allowed(str(path))
        })
        if wanted:
            self._stored.update(dict.fromkeys(wanted))
            self._stored.update(self._load(wanted))

    def parent(self, path: str) -> SelectedParent | None:
        """The admitted page's selected interpretation, or None when unavailable."""
        if path in self._parents:
            return self._parents[path]
        selected = None
        # A page the reader may not see is never interpreted for it.
        if self.allowed(path):
            self.prefetch([path])
            summary = self._stored.pop(path, None)
            try:
                if summary is not None:
                    selected = self.interpretations.parent(path, summary)
            except (ValueError, KeyError, TypeError):
                selected = None
            if selected is None or not selected.structure.complete:
                self.unavailable.add(path)
        self._parents[path] = selected
        return selected


def _registry_hashes(
    language: semantic_language_registry.SemanticLanguageRegistry,
    relations: relation_registry.RelationRegistry,
) -> tuple[str, str]:
    return (
        f"{language.schema_version}:{language.content_hash}",
        f"{relations.core_version}:{relations.extension_hash}",
    )


def current_registry_identity(vault_root: Path) -> tuple[int, str, str]:
    """Current parser and registry stamps, without reading or parsing a parent."""
    hashes = _registry_hashes(
        semantic_language_registry.core_registry(),
        relation_registry.core_registry(),
    )
    return (PARSER_VERSION, *hashes)


def page_projects(frontmatter: Mapping[Any, Any]) -> tuple[str, ...]:
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


def _rows_by_parent(
    rows: list[tuple[Any, ...]],
) -> dict[str, _SidecarParentRows]:
    grouped: dict[str, dict[str, set[Any]]] = {}
    for parent_path, parent_ref, generation, source_hash, parser_version, unit_ref in rows:
        values = grouped.setdefault(
            str(parent_path),
            {"unit_refs": set(), "parent_refs": set(), "stamps": set()},
        )
        values["unit_refs"].add(str(unit_ref))
        if parent_ref:
            values["parent_refs"].add(str(parent_ref))
        values["stamps"].add((str(generation), str(source_hash), int(parser_version)))
    return {
        path: _SidecarParentRows(
            frozenset(values["unit_refs"]),
            frozenset(values["parent_refs"]),
            frozenset(values["stamps"]),
        )
        for path, values in grouped.items()
    }


def _sqlite_unit_rows(path: Path, table: str, key_column: str) -> dict[str, _SidecarParentRows]:
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                f"SELECT parent_path, parent_ref, parent_generation, "
                f"parent_source_hash, parser_version, {key_column} FROM {table}"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return _rows_by_parent(rows)


def _graph_unit_rows(path: Path) -> dict[str, _SidecarParentRows]:
    """Stored structural occurrences per parent, keyed by occurrence key."""
    from .epistemic_graph import CANDIDATE_KIND

    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            node_rows = conn.execute(
                "SELECT path, metadata FROM graph_nodes WHERE kind = ?", (CANDIDATE_KIND,)
            ).fetchall()
            edge_rows = conn.execute(
                "SELECT source_path, metadata FROM graph_edges "
                "WHERE origin IN ('semantic_unit', 'semantic_block')"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    grouped: dict[str, dict[str, set[Any]]] = {}

    def values_for(parent_path: Any) -> dict[str, set[Any]]:
        return grouped.setdefault(
            str(parent_path), {"keys": set(), "stamps": set(), "derived": set()}
        )

    for parent_path, raw_metadata in node_rows:
        try:
            metadata = json.loads(raw_metadata)
        except (TypeError, ValueError):
            continue
        if not isinstance(metadata, dict) or not metadata.get("occurrence_key"):
            continue
        values = values_for(parent_path)
        values["keys"].add(str(metadata["occurrence_key"]))
        values["stamps"].add(
            (
                str(metadata.get("parent_generation") or ""),
                str(metadata.get("parent_source_hash") or ""),
                int(metadata.get("parser_version") or 0),
            )
        )
    for parent_path, raw_metadata in edge_rows:
        try:
            metadata = json.loads(raw_metadata)
        except (TypeError, ValueError):
            continue
        if isinstance(metadata, dict) and metadata.get("occurrence_key"):
            values_for(parent_path)["derived"].add(str(metadata["occurrence_key"]))
    return {
        parent_path: _SidecarParentRows(
            frozenset(values["keys"]),
            frozenset(),
            frozenset(values["stamps"]),
            frozenset(values["derived"]),
        )
        for parent_path, values in grouped.items()
    }


def _occurrence_keys(state: SemanticParentIndexState) -> frozenset[str]:
    """Every structural occurrence a neutral sidecar stores for this parse."""
    if state.candidates is None:
        return frozenset()
    return frozenset(
        semantic_units.occurrence_key(unit.form, unit.span, unit.source_hash)
        for unit in semantic_units.candidate_units(state.candidates)
    )


def _trash_original_paths(vault_root: Path) -> frozenset[str]:
    trash = vault.kb_root(vault_root) / "_trash"
    if not trash.is_dir():
        return frozenset()
    originals: set[str] = set()
    for path in trash.rglob("*.meta.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        original = value.get("original_path") if isinstance(value, dict) else None
        if isinstance(original, str) and original:
            originals.add(original)
    return frozenset(originals)


def audit_semantic_unit_sidecars(
    vault_root: Path,
    expected_states: Mapping[str, SemanticParentIndexState],
    *,
    include_lexical: bool = True,
    include_vectors: bool,
    include_graph: bool,
) -> tuple[SemanticUnitSidecarDrift, ...]:
    """Compare current parsed units with every enabled derived sidecar."""
    from . import epistemic_graph, index_paths, lexstore

    expected = dict(expected_states)
    expected_by_ref = {
        state.parent_ref: path for path, state in expected.items() if state.parent_ref is not None
    }
    # Each sidecar with the keys it stores for one parse: the structural
    # occurrences shared rows hold before any interpretation.
    sidecars: list[
        tuple[
            str,
            dict[str, _SidecarParentRows],
            Callable[[SemanticParentIndexState], frozenset[str]],
        ]
    ] = []
    if include_lexical:
        sidecars.append(
            (
                "lexical",
                _sqlite_unit_rows(lexstore.lexical_path(vault_root), "semantic_units", "unit_ref"),
                _occurrence_keys,
            )
        )
    if include_vectors:
        sidecars.append(
            (
                "vector",
                _sqlite_unit_rows(
                    index_paths.sidecar_path(vault_root), "semantic_unit_vectors", "unit_key"
                ),
                _occurrence_keys,
            )
        )
    if include_graph:
        sidecars.append(
            ("graph", _graph_unit_rows(epistemic_graph.sidecar_path(vault_root)), _occurrence_keys)
        )
    trashed = _trash_original_paths(vault_root)
    drift: list[SemanticUnitSidecarDrift] = []
    generations_by_parent: dict[str, dict[str, frozenset[str]]] = {}
    for sidecar, actual_by_parent, expected_keys in sidecars:
        for parent_path in sorted(set(expected) | set(actual_by_parent)):
            state = expected.get(parent_path)
            actual = actual_by_parent.get(parent_path)
            if state is None:
                if actual is None:
                    continue
                orphan_reasons = {"orphaned"}
                if parent_path in trashed:
                    orphan_reasons.add("trashed")
                moved_to = next(
                    (
                        expected_by_ref[parent_ref]
                        for parent_ref in actual.parent_refs
                        if parent_ref in expected_by_ref
                        and expected_by_ref[parent_ref] != parent_path
                    ),
                    None,
                )
                if moved_to is not None:
                    orphan_reasons.add("moved")
                drift.append(
                    SemanticUnitSidecarDrift(
                        sidecar,
                        parent_path,
                        tuple(sorted(orphan_reasons)),
                        actual_generations=tuple(sorted(stamp[0] for stamp in actual.stamps)),
                    )
                )
                continue
            expected_refs = expected_keys(state)
            if actual is None:
                if expected_refs:
                    drift.append(
                        SemanticUnitSidecarDrift(
                            sidecar,
                            parent_path,
                            ("missing",),
                            state.parent_generation,
                        )
                    )
                continue
            actual_generations = frozenset(stamp[0] for stamp in actual.stamps)
            generations_by_parent.setdefault(parent_path, {})[sidecar] = actual_generations
            reasons: set[str] = set()
            if actual.unit_refs != expected_refs:
                reasons.add("unit_set_mismatch")
            expected_stamp = (
                state.parent_generation,
                state.parent_source_hash,
                state.parser_version,
            )
            if actual.stamps != frozenset({expected_stamp}):
                reasons.add("stale")
            if len(actual.stamps) > 1:
                reasons.add("mixed_generation")
            if (
                sidecar == "graph"
                and actual.derived_refs is not None
                and actual.derived_refs != expected_refs
            ):
                reasons.add("missing_derived_edge")
            if reasons:
                drift.append(
                    SemanticUnitSidecarDrift(
                        sidecar,
                        parent_path,
                        tuple(sorted(reasons)),
                        state.parent_generation,
                        tuple(sorted(actual_generations)),
                    )
                )
    for parent_path, generations in sorted(generations_by_parent.items()):
        combined = {value for values in generations.values() for value in values}
        if len(combined) > 1:
            state = expected.get(parent_path)
            drift.append(
                SemanticUnitSidecarDrift(
                    "cross_sidecar",
                    parent_path,
                    ("mixed_generation",),
                    state.parent_generation if state is not None else None,
                    tuple(sorted(combined)),
                )
            )
    return tuple(sorted(drift, key=lambda item: (item.parent_path, item.sidecar)))
