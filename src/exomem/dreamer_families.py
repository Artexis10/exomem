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

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import dreamer_store, find_corpus

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
        known without reading its page, so any standing decision holds the
        pair until that direction is proposed again.
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
        return self._held_any(twin)

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

_CONTRIBUTORS_SQL = (
    "SELECT DISTINCT e.source_path, e.src_key, f.updated_date, f.origin_date, f.exomem_id, "
    "f.title "
    "FROM graph_edges e JOIN graph_nodes f "
    "ON f.node_key = ('file:' || e.source_path) AND f.kind = 'file' "
    "WHERE e.dst_key = ? AND e.source_path <> ? "
    "AND COALESCE(e.relation_type, '') <> 'derived_from' "
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
            *_HYDRATION_TYPES,
            *sorted(_INACTIVE_STATUSES),
            entity,
            _HYDRATION_ROW_LIMIT,
        ),
    ).fetchall()
    contributors: dict[str, dict[str, Any]] = {}
    for path, src_key, updated, origin, exomem_id, title in rows:
        path = str(path)
        fact_date = _date(updated, origin)
        if not fact_date or fact_date <= entity_date:
            continue
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


#: The families this build implements, in registry order.
REGISTRY: list[Family] = [LINK, HYDRATION]


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
    unreadable review state fails closed: nothing is deliverable.
    """
    from . import review_state

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
    return proposal
