"""Counts-only relation-quality census over one published graph snapshot.

The census answers "how good are this vault's edges?" with integers and the
ratios derived from them. It reads one `_open_read_snapshot()` plus the relation
and entity-type registries: it parses no Markdown, runs no model, builds no
index and writes nothing. When the snapshot is unavailable (disabled, warming,
catching up) it says so and never reports a zero.

Egress (N1c). Every number is a reduction over the caller's admitted subgraph.
A node is admitted when its page passes the caller's `keep` predicate (from
`release_walk_filter`) and structural exclusion; an edge is admitted only when
both endpoints and the page that authored it are admitted. Filtering happens
inside the walk, so a withheld page cannot change any count, and the graph
generation (a vault-wide write counter) is reported only to an unrestricted
caller. Counts mode names no path, title, label or vault extension key.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, audit, entity_types, relation_registry
from .kbdir import kb_prefix

CENSUS_VERSION = 1

_AUTHORED_ORIGINS = frozenset({"semantic_relation", "markdown_relation"})
# The indexer mints a `derived_from` edge from every unit or block to its own
# page. That is structure, not an authored relation, and it never connects a
# page to anything else.
_STRUCTURAL_ORIGINS = frozenset({"semantic_unit", "semantic_block"})
_BODY_LINK_ORIGINS = frozenset({"wikilink", "markdown_relation", "semantic_relation"})
_GENERIC = "relates_to"
_LINK = "links_to"
_STATUS_KEYS = (
    "core_specific",
    "core_generic",
    "extension",
    "alias",
    "deprecated",
    "unregistered",
    "scope_violation",
)
_EVIDENTIAL_KINDS = frozenset({"evidence", "result", "metric", "finding", "experiment"})
_QUESTION_KINDS = frozenset({"question", "open_question"})
_EPISTEMIC_FAMILIES = frozenset({"support", "contradiction", "supersession", "duplication"})
_EVIDENCE_FOLDERS = ("Sources/", "Evidence/")
_GOVERNANCE_SEGMENT = "/_Governance/"
_UNMEASURED = "unmeasured"
_DETAILS = frozenset({"counts", "keys"})

Keep = Callable[[str], bool] | None


def census(
    vault_root: Path,
    *,
    keep: Keep = None,
    detail: str = "counts",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Return the relation-quality census of the caller's admitted graph."""
    detail = _validate_detail(detail)
    start, end = _date_bounds(date_from, date_to)
    snapshot = _load(vault_root, keep=keep)
    if snapshot is None:
        return _unavailable(detail)
    return _census_payload(
        vault_root,
        snapshot,
        keep=keep,
        detail=detail,
        start=start,
        end=end,
        date_from=date_from,
        date_to=date_to,
    )


def infer_counts(
    vault_root: Path,
    *,
    keep: Keep,
    page_type: str | None,
    start: dt.date | None,
    end: dt.date | None,
) -> dict[str, Any] | None:
    """`infer`'s legacy census keys, computed from the snapshot, or None.

    The cohort stays infer's: every indexed Knowledge Base page matching the
    page-type scope, date-scoped by origin date exactly as the Markdown count
    did. None means the snapshot is unavailable and the caller keeps its own
    count.
    """
    snapshot = _load(vault_root, keep=keep)
    if snapshot is None:
        return None
    denominators = {"sampled": 0, "included": 0, "undated": 0, "excluded": 0}
    included: set[str] = set()
    prefix = kb_prefix()
    for page in snapshot.pages.values():
        if not page.admitted or not page.path.startswith(prefix):
            continue
        if page_type and page.page_type != page_type:
            continue
        denominators["sampled"] += 1
        origin = _origin_date(page.origin_date)
        if origin is None:
            denominators["undated"] += 1
            if start is None and end is None:
                included.add(page.path)
            continue
        if (start and origin < start) or (end and origin > end):
            denominators["excluded"] += 1
            continue
        denominators["included"] += 1
        included.add(page.path)
    relation_counts = {"core": 0, "extension": 0, "deprecated": 0, "generic": 0, "unregistered": 0}
    authored: set[str] = set()
    body_linked: set[str] = set()
    for edge in snapshot.edges:
        if edge.source_path not in included:
            continue
        if edge.origin in _BODY_LINK_ORIGINS and edge.touches_other:
            body_linked.add(edge.source_path)
        if edge.origin not in _AUTHORED_ORIGINS:
            continue
        authored.add(edge.source_path)
        status = edge.status
        if status == "deprecated":
            relation_counts["deprecated"] += 1
        elif status == "unregistered":
            relation_counts["unregistered"] += 1
        elif edge.relation == _GENERIC:
            relation_counts["generic"] += 1
        elif status == "core":
            relation_counts["core"] += 1
        elif status in {"extension", "alias"}:
            relation_counts["extension"] += 1
    return {
        "relation_counts": relation_counts,
        "page_counts": {
            "zero_authored_relation_rows": sum(path not in authored for path in included),
            "zero_body_connections": sum(path not in body_linked for path in included),
        },
        "denominators": denominators,
    }


