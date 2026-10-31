"""The barrier before an authored generic relation joins an entity.

A generic relation says two pages are related and nothing more. When a write adds
such an edge to an entity and the registry names a specific relation that fits both
pages, the write stops before it commits. The caller then writes the edge as one of
those relations, or keeps it generic and says why. The reason is logged against the
edge itself (`relation_census.operation_provenance`), so a later review reads it.

A generic edge between two notes is never refused. It joins `Verdict.offers`, and
`relation_offer` offers the swap to a specific relation once the write has committed.

The barrier judges only an edge this call adds (`relation_census.added_edges`), so an
edge a successor carries from its predecessor is never asked about again. A page
that is not live, a bulk operation, and an edge to a target the writer cannot
resolve all pass without a word. A withheld target reads as an absent one, so the
refusal never tells a restricted writer that a page exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import relation_endpoints, relation_options, relation_review, semantic_contract

CODE = "GENERIC_RELATION_NEEDS_REASON"
#: The `relation_disposition` that keeps a generic edge on a stated reason; a protocol token.
GENERIC = "generic"
REMEDIATION = (
    "Write the edge as one of the listed relations, or keep it generic: retry with "
    'relation_disposition "generic", a relation_review_reason saying why none fits, and the '
    'returned hash. On create-entity, add a "reason" to the relations item instead.'
)


@dataclass(frozen=True)
class Choice:
    """The caller's answer to the barrier: one reason for the call, or one per edge."""

    reason: str | None = None
    #: (relation as written, target page, reason) from the create-entity `relations` items.
    by_edge: tuple[tuple[str, str, str], ...] = ()

    def reason_for(self, relation: str, target_path: str) -> str | None:
        target = target_path.removesuffix(".md")
        for kind, page, reason in self.by_edge:
            if kind == relation and page == target:
                return reason
        return self.reason


@dataclass(frozen=True)
class Cleared:
    """A choice with the generic edges it kept, as (edge identity, reason) pairs."""

    choice: Choice
    edges: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Refusal:
    """The first generic edge that needs an answer, and how many did."""

    options: list[dict[str, Any]]
    relation: str
    target: str
    count: int

    def details(self) -> dict[str, Any]:
        return {
            "options": self.options,
            "edge": {"relation": self.relation, "target": self.target},
            "refused_edges": self.count,
        }

    @property
    def reason(self) -> str:
        edges = "a generic edge" if self.count == 1 else f"{self.count} generic edges"
        return f"{edges} to an entity, where a specific relation fits"


@dataclass(frozen=True)
class Offer:
    """A generic edge between two notes, with the specific relations that fit both pages.

    Never a refusal: the edge commits, and `relation_offer` offers the swap afterwards.
    """

    fact: semantic_contract.RelationFact
    options: list[dict[str, Any]]


@dataclass(frozen=True)
class Verdict:
    """What the barrier found: the edges `choice` kept, the refusal, and the offers to make later."""

    kept: tuple[tuple[str, str], ...] = ()
    refusal: Refusal | None = None
    offers: tuple[Offer, ...] = ()


def call_choice(disposition: str | None, reason: str | None) -> Choice | None:
    """The call-wide choice a `generic` disposition makes; None for any other disposition.

    Raises `RelationReviewError` for a missing or out-of-bounds reason.
    """
    if disposition != GENERIC:
        return None
    return Choice(reason=relation_review.review_reason(reason, "generic"))


def evaluate(
    page: semantic_contract.SemanticPageState,
    corpus: semantic_contract.SemanticCorpusContext,
    *,
    prior: semantic_contract.SemanticPageState | None,
    prior_corpus: semantic_contract.SemanticCorpusContext,
    operation: str,
    choice: Choice | None,
) -> Verdict:
    """The generic edges `choice` keeps, the refusal for those it leaves unanswered, and the offers.

    `prior` is the page this write replaces: the page before an edit, or the predecessor
    of a successor, in `prior_corpus`. An edge it already authored is not added by this call.
    """
    if operation in semantic_contract.BARRIER_EXEMPT_OPERATIONS or page.status_class != "live":
        return Verdict()
    registry = corpus.registry_for(page.path)
    current = semantic_contract.writer_authored_facts(page, corpus)

    def generic(fact: semantic_contract.RelationFact) -> bool:
        definition = registry.definition(fact.canonical_relation or "")
        return definition is not None and definition.generic

    if not any(generic(fact) for fact in current):
        return Verdict()
    from . import relation_census

    added = relation_census.added_edges(
        prior_corpus,
        corpus,
        prior_path=prior.path if prior is not None else None,
        current_path=page.path,
        prior_facts=(
            semantic_contract.writer_authored_facts(prior, prior_corpus)
            if prior is not None
            else ()
        ),
        current_facts=current,
    )
    types = semantic_contract.entity_type_registry(corpus.vault_root)
    kept: list[tuple[str, str]] = []
    # One answer settles every identical bullet, so the caller is asked once per relation and target.
    refused: dict[tuple[str, str], tuple[Any, list[dict[str, Any]]]] = {}
    offers: list[Offer] = []
    for edge in added.edges:
        fact = edge.fact
        # An unresolved target is all a restricted writer sees of a withheld page, so it passes.
        if not generic(fact) or fact.target_status != "resolved":
            continue
        subject, target = (
            semantic_contract.endpoint_page_kind(state, types)
            for state in semantic_contract.fact_endpoints(page, corpus, fact)
        )
        # A page whose note type cannot say what it is leaves the question open, never a refusal.
        if subject is None or target is None:
            continue
        options = relation_options.fitting_options(
            registry,
            subject,
            target,
            types,
            accepts=lambda key, fact=fact: semantic_contract.fits_scope(registry, key, page, fact),
        )
        if not options:
            continue
        # An edge between two notes is offered its options once committed, never asked about.
        if relation_endpoints.ENTITY_ROLE not in (subject.role, target.role):
            offers.append(Offer(fact, options))
            continue
        reason = choice.reason_for(fact.raw_relation, fact.logical_target_path) if choice is not None else None
        if reason is None:
            refused.setdefault((fact.raw_relation, fact.logical_target_path), (fact, options))
        else:
            kept.append((edge.identity, reason))
    if not refused:
        return Verdict(tuple(kept), None, tuple(offers))
    fact, options = next(iter(refused.values()))
    return Verdict(
        tuple(kept), Refusal(options, fact.raw_relation, fact.raw_target, len(refused)), tuple(offers)
    )
