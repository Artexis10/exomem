"""Batched, fingerprint-guarded relation-acceptance queue.

Assembles deterministic `suggest_relations` candidates across the
activation-eligible corpus into a read-only review batch. Nothing here mutates
the vault: candidates are proposed, filtered at read time, and only a separate
governed accept (`accept`) or triage decision writes anything.

Identity and fingerprints reuse the existing review-state machinery
(`review_state`) so decisions key on `review_id:signal_fingerprint` exactly like
the activation and attention queues. Relation refs are namespaced under
`exomem://review/relation/<id>` so they never resolve — and are never resolved
by — activation or attention items (the #198 isolation rule).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import activation as activation_module
from . import (
    context_refs,
    note_types,
    recall_policy,
    relation_endpoints,
    relation_options,
    relation_registry,
    semantic_language_registry,
    semantic_units,
)
from . import epistemic_graph as epistemic_graph_module
from . import find as find_module
from . import graph_sync as graph_sync_module
from . import review_state as review_state_module
from . import semantic_contract as semantic_contract_module
from . import vault as vault_module
from .cli_ops import OpError
from .kbdir import kb_dirname
from .vault import kb_root

RELATION_REVIEW_PREFIX = "exomem://review/relation/"
_DEFAULT_LIMIT_PAGES = 50
_DEFAULT_LIMIT_PER_PAGE = 10


@dataclass(frozen=True)
class ResolvedCandidate:
    review_id: str
    ref: str
    fingerprint: str
    candidate: dict[str, Any]
    target_ref: str
    source_page: Any | None = None
    identity_refs: tuple[str, str] | None = None


def is_relation_ref(value: str) -> bool:
    return str(value or "").strip().startswith(RELATION_REVIEW_PREFIX)


def relation_review_ref(review_id: str) -> str:
    clean = str(review_id or "").strip().lower()
    if len(clean) != 24 or any(char not in "0123456789abcdef" for char in clean):
        raise ValueError(
            f"INVALID_REVIEW_REFERENCE: invalid relation review id {review_id!r}"
        )
    return f"{RELATION_REVIEW_PREFIX}{clean}"


def parse_relation_review_ref(value: str) -> str:
    raw = str(value or "").strip()
    if not raw.startswith(RELATION_REVIEW_PREFIX):
        raise ValueError(
            f"INVALID_REVIEW_REFERENCE: expected {RELATION_REVIEW_PREFIX}<id>"
        )
    raw_id = raw[len(RELATION_REVIEW_PREFIX) :].lower()
    if len(raw_id) != 24 or any(char not in "0123456789abcdef" for char in raw_id):
        raise ValueError(
            f"INVALID_REVIEW_REFERENCE: invalid relation review reference {value!r}"
        )
    return raw_id


def _candidate_identity(candidate: dict[str, Any]) -> str:
    payload = "|".join(
        str(candidate.get(key) or "")
        for key in ("from", "to", "relation_type", "method")
    )
    return review_state_module.item_id(f"relation:{payload}")


def _bullet(candidate: dict[str, Any]) -> str:
    # A candidate with no relation names nothing to write; the generic relation is never assumed.
    relation = candidate.get("authored_label") or candidate.get("relation_type")
    if not relation:
        raise ValueError("INVALID_SELECTED_RELATION: the candidate names no relation to write")
    destination = str(candidate.get("to") or "").removesuffix(".md")
    return f"- {relation} [[{destination}]]"


def _usable_relation(
    registry: relation_registry.RelationRegistry,
    requested: str,
    page: Any,
    target: Any,
) -> str | None:
    """The canonical key when `requested` is active between these two pages, else None.

    The one rule for "this relation may be written between the reviewed endpoints", shared
    by the selection below and by the options a generated candidate offers.
    """
    source_project = page.frontmatter.get("project")
    target_project = target.frontmatter.get("project")
    if not isinstance(source_project, str):
        source_project = None
    if not isinstance(target_project, str):
        target_project = None
    resolved = semantic_contract_module._registry_resolution(
        registry,
        requested,
        projects=(source_project,) if source_project is not None else (),
        page_type=page.page_type,
        source_kind=page.page_type,
        target_kind=target.page_type,
        origin="markdown_relation",
    )
    definition, canonical = resolved.definition, resolved.canonical
    if (
        definition is None
        or canonical is None
        or resolved.status not in {"core", "extension", "alias"}
        or (definition.projects and target_project not in definition.projects)
    ):
        return None
    return canonical


def selected_relation(
    vault_root: Path,
    candidate: dict[str, Any],
    requested_relation: str | None,
    *,
    source_page: Any | None = None,
) -> dict[str, Any]:
    """Return the registered relation explicitly selected for reviewed endpoints."""
    if requested_relation is None:
        return dict(candidate)
    if not isinstance(requested_relation, str) or not requested_relation.strip():
        raise ValueError("INVALID_SELECTED_RELATION: a registered relation label is required")
    page = source_page or _page_for(vault_root, str(candidate.get("from") or ""))
    target = _page_for(vault_root, str(candidate.get("to") or ""))
    if page is None or target is None:
        raise ValueError("REVIEW_ITEM_CHANGED: refresh the relation candidate")
    registry = relation_registry.load_for_page(vault_root, page.rel_path, page.frontmatter)
    canonical = _usable_relation(registry, requested_relation, page, target)
    if canonical is None:
        raise ValueError("INVALID_SELECTED_RELATION: relation is not active for reviewed endpoints")
    return {
        **candidate,
        "relation_type": canonical,
        "authored_label": relation_registry.authored_label(registry, requested_relation, canonical),
    }


class RelationFit:
    """The specific relations that fit a generated candidate's two pages.

    A generator proposes the generic relation when its evidence shows two pages are related
    and nothing more (shared sources, a shared question). When the registry names a specific
    relation that joins those pages, the candidate carries it as an option and the agent
    chooses; `relation_options.fitting_options` is the one producer. Page lookups are held for
    one read, so a page that many candidates name is read once.
    """

    def __init__(self, vault_root: Path, *, basis: note_types.Basis | None = None) -> None:
        self._root = Path(vault_root)
        self._pages: dict[str, Any | None] = {}
        self._basis = basis or note_types.Basis(self._root)
        self._entity_types: Any = None

    def _types(self) -> Any:
        if self._entity_types is None:
            self._entity_types = semantic_contract_module.entity_type_registry(self._root)
        return self._entity_types

    def _page(self, rel_path: str) -> Any | None:
        rel = epistemic_graph_module._page_rel(rel_path)
        if rel not in self._pages:
            # A page recall policy keeps out is read no further, so it adds nothing to an option list.
            allowed = bool(rel) and recall_policy.is_recall_candidate(self._root, self._root / rel)
            self._pages[rel] = _page_for(self._root, rel) if allowed else None
        return self._pages[rel]

    def _kind(self, page: Any) -> relation_endpoints.PageKind | None:
        # Mirrors `semantic_contract.endpoint_page_kind` for a parsed page rather than a write state.
        resolved = self._basis.resolve(
            semantic_contract_module.normalized_compiled_type(page.page_type)
        )
        if resolved.note_type is None:
            return None
        return relation_endpoints.page_kind(
            resolved.note_type.role, page.frontmatter.get("entity_type"), self._types()
        )

    def options(
        self,
        candidate: dict[str, Any],
        *,
        registry: relation_registry.RelationRegistry | None = None,
        source_page: Any | None = None,
        relation: str | None = None,
    ) -> list[dict[str, Any]]:
        """Up to four fitting relations when the relation is the generic one, else none.

        `relation` is the one under judgement: the candidate's own, unless an agent asked for another.
        """
        page = source_page
        if registry is None:
            page = page or self._page(str(candidate.get("from") or ""))
            if page is None:
                return []
            registry = relation_registry.load_for_page(self._root, page.rel_path, page.frontmatter)
        definition = registry.definition(str(relation or candidate.get("relation_type") or ""))
        # Generic is a registry attribute (`generic`); no relation key is compared here.
        if definition is None or not definition.generic:
            return []
        page = page or self._page(str(candidate.get("from") or ""))
        target = self._page(str(candidate.get("to") or ""))
        if page is None or target is None:
            return []
        subject = self._kind(page)
        other = self._kind(target)
        if subject is None or other is None:
            return []
        return relation_options.fitting_options(
            registry,
            subject,
            other,
            self._types(),
            accepts=lambda key: _usable_relation(registry, key, page, target) is not None,
        )


#: What the agent does when the queue will not write the generic relation for a pair a specific one
#: fits. Naming a relation here takes a recorded vocabulary choice, so the steps run in this order.
RELATION_CHOICE_REMEDIATION = (
    "Choose one of the offered relations. Ask a vocabulary question for this candidate, "
    "record a reuse decision that names the relation, then accept again with requested_relation, "
    "vocabulary_ref and vocabulary_fingerprint. Or dismiss the candidate. "
    "A generic edge with a reason can be authored on the page."
)


def require_relation_choice(
    vault_root: Path,
    candidate: dict[str, Any],
    requested_relation: str | None,
    *,
    page: Any | None = None,
    fit: RelationFit | None = None,
    remediation: str = RELATION_CHOICE_REMEDIATION,
) -> None:
    """Refuse to accept the generic relation where a specific one fits; the agent chooses.

    Naming no relation takes the candidate's own, so a generic candidate is refused. Naming
    the generic relation is refused too. Any other registered relation passes. `remediation`
    lets a caller whose door takes other fields name its own next step.
    """
    page = page or _page_for(vault_root, str(candidate.get("from") or ""))
    if page is None:
        return
    registry = relation_registry.load_for_page(vault_root, page.rel_path, page.frontmatter)
    fit = fit or RelationFit(vault_root)
    relation = None
    if requested_relation is not None:
        chosen = selected_relation(vault_root, candidate, requested_relation, source_page=page)
        relation = str(chosen.get("relation_type") or "")
    options = fit.options(candidate, registry=registry, source_page=page, relation=relation)
    if options:
        raise OpError(
            "RELATION_CHOICE_REQUIRED",
            "a specific relation fits these two pages, so the generic relation is not chosen for you",
            remediation,
            details={"options": options, "mutated": False},
        )


def _evidence_signal_version(page: Any, candidate: dict[str, Any]) -> str:
    """A version string that changes whenever the candidate's evidence does.

    `review_state.fingerprint()` uses `meta.signal_version` verbatim as the
    reason's version WHENEVER it is supplied, ignoring `detail` entirely (see
    `review_state.fingerprint`). Folding in only the source page's own
    `activation._signal_version` (as the activation/attention queues do,
    since their findings are entirely about the source page) would miss
    candidate methods whose evidence is driven by a DIFFERENT page or the
    corpus index — `shared_sources` (a neighbour page's edge), and
    `embedding_proximity` (a corpus-wide cosine score) — so an edit to that
    OTHER page would never resurface a dismissed candidate. Hashing the
    source page's signal version together with the serialized evidence (and
    the candidate's own to/method/relation_type) means any of those changes
    is reflected, regardless of which page produced them.
    """
    payload = {
        "page_signal_version": activation_module._signal_version(page),
        "method": str(candidate.get("method") or ""),
        "relation_type": str(candidate.get("relation_type") or ""),
        "to": str(candidate.get("to") or ""),
        "evidence": candidate.get("evidence") or {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return vault_module.content_hash(encoded)[:16]


def _candidate_fingerprint(
    candidate: dict[str, Any],
    *,
    from_ref: str,
    to_ref: str,
    signal_version: str,
) -> str:
    reason = {
        "category": str(candidate.get("method") or ""),
        "detail": json.dumps(candidate.get("evidence") or {}, sort_keys=True),
        "related_paths": [str(candidate.get("to") or "")],
        "meta": {"signal_version": signal_version},
    }
    return review_state_module.fingerprint(
        target_ref=from_ref,
        categories=[str(candidate.get("relation_type") or "")],
        reasons=[reason],
        related_refs=[to_ref],
    )


def _relation_document(page: Any, vault_root: Path) -> Any:
    """Parse one already-read source with the current semantic registries."""
    return semantic_units.parse_semantic_units(
        page.body,
        validate=False,
        language_registry=semantic_language_registry.load_registry(vault_root),
        relation_registry=relation_registry.load_for_page(
            vault_root, page.rel_path, page.frontmatter
        ),
        page_type=page.page_type,
    )


def _authored_targets(
    page: Any,
    vault_root: Path,
    *,
    resolver: vault_module.WikilinkResolver | None = None,
) -> set[tuple[str, str]]:
    """Set of `(relation_type, target.md)` already authored under ``## Relations``.

    `resolver` is a caller-built resolver (the dreamer builds one from the graph
    snapshot, with no I/O); without one, resolution builds a whole-vault resolver.
    """
    document = _relation_document(page, vault_root)
    authored: set[tuple[str, str]] = set()
    for relation in document.canonical_note_relations:
        try:
            canonical, warning = vault_module.normalize_wikilink(
                epistemic_graph_module._page_target(relation.target),
                vault_root,
                resolver=resolver,
                strict=False,
            )
        except Exception:  # noqa: BLE001 - malformed authored links are ignored
            continue
        if warning:
            continue
        authored.add((relation.kind, epistemic_graph_module._page_rel(canonical)))
    return authored


def _is_placeholder_target(vault_root: Path, target: str) -> bool:
    return not (Path(vault_root) / epistemic_graph_module._page_rel(target)).is_file()


def _current_candidate_is_authored(
    vault_root: Path,
    page: Any,
    candidate: dict[str, Any],
) -> bool | None:
    """Check current Markdown using exact cached resolver authority."""
    relation_type = str(candidate.get("relation_type") or "")
    target = epistemic_graph_module._page_rel(str(candidate.get("to") or ""))
    if not relation_type or not target:
        return False
    matching_relations = tuple(
        relation
        for relation in _relation_document(page, vault_root).canonical_note_relations
        if relation.kind == relation_type
    )
    if not matching_relations:
        return False
    snapshot = semantic_contract_module.current_reference_identity_snapshot(vault_root)
    if snapshot is None:
        return None
    for relation in matching_relations:
        resolution = semantic_contract_module.resolve_reference_wikilink(
            vault_root,
            snapshot,
            str(relation.target or ""),
        )
        if resolution.status == "unavailable":
            return None
        if resolution.status == "resolved" and resolution.path == target:
            return True
    if not semantic_contract_module.reference_identity_snapshot_is_current(
        vault_root, snapshot
    ):
        return None
    return False


def _eligible_pages(vault_root: Path) -> list[Any]:
    kb = kb_root(vault_root)
    if not kb.is_dir():
        return []
    pages: list[Any] = []
    for path in find_module._walk_md(kb):
        try:
            page = find_module._parse_page(path, path.stat().st_mtime, vault_root)
        except OSError:
            continue
        if page is None or not activation_module._eligible(vault_root, page):
            continue
        pages.append(page)
    return pages


def _ordered_pages(vault_root: Path, scan: Any) -> list[Any]:
    """Eligible pages ordered by activation rank, then path (deterministic)."""
    rank: dict[str, int] = {}
    for index, finding in enumerate(scan.findings):
        rank.setdefault(finding.path, index)
    pages = _eligible_pages(vault_root)
    return sorted(
        pages,
        key=lambda page: (rank.get(page.rel_path, len(scan.findings)), page.rel_path),
    )


def _page_content_hash(page: Any) -> str:
    try:
        # Raw bytes, not `read_text`: `content_hash` is defined as the sha256
        # of a file's full raw text and `get_page` hands out exactly that, so a
        # hash taken over the newline-normalized form disagrees with it on every
        # CRLF page -- and the caller echoes this one straight back into
        # `edit(expected_hash=...)`, which then refuses the write.
        return vault_module.content_hash(page.path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        return ""


#: How far past the display budget candidate generation reaches, so that
#: classification has something to discard.
#:
#: `suggest_relations` truncates at the limit it is given, and the queue only
#: then drops the already-authored, placeholder-target and already-decided
#: candidates — so those drops used to come out of the display budget. A page
#: whose first `limit_per_page` candidates were all authored produced an empty
#: group, and the genuinely open candidates behind them never surfaced on any
#: read, however many times the queue was rebuilt.
#:
#: Decision regeneration excludes embeddings, but each deterministic method is
#: still capped to the same supported 64-candidate prefix as Lane B.  The cap is
#: PER METHOD: a visible item can legitimately follow a full filtered prefix
#: from an earlier method.
_CLASSIFICATION_HEADROOM = 4
_MAX_GENERATED_PER_METHOD = 64


def _body_wikilink_candidates(
    vault_root: Path,
    page: Any,
    *,
    limit: int,
    snapshot: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Reproduce Lane B's indexed body-wikilink evidence for one source.

    `snapshot` is a validated graph read snapshot the caller already holds; it
    is used as is and left open.
    """
    document = _relation_document(page, vault_root)
    canonical_lines = {
        relation.line for relation in document.note_relations if relation.canonical
    }
    index = epistemic_graph_module.EpistemicGraphIndex(vault_root)
    connection = snapshot if snapshot is not None else index._open_read_snapshot()
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT d.path, d.title, e.review_evidence "
            "FROM graph_edges e JOIN graph_nodes d "
            "ON d.node_key = e.dst_key AND d.kind = 'file' "
            "WHERE e.origin = 'wikilink' AND e.source_path = ? "
            "ORDER BY COALESCE(CAST(json_extract(e.review_evidence, "
            "'$.internal.occurrence') AS INTEGER), e.rowid), d.path, e.rowid "
            "LIMIT ?",
            (page.rel_path, max(0, int(limit))),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if snapshot is None:
            connection.close()

    indexed: dict[tuple[Any, ...], dict[str, Any]] = {}
    resolver_entries: list[tuple[str, str | None]] = []
    for target_path, title, raw_evidence in rows:
        try:
            payload = json.loads(str(raw_evidence or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        evidence = payload.get("evidence") or {}
        internal = payload.get("internal") or {}
        key = (
            str(target_path),
            str(evidence.get("target") or ""),
            int(internal.get("occurrence", -1)),
            int(internal.get("start", -1)),
            int(internal.get("end", -1)),
            int(internal.get("line", -1)),
        )
        indexed[key] = evidence
        resolver_entries.append((str(target_path), str(title) if title else None))
    resolver = vault_module.WikilinkResolver.from_entries(
        vault_root,
        resolver_entries,
    )
    observations = epistemic_graph_module._body_wikilink_observations(
        vault_root,
        page.body,
        skip_lines=canonical_lines,
        resolver=resolver,
    )
    candidates: list[dict[str, Any]] = []
    for observation in observations:
        key = (
            str(observation["target_path"]),
            str(observation["target"]),
            int(observation["occurrence"]),
            int(observation["start"]),
            int(observation["end"]),
            int(observation["line"]),
        )
        evidence = indexed.get(key)
        if evidence is None:
            continue
        candidates.append(
            {
                "from": page.rel_path,
                "to": str(observation["target_path"]),
                "relation_type": "links_to",
                "method": "wikilink",
                "evidence": evidence,
            }
        )
    return candidates


def _shared_source_candidates(
    vault_root: Path,
    rel_path: str,
    *,
    limit: int,
    snapshot: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Reproduce Lane B's bounded shared-source candidates for one source."""
    index = epistemic_graph_module.EpistemicGraphIndex(vault_root)
    connection = snapshot if snapshot is not None else index._open_read_snapshot()
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT e2.source_path, e1.dst_key "
            "FROM graph_edges e1 JOIN graph_edges e2 ON e2.dst_key = e1.dst_key "
            "JOIN graph_nodes d ON d.node_key = ('file:' || e2.source_path) "
            "WHERE e1.source_path = ? "
            "AND e1.origin = 'frontmatter' AND e1.source_anchor = 'sources' "
            "AND e1.relation_type = 'derived_from' "
            "AND e2.origin = 'frontmatter' AND e2.source_anchor = 'sources' "
            "AND e2.relation_type = 'derived_from' "
            "AND e2.source_path <> e1.source_path "
            "AND NOT EXISTS (SELECT 1 FROM graph_edges p "
            "WHERE p.src_key = ('file:' || e1.source_path) "
            "AND p.dst_page_key = ('file:' || e2.source_path) "
            "AND p.relation_type = ?) "
            "ORDER BY e2.source_path, e1.dst_key LIMIT ?",
            (rel_path, relation_registry.generic_relation_key(), max(0, int(limit))),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if snapshot is None:
            connection.close()
    return [
        {
            "from": rel_path,
            "to": epistemic_graph_module._page_rel(str(target_path)),
            "relation_type": relation_registry.generic_relation_key(),
            "method": "shared_sources",
            "evidence": {
                "shared_source": epistemic_graph_module._page_rel(
                    str(shared_key or "").removeprefix("file:")
                )
            },
        }
        for target_path, shared_key in rows
    ]


def _page_candidates(
    vault_root: Path,
    page: Any,
    *,
    limit_per_page: int,
    snapshot: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Re-derive one source's deterministic candidate neighborhood.

    This is the decision-time compatibility leaf, not queue assembly.  It may
    inspect the one hinted page and bounded graph neighborhoods, but deliberately
    excludes embedding proximity.  Explicit per-page ``suggest_relations`` keeps
    that discovery method; relation review decisions never recompute it.

    A caller that already holds a validated graph read snapshot passes it as
    `snapshot`, so the three graph-backed generators share it instead of each
    validating their own; it is left open.
    """
    budget = max(0, int(limit_per_page))
    method_cap = min(
        _MAX_GENERATED_PER_METHOD,
        max(1, budget * _CLASSIFICATION_HEADROOM),
    )
    generated = [
        *epistemic_graph_module._structural_candidates(
            vault_root, page.rel_path, connection=snapshot
        )[:method_cap],
        *_body_wikilink_candidates(vault_root, page, limit=method_cap, snapshot=snapshot),
        *epistemic_graph_module._frontmatter_source_candidates(page)[:method_cap],
        *_shared_source_candidates(
            vault_root, page.rel_path, limit=method_cap, snapshot=snapshot
        ),
    ]
    return epistemic_graph_module._dedupe_candidates(generated)


def _page_for(vault_root: Path, rel_path: str) -> Any | None:
    """Re-read one page fresh from disk, for accept's live re-validation.

    Deliberately bypasses any process-level cache: accept must judge the
    candidate against the file's CURRENT state, not a read from earlier in
    this call (or an earlier request). Returns `None` if the page is gone.
    """
    try:
        path, _rel = vault_module.resolve_under_vault(
            Path(vault_root),
            str(rel_path or ""),
            must_exist=True,
            must_be_file=True,
            must_be_under_kb=True,
        )
    except vault_module.VaultPathError:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return find_module._parse_page(path, mtime, Path(vault_root))


def _fallback_ref(rel_path: str) -> str:
    if rel_path.startswith(f"{kb_dirname()}/Sources/"):
        return context_refs.source_ref(rel_path)
    return context_refs.vault_ref(rel_path)


def _hinted_candidate_refs(
    vault_root: Path,
    candidate: dict[str, Any],
    *,
    snapshot: sqlite3.Connection | None = None,
) -> tuple[str, str] | None:
    """Derive Lane B's exact refs without opening or repairing the refs sidecar.

    `snapshot` is a validated graph read snapshot the caller already holds; it
    is used as is and left open.
    """
    from_path = epistemic_graph_module._page_rel(str(candidate.get("from") or ""))
    to_path = epistemic_graph_module._page_rel(str(candidate.get("to") or ""))
    paths = tuple(dict.fromkeys((from_path, to_path)))
    if not all(paths):
        return None
    identity_snapshot = semantic_contract_module.current_reference_identity_snapshot(
        vault_root
    )
    index = epistemic_graph_module.EpistemicGraphIndex(vault_root)
    connection = snapshot if snapshot is not None else index._open_read_snapshot()
    if connection is None:
        return None
    try:
        placeholders = ",".join("?" for _ in paths)
        rows = connection.execute(
            "SELECT path, exomem_id FROM graph_nodes "
            f"WHERE kind = 'file' AND path IN ({placeholders}) ORDER BY path",
            paths,
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        if snapshot is None:
            connection.close()
    identities = {str(path): exomem_id for path, exomem_id in rows}
    if set(identities) != set(paths):
        return None
    if identity_snapshot is None:
        if (
            semantic_contract_module.current_reference_identity_snapshot(vault_root)
            is not None
        ):
            return None
    elif not semantic_contract_module.reference_identity_snapshot_is_current(
        vault_root, identity_snapshot
    ):
        return None
    if any(exomem_id is not None for exomem_id in identities.values()):
        if identity_snapshot is None or not set(paths).issubset(
            identity_snapshot.reference_paths
        ):
            return None
        refs = {
            path: identity_snapshot.canonical_refs_by_path[path]
            or _fallback_ref(path)
            for path in paths
        }
    else:
        refs = {path: _fallback_ref(path) for path in paths}
    return refs[from_path], refs[to_path]


def _enrich(
    vault_root: Path,
    page: Any,
    candidate: dict[str, Any],
    *,
    exact_refs: tuple[str, str] | None = None,
    fit: RelationFit | None = None,
) -> dict[str, Any]:
    from_path = str(candidate.get("from") or page.rel_path)
    to_path = str(candidate.get("to") or "")
    if exact_refs is None:
        refs = review_state_module.refs_for_paths(vault_root, [from_path, to_path])
        from_ref = refs.get(from_path, from_path)
        to_ref = refs.get(to_path, to_path)
    else:
        from_ref, to_ref = exact_refs
    review_id = _candidate_identity(candidate)
    fingerprint = _candidate_fingerprint(
        candidate,
        from_ref=from_ref,
        to_ref=to_ref,
        signal_version=_evidence_signal_version(page, candidate),
    )
    # Where a specific relation fits, the item offers it and no bullet: the agent picks the relation.
    options = (fit or RelationFit(vault_root)).options(candidate, source_page=page)
    item = {
        "review_id": review_id,
        "ref": relation_review_ref(review_id),
        "fingerprint": fingerprint,
        "from": from_path,
        "to": to_path,
        "relation_type": candidate.get("relation_type"),
        "method": candidate.get("method"),
        "evidence": candidate.get("evidence") or {},
        "bullet": None if options else _bullet(candidate),
        "target_ref": from_ref,
        "state": "open",
    }
    if options:
        item["options"] = options
    return item


def _classify_candidate(
    vault_root: Path,
    page: Any,
    candidate: dict[str, Any],
    *,
    store: review_state_module.ReviewStateStore,
    state_payload: dict[str, Any],
    authored: set[tuple[str, str]] | None = None,
    exact_refs: tuple[str, str] | None = None,
    today=None,
    fit: RelationFit | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Apply the three read-time eligibility filters to one candidate.

    Shared by `build_queue` (batch read) and `accept` (single-candidate
    re-validation immediately before writing) so the two paths can never
    disagree about what counts as an open, acceptable candidate.

    Returns `(reason, enriched)`: `reason` is `None` when the candidate is
    open, else one of "authored_edge", "placeholder_target", "decided".
    `enriched` (the review-identity-bearing item) is computed only once the
    cheap authored/placeholder checks pass; it is `None` when filtered by
    either of those, since nothing downstream needs it.
    """
    if authored is None:
        authored = _authored_targets(page, vault_root)
    relation_type = str(candidate.get("relation_type") or "")
    to_path = str(candidate.get("to") or "")
    if (relation_type, epistemic_graph_module._page_rel(to_path)) in authored:
        return "authored_edge", None
    if _is_placeholder_target(vault_root, to_path):
        return "placeholder_target", None
    enriched = _enrich(vault_root, page, candidate, exact_refs=exact_refs, fit=fit)
    effective, _decision = store.effective_state(
        enriched["review_id"],
        enriched["fingerprint"],
        today=today,
        payload=state_payload,
    )
    if effective != "open":
        return "decided", enriched
    return None, enriched


def build_queue(
    vault_root: Path,
    *,
    limit_pages: int = _DEFAULT_LIMIT_PAGES,
    limit_per_page: int = _DEFAULT_LIMIT_PER_PAGE,
    today=None,
) -> dict[str, Any]:
    """Assemble one bounded graph-native relation-acceptance queue.

    The queue is the owner's: another audience receives the
    `audience_restricted` refusal before anything is read.
    """
    from . import lifecycle_statuses
    from .governance import egress

    refusal = egress.owner_only_aggregate(vault_root)
    if refusal is not None:
        return refusal
    vault_root = Path(vault_root)
    batch = epistemic_graph_module.EpistemicGraphIndex(
        vault_root
    ).relation_review_batch(
        limit_pages=limit_pages,
        limit_per_page=limit_per_page,
        status_basis=lifecycle_statuses.Basis(vault_root),
    )
    status = str(batch.get("status") or "warming")
    if status != "available":
        if not epistemic_graph_module.graph_enabled():
            status = "unavailable"
        else:
            sync_state = str(graph_sync_module.status(vault_root).get("state") or "")
            if sync_state == "recovery_required":
                status = "pending"
            elif sync_state == "unavailable":
                status = "unavailable"
            elif status not in {"warming", "pending", "unavailable"}:
                status = "warming"
        return {
            **batch,
            "status": status,
            "mode": "relation-queue",
            "mutated": False,
            "groups": [],
            "shown": 0,
            "pages_shown": 0,
            "retryable": True,
            "retry_after": "graph-current",
            "next_action": "retry-relation-queue",
        }

    groups: list[dict[str, Any]] = []
    for raw_group in batch.get("groups") or []:
        group = dict(raw_group)
        source_path = str(group.get("source_path") or group.get("path") or "")
        source_hash = str(
            group.get("source_content_hash") or group.get("content_hash") or ""
        )
        group["source_path"] = source_path
        group["source_content_hash"] = source_hash
        items: list[dict[str, Any]] = []
        for raw_item in group.get("items") or []:
            item = dict(raw_item)
            item["source_path"] = str(item.get("source_path") or source_path)
            item["source_content_hash"] = str(
                item.get("source_content_hash") or source_hash
            )
            items.append(item)
        group["items"] = items
        groups.append(group)
    return {
        **batch,
        "status": "available",
        "mode": "relation-queue",
        "mutated": False,
        "groups": groups,
        "retryable": False,
    }


def _refuse_other_audiences(vault_root: Path) -> None:
    """Relation review is owner work.

    A candidate names pages as resolved over the whole vault, so another
    audience is refused before any candidate is resolved or read, with one
    answer whatever the reference names.
    """
    from .governance import egress

    if egress.owner_only_aggregate(vault_root) is not None:
        raise ValueError(
            "AUDIENCE_RESTRICTED: relation review is served to the owner only"
        )


def _refresh_required(ref: str) -> ValueError:
    return ValueError(
        "REVIEW_REFRESH_REQUIRED: the relation candidate is not present in the "
        f"bounded current queue prefix; refresh the queue and inspect {ref} again"
    )


def resolve_candidate(
    vault_root: Path,
    ref: str,
    *,
    source_path: str | None = None,
) -> ResolvedCandidate:
    """Resolve one current candidate from a hint or the bounded legacy prefix."""
    _refuse_other_audiences(vault_root)
    vault_root = Path(vault_root)
    wanted = parse_relation_review_ref(ref)
    if source_path is None:
        queue = build_queue(
            vault_root,
            limit_pages=_DEFAULT_LIMIT_PAGES,
            limit_per_page=_DEFAULT_LIMIT_PER_PAGE,
        )
        if queue.get("status") != "available":
            raise _refresh_required(ref)
        for group in queue.get("groups") or []:
            for item in group.get("items") or []:
                if str(item.get("review_id") or "") != wanted:
                    continue
                candidate = {
                    key: item.get(key)
                    for key in (
                        "from",
                        "to",
                        "relation_type",
                        "method",
                        "evidence",
                    )
                }
                return ResolvedCandidate(
                    review_id=str(item["review_id"]),
                    ref=str(item["ref"]),
                    fingerprint=str(item["fingerprint"]),
                    candidate=candidate,
                    target_ref=str(item["target_ref"]),
                )
        raise _refresh_required(ref)

    rel_path = epistemic_graph_module._page_rel(str(source_path or ""))
    if not epistemic_graph_module.EpistemicGraphIndex(vault_root).available():
        raise _refresh_required(ref)
    page = _page_for(vault_root, rel_path)
    if page is None or not activation_module._eligible(vault_root, page):
        raise _refresh_required(ref)
    for candidate in _page_candidates(
        vault_root, page, limit_per_page=_MAX_GENERATED_PER_METHOD
    ):
        if str(candidate.get("from") or page.rel_path) != rel_path:
            continue
        if _candidate_identity(candidate) != wanted:
            continue
        identity_refs = _hinted_candidate_refs(vault_root, candidate)
        if identity_refs is None:
            raise _refresh_required(ref)
        enriched = _enrich(
            vault_root,
            page,
            candidate,
            exact_refs=identity_refs,
        )
        return ResolvedCandidate(
            review_id=enriched["review_id"],
            ref=enriched["ref"],
            fingerprint=enriched["fingerprint"],
            candidate=candidate,
            target_ref=enriched["target_ref"],
            source_page=page,
            identity_refs=identity_refs,
        )
    raise _refresh_required(ref)


def triage(
    vault_root: Path,
    *,
    ref: str,
    action: str,
    until: str | None = None,
    why: str | None = None,
    expected_fingerprint: str | None = None,
    source_path: str | None = None,
) -> dict[str, Any]:
    """Persist a fingerprint-bound dismiss/snooze/reopen for a relation candidate."""
    _refuse_other_audiences(vault_root)
    resolved = resolve_candidate(vault_root, ref, source_path=source_path)
    if expected_fingerprint and resolved.fingerprint != expected_fingerprint:
        raise ValueError(
            "REVIEW_ITEM_CHANGED: the relation candidate signal changed; refresh "
            f"the queue and inspect {ref} again"
        )
    result = review_state_module.ReviewStateStore(vault_root).apply(
        resolved.review_id,
        resolved.fingerprint,
        action=action,
        until=until,
        why=why,
    )
    result["ref"] = ref
    result["path"] = resolved.candidate.get("from")
    result["target_ref"] = resolved.target_ref
    result["categories"] = [resolved.candidate.get("relation_type")]
    return result


def accept(
    vault_root: Path,
    *,
    ref: str,
    expected_hash: str | None,
    why: str | None,
    expected_fingerprint: str | None = None,
    source_path: str | None = None,
    requested_relation: str | None = None,
    edit_memory: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Governed server-side accept: validate signal + hash, then author the bullet.

    ``edit_memory`` is injected (the registry's ``op_edit_memory``) so the write
    is byte-identical in effect to the Studio single-proposal path.

    ``expected_fingerprint`` is REQUIRED, not merely checked when present: the
    spec requires accept to validate the candidate's fingerprint against the
    live signal, and an optional-by-omission check is skippable by any caller
    that simply doesn't send it.
    """
    _refuse_other_audiences(vault_root)
    if not why or not str(why).strip():
        raise ValueError(
            "INVALID_ACCEPT: accept-relation requires an audit reason (`why`)"
        )
    if not expected_hash:
        raise ValueError(
            "INVALID_ACCEPT: accept-relation requires `expected_hash` from the target page"
        )
    if not expected_fingerprint:
        raise ValueError(
            "INVALID_ACCEPT: accept-relation requires `expected_fingerprint` from the queue read"
        )
    vault_root = Path(vault_root)
    resolved = resolve_candidate(vault_root, ref, source_path=source_path)
    if resolved.fingerprint != expected_fingerprint:
        raise ValueError(
            "REVIEW_ITEM_CHANGED: the relation candidate signal changed; refresh "
            f"the queue and inspect {ref} again"
        )
    candidate = resolved.candidate
    page = resolved.source_page or _page_for(
        vault_root, str(candidate.get("from") or "")
    )
    if page is None:
        raise ValueError(
            "REVIEW_ITEM_CHANGED: the relation candidate's source page no longer "
            f"exists; refresh the queue and inspect {ref} again"
        )
    authored_now = _current_candidate_is_authored(vault_root, page, candidate)
    if authored_now is None:
        raise _refresh_required(ref)
    store = review_state_module.ReviewStateStore(vault_root)
    reason, _enriched = _classify_candidate(
        vault_root,
        page,
        candidate,
        store=store,
        state_payload=store.load(),
        exact_refs=resolved.identity_refs,
        authored=(
            {
                (
                    str(candidate.get("relation_type") or ""),
                    epistemic_graph_module._page_rel(
                        str(candidate.get("to") or "")
                    ),
                )
            }
            if authored_now
            else set()
        ),
    )
    if reason is not None:
        raise ValueError(
            "REVIEW_ITEM_CHANGED: the relation candidate is no longer eligible "
            f"({reason}); refresh the queue and inspect {ref} again"
        )
    require_relation_choice(vault_root, candidate, requested_relation, page=page)
    selected = selected_relation(
        vault_root,
        candidate,
        requested_relation,
        source_page=page,
    )
    bullet = _bullet(selected)
    edit_result = edit_memory(
        vault_root,
        path=str(candidate.get("from") or ""),
        why=why,
        heading="Relations",
        section_position="append",
        new_string=bullet,
        expected_hash=expected_hash,
    )
    return {
        "accepted": True,
        "ref": ref,
        "path": candidate.get("from"),
        "from": selected.get("from"),
        "to": selected.get("to"),
        "relation_type": selected.get("relation_type"),
        "relation_label": relation_registry.display_label(
            relation_registry.load_for_page(vault_root, page.rel_path, page.frontmatter),
            str(selected.get("relation_type") or ""),
        ),
        "authored_label": selected.get("authored_label") or selected.get("relation_type"),
        "method": candidate.get("method"),
        "fingerprint": resolved.fingerprint,
        "bullet": bullet,
        "edit": edit_result,
    }
