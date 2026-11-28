"""Typed edges from governed unit operations and their semantic write view."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

from . import relation_registry
from .vault_store import relationships

if TYPE_CHECKING:
    from . import semantic_contract, vault

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


def relationship_page(
    root: Path, path: str, anchor: str, *,
    limit: int = 50, after: tuple[str, str] | None = None,
) -> RelationshipPage:
    """Read an exact visible page and count in one canonical snapshot."""
    from . import epistemic_graph, request_budget
    from .governance import egress
    from .vault_store import mode, read

    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("relationship page limit must be between 1 and 50")
    if not mode.store_mode_for_reads(root):
        return RelationshipPage()
    keep = egress.visible_page_filter(root)
    with read.request_snapshot(root), read.snapshot(root) as snapshot:
        identity = relationships.identities_at(snapshot.connection, {(path, anchor)}).get((path, anchor))
        if identity is None:
            return RelationshipPage(total=0)
        source_view = epistemic_graph.GraphView(root, snapshot.connection, keep=keep)
        source = source_view.parent(path)
        if (source is None or source.structure.parent_ref != identity.parent_ref
                or source.structure.resolve_fragment(anchor).index is None):
            return RelationshipPage(total=0)
        found = []
        total = 0
        remaining = False
        candidates = relationships.outgoing_unit(snapshot.connection, identity)
        while batch := tuple(islice(candidates, read.KEYS_PER_QUERY)):
            request_budget.checkpoint()
            view = epistemic_graph.GraphView(root, snapshot.connection, keep=keep)
            view.prefetch(edge.target_path for edge in batch)
            for edge in batch:
                target = view.parent(edge.target_path)
                if target is None:
                    continue
                if edge.target is not None:
                    if (target.structure.parent_ref != edge.target.parent_ref
                            or target.structure.resolve_fragment(edge.target.anchor).index is None):
                        continue
                elif edge.relationship.target_page_ref is not None and (
                    target.structure.parent_ref != edge.relationship.target_page_ref
                ):
                    continue
                total += 1
                key = _relationship_key(edge)
                if after is None or key > after:
                    if len(found) < limit:
                        found.append(edge)
                    else:
                        remaining = True
        return RelationshipPage(tuple(found), total, _relationship_key(found[-1]) if remaining else None)


def _relationship_key(edge: relationships.BoundRelationship) -> tuple[str, str]:
    return edge.relationship.relation_type, edge.relationship.id


def plan_change(
    root: Path, *, source: End, targets: tuple[tuple[str, End], ...],
    visible: Callable[[str], bool] | None, replacing: bool, reason: str | None,
    writes: tuple[vault.PlannedWrite, ...], guards: tuple[vault.PathGuard, ...],
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
        outgoing = tuple(edge for edge in relationships.incident(view.connection, source.path)
                         if edge.source == existing.get((source.path, source.anchor)))

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
    # Preview and commit may allocate different IDs for new ends. Pin the locator and guarded
    # versions, plus the exact active edges this operation would retire.
    intent = json.dumps([
        source.path, source.anchor,
        sorted(((kind, end.path, end.anchor, end.parent_ref) for kind, end in targets), key=repr),
        sorted(edge.id for edge in retired),
        sorted((guard.target, guard.expected_content_hash) for guard in guards),
    ], separators=(",", ":"))
    digest = hashlib.sha256(intent.encode()).hexdigest()
    expected = (source_id, frozenset(edge.relationship.id for edge in outgoing)) if replacing else None
    change = relationships.Change(
        effective_principal().audience_id, tuple(allocated.values()), added, retired, reason, expected,
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
    for item in stored:
        source = corpus.pages.get(item.source.parent_path)
        target = corpus.pages.get(item.target_path)
        if source is None or target is None:
            continue
        source_unit = _bound_unit(source, item.source)
        if source_unit is None:
            continue
        target_unit = None
        if item.target is not None:
            target_unit = _bound_unit(target, item.target)
            if target_unit is None:
                continue
        elif (reference := item.relationship.target_page_ref) and reference != target.document.parent_ref:
            continue
        facts.append(semantic_contract.stored_relationship_fact(
            corpus, relationship_id=item.relationship.id, source=source, source_unit=source_unit,
            relation_type=item.relationship.relation_type, target=target, target_unit=target_unit,
        ))
    return tuple(facts)


def _bound_unit(page: semantic_contract.SemanticPageState, identity: relationships.UnitIdentity):
    if identity.parent_ref is not None and identity.parent_ref != page.document.parent_ref:
        return None
    unit = page.document.resolve_fragment(identity.anchor).unit
    return unit if unit is not None and unit.anchor == identity.anchor else None


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
