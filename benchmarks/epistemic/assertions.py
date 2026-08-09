"""The 18 pre-registered assertions, as deterministic functions over snapshots.

Every assertion takes an :class:`AssertionContext` — one snapshot, or a
snapshot pair for the transition invariants — and returns an
:class:`AssertionResult`. Nothing here reads a clock, a network, or a provider
internal, so a result is a pure function of the fixture that produced it.

Three rules hold across all eighteen:

1. **Acceptance predicates, not implementations.** PREREGISTRATION §4 requires
   at least two structurally different representations to satisfy each
   invariant. The alternates are tried in order and the winning representation
   is named in the evidence string, so a pass says *how* the product satisfied
   the invariant rather than merely that it did.

2. **Absence is typed, never zero.** ``not_applicable`` comes only from a
   capability declaration of ``absent_by_design``; a projector that cannot
   observe the field at all yields ``unsupported``. This mirrors the three
   honesty tiers in :mod:`membench.scoring.health`, where an underivable
   measurement stays ``None`` rather than becoming a zero. A product whose own
   materials claim the property scores ``fail`` with the claim cited, never
   ``not_applicable``.

3. **One text-matching rule.** Where an assertion has to ask whether some text
   states a value, it calls :func:`membench.scoring.gates.states_value` — the
   harness's single fixed matching rule — and never bare substring containment.
   Structural comparisons (did this item's content change?) use a content hash,
   which is identity rather than text matching.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from membench.scoring.gates import states_value
from pydantic import Field

from .snapshot import EpistemicStateSnapshot, StateItem, StrictModel

#: The five-valued outcome vocabulary from the spec.
Outcome = Literal["pass", "fail", "not_applicable", "unsupported", "blocked"]

#: Item kinds that assert something and therefore owe an evidence path.
BELIEF_KINDS: frozenset[str] = frozenset(
    {"claim", "decision", "hypothesis", "derived_inference"}
)

#: Edges that count as an evidence hop, in PROV-O terms.
EVIDENCE_PREDICATES: frozenset[str] = frozenset({"cites", "derived_from", "evidenced_by"})

#: Review vocabularies. Providers name these differently, so the comparison is
#: over a normalized token rather than an exact string.
CLOSED_REVIEW_STATES: frozenset[str] = frozenset(
    {"closed", "dismissed", "resolved", "snoozed", "accepted", "done", "ignored"}
)
OPEN_REVIEW_STATES: frozenset[str] = frozenset(
    {"open", "reopened", "needs_review", "needs-review", "pending", "conflict", "flagged"}
)
#: Review states that mark a recorded conflict. A *closed vocabulary*, not a
#: text match: ``states_value("conflict", "no conflict")`` is true, so matching
#: the word inside a review-state field turned an explicit "no conflict" into
#: evidence of a visible contradiction.
CONFLICT_REVIEW_STATES: frozenset[str] = frozenset(
    {"conflict", "conflicted", "conflicting", "contradiction", "contradicted", "disputed"}
)
#: Review states that declare unresolved confidence.
UNCERTAIN_REVIEW_STATES: frozenset[str] = frozenset(
    {"uncertain", "unverified", "unconfirmed", "provisional"}
)

#: Collection/folder path segments that name a settled decision, and ones that
#: name something still open. Closed vocabularies, matched case-insensitively
#: against a single path segment — the same discipline as the review-state
#: vocabularies above and for the same reason: two items merely filed in
#: *different* folders say nothing about which one is decided, so accepting any
#: difference turned the documented-convention alternate into a diff detector.
DECISION_COLLECTION_TERMS: frozenset[str] = frozenset({"decision", "decisions"})
HYPOTHESIS_COLLECTION_TERMS: frozenset[str] = frozenset(
    {"hypothesis", "hypotheses", "proposal", "proposals"}
)

#: Cap on how many offenders one evidence string lists (as the gates do).
_MAX_LISTED = 8


class AssertionResult(StrictModel):
    """One deterministic verdict. Final by contract: no judge may overturn it."""

    name: str = Field(min_length=1)
    outcome: Outcome
    evidence: str = Field(min_length=1)
    subject: str | None = None


@dataclass(frozen=True)
class AssertionContext:
    """Everything an assertion may read.

    ``snapshot`` is the snapshot under test. ``prior`` is the earlier snapshot
    for transition invariants (and the *live* snapshot when the assertion
    compares an export against it). Every parameter is caller-supplied so a
    result never depends on ambient state.
    """

    snapshot: EpistemicStateSnapshot
    prior: EpistemicStateSnapshot | None = None
    subject: str | None = None
    counterpart: str | None = None
    #: Ids a retrieval surface actually served, when the scenario captured them.
    served_items: tuple[str, ...] | None = None
    #: Foreign-case canary hits from the isolation probe; ``None`` = not run.
    foreign_case_hits: tuple[str, ...] | None = None
    #: Declared freshness bound for external-edit adoption, in seconds.
    freshness_bound_s: float | None = None
    #: When the out-of-band edit happened, RFC3339, caller-supplied.
    external_edit_at: str | None = None
    #: Allowed reconstruction loss for the export comparison, 0.0 - 1.0.
    tolerance: float = 0.0

    def replace(self, **changes: object) -> "AssertionContext":
        return replace(self, **changes)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Result helpers
# --------------------------------------------------------------------------


def _result(name: str, outcome: Outcome, evidence: str, subject: str | None) -> AssertionResult:
    return AssertionResult(name=name, outcome=outcome, evidence=evidence, subject=subject)


def _listed(values: Iterable[str]) -> str:
    ordered = sorted(values)
    head = ordered[:_MAX_LISTED]
    suffix = f" (+{len(ordered) - _MAX_LISTED} more)" if len(ordered) > _MAX_LISTED else ""
    return ", ".join(head) + suffix


def _content_hash(item: StateItem) -> str:
    """Identity of an item's material content; not a text-matching rule."""

    payload = f"{item.title}\x00{item.text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_state(state: str | None) -> str:
    return (state or "").strip().casefold()


def _is_closed(state: str | None) -> bool:
    return _normalize_state(state) in CLOSED_REVIEW_STATES


def _is_open(state: str | None) -> bool:
    return _normalize_state(state) in OPEN_REVIEW_STATES


def _is_uncertain(state: str | None) -> bool:
    return _normalize_state(state) in UNCERTAIN_REVIEW_STATES


