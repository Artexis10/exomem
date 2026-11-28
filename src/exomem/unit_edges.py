"""Typed edges from governed unit operations and their semantic write view."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import relation_registry
from .vault_store import relationships

if TYPE_CHECKING:
    from . import epistemic_graph, semantic_contract, semantic_index, semantic_units, vault
    from .vault_store import read

Edge = tuple[str, str]


@dataclass(frozen=True, slots=True)
class StoredPlan:
    """One governed unit's relationship intent and any anchors needed by its ends."""

    digest: str
    change: relationships.Change | None = None
    writes: tuple[vault.PlannedWrite, ...] = ()
    guards: tuple[vault.PathGuard, ...] = ()
    target_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class End:
    path: str
    anchor: str | None
    parent_ref: str | None


@dataclass(frozen=True, slots=True)
class RelationshipPage:
    edges: tuple[relationships.BoundRelationship, ...] = ()
    total: int | None = None
    next_after: tuple[str, str] | None = None

    def as_dict(self) -> dict:
        if self.total is None:
            return {"status": "unavailable"}
        return {
            "status": "available",
            "total": self.total,
            "next_after": list(self.next_after) if self.next_after is not None else None,
            "items": [
                {
                    "id": edge.relationship.id,
                    "kind": edge.relationship.relation_type,
                    "target_path": edge.target_path,
                    "target_anchor": edge.target.anchor if edge.target is not None else None,
                }
                for edge in self.edges
            ],
        }


def relationship_page(
    root: Path, path: str, anchor: str | None, *,
    limit: int = 50, after: tuple[str, str] | None = None,
) -> RelationshipPage:
    """Read an exact visible page and count in one canonical snapshot."""
    from . import epistemic_graph, request_budget
    from .cli_ops import OpError
    from .governance import egress
    from .vault_store import derivation, mode, read

    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("relationship page limit must be between 1 and 50")
    if not mode.store_mode_for_reads(root):
        return RelationshipPage()
    if anchor is None:
        return RelationshipPage(total=0)
    with (
        request_budget.bound_to(derivation.READ_WAIT_SECONDS) as budget,
        egress.release_decision_context(root),
        read.request_snapshot(root),
    ):
        conn = read.lend(root)
        if conn is None:
            return RelationshipPage()

        def expired() -> bool:
            return time.monotonic() >= budget.deadline

        outer_check = request_budget.current_checkpoint()
        check = request_budget.ReadCheckpoint(
            budget, bound_disclosure=outer_check is not None and outer_check.bound_disclosure,
        )
        with closing(conn), read.snapshot(root) as snapshot, request_budget.checking(check):
            conn.set_progress_handler(lambda: int(expired()), 1000)
            try:
                if not conn.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='store_relationships'",
                ).fetchone():
                    return RelationshipPage()
                identity = relationships.identities_at(conn, {(path, anchor)}).get((path, anchor))
                if identity is None:
                    return RelationshipPage(total=0)
                read.require_current_parser(conn)
                keep = egress.reader_view(root)
                restricted = egress.caller_restricted(root)

                def view() -> epistemic_graph.GraphView:
                    return epistemic_graph.GraphView(root, conn, keep=keep, check=check)

                with snapshot.prefetch([path]):
                    source_view = view()
                    source_view.admit([path])
                    if restricted and path not in egress.full_release_among(root, [path]):
                        return RelationshipPage(total=0)
                    source = _current_parent(snapshot, source_view, path)
                    if (source is None or source.structure.parent_ref != identity.parent_ref
                            or source.structure.resolve_fragment(anchor).index is None):
                        return RelationshipPage(total=0)
                    registry = source.relations

                found: list[relationships.BoundRelationship] = []
                endpoints: OrderedDict[str, _RelationshipEndpoint | None] = OrderedDict()
                total = 0
                more = False
                # Seek the next page first. Count the prefix separately so the visible total
                # still includes rows before the cursor, even if policy changed since page one.
                ranges = ((after, None), (None, after)) if after else ((None, None),)
                for lower, upper in ranges:
                    cursor = lower
                    while True:
                        check()
                        batch = tuple(relationships.outgoing_unit(
                            conn, identity, after=cursor, through=upper, limit=read.KEYS_PER_QUERY,
                        ))
                        if not batch:
                            break
                        admitted_types = tuple(
                            edge for edge in batch
                            if registry.definition(edge.relationship.relation_type) is not None
                        )
                        visible = _visible_relationships(
                            root, snapshot, view, admitted_types, restricted, endpoints,
                        )
                        for edge in visible:
                            total += 1
                            if upper is None:
                                if len(found) < limit:
                                    found.append(edge)
                                else:
                                    more = True
                        if len(batch) < read.KEYS_PER_QUERY:
                            break
                        cursor = _relationship_key(batch[-1])
                check()
                next_after = _relationship_key(found[-1]) if more else None
                return RelationshipPage(tuple(found), total, next_after)
            except (TimeoutError, request_budget.ReadBudgetExhausted, _RelationshipReadUnavailable):
                return RelationshipPage()
            except OpError as error:
                if error.code == read.DERIVED_BEHIND:
                    return RelationshipPage()
                raise
            except sqlite3.OperationalError as error:
                error_code = getattr(error, "sqlite_errorcode", None)
                if error_code == sqlite3.SQLITE_INTERRUPT and expired():
                    return RelationshipPage()
                raise


