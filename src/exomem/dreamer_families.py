"""The dreamer's candidate families and the per-page dispatch that feeds them.

A family turns one changed page into zero or more upkeep proposals, each an
existing governed action the agent may take. Every family has three hooks:

* `on_page(ctx, rel_path)` for a changed page that still exists;
* `on_delete(ctx, rel_path)` for a page that is gone;
* `revalidate(ctx, candidate)` for an open candidate whose subject or evidence
  page just changed. It reads only that candidate's own pages.

Contributions are per page and idempotent: a page's proposals are recomputed
from that page (and bounded graph lookups) alone and replace what it proposed
before, so nothing here needs a vault-wide snapshot.

Families never write the vault, never take the writer lease, never enqueue graph
debt, never build or repair an index and never load a model. They hold no
writer handle; the route each proposal carries names an existing governed leaf
the agent calls under that leaf's own authority.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import dreamer_store, episode_capture, find_corpus
from .vocabulary_fold import fold_term

#: Every served item says this, the vocabulary advisory's exact phrase.
PERMISSION = "consideration does not authorize mutation"

#: The producer name for detections made by the dreamer itself.
PRODUCER = "dreamer"

#: A candidate is deliverable only once its evidence has been stable this long.
SETTLE_SECONDS = 3600.0

#: An undisposed item delivered this many times is held until its fingerprint
#: changes. It stays listed on explicit review.
MAX_DELIVERIES = 2

_INACTIVE_STATUSES = frozenset({"superseded", "archived", "draft", "planned", "dropped"})


class Deferred(Exception):
    """A page cannot be processed right now (a derived read is unavailable).

    Not a failure: the tick skips the page, which stays pending behind the
    pages that can run, and a later tick retries it. Raised instead of
    proposing from a partial view. `reason` is a closed code status reports.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


#: Every `Deferred.reason`: why a page is held, as status reports it.
DEFERRAL_REASONS = frozenset({"graph_unavailable", "identity_cache_cold", "identity_unavailable"})
#: The reasons that hold every page, not just the one that met them.
VAULT_WIDE_DEFERRALS = frozenset({"graph_unavailable"})


@dataclass
class Context:
    """One page's processing context: the store transaction and small memos."""

    vault_root: Path
    #: None on the request path, where a proposal is only recomputed in memory.
    store: dreamer_store.DreamerStore | None
    conn: sqlite3.Connection | None
    now: float
    _pages: dict[str, Any] = field(default_factory=dict)
    _review: dict[str, Any] = field(default_factory=dict)
    _graph: list[Any] = field(default_factory=list)
    _resolver: list[Any] = field(default_factory=list)
    _authored: dict[str, set[tuple[str, str]]] = field(default_factory=dict)
    #: One tick's memo, shared by every page it processes (the category
    #: registry is loaded once per tick). Empty on the request path.
    shared: dict[str, Any] = field(default_factory=dict)
    _members: list[sqlite3.Connection] = field(default_factory=list)

    def graph(self) -> Any:
        """One validated graph read snapshot for this page, opened once.

        Closed by `close()` at the end of the page, so no read transaction
        outlives the page it served. Raises `Deferred` when the graph is not
        available: nothing is proposed from a partial view.
        """
        if not self._graph:
            from . import epistemic_graph

            conn = epistemic_graph.EpistemicGraphIndex(self.vault_root)._open_read_snapshot()
            if conn is None:
                raise Deferred("graph_unavailable")
            self._graph.append(conn)
        return self._graph[0]

    def close(self) -> None:
        while self._graph:
            self._graph.pop().close()
        while self._members:
            self._members.pop().close()

    def members_conn(self) -> sqlite3.Connection:
        """The sidecar the page contributions live in.

        The worker's own connection, or on the request path one read-only,
        non-waiting connection opened once for this context. Raises `Deferred`
        when the sidecar cannot be read.
        """
        if self.conn is not None:
            return self.conn
        if not self._members:
            conn = dreamer_store.open_readonly(self.vault_root)
            if conn is None:
                raise Deferred("sidecar_unavailable")
            self._members.append(conn)
        return self._members[0]

    def member_sig(self, rel_path: str) -> str | None:
        """The signature a page's contribution stands on.

        The worker records the live signature of the page it processes. A
        request reads the signature the worker processed the page at (`seen`),
        so the carrier's liveness check fails once the page moves on.
        """
        if self.conn is not None:
            return _sig(self, rel_path)
        row = (
            self.members_conn().execute("SELECT sig FROM seen WHERE path=?", (rel_path,)).fetchone()
        )
        return None if row is None else str(row[0])

    def registry(self) -> Any:
        """The semantic-language registry, loaded once per tick."""
        if "registry" not in self.shared:
            from . import semantic_language_registry

            self.shared["registry"] = semantic_language_registry.load_registry(self.vault_root)
        return self.shared["registry"]

    def resolver(self) -> Any:
        """A wikilink resolver over the graph snapshot's pages, built once, no I/O.

        Resolving an authored `## Relations` target without one builds a
        whole-vault resolver: a walk that reads every page's title.
        """
        if not self._resolver:
            from . import vault as vault_module

            rows = (
                self.graph()
                .execute("SELECT path, title FROM graph_nodes WHERE kind = 'file'")
                .fetchall()
            )
            self._resolver.append(
                vault_module.WikilinkResolver.from_entries(
                    self.vault_root,
                    ((str(path), str(title) if title else None) for path, title in rows),
                )
            )
        return self._resolver[0]

    def authored(self, page: Any) -> set[tuple[str, str]]:
        """`(relation_type, target.md)` pairs `page` authors, resolved without I/O."""
        rel = str(page.rel_path)
        if rel not in self._authored:
            from . import relation_queue

            self._authored[rel] = relation_queue._authored_targets(
                page, self.vault_root, resolver=self.resolver()
            )
        return self._authored[rel]

    def page(self, rel_path: str) -> Any | None:
        """One parsed page through the shared parse cache, or None when gone.

        Lazy: a family that needs no Markdown never pays a parse.
        """
        if rel_path not in self._pages:
            path = Path(self.vault_root) / rel_path
            try:
                self._pages[rel_path] = find_corpus.CACHE.get(path, Path(self.vault_root))
            except OSError:
                self._pages[rel_path] = None
        return self._pages[rel_path]

    def review_payload(self) -> dict[str, Any] | None:
        """The review-state payload, read once per context; None when unreadable."""
        if "payload" not in self._review:
            from . import review_state

            store = review_state.ReviewStateStore(self.vault_root)
            try:
                self._review["payload"] = store.load()
            except ValueError:
                self._review["payload"] = None
            self._review["store"] = store
        return self._review["payload"]

    def review_store(self) -> Any:
        self.review_payload()
        return self._review["store"]

    def parked(self, cid: str, fingerprint: str) -> bool:
        """True when a candidate no longer competes for the family cap: it is
        decided in the review state, held after its deliveries, or the other
        direction of a link pair that is (see `pair_held`)."""
        memo = self._review.setdefault("parked", {})
        key = (cid, fingerprint)
        if key not in memo:
            held = self._held(cid, fingerprint)
            if not held and self.store is not None and self.conn is not None:
                row = self.store.candidate(self.conn, cid)
                held = row is not None and self.pair_held(row)
            memo[key] = held
        return memo[key]

    def pair_held(self, row: dict[str, Any]) -> bool:
        """True when the other direction of this link row's pair is decided or
        held, whether or not its row is still in the sidecar.

        A present twin is judged on its own `(id, fingerprint)`. An absent one
        (evicted, or never proposed from its page) is judged on every decision
        and delivery recorded for its id: its current fingerprint cannot be
        known without reading its page. Any standing decision holds the pair,
        and the twin's page is queued so that direction is proposed again and
        judged on its current fingerprint.
        """
        twin = _twin_id(row)
        if twin is None:
            return False
        present = (
            self.store.candidate(self.conn, twin)
            if self.store is not None and self.conn is not None
            else None
        )
        if present is not None and present.get("state") == "open":
            return self._held(twin, str(present["fingerprint"]))
        if not self._held_any(twin):
            return False
        self._requeue_twin(twin, str((row.get("measures") or {}).get("to") or ""))
        return True

    def _requeue_twin(self, twin: str, page: str) -> None:
        """Queue an absent twin's page once per page version.

        The page is queued again only when it changed since the last time, so a
        page that no longer proposes that direction is not reprocessed forever.
        """
        if self.store is None or self.conn is None or not page:
            return
        seen = self.store.seen_get(self.conn, page)
        memo = self.store.get_meta(self.conn, _REQUEUED_META)
        memo = dict(memo) if isinstance(memo, dict) else {}
        if twin in memo and memo[twin] == seen:
            return
        self.store.pending_add(self.conn, [page])
        memo.pop(twin, None)
        memo[twin] = seen
        while len(memo) > _REQUEUED_LIMIT:
            memo.pop(next(iter(memo)))
        self.store.set_meta(self.conn, _REQUEUED_META, memo)

    def _deliveries(self) -> dict[tuple[str, str], int]:
        if "deliveries" not in self._review:
            counts: dict[tuple[str, str], int] = {}
            if self.store is not None and self.conn is not None:
                for rid, fp, _caller, _at in self.store.deliveries(self.conn):
                    counts[(rid, fp)] = counts.get((rid, fp), 0) + 1
            self._review["deliveries"] = counts
        return self._review["deliveries"]

    def _held(self, cid: str, fingerprint: str) -> bool:
        """Decided in the review state, or delivered as often as allowed."""
        payload = self.review_payload()
        if payload is not None:
            state, _decision = self.review_store().effective_state(
                cid, fingerprint, payload=payload
            )
            if state != "open":
                return True
        return self._deliveries().get((cid, fingerprint), 0) >= MAX_DELIVERIES

    def _held_any(self, cid: str) -> bool:
        """`_held` for any fingerprint recorded against `cid`."""
        if "by_id" not in self._review:
            by_id: dict[str, set[str]] = {}
            payload = self.review_payload()
            for key in (payload or {}).get("records") or {}:
                rid, _sep, fp = str(key).partition(":")
                by_id.setdefault(rid, set()).add(fp)
            for rid, fp in self._deliveries():
                by_id.setdefault(rid, set()).add(fp)
            self._review["by_id"] = by_id
        return any(self._held(cid, fp) for fp in sorted(self._review["by_id"].get(cid, ())))


