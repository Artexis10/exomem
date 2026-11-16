"""The topic brief: what one subject's notes conclude, cite and still leave open.

`read_memory(brief=True)` serves it and `exomem://brief/<seed id>` reads it again. The member walk and the
closing relations come from the graph index over the caller's view, so a read costs graph queries only. A
cold compile reads the member pages once, and the compiled brief is cached until a member, a closing
relation or a registry changes (openspec/changes/add-topic-brief, design D1 to D5).
"""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import (
    brief_watermarks,
    call_spans,
    context_catalogue,
    context_views,
    currency_boundary,
    epistemic_graph,
    lifecycle_statuses,
    markdown_regions,
    memory_refs,
    metrics,
    note_types,
    page_names,
    recall_policy,
    relation_registry,
    semantic_contract,
    semantic_language_registry,
    semantic_units,
    state_paths,
    traversal_profiles,
    vault,
    working_set,
    working_set_currency,
)
from .context_views import INDEX_WARMING, READY, UNAVAILABLE
from .governance import connector_boundary, egress, principal
from .vocabulary import registry as vocabulary_registry

#: The shipped traversal profile whose relation families the member walk follows.
PROFILE = "brief"
#: The brief's own metric series. A brief read is also a `read_page` call that `exomem_tool_*` already counts, so a series
#: of its own, not a second `tool` label on that one, keeps a sum over `tool` equal to the calls served.
READS = "exomem_brief_reads_total"
FAILURES = "exomem_brief_failures_total"
DURATION = "exomem_brief_read_duration_ms"

#: Resource bounds of the brief contract (spec: "Brief members are bounded"), not vault vocabulary.
MAX_HOPS = 2
NOTE_CAP = 40
SOURCE_CAP = 40
RELATED_CAP = 10
#: Edge rows a closing read inspects before it stops; a bound on work, never a count served.
EDGE_LIMIT = 20_000
#: Edge rows one walk step reads for each role, shared evenly among the pages it expands and never fewer than `PAGE_ROWS_FLOOR`
#: each, so a hub that links thousands of pages costs a step its share, not its degree. A row counts toward a share only when its
#: far page is of that role, so rows that cannot yield a member spend none. A cut is reported as an inexact count.
#: A restricted reader decides each row's other end, about 0.6 ms each: on a generated store with a 10,000-link hub a step
#: of 4,000 rows cost such a read about 4 s and a step of 500 rows under 1 s (tasks.md 3.2), so the bound is the budget.
STEP_ROWS = 500
PAGE_ROWS_FLOOR = 50
#: What a walk step reads, by the role a far page plays in a brief, with the type predicate that selects it. The names are the
#: `missing` roles of the response schema. Types come from the registry through the predicate, never from a list here.
COMPILED_NOTE = "compiled_note"
SOURCE = "source"
_ROLES = {COMPILED_NOTE: note_types.compiled, SOURCE: note_types.captured_source}  # nosemgrep: ep-word-set -- the response schema's own role names.
#: Candidates past a cap that the walk checks for visibility before it reports a lower bound: a bound on work, never a count served.
COUNT_BOUND = 40
#: Bound parameters per statement, well under SQLite's variable limit.
_CHUNK = 400
CACHE_SIZE = 128

# The response schema of the brief fixes these section names (spec: "A brief has conclusions, sources and open questions").
CONCLUSIONS = "conclusions"
SOURCES = "sources"
OPEN_QUESTIONS = "open_questions"

#: The fields of a response that the terminal guard restores from this invocation's compile.
_PINNED = ("state", "seed", "items", "missing", "related", "changed_since")  # nosemgrep: ep-word-set -- the response schema's own field names.

_CACHE: OrderedDict[tuple[Any, ...], tuple[date | None, _Compiled]] = OrderedDict()
#: A page's external links by the content hash of its text. Reading the links is the dearest step of a compile (a body of
#: ten thousand list items takes over a second), and a hub that many briefs share needs it once per version of its text.
LINKS_CACHE_SIZE = 512
_LINKS: OrderedDict[str, tuple[markdown_regions.ExternalLink, ...]] = OrderedDict()
#: A seed's walk and its closing edges, kept per graph snapshot so a warm read of an unchanged graph reads no edge.
#: Bounded like `_CACHE`: a process serves a few dozen topics, and a value holds at most NOTE_CAP + SOURCE_CAP pages.
_WALKS: OrderedDict[tuple[Any, ...], tuple[_Walk, tuple[tuple[str, str, str], ...]]] = OrderedDict()
_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# The graph, read in the caller's view
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Node:
    """One page as the graph index holds it: its identity, its type and the hash of its bytes."""

    path: str
    id: str | None
    title: str
    page_type: str | None
    lifecycle: str
    updated: str
    captured: str
    content_hash: str

    @property
    def ref(self) -> str:
        assert self.id is not None
        return memory_refs.memory_ref(self.id)


def _chunks(values: Sequence[Any]) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]