class _RelationshipReadUnavailable(Exception):
    """An admitted relationship endpoint has no current interpretation."""


def _current_parent(
    snapshot: read.StoreSnapshot, view: epistemic_graph.GraphView, path: str,
) -> semantic_index.SelectedParent | None:
    meta = snapshot.page_meta(path)
    if meta is None or not view.allowed(path):
        return None
    parent = view.parent(path)
    if (parent is None or path in view.unavailable
            or parent.metadata.get("parent_source_hash") != meta.sha256):
        raise _RelationshipReadUnavailable()
    return parent


@dataclass(frozen=True, slots=True)
class _RelationshipEndpoint:
    parent_ref: str | None
    anchored_refs: frozenset[str]

    @classmethod
    def from_parent(cls, parent: semantic_index.SelectedParent) -> _RelationshipEndpoint:
        counts = Counter(
            unit.unit_ref for unit in parent.structure.units if unit.unit_ref is not None
        )
        return cls(
            parent.structure.parent_ref, frozenset(ref for ref, count in counts.items() if count == 1),
        )


def _visible_relationships(
    root: Path, snapshot: read.StoreSnapshot, new_view: Callable[[], epistemic_graph.GraphView],
    batch: tuple[relationships.BoundRelationship, ...], restricted: bool,
    endpoints: OrderedDict[str, _RelationshipEndpoint | None],
) -> Iterator[relationships.BoundRelationship]:
    from . import request_budget, semantic_units
    from .governance import egress
    from .vault_store import read

    if not batch:
        return
    paths = {edge.target_path for edge in batch}
    unit_paths = {edge.target_path for edge in batch if edge.target is not None}
    with snapshot.prefetch(paths):
        full = egress.full_release_among(root, unit_paths) if restricted else unit_paths
        paths = full | {edge.target_path for edge in batch if edge.target is None}
        missing = paths.difference(endpoints)
        if missing:
            view = new_view()
            present = [path for path in sorted(missing) if snapshot.page_meta(path) is not None]
            view.admit(present)
            view.prefetch(present)
            for path in missing:
                parent = _current_parent(snapshot, view, path)
                endpoints[path] = None if parent is None else _RelationshipEndpoint.from_parent(parent)
        for edge in batch:
            request_budget.checkpoint()
            target_path = edge.target_path
            target = endpoints.get(target_path) if target_path in paths else None
            if target is None:
                continue
            parent_ref = target.parent_ref
            if edge.target is not None:
                if target_path not in full or not parent_ref or parent_ref != edge.target.parent_ref:
                    continue
                ref = semantic_units._anchored_unit_ref(parent_ref, edge.target.anchor)
                if ref not in target.anchored_refs:
                    continue
            elif (edge.relationship.target_page_ref is not None
                  and parent_ref != edge.relationship.target_page_ref):
                continue
            yield edge
        # Keep one batch of adjacent endpoint identities under the same pinned caller view.
        for path in sorted(paths):
            endpoints.move_to_end(path)
        while len(endpoints) > read.KEYS_PER_QUERY:
            endpoints.popitem(last=False)

def _relationship_key(edge: relationships.BoundRelationship) -> tuple[str, str]:
    return edge.relationship.relation_type, edge.relationship.id