def _conflict_marker(item: StateItem) -> bool:
    """A conflict marker on the item, read from the closed review vocabulary."""

    return _normalize_state(item.review_state) in CONFLICT_REVIEW_STATES


def _folder(locator: str | None) -> str:
    if not locator or "/" not in locator:
        return ""
    return locator.rsplit("/", 1)[0]


def _collection_terms(locator: str | None) -> tuple[bool, bool]:
    """``(names a decision collection, names a hypothesis/proposal collection)``.

    Reads the *segments* of the containing folder against the closed
    vocabularies, case-insensitively. Segment-level matching is deliberate: it
    keeps ``Notes/Decisions`` a hit while leaving ``Notes/Decision-Log-Archive``
    a miss rather than guessing at substrings.
    """

    segments = {
        segment.strip().casefold()
        for segment in _folder(locator).split("/")
        if segment.strip()
    }
    return (
        bool(segments & DECISION_COLLECTION_TERMS),
        bool(segments & HYPOTHESIS_COLLECTION_TERMS),
    )


def _elapsed_seconds(start: str, end: str) -> float | None:
    try:
        first = datetime.fromisoformat(start)
        second = datetime.fromisoformat(end)
    except ValueError:
        return None
    if (first.tzinfo is None) != (second.tzinfo is None):
        return None
    return (second - first).total_seconds()


# --------------------------------------------------------------------------
# Capability gate: three honesty tiers before any structural evaluation
# --------------------------------------------------------------------------


def _absence_result(name: str, declaration, subject: str | None) -> AssertionResult:
    """``not_applicable`` for a designed absence — or ``fail`` if it is claimed."""

    if declaration.marketing_claim:
        return _result(
            name,
            "fail",
            f"'{declaration.field}' is declared absent_by_design, but the product's own "
            f"materials claim it: {declaration.marketing_claim} "
            f"(declaration evidence: {declaration.evidence})",
            subject,
        )
    return _result(
        name,
        "not_applicable",
        f"'{declaration.field}' declared absent_by_design ({declaration.evidence})",
        subject,
    )


def _gate(
    ctx: AssertionContext,
    name: str,
    primary: str,
    *siblings: str,
) -> AssertionResult | None:
    """``None`` when the invariant is observable; otherwise the honest non-pass.

    Every assertion names one **primary** field — the capability the invariant
    is actually about — plus any number of siblings that can widen how it is
    observed. The asymmetry is deliberate and was a real bug before it existed:
    with a flat OR over fields, a product that declared ``external_edit``
    ``absent_by_design`` still got evaluated (and could take a *catastrophic
    failure*) purely because the unrelated ``locator`` field happened to be
    declared. A designed absence of the primary field now short-circuits, and
    no sibling can override it.

    Siblings only ever act in the observable direction: they can make an
    otherwise unobservable invariant evaluable, never the reverse.

    Otherwise this mirrors :mod:`membench.scoring.health` — declared capability
    is evaluated, designed absence is ``not_applicable``, anything the projector
    cannot see is ``unsupported``, and nothing silently becomes a zero.
    """

    fields = (primary, *siblings)
    primary_declaration = ctx.snapshot.declaration(primary)

    # A designed absence of the primary capability decides on its own.
    if primary_declaration is not None and primary_declaration.status == "absent_by_design":
        return _absence_result(name, primary_declaration, ctx.subject)

    declarations = [
        declaration
        for declaration in (ctx.snapshot.declaration(field) for field in fields)
        if declaration is not None
    ]
    if any(declaration.observable for declaration in declarations):
        return None

    by_design = [d for d in declarations if d.status == "absent_by_design"]
    claimed = [d for d in by_design if d.marketing_claim]
    if claimed:
        return _absence_result(name, claimed[0], ctx.subject)
    if by_design:
        return _absence_result(name, by_design[0], ctx.subject)
    if declarations:
        declaration = declarations[0]
        return _result(
            name,
            "unsupported",
            f"'{declaration.field}' declared unavailable to the projector "
            f"({declaration.evidence})",
            ctx.subject,
        )
    return _result(
        name,
        "unsupported",
        f"no capability declaration for {', '.join(fields)}; "
        "an undeclared field is unsupported, never a zero",
        ctx.subject,
    )


# --------------------------------------------------------------------------
# Lineage helpers
# --------------------------------------------------------------------------


def _superseded_ids(snapshot: EpistemicStateSnapshot) -> set[str]:
    superseded = {item.revision_of for item in snapshot.items if item.revision_of}
    superseded |= {
        relation.object for relation in snapshot.relations if relation.predicate == "supersedes"
    }
    return {value for value in superseded if value}


def _union_roots(snapshot: EpistemicStateSnapshot) -> dict[str, str]:
    """item id -> lineage root, unioned over ``revision_of`` and supersedes edges."""

    parent: dict[str, str] = {item.id: item.id for item in snapshot.items}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    known = set(parent)
    for item in snapshot.items:
        if item.revision_of and item.revision_of in known:
            union(item.id, item.revision_of)
    for relation in snapshot.relations:
        if (
            relation.predicate == "supersedes"
            and relation.subject in known
            and relation.object in known
        ):
            union(relation.subject, relation.object)
    return {item.id: find(item.id) for item in snapshot.items}


def _revision_groups(snapshot: EpistemicStateSnapshot) -> dict[str, tuple[StateItem, ...]]:
    """Group items into revision chains, keyed on the lineage root.

    Keyed on the *root*, never on the declared ``revision_chain_id``: two
    genuinely independent chains that both label themselves with the same chain
    id used to collide in this dict, and the later one silently overwrote the
    earlier — which meant a chain with two current revisions could vanish from
    the evaluation entirely. The declared chain id is still honoured for
    *subject matching* in :func:`exactly_one_current_revision`, where it is a
    label the scenario can name, not an identity the engine trusts.
    """

    roots = _union_roots(snapshot)
    by_root: dict[str, list[StateItem]] = {}
    for item in snapshot.items:
        by_root.setdefault(roots[item.id], []).append(item)

    groups: dict[str, tuple[StateItem, ...]] = {}
    for root, members in by_root.items():
        is_chain = len(members) > 1 or any(
            m.revision_of or m.retired_reason or m.revision_chain_id for m in members
        )
        if not is_chain:
            continue
        groups[root] = tuple(sorted(members, key=lambda m: m.id))
    return groups


