"""Counts-only relation-quality census over one published graph snapshot.

The census answers "how good are this vault's edges?" with integers and the
ratios derived from them. It reads one `_open_read_snapshot()` plus the relation
and entity-type registries. The census itself parses no Markdown, runs no
model, builds no index and writes nothing; under a governed policy the owner's
release filter reads each page's bytes to decide its release. When the snapshot
is unavailable (disabled, warming, catching up) it says so and never reports a
zero.

It streams: edge rows are reduced as they are read, and the two checks that
compare an edge with its reverse run as SQL aggregates, so memory follows the
page count, not the edge count.

Egress (D15). Links resolve against the whole vault, so a withheld page can
change how a visible page's bare link resolves: a shared stem makes it
ambiguous, a matching stem or title wins it. The snapshot keeps only the
result, so no filter applied to it can undo that. The census is therefore
served whole: under an empty governance policy to every caller, and under a
governed policy to the owner only. Every other audience receives
`audience_restricted` before anything is read.

Within the view it serves, a node is admitted when its page passes the release
filter and structural exclusion, and an edge only when both endpoints are
admitted indexed nodes and the page that authored it is admitted. A placeholder
for a missing target is never admitted: rows whose target is outside the view
count as `unresolved_target_edges`. Counts mode names no path, title, label or
vault extension key.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import heapq
import json
import math
import os
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from . import __version__, audit, entity_types, relation_registry
from .kbdir import kb_prefix

CENSUS_VERSION = 1
AUDIENCE_RESTRICTED = "audience_restricted"
POLICY_BLOCKED = "policy_blocked"

_AUTHORED_ORIGINS = frozenset({"semantic_relation", "markdown_relation"})
# The indexer mints a `derived_from` edge from every unit or block to its own
# page. That is structure, not an authored relation, and it never connects a
# page to anything else.
_STRUCTURAL_ORIGINS = ("semantic_unit", "semantic_block")
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
_QUESTION_KINDS = ("question", "open_question")
_EPISTEMIC_FAMILIES = frozenset({"support", "contradiction", "supersession", "duplication"})
_EVIDENCE_FOLDERS = ("Sources/", "Evidence/")
_GOVERNANCE_SEGMENT = "/_Governance/"
_UNMEASURED = "unmeasured"
_DETAILS = frozenset({"counts", "keys"})
#: Verdicts the judging agent records per sampled edge; only `precise` is not
#: false precision.
VERDICTS = ("precise", "too_specific", "wrong_direction", "wrong_predicate", "should_be_generic")
DEFAULT_SAMPLE_SIZE = 40
_WILSON_Z = 1.959964

Keep = Callable[[str], bool] | None
#: Default for `keep`: decide the view from the bound principal and policy.
#: Every product surface uses it. An explicit predicate (or None, the whole
#: graph) is a trusted in-process caller's own admission rule: it narrows what
#: is counted but cannot undo whole-vault link resolution, so it must never
#: stand in for a restricted audience.
CALLER: Any = object()


class ServiceKeyRefused(Exception):
    """The managed service answered, and refused the REST key."""

    def __init__(self, status: int) -> None:
        super().__init__(f"the managed service refused the REST key (HTTP {status})")
        self.status = status


def census(
    vault_root: Path,
    *,
    keep: Any = CALLER,
    detail: str = "counts",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Return the relation-quality census of the caller's view of the graph."""
    detail = _validate_detail(detail)
    start, end = _date_bounds(date_from, date_to)
    view = _resolve_view(vault_root, keep)
    if isinstance(view, str):
        return _refused(view, detail)
    with _reader(vault_root, view.keep) as reader:
        if reader is None:
            return _unavailable(detail)
        return _census_payload(
            vault_root,
            reader,
            whole=view.whole,
            detail=detail,
            start=start,
            end=end,
            date_from=date_from,
            date_to=date_to,
        )


def refusal(vault_root: Path) -> dict[str, Any] | None:
    """`infer`'s census field for a caller the census refuses, else None."""
    view = _resolve_view(vault_root, CALLER)
    return {"available": False, "reason": view} if isinstance(view, str) else None