@dataclass(frozen=True)
class Family:
    name: str
    kinds: tuple[str, ...]
    on_page: Callable[[Context, str], None]
    on_delete: Callable[[Context, str], None]
    revalidate: Callable[[Context, dict[str, Any]], None]
    #: Recompute one candidate's current proposal in memory, writing nothing:
    #: the upsert arguments with its current fingerprint, or None when the
    #: proposal no longer holds. The request path revalidates through this.
    propose: Callable[[Context, dict[str, Any]], dict[str, Any] | None] | None = None
    #: Needs vault-wide counts, so stays silent until a reseed drains.
    global_counts: bool = False
    #: The row as one caller may see it: per-item egress recomputed from the
    #: members that caller may see, or None when the family's minimum does not
    #: hold on them. Families without it are served from the stored row.
    release: Callable[[Context, dict[str, Any], Any], dict[str, Any] | None] | None = None


def _status(page: Any) -> str:
    status = getattr(page, "status", None)
    return status.strip().casefold() if isinstance(status, str) else ""


def _sig(ctx: Context, rel_path: str) -> str | None:
    from . import dreamer_delta

    return dreamer_store.encode_sig(dreamer_delta.live_signature(ctx.vault_root, rel_path))


def _open_for_subject(ctx: Context, family: str, subject: str) -> set[str]:
    return {
        str(row[0])
        for row in ctx.conn.execute(
            "SELECT id FROM candidates WHERE family=? AND subject_path=? AND state='open'",
            (family, subject),
        )
    }


def _resolve_all(ctx: Context, ids: set[str]) -> None:
    for cid in sorted(ids):
        ctx.store.resolve(ctx.conn, cid, producer=PRODUCER, now=ctx.now)


# ----------------------------------------------------------------------
# link: `upkeep_link`
# ----------------------------------------------------------------------

LINK_FAMILY = "upkeep_link"
LINK_KIND = "relation.accept"

#: The structural methods upkeep proposes. `wikilink` is relation-typing debt
#: (already the default `relation_debt` family), `frontmatter_sources` is a page
#: citing its own source (provenance the author already wrote), and
#: `embedding_proximity` would mean a model encode; the per-page generator used
#: here never produces it in the first place.
LINK_METHODS = frozenset(
    {"shared_sources", "shared_open_question", "shared_resolution_target", "unit_relation_lift"}
)
_LINK_LIMIT_PER_PAGE = 10


def _authored_between(ctx: Context, page: Any, other: Any) -> bool:
    """True when either page already authors any relation to the other."""
    from . import epistemic_graph

    for source, target in ((page, other), (other, page)):
        wanted = epistemic_graph._with_md(target.rel_path)
        if any(path == wanted for _kind, path in ctx.authored(source)):
            return True
    return False


def _identity_wait(vault_root: Path) -> str:
    """Why exact relation refs are unavailable: a cold cache, or a stale one."""
    from . import semantic_contract

    if semantic_contract.current_reference_identity_snapshot(vault_root) is None:
        return "identity_cache_cold"
    return "identity_unavailable"


def _link_proposals(ctx: Context, rel_path: str) -> dict[str, dict[str, Any]]:
    """The open relation proposals whose source page is `rel_path`, by relation id."""
    from . import activation, epistemic_graph, relation_queue, review_state

    page = ctx.page(rel_path)
    if page is None or not activation._eligible(ctx.vault_root, page):
        return {}
    snapshot = ctx.graph()  # raises Deferred when the graph cannot be read
    payload = ctx.review_payload()
    if payload is None:
        payload = review_state.empty_state()
    out: dict[str, dict[str, Any]] = {}
    for candidate in relation_queue._page_candidates(
        ctx.vault_root, page, limit_per_page=_LINK_LIMIT_PER_PAGE, snapshot=snapshot
    ):
        if str(candidate.get("method") or "") not in LINK_METHODS:
            continue
        if epistemic_graph._with_md(str(candidate.get("from") or rel_path)) != rel_path:
            continue
        target_rel = epistemic_graph._with_md(str(candidate.get("to") or ""))
        target = ctx.page(target_rel)
        if target is None or _status(target) in _INACTIVE_STATUSES:
            continue
        # Only a governed page is a link target: never raw evidence, a Source,
        # an episode recap or a navigation page.
        if not activation.is_eligible_governed_page(ctx.vault_root, target):
            continue
        if _authored_between(ctx, page, target):
            continue
        refs = relation_queue._hinted_candidate_refs(ctx.vault_root, candidate, snapshot=snapshot)
        if refs is None:
            raise Deferred(_identity_wait(ctx.vault_root))
        reason, enriched = relation_queue._classify_candidate(
            ctx.vault_root,
            page,
            candidate,
            store=ctx.review_store(),
            state_payload=payload,
            authored=ctx.authored(page),
            exact_refs=refs,
        )
        if reason in {"authored_edge", "placeholder_target"} or enriched is None:
            continue
        evidence = [
            {
                "path": target_rel,
                "ref": refs[1],
                "sig": _sig(ctx, target_rel),
                "role": "target",
                "origin": "",
                "title": getattr(target, "title", None),
            }
        ]
        shared = (candidate.get("evidence") or {}).get("shared_source")
        if isinstance(shared, str) and _sig(ctx, epistemic_graph._with_md(shared)):
            shared_rel = epistemic_graph._with_md(shared)
            # A Source is raw material: its title comes from the graph, unparsed.
            node = snapshot.execute(
                "SELECT title FROM graph_nodes WHERE node_key = ? AND kind = 'file'",
                (f"file:{shared_rel}",),
            ).fetchone()
            evidence.append(
                {
                    "path": shared_rel,
                    "ref": relation_queue._fallback_ref(shared_rel),
                    "sig": _sig(ctx, shared_rel),
                    "role": "shared_source",
                    "origin": "",
                    "title": node[0] if node is not None else None,
                }
            )
        out[str(enriched["review_id"])] = {
            "candidate": candidate,
            "enriched": enriched,
            "refs": refs,
            "evidence": evidence,
            "signal_version": relation_queue._evidence_signal_version(page, candidate),
        }
    return out


def _link_kwargs(
    ctx: Context, rel_path: str, review_id: str, proposal: dict[str, Any]
) -> dict[str, Any]:
    """One link proposal as the store's upsert arguments."""
    from . import epistemic_graph

    candidate = proposal["candidate"]
    enriched = proposal["enriched"]
    page = ctx.page(rel_path)
    subject_evidence = {
        "path": rel_path,
        "ref": proposal["refs"][0],
        "sig": _sig(ctx, rel_path),
        "role": "source",
        "origin": "",
        "title": getattr(page, "title", None),
    }
    return {
        "family": LINK_FAMILY,
        "kind": LINK_KIND,
        "subject_path": rel_path,
        "subject_ref": proposal["refs"][0],
        "proposal_key": review_id,
        "evidence": [subject_evidence, *proposal["evidence"]],
        "route": {
            "tool": "connect_memory",
            "args": {
                "operation": "accept-relation",
                "ref": enriched["ref"],
                "path": rel_path,
                "expected_fingerprint": enriched["fingerprint"],
            },
        },
        "reason_code": str(candidate.get("method") or ""),
        "signal_version": proposal["signal_version"],
        "measures": {
            "relation_type": str(candidate.get("relation_type") or ""),
            "method": str(candidate.get("method") or ""),
            "to": epistemic_graph._with_md(str(candidate.get("to") or "")),
        },
        "identity": review_id,
        "ref": enriched["ref"],
        "fingerprint": enriched["fingerprint"],
    }


def _link_on_page(ctx: Context, rel_path: str) -> None:
    proposals = _link_proposals(ctx, rel_path)
    _resolve_all(ctx, _open_for_subject(ctx, LINK_FAMILY, rel_path) - set(proposals))
    for review_id, proposal in sorted(proposals.items()):
        ctx.store.upsert_proposal(
            ctx.conn,
            producer=PRODUCER,
            now=ctx.now,
            parked=ctx.parked,
            **_link_kwargs(ctx, rel_path, review_id, proposal),
        )


def _link_propose(ctx: Context, row: dict[str, Any]) -> dict[str, Any] | None:
    subject = str(row.get("subject_path") or "")
    if _sig(ctx, subject) is None:
        return None
    proposal = _link_proposals(ctx, subject).get(str(row.get("id") or ""))
    if proposal is None:
        return None
    return _link_kwargs(ctx, subject, str(row["id"]), proposal)


def _link_on_delete(ctx: Context, rel_path: str) -> None:
    _resolve_all(ctx, _open_for_subject(ctx, LINK_FAMILY, rel_path))


def _link_revalidate(ctx: Context, row: dict[str, Any]) -> None:
    subject = str(row.get("subject_path") or "")
    if _sig(ctx, subject) is None:
        _link_on_delete(ctx, subject)
        return
    _link_on_page(ctx, subject)


LINK = Family(
    name=LINK_FAMILY,
    kinds=(LINK_KIND,),
    on_page=_link_on_page,
    on_delete=_link_on_delete,
    revalidate=_link_revalidate,
    propose=_link_propose,
)


# ----------------------------------------------------------------------
# hydration: `upkeep_hydration`
# ----------------------------------------------------------------------

HYDRATION_FAMILY = "upkeep_hydration"
HYDRATION_KIND = "curation.hydrate"

#: The one origin every contributor that declares no Source shares.
_UNSOURCED_ORIGIN = "unsourced"
#: Independent origins a hydration proposal needs.
HYDRATION_MIN_ORIGINS = 2

#: Entities examined per changed page, and contributing rows per entity.
_HYDRATION_ENTITIES_PER_PAGE = 8
_HYDRATION_ROW_LIMIT = 64

#: Contributing pages named in the route (the entity is the eighth path).
_HYDRATION_ROUTE_PAGES = 7

#: Unit refs folded into the signal per contributing page.
_HYDRATION_UNITS_PER_PAGE = 8

#: Compiled page types whose units count as facts about an entity. Sources and
#: Evidence are compile material, not hydration.
_HYDRATION_TYPES = (
    "research-note",
    "insight",
    "pattern",
    "failure",
    "experiment",
    "production-log",
)

_ENTITY_TARGETS_SQL = (
    "SELECT DISTINCT d.path FROM graph_edges e JOIN graph_nodes d "
    "ON d.node_key = e.dst_key AND d.kind = 'file' "
    "WHERE e.source_path = ? AND d.page_type = 'entity' AND d.path <> ? "
    "AND COALESCE(e.relation_type, '') <> 'derived_from' "
    "ORDER BY d.path LIMIT ?"
)