def _lineage_members(
    snapshot: EpistemicStateSnapshot, item_id: str | None
) -> tuple[StateItem, ...]:
    """Items sharing ``item_id``'s lineage (itself included); empty if unknown."""

    if not item_id:
        return ()
    roots = _union_roots(snapshot)
    root = roots.get(item_id)
    if root is None:
        return ()
    return tuple(item for item in snapshot.items if roots[item.id] == root)


def _evidence_targets(snapshot: EpistemicStateSnapshot, item: StateItem) -> tuple[str, ...]:
    targets = list(item.cites)
    for relation in snapshot.relations:
        if relation.subject == item.id and relation.predicate in EVIDENCE_PREDICATES:
            targets.append(relation.object)
    seen: list[str] = []
    for target in targets:
        if target not in seen:
            seen.append(target)
    return tuple(seen)


def _outgoing_edges(
    snapshot: EpistemicStateSnapshot, item_id: str
) -> frozenset[tuple[str, str]]:
    """``(predicate, object)`` for every edge leaving ``item_id``."""

    return frozenset(
        (relation.predicate, relation.object)
        for relation in snapshot.relations
        if relation.subject == item_id
    )


def _belief_items(ctx: AssertionContext) -> tuple[StateItem, ...]:
    items = tuple(
        item
        for item in ctx.snapshot.items
        if item.kind in BELIEF_KINDS and item.current != "no"
    )
    if ctx.subject:
        items = tuple(item for item in items if item.id == ctx.subject)
    return items


def _pair(ctx: AssertionContext) -> tuple[StateItem | None, StateItem | None]:
    left = ctx.snapshot.item(ctx.subject) if ctx.subject else None
    right = ctx.snapshot.item(ctx.counterpart) if ctx.counterpart else None
    return left, right


def _declared_but_unobservable(
    ctx: AssertionContext, name: str, *, check_counterpart: bool = False
) -> AssertionResult | None:
    """``unsupported`` when a declared subject/counterpart is not in the snapshot.

    "Nothing was declared" and "what was declared is not there" are different
    facts, and every assertion that collapsed them leaked the same way: the
    unresolvable name fell through to a snapshot-wide reading, or to some
    substituted item, and answered a question about state nobody could observe.
    Once with a catastrophic assertion (see R-B1b) and twice more besides.

    So a declared name that does not resolve is reported as exactly that. This
    is also the only honest answer: an id-shape mismatch between a projector
    (vault-relative paths) and a scenario fixture (logical ids) is a harness
    problem, and scoring it as either a pass or a fail would attribute a
    harness problem to the provider.

    ``check_counterpart`` is opt-in because the counterpart does not mean the
    same thing everywhere. Where it names a co-equal item under comparison, an
    absent one is unobservable. Where it names an item whose *absence is the
    finding* — ``prior_revision_retained``'s declared predecessor — this guard
    must stay off, or it would convert destroyed history into ``unsupported``.
    """

    candidates = [("subject", ctx.subject)]
    if check_counterpart:
        candidates.append(("counterpart", ctx.counterpart))
    for label, declared in candidates:
        if declared and ctx.snapshot.item(declared) is None:
            return _result(
                name,
                "unsupported",
                f"declared {label} {declared!r} is not observable in this snapshot",
                ctx.subject,
            )
    return None


# --------------------------------------------------------------------------
# 1-2: currency
# --------------------------------------------------------------------------


def exactly_one_current_revision(ctx: AssertionContext) -> AssertionResult:
    """Each revision chain exposes exactly one current revision."""

    name = "exactly_one_current_revision"
    gated = _gate(ctx, name, "current", "revision_of")
    if gated is not None:
        return gated

    groups = _revision_groups(ctx.snapshot)
    if ctx.subject:
        # The subject may name a lineage root, a member item, or the chain id
        # the provider declared. All three are legitimate scenario handles.
        groups = {
            key: members
            for key, members in groups.items()
            if key == ctx.subject
            or any(
                member.id == ctx.subject or member.revision_chain_id == ctx.subject
                for member in members
            )
        }
    if not groups:
        return _result(
            name,
            "unsupported",
            "no revision lineage observed; currency of a chain cannot be evaluated",
            ctx.subject,
        )

    offenders = []
    for key in sorted(groups):
        current = [member.id for member in groups[key] if member.current == "yes"]
        if len(current) != 1:
            offenders.append(f"{key}: {len(current)} current [{_listed(current) or 'none'}]")
    if offenders:
        return _result(name, "fail", f"chains without a single current revision: {'; '.join(offenders)}", ctx.subject)
    return _result(
        name,
        "pass",
        f"{len(groups)} revision chain(s) each expose exactly one current revision: {_listed(groups)}",
        ctx.subject,
    )


def no_retired_state_served_as_current(ctx: AssertionContext) -> AssertionResult:
    """Retired state is never served as, or marked, current. CATASTROPHIC."""

    name = "no_retired_state_served_as_current"
    gated = _gate(ctx, name, "current")
    if gated is not None:
        return gated

    snapshot = ctx.snapshot
    by_id = snapshot.items_by_id()
    superseded = _superseded_ids(snapshot)

    def retired(item: StateItem) -> bool:
        return item.current == "no" or bool(item.retired_reason) or item.id in superseded

    if ctx.served_items is not None:
        offenders: list[str] = []
        unresolved: list[str] = []
        for served in ctx.served_items:
            item = by_id.get(served)
            if item is None:
                unresolved.append(served)
                continue
            if retired(item):
                offenders.append(
                    f"{served} (current={item.current}, retired_reason={item.retired_reason!r})"
                )
        if offenders:
            return _result(
                name,
                "fail",
                f"retrieval served retired state: {_listed(offenders)}",
                ctx.subject,
            )
        note = f"; {len(unresolved)} served id(s) not present in the snapshot" if unresolved else ""
        return _result(
            name,
            "pass",
            f"{len(ctx.served_items)} served item(s) are all current{note}",
            ctx.subject,
        )

    if not snapshot.items:
        return _result(name, "unsupported", "empty snapshot; nothing observable to serve", ctx.subject)
    # ``undeclared`` counts alongside ``yes``: a retired item whose currency the
    # product never states is still offered as though it were live, and letting
    # silence exempt it would reward not answering the question.
    offenders = [
        f"{item.id} (current={item.current}, retired_reason={item.retired_reason!r})"
        for item in snapshot.items
        if item.current in {"yes", "undeclared"}
        and (item.retired_reason or item.id in superseded)
    ]
    if offenders:
        return _result(
            name,
            "fail",
            f"retired state is not marked retired: {_listed(offenders)}",
            ctx.subject,
        )
    return _result(
        name,
        "pass",
        "no served-item list captured; every retired or superseded item is marked "
        "non-current",
        ctx.subject,
    )