def infer_counts(
    vault_root: Path,
    *,
    keep: Any = CALLER,
    page_type: str | None,
    start: dt.date | None,
    end: dt.date | None,
) -> dict[str, Any] | None:
    """`infer`'s census keys, computed from the snapshot; None if unavailable.

    The cohort stays infer's: every indexed Knowledge Base page matching the
    page-type scope, date-scoped by origin date exactly as the Markdown count
    did. An authored row counts whether or not its target resolves to a page
    in view, so `relation_counts` and `zero_authored_relation_rows` keep the
    Markdown values. `zero_body_connections` counts pages with no wikilink or
    relation row that resolves to another page in view.
    """
    view = _resolve_view(vault_root, keep)
    if isinstance(view, str):
        return {"available": False, "reason": view}
    with _reader(vault_root, view.keep) as reader:
        if reader is None:
            return None
        denominators = {"sampled": 0, "included": 0, "undated": 0, "excluded": 0}
        included: set[str] = set()
        prefix = kb_prefix()
        for page in reader.pages.values():
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
        relation_counts = {
            "core": 0,
            "extension": 0,
            "deprecated": 0,
            "generic": 0,
            "unregistered": 0,
        }
        authored: set[str] = set()
        body_linked: set[str] = set()
        for row in reader.rows():
            if row.source_path not in included:
                continue
            if row.in_view and row.origin in _BODY_LINK_ORIGINS and row.touches_other:
                body_linked.add(row.source_path)
            if row.origin not in _AUTHORED_ORIGINS:
                continue
            authored.add(row.source_path)
            status = row.status
            if status == "deprecated":
                relation_counts["deprecated"] += 1
            elif status == "unregistered":
                relation_counts["unregistered"] += 1
            elif row.relation == _GENERIC:
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
    is a read over REST, never out-of-process index work. A refused key raises
    `ServiceKeyRefused` so the caller can say so; any other failure (no managed
    install, no REST key, no answer, an older service) returns None and the
    caller opens the sidecar read-only instead.
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
        if response.status_code in (401, 403):
            raise ServiceKeyRefused(response.status_code)
        if response.status_code != 200:
            return None
        payload = response.json()
    except ServiceKeyRefused:
        raise
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
        if result.get("reason") == AUDIENCE_RESTRICTED:
            return "Relation census withheld: it is served to the vault owner only."
        if result.get("reason") == POLICY_BLOCKED:
            return "Relation census unavailable: the governance policy does not compile."
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
# Views, snapshot reading and admission
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _View:
    keep: Keep
    whole: bool  # the caller may see vault-wide facts such as the generation


def _resolve_view(vault_root: Path, keep: Any) -> _View | str:
    """The view to count, or the reason the bound caller gets no census."""
    if keep is not CALLER:
        return _View(keep=keep, whole=keep is None)
    from .governance import egress
    from .governance import policy as policy_module
    from .governance.principal import OWNER_AUDIENCE, effective_principal

    root = Path(vault_root)
    current = policy_module.load(root)
    if current.empty:
        return _View(keep=egress.release_walk_filter(root), whole=True)
    who = effective_principal()
    if not (who.resolved and who.audience_id == OWNER_AUDIENCE):
        return AUDIENCE_RESTRICTED
    if current.blocked:
        return POLICY_BLOCKED
    return _View(keep=egress.release_walk_filter(root, principal=who), whole=True)


@dataclass(slots=True)
class _Page:
    path: str
    page_type: str | None
    status: str | None
    tags: tuple[str, ...]
    origin_date: str | None
    entity_type: str | None
    admitted: bool


class _Row(NamedTuple):
    edge_key: str
    relation: str | None
    raw_relation: str
    status: str
    origin: str
    source_path: str
    source_anchor: str | None
    source_kind: str | None
    target_kind: str | None
    line: Any
    in_view: bool
    src_page: str | None
    dst_page: str | None
    dst_kind: str | None  # "file" or the unit kind
    dst_anchor: str | None

    @property
    def touches_other(self) -> bool:
        return self.src_page != self.source_path or self.dst_page != self.source_path