def service_census(detail: str) -> dict[str, Any] | None:
    """Ask the running managed service for its census, or return None.

    The CLI prefers the service so it reads the live published snapshot. This
    is a read over REST, never out-of-process index work. Any failure (no
    managed install, no REST key, no answer, an older service) returns None
    and the caller opens the sidecar read-only instead.
    """
    api_key = os.environ.get("EXOMEM_REST_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from . import install_info

        target = install_info.report().get("managed_service_target")
        if not target:
            return None
        import httpx

        response = httpx.post(
            f"{str(target).rstrip('/')}/api/schema_memory",
            json={"subject": "relations", "operation": "census", "detail": detail},
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=30.0,
            follow_redirects=False,
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except Exception:  # noqa: BLE001 - the local snapshot is the fallback
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    if not isinstance(data, dict) or "census_version" not in data:
        return None
    return data


def summary_line(result: Mapping[str, Any]) -> str:
    """One human line for doctor and the CLI."""
    if not result.get("available"):
        return "Relation census unavailable: the graph snapshot is not current."
    metrics = result["metrics"]
    eligible = result["cohort"]["eligible_pages"]
    return (
        f"Relation census: generic share {_percent(metrics['generic_share'])}, "
        f"specific coverage {_percent(metrics['specific_coverage']['ratio'])}, "
        f"{metrics['disconnected']['pages']} of {eligible} eligible pages disconnected, "
        f"{metrics['by_status']['unregistered']} unregistered authored edges."
    )


# ---------------------------------------------------------------------------
# Snapshot loading and admission
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Page:
    path: str
    page_type: str | None
    status: str | None
    tags: tuple[str, ...]
    origin_date: str | None
    entity_type: str | None
    admitted: bool


@dataclass(slots=True)
class _Edge:
    src_page: str
    dst_page: str
    dst_kind: str | None  # unit kind, "file", or None for a placeholder
    src_known: bool
    dst_known: bool
    relation: str | None
    raw_relation: str
    status: str
    origin: str
    source_path: str
    source_anchor: str | None
    source_kind: str | None
    target_kind: str | None
    line: Any

    @property
    def touches_other(self) -> bool:
        return self.src_page != self.source_path or self.dst_page != self.source_path


@dataclass(slots=True)
class _Snapshot:
    generation: int | None
    pages: dict[str, _Page]
    units_by_page: dict[str, set[str]]
    edges: list[_Edge] = field(default_factory=list)


def _load(vault_root: Path, *, keep: Keep) -> _Snapshot | None:
    from .epistemic_graph import EpistemicGraphIndex, _placeholder_path_allowed

    connection = EpistemicGraphIndex(vault_root)._open_read_snapshot()
    if connection is None:
        return None
    verdicts: dict[str, bool] = {}

    def path_ok(path: str) -> bool:
        cached = verdicts.get(path)
        if cached is None:
            cached = _GOVERNANCE_SEGMENT not in f"/{path}" and (keep is None or bool(keep(path)))
            verdicts[path] = cached
        return cached

    try:
        generation_row = connection.execute(
            "SELECT value FROM graph_meta WHERE key = 'generation'"
        ).fetchone()
        pages: dict[str, _Page] = {}
        file_keys: dict[str, str] = {}
        for key, path, page_type, status, tags_json, origin_date, tier, entity, scope in (
            connection.execute(
                "SELECT node_key, path, page_type, lifecycle_status, tags_json, origin_date, "
                "access_tier, json_extract(metadata, '$.entity_type'), "
                "json_extract(metadata, '$.scope') FROM graph_nodes WHERE kind = 'file'"
            )
        ):
            pages[path] = _Page(
                path=path,
                page_type=page_type,
                status=status,
                tags=_tags(tags_json),
                origin_date=origin_date,
                entity_type=(entity or scope) if page_type == "entity" else None,
                admitted=tier != "excluded" and path_ok(path),
            )
            file_keys[key] = path
        units: dict[str, tuple[str, str]] = {}
        units_by_page: dict[str, set[str]] = {}
        for key, kind, path in connection.execute(
            "SELECT node_key, kind, path FROM graph_nodes WHERE kind != 'file'"
        ):
            units[key] = (kind, path)
            units_by_page.setdefault(path, set()).add(kind)

        def endpoint(key: str) -> tuple[str | None, str | None, bool, bool]:
            """(page path, kind, known node, admitted) for one edge endpoint."""
            path = file_keys.get(key)
            if path is not None:
                return path, "file", True, pages[path].admitted
            unit = units.get(key)
            if unit is not None:
                kind, page_path = unit
                page = pages.get(page_path)
                allowed = page.admitted if page is not None else path_ok(page_path)
                return page_path, kind, True, allowed
            if key.startswith("file:"):
                placeholder = key[len("file:") :]
                allowed = _placeholder_path_allowed(vault_root, placeholder) and path_ok(
                    placeholder
                )
                return placeholder, None, False, allowed
            return None, None, False, False

        snapshot = _Snapshot(
            generation=_int_or_none(generation_row[0] if generation_row else None),
            pages=pages,
            units_by_page=units_by_page,
        )
        for (
            src_key,
            dst_key,
            relation,
            raw_relation,
            status,
            origin,
            source_path,
            source_anchor,
            source_kind,
            target_kind,
            line,
        ) in connection.execute(
            "SELECT src_key, dst_key, relation_type, raw_relation, registry_status, origin, "
            "source_path, source_anchor, resolver_source_kind, resolver_target_kind, "
            "CASE WHEN registry_status = 'unregistered' "
            "THEN json_extract(metadata, '$.line') END FROM graph_edges"
        ):
            if origin in _STRUCTURAL_ORIGINS:
                continue
            author = pages.get(source_path)
            if not (author.admitted if author is not None else path_ok(source_path)):
                continue
            src_page, _src_kind, src_known, src_ok = endpoint(src_key)
            dst_page, dst_kind, dst_known, dst_ok = endpoint(dst_key)
            if not (src_ok and dst_ok) or src_page is None or dst_page is None:
                continue
            snapshot.edges.append(
                _Edge(
                    src_page=src_page,
                    dst_page=dst_page,
                    dst_kind=dst_kind,
                    src_known=src_known,
                    dst_known=dst_known,
                    relation=relation,
                    raw_relation=raw_relation,
                    status=status,
                    origin=origin,
                    source_path=source_path,
                    source_anchor=source_anchor,
                    source_kind=source_kind,
                    target_kind=target_kind,
                    line=line,
                )
            )
    finally:
        connection.close()
    return snapshot


# ---------------------------------------------------------------------------
# The census reduction
# ---------------------------------------------------------------------------


def _census_payload(
    vault_root: Path,
    snapshot: _Snapshot,
    *,
    keep: Keep,
    detail: str,
    start: dt.date | None,
    end: dt.date | None,
    date_from: str | None,
    date_to: str | None,
) -> dict[str, Any]:
    from .memory_schema import _normalize_indexed_relation

    registry = relation_registry.load_registry(vault_root)
    types = entity_types.load_entity_types(vault_root)
    prefix = kb_prefix()

    eligible: set[str] = set()
    undated = outside = 0
    for page in snapshot.pages.values():
        if not page.admitted or not page.path.startswith(prefix):
            continue
        if not audit.relation_debt_eligible(
            vault_root,
            page_type=page.page_type,
            rel_path=page.path,
            status=page.status,
            tags=page.tags,
        ):
            continue
        if start is not None or end is not None:
            origin = _origin_date(page.origin_date)
            if origin is None:
                undated += 1
                continue
            if (start and origin < start) or (end and origin > end):
                outside += 1
                continue
        eligible.add(page.path)

    def entity_family(path: str) -> str | None:
        page = snapshot.pages.get(path)
        if page is None or not page.admitted or page.page_type != "entity":
            return None
        if not page.entity_type or types.resolve(page.entity_type) is None:
            return None
        return types.family_of(page.entity_type)

    entity_pages = {path for path in eligible if entity_family(path) is not None}

    by_status = dict.fromkeys(_STATUS_KEYS, 0)
    authored_edges = 0
    predicate_edges: Counter[str] = Counter()
    aliases_in_use: set[str] = set()
    unregistered_pages: dict[str, set[str]] = {}
    connected: set[str] = set()
    inbound: set[str] = set()
    typed_pages: set[str] = set()
    specific_pages: set[str] = set()
    pages_with_sources: set[str] = set()
    population: list[_Edge] = []
    supersession: list[_Edge] = []
    entity_edges = dict.fromkeys(_STATUS_KEYS, 0)
    entity_specific: set[str] = set()
    entity_generic_only: dict[str, bool] = {}
    affiliated: set[str] = set()

    for edge in snapshot.edges:
        source = edge.source_path
        for page in (edge.src_page, edge.dst_page):
            if page != source:
                inbound.add(page)
        if (
            edge.origin == "frontmatter"
            and edge.relation == "derived_from"
            and edge.source_anchor == "sources"
        ):
            pages_with_sources.add(source)
        typed = (
            edge.relation is not None
            and edge.status != "unregistered"
            and edge.relation != _LINK
            and edge.origin != "wikilink"
        )
        if typed and edge.relation == "supersedes":
            supersession.append(edge)
        if source not in eligible:
            continue
        if edge.touches_other:
            connected.add(source)
            if typed:
                typed_pages.add(source)
                if edge.relation != _GENERIC:
                    specific_pages.add(source)
        if typed:
            population.append(edge)
            definition = registry.definition(edge.relation or "")
            if definition is not None and definition.family == "affiliation":
                affiliated.add(source)
        if edge.origin in _AUTHORED_ORIGINS:
            authored_edges += 1
            bucket = _bucket(edge)
            by_status[bucket] += 1
            if bucket == "unregistered":
                label = _normalize_indexed_relation(edge.raw_relation, edge.line)
                unregistered_pages.setdefault(label, set()).add(source)
            else:
                predicate_edges[edge.relation or ""] += 1
                if bucket == "alias":
                    aliases_in_use.add(relation_registry.normalize_relation(edge.raw_relation))
        if source in entity_pages and edge.origin != "wikilink" and edge.touches_other:
            other = edge.dst_page if edge.src_page == source else edge.src_page
            if other != source and entity_family(other) is not None:
                bucket = _bucket(edge)
                entity_edges[bucket] += 1
                generic = edge.relation == _GENERIC and bucket == "core_generic"
                entity_generic_only[source] = entity_generic_only.get(source, True) and generic
                if typed and edge.relation != _GENERIC:
                    entity_specific.add(source)

    eligible_count = len(eligible)
    registered_edges = authored_edges - by_status["unregistered"]
    top3 = sum(count for _key, count in _ranked(predicate_edges)[:3])
    core_used = sorted(key for key in predicate_edges if key in registry.core)
    extension_used = sorted(key for key in predicate_edges if key in registry.extensions)
    disconnected = eligible - connected
    persons = {path for path in entity_pages if entity_family(path) == "person"}

    metrics: dict[str, Any] = {
        "authored_edges": authored_edges,
        "by_status": by_status,
        "generic_share": _ratio(by_status["core_generic"], registered_edges),
        "typed_coverage": {
            "pages": len(typed_pages),
            "ratio": _ratio(len(typed_pages), eligible_count),
        },
        "specific_coverage": {
            "pages": len(specific_pages),
            "ratio": _ratio(len(specific_pages), eligible_count),
        },
        "predicate_utilisation": {
            "core_keys_used": len(core_used),
            "core_keys": len(registry.core),
            "extension_keys_used": len(extension_used),
            "extension_keys": len(registry.extensions),
            "top3_edges": top3,
            "registered_edges": registered_edges,
            "top3_share": _ratio(top3, registered_edges),
        },
        "extension_use": {
            # Standing (provisional, common, dormant) is derived by the
            # vocabulary loop; until it exists the census does not guess.
            "standing": _UNMEASURED,
            "registered": len(registry.extensions),
            "deprecated": sum(
                item.status == "deprecated" for item in registry.extensions.values()
            ),
            "used": len(extension_used),
            "edges": sum(predicate_edges[key] for key in extension_used),
            "aliases_in_use": len(aliases_in_use),
        },
        "disconnected": {
            "pages": len(disconnected),
            "isolated": sum(path not in inbound for path in disconnected),
        },
        "entity_coverage": {
            "entity_pages": len(entity_pages),
            "with_specific_entity_edge": len(entity_specific),
            "entity_edges": entity_edges,
            "only_generic_entity_edges": sum(entity_generic_only.values()),
            "persons": len(persons),
            "persons_without_affiliation": len(persons - affiliated),
            "without_inbound": sum(path not in inbound for path in entity_pages),
        },
        "unregistered_pressure": {
            "distinct_labels": len(unregistered_pages),
            "edges": by_status["unregistered"],
            "labels_at_three_or_more_pages": sum(
                len(pages) >= 3 for pages in unregistered_pages.values()
            ),
        },
        "inverse_duplicates": _inverse_duplicates(population, registry),
        # Near-duplicate groups need the vocabulary detector (S2-S7).
        "near_duplicate_groups": _UNMEASURED,
        "false_precision_judged": _UNMEASURED,
    }
    checks = _structural_checks(
        snapshot,
        population,
        supersession,
        registry=registry,
        pages_with_sources=pages_with_sources,
    )
    payload: dict[str, Any] = {
        "census_version": CENSUS_VERSION,
        "exomem_version": __version__,
        "available": True,
        "detail": detail,
        # A write counter over the whole vault: withheld writes move it too.
        "graph_generation": snapshot.generation if keep is None else None,
        "registry": {
            "core_version": registry.core_version,
            "extension_hash": registry.extension_hash,
        },
        "cohort": {
            "eligible_pages": eligible_count,
            "entity_pages": len(entity_pages),
            "date_from": date_from,
            "date_to": date_to,
            "undated": undated,
            "outside_scope": outside,
        },
        "metrics": metrics,
        "checks": checks,
        "sample": None,
    }
    if detail == "keys":
        payload["keys"] = {
            "predicates": {key: predicate_edges[key] for key in sorted(predicate_edges)},
            "extensions_unused": sorted(set(registry.extensions) - set(extension_used)),
        }
    return payload


def _structural_checks(
    snapshot: _Snapshot,
    population: list[_Edge],
    supersession: list[_Edge],
    *,
    registry: relation_registry.RelationRegistry,
    pages_with_sources: set[str],
) -> dict[str, dict[str, Any]]:
    """Seven structure-only rules, each over the edges it can apply to."""
    counts = {
        name: {"applicable": 0, "violations": 0}
        for name in (
            "signature_mismatch",
            "supersedes_backwards",
            "evidence_target_not_evidential",
            "answers_without_question",
            "same_page_epistemic",
            "directed_both_ways",
            "contradicts_within_chain",
        )
    }

    def record(name: str, violated: bool) -> None:
        counts[name]["applicable"] += 1
        counts[name]["violations"] += int(violated)

    chain = _UnionFind()
    for edge in supersession:
        if edge.src_known and edge.dst_known and edge.src_page != edge.dst_page:
            chain.union(edge.src_page, edge.dst_page)
    directed = {
        (edge.src_page, edge.dst_page, edge.relation)
        for edge in population
        if edge.src_known and edge.dst_known
    }

    for edge in population:
        definition = registry.definition(edge.relation or "")
        if edge.status == "scope_violation" or (
            definition is not None and (definition.source_kinds or definition.target_kinds)
        ):
            outside = definition is not None and (
                (
                    definition.source_kinds
                    and edge.source_kind is not None
                    and edge.source_kind not in definition.source_kinds
                )
                or (
                    definition.target_kinds
                    and edge.target_kind is not None
                    and edge.target_kind not in definition.target_kinds
                )
            )
            record("signature_mismatch", edge.status == "scope_violation" or bool(outside))
        if not (edge.src_known and edge.dst_known):
            continue  # a placeholder endpoint cannot satisfy or break a rule
        same_page = edge.src_page == edge.dst_page
        if edge.relation == "supersedes" and not same_page:
            newer = _origin_date(_page_origin(snapshot, edge.src_page))
            older = _origin_date(_page_origin(snapshot, edge.dst_page))
            if newer is not None and older is not None:
                record("supersedes_backwards", newer < older)
        if edge.relation == "evidenced_by":
            if edge.dst_kind == "file":
                relative = edge.dst_page.removeprefix(kb_prefix())
                evidential = relative.startswith(_EVIDENCE_FOLDERS) or (
                    edge.dst_page in pages_with_sources
                )
            else:
                evidential = edge.dst_kind in _EVIDENTIAL_KINDS
            record("evidence_target_not_evidential", not evidential)
        if edge.relation == "answers":
            if edge.dst_kind == "file":
                answered = bool(snapshot.units_by_page.get(edge.dst_page, set()) & _QUESTION_KINDS)
            else:
                answered = edge.dst_kind in _QUESTION_KINDS
            record("answers_without_question", not answered)
        if definition is not None and definition.family in _EPISTEMIC_FAMILIES:
            record("same_page_epistemic", same_page)
        if definition is not None and definition.direction == "directed" and not same_page:
            record(
                "directed_both_ways",
                (edge.dst_page, edge.src_page, edge.relation) in directed,
            )
        if edge.relation == "contradicts" and not same_page:
            record(
                "contradicts_within_chain",
                chain.find(edge.src_page) == chain.find(edge.dst_page),
            )
    counts["directed_both_ways"]["inspection_only"] = True
    return counts


def _inverse_duplicates(
    population: Iterable[_Edge], registry: relation_registry.RelationRegistry
) -> dict[str, int]:
    """Pairs where a->b is typed P and b->a is typed P's registered inverse."""
    edges = [
        edge
        for edge in population
        if edge.src_known and edge.dst_known and edge.src_page != edge.dst_page
    ]
    present = {(edge.src_page, edge.dst_page, edge.relation) for edge in edges}
    applicable = 0
    pairs: set[tuple[tuple[str, str, str], tuple[str, str, str]]] = set()
    for edge in edges:
        definition = registry.definition(edge.relation or "")
        if definition is None or not definition.inverse:
            continue
        applicable += 1
        forward = (edge.src_page, edge.dst_page, edge.relation or "")
        reverse = (edge.dst_page, edge.src_page, definition.inverse)
        if reverse in present:
            pairs.add((min(forward, reverse), max(forward, reverse)))
    return {"applicable": applicable, "pairs": len(pairs)}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        parent = self._parent.setdefault(item, item)
        while parent != self._parent[parent]:
            self._parent[parent] = self._parent[self._parent[parent]]
            parent = self._parent[parent]
        self._parent[item] = parent
        return parent

    def union(self, left: str, right: str) -> None:
        root_left, root_right = self.find(left), self.find(right)
        if root_left != root_right:
            self._parent[max(root_left, root_right)] = min(root_left, root_right)


def _bucket(edge: _Edge) -> str:
    status = edge.status
    if status in {"unregistered", "scope_violation", "deprecated"}:
        return status
    if edge.relation == _GENERIC:
        return "core_generic"
    if status == "core":
        return "core_specific"
    if status == "alias":
        return "alias"
    return "extension"


def _ranked(counter: Counter[str]) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _page_origin(snapshot: _Snapshot, path: str) -> str | None:
    page = snapshot.pages.get(path)
    return page.origin_date if page is not None else None


def _origin_date(value: str | None) -> dt.date | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _tags(raw: str | None) -> tuple[str, ...]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ()
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate_detail(detail: str | None) -> str:
    value = (detail or "counts").strip().lower()
    if value not in _DETAILS:
        raise ValueError("INVALID_RELATION_ARGUMENT: detail must be counts or keys")
    return value


def _date_bounds(
    date_from: str | None, date_to: str | None
) -> tuple[dt.date | None, dt.date | None]:
    try:
        start = dt.date.fromisoformat(date_from) if date_from is not None else None
        end = dt.date.fromisoformat(date_to) if date_to is not None else None
    except ValueError as exc:
        raise ValueError("INVALID_DATE_SCOPE: date bounds must be ISO dates") from exc
    if start is not None and end is not None and start > end:
        raise ValueError("INVALID_DATE_SCOPE: date_from must not be after date_to")
    return start, end


def _unavailable(detail: str) -> dict[str, Any]:
    return {
        "census_version": CENSUS_VERSION,
        "exomem_version": __version__,
        "available": False,
        "reason": "graph_unavailable",
        "detail": detail,
    }