# --------------------------------------------------------------------------
# 3-4: lineage
# --------------------------------------------------------------------------


def prior_revision_retained(ctx: AssertionContext) -> AssertionResult:
    """History survives a correction. CATASTROPHIC.

    Accepts a resolving revision chain, a retained superseded artifact *in the
    subject's own lineage*, or a documented ``available_via:<mechanism>``
    history surface.

    The lineage scoping is the whole point. An earlier build accepted any
    retired item anywhere in the snapshot as proof that history was retained,
    which meant a product that destroyed the predecessor of the corrected
    conclusion still passed a catastrophic invariant because some unrelated
    note happened to be archived. Retention is a claim about *this* subject's
    predecessor, so the retained artifact must be reachable from the subject:
    same lineage (``revision_of`` / supersedes edge / shared chain id), or the
    predecessor the scenario itself declared via ``counterpart``.

    A ``revision_of`` that no longer dereferences is a **failure**, not a
    fall-through to the weaker alternates: the product recorded that it had a
    predecessor and then lost it, which is exactly the harm being measured.
    """

    name = "prior_revision_retained"
    gated = _gate(ctx, name, "prior_revision", "revision_of", "current")
    if gated is not None:
        return gated

    snapshot = ctx.snapshot
    by_id = snapshot.items_by_id()
    subject = snapshot.item(ctx.subject) if ctx.subject else None

    # An unresolvable subject must not fall through to the snapshot-wide
    # widening below — that was R-B1b, and it handed a catastrophic pass off any
    # retired item anywhere. The counterpart is deliberately NOT checked here:
    # for this assertion it names the declared predecessor, whose absence is the
    # finding rather than a reason to stop scoring.
    unobservable = _declared_but_unobservable(ctx, name)
    if unobservable is not None:
        return unobservable

    successors = (
        (subject,)
        if subject is not None
        else tuple(item for item in snapshot.items if item.revision_of)
    )
    for item in successors:
        if item is None or not item.revision_of:
            continue
        if item.revision_of in by_id:
            return _result(
                name,
                "pass",
                f"revision chain: {item.id} links to retained predecessor {item.revision_of}",
                ctx.subject,
            )
        return _result(
            name,
            "fail",
            f"history destroyed: {item.id} declares revision_of "
            f"{item.revision_of!r}, which no longer resolves to any item",
            ctx.subject,
        )

    # Representation #2, scoped: a retained superseded artifact that the subject
    # can actually be connected to.
    lineage = {member.id for member in _lineage_members(snapshot, ctx.subject)}
    if not ctx.subject:
        # Only an *undeclared* subject widens to every revision group. Past this
        # point an unresolvable subject has already returned unsupported.
        lineage = {
            member.id
            for members in _revision_groups(snapshot).values()
            for member in members
        }
    if ctx.counterpart:
        lineage.add(ctx.counterpart)
    lineage.discard(ctx.subject or "")
    retained = [
        item.id
        for item in snapshot.items
        if item.id in lineage and (item.current == "no" or item.retired_reason)
    ]
    if retained:
        scope = "the subject's lineage" if ctx.subject else "an observed revision lineage"
        return _result(
            name,
            "pass",
            f"retained superseded artifact(s) in {scope}: {_listed(retained)}",
            ctx.subject,
        )

    for field in ("prior_revision", "revision_of"):
        declaration = snapshot.declaration(field)
        if declaration is not None and declaration.mechanism:
            return _result(
                name,
                "pass",
                f"documented history mechanism available_via:{declaration.mechanism} "
                f"for '{field}' ({declaration.evidence})",
                ctx.subject,
            )

    return _result(
        name,
        "fail",
        "no prior revision retained for this lineage: no resolving revision chain, no "
        "retained superseded artifact reachable from the subject, and no declared "
        "history mechanism",
        ctx.subject,
    )


def revision_links_to_predecessor(ctx: AssertionContext) -> AssertionResult:
    """The current revision names what it replaced.

    Accepts an explicit supersession edge, an in-content reference naming the
    replaced item, or a version chain (chain id plus index).
    """

    name = "revision_links_to_predecessor"
    gated = _gate(ctx, name, "revision_of", "prior_revision")
    if gated is not None:
        return gated

    # A declared subject that does not resolve used to be silently replaced by
    # "the first item with a revision_of, else the first current item" — so the
    # verdict was about an item the scenario never named, and could be a pass.
    unobservable = _declared_but_unobservable(ctx, name)
    if unobservable is not None:
        return unobservable

    snapshot = ctx.snapshot
    subject = snapshot.item(ctx.subject) if ctx.subject else None
    if subject is None:
        # Nothing declared: picking the observed successor is the intended wide
        # reading, not a substitution.
        successors = [item for item in snapshot.items if item.revision_of] or [
            item for item in snapshot.items if item.current == "yes"
        ]
        subject = successors[0] if successors else None
    if subject is None:
        return _result(name, "unsupported", "no current revision observed", ctx.subject)

    if subject.revision_of:
        return _result(
            name,
            "pass",
            f"explicit supersession edge: {subject.id} revision_of {subject.revision_of}",
            subject.id,
        )
    for relation in snapshot.relations:
        if relation.predicate == "supersedes" and relation.subject == subject.id:
            return _result(
                name,
                "pass",
                f"typed supersedes relation: {subject.id} -> {relation.object}",
                subject.id,
            )

    # The in-content alternate only counts when the named item could actually be
    # this subject's predecessor. Scanning every retired item in the snapshot
    # let a body that happened to mention an unrelated archived note pass as a
    # supersession reference.
    predecessors: list[StateItem] = []
    if ctx.counterpart:
        candidate = snapshot.item(ctx.counterpart)
        if candidate is not None:
            predecessors.append(candidate)
    else:
        predecessors.extend(
            member
            for member in _lineage_members(snapshot, subject.id)
            if member.id != subject.id and (member.current == "no" or member.retired_reason)
        )
    for predecessor in predecessors:
        for token in (predecessor.title, predecessor.id):
            if token and states_value(token, subject.text):
                return _result(
                    name,
                    "pass",
                    f"in-content reference: {subject.id} names replaced item {token!r}",
                    subject.id,
                )

    if subject.revision_chain_id and subject.revision_index is not None:
        siblings = [
            item.id
            for item in snapshot.items
            if item.id != subject.id and item.revision_chain_id == subject.revision_chain_id
        ]
        if siblings:
            return _result(
                name,
                "pass",
                f"version chain {subject.revision_chain_id} index {subject.revision_index} "
                f"with sibling revision(s) {_listed(siblings)}",
                subject.id,
            )

    return _result(
        name,
        "fail",
        f"{subject.id} names no predecessor: no supersession edge, no in-content "
        "reference, and no version chain",
        subject.id,
    )