_ROWS_SQL = (
    "SELECT edge_key, src_key, dst_key, relation_type, raw_relation, registry_status, "
    "origin, source_path, source_anchor, resolver_source_kind, resolver_target_kind, "
    "CASE WHEN registry_status = 'unregistered' "
    "THEN json_extract(metadata, '$.line') END "
    "FROM graph_edges WHERE origin NOT IN (?, ?)"
)

# Typed edges between two admitted pages, authored on an eligible page: the
# population the edge-and-its-reverse checks run over, grouped in SQL.
_POPULATION_SQL = (
    "FROM graph_edges AS e {join}"
    "JOIN graph_nodes AS s ON s.node_key = e.src_key "
    "JOIN graph_nodes AS d ON d.node_key = e.dst_key "
    "WHERE e.origin NOT IN ('semantic_unit', 'semantic_block', 'wikilink') "
    "AND e.registry_status != 'unregistered' AND e.relation_type IS NOT NULL "
    "AND e.relation_type != 'links_to' AND s.path != d.path "
    "AND exomem_census_eligible(e.source_path) "
    "AND exomem_census_admitted(s.path) AND exomem_census_admitted(d.path)"
)


class _Reader:
    """One open snapshot: pages in memory, edge rows streamed."""

    def __init__(self, connection: sqlite3.Connection, keep: Keep) -> None:
        self.connection = connection
        self._keep = keep
        self._verdicts: dict[str, bool] = {}
        row = connection.execute("SELECT value FROM graph_meta WHERE key = 'generation'").fetchone()
        self.generation = _int_or_none(row[0] if row else None)
        self.pages: dict[str, _Page] = {}
        for path, page_type, status, tags_json, origin_date, tier, entity, scope in (
            connection.execute(
                "SELECT path, page_type, lifecycle_status, tags_json, origin_date, "
                "access_tier, json_extract(metadata, '$.entity_type'), "
                "json_extract(metadata, '$.scope') FROM graph_nodes WHERE kind = 'file'"
            )
        ):
            self.pages[path] = _Page(
                path=path,
                page_type=page_type,
                status=status,
                tags=_tags(tags_json),
                origin_date=origin_date,
                entity_type=(entity or scope) if page_type == "entity" else None,
                admitted=tier != "excluded" and self._path_ok(path),
            )
        # Unit and block nodes, keyed for endpoint lookups: (page, kind, anchor).
        # File nodes need no entry, since their key is `file:` plus the path.
        self._units: dict[str, tuple[str, str, str | None]] = {}
        self.question_pages: set[str] = set()
        for key, kind, path, anchor in connection.execute(
            "SELECT node_key, kind, path, anchor FROM graph_nodes WHERE kind != 'file'"
        ):
            self._units[key] = (path, kind, anchor)
            if kind in _QUESTION_KINDS:
                self.question_pages.add(path)

    def _path_ok(self, path: str) -> bool:
        cached = self._verdicts.get(path)
        if cached is None:
            cached = _GOVERNANCE_SEGMENT not in f"/{path}" and (
                self._keep is None or bool(self._keep(path))
            )
            self._verdicts[path] = cached
        return cached

    def admitted(self, path: str | None) -> bool:
        page = self.pages.get(path) if path is not None else None
        return page is not None and page.admitted

    def author_ok(self, source_path: str) -> bool:
        author = self.pages.get(source_path)
        return author.admitted if author is not None else self._path_ok(source_path)

    def _endpoint(self, key: str) -> tuple[str, str, str | None] | None:
        """(page, kind, anchor) of an admitted indexed node, else None."""
        if key.startswith("file:"):
            page = self.pages.get(key[5:])
            return (page.path, "file", None) if page is not None and page.admitted else None
        unit = self._units.get(key)
        if unit is not None and self.admitted(unit[0]):
            return unit
        return None

    def rows(self) -> Iterator[_Row]:
        """Every non-structural edge authored on an admitted page, streamed."""
        for (
            edge_key,
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
        ) in self.connection.execute(_ROWS_SQL, _STRUCTURAL_ORIGINS):
            if not self.author_ok(source_path):
                continue
            source_end = self._endpoint(src_key)
            target_end = self._endpoint(dst_key) if source_end is not None else None
            in_view = target_end is not None
            src_path = source_end[0] if in_view else None
            dst_path, dst_kind, dst_anchor = target_end if in_view else (None, None, None)
            yield _Row(
                edge_key,
                relation,
                raw_relation,
                status,
                origin,
                source_path,
                source_anchor,
                source_kind,
                target_kind,
                line,
                in_view,
                src_path,
                dst_path,
                dst_kind,
                dst_anchor,
            )

    def pair_checks(
        self, eligible: set[str], registry: relation_registry.RelationRegistry
    ) -> tuple[dict[str, int], dict[str, int]]:
        """`directed_both_ways` and `inverse_duplicates`, aggregated in SQL."""
        self.connection.create_function(
            "exomem_census_eligible", 1, lambda path: path in eligible, deterministic=True
        )
        self.connection.create_function(
            "exomem_census_admitted", 1, self.admitted, deterministic=True
        )
        definitions = {**registry.core, **registry.extensions}
        directed = sorted(
            key for key, item in definitions.items() if item.direction == "directed"
        )
        both = self.connection.execute(
            "SELECT COALESCE(SUM(n), 0), "
            "COALESCE(SUM(CASE WHEN forward > 0 AND backward > 0 THEN n ELSE 0 END), 0) "
            "FROM (SELECT COUNT(*) AS n, SUM(s.path < d.path) AS forward, "
            "SUM(s.path > d.path) AS backward "
            f"{_POPULATION_SQL.format(join='')} "
            "AND e.relation_type IN (SELECT value FROM json_each(?)) "
            "GROUP BY min(s.path, d.path), max(s.path, d.path), e.relation_type)",
            (json.dumps(directed),),
        ).fetchone()
        # Each inverse-bearing relation maps to (class, representative?, counted?):
        # an edge typed by the non-representative member is the same fact as the
        # representative in reverse, so both land in one oriented group.
        inverses: dict[str, list[Any]] = {}
        for key, item in sorted(definitions.items()):
            if not item.inverse:
                continue
            group = min(key, item.inverse)
            inverses[key] = [group, int(key == group), 1, int(key == item.inverse)]
            inverses.setdefault(
                item.inverse, [group, int(item.inverse == group), 0, int(key == item.inverse)]
            )
        pairs = self.connection.execute(
            "WITH inv AS (SELECT key AS r, json_extract(value, '$[0]') AS grp, "
            "json_extract(value, '$[1]') AS rep, json_extract(value, '$[2]') AS counted, "
            "json_extract(value, '$[3]') AS self_inverse FROM json_each(?)), "
            "oriented AS (SELECT inv.grp AS grp, inv.counted AS counted, "
            "CASE WHEN inv.self_inverse THEN s.path < d.path ELSE inv.rep END AS rep, "
            "s.path AS a, d.path AS b "
            f"{_POPULATION_SQL.format(join='JOIN inv ON inv.r = e.relation_type ')}) "
            "SELECT COALESCE(SUM(n), 0), "
            "COALESCE(SUM(CASE WHEN reps > 0 AND others > 0 THEN 1 ELSE 0 END), 0) "
            "FROM (SELECT SUM(counted) AS n, SUM(rep) AS reps, SUM(1 - rep) AS others "
            "FROM oriented GROUP BY CASE WHEN rep THEN a ELSE b END, "
            "CASE WHEN rep THEN b ELSE a END, grp)",
            (json.dumps(inverses),),
        ).fetchone()
        return (
            {"applicable": int(both[0]), "violations": int(both[1])},
            {"applicable": int(pairs[0]), "pairs": int(pairs[1])},
        )

    def sources_pages(self) -> set[str]:
        """Pages with an admitted frontmatter `sources:` edge."""
        found: set[str] = set()
        for source_path, dst_path in self.connection.execute(
            "SELECT e.source_path, d.path FROM graph_edges AS e "
            "JOIN graph_nodes AS d ON d.node_key = e.dst_key "
            "WHERE e.relation_type = 'derived_from' AND e.origin = 'frontmatter' "
            "AND e.source_anchor = 'sources'"
        ):
            if self.admitted(source_path) and self.admitted(dst_path):
                found.add(source_path)
        return found

    def supersession_chains(self) -> _UnionFind:
        chain = _UnionFind()
        for src_path, dst_path, source_path in self.connection.execute(
            "SELECT s.path, d.path, e.source_path FROM graph_edges AS e "
            "JOIN graph_nodes AS s ON s.node_key = e.src_key "
            "JOIN graph_nodes AS d ON d.node_key = e.dst_key "
            "WHERE e.relation_type = 'supersedes' AND e.registry_status != 'unregistered' "
            "AND e.origin != 'wikilink'"
        ):
            if (
                src_path != dst_path
                and self.admitted(src_path)
                and self.admitted(dst_path)
                and self.author_ok(source_path)
            ):
                chain.union(src_path, dst_path)
        return chain


