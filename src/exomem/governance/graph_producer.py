"""Translate exact semantic-write snapshots into projected graph successors."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .. import semantic_contract, semantic_language_registry, vault
from . import catalog_publication, projected_graph


class GraphProducerError(RuntimeError):
    """A planned Markdown batch cannot produce an exact graph successor."""


def _edge_key(
    edge: projected_graph.ProjectionGraphEdge,
) -> tuple[bytes, bytes, bytes]:
    return (
        edge.source_item_identity.encode("utf-8"),
        edge.target_item_identity.encode("utf-8"),
        edge.relation_type.encode("utf-8"),
    )


def _edges_by_source(
    corpus: semantic_contract.SemanticCorpusContext,
) -> dict[str, tuple[projected_graph.ProjectionGraphEdge, ...]]:
    pages = frozenset(corpus.pages)
    resolver = vault.WikilinkResolver.from_entries(
        corpus.vault_root,
        corpus.resolver_entries,
    )
    edges: dict[str, set[projected_graph.ProjectionGraphEdge]] = {
        path: set() for path in pages
    }
    for fact in corpus.relation_facts:
        source = fact.logical_source_path
        target = fact.logical_target_path
        relation = fact.canonical_relation
        if (
            source not in pages
            or target not in pages
            or fact.resolved_target_path is None
            or not isinstance(relation, str)
            or not relation
        ):
            continue
        edges[source].add(
            projected_graph.ProjectionGraphEdge(
                source_item_identity=source,
                target_item_identity=target,
                relation_type=relation,
            )
        )
    for source, state in corpus.pages.items():
        for raw_target, _line in state.body_wikilinks:
            try:
                canonical, warning = vault.normalize_wikilink(
                    raw_target,
                    corpus.vault_root,
                    resolver=resolver,
                    strict=False,
                )
            except (OSError, RuntimeError, UnicodeError, ValueError):
                continue
            target = canonical.split("#", 1)[0]
            if target and not target.casefold().endswith(".md"):
                target += ".md"
            if warning is not None or target not in pages:
                continue
            edges[source].add(
                projected_graph.ProjectionGraphEdge(
                    source_item_identity=source,
                    target_item_identity=target,
                    relation_type="links_to",
                )
            )
    return {
        source: tuple(sorted(values, key=_edge_key))
        for source, values in edges.items()
    }


def _planned_overlay(
    vault_root: Path,
    *,
    base_corpus: semantic_contract.SemanticCorpusContext,
    writes: tuple[vault.PlannedWrite, ...],
    semantic_states: Mapping[str, semantic_contract.SemanticPageState] | None = None,
    language_registry: semantic_language_registry.SemanticLanguageRegistry | None = None,
) -> semantic_contract.SemanticCorpusContext:
    root = Path(vault_root)
    if base_corpus.vault_root != root:
        raise GraphProducerError("graph producer corpus belongs to another vault")
    if type(writes) is not tuple or not writes:
        raise GraphProducerError("graph producer requires a finite planned-write batch")
    prepared_states = dict(semantic_states or {})
    mutations: dict[str, catalog_publication.MarkdownCatalogMutation] = {}
    try:
        for write in writes:
            mutation = catalog_publication.mutation_from_planned_write(root, write)
            if mutation is None:
                continue
            if mutation.path in mutations:
                raise GraphProducerError("graph producer Markdown targets collide")
            mutations[mutation.path] = mutation
    except catalog_publication.CatalogPublicationError as error:
        raise GraphProducerError(str(error)) from error
    if not mutations:
        return base_corpus

    language = language_registry
    target_corpus = base_corpus
    try:
        for path in sorted(mutations):
            mutation = mutations[path]
            state = prepared_states.get(path)
            if state is None:
                existing = target_corpus.pages.get(path)
                if existing is not None and existing.source_hash == vault.content_hash(
                    mutation.source
                ):
                    state = existing
                else:
                    if language is None:
                        language = semantic_language_registry.load_registry(root)
                    state = semantic_contract.build_page_state(
                        root,
                        path,
                        mutation.source,
                        relation_registry=base_corpus.registry,
                        language_registry=language,
                    )
            if state.path != path or state.source_hash != vault.content_hash(
                mutation.source
            ):
                raise GraphProducerError(
                    "graph producer semantic state does not bind planned bytes"
                )
            target_corpus = target_corpus.with_candidate(state)
    except GraphProducerError:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        raise GraphProducerError(
            "graph producer cannot derive the planned semantic topology"
        ) from error
    return target_corpus


def _replacements_between(
    before_corpus: semantic_contract.SemanticCorpusContext,
    after_corpus: semantic_contract.SemanticCorpusContext,
) -> tuple[catalog_publication.GraphMeasurementReplacement, ...]:
    before_edges = _edges_by_source(before_corpus)
    after_edges = _edges_by_source(after_corpus)
    affected = {
        path
        for path, state in after_corpus.pages.items()
        if (before := before_corpus.pages.get(path)) is None
        or before.source_hash != state.source_hash
    }
    affected.update(
        source
        for source in set(before_edges) | set(after_edges)
        if before_edges.get(source, ()) != after_edges.get(source, ())
    )
    return tuple(
        catalog_publication.GraphMeasurementReplacement(
            item_identity=source,
            content_hash=after_corpus.pages[source].source_hash,
            edges=after_edges.get(source, ()),
        )
        for source in sorted(affected)
        if source in after_corpus.pages
    )


def replacements_for_planned_markdown(
    vault_root: Path,
    *,
    before_corpus: semantic_contract.SemanticCorpusContext,
    writes: tuple[vault.PlannedWrite, ...],
    semantic_states: Mapping[str, semantic_contract.SemanticPageState] | None = None,
    language_registry: semantic_language_registry.SemanticLanguageRegistry | None = None,
) -> tuple[catalog_publication.GraphMeasurementReplacement, ...]:
    """Build replacements from one already-reviewed Markdown overlay."""

    after_corpus = _planned_overlay(
        vault_root,
        base_corpus=before_corpus,
        writes=writes,
        semantic_states=semantic_states,
        language_registry=language_registry,
    )
    return _replacements_between(before_corpus, after_corpus)


def replacements_for_semantic_transition(
    vault_root: Path,
    *,
    before_corpus: semantic_contract.SemanticCorpusContext,
    after_corpus: semantic_contract.SemanticCorpusContext,
    writes: tuple[vault.PlannedWrite, ...],
    semantic_states: Mapping[str, semantic_contract.SemanticPageState] | None = None,
    language_registry: semantic_language_registry.SemanticLanguageRegistry | None = None,
) -> tuple[catalog_publication.GraphMeasurementReplacement, ...]:
    """Build replacements from exact detached before/after semantic corpora."""

    root = Path(vault_root)
    if before_corpus.vault_root != root or after_corpus.vault_root != root:
        raise GraphProducerError("graph producer corpus belongs to another vault")
    if (
        before_corpus.registry.core_version,
        before_corpus.registry.extension_hash,
    ) != (
        after_corpus.registry.core_version,
        after_corpus.registry.extension_hash,
    ):
        raise GraphProducerError("graph producer semantic registries disagree")
    target_corpus = _planned_overlay(
        root,
        base_corpus=after_corpus,
        writes=writes,
        semantic_states=semantic_states,
        language_registry=language_registry,
    )
    return _replacements_between(before_corpus, target_corpus)

__all__ = [
    "GraphProducerError",
    "replacements_for_planned_markdown",
    "replacements_for_semantic_transition",
]