# --------------------------------------------------------------------------
# 5-6: evidence paths
# --------------------------------------------------------------------------


def evidence_path_exists(ctx: AssertionContext) -> AssertionResult:
    """Every promoted conclusion has at least one evidence hop."""

    name = "evidence_path_exists"
    gated = _gate(ctx, name, "cites")
    if gated is not None:
        return gated

    beliefs = _belief_items(ctx)
    if not beliefs:
        return _result(name, "unsupported", "no belief-bearing items observed", ctx.subject)
    missing = [item.id for item in beliefs if not _evidence_targets(ctx.snapshot, item)]
    if missing:
        return _result(
            name, "fail", f"conclusions with no evidence path: {_listed(missing)}", ctx.subject
        )
    return _result(
        name,
        "pass",
        f"{len(beliefs)} conclusion(s) each carry an evidence path",
        ctx.subject,
    )


def evidence_path_resolves(ctx: AssertionContext) -> AssertionResult:
    """Every hop dereferences to a live artifact. CATASTROPHIC.

    Vacuity is a failure here on purpose: erasing the evidence edges must not
    be a cheaper way to satisfy a catastrophic invariant than keeping them.
    """

    name = "evidence_path_resolves"
    gated = _gate(ctx, name, "cites")
    if gated is not None:
        return gated

    beliefs = _belief_items(ctx)
    if not beliefs:
        return _result(name, "unsupported", "no belief-bearing items observed", ctx.subject)

    snapshot = ctx.snapshot
    by_id = snapshot.items_by_id()
    dangling: list[str] = []
    empty: list[str] = []
    for belief in beliefs:
        seen: set[str] = {belief.id}
        frontier = list(_evidence_targets(snapshot, belief))
        if not frontier:
            empty.append(belief.id)
            continue
        while frontier:
            target = frontier.pop()
            if target in seen:
                continue
            seen.add(target)
            item = by_id.get(target)
            if item is None:
                dangling.append(f"{belief.id} -> {target}")
                continue
            frontier.extend(_evidence_targets(snapshot, item))
    if dangling or empty:
        parts = []
        if dangling:
            parts.append(f"unresolvable hops: {_listed(dangling)}")
        if empty:
            parts.append(f"conclusions with no path to dereference: {_listed(empty)}")
        return _result(name, "fail", "; ".join(parts), ctx.subject)
    return _result(
        name,
        "pass",
        f"every evidence hop from {len(beliefs)} conclusion(s) dereferences to a live item",
        ctx.subject,
    )


# --------------------------------------------------------------------------
# 7-8: contradiction
# --------------------------------------------------------------------------


def contradiction_visible(ctx: AssertionContext) -> AssertionResult:
    """A conflict is observable.

    Accepts a typed contradiction relation, both sides current with a conflict
    marker, or a review-queue entry citing both sides.
    """

    name = "contradiction_visible"
    gated = _gate(ctx, name, "contradicts")
    if gated is not None:
        return gated

    snapshot = ctx.snapshot
    left, right = _pair(ctx)
    if ctx.subject and ctx.counterpart:
        pair_ids = {ctx.subject, ctx.counterpart}
        if left is not None and ctx.counterpart in left.contradicts:
            return _result(name, "pass", f"typed contradiction edge {left.id} -> {ctx.counterpart}", ctx.subject)
        if right is not None and ctx.subject in right.contradicts:
            return _result(name, "pass", f"typed contradiction edge {right.id} -> {ctx.subject}", ctx.subject)
        for relation in snapshot.relations:
            if relation.predicate == "contradicts" and {relation.subject, relation.object} == pair_ids:
                return _result(
                    name, "pass", f"typed contradicts relation between {_listed(pair_ids)}", ctx.subject
                )
        if (
            left is not None
            and right is not None
            and left.current == "yes"
            and right.current == "yes"
            and (_conflict_marker(left) or _conflict_marker(right))
        ):
            return _result(
                name,
                "pass",
                f"both sides current with a conflict marker ({_listed(pair_ids)})",
                ctx.subject,
            )
        for item in snapshot.items:
            if _conflict_marker(item) and pair_ids <= set(item.cites):
                return _result(
                    name,
                    "pass",
                    f"review-queue conflict entry {item.id} cites both sides",
                    ctx.subject,
                )
        return _result(
            name,
            "fail",
            f"no contradiction representation for {_listed(pair_ids)}: no typed edge, "
            "no conflict marker on co-current items, no review-queue entry",
            ctx.subject,
        )

    edges = [item.id for item in snapshot.items if item.contradicts]
    edges += [
        relation.subject for relation in snapshot.relations if relation.predicate == "contradicts"
    ]
    markers = [item.id for item in snapshot.items if _conflict_marker(item)]
    if edges or markers:
        return _result(
            name,
            "pass",
            f"contradiction representation present (edges: {_listed(set(edges)) or 'none'}; "
            f"markers: {_listed(set(markers)) or 'none'})",
            ctx.subject,
        )
    return _result(name, "fail", "no contradiction edge or conflict marker in state", ctx.subject)