@contextmanager
def _reader(vault_root: Path, keep: Keep) -> Iterator[_Reader | None]:
    from .epistemic_graph import EpistemicGraphIndex

    connection = EpistemicGraphIndex(vault_root)._open_read_snapshot()
    if connection is None:
        yield None
        return
    try:
        yield _Reader(connection, keep)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# The census reduction
# ---------------------------------------------------------------------------


def _census_payload(
    vault_root: Path,
    reader: _Reader,
    *,
    whole: bool,
    detail: str,
    start: dt.date | None,
    end: dt.date | None,
    date_from: str | None,
    date_to: str | None,
) -> dict[str, Any]:
    from .memory_schema import _normalize_indexed_relation

    registry = relation_registry.load_registry(vault_root)
    types = entity_types.load_entity_types(vault_root)
    eligible, undated, outside = _eligible_cohort(vault_root, reader, start=start, end=end)

    def entity_family(path: str) -> str | None:
        page = reader.pages.get(path)
        if page is None or not page.admitted or page.page_type != "entity":
            return None
        if not page.entity_type or types.resolve(page.entity_type) is None:
            return None
        return types.family_of(page.entity_type)

    entity_pages = {path for path in eligible if entity_family(path) is not None}
    pages_with_sources = reader.sources_pages()
    chain = reader.supersession_chains()

    by_status = dict.fromkeys(_STATUS_KEYS, 0)
    authored_edges = 0
    unresolved_target_edges = 0
    predicate_edges: Counter[str] = Counter()
    aliases_in_use: set[str] = set()
    unregistered_pages: dict[str, set[str]] = {}
    connected: set[str] = set()
    inbound: set[str] = set()
    typed_pages: set[str] = set()
    specific_pages: set[str] = set()
    entity_edges = dict.fromkeys(_STATUS_KEYS, 0)
    entity_specific: set[str] = set()
    entity_generic_only: dict[str, bool] = {}
    affiliated: set[str] = set()
    checks = {
        name: {"applicable": 0, "violations": 0}
        for name in (
            "signature_mismatch",
            "supersedes_backwards",
            "evidence_target_not_evidential",
            "answers_without_question",
            "same_page_epistemic",
            "contradicts_within_chain",
        )
    }

    def record(name: str, violated: bool) -> None:
        checks[name]["applicable"] += 1
        checks[name]["violations"] += int(violated)

    for row in reader.rows():
        source = row.source_path
        if not row.in_view:
            if source in eligible and row.origin in _AUTHORED_ORIGINS:
                unresolved_target_edges += 1
            continue
        for page in (row.src_page, row.dst_page):
            if page != source:
                inbound.add(page)
        if source not in eligible:
            continue
        typed = (
            row.relation is not None
            and row.status != "unregistered"
            and row.relation != _LINK
            and row.origin != "wikilink"
        )
        if row.touches_other:
            connected.add(source)
            if typed:
                typed_pages.add(source)
                if row.relation != _GENERIC:
                    specific_pages.add(source)
        if typed:
            definition = registry.definition(row.relation or "")
            if definition is not None and definition.family == "affiliation":
                affiliated.add(source)
            _check_row(
                row,
                definition,
                record,
                reader=reader,
                chain=chain,
                pages_with_sources=pages_with_sources,
            )
        if row.origin in _AUTHORED_ORIGINS:
            authored_edges += 1
            bucket = _bucket(row)
            by_status[bucket] += 1
            if bucket == "unregistered":
                label = _normalize_indexed_relation(row.raw_relation, row.line)
                unregistered_pages.setdefault(label, set()).add(source)
            else:
                predicate_edges[row.relation or ""] += 1
                if bucket == "alias":
                    aliases_in_use.add(relation_registry.normalize_relation(row.raw_relation))
        if source in entity_pages and row.origin != "wikilink" and row.touches_other:
            other = row.dst_page if row.src_page == source else row.src_page
            if other != source and other is not None and entity_family(other) is not None:
                bucket = _bucket(row)
                entity_edges[bucket] += 1
                generic = row.relation == _GENERIC and bucket == "core_generic"
                entity_generic_only[source] = entity_generic_only.get(source, True) and generic
                if typed and row.relation != _GENERIC:
                    entity_specific.add(source)

    directed_both_ways, inverse_duplicates = reader.pair_checks(eligible, registry)
    checks["directed_both_ways"] = {**directed_both_ways, "inspection_only": True}
    eligible_count = len(eligible)
    registered_edges = authored_edges - by_status["unregistered"]
    top3 = sum(count for _key, count in _ranked(predicate_edges)[:3])
    core_used = sorted(key for key in predicate_edges if key in registry.core)
    extension_used = sorted(key for key in predicate_edges if key in registry.extensions)
    disconnected = eligible - connected
    persons = {path for path in entity_pages if entity_family(path) == "person"}

    metrics: dict[str, Any] = {
        "authored_edges": authored_edges,
        "unresolved_target_edges": unresolved_target_edges,
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
        "inverse_duplicates": inverse_duplicates,
        # Near-duplicate groups need the vocabulary detector (S2-S7).
        "near_duplicate_groups": _UNMEASURED,
        "false_precision_judged": _UNMEASURED,
    }
    payload: dict[str, Any] = {
        "census_version": CENSUS_VERSION,
        "exomem_version": __version__,
        "available": True,
        "detail": detail,
        # A write counter over the whole vault, withheld writes included.
        "graph_generation": reader.generation if whole else None,
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
        "checks": {
            name: checks[name]
            for name in (
                "signature_mismatch",
                "supersedes_backwards",
                "evidence_target_not_evidential",
                "answers_without_question",
                "same_page_epistemic",
                "directed_both_ways",
                "contradicts_within_chain",
            )
        },
        "sample": None,
    }
    if detail == "keys":
        payload["keys"] = {
            "predicates": {key: predicate_edges[key] for key in sorted(predicate_edges)},
            "extensions_unused": sorted(set(registry.extensions) - set(extension_used)),
        }
    return payload