class _Graph:
    """One read snapshot of the graph index, filtered by what the caller may see."""

    def __init__(self, root: Path, caller: context_views.CallerView, conn: Any) -> None:
        self.conn = conn
        self.caller = caller
        self.view = epistemic_graph.GraphView(root, conn, keep=caller.visible, reads_placeholders=False)
        relations = relation_registry.load_registry(root)
        self.profile = traversal_profiles.load_profiles(root, registry=relations).resolve(PROFILE)
        self.types = note_types.Basis(root)
        self._seen: dict[str, bool] = {}

    def token(self) -> tuple[str | None, ...] | None:
        """What names this snapshot's rows, read in the walk's own transaction; None when it names no generation."""
        return epistemic_graph.snapshot_cache_token(self.conn)

    def sees(self, path: str) -> bool:
        """Whether the caller may see the page, decided once per read: the snapshot and the caller do not change under it."""
        seen = self._seen.get(path)
        if seen is None:
            seen = self._seen[path] = self.caller.sees(path) and self.view.allowed(path)
        return seen

    def _decide(self, paths: Iterable[str]) -> None:
        """Decide the visibility of the pages not yet decided as one set, so a restricted caller costs one release context per batch."""
        unknown = [path for path in dict.fromkeys(paths) if path not in self._seen]
        if not unknown:
            return
        kept = None if self.caller.visible is None else egress.admitted(self.caller.visible, unknown)
        self.view.parents.admit(unknown)
        for path in unknown:
            self._seen[path] = (kept is None or path in kept) and self.view.allowed(path)

    def admit(self, ordered: Sequence[_Node], room: int) -> tuple[list[_Node], int, int, bool]:
        """The first `room` candidates that the caller sees and that have an id, with what lay behind them.

        Visibility is decided in order, a batch at a time, and stops once `room + COUNT_BOUND` visible candidates are
        inspected, so a hub of thousands of pages costs what its first pages cost. Returns the taken pages, the
        visible named pages inspected past `room`, the visible pages with no id, and whether every candidate was
        inspected. A withheld page moves none of these: it is skipped before anything counts it.
        """
        taken: list[_Node] = []
        cut = unnamed = 0
        for index, node in enumerate(ordered):
            if node.path not in self._seen:
                self._decide(candidate.path for candidate in ordered[index : index + room + COUNT_BOUND])
            if not self._seen[node.path]:
                continue
            if node.id is None:
                unnamed += 1
            elif len(taken) < room:
                taken.append(node)
            else:
                cut += 1
            if len(taken) + cut + unnamed >= room + COUNT_BOUND:
                return taken, cut, unnamed, False
        return taken, cut, unnamed, True

    def nodes(self, paths: Sequence[str]) -> dict[str, _Node]:
        found: dict[str, _Node] = {}
        for chunk in _chunks(list(paths)):
            marks = ",".join("?" for _ in chunk)
            # The unary plus keeps `kind` out of index choice: left alone, the planner scans every file node through the `kind` index per chunk.
            rows = self.conn.execute(
                "SELECT path, exomem_id, title, page_type, lifecycle_status, updated_date, origin_date, source_hash "
                f"FROM graph_nodes WHERE +kind = 'file' AND path IN ({marks})",
                list(chunk),
            ).fetchall()
            for path, exomem_id, title, page_type, lifecycle, updated, captured, content_hash in rows:
                found[path] = _Node(
                    path, memory_refs.normalize_id(exomem_id), title or "", page_type, lifecycle or "",
                    updated or "", captured or "", content_hash,
                )
        return found

    def departed(self, refs: Sequence[str]) -> list[str]:
        """Of the refs of pages that left a brief, those a reader may be told left: the page is gone, or the caller sees it.

        A page that is still in the graph but that the caller may not see now left no trace the caller can read, so a
        withholding is neither a removal nor a change.
        """
        ids = {ref: memory_refs.parse_memory_ref(ref) or "" for ref in refs}
        held: dict[str, list[str]] = {}
        for chunk in _chunks(sorted(set(ids.values()))):
            marks = ",".join("?" for _ in chunk)
            for exomem_id, path in self.conn.execute(
                f"SELECT exomem_id, path FROM graph_nodes WHERE +kind = 'file' AND exomem_id IN ({marks})", list(chunk),
            ):
                held.setdefault(exomem_id, []).append(path)
        self._decide(path for paths in held.values() for path in paths)
        return [ref for ref in refs if ids[ref] not in held or any(self.sees(path) for path in held[ids[ref]])]

    def node_keys(self, paths: Sequence[str], *, units_only: bool = False) -> dict[str, list[str]]:
        """Every graph node of these pages, by page: the page itself and each unit, which a relation can name."""
        keys: dict[str, list[str]] = {path: [] for path in paths}
        for chunk in _chunks(list(paths)):
            marks = ",".join("?" for _ in chunk)
            where = "kind != 'file' AND " if units_only else ""
            for path, node_key in self.conn.execute(
                f"SELECT path, node_key FROM graph_nodes WHERE {where}path IN ({marks})", list(chunk),
            ):
                keys[path].append(node_key)
        return keys

    def _paths_of(self, keys: set[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for chunk in _chunks(sorted(keys)):
            marks = ",".join("?" for _ in chunk)
            found.update(self.conn.execute(
                f"SELECT node_key, path FROM graph_nodes WHERE node_key IN ({marks})", list(chunk),
            ).fetchall())
        return found

    def _edges(
        self, groups: Iterable[Sequence[str]], accept: Callable[[dict[str, Any]], bool], *, row_limit: int | None = None,
        end_types: frozenset[str] = frozenset(),
        beyond: frozenset[str] = frozenset(),
    ) -> tuple[list[dict[str, Any]], bool]:
        """The live edges that `accept` takes at each group of node keys, and whether a bound cut the read.

        Each group is read on its own, so a hub in one group cannot spend the rows of the others; without a `row_limit`
        the read keeps the graph's own bounds. With `end_types` a bounded read counts only rows whose far page is of one
        and is none of the pages in `beyond`.
        """
        edges: dict[str, dict[str, Any]] = {}
        overflow = False
        for keys in groups:
            for chunk in _chunks(list(keys)):
                found, over = self.view.neighbor_edges(
                    set(chunk), limit=EDGE_LIMIT if row_limit is None else row_limit, accept=accept, row_limit=row_limit,
                    end_types=end_types, beyond=beyond,
                )
                overflow = overflow or over
                edges.update({str(edge["edge_key"]): edge for edge in found})
        return [edges[key] for key in sorted(edges)], overflow

    def _walkable(self, edge: dict[str, Any]) -> bool:
        """Whether the relation's family is one of the shipped `brief` profile, read through its authoring page's registry."""
        registry = self.view.registry_for(str(edge.get("source_path") or ""))
        definition = registry.definition(str(edge.get("relation_type") or ""))
        return definition is not None and traversal_profiles.relation_allowed(self.profile, definition)

    def adjacent(
        self, frontier: Sequence[str], roles: Iterable[str] = _ROLES,
    ) -> tuple[dict[str, set[str]], frozenset[str]]:
        """The pages joined to the frontier by an edge of the profile, in either direction.

        Returns each such page with the frontier pages it touches, and the roles whose rows the bound cut. A page whose
        rows all fit its share is read once. A page that does not fit is read once per role, counting only rows whose far
        page plays that role, so many cited Sources cannot spend the rows of the linked notes and a row that cannot
        yield a member spends none. A withheld page moves neither choice: it counts toward neither test.
        """
        # One read per page, each with an even share of the step's rows: the frontier's order is fixed, so the cut is too.
        share = max(PAGE_ROWS_FLOOR, STEP_ROWS // max(1, len(frontier)))
        edges: dict[str, dict[str, Any]] = {}
        cut: set[str] = set()
        for page_keys in self.node_keys(frontier).values():
            for chunk in _chunks(page_keys):
                fits = self.view.rows_fit(set(chunk), share)
                for role in (None,) if fits else roles:
                    end_types = frozenset() if role is None else frozenset(self.types.keys(_ROLES[role]))
                    if role is not None and not end_types:
                        continue
                    found, overflow = self._edges(
                        [chunk], self._walkable, row_limit=share, end_types=end_types, beyond=frozenset(frontier),
                    )
                    if overflow and role is not None:
                        cut.add(role)
                    edges.update({str(edge["edge_key"]): edge for edge in found})
        ends = self._paths_of({key for edge in edges.values() for key in (edge["src_key"], edge["dst_key"])})
        near = set(frontier)
        touches: dict[str, set[str]] = {}
        for key in sorted(edges):
            source, target = ends.get(edges[key]["src_key"]), ends.get(edges[key]["dst_key"])
            if source is None or target is None or source == target:
                continue
            if source in near and target not in near:
                touches.setdefault(target, set()).add(source)
            if target in near and source not in near:
                touches.setdefault(source, set()).add(target)
        return touches, frozenset(cut)

    def closing(self, paths: Sequence[str]) -> tuple[tuple[str, str, str], ...]:
        """The edges, from pages the caller sees, that settle or bear on a unit of these pages: (edge, relation, unit key).

        A relation that names a unit by its fragment lands on that unit's node, so the unit it closes is the edge's end.
        """
        keys = [key for unit_keys in self.node_keys(paths, units_only=True).values() for key in unit_keys]
        unit_keys = set(keys)

        def closes(edge: dict[str, Any]) -> bool:
            return self.view.has(edge, "settles") or self.view.has(edge, "bears_on")

        edges, _overflow = self._edges([keys], closes)
        closers = [edge for edge in edges if edge["dst_key"] in unit_keys]
        self._decide(str(edge["source_path"]) for edge in closers)
        return tuple(sorted(
            (str(edge["edge_key"]), str(edge["relation_type"]), str(edge["dst_key"]))
            for edge in closers
            if self.sees(str(edge["source_path"]))
        ))


@contextmanager
def _open_graph(root: Path, caller: context_views.CallerView) -> Iterator[_Graph | None]:
    # A request never waits on the whole-vault cold proof (#1454): `SINGLE_FLIGHT` proves inline in a one-shot process, hands
    # it to the background in a managed service, and the brief then abstains as warming, as the find graph lane does.
    index = epistemic_graph.EpistemicGraphIndex(root, prove_cold_snapshots=epistemic_graph.SINGLE_FLIGHT)
    conn = index._open_read_snapshot()  # noqa: SLF001 - the graph's read transaction, opened as its other readers open it
    if conn is None:
        yield None
        return
    try:
        yield _Graph(root, caller, conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The seed and the members
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Walk:
    seed: _Node
    notes: tuple[tuple[_Node, int], ...]
    sources: tuple[_Node, ...]
    missing: tuple[dict[str, Any], ...]


def _find_seed(
    root: Path, graph: _Graph, subject: str, purpose: str | None
) -> tuple[_Node | None, list[_Node] | None, bool]:
    """The seed, the compiled notes that a topic search also found (None when no search ran), and whether the index lagged it.

    A subject that names a page the caller may read is that page. Any other subject is topic text, and one hybrid
    search over the caller's view answers it: its top compiled-note hit is the seed.
    """
    target = context_views.parse_view_ref(subject)
    if target is not None and target.kind == context_views.VIEW_BRIEF:
        exomem_id = memory_refs.normalize_id(target.key)
        if exomem_id is None:
            raise context_views.not_found()
        path = page_names.resolve_page(root, memory_refs.memory_ref(exomem_id), serve_stale=True)
        seed = graph.nodes([path]).get(path)
        if seed is None or not graph.sees(path):
            raise context_views.not_found()
        return seed, None, False
    path = page_names.resolve_page(root, subject, serve_stale=True)
    named = graph.nodes([path]).get(path)
    if named is not None and graph.sees(path):
        return named, None, False
    from . import commands

    found = commands.op_find(
        root, query=subject, types=list(graph.types.keys(note_types.compiled)), detail="compact",
        limit=1 + RELATED_CAP + NOTE_CAP, result_level="page", purpose=purpose,
    )
    hits = found.get("hits", []) if isinstance(found, Mapping) else found
    lagging = isinstance(found, Mapping) and bool(found.get("warming") or found.get("degraded"))
    paths = list(dict.fromkeys(str(hit["path"]) for hit in hits if isinstance(hit, Mapping) and hit.get("path")))
    nodes = graph.nodes(paths)
    hit_nodes = [nodes[p] for p in paths if p in nodes and graph.sees(p)
                 and graph.types.selects(nodes[p].page_type, note_types.compiled)]
    if not hit_nodes:
        return None, None, lagging
    return hit_nodes[0], hit_nodes[1:], False


def _newest_first(nodes: Sequence[_Node]) -> list[_Node]:
    ordered = sorted(nodes, key=lambda node: node.path)
    ordered.sort(key=lambda node: node.updated, reverse=True)
    return ordered


def _walk(graph: _Graph, seed: _Node) -> _Walk:
    """The members of the seed's brief, over the caller's view, from the seed alone.

    Compiled notes within `MAX_HOPS` of the seed through the profile's families, passing only through compiled
    notes; the Sources they attach. A candidate is filtered by what the caller sees before anything counts it.
    Each page's edges are read once for each role, and a page's neighbours are checked for visibility only as far as a cap needs.
    """
    compiled = graph.types.selects(seed.page_type, note_types.compiled)
    distance: dict[str, int] = {seed.path: 0}
    notes: list[_Node] = [seed] if compiled else []
    #: Each page next to an expanded page, with the expanded pages it touches.
    adjacency: dict[str, set[str]] = {}
    expanded: set[str] = set()
    #: The roles whose rows a step's bound cut: what the step left unread is of unknown number.
    row_cut: set[str] = set()

    def expand(pages: Sequence[str], roles: Iterable[str] = _ROLES) -> dict[str, set[str]]:
        touches, cut_roles = graph.adjacent(pages, roles)
        expanded.update(pages)
        row_cut.update(cut_roles)
        for page, near in touches.items():
            adjacency.setdefault(page, set()).update(near)
        return touches

    # The seed starts the walk even when it is no compiled note: a brief of a Source reads its citing notes.
    frontier = [seed.path]
    cut, exact, unnamed_notes = 0, True, 0
    for hop in range(1, MAX_HOPS + 1):
        touches = expand(frontier)
        exact = exact and COMPILED_NOTE not in row_cut
        found = graph.nodes([path for path in touches if path not in distance])
        candidates = _newest_first([
            node for node in found.values() if graph.types.selects(node.page_type, note_types.compiled)
        ])
        # A page with no stable id has no ref to serve its items under: counted, never silently left out.
        taken, left, unnamed, complete = graph.admit(candidates, max(0, NOTE_CAP - len(notes)))
        unnamed_notes += unnamed
        cut += left
        # Pages behind a cut page are never examined, so the count then holds at least what was cut.
        exact = exact and complete and not left
        for node in taken:
            distance[node.path] = hop
            notes.append(node)
        frontier = [node.path for node in taken]
        if not frontier:
            break
    anchors = list(dict.fromkeys([seed.path, *(node.path for node in notes)]))
    unread = [path for path in anchors if path not in expanded]
    if unread:
        expand(unread, (SOURCE,))  # a note at the last hop is read only for the Sources it cites
    found = graph.nodes([path for path in adjacency if path not in anchors])
    attached = {
        node.path: min(distance[near] for near in adjacency[node.path])
        for node in found.values()
        if graph.types.selects(node.page_type, note_types.captured_source)
    }
    if graph.types.selects(seed.page_type, note_types.captured_source):
        attached[seed.path] = 0
        found[seed.path] = seed
    ordered = _newest_first([found[path] for path in attached])
    ordered.sort(key=lambda node: attached[node.path])
    sources, left_sources, unnamed_sources, complete_sources = graph.admit(ordered, SOURCE_CAP)
    exact_sources = SOURCE not in row_cut and complete_sources
    missing = []
    # A lower bound: when a bound cut rows before any candidate was counted, the count is unknown and says so, never 0.
    if cut or not exact:
        missing.append({"role": COMPILED_NOTE, "reason": "cap", "count": cut or None, "exact": exact})
    if left_sources or not exact_sources:
        missing.append({"role": SOURCE, "reason": "cap", "count": left_sources or None, "exact": exact_sources})
    for role, count in ((COMPILED_NOTE, unnamed_notes), (SOURCE, unnamed_sources)):
        if count:
            missing.append({"role": role, "reason": "unreferenced", "count": count})
    ranked = _newest_first(notes)
    ranked.sort(key=lambda node: distance[node.path])
    return _Walk(seed, tuple((node, distance[node.path]) for node in ranked), tuple(sources), tuple(missing))


def _members(
    root: Path, caller: context_views.CallerView, graph: _Graph, seed: _Node, registries: str,
) -> tuple[_Walk, tuple[tuple[str, str, str], ...]]:
    """The seed's walk and closing edges: from the memo when the graph snapshot and the policy are the ones that made it.

    Only an owner's walk is kept. A restricted caller's release decision reads the governance policy, the tombstones
    and its authorization live, and no graph token names them, so that walk is made afresh on every read. For the
    owner the walk reads the graph's rows and the recall policy, both of which the key names.
    """
    token = None if caller.restricted else graph.token()
    key = None if token is None else (
        str(root), caller.binding, seed.path, token, recall_policy.recall_policy_identity(root), registries,
    )
    if key is not None:
        with _LOCK:
            held = _WALKS.get(key)
            if held is not None:
                _WALKS.move_to_end(key)
                return held
    walk = _walk(graph, seed)
    made = (walk, graph.closing([node.path for node, _distance in walk.notes]))
    if key is not None:
        with _LOCK:
            _WALKS[key] = made
            _WALKS.move_to_end(key)
            while len(_WALKS) > CACHE_SIZE:
                _WALKS.popitem(last=False)
    return made


# --------------------------------------------------------------------------- #
# Compile
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Compiled:
    """The served items of one brief, with the unit wires that terminal egress decides again."""

    items: tuple[dict[str, Any], ...]
    #: For each item, the wire of the unit it serves, else None.
    wires: tuple[dict[str, Any] | None, ...]
    #: The refs of the served units, for `changed_since`.
    units: tuple[str, ...]
    #: A member page's bytes differ from what the index holds, so this compile is never kept.
    stale: bool


def _read_page(root: Path, path: str) -> tuple[str, Any]:
    """One member page's text, with the guard that proves its bytes at the end of the compile.

    The compile's only page read: a warm read performs none, and a test counts the calls here.
    """
    return vault.read_guarded_text(root, root / path)


def _external_links(content_hash: str, body: str) -> tuple[markdown_regions.ExternalLink, ...]:
    """The external links of a page body, kept under the hash of the text it came from."""
    with _LOCK:
        held = _LINKS.get(content_hash)
        if held is not None:
            _LINKS.move_to_end(content_hash)
            return held
    found = markdown_regions.external_links(body)
    with _LOCK:
        _LINKS[content_hash] = found
        while len(_LINKS) > LINKS_CACHE_SIZE:
            _LINKS.popitem(last=False)
    return found


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _registry_digest(root: Path) -> str:
    """What the registries that shape a compile say: the profile, relations, note types, statuses and unit kinds.

    Every instance's relations and categories count, not the public instance's alone: a private instance can
    reinterpret a member's units with no edit to the page, so the page's content hash does not name that change.
    """
    relations = relation_registry.load_registry(root)
    parts = [
        traversal_profiles.load_profiles(root, registry=relations).content_hash,
        relations.extension_hash,
        vocabulary_registry.load(note_types.SPEC, root).effective_digest,
        vocabulary_registry.load(lifecycle_statuses.SPEC, root).effective_digest,
        semantic_language_registry.load_registry(root).content_hash,
        semantic_contract.page_definitions_dependency(root),
    ]
    return _digest(parts)


def _unlinked(text: str) -> str:
    # A link names a page that may be withheld: served without it, no withheld page changes a byte.
    return egress.without_wikilinks(text)


@dataclass(slots=True)
class _Candidate:
    item: working_set.LaneItem
    node: _Node
    section: str
    kind: str
    category: str
    closed: bool


def _compile(
    root: Path, caller: context_views.CallerView, walk: _Walk, closing: Sequence[tuple[str, str, str]],
    *, today: date,
) -> tuple[_Compiled, date | None]:
    """Read the members once and assemble the three sections; the date is when the result stops being current."""
    from . import audit

    closed_keys = {key for _edge, _relation, key in closing}
    status_basis = lifecycle_statuses.Basis(root)
    stale = False
    guards: dict[str, Any] = {}
    headers: dict[str, dict[str, Any]] = {}
    candidates: list[_Candidate] = []
    links: list[dict[str, Any]] = []
    seen_links: set[str] = set()
    for node, distance in walk.notes:
        try:
            source, guard = _read_page(root, node.path)
        except (OSError, ValueError):
            stale = True
            continue
        guards[node.path] = guard
        text_hash = vault.content_hash(source)
        stale = stale or text_hash != node.content_hash
        page = semantic_contract.build_page_state(root, node.path, source)
        language = (
            page.definitions.snapshots["categories"].typed if page.definitions is not None
            else semantic_language_registry.core_registry()
        )
        relations = (
            page.definitions.snapshots["relations"].typed if page.definitions is not None
            else relation_registry.core_registry()
        )
        summary = page.frontmatter.get("summary") or page.frontmatter.get("question") or ""
        headers[node.path] = {
            "section": CONCLUSIONS, "kind": "note", "ref": node.ref, "path": node.path, "updated": node.updated,
            "title": _unlinked(page.title), "text": _unlinked(str(summary)), "distance": distance,
            "omitted": {"superseded": 0, "closed": 0, "expired": 0},
        }
        for link in _external_links(text_hash, page.body):
            if link.url not in seen_links:
                seen_links.add(link.url)
                links.append({
                    "section": SOURCES, "kind": "link", "ref": node.ref, "path": node.path,
                    "updated": node.updated, "title": _unlinked(link.label), "url": link.url,
                })
        for unit in page.document.units:
            if not unit.unit_ref or any(
                unit.span.start_line <= error.line <= unit.span.end_line
                for error in page.document.errors if error.line is not None
            ):
                continue
            if language.has(unit.kind, "question"):
                section = OPEN_QUESTIONS
            elif language.has(unit.kind, "assertion") or (unit.form == "compact" and unit.category):
                # A compact unit is an observation, and only a categorised one is a conclusion; both are closed schema attributes.
                section = CONCLUSIONS
            else:
                continue
            unit_key = epistemic_graph.unit_node_key(
                node.path, semantic_units.occurrence_key(unit.form, unit.span, unit.source_hash), unit.form,
            )
            # Only a kind that carries the `closable` trait can close, by its own verdict or by a relation that bears on it.
            closed = language.has(unit.kind, "closable") and bool(
                unit.verdict or audit._bears_on_unit(relations, unit) or unit_key in closed_keys  # noqa: SLF001
            )
            candidates.append(_Candidate(
                working_set.LaneItem(
                    role="", level="unit", ref=unit.unit_ref, path=node.path, title=page.title,
                    text=working_set.bounded_text(_unlinked(unit.content)), lifecycle=page.status or "",
                    updated=unit.as_of or "", anchor=node.path,
                    provenance=currency_boundary.unit_provenance(unit, registry=relations),
                ),
                node, section, unit.kind, unit.category if unit.form == "compact" else "", closed,
            ))
    source_items: list[dict[str, Any]] = []
    for node in walk.sources:
        try:
            text, guard = _read_page(root, node.path)
        except (OSError, ValueError):
            stale = True
            continue
        guards[node.path] = guard
        stale = stale or vault.content_hash(text) != node.content_hash
        frontmatter, _body, _raw = vault.parse_frontmatter(text)
        source_items.append({
            "section": SOURCES, "kind": "source", "ref": node.ref, "path": node.path, "updated": node.updated,
            "title": _unlinked(node.title), "source_kind": str(frontmatter.get("source_type") or ""),
            "captured": node.captured,
        })
    wires = [working_set._served_unit(entry.item) for entry in candidates]  # noqa: SLF001 - exactly the existing unit admission shape
    verdicts = egress.classify_units(root, wires, purpose=caller.purpose)
    kept = [entry for entry, verdict in zip(candidates, verdicts, strict=True) if verdict == egress.UNIT_KEPT]
    vault.recheck_path_guards(root, guards.values())
    annotated = working_set_currency.annotate(
        tuple(entry.item for entry in kept), status_basis=status_basis, visible=caller.visible,
    )
    items: list[dict[str, Any]] = []
    unit_wires: list[dict[str, Any] | None] = []
    units: list[str] = []
    boundaries: list[date] = []
    conclusions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    questions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry, item in zip(kept, annotated, strict=True):
        boundaries += currency_boundary.boundaries(item.provenance, today=today)
        history, review_due = working_set_currency.state(item, today=today, status_basis=status_basis)
        omitted = headers[entry.node.path]["omitted"]
        if item.lifecycle == working_set_currency.SUPERSEDED:
            omitted["superseded"] += 1
        elif entry.closed:
            omitted["closed"] += 1
        elif history:
            omitted["expired"] += 1
        elif item.text:
            served = {
                "section": entry.section, "kind": entry.kind, "ref": item.ref, "path": entry.node.path,
                "updated": entry.node.updated, "title": _unlinked(item.title), "text": item.text,
            }
            if entry.category:
                served["category"] = entry.category
            if review_due:
                served["review_due"] = review_due
            (conclusions if entry.section == CONCLUSIONS else questions).append((served, working_set._served_unit(item)))  # noqa: SLF001
            units.append(item.ref)
    by_page: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for served, wire in conclusions:
        by_page.setdefault(served["path"], []).append((served, wire))
    for node, _distance in walk.notes:
        if node.path not in headers:
            continue
        items.append(headers[node.path])
        unit_wires.append(None)
        for served, wire in by_page.get(node.path, ()):
            items.append(served)
            unit_wires.append(wire)
    items += source_items + links
    unit_wires += [None] * (len(source_items) + len(links))
    for served, wire in questions:
        items.append(served)
        unit_wires.append(wire)
    return _Compiled(tuple(items), tuple(unit_wires), tuple(units), stale), min(boundaries) if boundaries else None


def _compiled(
    root: Path, caller: context_views.CallerView, walk: _Walk, closing: Sequence[tuple[str, str, str]], registries: str,
) -> _Compiled:
    """The compiled brief for these members: from the cache when its key still holds, else compiled and kept."""
    today = date.today()
    members = [(node.id, node.content_hash, node.lifecycle, node.path, distance) for node, distance in walk.notes]
    sources = [(node.id, node.content_hash, node.lifecycle, node.path) for node in walk.sources]
    key = (
        str(root), caller.binding,
        _digest([walk.seed.path, members, sources, list(closing), registries, [PROFILE, MAX_HOPS, NOTE_CAP, SOURCE_CAP]]),
    )
    with _LOCK:
        held = _CACHE.get(key)
        if held is not None and (held[0] is None or today < held[0]):
            _CACHE.move_to_end(key)
            cached = held[1]
        else:
            cached = None
    # A release decision can change with no member changing, so the cached units are decided again, without a page read.
    if cached is not None and all(
        verdict == egress.UNIT_KEPT for verdict in egress.classify_units(
            root, [wire for wire in cached.wires if wire is not None], purpose=caller.purpose,
        )
    ):
        return copy.deepcopy(cached)
    compiled, expires = _compile(root, caller, walk, closing, today=today)
    if not compiled.stale:
        with _LOCK:
            _CACHE[key] = (expires, copy.deepcopy(compiled))
            _CACHE.move_to_end(key)
            while len(_CACHE) > CACHE_SIZE:
                _CACHE.popitem(last=False)
    return compiled


# --------------------------------------------------------------------------- #
# What changed since the principal last read
# --------------------------------------------------------------------------- #


def _view_key(root: Path, caller: context_views.CallerView) -> str:
    """What a watermark was read through: the audience, the declared purpose and the connector ceiling.

    The ceiling is its denied scopes, not the client or the login, so a returning client keeps its watermark and two
    clients that see alike share one. A login's own grants reach the diff only through the view filter.
    """
    who = principal.effective_principal()
    return _digest([who.audience_id, caller.purpose, connector_boundary.ceiling_identity(root, who)])


def _changed_since(
    graph: _Graph, root: Path, view: str, seed: _Node, walk: _Walk, compiled: _Compiled,
) -> tuple[dict[str, Any], Callable[[], bool] | None]:
    """`changed_since` against this view's watermark, and the call that advances it once the brief is served.

    Everything compared is what the caller sees now: the members and units come from the caller's walk, and a
    stored member that the caller may no longer see is left out of `removed`. No call is returned when the
    watermark could not be read, since the next read would then call a changed brief a first one.
    """
    members = {node.ref: node.content_hash for node, _distance in walk.notes}
    members.update({node.ref: node.content_hash for node in walk.sources})
    where = {node.ref: node for node, _distance in walk.notes}
    where.update({node.ref: node for node in walk.sources})
    advance = functools.partial(brief_watermarks.write, root, view, seed.id or "", members, compiled.units)
    try:
        mark = brief_watermarks.read(root, view, seed.id or "")
    except brief_watermarks.WatermarkUnavailable:
        return {"state": UNAVAILABLE}, None
    if mark is None:
        return {"state": "first_read"}, advance
    new = [ref for ref in members if ref not in mark.members]
    changed = [
        ref for ref in members
        if ref in mark.members and mark.members[ref] != brief_watermarks.member_digest(members[ref])
    ]
    removed = graph.departed([ref for ref in mark.members if ref not in members])
    # A row cut at its unit bound cannot say which units were new, and says so instead of listing the rest as new.
    new_units = (
        [ref for ref in compiled.units if brief_watermarks.unit_digest(ref) not in mark.units] if mark.units_exact else []
    )
    report: dict[str, Any] = {
        "state": "changed" if new or changed or removed or new_units else "unchanged",
        "new": [{"ref": ref, "path": where[ref].path, "title": _unlinked(where[ref].title)} for ref in new],
        "changed": [
            {"ref": ref, "path": where[ref].path, "title": _unlinked(where[ref].title), "updated": where[ref].updated}
            for ref in changed
        ],
        "removed": removed,
        "new_units": new_units,
    }
    if not mark.units_exact:
        report["new_units_exact"] = False
    return report, advance


# --------------------------------------------------------------------------- #
# Serving
# --------------------------------------------------------------------------- #


def _pin_key(root: Path, response: Mapping[str, Any], caller: context_views.CallerView) -> tuple[Any, ...]:
    return ("brief", str(root), response.get("ref"), caller.binding, response.get("generation"), response.get("continuation"))


def _pin(
    root: Path, caller: context_views.CallerView, response: dict[str, Any], wires: list[dict[str, Any]],
    advance: Callable[[], bool] | None = None,
) -> None:
    """Keep what this invocation compiled for the terminal guard, with the watermark write that a served brief owes."""
    held = principal.invocation_served()
    if held is not None:
        held[_pin_key(root, response, caller)] = (copy.deepcopy(response), copy.deepcopy(wires), advance)


def unavailable(response: Mapping[str, Any]) -> dict[str, Any]:
    """A brief that this response cannot be proven to be: no item, no continuation, no name."""
    return {
        "ref": response.get("ref"), "state": UNAVAILABLE, "generation": None, "stale": False,
        "items": [], "continuation": None,
    }


def _paths(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, inner in value.items():
            if key == "path" and isinstance(inner, str):
                yield inner
            else:
                yield from _paths(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _paths(inner)


def guard_response(
    root: Path, caller: context_views.CallerView, response: dict[str, Any]
) -> dict[str, Any]:
    """The terminal guard's pass: serve what this invocation compiled for this caller, decided again.

    Nothing but the response's own markers is taken from the response. A brief that this invocation did not
    compile, or that names a page the caller may no longer see or a unit that egress now removes, is served as
    unavailable whole: the response states counts and changes that no item removal could keep in step.
    """
    held = principal.invocation_served()
    pinned = None if held is None else held.get(_pin_key(root, response, caller))
    if pinned is None:
        return unavailable(response)
    body, wires, advance = pinned
    if any(not caller.sees(path) for path in _paths({field: body.get(field) for field in _PINNED})):
        return unavailable(response)
    if any(verdict != egress.UNIT_KEPT for verdict in egress.classify_units(
        root, copy.deepcopy(wires), purpose=caller.purpose,
    )):
        return unavailable(response)
    if advance is not None:
        # The guard has passed this page: the last page of a brief moves the watermark only now that it is served.
        advance()
    return {**response, **{field: copy.deepcopy(body[field]) for field in _PINNED if field in body}}


def _abstain(root: Path, caller: context_views.CallerView, ref: str | None, state: str) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ref": ref, "state": state, "generation": None, "stale": False, "items": [], "continuation": None,
    }
    _pin(root, caller, response, [])
    return response


def _record(started: float, error: BaseException | None) -> None:
    """Count one brief read and time it, as series of the brief's own (spec: latency and errors of brief reads)."""
    try:
        metrics.inc_counter(READS, {"outcome": "success" if error is None else "failure"})
        metrics.observe_duration_ms(DURATION, (time.perf_counter() - started) * 1000)
        if error is not None:
            prefix = str(error).partition(":")[0]
            # A refusal opens with its upper-case code; anything else is named by its class, so the label stays bounded.
            code = prefix if re.fullmatch(r"[A-Z][A-Z_]+", prefix) else type(error).__name__
            metrics.inc_counter(FAILURES, {"code": code})
    except Exception:  # noqa: BLE001 - observability must never break a read
        pass


def read(
    root: Path, subject: str, *, continuation: str | None = None, purpose: str | None = None
) -> dict[str, Any]:
    """Serve one page of the brief of `subject`, for the bound principal under the `purpose` the request declared."""
    with state_paths.resolution_scope():
        if purpose is None:
            return read_in_scope(Path(root), subject, continuation=continuation, purpose=None)
        # The readers under the brief take the purpose from the bound principal, as `context_views.read_view` binds it.
        with principal.request_scope(principal.effective_principal().with_purpose(purpose)):
            return read_in_scope(Path(root), subject, continuation=continuation, purpose=purpose)


def read_in_scope(
    root: Path, subject: str, *, continuation: str | None = None, purpose: str | None = None
) -> dict[str, Any]:
    """`read`, inside the request's scopes; the one route that both a topic read and a brief ref take."""
    started = time.perf_counter()
    try:
        with call_spans.span("topic_brief.read"):
            response = _serve(Path(root), subject, continuation, purpose)
    except Exception as error:
        _record(started, error)
        raise
    _record(started, None)
    return response


def _serve(root: Path, subject: str, continuation: str | None, purpose: str | None) -> dict[str, Any]:
    caller = context_views.CallerView.current(root, purpose=purpose)
    with _open_graph(root, caller) as graph:
        if graph is None:
            target = context_views.parse_view_ref(subject)
            named = subject if target is not None and target.kind == context_views.VIEW_BRIEF else None
            return _abstain(root, caller, named, INDEX_WARMING)
        seed, related, lagging = _find_seed(root, graph, subject, purpose)
        if seed is None:
            if lagging:
                return _abstain(root, caller, None, INDEX_WARMING)
            raise context_views.not_found()
        if seed.id is None:
            raise ValueError("BRIEF_SEED_UNAVAILABLE: this page has no stable id to name its brief by")
        ref = context_views.BRIEF_REF_PREFIX + seed.id
        registries = _registry_digest(root)
        walk, closing = _members(root, caller, graph, seed, registries)
        try:
            compiled = _compiled(root, caller, walk, closing, registries)
        except working_set_currency.CurrencyUnavailable:
            return _abstain(root, caller, ref, UNAVAILABLE)
        # Inside the graph's snapshot: a stored member that left the brief is looked up in it, and decided for the caller.
        changed, advance = _changed_since(graph, root, _view_key(root, caller), seed, walk, compiled)
    shown = {
        "seed": {"ref": seed.ref, "path": seed.path, "title": _unlinked(seed.title)},
        # A copy: the walk is shared across reads, and a response is the caller's to change.
        "missing": [dict(entry) for entry in walk.missing],
    }
    generation = _digest([list(compiled.items), shown])
    offset = 0
    if continuation is not None:
        secret = _secret(root)
        if not secret:
            return _abstain(root, caller, ref, UNAVAILABLE)
        minted_for, offset = context_views._open(secret, continuation, ref, caller)  # noqa: SLF001 - the views' one continuation token
        if minted_for != generation:
            raise ValueError("CONTINUATION_STALE: the view changed since this continuation was issued")
    page = list(compiled.items[offset : offset + context_views.VIEW_PAGE_SIZE])
    wires = [wire for wire in compiled.wires[offset : offset + context_views.VIEW_PAGE_SIZE] if wire is not None]
    following = offset + len(page)
    token = None
    if following < len(compiled.items):
        secret = _secret(root)
        if not secret:
            return _abstain(root, caller, ref, UNAVAILABLE)
        token = context_views._mint(secret, ref, caller, generation, following)  # noqa: SLF001
    response: dict[str, Any] = {
        "ref": ref, "state": READY, "generation": generation, "stale": compiled.stale,
        "items": page, "continuation": token, **shown, "changed_since": changed,
    }
    if related is not None and continuation is None:
        # Only a topic-text read has search hits, and only its first page names them.
        in_brief = {node.path for node, _distance in walk.notes}
        response["related"] = [
            {"title": _unlinked(node.title), "ref": node.ref, "path": node.path}
            for node in related if node.id is not None and node.path not in in_brief
        ][:RELATED_CAP]
    # Only the last page owes the watermark its advance, and the terminal guard makes it once that page is served: a read
    # stopped half way, or a response the guard withholds, is read again in full.
    _pin(root, caller, response, wires, advance if token is None else None)
    return response


def _secret(root: Path) -> bytes:
    catalogue = context_catalogue.ContextCatalogue(root)
    try:
        return catalogue.secret()
    finally:
        catalogue.close()