def contradiction_not_flattened(ctx: AssertionContext) -> AssertionResult:
    """Neither side of a conflict is silently gone. CATASTROPHIC."""

    name = "contradiction_not_flattened"
    gated = _gate(ctx, name, "contradicts", "current")
    if gated is not None:
        return gated

    snapshot = ctx.snapshot
    if ctx.subject and ctx.counterpart:
        left, right = _pair(ctx)
        if left is None and right is None:
            return _result(
                name, "unsupported", "neither side of the declared pair is observable", ctx.subject
            )
        if left is None or right is None:
            missing = ctx.subject if left is None else ctx.counterpart
            survivor = right if left is None else left
            assert survivor is not None
            return _result(
                name,
                "fail",
                f"flattened: {missing} is absent while {survivor.id} remains "
                f"(current={survivor.current}) with no conflict record",
                ctx.subject,
            )
        if left.current == "yes" and right.current == "yes":
            return _result(
                name,
                "pass",
                f"both sides retrievable as current ({left.id}, {right.id})",
                ctx.subject,
            )
        edge = (
            right.id in left.contradicts
            or left.id in right.contradicts
            or any(
                relation.predicate == "contradicts"
                and {relation.subject, relation.object} == {left.id, right.id}
                for relation in snapshot.relations
            )
        )
        retired = [item for item in (left, right) if item.current == "no"]
        if retired and (retired[0].retired_reason or edge):
            return _result(
                name,
                "pass",
                f"{retired[0].id} retired with a recorded reason/edge; both sides retained",
                ctx.subject,
            )
        return _result(
            name,
            "fail",
            f"one side settled without a conflict record ({left.id} current={left.current}, "
            f"{right.id} current={right.current})",
            ctx.subject,
        )

    by_id = snapshot.items_by_id()
    edges: list[tuple[str, str]] = []
    for item in snapshot.items:
        edges.extend((item.id, target) for target in item.contradicts)
    edges.extend(
        (relation.subject, relation.object)
        for relation in snapshot.relations
        if relation.predicate == "contradicts"
    )
    if not edges:
        return _result(name, "unsupported", "no contradiction edge observed", ctx.subject)
    dangling = [f"{a} -> {b}" for a, b in edges if b not in by_id or a not in by_id]
    if dangling:
        return _result(
            name, "fail", f"contradiction edges with a vanished side: {_listed(dangling)}", ctx.subject
        )
    return _result(
        name, "pass", f"{len(edges)} contradiction edge(s) retain both sides", ctx.subject
    )


# --------------------------------------------------------------------------
# 9-11: typing, questions, uncertainty
# --------------------------------------------------------------------------


def decision_distinguishable_from_hypothesis(ctx: AssertionContext) -> AssertionResult:
    """A fresh agent can tell a settled decision from a live hypothesis.

    Accepts a type/kind field, a documented collection convention visible in
    the locator, or a schema/metadata attribute.
    """

    name = "decision_distinguishable_from_hypothesis"
    gated = _gate(ctx, name, "kind")
    if gated is not None:
        return gated

    # Both members are co-equal items under comparison, so an unresolvable one
    # is unobservable rather than an invitation to scan the whole snapshot for
    # some other decision/hypothesis pair and answer about that instead.
    unobservable = _declared_but_unobservable(ctx, name, check_counterpart=True)
    if unobservable is not None:
        return unobservable

    snapshot = ctx.snapshot
    left, right = _pair(ctx)
    if left is None or right is None:
        decisions = [item for item in snapshot.items if item.kind == "decision"]
        hypotheses = [item for item in snapshot.items if item.kind == "hypothesis"]
        if decisions and hypotheses:
            return _result(
                name,
                "pass",
                f"typed kinds separate {decisions[0].id} (decision) from "
                f"{hypotheses[0].id} (hypothesis)",
                ctx.subject,
            )
        return _result(
            name,
            "unsupported",
            "no decision/hypothesis pair observable for the comparison",
            ctx.subject,
        )

    if left.kind != right.kind:
        return _result(
            name,
            "pass",
            f"typed kind field: {left.id}={left.kind} vs {right.id}={right.kind}",
            ctx.subject,
        )
    left_folder, right_folder = _folder(left.locator), _folder(right.locator)
    if left_folder and right_folder and left_folder != right_folder:
        left_decision_folder, left_hypothesis_folder = _collection_terms(left.locator)
        right_decision_folder, right_hypothesis_folder = _collection_terms(right.locator)
        if (left_decision_folder and right_hypothesis_folder) or (
            left_hypothesis_folder and right_decision_folder
        ):
            return _result(
                name,
                "pass",
                f"documented collection convention names the distinction: "
                f"{left_folder} vs {right_folder}",
                ctx.subject,
            )
    # A schema/metadata attribute only distinguishes the two concepts if it
    # *states* them. Two values that merely differ ("active" vs "draft") tell a
    # fresh agent nothing about which one is a settled decision, so accepting
    # any difference turned this invariant into a diff detector.
    shared_keys = sorted(set(left.raw) & set(right.raw))
    for key in shared_keys:
        left_value, right_value = left.raw[key], right.raw[key]
        if left_value == right_value:
            continue
        left_decision = states_value("decision", left_value)
        left_hypothesis = states_value("hypothesis", left_value)
        right_decision = states_value("decision", right_value)
        right_hypothesis = states_value("hypothesis", right_value)
        if (left_decision and right_hypothesis) or (left_hypothesis and right_decision):
            return _result(
                name,
                "pass",
                f"metadata attribute {key!r} states the distinction: "
                f"{left_value!r} vs {right_value!r}",
                ctx.subject,
            )
    return _result(
        name,
        "fail",
        f"{left.id} and {right.id} are indistinguishable: same kind {left.kind!r}, "
        "no collection naming decision vs hypothesis, and no metadata attribute "
        "stating it",
        ctx.subject,
    )


def open_question_queryable(ctx: AssertionContext) -> AssertionResult:
    """Modeled ignorance is retrievable.

    Accepts a dedicated item kind, a queryable tag/attribute, or a task/review
    queue entry.
    """

    name = "open_question_queryable"
    gated = _gate(ctx, name, "open_question", "kind")
    if gated is not None:
        return gated

    snapshot = ctx.snapshot
    typed = [item.id for item in snapshot.items if item.kind == "open_question"]
    if typed:
        return _result(name, "pass", f"dedicated open_question item(s): {_listed(typed)}", ctx.subject)
    for item in snapshot.items:
        for key, value in sorted(item.raw.items()):
            if states_value("open-question", value) or states_value("open_question", value):
                return _result(
                    name,
                    "pass",
                    f"queryable attribute {item.id}.{key} states an open question",
                    ctx.subject,
                )
    queued = [item.id for item in snapshot.items if _is_open(item.review_state)]
    if queued:
        return _result(name, "pass", f"review-queue entries open for inspection: {_listed(queued)}", ctx.subject)
    return _result(
        name,
        "fail",
        "no open question is queryable: no dedicated kind, no attribute, no queue entry",
        ctx.subject,
    )


