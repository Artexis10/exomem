"""Shared deterministic generation state for derived semantic-unit indexes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import memory_refs, relation_registry, semantic_language_registry, semantic_units, vault

PARSER_VERSION = 4
_GENERATION_SCHEMA = "exomem.semantic-unit.parent-generation.v4"
_ACTIVE_PARENT_STATES: ContextVar[Mapping[str, SemanticParentIndexState] | None] = (
    ContextVar("exomem_semantic_parent_states", default=None)
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


def parent_state_for_path(
    vault_root: Path, path: Path | str
) -> SemanticParentIndexState | None:
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
    language = semantic_language_registry.load_registry(root)
    relations = relation_registry.load_registry(root)
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
    frontmatter, body, _ = vault.parse_frontmatter(source)
    parent_ref = memory_refs.ref_from_markdown(source)
    page_type = str(frontmatter["type"]) if frontmatter.get("type") else None
    projects = _page_projects(frontmatter)
    language = semantic_language_registry.load_registry(root)
    relations = relation_registry.load_registry(root)
    language_hash, relation_hash = _registry_hashes(language, relations)
    document = semantic_units.parse_semantic_units(
        body,
        path=rel_path,
        parent_ref=parent_ref,
        validate=True,
        language_registry=semantic_language_registry.for_attached_projects(
            language, projects
        ),
        relation_registry=relations,
        include_legacy_relations=True,
        retain_unknown_relations=True,
        project=None,
        page_type=page_type,
    )
    source_hash = vault.content_hash(source)
    return SemanticParentIndexState(
        path=rel_path,
        parent_ref=parent_ref,
        parent_source_hash=source_hash,
        language_registry_hash=language_hash,
        relation_registry_hash=relation_hash,
        parent_generation=parent_generation(
            parent_path=rel_path,
            parent_ref=parent_ref,
            parent_source_hash=source_hash,
            language_registry_hash=language_hash,
            relation_registry_hash=relation_hash,
        ),
        parser_version=PARSER_VERSION,
        document=document,
    )


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
    active = parent_state_for_path(root, path)
    language = semantic_language_registry.load_registry(root)
    relations = relation_registry.load_registry(root)
    language_hash, relation_hash = _registry_hashes(language, relations)
    if (
        active is not None
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
        memory_refs.memory_ref(str(state.identity))
        if state.identity_kind == "exomem_id"
        else None
    )
    return SemanticParentIndexState(
        path=path,
        parent_ref=parent_ref,
        parent_source_hash=source_hash,
        language_registry_hash=str(state.language_registry_hash),
        relation_registry_hash=str(state.relation_registry_hash),
        parent_generation=parent_generation(
            parent_path=path,
            parent_ref=parent_ref,
            parent_source_hash=source_hash,
            language_registry_hash=str(state.language_registry_hash),
            relation_registry_hash=str(state.relation_registry_hash),
        ),
        parser_version=PARSER_VERSION,
        document=state.document,
    )


def _registry_hashes(
    language: semantic_language_registry.SemanticLanguageRegistry,
    relations: relation_registry.RelationRegistry,
) -> tuple[str, str]:
    return (
        f"{language.schema_version}:{language.content_hash}",
        f"{relations.core_version}:{relations.extension_hash}",
    )


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
        values["stamps"].add(
            (str(generation), str(source_hash), int(parser_version))
        )
    return {
        path: _SidecarParentRows(
            frozenset(values["unit_refs"]),
            frozenset(values["parent_refs"]),
            frozenset(values["stamps"]),
        )
        for path, values in grouped.items()
    }


def _sqlite_unit_rows(path: Path, table: str) -> dict[str, _SidecarParentRows]:
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                f"SELECT parent_path, parent_ref, parent_generation, "
                f"parent_source_hash, parser_version, unit_ref FROM {table}"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return _rows_by_parent(rows)


def _graph_unit_rows(path: Path) -> dict[str, _SidecarParentRows]:
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            node_rows = conn.execute(
                "SELECT path, metadata FROM graph_nodes"
            ).fetchall()
            edge_rows = conn.execute(
                "SELECT source_path, metadata FROM graph_edges "
                "WHERE relation_type = 'derived_from'"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    grouped: dict[str, dict[str, set[Any]]] = {}
    for parent_path, raw_metadata in node_rows:
        try:
            metadata = json.loads(raw_metadata)
        except (TypeError, ValueError):
            continue
        if metadata.get("record_type") != "semantic_unit" or not metadata.get(
            "unit_ref"
        ):
            continue
        values = grouped.setdefault(
            str(parent_path),
            {
                "unit_refs": set(),
                "parent_refs": set(),
                "stamps": set(),
                "derived_refs": set(),
            },
        )
        values["unit_refs"].add(str(metadata["unit_ref"]))
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
        if metadata.get("record_type") != "semantic_unit" or not metadata.get(
            "unit_ref"
        ):
            continue
        values = grouped.setdefault(
            str(parent_path),
            {
                "unit_refs": set(),
                "parent_refs": set(),
                "stamps": set(),
                "derived_refs": set(),
            },
        )
        values["derived_refs"].add(str(metadata["unit_ref"]))
    return {
        parent_path: _SidecarParentRows(
            frozenset(values["unit_refs"]),
            frozenset(),
            frozenset(values["stamps"]),
            frozenset(values["derived_refs"]),
        )
        for parent_path, values in grouped.items()
    }


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
        state.parent_ref: path
        for path, state in expected.items()
        if state.parent_ref is not None
    }
    sidecars: list[tuple[str, dict[str, _SidecarParentRows]]] = []
    if include_lexical:
        sidecars.append(
            (
                "lexical",
                _sqlite_unit_rows(
                    lexstore.lexical_path(vault_root), "semantic_units"
                ),
            )
        )
    if include_vectors:
        sidecars.append(
            (
                "vector",
                _sqlite_unit_rows(
                    index_paths.sidecar_path(vault_root), "semantic_unit_vectors"
                ),
            )
        )
    if include_graph:
        sidecars.append(
            ("graph", _graph_unit_rows(epistemic_graph.sidecar_path(vault_root)))
        )
    trashed = _trash_original_paths(vault_root)
    drift: list[SemanticUnitSidecarDrift] = []
    generations_by_parent: dict[str, dict[str, frozenset[str]]] = {}
    for sidecar, actual_by_parent in sidecars:
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
                        actual_generations=tuple(
                            sorted(stamp[0] for stamp in actual.stamps)
                        ),
                    )
                )
                continue
            expected_refs = frozenset(
                unit.unit_ref
                for unit in state.document.units
                if unit.unit_ref is not None
            )
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
            generations_by_parent.setdefault(parent_path, {})[sidecar] = (
                actual_generations
            )
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
