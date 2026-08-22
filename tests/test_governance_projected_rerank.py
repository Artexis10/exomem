"""Projected-only reranking before final top-k."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from governance_projection_support import verified_namespace

from exomem.governance import projected_retrieval, projection_store, projections
from exomem.governance.decisions import Decision


def _key() -> projections.ProjectionNamespaceKey:
    return projections.ProjectionNamespaceKey(
        policy_fingerprint="c" * 64,
        projector_schema_version=1,
        catalog_generation=14,
    )


def _variant(
    item_identity: str,
    content_hash: str,
    *,
    level: int,
    text: str,
) -> projections.ProjectionVariant:
    option_key = {1: "notice", 2: "constraint", 3: "abstract"}.get(level)
    variant = projections.build_projection_variant(
        item_identity=item_identity,
        content_hash=content_hash,
        decision=Decision(
            level=level,
            options={} if option_key is None else {option_key: text},
        ),
        projector_schema_version=1,
        full_search_fields={"body": text, "title": item_identity},
    )
    assert variant is not None
    return variant


def _item(
    item_identity: str,
    content_hash: str,
    *variants: projections.ProjectionVariant,
) -> projection_store.ProjectionItemVariants:
    return projection_store.ProjectionItemVariants(
        item_identity=item_identity,
        content_hash=content_hash,
        variants=variants,
    )


def _namespace(
    *items: projection_store.ProjectionItemVariants,
) -> projection_store.VerifiedProjectionNamespace:
    return verified_namespace(_key(), items)


def _map(
    *pairs: tuple[
        projection_store.ProjectionItemVariants,
        projections.ProjectionVariant | None,
    ],
) -> projected_retrieval.AuthorizationProjectionMap:
    return projected_retrieval.AuthorizationProjectionMap(
        _key(),
        tuple(
            projected_retrieval.ProjectionSelection(
                item_identity=item.item_identity,
                content_hash=item.content_hash,
                projection_variant_id=(
                    None if variant is None else variant.projection_variant_id
                ),
            )
            for item, variant in pairs
        ),
    )


def test_reranker_receives_only_the_selected_projection_fields() -> None:
    low = _variant("shared", "1" * 64, level=2, text="approved abstraction")
    full = _variant("shared", "1" * 64, level=6, text="raw hidden body")
    item = _item("shared", "1" * 64, low, full)
    reranker = projected_retrieval.ProjectedReranker(_namespace(item))
    observed: list[Mapping[str, str]] = []

    def scorer(_query: str, fields: Mapping[str, str]) -> float:
        observed.append(fields)
        return 1.0 if "approved" in " ".join(fields.values()) else 0.0

    hit = reranker.rerank(
        _map((item, low)),
        "approved",
        ("shared",),
        scorer=scorer,
        k=1,
    )[0]

    assert observed == [{"constraint": "approved abstraction"}]
    assert hit.projection_variant_id == low.projection_variant_id
    assert "raw hidden body" not in hit.snippet


def test_raw_or_l0_candidate_is_refused_before_scorer_invocation() -> None:
    visible = _variant("visible", "2" * 64, level=3, text="visible")
    hidden = _variant("hidden", "3" * 64, level=6, text="hidden")
    visible_item = _item("visible", "2" * 64, visible)
    hidden_item = _item("hidden", "3" * 64, hidden)
    reranker = projected_retrieval.ProjectedReranker(
        _namespace(visible_item, hidden_item)
    )
    calls = 0

    def scorer(_query: str, _fields: Mapping[str, str]) -> float:
        nonlocal calls
        calls += 1
        return 1.0

    with pytest.raises(
        projected_retrieval.ProjectedRetrievalUnavailable,
        match="candidate",
    ):
        reranker.rerank(
            _map((visible_item, visible), (hidden_item, None)),
            "hidden",
            ("hidden", "visible"),
            scorer=scorer,
            k=1,
        )
    assert calls == 0


def test_reranking_scores_complete_projected_candidates_before_top_k() -> None:
    variants = tuple(
        _variant(
            f"item-{index}",
            f"{index + 10:064x}",
            level=3,
            text=f"score-{index}",
        )
        for index in range(5)
    )
    items = tuple(
        _item(variant.item_identity, variant.content_hash, variant)
        for variant in variants
    )
    reranker = projected_retrieval.ProjectedReranker(_namespace(*items))

    hits = reranker.rerank(
        _map(*zip(items, variants, strict=True)),
        "score",
        tuple(item.item_identity for item in items),
        scorer=lambda _query, fields: float(
            next(iter(fields.values())).removeprefix("score-")
        ),
        k=2,
    )

    assert [hit.item_identity for hit in hits] == ["item-4", "item-3"]


def test_rerank_score_must_be_finite_and_ties_preserve_projected_order() -> None:
    first = _variant("first", "4" * 64, level=1, text="first")
    second = _variant("second", "5" * 64, level=1, text="second")
    first_item = _item("first", "4" * 64, first)
    second_item = _item("second", "5" * 64, second)
    reranker = projected_retrieval.ProjectedReranker(
        _namespace(first_item, second_item)
    )
    authorization = _map((first_item, first), (second_item, second))

    hits = reranker.rerank(
        authorization,
        "same",
        ("second", "first"),
        scorer=lambda _query, _fields: 0.5,
        k=2,
    )
    assert [hit.item_identity for hit in hits] == ["second", "first"]

    with pytest.raises(projected_retrieval.ProjectedLaneUnavailable, match="finite"):
        reranker.rerank(
            authorization,
            "bad",
            ("first",),
            scorer=lambda _query, _fields: float("nan"),
            k=1,
        )