def plan_change(
    root: Path, *, source: End, targets: tuple[tuple[str, End], ...],
    visible: Callable[[str], bool] | None, replacing: bool, reason: str | None,
    writes: tuple[vault.PlannedWrite, ...], guards: tuple[vault.PathGuard, ...],
    supersession_check: relationships.SupersessionCheck | None = None,
) -> StoredPlan | None:
    """Bind admitted ends to allocated IDs and compute a guarded change of active edges."""
    from .cli_ops import OpError
    from .governance.principal import effective_principal
    from .vault_store import read

    assert source.anchor is not None
    locations = {(source.path, source.anchor)}
    locations.update((end.path, end.anchor) for _, end in targets if end.anchor is not None)
    with read.snapshot(root) as view:
        existing = relationships.identities_at(view.connection, locations)
        current_source = existing.get((source.path, source.anchor))
        outgoing = () if current_source is None else tuple(relationships.outgoing_unit(view.connection, current_source))

    allocated: dict[tuple[str, str], relationships.UnitIdentity] = {}

    def identity(end: End) -> str:
        assert end.anchor is not None
        locator = (end.path, end.anchor)
        unit = existing.get(locator)
        if unit is not None:
            if unit.parent_ref != end.parent_ref:
                raise OpError("UNIT_IDENTITY_CHANGED", "a stored unit's parent identity changed")
            return unit.id
        if locator not in allocated:
            allocated[locator] = relationships.UnitIdentity(uuid.uuid4().hex, *locator, end.parent_ref)
        return allocated[locator].id

    if not targets and not outgoing:
        return None
    source_id = identity(source)
    wanted = {
        (kind, identity(end) if end.anchor is not None else None,
         end.path if end.anchor is None else None, end.parent_ref if end.anchor is None else None)
        for kind, end in targets
    }
    active = {
        (edge.relationship.relation_type, edge.relationship.target_unit_id,
         edge.relationship.target_page_path, edge.relationship.target_page_ref): edge
        for edge in outgoing
    }
    added = tuple(relationships.Relationship(uuid.uuid4().hex, source_id, *item)
                  for item in sorted(wanted - active.keys(), key=repr))
    retired = tuple(relationships.Retirement(edge.relationship.id)
                    for item, edge in active.items() if replacing and item not in wanted
                    and (visible is None or visible(edge.target_path)))
    wanted_keys = tuple(sorted(wanted, key=repr))
    with read.snapshot(root) as view:
        last_retired = relationships.last_retirement(view.connection, source_id, wanted_keys)
    # Preview and commit may allocate different IDs for new ends. Pin the locator and guarded
    # versions, plus the exact active edges this operation would retire.
    intent = json.dumps([
        source.path, source.anchor,
        sorted(((kind, end.path, end.anchor, end.parent_ref) for kind, end in targets), key=repr),
        sorted(edge.id for edge in retired),
        sorted((guard.target, guard.expected_content_hash) for guard in guards),
        last_retired,
    ], separators=(",", ":"))
    digest = hashlib.sha256(intent.encode()).hexdigest()
    expected = (source_id, frozenset(edge.relationship.id for edge in outgoing)) if replacing else None
    change = relationships.Change(
        effective_principal().audience_id, tuple(allocated.values()), added, retired, reason, expected,
        supersession_check=supersession_check, expected_retirement=(source_id, wanted_keys, last_retired),
    )
    retired_ids = {edge.id for edge in retired}
    paths = {end.path for _, end in targets}
    paths.update(edge.target_path for edge in outgoing if edge.relationship.id in retired_ids)
    return StoredPlan(digest, change if added or retired else None, writes, guards, tuple(sorted(paths)))


def with_stored_facts(
    root: Path,
    path: str,
    before: semantic_contract.SemanticCorpusContext,
    after: semantic_contract.SemanticCorpusContext,
    plan: StoredPlan | None,
) -> tuple[semantic_contract.SemanticCorpusContext, semantic_contract.SemanticCorpusContext]:
    """Read the incident relationships once and bind each side to its own page states."""
    from .vault_store import mode, read

    if not mode.store_mode_for_reads(root):
        return before, after
    change = plan.change if plan is not None else None
    with read.snapshot(root) as view:
        committed = tuple(relationships.incident(view.connection, path))
        added = relationships.bind_additions(view.connection, change) if change is not None else ()
    if not committed and not added:
        return before, after
    retired = {edge.id for edge in change.retired} if change is not None else set()
    retained = tuple(item for item in committed if item.relationship.id not in retired)
    return (
        replace(before, stored_relation_facts=_facts(before, committed)),
        replace(after, stored_relation_facts=_facts(after, (*retained, *added))),
    )


