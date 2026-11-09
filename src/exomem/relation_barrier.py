"""The barrier that refuses a generic relation a write would add.

A generic relation says two pages are related and nothing more. Any write that adds
one, between two notes as much as to an entity, stops before it commits. The refusal
hands the writer a ranked menu of specific relations (`relation_options`) and the
three routes that end it: retry with a specific relation, propose a new relation type,
or, as a last resort, write a plain link. No answer keeps the generic edge.

The barrier judges only an edge this call adds (`relation_census.added_edges`), so a
page that already holds a generic edge from before can still be edited, and an edge a
successor carries from its predecessor is never asked about again. A page that is not
live and a bulk operation pass without a word. The refusal fires on the registry's
`generic` attribute of the relation, so it leaks nothing about the target: a withheld target reads as an absent one, and
the menu is built from the writer's own page and the pages the writer may see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import agent_refusal, capabilities, relation_options, relation_registry, semantic_contract

CODE = "GENERIC_RELATION_REFUSED"
REMEDIATION = (
    "A generic relation says two pages are related and nothing more, so a write cannot add "
    "one. Retry with one of the listed specific relations; when none fits, propose a new "
    "relation type; as a last resort, write a plain link."
)


@dataclass(frozen=True)
class Refusal:
    """The first generic edge the write adds, how many it adds, and the ways to answer."""

    options: list[dict[str, Any]]
    relation: str
    target: str
    count: int
    #: The plain link's relation key, from the registry.
    link: str
    #: The writer that was called; the `link` writer takes an item, every other writer a bullet.
    writer: str | None = None

    def details(self) -> dict[str, Any]:
        return {
            "options": self.options,
            "options_route": capabilities.active_call_route(
                "schema_memory", {"operation": "inspect", "subject": "relations"}
            ),
            "edge": {"relation": self.relation, "target": self.target},
            "refused_edges": self.count,
            "routes": self._routes(),
            "candidates": agent_refusal.ranked_with_link(self.options, self.link),
        }

    @property
    def reason(self) -> str:
        edges = "a generic relation" if self.count == 1 else f"{self.count} generic relations"
        return f"the write adds {edges}; name a specific relation, propose one, or write a plain link"

    def _edge(self, key: str) -> str:
        """The edge written with `key`, in the shape the called writer takes."""
        if self.writer == "link":
            return f'relations item {{"kind": "{key}", "target": "[[{self.target}]]"}}'
        return f"`## Relations` bullet `- {key} [[{self.target}]]`"

    def _routes(self) -> dict[str, Any]:
        best = self.options[0]["key"] if self.options else "<relation key>"
        return {
            "specific": {
                "do": f"Repeat this call with the edge written as {self._edge(best)}; "
                "any key in `options` fits the same way.",
            },
            "propose": {
                "do": "When no listed relation fits, propose the relation, save it with the reviewed "
                "proposal and its hash, then repeat this call with the new key.",
                "propose": capabilities.active_call_route(
                    "schema_memory",
                    {
                        "operation": "propose-relation",
                        "subject": "relations",
                        "proposal": {
                            "requested_label": "<label>",
                            "parent": "<core relation key>",
                            "description": "<what the relation means>",
                            "direction": "directed",
                        },
                    },
                ),
                "save": capabilities.active_call_route(
                    "schema_memory",
                    {
                        "operation": "save-relations",
                        "subject": "relations",
                        "proposal": "<the reviewed delta the propose call returned>",
                        "expected_hash": "<the content_hash the propose call returned>",
                        "why": "<why the vault needs this relation>",
                    },
                ),
            },
            "plain_link": {
                "do": f"As a last resort, repeat this call with the edge written as {self._edge(self.link)}.",
            },
        }


def evaluate(
    page: semantic_contract.SemanticPageState,
    corpus: semantic_contract.SemanticCorpusContext,
    *,
    prior: semantic_contract.SemanticPageState | None,
    prior_corpus: semantic_contract.SemanticCorpusContext,
    operation: str,
    writer: str | None = None,
) -> Refusal | None:
    """The refusal for the generic relations this write adds; None when it adds none.

    `prior` is the page this write replaces: the page before an edit, or the predecessor
    of a successor, in `prior_corpus`. An edge it already authored is not added by this call.
    """
    if operation in semantic_contract.BARRIER_EXEMPT_OPERATIONS or page.status_class != "live":
        return None
    registry = corpus.registry_for(page.path)
    current = semantic_contract.writer_authored_facts(page, corpus)
    if not any(registry.is_generic(fact.canonical_relation or "") for fact in current):
        return None
    from . import relation_census

    prior_facts = (
        semantic_contract.writer_authored_facts(prior, prior_corpus) if prior is not None else ()
    )
    added = relation_census.added_edges(
        prior_corpus,
        corpus,
        prior_path=prior.path if prior is not None else None,
        current_path=page.path,
        prior_facts=prior_facts,
        current_facts=current,
    )
    # The census proves most new edges by stable identity; a relation inside a unit with no
    # anchor it can only judge by what was written, and returns apart as `unproven`.
    new_facts = [edge.fact for edge in added.edges] + list(added.unproven)
    # One answer settles every identical bullet, so the writer hears of each relation and target once.
    refused: dict[tuple[str, str], semantic_contract.RelationFact] = {}
    for fact in new_facts:
        if registry.is_generic(fact.canonical_relation or ""):
            refused.setdefault((fact.raw_relation, fact.raw_target), fact)
    if not refused:
        return None
    fact = next(iter(refused.values()))
    return Refusal(
        _menu(page, corpus, prior_corpus, registry, fact),
        fact.raw_relation,
        fact.raw_target,
        len(refused),
        registry.link_key,
        writer,
    )


def _menu(
    page: semantic_contract.SemanticPageState,
    corpus: semantic_contract.SemanticCorpusContext,
    prior_corpus: semantic_contract.SemanticCorpusContext,
    registry: relation_registry.RelationRegistry,
    fact: semantic_contract.RelationFact,
) -> list[dict[str, Any]]:
    """The ranked specific relations for the edge, from its pages and what the vault already authors."""
    types = semantic_contract.entity_type_registry(corpus.vault_root)
    subject_state, target_state = semantic_contract.fact_endpoints(page, corpus, fact)
    subject_kind = semantic_contract.endpoint_page_kind(subject_state, types)
    target_kind = semantic_contract.endpoint_page_kind(target_state, types)
    # Only a resolved target is the writer's to see, so an unresolved one stays out of the ranking.
    resolved = fact.target_status == "resolved"
    usage = relation_options.pair_usage(
        prior_corpus.relation_facts,
        registry,
        relation_options.page_class(fact.source_kind, page.page_type),
        relation_options.page_class(fact.target_kind, fact.target_page_type) if resolved else None,
        semantic_contract.writer_visible(prior_corpus),
    )
    return relation_options.ranked_options(
        registry,
        subject_kind,
        target_kind if resolved else None,
        types,
        accepts=lambda key: semantic_contract.fits_scope(registry, key, page, fact),
        usage=usage,
    )