def _check_row(
    row: _Row,
    definition: relation_registry.RelationDefinition | None,
    record: Callable[[str, bool], None],
    *,
    reader: _Reader,
    chain: _UnionFind,
    pages_with_sources: set[str],
) -> None:
    """The structure-only rules that one typed edge can satisfy or break."""
    if row.status == "scope_violation" or (
        definition is not None and (definition.source_kinds or definition.target_kinds)
    ):
        outside = definition is not None and (
            (
                definition.source_kinds
                and row.source_kind is not None
                and row.source_kind not in definition.source_kinds
            )
            or (
                definition.target_kinds
                and row.target_kind is not None
                and row.target_kind not in definition.target_kinds
            )
        )
        record("signature_mismatch", row.status == "scope_violation" or bool(outside))
    src_page, dst_page = row.src_page or "", row.dst_page or ""
    same_page = src_page == dst_page
    if row.relation == "supersedes" and not same_page:
        newer = _origin_date(reader.pages[src_page].origin_date)
        older = _origin_date(reader.pages[dst_page].origin_date)
        if newer is not None and older is not None:
            record("supersedes_backwards", newer < older)
    if row.relation == "evidenced_by":
        if row.dst_kind == "file":
            relative = dst_page.removeprefix(kb_prefix())
            evidential = relative.startswith(_EVIDENCE_FOLDERS) or dst_page in pages_with_sources
        else:
            evidential = row.dst_kind in _EVIDENTIAL_KINDS
        record("evidence_target_not_evidential", not evidential)
    if row.relation == "answers":
        if row.dst_kind == "file":
            answered = dst_page in reader.question_pages
        else:
            answered = row.dst_kind in _QUESTION_KINDS
        record("answers_without_question", not answered)
    if definition is not None and definition.family in _EPISTEMIC_FAMILIES:
        record("same_page_epistemic", same_page)
    if row.relation == "contradicts" and not same_page:
        record("contradicts_within_chain", chain.find(src_page) == chain.find(dst_page))