def _facts(
    corpus: semantic_contract.SemanticCorpusContext,
    stored: Iterable[relationships.BoundRelationship],
) -> tuple[semantic_contract.RelationFact, ...]:
    from . import semantic_contract

    facts = []
    anchors: dict[str, dict[str, semantic_units.SemanticUnit | None]] = {}
    for item in stored:
        source = corpus.pages.get(item.source.parent_path)
        target = corpus.pages.get(item.target_path)
        if source is None or target is None:
            continue
        source_unit = _bound_unit(source, item.source, anchors)
        if source_unit is None:
            continue
        target_unit = None
        if item.target is not None:
            target_unit = _bound_unit(target, item.target, anchors)
            if target_unit is None:
                continue
        elif (reference := item.relationship.target_page_ref) and reference != target.document.parent_ref:
            continue
        facts.append(semantic_contract.stored_relationship_fact(
            corpus, relationship_id=item.relationship.id, source=source, source_unit=source_unit,
            relation_type=item.relationship.relation_type, target=target, target_unit=target_unit,
        ))
    return tuple(facts)


def _bound_unit(
    page: semantic_contract.SemanticPageState,
    identity: relationships.UnitIdentity,
    anchors: dict[str, dict[str, semantic_units.SemanticUnit | None]],
) -> semantic_units.SemanticUnit | None:
    if identity.parent_ref is not None and identity.parent_ref != page.document.parent_ref:
        return None
    if page.path not in anchors:
        captured: dict[str, semantic_units.SemanticUnit | None] = {}
        for unit in page.document.units:
            if unit.anchor is not None:
                captured[unit.anchor] = None if unit.anchor in captured else unit
        anchors[page.path] = captured
    return anchors[page.path].get(identity.anchor)


def current_supersessions(
    root: Path, paths: Iterable[str], registry: relation_registry.RelationRegistry,
) -> dict[str, list[tuple[str, str]]]:
    """Read current same-page replacement references in one keyed store query."""
    from . import semantic_units
    from .vault_store import mode, read

    paths = tuple(paths)
    if not paths or not mode.store_mode_for_reads(root):
        return {}
    found: dict[str, list[tuple[str, str]]] = {}
    with read.snapshot(root) as view:
        for path, source_ref, source, target_ref, target in relationships.same_page_pairs(
            view.connection, paths, registry.keys_with("replaces"),
        ):
            found.setdefault(path, []).append((
                semantic_units._anchored_unit_ref(source_ref, source),
                semantic_units._anchored_unit_ref(target_ref, target),
            ))
    return found


def planned_supersessions(
    facts: Sequence[semantic_contract.RelationFact],
    path: str,
    registry: relation_registry.RelationRegistry,
) -> tuple[tuple[str, str], ...]:
    """Use the preflight overlay for lifecycle history before the transaction commits."""
    return tuple(
        (fact.logical_source_ref, fact.logical_target_ref)
        for fact in facts
        if fact.logical_source_path == fact.logical_target_path == path
        and fact.logical_source_ref and fact.logical_target_ref
        and registry.has(fact.canonical_relation or "", "replaces")
    )


def add_unit_edges(current: Iterable[Edge], added: Iterable[Edge]) -> tuple[Edge, ...]:
    """The unit's edges after `added`: the current ones first, each distinct edge once, in order."""
    return tuple(dict.fromkeys((*current, *added)))


def supersession_edge(registry: relation_registry.RelationRegistry, anchor: str) -> Edge:
    """The edge by which a successor replaces the unit `anchor` on the same page.

    The relation is the one shipped relation with the `replaces` behaviour, read from the registry
    rather than named here, so the key lives only in the pack. The shipped pack has exactly one;
    any other count is a defect in the pack.
    """
    keys = sorted(registry.keys_with("replaces") & set(registry.core))
    if len(keys) != 1:
        raise ValueError(f"the relation pack must ship one replacing relation, found {keys}")
    return keys[0], f"[[#{anchor}]]"