#: A graph file node `f` dated after the bound date, as `_date` reads its
#: dates. In SQL, so a row limit applies to qualifying rows only: a page that
#: many older pages link is still reached by the newer ones.
_NEWER_THAN = (
    "substr(COALESCE(NULLIF(f.updated_date, ''), NULLIF(f.origin_date, ''), ''), 1, 10) > ?"
)

_CONTRIBUTORS_SQL = (
    "SELECT DISTINCT e.source_path, e.src_key, f.updated_date, f.origin_date, f.exomem_id, "
    "f.title "
    "FROM graph_edges e JOIN graph_nodes f "
    "ON f.node_key = ('file:' || e.source_path) AND f.kind = 'file' "
    "WHERE e.dst_key = ? AND e.source_path <> ? "
    "AND COALESCE(e.relation_type, '') <> 'derived_from' "
    f"AND {_NEWER_THAN} "
    f"AND f.page_type IN ({','.join('?' for _ in _HYDRATION_TYPES)}) "
    f"AND COALESCE(f.lifecycle_status, '') NOT IN "
    f"({','.join('?' for _ in _INACTIVE_STATUSES)}) "
    "AND NOT EXISTS (SELECT 1 FROM graph_edges b WHERE b.source_path = ? "
    "AND b.dst_key = ('file:' || e.source_path)) "
    "ORDER BY e.source_path, e.src_key LIMIT ?"
)


def _memory_or_path_ref(rel_path: str, exomem_id: str | None) -> str:
    from . import memory_refs, relation_queue

    if exomem_id:
        try:
            return memory_refs.memory_ref(exomem_id)
        except ValueError:
            pass
    return relation_queue._fallback_ref(rel_path)


def _date(updated: Any, origin: Any) -> str:
    value = updated or origin
    return str(value)[:10] if value else ""


def _hydration_entities(ctx: Context, rel_path: str) -> list[str]:
    """The entity pages this page is about: itself, and what it links (at most 8)."""
    graph = ctx.graph()
    own = graph.execute(
        "SELECT page_type FROM graph_nodes WHERE node_key = ? AND kind = 'file'",
        (f"file:{rel_path}",),
    ).fetchone()
    linked = [
        str(row[0])
        for row in graph.execute(
            _ENTITY_TARGETS_SQL, (rel_path, rel_path, _HYDRATION_ENTITIES_PER_PAGE)
        )
    ]
    entities = [rel_path] if own is not None and own[0] == "entity" else []
    return [*entities, *(path for path in linked if path not in entities)]


def _hydration_detect(ctx: Context, entity: str) -> dict[str, Any] | None:
    """The hydration proposal for one entity, from the graph snapshot alone.

    Newer facts about the entity live on compiled pages that link it, and the
    entity's own page does not link or cite those pages back. Reads no
    Markdown: every input is a row of the published graph sidecar.
    """
    from . import provenance

    graph = ctx.graph()
    node = graph.execute(
        "SELECT page_type, lifecycle_status, updated_date, origin_date, exomem_id, title "
        "FROM graph_nodes WHERE node_key = ? AND kind = 'file'",
        (f"file:{entity}",),
    ).fetchone()
    if node is None or node[0] != "entity":
        return None
    if str(node[1] or "").strip().casefold() in _INACTIVE_STATUSES:
        return None
    entity_date = _date(node[2], node[3])
    rows = graph.execute(
        _CONTRIBUTORS_SQL,
        (
            f"file:{entity}",
            entity,
            entity_date,
            *_HYDRATION_TYPES,
            *sorted(_INACTIVE_STATUSES),
            entity,
            _HYDRATION_ROW_LIMIT,
        ),
    ).fetchall()
    contributors: dict[str, dict[str, Any]] = {}
    for path, src_key, _updated, _origin, exomem_id, title in rows:
        path = str(path)
        entry = contributors.setdefault(
            path,
            {"exomem_id": exomem_id, "title": title, "units": set(), "page_level": False},
        )
        if str(src_key) == f"file:{path}":
            entry["page_level"] = True
        else:
            unit = graph.execute(
                "SELECT unit_ref FROM graph_nodes WHERE node_key = ? AND unit_ref IS NOT NULL",
                (str(src_key),),
            ).fetchone()
            if unit is not None:
                entry["units"].add(str(unit[0]))
    sources: dict[str, set[str]] = {}
    for path, entry in list(contributors.items()):
        if entry["page_level"]:
            entry["units"].update(
                str(row[0])
                for row in graph.execute(
                    "SELECT unit_ref FROM graph_nodes WHERE path = ? "
                    "AND unit_ref IS NOT NULL ORDER BY unit_ref LIMIT ?",
                    (path, _HYDRATION_UNITS_PER_PAGE),
                )
            )
        if not entry["units"]:
            contributors.pop(path)
            continue
        sources[path] = {
            str(row[0])
            for row in graph.execute(
                "SELECT dst_key FROM graph_edges WHERE src_key = ? "
                "AND origin = 'frontmatter' AND source_anchor = 'sources' "
                "AND relation_type = 'derived_from'",
                (f"file:{path}",),
            )
        }
    if not contributors:
        return None
    # Pages that declare no Source count as ONE origin between them. The graph
    # carries no session key to tell their conversations apart, so the
    # conservative reading is a single conversation-only fan-out.
    unsourced = {path: _UNSOURCED_ORIGIN for path, declared in sources.items() if not declared}
    origins = provenance.origin_keys(sources, fallback=unsourced)
    if len(set(origins.values())) < HYDRATION_MIN_ORIGINS:
        return None
    pairs = sorted(
        (origins[path], unit)
        for path, entry in contributors.items()
        for unit in sorted(entry["units"])[:_HYDRATION_UNITS_PER_PAGE]
    )
    return {
        "entity_ref": _memory_or_path_ref(entity, node[4]),
        "entity_title": node[5],
        "contributors": contributors,
        "origins": origins,
        "signal_version": review_state_digest(pairs),
    }


def review_state_digest(value: Any) -> str:
    import hashlib
    import json

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _hydration_kwargs(ctx: Context, entity: str) -> dict[str, Any] | None:
    """One entity's hydration proposal as the store's upsert arguments."""
    proposal = _hydration_detect(ctx, entity) if _sig(ctx, entity) else None
    if proposal is None:
        return None
    contributors = proposal["contributors"]
    origins = proposal["origins"]
    ordered = sorted(contributors, key=lambda path: (origins[path], path))
    evidence = [
        {
            "path": path,
            "ref": _memory_or_path_ref(path, contributors[path]["exomem_id"]),
            "sig": _sig(ctx, path),
            "role": "contributor",
            "origin": origins[path],
            "title": contributors[path]["title"],
        }
        # One slot is the entity itself, so the whole list fits the cap.
        for path in ordered[: dreamer_store.EVIDENCE_CAP - 1]
    ]
    return {
        "family": HYDRATION_FAMILY,
        "kind": HYDRATION_KIND,
        "subject_path": entity,
        "subject_ref": proposal["entity_ref"],
        "proposal_key": "",
        "evidence": [
            {
                "path": entity,
                "ref": proposal["entity_ref"],
                "sig": _sig(ctx, entity),
                "role": "subject",
                "origin": "",
                "title": proposal["entity_title"],
            },
            *evidence,
        ],
        "evidence_count": len(contributors) + 1,
        "route": {
            "tool": "maintain_memory",
            "args": {
                "mode": "curation",
                "curation_action": "work-item",
                "paths": [entity, *ordered[:_HYDRATION_ROUTE_PAGES]],
            },
        },
        "reason_code": "newer_linked_facts",
        "signal_version": proposal["signal_version"],
        "measures": {
            "origins": len(set(origins.values())),
            "contributors": len(contributors),
            "units": sum(len(entry["units"]) for entry in contributors.values()),
        },
    }


def _hydration_refresh(ctx: Context, entity: str) -> None:
    """Recompute one entity's proposal and write or resolve it."""
    kwargs = _hydration_kwargs(ctx, entity)
    if kwargs is None:
        ctx.store.resolve(
            ctx.conn,
            dreamer_store.candidate_id(HYDRATION_KIND, entity, ""),
            producer=PRODUCER,
            now=ctx.now,
        )
        return
    ctx.store.upsert_proposal(ctx.conn, producer=PRODUCER, now=ctx.now, parked=ctx.parked, **kwargs)


def _hydration_propose(ctx: Context, row: dict[str, Any]) -> dict[str, Any] | None:
    kwargs = _hydration_kwargs(ctx, str(row.get("subject_path") or ""))
    if kwargs is None:
        return None
    kwargs["fingerprint"] = dreamer_store.proposal_fingerprint(
        family=kwargs["family"],
        subject_ref=kwargs["subject_ref"],
        signal_version=kwargs["signal_version"],
        evidence=kwargs["evidence"],
    )
    return kwargs


def _hydration_on_page(ctx: Context, rel_path: str) -> None:
    for entity in _hydration_entities(ctx, rel_path):
        _hydration_refresh(ctx, entity)


def _hydration_on_delete(ctx: Context, rel_path: str) -> None:
    ctx.store.resolve(
        ctx.conn,
        dreamer_store.candidate_id(HYDRATION_KIND, rel_path, ""),
        producer=PRODUCER,
        now=ctx.now,
    )


def _hydration_revalidate(ctx: Context, row: dict[str, Any]) -> None:
    _hydration_refresh(ctx, str(row.get("subject_path") or ""))


HYDRATION = Family(
    name=HYDRATION_FAMILY,
    kinds=(HYDRATION_KIND,),
    on_page=_hydration_on_page,
    on_delete=_hydration_on_delete,
    revalidate=_hydration_revalidate,
    propose=_hydration_propose,
)


# ----------------------------------------------------------------------
# shared by the global families: alias/anchor and convention/category
# ----------------------------------------------------------------------

#: Names, link targets, tags or labels read from one page.
PER_PAGE_TERMS = 16

#: Members named in one item's evidence besides its subject.
_OTHER_MEMBERS = dreamer_store.EVIDENCE_CAP - 1


def _member_entry(ctx: Context, rel_path: str, role: str, **extra: Any) -> dict[str, Any]:
    """One evidence entry: the page's ref and title through the parse cache."""
    page = ctx.page(rel_path)
    frontmatter = page.frontmatter if page is not None else {}
    exomem_id = frontmatter.get("exomem_id") if isinstance(frontmatter, dict) else None
    return {
        "path": rel_path,
        "ref": _memory_or_path_ref(rel_path, exomem_id if isinstance(exomem_id, str) else None),
        "sig": ctx.member_sig(rel_path),
        "role": role,
        "origin": "",
        "title": getattr(page, "title", None),
        **extra,
    }