def uncertainty_declared(ctx: AssertionContext) -> AssertionResult:
    """A conclusion whose support is thin says so."""

    name = "uncertainty_declared"
    gated = _gate(ctx, name, "uncertainty")
    if gated is not None:
        return gated

    snapshot = ctx.snapshot
    subjects = _belief_items(ctx)
    if ctx.subject and not subjects:
        candidate = snapshot.item(ctx.subject)
        subjects = (candidate,) if candidate is not None else ()
    if not subjects:
        return _result(name, "unsupported", "no conclusion observed to carry uncertainty", ctx.subject)

    missing: list[str] = []
    for item in subjects:
        if item.uncertainty and item.uncertainty.strip():
            continue
        if _is_open(item.review_state) or _is_uncertain(item.review_state):
            continue
        if any(
            question.kind == "open_question" and item.id in question.cites
            for question in snapshot.items
        ):
            continue
        missing.append(item.id)
    if missing:
        return _result(
            name, "fail", f"conclusions with no declared uncertainty: {_listed(missing)}", ctx.subject
        )
    return _result(
        name, "pass", f"{len(subjects)} conclusion(s) declare their uncertainty", ctx.subject
    )


# --------------------------------------------------------------------------
# 12-14: review lifecycle (snapshot pairs)
# --------------------------------------------------------------------------


def _require_pair(ctx: AssertionContext, name: str) -> AssertionResult | None:
    if ctx.prior is None:
        return _result(
            name, "unsupported", "this invariant needs a snapshot pair; none supplied", ctx.subject
        )
    return None


def review_state_durable(ctx: AssertionContext) -> AssertionResult:
    """A recorded review decision survives the transition."""

    name = "review_state_durable"
    gated = _gate(ctx, name, "review_state")
    if gated is not None:
        return gated
    missing_pair = _require_pair(ctx, name)
    if missing_pair is not None:
        return missing_pair
    assert ctx.prior is not None

    prior_by = ctx.prior.items_by_id()
    post_by = ctx.snapshot.items_by_id()
    tracked = [item for item in prior_by.values() if item.review_state]
    if ctx.subject:
        tracked = [item for item in tracked if item.id == ctx.subject]
    if not tracked:
        return _result(name, "unsupported", "no review state observed before the transition", ctx.subject)

    lost: list[str] = []
    for item in tracked:
        after = post_by.get(item.id)
        if after is None:
            lost.append(f"{item.id} absent after the transition")
        elif not after.review_state:
            lost.append(f"{item.id} lost review state {item.review_state!r}")
    if lost:
        return _result(name, "fail", f"review decisions did not survive: {_listed(lost)}", ctx.subject)
    return _result(
        name, "pass", f"{len(tracked)} review decision(s) survived the transition", ctx.subject
    )


def review_reopens_on_material_change(ctx: AssertionContext) -> AssertionResult:
    """A closed item whose content materially changed comes back open."""

    name = "review_reopens_on_material_change"
    gated = _gate(ctx, name, "review_state")
    if gated is not None:
        return gated
    missing_pair = _require_pair(ctx, name)
    if missing_pair is not None:
        return missing_pair
    assert ctx.prior is not None

    prior_by = ctx.prior.items_by_id()
    post_by = ctx.snapshot.items_by_id()
    candidates = []
    for item_id, before in prior_by.items():
        after = post_by.get(item_id)
        if after is None or not _is_closed(before.review_state):
            continue
        if _content_hash(before) != _content_hash(after):
            candidates.append((before, after))
    if ctx.subject:
        candidates = [pair for pair in candidates if pair[0].id == ctx.subject]
    if not candidates:
        return _result(
            name,
            "unsupported",
            "no closed item underwent a material change between the snapshots",
            ctx.subject,
        )

    offenders = [
        f"{after.id} stayed {after.review_state!r}"
        for _before, after in candidates
        if not _is_open(after.review_state)
    ]
    if offenders:
        return _result(
            name, "fail", f"material change did not reopen review: {_listed(offenders)}", ctx.subject
        )
    return _result(
        name,
        "pass",
        f"{len(candidates)} closed item(s) reopened after a material change",
        ctx.subject,
    )


def review_stays_closed_on_irrelevant_change(ctx: AssertionContext) -> AssertionResult:
    """A closed decision is not churned back open by an unrelated change."""

    name = "review_stays_closed_on_irrelevant_change"
    gated = _gate(ctx, name, "review_state")
    if gated is not None:
        return gated
    missing_pair = _require_pair(ctx, name)
    if missing_pair is not None:
        return missing_pair
    assert ctx.prior is not None

    prior_by = ctx.prior.items_by_id()
    post_by = ctx.snapshot.items_by_id()
    candidates = []
    for item_id, before in prior_by.items():
        after = post_by.get(item_id)
        if after is None or not _is_closed(before.review_state):
            continue
        if _content_hash(before) == _content_hash(after):
            candidates.append((before, after))
    if ctx.subject:
        candidates = [pair for pair in candidates if pair[0].id == ctx.subject]
    if not candidates:
        return _result(
            name,
            "unsupported",
            "no closed item survived unchanged across the snapshots",
            ctx.subject,
        )

    offenders = [
        f"{after.id} became {after.review_state!r}"
        for _before, after in candidates
        if not _is_closed(after.review_state)
    ]
    if offenders:
        return _result(
            name,
            "fail",
            f"closed review reopened without a material change: {_listed(offenders)}",
            ctx.subject,
        )
    return _result(
        name,
        "pass",
        f"{len(candidates)} closed decision(s) stayed closed through an irrelevant change",
        ctx.subject,
    )


# --------------------------------------------------------------------------
# 15-17: out-of-band operations (snapshot pairs)
# --------------------------------------------------------------------------


