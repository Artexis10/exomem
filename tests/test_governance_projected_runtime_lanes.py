"""Projected vector, CLIP, graph, fusion, and rerank runtime integration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from governance_projection_support import verified_namespace

from exomem import embeddings, readiness
from exomem.governance import (
    principal,
    projected_graph,
    projected_retrieval,
    projection_authorization,
    projection_measurement_store,
    projection_runtime,
    projection_store,
    projections,
    schema_v4,
)
from exomem.governance.decisions import Decision
from exomem.governance.policy import Policy

_VECTOR_EXTRACTOR = "projected-text-v1"
_CLIP_EXTRACTOR = "pixels-v1"
_GRAPH_EXTRACTOR = "projected-graph-v1"
_GRAPH_MODEL = "graph-schema-v1"


@pytest.fixture(autouse=True)
def _enable_projected_query_models(monkeypatch):
    """Make model availability explicit; hard-off behavior has its own test."""

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_RANKING", raising=False)


def _variant(
    identity: str,
    content_hash: str,
    text: str,
    *,
    level: int = 6,
    fields: Mapping[str, str] | None = None,
):
    search_fields = {"title": identity.rsplit("/", 1)[-1], "body": text}
    search_fields.update(fields or {})
    variant = projections.build_projection_variant(
        item_identity=identity,
        content_hash=content_hash,
        decision=Decision(
            level=level,
            options={"abstract": text} if level == 3 else {},
        ),
        projector_schema_version=projections.PROJECTOR_SCHEMA_VERSION,
        full_search_fields=search_fields,
    )
    assert variant is not None
    return variant


def _item(variant):
    return projection_store.ProjectionItemVariants(
        item_identity=variant.item_identity,
        content_hash=variant.content_hash,
        variants=(variant,),
    )


def _measurement_root(
    key,
    *,
    lane: str,
    extractor_version: str,
    model_version: str,
    measurement_count: int,
    vector_dimension: int | None,
    graph_edge_count: int = 0,
):
    family = projection_measurement_store.MeasurementFamilyKey(
        namespace_key=key,
        lane=lane,
        extractor_version=extractor_version,
        model_version=model_version,
    )
    return projection_store.ProjectionMeasurementRoot(
        namespace_key=key,
        family_id=family.family_id,
        lane=lane,
        extractor_version=extractor_version,
        model_version=model_version,
        measurement_count=measurement_count,
        vector_dimension=vector_dimension,
        graph_edge_count=graph_edge_count,
        rows_digest={"vector": "a", "clip": "b", "graph": "c"}[lane] * 64,
    )


def _runtime(
    items: Sequence[projection_store.ProjectionItemVariants],
    *,
    vectors: Sequence[projected_retrieval.ProjectionVectorMeasurement] = (),
    clips: Sequence[projected_retrieval.ProjectionClipMeasurement] = (),
    graphs: Sequence[projected_graph.ProjectionGraphMeasurement] = (),
):
    policy = Policy(fingerprint="f" * 64)
    key = projections.ProjectionNamespaceKey(
        policy.fingerprint,
        projections.PROJECTOR_SCHEMA_VERSION,
        1,
    )
    namespace = verified_namespace(key, tuple(items))
    snapshot = schema_v4.ActivePolicySnapshot(
        active=schema_v4.VerifiedActiveGovernanceState(
            logical_vault_id="fixture-vault",
            activation_store_id="fixture-store",
            activation_epoch=1,
            activation_state_digest=namespace.active_state_digest,
            policy_generation_id="fixture-policy",
            policy_fingerprint=key.policy_fingerprint,
            projector_schema_version=key.projector_schema_version,
            catalog_generation=key.catalog_generation,
            projection_namespace_id=key.namespace_id,
        ),
        policy=policy,
        source_documents=(),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(
            key, tuple(items)
        ),
        projection_namespace_evidence=b"fixture",
    )
    roots = []
    vector_index = None
    clip_index = None
    graph_index = None
    if vectors:
        vector_index = projected_retrieval.ProjectedVectorIndex(
            namespace,
            vectors,
            extractor_version=_VECTOR_EXTRACTOR,
            model_version=embeddings.MODEL_NAME,
        )
        roots.append(
            _measurement_root(
                key,
                lane="vector",
                extractor_version=_VECTOR_EXTRACTOR,
                model_version=embeddings.MODEL_NAME,
                measurement_count=len(vectors),
                vector_dimension=len(vectors[0].vector),
            )
        )
    if clips:
        clip_index = projected_retrieval.ProjectedClipIndex(
            namespace,
            clips,
            extractor_version=_CLIP_EXTRACTOR,
            model_version=embeddings.CLIP_MODEL_NAME,
        )
        roots.append(
            _measurement_root(
                key,
                lane="clip",
                extractor_version=_CLIP_EXTRACTOR,
                model_version=embeddings.CLIP_MODEL_NAME,
                measurement_count=len(clips),
                vector_dimension=len(clips[0].vector),
            )
        )
    if graphs:
        graph_index = projected_graph.ProjectedGraphIndex(
            namespace,
            tuple(graphs),
            extractor_version=_GRAPH_EXTRACTOR,
            model_version=_GRAPH_MODEL,
        )
        roots.append(
            _measurement_root(
                key,
                lane="graph",
                extractor_version=_GRAPH_EXTRACTOR,
                model_version=_GRAPH_MODEL,
                measurement_count=len(graphs),
                vector_dimension=None,
                graph_edge_count=sum(len(row.edges) for row in graphs),
            )
        )
    return projection_runtime.ActiveProjectionRuntime(
        snapshot,
        namespace,
        tuple(roots),
        vector_index=vector_index,
        clip_index=clip_index,
        graph_index=graph_index,
    )


def _vector(variant, value):
    return projected_retrieval.ProjectionVectorMeasurement(
        projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane="vector",
            extractor_version=_VECTOR_EXTRACTOR,
            model_version=embeddings.MODEL_NAME,
        ),
        value,
    )


def _clip(variant, value):
    return projected_retrieval.ProjectionClipMeasurement(
        projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane="clip",
            extractor_version=_CLIP_EXTRACTOR,
            model_version=embeddings.CLIP_MODEL_NAME,
        ),
        value,
    )


def _graph(variant, *targets):
    return projected_graph.ProjectionGraphMeasurement(
        projections.MeasurementKey(
            projection_variant_id=variant.projection_variant_id,
            lane="graph",
            extractor_version=_GRAPH_EXTRACTOR,
            model_version=_GRAPH_MODEL,
        ),
        tuple(
            projected_graph.ProjectionGraphEdge(
                source_item_identity=variant.item_identity,
                target_item_identity=target,
                relation_type="links_to",
            )
            for target in targets
        ),
    )


def test_hybrid_runtime_fuses_complete_projected_lanes_and_graph(monkeypatch, tmp_path):
    alpha = _variant(
        "Knowledge Base/alpha.md",
        "1" * 64,
        "alpha exact",
        fields={"media_type": "image"},
    )
    beta = _variant(
        "Knowledge Base/beta.md",
        "2" * 64,
        "alpha semantic",
        fields={"media_type": "image"},
    )
    gamma = _variant(
        "Knowledge Base/gamma.md",
        "3" * 64,
        "alpha graph",
        fields={"media_type": "image"},
    )
    items = tuple(_item(variant) for variant in (alpha, beta, gamma))
    runtime = _runtime(
        items,
        vectors=(
            _vector(alpha, (0.0, 1.0)),
            _vector(beta, (1.0, 0.0)),
            _vector(gamma, (0.2, 0.8)),
        ),
        clips=(
            _clip(alpha, (0.0, 1.0)),
            _clip(beta, (1.0, 0.0)),
            _clip(gamma, (0.2, 0.8)),
        ),
        graphs=(
            _graph(alpha, gamma.item_identity),
            _graph(beta, gamma.item_identity),
            _graph(gamma),
        ),
    )
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query: [[1.0, 0.0]],
    )
    monkeypatch.setattr(embeddings, "embed_clip_text", lambda query: [1.0, 0.0])

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="alpha",
        limit=3,
        mode="hybrid",
        graph=True,
        rerank=False,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert [hit.path for hit in result.hits] == [
        beta.item_identity,
        alpha.item_identity,
        gamma.item_identity,
    ]
    assert result.hits[0].vector_rank == 1
    assert result.hits[0].clip_rank == 1
    assert result.hits[2].graph_in_degree == 2
    assert result.hits[2].graph_hop is False
    assert result.warming_components == ()


def test_graph_lane_prefers_typed_edges_and_preserves_provenance(tmp_path):
    seed = _variant("Knowledge Base/seed.md", "e" * 64, "seed match")
    plain = _variant("Knowledge Base/a-plain.md", "f" * 64, "plain neighbor")
    typed = _variant("Knowledge Base/z-typed.md", "0" * 64, "typed neighbor")
    runtime = _runtime(
        tuple(_item(variant) for variant in (seed, plain, typed)),
        graphs=(
            projected_graph.ProjectionGraphMeasurement(
                projections.MeasurementKey(
                    projection_variant_id=seed.projection_variant_id,
                    lane="graph",
                    extractor_version=_GRAPH_EXTRACTOR,
                    model_version=_GRAPH_MODEL,
                ),
                (
                    projected_graph.ProjectionGraphEdge(
                        seed.item_identity,
                        plain.item_identity,
                        "links_to",
                    ),
                    projected_graph.ProjectionGraphEdge(
                        seed.item_identity,
                        typed.item_identity,
                        "supports",
                    ),
                ),
            ),
            _graph(plain),
            _graph(typed),
        ),
    )

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="seed",
        limit=3,
        mode="hybrid",
        graph=True,
        rerank=False,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert [hit.path for hit in result.hits] == [
        seed.item_identity,
        typed.item_identity,
        plain.item_identity,
    ]
    assert result.hits[1].graph_provenance is not None
    assert result.hits[1].graph_provenance.relation_type == "supports"
    assert result.hits[1].graph_provenance.direction == "outbound"
    assert result.hits[1].graph_provenance.seed == seed.item_identity
    assert result.hits[2].graph_provenance is not None
    assert result.hits[2].graph_provenance.relation_type == "links_to"


def test_graph_promotes_a_target_below_the_complete_vector_candidate_prefix(
    monkeypatch,
    tmp_path,
):
    seed = _variant("Knowledge Base/seed.md", "1" * 64, "seed")
    filler = _variant("Knowledge Base/filler.md", "2" * 64, "filler")
    target = _variant("Knowledge Base/target.md", "3" * 64, "target")
    runtime = _runtime(
        tuple(_item(variant) for variant in (seed, filler, target)),
        vectors=(
            _vector(seed, (1.0, 0.0)),
            _vector(filler, (0.5, 0.5)),
            _vector(target, (-1.0, 0.0)),
        ),
        graphs=(
            _graph(seed, target.item_identity),
            _graph(filler),
            _graph(target),
        ),
    )
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query: [[1.0, 0.0]],
    )

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="seed",
        limit=2,
        mode="hybrid",
        graph=True,
        rerank=False,
        rank_config=projection_runtime.ranking_config.RankingConfig(
            candidate_multiplier=1,
            candidate_floor=2,
            graph_seed_cap=1,
        ),
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert [hit.path for hit in result.hits] == [
        seed.item_identity,
        target.item_identity,
    ]
    assert result.hits[1].graph_hop is True


def test_graph_does_not_reintroduce_a_rejected_raw_bm25_candidate(tmp_path):
    seed = _variant("Knowledge Base/seed.md", "4" * 64, "what alpha")
    rejected = _variant("Knowledge Base/rejected.md", "5" * 64, "alpha only")
    runtime = _runtime(
        (_item(seed), _item(rejected)),
        graphs=(
            _graph(seed, rejected.item_identity),
            _graph(rejected),
        ),
    )

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="what is alpha",
        limit=2,
        mode="hybrid",
        graph=True,
        rerank=False,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert [hit.path for hit in result.hits] == [seed.item_identity]


def test_rerank_batches_every_projected_candidate_before_final_limit(
    monkeypatch, tmp_path
):
    alpha = _variant("Knowledge Base/alpha.md", "4" * 64, "alpha first")
    beta = _variant("Knowledge Base/beta.md", "5" * 64, "alpha second")
    runtime = _runtime((_item(alpha), _item(beta)))
    observed = []

    def rerank_pairs(query: str, passages: list[str]):
        observed.append((query, passages))
        return [0.1, 0.9]

    monkeypatch.setattr(embeddings, "rerank_pairs", rerank_pairs)

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="alpha",
        limit=1,
        mode="hybrid",
        graph=False,
        rerank=True,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert observed == [
        (
            "alpha",
            [
                "alpha first alpha.md",
                "alpha second beta.md",
            ],
        )
    ]
    assert [hit.path for hit in result.hits] == [beta.item_identity]
    assert result.hits[0].rerank_score == 0.9
    assert result.hits[0].rerank_input_rank == 2
    assert result.warming_components == ("vector",)


def test_incomplete_selected_vector_warms_without_reopening_raw_lane(
    monkeypatch, tmp_path
):
    alpha = _variant("Knowledge Base/alpha.md", "7" * 64, "alpha first")
    beta = _variant("Knowledge Base/beta.md", "8" * 64, "alpha second")
    runtime = _runtime(
        (_item(alpha), _item(beta)),
        vectors=(_vector(alpha, (1.0, 0.0)),),
    )
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query: [[1.0, 0.0]],
    )

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="alpha",
        limit=2,
        mode="hybrid",
        graph=False,
        rerank=False,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert [hit.path for hit in result.hits] == [
        alpha.item_identity,
        beta.item_identity,
    ]
    assert all(hit.vector_rank is None for hit in result.hits)
    assert result.warming_components == ("vector",)


def test_hybrid_retention_drops_weak_bm25_only_projection(tmp_path):
    weak = _variant("Knowledge Base/weak.md", "7" * 64, "alpha only")
    strong = _variant("Knowledge Base/strong.md", "8" * 64, "what alpha")
    runtime = _runtime((_item(weak), _item(strong)))

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="what is alpha",
        limit=2,
        mode="hybrid",
        graph=False,
        rerank=False,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert [hit.path for hit in result.hits] == [strong.item_identity]


def test_projected_rank_policy_applies_type_and_status_multipliers(tmp_path):
    stale_source = _variant(
        "Knowledge Base/a-source.md",
        "9" * 64,
        "alpha",
        fields={"type": "source", "status": "superseded"},
    )
    active_insight = _variant(
        "Knowledge Base/z-insight.md",
        "a" * 64,
        "alpha",
        fields={"type": "insight", "status": "active"},
    )
    runtime = _runtime((_item(stale_source), _item(active_insight)))

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="alpha",
        limit=2,
        mode="hybrid",
        graph=False,
        rerank=False,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert [hit.path for hit in result.hits] == [
        active_insight.item_identity,
        stale_source.item_identity,
    ]


def test_projected_rerank_applies_rank_policy_to_raw_scores(monkeypatch, tmp_path):
    stale_source = _variant(
        "Knowledge Base/a-source.md",
        "b" * 64,
        "alpha",
        fields={"type": "source", "status": "superseded"},
    )
    active_insight = _variant(
        "Knowledge Base/z-insight.md",
        "c" * 64,
        "alpha",
        fields={"type": "insight", "status": "active"},
    )
    runtime = _runtime((_item(stale_source), _item(active_insight)))
    monkeypatch.setattr(
        embeddings,
        "rerank_pairs",
        lambda _query, _passages: [0.8, 1.0],
    )

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="alpha",
        limit=2,
        mode="hybrid",
        graph=False,
        rerank=True,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert [hit.path for hit in result.hits] == [
        active_insight.item_identity,
        stale_source.item_identity,
    ]
    assert result.hits[0].rerank_raw_score == 0.8
    assert result.hits[0].rerank_score == pytest.approx(0.92)


def test_projected_auto_rerank_uses_public_policy(monkeypatch, tmp_path):
    alpha = _variant("Knowledge Base/alpha.md", "d" * 64, "one two three four five")
    runtime = _runtime((_item(alpha),))
    observed = []

    def rerank_pairs(query: str, passages: list[str]):
        observed.append((query, passages))
        return [1.0]

    monkeypatch.setattr(embeddings, "rerank_pairs", rerank_pairs)

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="one two three four five",
        limit=1,
        mode="hybrid",
        graph=False,
        rerank=None,
        auto_rerank=True,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert observed
    assert result.hits[0].rerank_score == 1.0


def test_projected_models_respect_hard_off_and_readiness(
    monkeypatch, tmp_path
):
    alpha = _variant(
        "Knowledge Base/alpha.md",
        "e" * 64,
        "alpha",
        fields={"media_type": "image"},
    )
    runtime = _runtime(
        (_item(alpha),),
        vectors=(_vector(alpha, (1.0, 0.0)),),
        clips=(_clip(alpha, (1.0, 0.0)),),
    )
    def model_called(*_args, **_kwargs):
        raise AssertionError("disabled or warming model must not run")
    monkeypatch.setattr(embeddings, "embed_texts", model_called)
    monkeypatch.setattr(embeddings, "embed_clip_text", model_called)
    monkeypatch.setattr(embeddings, "rerank_pairs", model_called)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")

    disabled = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="alpha",
        limit=1,
        mode="hybrid",
        graph=False,
        rerank=True,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )
    assert disabled.warming_components == ()

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP")
    monkeypatch.delenv("EXOMEM_DISABLE_RANKING")
    monkeypatch.setattr(readiness, "should_defer", lambda _component: True)
    warming = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="alpha",
        limit=1,
        mode="hybrid",
        graph=False,
        rerank=True,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )
    assert warming.warming_components == ("vector", "clip", "rerank")


def test_clip_lane_is_non_applicable_without_a_selected_l6_variant(
    monkeypatch, tmp_path
):
    alpha = _variant(
        "Knowledge Base/alpha.md",
        "d" * 64,
        "approved abstraction",
        level=3,
    )
    runtime = _runtime((_item(alpha),))
    monkeypatch.setattr(
        projection_authorization,
        "build_authorization_map",
        lambda *_args, **_kwargs: projected_retrieval.AuthorizationProjectionMap(
            runtime.namespace.namespace_key,
            (
                projected_retrieval.ProjectionSelection(
                    alpha.item_identity,
                    alpha.content_hash,
                    alpha.projection_variant_id,
                    decision=Decision(
                        level=3,
                        options={"abstract": "approved abstraction"},
                    ),
                ),
            ),
        ),
    )

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="approved",
        limit=1,
        mode="hybrid",
        graph=True,
        rerank=False,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert [hit.path for hit in result.hits] == [alpha.item_identity]
    assert result.hits[0].decision.level == 3
    assert result.warming_components == ("vector",)


def test_vector_runtime_does_not_invent_a_lexical_rank(monkeypatch, tmp_path):
    alpha = _variant("Knowledge Base/alpha.md", "b" * 64, "semantic only")
    runtime = _runtime(
        (_item(alpha),),
        vectors=(_vector(alpha, (1.0, 0.0)),),
    )
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query: [[1.0, 0.0]],
    )

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="unmatched",
        limit=1,
        mode="vector",
        graph=False,
        rerank=False,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert len(result.hits) == 1
    assert result.hits[0].vector_rank == 1
    assert result.hits[0].bm25_rank is None
    assert result.hits[0].keyword_rank is None


def test_keyword_runtime_does_not_admit_bm25_only_candidates(tmp_path):
    alpha = _variant("Knowledge Base/alpha.md", "9" * 64, "alpha only")
    beta = _variant("Knowledge Base/beta.md", "a" * 64, "beta only")
    runtime = _runtime((_item(alpha), _item(beta)))

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="alpha beta",
        limit=2,
        mode="keyword",
        graph=False,
        rerank=False,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert result.hits == ()


def test_keyword_runtime_ignores_rerank_like_the_public_keyword_mode(
    monkeypatch, tmp_path
):
    alpha = _variant("Knowledge Base/alpha.md", "c" * 64, "alpha only")
    runtime = _runtime((_item(alpha),))
    monkeypatch.setattr(
        embeddings,
        "rerank_pairs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("keyword mode must not call the reranker")
        ),
    )

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="alpha",
        limit=1,
        mode="keyword",
        graph=False,
        rerank=True,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    assert [hit.path for hit in result.hits] == [alpha.item_identity]
    assert result.warming_components == ()
    assert result.hits[0].rerank_score is None


def test_lane_result_caps_cover_the_repository_catalog_capacity():
    variant = _variant("Knowledge Base/only.md", "6" * 64, "only")
    item = _item(variant)
    namespace = verified_namespace(
        projections.ProjectionNamespaceKey(
            "f" * 64,
            projections.PROJECTOR_SCHEMA_VERSION,
            1,
        ),
        (item,),
    )
    authorization = projected_retrieval.AuthorizationProjectionMap(
        namespace.namespace_key,
        (
            projected_retrieval.ProjectionSelection(
                variant.item_identity,
                variant.content_hash,
                variant.projection_variant_id,
            ),
        ),
    )
    lexical = projected_retrieval.ProjectedLexicalIndex(namespace)

    assert lexical.search_bm25(
        authorization,
        "only",
        k=projections.MAX_GOVERNED_CATALOG_ITEMS,
    )
    graph = projected_graph.ProjectedGraphIndex(
        namespace,
        (_graph(variant),),
        extractor_version=_GRAPH_EXTRACTOR,
        model_version=_GRAPH_MODEL,
    ).authorize(authorization)
    assert graph.rank_by_in_degree(
        k=projections.MAX_GOVERNED_CATALOG_ITEMS
    ) == (variant.item_identity,)