def _governed(ctx: Context, rel_path: str) -> Any | None:
    """The parsed page when it is an active governed page, else None."""
    from . import activation

    page = ctx.page(rel_path)
    if page is None or not activation.is_eligible_governed_page(ctx.vault_root, page):
        return None
    return page


def _pages_in(rows: list[tuple[str, ...]]) -> dict[str, list[Any]]:
    """`(path, *columns)` rows grouped by page, in path order.

    One column comes back as its value, several as a tuple.
    """
    pages: dict[str, list[Any]] = {}
    for path, *values in rows:
        pages.setdefault(path, []).append(values[0] if len(values) == 1 else tuple(values))
    return pages


def _released(view: dict[str, Any] | None, row: dict[str, Any]) -> dict[str, Any] | None:
    """A stored row replaced by one caller's view of it, with the served fingerprint."""
    if view is None:
        return None
    released = {**row, **view}
    released["evidence"] = sorted(view["evidence"], key=lambda item: str(item.get("path") or ""))
    released["fingerprint"] = view.get("fingerprint") or dreamer_store.proposal_fingerprint(
        family=view["family"],
        subject_ref=view["subject_ref"],
        signal_version=view["signal_version"],
        evidence=released["evidence"],
    )
    return released


# ----------------------------------------------------------------------
# alias/anchor: `upkeep_alias`
# ----------------------------------------------------------------------

ALIAS_FAMILY = "upkeep_alias"
ALIAS_KIND = "anchor.alias"


def alias_id(key: str) -> str:
    """An alias proposal's identity: its fold key alone.

    The page it is served on is chosen per caller, as the one page carrying
    the name that caller may see, so no path is part of the identity.
    """
    return dreamer_store.candidate_id(ALIAS_KIND, "", key)


def _page_names(rel_path: str, page: Any) -> list[tuple[str, str, str]]:
    """`(fold key, source, spelling)` for a page's names, at most 16.

    Its title, its file stem, its frontmatter `aliases` and its accepted
    `learned_aliases`. The spelling is casefolded; a learned name is kept in
    the activation index's own normal form, which is casefolded too.
    """
    from . import activation_conventions, working_set_index

    frontmatter = page.frontmatter if isinstance(page.frontmatter, dict) else {}
    learned, _rejected = working_set_index.learned_alias_verdicts(
        frontmatter.get(working_set_index.LEARNED_ALIASES_FIELD),
        filler=activation_conventions.shipped_conventions().conventions.referential_filler,
    )
    spellings = [
        ("title", str(frontmatter.get("title") or page.title or "").strip()),
        ("stem", Path(rel_path).stem),
        *(("alias", alias) for alias in working_set_index._strings(frontmatter.get("aliases"))),
        *(("learned", name) for name in learned),
    ]
    out: list[tuple[str, str, str]] = []
    for source, spelling in spellings:
        key = fold_term(spelling) if spelling else ""
        folded = spelling if source == "learned" else spelling.casefold()
        if key and (key, source, folded) not in out:
            out.append((key, source, folded))
        if len(out) >= PER_PAGE_TERMS:
            break
    return out


def _page_refs(ctx: Context, rel_path: str) -> list[tuple[str, str, str]]:
    """`(fold key, spelling, casefolded spelling)` for a page's bare link targets, at most 16.

    Read from the published graph's authored dependencies. A path-shaped
    target is not a name: an alias can only answer a bare one. Whether a bare
    target resolves depends on who is asking (it resolves by title or stem to
    a page that then carries the same fold key), so every one is a member.
    """
    out: list[tuple[str, str, str]] = []
    for (raw,) in ctx.graph().execute(
        "SELECT DISTINCT raw_target FROM graph_dependencies WHERE source_path = ? "
        "ORDER BY raw_target",
        (rel_path,),
    ):
        spelling = str(raw).split("|", 1)[0].split("#", 1)[0].strip()
        spelling = spelling.removesuffix(".md").strip()
        if not spelling or "/" in spelling or any(spelling == seen for _k, seen, _c in out):
            continue
        key = fold_term(spelling)
        if key:
            out.append((key, spelling, spelling.casefold()))
        if len(out) >= PER_PAGE_TERMS:
            break
    return out


def _alias_view(
    ctx: Context, key: str, *, keep: Callable[[str], bool] | None
) -> dict[str, Any] | None:
    """The alias proposal on one fold key as a caller whose predicate is `keep` sees it.

    Served when exactly one page the caller may see carries the name, it is an
    active governed page, another page the caller may see links the name by a
    spelling that page does not resolve, and the key is within the member
    bound among the pages the caller may see. Every input is a released row.
    """
    from . import working_set_index

    conn = ctx.members_conn()
    names = _pages_in(dreamer_store.DreamerStore.members(conn, "name_keys", key, keep=keep))
    refs = _pages_in(dreamer_store.DreamerStore.members(conn, "name_refs", key, keep=keep))
    if len(set(names) | set(refs)) > dreamer_store.MEMBER_BOUND or len(names) != 1:
        return None
    subject = next(iter(names))
    if ctx.member_sig(subject) is None or _governed(ctx, subject) is None:
        return None
    resolving = {spelling for source, spelling in names[subject] if source != "learned"}
    learned = {spelling for source, spelling in names[subject] if source == "learned"}
    referrers = {
        path: sorted({raw for raw, folded in rows if folded not in resolving})
        for path, rows in refs.items()
        if path != subject
    }
    referrers = {path: raws for path, raws in referrers.items() if raws}
    if not referrers:
        return None
    uses: dict[str, int] = {}
    for raws in referrers.values():
        for raw in raws:
            uses[raw] = uses.get(raw, 0) + 1
    spelling = min(uses, key=lambda raw: (-uses[raw], raw))
    reason = (
        "learned_alias_referenced"
        if working_set_index.normalize(spelling) in learned
        else "variant_reference"
    )
    subject_entry = _member_entry(ctx, subject, "subject")
    return {
        "family": ALIAS_FAMILY,
        "kind": ALIAS_KIND,
        "subject_path": subject,
        "subject_ref": subject_entry["ref"],
        "proposal_key": key,
        "evidence": [
            subject_entry,
            *(
                _member_entry(ctx, path, "referrer", spelling=raws[0])
                for path, raws in sorted(referrers.items())[:_OTHER_MEMBERS]
            ),
        ],
        "evidence_count": len(referrers) + 1,
        "route": {
            "tool": "edit_memory",
            "args": {
                "path": subject,
                "operation": {"kind": "patch_frontmatter", "field": "aliases"},
            },
        },
        "reason_code": reason,
        "signal_version": review_state_digest(
            [key, reason, spelling, sorted((p, r) for p, raws in referrers.items() for r in raws)]
        ),
        "measures": {"fold_key": key, "spelling": spelling, "referrers": len(referrers)},
    }


#: A link spelling that fails to resolve to at least one page carrying the
#: name: for a caller who sees only that page and the link, it is a variant.
#: Two counts per spelling, whatever the key's size.
_VARIANTS_SQL = (
    "SELECT DISTINCT r.raw_cf FROM name_refs r WHERE r.fold_key = ?1 AND ("
    "SELECT count(DISTINCT n.path) FROM name_keys n WHERE n.fold_key = ?1 "
    "AND n.source <> 'learned' AND n.spelling = r.raw_cf) < ?2 ORDER BY r.raw_cf LIMIT ?3"
)


def _alias_stored(ctx: Context, key: str) -> dict[str, Any] | None:
    """The stored row for one fold key: the owner's view when it is served,
    else the superset row, which exists whenever some caller could be served.

    That is: some page carries the name, and some link spelling fails to
    resolve to at least one of those pages. The superset row is bookkeeping:
    every served field is recomputed per caller (`_alias_release`).
    """
    conn = ctx.members_conn()
    carriers = int(
        conn.execute(
            "SELECT count(DISTINCT path) FROM name_keys WHERE fold_key = ?", (key,)
        ).fetchone()[0]
    )
    if not carriers:
        return None
    variants = [str(row[0]) for row in conn.execute(_VARIANTS_SQL, (key, carriers, PER_PAGE_TERMS))]
    if not variants:
        return None
    owner = _alias_view(ctx, key, keep=None)
    if owner is not None:
        return owner
    subject = str(
        conn.execute(
            "SELECT path FROM name_keys WHERE fold_key = ? ORDER BY path LIMIT 1", (key,)
        ).fetchone()[0]
    )
    marks = ",".join("?" for _ in variants)
    referrers = _pages_in(
        [
            (str(path), str(raw))
            for path, raw in conn.execute(
                f"SELECT path, raw FROM name_refs WHERE fold_key = ? AND raw_cf IN ({marks}) "
                "ORDER BY path, raw LIMIT ?",
                (key, *variants, 4 * _OTHER_MEMBERS),
            )
        ]
    )
    subject_entry = _member_entry(ctx, subject, "subject")
    return {
        "family": ALIAS_FAMILY,
        "kind": ALIAS_KIND,
        "subject_path": subject,
        "subject_ref": subject_entry["ref"],
        "proposal_key": key,
        "evidence": [
            subject_entry,
            *(
                _member_entry(ctx, path, "referrer", spelling=raws[0])
                for path, raws in list(referrers.items())[:_OTHER_MEMBERS]
                if path != subject
            ),
        ],
        "evidence_count": carriers + len(referrers),
        "route": {
            "tool": "edit_memory",
            "args": {
                "path": subject,
                "operation": {"kind": "patch_frontmatter", "field": "aliases"},
            },
        },
        "reason_code": "variant_reference",
        "signal_version": review_state_digest([key, "superset", carriers, variants]),
        "measures": {"fold_key": key, "spelling": variants[0], "referrers": len(referrers)},
    }