def external_edit_authoritative_within(ctx: AssertionContext) -> AssertionResult:
    """An out-of-band edit to the canonical artifact wins, inside the declared
    freshness bound. CATASTROPHIC."""

    name = "external_edit_authoritative_within"
    gated = _gate(ctx, name, "external_edit", "locator")
    if gated is not None:
        return gated
    missing_pair = _require_pair(ctx, name)
    if missing_pair is not None:
        return missing_pair
    assert ctx.prior is not None

    if ctx.freshness_bound_s is None:
        return _result(
            name,
            "unsupported",
            "no freshness bound declared; adoption latency cannot be scored",
            ctx.subject,
        )
    if ctx.external_edit_at is None:
        return _result(
            name, "unsupported", "no external-edit timestamp supplied by the scenario", ctx.subject
        )

    prior_by = ctx.prior.items_by_id()
    post_by = ctx.snapshot.items_by_id()
    target_ids = [ctx.subject] if ctx.subject else sorted(prior_by)
    adopted: list[str] = []
    ignored: list[str] = []
    for item_id in target_ids:
        before = prior_by.get(item_id)
        after = post_by.get(item_id)
        if before is None:
            continue
        if after is None:
            ignored.append(f"{item_id} vanished after the external edit")
        elif _content_hash(before) == _content_hash(after):
            ignored.append(f"{item_id} unchanged in observed state")
        else:
            adopted.append(item_id)
    if not adopted:
        return _result(
            name,
            "fail",
            f"authoritative external edit not reflected: {_listed(ignored) or 'no target observed'}",
            ctx.subject,
        )

    elapsed = _elapsed_seconds(ctx.external_edit_at, ctx.snapshot.taken_at)
    if elapsed is None:
        return _result(
            name, "blocked", "external-edit or snapshot timestamp is not parsable", ctx.subject
        )
    if elapsed <= ctx.freshness_bound_s:
        return _result(
            name,
            "pass",
            f"external edit to {_listed(adopted)} adopted in {elapsed:g}s, within the "
            f"declared bound {ctx.freshness_bound_s}s",
            ctx.subject,
        )
    return _result(
        name,
        "fail",
        f"external edit to {_listed(adopted)} adopted after {elapsed:g}s, outside the "
        f"declared bound {ctx.freshness_bound_s}s",
        ctx.subject,
    )


def export_reconstructs_state(ctx: AssertionContext) -> AssertionResult:
    """The export reconstructs items, currency, lineage, and evidence edges.

    ``ctx.prior`` is the live snapshot; ``ctx.snapshot`` is the export-derived
    one. Reconstruction loss above ``ctx.tolerance`` fails.
    """

    name = "export_reconstructs_state"
    gated = _gate(ctx, name, "export")
    if gated is not None:
        return gated
    missing_pair = _require_pair(ctx, name)
    if missing_pair is not None:
        return missing_pair
    assert ctx.prior is not None

    live = ctx.prior
    derived_by = ctx.snapshot.items_by_id()
    if not live.items:
        return _result(name, "unsupported", "live snapshot has no items to reconstruct", ctx.subject)

    mismatches: list[str] = []
    for item in live.items:
        found = derived_by.get(item.id)
        if found is None:
            mismatches.append(f"{item.id}: missing from export")
        elif found.kind != item.kind:
            mismatches.append(f"{item.id}: kind {found.kind} != {item.kind}")
        elif found.current != item.current:
            mismatches.append(f"{item.id}: currency {found.current} != {item.current}")
        elif found.revision_of != item.revision_of:
            mismatches.append(f"{item.id}: lineage {found.revision_of} != {item.revision_of}")
        elif set(_evidence_targets(live, item)) != set(
            _evidence_targets(ctx.snapshot, found)
        ):
            mismatches.append(f"{item.id}: evidence edges differ")
        elif _outgoing_edges(live, item.id) != _outgoing_edges(ctx.snapshot, item.id):
            # Typed relations are state. An export that keeps every item but
            # drops the graph has not reconstructed the state, and comparing
            # ``cites`` alone could not see that.
            mismatches.append(f"{item.id}: typed relations differ")
    loss = len(mismatches) / len(live.items)
    if loss <= ctx.tolerance:
        return _result(
            name,
            "pass",
            f"export reconstructs {len(live.items) - len(mismatches)}/{len(live.items)} "
            f"items (loss {loss:g} <= tolerance {ctx.tolerance:g})",
            ctx.subject,
        )
    return _result(
        name,
        "fail",
        f"export loss {loss:g} exceeds tolerance {ctx.tolerance:g}: {_listed(mismatches)}",
        ctx.subject,
    )


def dependent_conclusions_surfaced_for_review(ctx: AssertionContext) -> AssertionResult:
    """When support is retired, what depended on it is surfaced, not rewritten.

    A dependent that was quietly *rewritten* to match the new state is the
    failure this invariant exists to catch, not an excuse for it. An earlier
    build treated any content change as evidence the system had handled the
    retirement, which meant the most harmful behaviour — silently editing a
    conclusion the user still believes they authored — scored a pass. Only an
    open review flag (or the dependent being explicitly retired itself) counts
    as surfacing it.
    """

    name = "dependent_conclusions_surfaced_for_review"
    gated = _gate(ctx, name, "review_state", "cites")
    if gated is not None:
        return gated
    missing_pair = _require_pair(ctx, name)
    if missing_pair is not None:
        return missing_pair
    assert ctx.prior is not None

    prior_by = ctx.prior.items_by_id()
    post_by = ctx.snapshot.items_by_id()
    retired = {
        item_id
        for item_id, before in prior_by.items()
        if before.current != "no"
        and (post_by.get(item_id) is None or post_by[item_id].current == "no")
    }
    if not retired:
        return _result(
            name, "unsupported", "no support was retired between the snapshots", ctx.subject
        )

    offenders: list[str] = []
    dependents: list[str] = []
    for item in ctx.snapshot.items:
        targets = set(_evidence_targets(ctx.snapshot, item))
        if not targets & retired:
            continue
        dependents.append(item.id)
        before = prior_by.get(item.id)
        revised = before is not None and _content_hash(before) != _content_hash(item)
        if _is_open(item.review_state) or item.current == "no":
            continue
        if revised:
            offenders.append(f"{item.id} silently rewritten with no review flag")
        else:
            offenders.append(f"{item.id} still current, unflagged, unchanged")
    if not dependents:
        return _result(
            name, "unsupported", "no dependent conclusion cites the retired support", ctx.subject
        )
    if offenders:
        return _result(
            name,
            "fail",
            f"dependents silently unaffected by retired support: {_listed(offenders)}",
            ctx.subject,
        )
    return _result(
        name,
        "pass",
        f"{len(dependents)} dependent conclusion(s) surfaced for review after retirement",
        ctx.subject,
    )


# --------------------------------------------------------------------------
# 18: isolation
# --------------------------------------------------------------------------


def no_cross_case_residue(ctx: AssertionContext) -> AssertionResult:
    """No foreign-case canary is reachable from this case. CATASTROPHIC."""

    name = "no_cross_case_residue"
    if ctx.foreign_case_hits is None:
        return _result(
            name, "unsupported", "no cross-case canary probe was executed for this run", ctx.subject
        )
    if ctx.foreign_case_hits:
        return _result(
            name,
            "fail",
            f"foreign-case canary reachable: {_listed(ctx.foreign_case_hits)}",
            ctx.subject,
        )
    return _result(name, "pass", "cross-case canary probe returned no foreign hits", ctx.subject)