def _eligible_cohort(
    vault_root: Path,
    reader: _Reader,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> tuple[set[str], int, int]:
    """Admitted pages that pass the shared relation-debt predicate, date-scoped."""
    prefix = kb_prefix()
    eligible: set[str] = set()
    undated = outside = 0
    for page in reader.pages.values():
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
    return eligible, undated, outside


# ---------------------------------------------------------------------------
# Judged sample (optional): refs only, seeded, stratified by family
# ---------------------------------------------------------------------------


def sample(
    vault_root: Path,
    *,
    keep: Any = CALLER,
    size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = 0,
) -> dict[str, Any]:
    """Draw a seeded, family-stratified sample of specific authored edges.

    Items are refs only (page path and anchor at each end) so the owner's agent
    can read and judge them locally; the file never leaves the machine, and
    nothing but the folded counts re-enters the census. Specific means an
    authored, registered edge between pages in view that is neither
    `relates_to` nor `links_to`. Each family keeps the edges with the smallest
    seeded hash of their identity, so one pass draws a uniform, reproducible
    sample while holding at most `size` candidates per family.
    """
    if type(size) is not int or size < 1:
        raise ValueError("INVALID_RELATION_ARGUMENT: sample size must be a positive integer")
    view = _resolve_view(vault_root, keep)
    if isinstance(view, str):
        return _refused(view, "counts")
    with _reader(vault_root, view.keep) as reader:
        if reader is None:
            return _unavailable("counts")
        registry = relation_registry.load_registry(vault_root)
        eligible, _undated, _outside = _eligible_cohort(vault_root, reader)
        strata: Counter[str] = Counter()
        # Per family, a max-heap (by negated hash) of the `size` smallest hashes.
        kept: dict[str, list[tuple[int, tuple[str, ...], dict[str, Any]]]] = {}
        for row in reader.rows():
            if (
                not row.in_view
                or row.source_path not in eligible
                or row.origin not in _AUTHORED_ORIGINS
                or row.relation in (None, _GENERIC, _LINK)
                or row.status == "unregistered"
            ):
                continue
            definition = registry.definition(row.relation or "")
            family = definition.family if definition is not None else "unknown"
            strata[family] += 1
            rank = int.from_bytes(
                hashlib.sha256(f"{seed}\0{row.edge_key}".encode()).digest()[:8], "big"
            )
            heap = kept.setdefault(family, [])
            entry = (
                -rank,
                (
                    row.source_path,
                    row.source_anchor or "",
                    row.relation or "",
                    row.dst_page or "",
                    row.dst_anchor or "",
                ),
                {
                    "family": family,
                    "relation": row.relation,
                    "source": {"path": row.source_path, "anchor": row.source_anchor},
                    "target": {"path": row.dst_page, "anchor": row.dst_anchor},
                    "verdict": None,
                },
            )
            if len(heap) < size:
                heapq.heappush(heap, entry)
            elif entry[:2] > heap[0][:2]:
                heapq.heapreplace(heap, entry)
        quotas = _allocate(size, strata)
        items: list[dict[str, Any]] = []
        for family in sorted(strata):
            smallest = sorted(kept[family], key=lambda entry: (-entry[0], entry[1]))
            for _rank, _order, item in sorted(
                smallest[: quotas.get(family, 0)], key=lambda entry: entry[1]
            ):
                items.append({"id": f"s{len(items) + 1:03d}", **item})
        return {
            "kind": "relation_census_sample",
            "census_version": CENSUS_VERSION,
            "graph_generation": reader.generation if view.whole else None,
            "registry": {
                "core_version": registry.core_version,
                "extension_hash": registry.extension_hash,
            },
            "seed": seed,
            "requested": size,
            "drawn": len(items),
            "strata": {family: strata[family] for family in sorted(strata)},
            "verdicts": list(VERDICTS),
            "items": items,
        }


def fold_judgments(judged: Mapping[str, Any]) -> dict[str, Any] | str:
    """Fold an agent-judged sample into counts with a 95% Wilson interval.

    Unjudged items (verdict null) are counted but not scored; a sample with no
    verdict at all stays `unmeasured`. An unknown verdict is refused, not
    guessed: a parser that meets an unexpected value is looking at a bad file.
    """
    items = judged.get("items") if isinstance(judged, Mapping) else None
    if not isinstance(items, list):
        raise ValueError("INVALID_JUDGMENT: judged file must hold a sample's items")
    by_verdict = dict.fromkeys(VERDICTS, 0)
    unjudged = 0
    for item in items:
        verdict = item.get("verdict") if isinstance(item, Mapping) else None
        if verdict is None:
            unjudged += 1
            continue
        if verdict not in by_verdict:
            raise ValueError(f"INVALID_JUDGMENT: unknown verdict {verdict!r}")
        by_verdict[verdict] += 1
    count = sum(by_verdict.values())
    if count == 0:
        return _UNMEASURED
    false = count - by_verdict["precise"]
    return {
        "judged": count,
        "unjudged": unjudged,
        "false": false,
        "rate": round(false / count, 4),
        "wilson_95": _wilson(false, count),
        "by_verdict": by_verdict,
    }


def _allocate(size: int, counts: Mapping[str, int]) -> dict[str, int]:
    """Quota per family: one each first (largest families first), then by share."""
    families = sorted(counts, key=lambda family: (-counts[family], family))
    quotas = dict.fromkeys(families, 0)
    remaining = min(size, sum(counts.values()))
    for family in families:
        if remaining == 0:
            break
        if counts[family]:
            quotas[family] = 1
            remaining -= 1
    while remaining:
        capacity = {family: counts[family] - quotas[family] for family in families}
        open_total = sum(capacity.values())
        shares = {family: remaining * capacity[family] / open_total for family in families}
        grants = {family: min(capacity[family], int(shares[family])) for family in families}
        if not any(grants.values()):
            # `families` is ordered largest first, then by name, and max keeps
            # the first of equals, so the tie-break is deterministic.
            best = max(
                (family for family in families if capacity[family]),
                key=lambda family: shares[family],
            )
            grants[best] = 1
        for family, grant in grants.items():
            quotas[family] += grant
            remaining -= grant
    return quotas


def _wilson(successes: int, total: int) -> list[float]:
    z2 = _WILSON_Z * _WILSON_Z
    proportion = successes / total
    denominator = 1 + z2 / total
    centre = (proportion + z2 / (2 * total)) / denominator
    half = (
        _WILSON_Z
        * math.sqrt(proportion * (1 - proportion) / total + z2 / (4 * total * total))
        / denominator
    )
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


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


def _bucket(row: _Row) -> str:
    status = row.status
    if status in {"unregistered", "scope_violation", "deprecated"}:
        return status
    if row.relation == _GENERIC:
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
    return _refused("graph_unavailable", detail)


def _refused(reason: str, detail: str) -> dict[str, Any]:
    return {
        "census_version": CENSUS_VERSION,
        "exomem_version": __version__,
        "available": False,
        "reason": reason,
        "detail": detail,
    }