def _alias_refresh_key(ctx: Context, key: str) -> None:
    """Recompute the one proposal on a fold key, and its ambiguity mark."""
    store, conn = ctx.store, ctx.conn
    kwargs = _alias_stored(ctx, key)
    if kwargs is None:
        store.resolve(conn, alias_id(key), producer=PRODUCER, now=ctx.now)
    else:
        store.upsert_proposal(
            conn,
            producer=PRODUCER,
            now=ctx.now,
            parked=ctx.parked,
            identity=alias_id(key),
            **kwargs,
        )
    # Two pages carry the name and a page links it: for a caller who sees
    # them, the link is an ambiguity owned by the audit's link categories.
    # Which category, and whether any, is judged per request (`ambiguity`).
    store.clear_integrity_key(conn, key)
    linked = conn.execute("SELECT 1 FROM name_refs WHERE fold_key=? LIMIT 1", (key,)).fetchone()
    shared = conn.execute(
        "SELECT count(DISTINCT path) >= 2 FROM name_keys WHERE fold_key=?", (key,)
    ).fetchone()[0]
    if linked is not None and shared:
        store.note_ambiguity(conn, key, ctx.now)


def ambiguity(ctx: Context, key: str, keep) -> str | None:
    """The audit category a caller would see for one fold key's links, or None.

    Two or more pages the caller may see carry the name, and a page the
    caller may see links it by a spelling that resolves to none of them
    (`forward_reference`) or to more than one (`broken_wikilink`). Reads the
    released rows of that key only, within the member bound items keep, and
    takes titles from the name rows: no page is parsed and a withheld page
    never counts.
    """
    from . import vault as vault_module

    conn = ctx.members_conn()
    names = _pages_in(dreamer_store.DreamerStore.members(conn, "name_keys", key, keep=keep))
    if len(names) < 2:
        return None
    refs = dreamer_store.DreamerStore.members(conn, "name_refs", key, keep=keep)
    if not refs or len(set(names) | {row[0] for row in refs}) > dreamer_store.MEMBER_BOUND:
        return None
    titles = [
        (path, next((spelling for source, spelling in rows if source == "title"), None))
        for path, rows in names.items()
    ]
    resolver = vault_module.WikilinkResolver.from_entries(ctx.vault_root, titles)
    category = None
    for raw in sorted({raw for _path, raw, _folded in refs}):
        _canonical, warning = vault_module.normalize_wikilink(
            raw, ctx.vault_root, resolver=resolver, strict=False, visible=keep
        )
        if warning is None:
            continue
        if "does not resolve" not in warning:
            return "broken_wikilink"
        category = "forward_reference"
    return category


def _alias_contribute(
    ctx: Context,
    rel_path: str,
    names: list[tuple[str, str, str]],
    refs: list[tuple[str, str, str]],
) -> None:
    store, conn = ctx.store, ctx.conn
    keys = store.contribution_keys(conn, "name_keys", rel_path)
    keys |= store.contribution_keys(conn, "name_refs", rel_path)
    store.replace_contributions(conn, "name_keys", rel_path, names)
    store.replace_contributions(conn, "name_refs", rel_path, refs)
    keys |= {row[0] for row in names} | {row[0] for row in refs}
    for key in sorted(keys):
        _alias_refresh_key(ctx, key)


def _alias_on_page(ctx: Context, rel_path: str) -> None:
    page = ctx.page(rel_path)
    if page is None:
        _alias_contribute(ctx, rel_path, [], [])
        return
    refs = _page_refs(ctx, rel_path)  # raises Deferred when the graph cannot be read
    _alias_contribute(ctx, rel_path, _page_names(rel_path, page), refs)


def _alias_on_delete(ctx: Context, rel_path: str) -> None:
    _alias_contribute(ctx, rel_path, [], [])


def _alias_revalidate(ctx: Context, row: dict[str, Any]) -> None:
    _alias_refresh_key(ctx, str(row.get("proposal_key") or ""))


def _alias_propose(ctx: Context, row: dict[str, Any]) -> dict[str, Any] | None:
    return _alias_stored(ctx, str(row.get("proposal_key") or ""))


def _alias_release(ctx: Context, row: dict[str, Any], keep) -> dict[str, Any] | None:
    return _released(_alias_view(ctx, str(row.get("proposal_key") or ""), keep=keep), row)


ALIAS = Family(
    name=ALIAS_FAMILY,
    kinds=(ALIAS_KIND,),
    on_page=_alias_on_page,
    on_delete=_alias_on_delete,
    revalidate=_alias_revalidate,
    propose=_alias_propose,
    global_counts=True,
    release=_alias_release,
)


# ----------------------------------------------------------------------
# convention/category: `upkeep_convention`
# ----------------------------------------------------------------------

CONVENTION_FAMILY = "upkeep_convention"
TAG_KIND = "convention.tag"
CATEGORY_KIND = "convention.category"

#: The registry content hash the stored category rows were checked against.
_REGISTRY_META = "category_registry"

#: Set when a global family skipped a page at the size cap; cleared by the
#: reseed that makes the skipped pages good again.
CAPACITY_BEHIND_META = "capacity_behind"


def _page_tags(page: Any) -> list[tuple[str, str]]:
    """`(fold key, spelling)` for a page's raw frontmatter tags, at most 16."""
    value = page.frontmatter.get("tags") if isinstance(page.frontmatter, dict) else None
    raws = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    out: list[tuple[str, str]] = []
    for raw in raws:
        spelling = raw.strip() if isinstance(raw, str) else ""
        key = fold_term(spelling) if spelling else ""
        if key and (key, spelling) not in out:
            out.append((key, spelling))
        if len(out) >= PER_PAGE_TERMS:
            break
    return out


def _spelling_counts(counts: dict[str, int]) -> str:
    ordered = sorted(counts, key=lambda raw: (-counts[raw], raw))
    return ", ".join(
        f'"{raw}" on {counts[raw]} note{"" if counts[raw] == 1 else "s"}' for raw in ordered
    )


def _tag_view(
    ctx: Context, key: str, *, keep: Callable[[str], bool] | None, strict: bool
) -> dict[str, Any] | None:
    """One tag cluster over the members `keep` admits (None: all of them).

    Strict is a caller's view: two or more spellings are released, the served
    subject's spelling is strictly outnumbered, and the cluster is within the
    member bound. The server never declares which spelling is the vault's.
    """
    pages = _pages_in(
        dreamer_store.DreamerStore.members(ctx.members_conn(), "term_uses", key, keep=keep)
    )
    if strict and len(pages) > dreamer_store.MEMBER_BOUND:
        return None
    counts: dict[str, int] = {}
    for raws in pages.values():
        for raw in set(raws):
            counts[raw] = counts.get(raw, 0) + 1
    if len(counts) < 2:
        return None
    minority = min(counts, key=lambda raw: (counts[raw], raw))
    if strict and max(counts.values()) <= counts[minority]:
        return None
    subject = next(path for path, raws in pages.items() if minority in raws)
    others = [
        (path, next(raw for raw in raws if raw != minority))
        for path, raws in pages.items()
        if path != subject and any(raw != minority for raw in raws)
    ]
    if not others:
        return None
    subject_entry = _member_entry(ctx, subject, "subject", spelling=minority)
    return {
        "family": CONVENTION_FAMILY,
        "kind": TAG_KIND,
        "subject_path": subject,
        "subject_ref": subject_entry["ref"],
        "proposal_key": key,
        "evidence": [
            subject_entry,
            *(
                _member_entry(ctx, path, "member", spelling=raw)
                for path, raw in others[:_OTHER_MEMBERS]
            ),
        ],
        "evidence_count": len(others) + 1,
        "route": {
            "tool": "edit_memory",
            "args": {"path": subject, "operation": {"kind": "replace_tags"}},
        },
        "reason_code": "tag_spelling_variant",
        "signal_version": review_state_digest(
            [key, sorted((path, raw) for path, raws in pages.items() for raw in raws)]
        ),
        "measures": {
            "fold_key": key,
            "spelling": minority,
            "counts": dict(sorted(counts.items())),
            "why": _spelling_counts(counts),
        },
    }


def _tag_kwargs(ctx: Context, key: str) -> dict[str, Any] | None:
    """The stored superset row for one tag fold key, or None.

    It exists whenever two spellings are carried at all (two index probes),
    whatever any one caller may see; the stored `subject_path` is the first
    member by path and is bookkeeping only.
    """
    conn = ctx.members_conn()
    span = dreamer_store.DreamerStore.spelling_span(conn, key)
    if span is None or span[0] == span[1]:
        return None
    view = _tag_view(ctx, key, keep=None, strict=False)
    if view is None:
        # Past the member bound one spelling can fill every page read; the
        # row still exists, since a caller who sees fewer pages may see both.
        firsts = dreamer_store.DreamerStore.spelling_firsts(conn, key)
        view = _tag_fallback(ctx, key, firsts)
    first = dreamer_store.DreamerStore.first_member(conn, "term_uses", key)
    return {**view, "subject_path": first or view["subject_path"]}


def _tag_fallback(ctx: Context, key: str, firsts: list[tuple[str, str]]) -> dict[str, Any]:
    """A stored cluster row from the first page of two spellings (two index seeks)."""
    (subject, minority), *others = firsts
    subject_entry = _member_entry(ctx, subject, "subject", spelling=minority)
    return {
        "family": CONVENTION_FAMILY,
        "kind": TAG_KIND,
        "subject_path": subject,
        "subject_ref": subject_entry["ref"],
        "proposal_key": key,
        "evidence": [
            subject_entry,
            *(_member_entry(ctx, path, "member", spelling=raw) for path, raw in others),
        ],
        "evidence_count": len(firsts),
        "route": {
            "tool": "edit_memory",
            "args": {"path": subject, "operation": {"kind": "replace_tags"}},
        },
        "reason_code": "tag_spelling_variant",
        "signal_version": review_state_digest([key, "past-bound", firsts]),
        "measures": {"fold_key": key, "spelling": minority, "counts": {}, "why": ""},
    }


def _tag_refresh(ctx: Context, key: str) -> None:
    cid = dreamer_store.candidate_id(TAG_KIND, "", key)
    kwargs = _tag_kwargs(ctx, key)
    if kwargs is None:
        ctx.store.resolve(ctx.conn, cid, producer=PRODUCER, now=ctx.now)
        return
    ctx.store.upsert_proposal(
        ctx.conn, producer=PRODUCER, now=ctx.now, parked=ctx.parked, identity=cid, **kwargs
    )


def _registry_folds(registry: Any) -> dict[str, frozenset[str]]:
    """Fold key -> the registered category keys a label of that fold names."""
    memo = _FOLD_MEMO.get(registry.content_hash)
    if memo is not None and memo[0] is registry:
        return memo[1]
    folds: dict[str, set[str]] = {}
    for labels in (
        {key: key for key in registry.core_categories},
        registry.core_category_aliases,
        {key: key for key in registry.categories},
        registry.category_aliases,
    ):
        for label, canonical in labels.items():
            folded = fold_term(label)
            if folded:
                folds.setdefault(folded, set()).add(str(canonical))
    frozen = {key: frozenset(values) for key, values in folds.items()}
    _FOLD_MEMO.clear()
    _FOLD_MEMO[registry.content_hash] = (registry, frozen)
    return frozen


