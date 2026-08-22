"""Projected-corpus lexical acquisition and request-local authorization."""

from __future__ import annotations

from dataclasses import replace

import pytest
from governance_projection_support import verified_namespace

from exomem.governance import projected_retrieval, projection_store, projections
from exomem.governance.decisions import Decision


def _key() -> projections.ProjectionNamespaceKey:
    return projections.ProjectionNamespaceKey(
        policy_fingerprint="a" * 64,
        projector_schema_version=1,
        catalog_generation=9,
    )


def _variant(
    item_identity: str,
    content_hash: str,
    *,
    level: int,
    text: str,
) -> projections.ProjectionVariant:
    option_key = {1: "notice", 2: "constraint", 3: "abstract"}.get(level)
    options = {} if option_key is None else {option_key: text}
    full_fields = {"body": text, "title": f"title {item_identity}"}
    variant = projections.build_projection_variant(
        item_identity=item_identity,
        content_hash=content_hash,
        decision=Decision(level=level, options=options),
        projector_schema_version=1,
        full_search_fields=full_fields,
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


def _selection(
    item: projection_store.ProjectionItemVariants,
    variant: projections.ProjectionVariant | None,
) -> projected_retrieval.ProjectionSelection:
    return projected_retrieval.ProjectionSelection(
        item_identity=item.item_identity,
        content_hash=item.content_hash,
        projection_variant_id=(
            None if variant is None else variant.projection_variant_id
        ),
    )


def test_hidden_present_and_physically_absent_have_identical_bm25_envelopes() -> None:
    visible_a = _variant("visible-a", "1" * 64, level=6, text="alpha alpha permitted")
    visible_b = _variant("visible-b", "2" * 64, level=6, text="alpha permitted")
    hidden = _variant("hidden", "3" * 64, level=6, text="alpha alpha alpha raw secret")
    item_a = _item("visible-a", "1" * 64, visible_a)
    item_b = _item("visible-b", "2" * 64, visible_b)
    hidden_item = _item("hidden", "3" * 64, hidden)

    present_index = projected_retrieval.ProjectedLexicalIndex(
        _namespace(item_a, item_b, hidden_item)
    )
    absent_index = projected_retrieval.ProjectedLexicalIndex(_namespace(item_a, item_b))
    present_map = projected_retrieval.AuthorizationProjectionMap(
        _key(),
        (
            _selection(item_a, visible_a),
            _selection(item_b, visible_b),
            _selection(hidden_item, None),
        ),
    )
    absent_map = projected_retrieval.AuthorizationProjectionMap(
        _key(),
        (_selection(item_a, visible_a), _selection(item_b, visible_b)),
    )

    assert present_index.search_bm25(present_map, "alpha", k=2) == (
        absent_index.search_bm25(absent_map, "alpha", k=2)
    )


def test_projection_only_term_acquires_before_cap_and_raw_only_term_does_not() -> None:
    projected = _variant(
        "projected",
        "4" * 64,
        level=2,
        text="approved-projection-term",
    )
    item = _item("projected", "4" * 64, projected)
    index = projected_retrieval.ProjectedLexicalIndex(_namespace(item))
    authorization = projected_retrieval.AuthorizationProjectionMap(
        _key(), (_selection(item, projected),)
    )

    hits = index.search_bm25(authorization, "approved projection term", k=1)

    assert [hit.item_identity for hit in hits] == ["projected"]
    assert hits[0].search_fields == {"constraint": "approved-projection-term"}
    assert index.search_bm25(authorization, "raw-secret-source", k=1) == ()


def test_bm25_query_terms_keep_any_term_candidate_semantics() -> None:
    alpha = _variant("alpha-item", "b" * 64, level=6, text="alpha only")
    beta = _variant("beta-item", "c" * 64, level=6, text="beta only")
    alpha_item = _item("alpha-item", "b" * 64, alpha)
    beta_item = _item("beta-item", "c" * 64, beta)
    index = projected_retrieval.ProjectedLexicalIndex(
        _namespace(alpha_item, beta_item)
    )
    authorization = projected_retrieval.AuthorizationProjectionMap(
        _key(),
        (_selection(alpha_item, alpha), _selection(beta_item, beta)),
    )

    hits = index.search_bm25(authorization, "alpha beta", k=2)

    assert {hit.item_identity for hit in hits} == {"alpha-item", "beta-item"}


def test_l5_later_source_term_cannot_acquire_or_open_a_hidden_snippet() -> None:
    body = " ".join(["visible"] * 100) + " raw-hidden-later-term"
    l5 = _variant("l5", "5" * 64, level=5, text=body)
    item = _item("l5", "5" * 64, l5)
    index = projected_retrieval.ProjectedLexicalIndex(_namespace(item))
    authorization = projected_retrieval.AuthorizationProjectionMap(
        _key(), (_selection(item, l5),)
    )

    assert index.search_bm25(authorization, "raw hidden later term", k=1) == ()
    hit = index.search_bm25(authorization, "visible", k=1)[0]
    assert hit.snippet in l5.search_fields["body"]
    assert "raw-hidden-later-term" not in hit.snippet


def test_hidden_items_do_not_consume_keyword_or_bm25_candidate_caps() -> None:
    visible = _variant("visible", "6" * 64, level=3, text="needle")
    visible_item = _item("visible", "6" * 64, visible)
    hidden_items = tuple(
        _item(
            f"hidden-{index:03d}",
            f"{index + 10:064x}",
            _variant(
                f"hidden-{index:03d}",
                f"{index + 10:064x}",
                level=6,
                text="needle needle needle",
            ),
        )
        for index in range(80)
    )
    index = projected_retrieval.ProjectedLexicalIndex(
        _namespace(*hidden_items, visible_item)
    )
    authorization = projected_retrieval.AuthorizationProjectionMap(
        _key(),
        (
            *(_selection(item, None) for item in hidden_items),
            _selection(visible_item, visible),
        ),
    )

    assert [hit.item_identity for hit in index.search_bm25(authorization, "needle", k=1)] == [
        "visible"
    ]
    assert [
        hit.item_identity for hit in index.search_keyword(authorization, "needle", k=1)
    ] == ["visible"]


def test_authorization_map_is_exact_total_and_namespace_bound() -> None:
    first = _variant("first", "7" * 64, level=1, text="first")
    second = _variant("second", "8" * 64, level=1, text="second")
    first_item = _item("first", "7" * 64, first)
    second_item = _item("second", "8" * 64, second)
    index = projected_retrieval.ProjectedLexicalIndex(
        _namespace(first_item, second_item)
    )

    incomplete = projected_retrieval.AuthorizationProjectionMap(
        _key(), (_selection(first_item, first),)
    )
    with pytest.raises(projected_retrieval.ProjectedRetrievalUnavailable, match="catalog"):
        index.search_bm25(incomplete, "first", k=1)

    wrong_namespace = projected_retrieval.AuthorizationProjectionMap(
        replace(_key(), catalog_generation=10),
        (_selection(first_item, first), _selection(second_item, None)),
    )
    with pytest.raises(projected_retrieval.ProjectedRetrievalUnavailable, match="namespace"):
        index.search_bm25(wrong_namespace, "first", k=1)

    wrong_row = projected_retrieval.AuthorizationProjectionMap(
        _key(),
        (
            replace(_selection(first_item, first), projection_variant_id="f" * 64),
            _selection(second_item, None),
        ),
    )
    with pytest.raises(projected_retrieval.ProjectedRetrievalUnavailable, match="variant"):
        index.search_bm25(wrong_row, "first", k=1)


def test_same_persistent_rows_support_distinct_request_local_maps() -> None:
    low = _variant("shared", "9" * 64, level=1, text="limited notice")
    full = _variant("shared", "9" * 64, level=6, text="complete permitted body")
    item = _item("shared", "9" * 64, low, full)
    index = projected_retrieval.ProjectedLexicalIndex(_namespace(item))
    low_map = projected_retrieval.AuthorizationProjectionMap(
        _key(), (_selection(item, low),)
    )
    full_map = projected_retrieval.AuthorizationProjectionMap(
        _key(), (_selection(item, full),)
    )

    assert index.search_bm25(low_map, "complete", k=1) == ()
    assert [hit.decision_level for hit in index.search_bm25(full_map, "complete", k=1)] == [6]


def test_keyword_mode_retains_strict_substring_not_stem_semantics() -> None:
    variant = _variant("keyword", "a" * 64, level=6, text="compounding approved")
    item = _item("keyword", "a" * 64, variant)
    index = projected_retrieval.ProjectedLexicalIndex(_namespace(item))
    authorization = projected_retrieval.AuthorizationProjectionMap(
        _key(), (_selection(item, variant),)
    )

    assert [
        hit.item_identity
        for hit in index.search_keyword(authorization, "compound approve", k=1)
    ] == ["keyword"]
    assert index.search_keyword(authorization, "compounded", k=1) == ()