_FOLD_MEMO: dict[str, tuple[Any, dict[str, frozenset[str]]]] = {}


def _in_scope(registry: Any, key: str, projects: tuple[str, ...], page_type: str | None) -> bool:
    """Whether a registered category may be used on a page with these projects.

    The unit parser's own rule: in scope for any one of the page's projects.
    A core category has no scope.
    """
    from . import semantic_language_registry

    definition = registry.categories.get(key)
    if definition is None:
        return key in registry.core_categories
    return any(
        not semantic_language_registry._scope_findings(
            "categories", key, definition, project=project, page_type=page_type
        )
        for project in projects or (None,)
    )


def _page_labels(ctx: Context, rel_path: str) -> list[str]:
    """A page's semantic-unit category labels as authored, at most 16, from the graph."""
    import json

    labels: list[str] = []
    for (metadata,) in ctx.graph().execute(
        "SELECT metadata FROM graph_nodes WHERE path = ? AND unit_ref IS NOT NULL "
        "ORDER BY line_start, node_key",
        (rel_path,),
    ):
        try:
            label = (json.loads(metadata) or {}).get("category_raw")
        except (TypeError, ValueError):
            continue
        if isinstance(label, str) and label.strip() and label.strip() not in labels:
            labels.append(label.strip())
        if len(labels) >= PER_PAGE_TERMS:
            break
    return labels


def _category_proposals(ctx: Context, rel_path: str) -> dict[str, dict[str, Any]]:
    """The category proposals whose page is `rel_path`, by candidate id."""
    from . import semantic_language_registry

    page = _governed(ctx, rel_path) if _sig(ctx, rel_path) is not None else None
    if page is None:
        return {}
    registry = ctx.registry()
    folds = _registry_folds(registry)
    projects = tuple(sorted(find_corpus.all_projects(page.frontmatter)))
    view = semantic_language_registry.for_attached_projects(registry, projects)
    out: dict[str, dict[str, Any]] = {}
    for label in _page_labels(ctx, rel_path):
        resolution = view.resolve_category(label, page_type=page.page_type)
        key = fold_term(resolution.key)
        if resolution.status == "unregistered":
            # Only a category this page may use is ever named, and the fold
            # must reach exactly one of them.
            targets = {
                target
                for target in folds.get(key, frozenset())
                if _in_scope(registry, target, projects, page.page_type)
            }
            if len(targets) != 1:
                continue
            target, reason = next(iter(targets)), "category_fold_registered"
        elif resolution.status == "deprecated" and resolution.replacement:
            target, reason = str(resolution.replacement), "category_replaced"
            if not _in_scope(registry, target, projects, page.page_type):
                continue
        else:
            continue
        cid = dreamer_store.candidate_id(CATEGORY_KIND, rel_path, key)
        if not key or cid in out:
            continue
        subject_entry = _member_entry(ctx, rel_path, "subject", spelling=label)
        out[cid] = {
            "family": CONVENTION_FAMILY,
            "kind": CATEGORY_KIND,
            "subject_path": rel_path,
            "subject_ref": subject_entry["ref"],
            "proposal_key": key,
            "evidence": [subject_entry],
            "route": {
                "tool": "edit_memory",
                "args": {"path": rel_path, "operation": {"kind": "replace_string"}},
            },
            "reason_code": reason,
            "signal_version": review_state_digest([label, reason, target, registry.content_hash]),
            "measures": {"label": label, "target": target, "registry": registry.content_hash},
        }
    return out


def _open_category_rows(ctx: Context, rel_path: str) -> set[str]:
    return {
        str(row[0])
        for row in ctx.conn.execute(
            "SELECT id FROM candidates WHERE family=? AND kind=? AND subject_path=? "
            "AND state='open'",
            (CONVENTION_FAMILY, CATEGORY_KIND, rel_path),
        )
    }


def _category_refresh(ctx: Context, rel_path: str) -> None:
    proposals = _category_proposals(ctx, rel_path)
    _resolve_all(ctx, _open_category_rows(ctx, rel_path) - set(proposals))
    for cid, kwargs in sorted(proposals.items()):
        ctx.store.upsert_proposal(
            ctx.conn, producer=PRODUCER, now=ctx.now, parked=ctx.parked, identity=cid, **kwargs
        )


def _convention_contribute(ctx: Context, rel_path: str, tags: list[tuple[str, str]]) -> None:
    store, conn = ctx.store, ctx.conn
    keys = store.contribution_keys(conn, "term_uses", rel_path)
    store.replace_contributions(conn, "term_uses", rel_path, tags)
    for key in sorted(keys | {key for key, _raw in tags}):
        _tag_refresh(ctx, key)


def _convention_on_page(ctx: Context, rel_path: str) -> None:
    page = _governed(ctx, rel_path)
    _convention_contribute(ctx, rel_path, _page_tags(page) if page is not None else [])
    _category_refresh(ctx, rel_path)


def _convention_on_delete(ctx: Context, rel_path: str) -> None:
    _convention_contribute(ctx, rel_path, [])
    _resolve_all(ctx, _open_category_rows(ctx, rel_path))


def _convention_revalidate(ctx: Context, row: dict[str, Any]) -> None:
    if row.get("kind") == TAG_KIND:
        _tag_refresh(ctx, str(row.get("proposal_key") or ""))
    else:
        _category_refresh(ctx, str(row.get("subject_path") or ""))


def _convention_propose(ctx: Context, row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("kind") == TAG_KIND:
        return _tag_kwargs(ctx, str(row.get("proposal_key") or ""))
    return _category_proposals(ctx, str(row.get("subject_path") or "")).get(str(row.get("id")))


def _convention_release(ctx: Context, row: dict[str, Any], keep) -> dict[str, Any] | None:
    if row.get("kind") != TAG_KIND:
        # A category row names only its own page: egress is exact already.
        return row
    return _released(
        _tag_view(ctx, str(row.get("proposal_key") or ""), keep=keep, strict=True), row
    )


CONVENTION = Family(
    name=CONVENTION_FAMILY,
    kinds=(TAG_KIND, CATEGORY_KIND),
    on_page=_convention_on_page,
    on_delete=_convention_on_delete,
    revalidate=_convention_revalidate,
    propose=_convention_propose,
    global_counts=True,
    release=_convention_release,
)


# ----------------------------------------------------------------------
# episode-recap fold `upkeep_fold`, and profile `upkeep_profile`
# ----------------------------------------------------------------------
#
# Both are per-subject counts over the published graph: the pages that link
# one governed page P, grouped into independent origins. Neither keeps page
# contributions of its own. Each is served per caller (`release`): every
# served field, count and fingerprint is recomputed from the pages that caller
# may see, so a withheld page equals an absent one. The stored row is the
# owner's view, a superset that exists whenever some caller could be served.

FOLD_FAMILY = "upkeep_fold"
FOLD_KIND = "episode.fold"
PROFILE_FAMILY = "upkeep_profile"
PROFILE_KIND = "profile.summary"

#: Independent origins either family needs: two episodes, or two Source origins.
FOLD_MIN_ORIGINS = 2
PROFILE_MIN_ORIGINS = 2

#: Governed pages a changed page links that are examined again, per page.
_LINKED_SUBJECTS_PER_PAGE = PER_PAGE_TERMS
#: Linking pages read per subject; on the request path, released ones only.
_LINKER_ROW_LIMIT = 64

_LINKED_SUBJECTS_SQL = (
    "SELECT DISTINCT d.path FROM graph_edges e JOIN graph_nodes d "
    "ON d.node_key = e.dst_key AND d.kind = 'file' "
    "WHERE e.source_path = ? AND d.path <> ? AND d.review_eligible = 1 "
    "AND COALESCE(e.relation_type, '') <> 'derived_from' "
    "ORDER BY d.path LIMIT ?"
)

#: Live episode recaps recorded after the subject's date that link the subject
#: and that it neither links nor cites.
_RECAPS_SQL = (
    "SELECT DISTINCT e.source_path, f.updated_date, f.origin_date "
    "FROM graph_edges e JOIN graph_nodes f "
    "ON f.node_key = ('file:' || e.source_path) AND f.kind = 'file' "
    "WHERE e.dst_key = ? AND substr(e.source_path, 1, ?) = ? "
    "AND COALESCE(e.relation_type, '') <> 'derived_from' "
    f"AND {_NEWER_THAN} "
    f"AND COALESCE(f.lifecycle_status, '') NOT IN ({','.join('?' for _ in _INACTIVE_STATUSES)}) "
    "AND NOT EXISTS (SELECT 1 FROM graph_edges b WHERE b.source_path = ? "
    "AND b.dst_key = ('file:' || e.source_path)) "
    "ORDER BY e.source_path"
)

#: Active governed pages that link the subject.
_REFERRERS_SQL = (
    "SELECT DISTINCT e.source_path FROM graph_edges e JOIN graph_nodes f "
    "ON f.node_key = ('file:' || e.source_path) AND f.kind = 'file' "
    "WHERE e.dst_key = ? AND e.source_path <> ? AND f.review_eligible = 1 "
    "AND COALESCE(e.relation_type, '') <> 'derived_from' "
    "ORDER BY e.source_path"
)

#: The `## Summary` section the entity page shape carries (`link.py`), and
#: the next heading of its level or above, which ends it.
_SUMMARY_SECTION = re.compile(r"^##[ \t]+summary[ \t]*$", re.IGNORECASE | re.MULTILINE)
_SECTION_END = re.compile(r"^#{1,2}[ \t]", re.MULTILINE)


def _episodes_prefix() -> str:
    from . import source_taxonomy
    from .kbdir import kb_dirname

    return f"{kb_dirname()}/{source_taxonomy.SOURCES_ROOT}/{source_taxonomy.EPISODE_PATH_LABEL}/"


def _subject_node(ctx: Context, subject: str, keep) -> tuple[Any, ...] | None:
    """The subject's graph row when it is an active governed page the caller may see."""
    if not _visible(keep, subject):
        return None
    node = (
        ctx.graph()
        .execute(
            "SELECT review_eligible, updated_date, origin_date, exomem_id, title "
            "FROM graph_nodes WHERE node_key = ? AND kind = 'file'",
            (f"file:{subject}",),
        )
        .fetchone()
    )
    return node if node is not None and node[0] else None


def _visible(keep, path: str) -> bool:
    return keep is None or bool(keep(path))


def _basis_fingerprint(family: str, subject_ref: str, signal: str) -> str:
    """A proposal's fingerprint over its subject and signal alone, not its
    evidence paths, which move without the proposal changing."""
    return dreamer_store.proposal_fingerprint(
        family=family, subject_ref=subject_ref, signal_version=signal, evidence=[]
    )


def subject_rows_touched(ctx: Context, paths: list[str]) -> set[str]:
    """Fold and profile rows the given pages may be members of, by candidate id:
    the rows on each page and on each governed page it links. A superset."""
    linked: set[str] = set()
    if paths:
        marks = ",".join("?" for _ in paths)
        try:
            linked = {
                str(row[0])
                for row in ctx.graph().execute(
                    "SELECT DISTINCT d.path FROM graph_edges e JOIN graph_nodes d "
                    "ON d.node_key = e.dst_key AND d.kind = 'file' "
                    f"WHERE e.source_path IN ({marks}) AND d.review_eligible = 1",
                    paths,
                )
            }
        except (sqlite3.Error, Deferred):
            linked = set()
    return {
        dreamer_store.candidate_id(kind, subject, "")
        for subject in {*paths, *linked}
        for kind in (FOLD_KIND, PROFILE_KIND)
    }


def _released_rows(cursor, keep) -> list[tuple[Any, ...]]:
    """The first `_LINKER_ROW_LIMIT` rows whose page the caller may see, in path order."""
    out: list[tuple[Any, ...]] = []
    for row in cursor:
        if _visible(keep, str(row[0])):
            out.append(row)
            if len(out) >= _LINKER_ROW_LIMIT:
                break
    return out


def _linked_subjects(ctx: Context, rel_path: str) -> list[str]:
    """The page itself and the governed pages it links (at most 16)."""
    linked = [
        str(row[0])
        for row in ctx.graph().execute(
            _LINKED_SUBJECTS_SQL, (rel_path, rel_path, _LINKED_SUBJECTS_PER_PAGE)
        )
    ]
    return [rel_path, *linked]


def _fold_view(ctx: Context, subject: str, *, keep) -> dict[str, Any] | None:
    """The fold proposal on one page, as a caller whose predicate is `keep` sees it.

    Two or more episodes recorded recaps that link the page after it was last
    updated, and the page neither links nor cites them: what was decided or
    worked on in those conversations may not have reached its home. Revisions
    of one episode are one origin; superseded revisions are not live.
    """
    node = _subject_node(ctx, subject, keep)
    if node is None:
        return None
    since = _date(node[1], node[2])
    prefix = _episodes_prefix()
    rows = _released_rows(
        ctx.graph().execute(
            _RECAPS_SQL,
            (
                f"file:{subject}",
                len(prefix),
                prefix,
                since,
                *sorted(_INACTIVE_STATUSES),
                subject,
            ),
        ),
        keep,
    )
    episodes: dict[str, tuple[str, str, str]] = {}
    for path, updated, origin in rows:
        path = str(path)
        page = ctx.page(path)
        frontmatter = page.frontmatter if page is not None else {}
        key = frontmatter.get("episode") if isinstance(frontmatter, dict) else None
        origin_key = review_state_digest(["episode", key if isinstance(key, str) else path])
        # The newest revision speaks for its episode: the recorder's order
        # token in the filename decides among revisions of one day.
        parts = episode_capture.filename_parts(Path(path).name)
        newer = (_date(updated, origin), parts[1] if parts else "", path)
        if origin_key not in episodes or newer > episodes[origin_key]:
            episodes[origin_key] = newer
    if len(episodes) < FOLD_MIN_ORIGINS:
        return None
    chosen = sorted((path, origin) for origin, (_d, _o, path) in episodes.items())
    chosen = chosen[:_OTHER_MEMBERS]
    subject_entry = _member_entry(ctx, subject, "subject")
    # Bound to the episodes, not to their revisions' paths: a new revision of
    # a counted episode neither reopens a dismissal nor restarts delivery.
    signal = review_state_digest([subject, sorted(episodes)])
    return {
        "family": FOLD_FAMILY,
        "kind": FOLD_KIND,
        "subject_path": subject,
        "subject_ref": subject_entry["ref"],
        "proposal_key": "",
        "evidence": [
            subject_entry,
            *(_member_entry(ctx, path, "recap", origin=origin) for path, origin in chosen),
        ],
        "evidence_count": len(episodes) + 1,
        "route": {
            "tool": "maintain_memory",
            "args": {
                "mode": "curation",
                "curation_action": "work-item",
                "paths": [subject, *(path for path, _origin in chosen)],
            },
        },
        "reason_code": "recaps_newer_than_page",
        "signal_version": signal,
        "fingerprint": _basis_fingerprint(FOLD_FAMILY, subject_entry["ref"], signal),
        "measures": {"episodes": len(episodes)},
    }


def _self_described(page: Any) -> bool:
    """True when the page says what it is: a `summary` field, or a `## Summary`
    section with text outside code."""
    from .vault import _mask_code_spans

    frontmatter = page.frontmatter if isinstance(page.frontmatter, dict) else {}
    value = frontmatter.get("summary")
    if isinstance(value, str) and value.strip():
        return True
    masked = _mask_code_spans(str(getattr(page, "body", "") or ""))
    heading = _SUMMARY_SECTION.search(masked)
    if heading is None:
        return False
    following = _SECTION_END.search(masked, heading.end())
    return bool(masked[heading.end() : following.start() if following else None].strip())


def _profile_view(ctx: Context, subject: str, *, keep) -> dict[str, Any] | None:
    """The profile proposal on one page, as a caller whose predicate is `keep` sees it.

    Pages of two or more independent origins link the page, and it carries no
    summary of itself. Origins are the union-find over the referrers' declared
    Sources the caller may see; referrers that declare none are one origin.
    """
    from . import provenance

    if _subject_node(ctx, subject, keep) is None:
        return None
    page = ctx.page(subject)
    if page is None or _self_described(page):
        return None
    graph = ctx.graph()
    referrers = [
        str(row[0])
        for row in _released_rows(graph.execute(_REFERRERS_SQL, (f"file:{subject}", subject)), keep)
    ]
    if len(referrers) < PROFILE_MIN_ORIGINS:
        return None
    # A Source the caller may not see is no origin: the referrer reads as
    # declaring only the Sources it may see, as on a vault without that Source.
    sources: dict[str, set[str]] = {path: set() for path in referrers}
    marks = ",".join("?" for _ in referrers)
    for src_key, dst_key in graph.execute(
        f"SELECT src_key, dst_key FROM graph_edges WHERE src_key IN ({marks}) "
        "AND origin = 'frontmatter' AND source_anchor = 'sources' "
        "AND relation_type = 'derived_from'",
        [f"file:{path}" for path in referrers],
    ):
        if _visible(keep, str(dst_key).removeprefix("file:")):
            sources[str(src_key).removeprefix("file:")].add(str(dst_key))
    unsourced = {path: _UNSOURCED_ORIGIN for path, declared in sources.items() if not declared}
    origins = provenance.origin_keys(sources, fallback=unsourced)
    if len(set(origins.values())) < PROFILE_MIN_ORIGINS:
        return None
    firsts: dict[str, str] = {}
    for path in sorted(referrers, key=lambda path: (origins[path], path)):
        firsts.setdefault(origins[path], path)
    chosen = sorted(firsts.values())[:_OTHER_MEMBERS]
    subject_entry = _member_entry(ctx, subject, "subject")
    # Bound to the origins: a new referrer of a counted origin changes nothing.
    signal = review_state_digest([subject, sorted(set(origins.values()))])
    return {
        "family": PROFILE_FAMILY,
        "kind": PROFILE_KIND,
        "subject_path": subject,
        "subject_ref": subject_entry["ref"],
        "proposal_key": "",
        "evidence": [
            subject_entry,
            *(_member_entry(ctx, path, "referrer", origin=origins[path]) for path in chosen),
        ],
        "evidence_count": len(referrers) + 1,
        "route": {
            "tool": "edit_memory",
            "args": {
                "path": subject,
                "operation": {"kind": "patch_frontmatter", "field": "summary"},
            },
        },
        "reason_code": "linked_without_summary",
        "signal_version": signal,
        "fingerprint": _basis_fingerprint(PROFILE_FAMILY, subject_entry["ref"], signal),
        "measures": {"origins": len(set(origins.values())), "referrers": len(referrers)},
    }


def _subject_family(
    name: str,
    kind: str,
    view: Callable[..., dict[str, Any] | None],
    *,
    linked_from: Callable[[str], bool],
) -> Family:
    """A family whose one proposal per subject page is `view` of that page.

    A changed page recomputes its own proposal, and those of the governed
    pages it links when `linked_from(path)` says it can count toward them.
    """

    def refresh(ctx: Context, subject: str) -> None:
        kwargs = view(ctx, subject, keep=None) if _sig(ctx, subject) else None
        if kwargs is None:
            ctx.store.resolve(
                ctx.conn,
                dreamer_store.candidate_id(kind, subject, ""),
                producer=PRODUCER,
                now=ctx.now,
            )
            return
        ctx.store.upsert_proposal(
            ctx.conn, producer=PRODUCER, now=ctx.now, parked=ctx.parked, **kwargs
        )

    def on_page(ctx: Context, rel_path: str) -> None:
        subjects = _linked_subjects(ctx, rel_path) if linked_from(rel_path) else [rel_path]
        for subject in subjects:
            refresh(ctx, subject)

    def on_delete(ctx: Context, rel_path: str) -> None:
        refresh(ctx, rel_path)

    def revalidate(ctx: Context, row: dict[str, Any]) -> None:
        refresh(ctx, str(row.get("subject_path") or ""))

    def propose(ctx: Context, row: dict[str, Any]) -> dict[str, Any] | None:
        subject = str(row.get("subject_path") or "")
        return view(ctx, subject, keep=None) if _sig(ctx, subject) else None

    def release(ctx: Context, row: dict[str, Any], keep) -> dict[str, Any] | None:
        return _released(view(ctx, str(row.get("subject_path") or ""), keep=keep), row)

    return Family(
        name=name,
        kinds=(kind,),
        on_page=on_page,
        on_delete=on_delete,
        revalidate=revalidate,
        propose=propose,
        release=release,
    )


FOLD = _subject_family(
    FOLD_FAMILY,
    FOLD_KIND,
    _fold_view,
    linked_from=lambda path: path.startswith(_episodes_prefix()),
)
PROFILE = _subject_family(PROFILE_FAMILY, PROFILE_KIND, _profile_view, linked_from=lambda _p: True)


def tick_start(ctx: Context) -> None:
    """Once per tick: resume after the size cap, and follow a registry change.

    A global family that skipped pages at the size cap reseeds once the file
    is back under it: every indexed page is queued again and the families
    report incomplete until that drains. A category row's signal is anchored
    on the registry's content hash, which no page change reports, so its
    pages are queued again when the hash moves. Writes only on either change.
    """
    store, conn = ctx.store, ctx.conn
    if store.get_meta(conn, CAPACITY_BEHIND_META) and not store.capacity_exceeded(conn):
        with store.write(conn):
            store.pending_add(conn, sorted(store.seen_map(conn)))
            store.set_meta(conn, "reseeding", True)
            store.set_meta(conn, CAPACITY_BEHIND_META, None)
    registry = ctx.registry()
    if ctx.store.get_meta(ctx.conn, _REGISTRY_META) == registry.content_hash:
        return
    subjects = sorted(
        {
            str(row[0])
            for row in ctx.conn.execute(
                "SELECT subject_path FROM candidates WHERE family=? AND kind=? AND state='open'",
                (CONVENTION_FAMILY, CATEGORY_KIND),
            )
        }
    )
    with ctx.store.write(ctx.conn):
        if subjects:
            ctx.store.pending_add(ctx.conn, subjects)
        ctx.store.set_meta(ctx.conn, _REGISTRY_META, registry.content_hash)


def release(ctx: Context, row: dict[str, Any], keep) -> dict[str, Any] | None:
    """One stored row as a caller whose release predicate is `keep` may see it.

    A family without per-item egress is served from its stored row. A global
    family recomputes the row from the members that caller may see, so a
    withheld page equals an absent one in every served field, count and
    fingerprint. None when the family's minimum does not hold on them, or the
    sidecar cannot be read without waiting.
    """
    family = family_for(str(row.get("family") or ""))
    if family is None or family.release is None:
        return row
    try:
        return family.release(ctx, row, keep)
    except (sqlite3.Error, Deferred):
        return None


#: The families this build implements, in registry order.
REGISTRY: list[Family] = [LINK, HYDRATION, ALIAS, CONVENTION, FOLD, PROFILE]


def family_names() -> tuple[str, ...]:
    return tuple(family.name for family in REGISTRY)


def family_for(name: str) -> Family | None:
    return next((family for family in REGISTRY if family.name == name), None)


def process_page(ctx: Context, rel_path: str, *, exists: bool, changed: bool = True) -> None:
    """Run every family over one page, then queue what its change touches.

    `on_page`/`on_delete` own every proposal whose SUBJECT is this page: they
    recompute them and resolve the ones that no longer hold. A proposal that
    merely cites this page as evidence belongs to another subject: when this
    page CHANGED, that subject is queued as a page of its own (once, however
    many of its rows cite this one), so one page's work stays one page's. A
    page processed only because it was queued changed nothing, and queues
    nothing, so two pages that cite each other cannot requeue each other.
    """
    cited_by = set(ctx.store.candidates_for_path(ctx.conn, rel_path)) if changed else set()
    for family in REGISTRY:
        # Past the size cap a global family records nothing more (a deletion
        # only removes rows, so it still runs). The skipped page is owed: the
        # family is marked behind, reports incomplete and delivers nothing,
        # and reseeds once there is room again (`tick_start`).
        if exists and family.global_counts and not ctx.store.family_enabled(ctx.conn, family.name):
            ctx.store.set_meta(ctx.conn, CAPACITY_BEHIND_META, True)
            continue
        if exists:
            family.on_page(ctx, rel_path)
        else:
            family.on_delete(ctx, rel_path)
    live = _sig(ctx, rel_path) if exists else None
    subjects: set[str] = set()
    for cid in sorted(cited_by):
        row = ctx.store.candidate(ctx.conn, cid)
        if row is None or row.get("state") != "open":
            continue
        subject = str(row.get("subject_path") or "")
        if not subject or subject == rel_path or family_for(str(row.get("family") or "")) is None:
            continue
        # A row that already saw this page's current signature needs nothing:
        # during a reseed its subject was processed after this page changed.
        recorded = {
            item.get("sig") for item in row.get("evidence") or () if item.get("path") == rel_path
        }
        if recorded != {live}:
            subjects.add(subject)
    if subjects:
        ctx.store.pending_add(ctx.conn, sorted(subjects))


# ----------------------------------------------------------------------
# deliverability, precomputed by the worker for the carrier
# ----------------------------------------------------------------------


def review_state_token(vault_root: Path) -> str | None:
    """The review-state file's identity: inode, mtime and size, or None."""
    from . import review_state

    try:
        info = review_state.state_path(vault_root).stat()
    except OSError:
        return None
    return f"{info.st_ino}:{info.st_mtime_ns}:{info.st_size}"


#: Sidecar memo of absent twins whose page was queued: twin id -> the page's
#: `seen` signature then. Bounded; the oldest entries go first.
_REQUEUED_META = "requeued_twins"
_REQUEUED_LIMIT = 256


def _twin_id(row: dict[str, Any]) -> str | None:
    """The relation id of a link row's other direction, or None."""
    if row.get("family") != LINK_FAMILY:
        return None
    from . import relation_queue

    measures = row.get("measures") or {}
    subject, target = str(row.get("subject_path") or ""), str(measures.get("to") or "")
    if not subject or not target:
        return None
    return relation_queue._candidate_identity(
        {
            "from": target,
            "to": subject,
            "relation_type": str(measures.get("relation_type") or ""),
            "method": str(measures.get("method") or ""),
        }
    )


def _pair_key(row: dict[str, Any]) -> tuple[str, ...] | None:
    if row.get("family") != LINK_FAMILY:
        return None
    measures = row.get("measures") or {}
    ends = sorted([str(row.get("subject_path") or ""), str(measures.get("to") or "")])
    return (*ends, str(measures.get("relation_type") or ""), str(measures.get("method") or ""))


def precompute_deliverable(ctx: Context, *, extra_deliveries=()) -> float | None:
    """Mark every open candidate deliverable or not, for the carrier to read.

    Deliverable means: its evidence has settled, its review-state decision for
    `(id, fingerprint)` is open, its family disposition is `normal`, and it is
    not held (delivered twice without a disposition). The two directions of one
    link pair are one proposal: only the smaller source path is ever offered,
    so a decision on it holds the other direction too. An
    unreadable review state fails closed: nothing is deliverable. While a
    reseed drains, a family that needs vault-wide counts delivers nothing:
    its membership is not complete yet.
    """
    from . import review_state

    reseeding = bool(ctx.store.get_meta(ctx.conn, "reseeding")) and (
        ctx.store.pending_count(ctx.conn) > 0
    )
    counting = {family.name for family in REGISTRY if family.global_counts}
    token = review_state_token(ctx.vault_root)
    payload = ctx.review_payload()
    store = ctx.review_store()
    counts: dict[tuple[str, str], int] = {}
    for cid, fingerprint, _caller, _at in (*ctx.store.deliveries(ctx.conn), *extra_deliveries):
        counts[(cid, fingerprint)] = counts.get((cid, fingerprint), 0) + 1
    rows = ctx.store.open_candidates(ctx.conn)
    eligible: dict[str, bool] = {}
    settled_at: dict[str, float | None] = {}
    for row in rows:
        cid = str(row["id"])
        refreshed = float(row.get("refreshed_at") or ctx.now)
        settled = ctx.now - refreshed >= SETTLE_SECONDS
        settled_at[cid] = row.get("settled_at") or (refreshed + SETTLE_SECONDS if settled else None)
        if payload is None or not settled:
            eligible[cid] = False
            continue
        state, _decision = store.effective_state(cid, str(row["fingerprint"]), payload=payload)
        family = str(row.get("family") or "")
        eligible[cid] = (
            state == "open"
            and not (reseeding and family in counting)
            and review_state.disposition_for(family, payload=payload) == "normal"
            and counts.get((cid, str(row["fingerprint"])), 0) < MAX_DELIVERIES
            # A decision on either direction of a link pair holds the pair,
            # whether or not that direction's row is still stored.
            and not ctx.pair_held(row)
        )
    # The pair's offered direction is chosen among its open rows, not its
    # eligible ones, so the other direction is never promoted in its place.
    pairs: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        key = _pair_key(row)
        if key is not None:
            pairs.setdefault(key, []).append(row)
    for members in pairs.values():
        members.sort(key=lambda row: str(row.get("subject_path") or ""))
        for row in members[1:]:
            eligible[str(row["id"])] = False
    next_settle: float | None = None
    for row in rows:
        cid = str(row["id"])
        if settled_at[cid] is None:
            due = float(row.get("refreshed_at") or ctx.now) + SETTLE_SECONDS
            next_settle = due if next_settle is None else min(next_settle, due)
        if (
            bool(row.get("deliverable")) == eligible[cid]
            and row.get("deliverable_token") == token
            and row.get("settled_at") == settled_at[cid]
        ):
            continue
        ctx.store.set_deliverable(
            ctx.conn, cid, deliverable=eligible[cid], token=token, settled_at=settled_at[cid]
        )
    return next_settle


def propose(ctx: Context, row: dict[str, Any]) -> dict[str, Any] | None:
    """One stored candidate's current proposal, recomputed in memory.

    The result carries the current evidence, signal version and fingerprint.
    None when the proposal no longer holds or its family is not registered.
    Writes nothing: it is the request path's bounded revalidation.
    """
    family = family_for(str(row.get("family") or ""))
    if family is None or family.propose is None:
        return None
    proposal = family.propose(ctx, row)
    if proposal is None:
        return None
    proposal = dict(proposal)
    proposal.pop("identity", None)
    proposal["evidence"] = sorted(
        proposal["evidence"], key=lambda item: str(item.get("path") or "")
    )[: dreamer_store.EVIDENCE_CAP]
    if "fingerprint" not in proposal:
        proposal["fingerprint"] = dreamer_store.proposal_fingerprint(
            family=proposal["family"],
            subject_ref=proposal["subject_ref"],
            signal_version=proposal["signal_version"],
            evidence=proposal["evidence"],
        )
    return proposal
