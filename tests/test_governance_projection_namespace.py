"""Immutable authorization-projection namespace and variant contracts."""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import replace

import pytest

from exomem.governance import bridges, projections
from exomem.governance.decisions import Decision, decide
from exomem.governance.policy import Policy, Rule, Scope, StandingGrant
from exomem.governance.principal import OWNER_AUDIENCE


def test_namespace_key_excludes_content_and_measurement_versions() -> None:
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint="a" * 64,
        projector_schema_version=3,
        catalog_generation=17,
    )

    assert key.as_tuple() == ("a" * 64, 3, 17)
    assert key.namespace_id == "3941eaec3d595ef5bdd14d3be33a1ef270586ddbbf9edca87012328045687205"

    first = projections.MeasurementKey(
        projection_variant_id="b" * 64,
        lane="vector",
        extractor_version="extractor-v1",
        model_version="model-v1",
    )
    second = projections.MeasurementKey(
        projection_variant_id="b" * 64,
        lane="vector",
        extractor_version="extractor-v2",
        model_version="model-v2",
    )
    assert first != second
    assert key.as_tuple() == ("a" * 64, 3, 17)


def test_closed_jcs_uses_utf16_key_order_and_rejects_unclosed_values() -> None:
    # RFC 8785 sorts object keys by UTF-16 code units.  Python's default
    # code-point order would put U+E000 before U+10000 and fail this vector.
    assert projections.canonical_jcs({"\ue000": 1, "\U00010000": 2}) == (
        '{"\U00010000":2,"\ue000":1}'.encode()
    )

    for unsupported in (1.5, float("nan"), {"value"}, 2**53):
        with pytest.raises(projections.ProjectionCanonicalizationError):
            projections.canonical_jcs({"unsupported": unsupported})


def test_projection_identities_refuse_type_confusion_content_free() -> None:
    with pytest.raises(
        projections.ProjectionCanonicalizationError,
        match="policy_fingerprint",
    ):
        projections.ProjectionNamespaceKey(  # type: ignore[arg-type]
            policy_fingerprint=None,
            projector_schema_version=1,
            catalog_generation=1,
        )


def test_repository_governed_retrieval_limits_are_fixed() -> None:
    assert projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_MS == 25
    assert projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_RATIO == 0.10
    assert projections.MAX_GOVERNED_CATALOG_ITEMS == 16_384
    assert projections.MAX_GOVERNED_SEARCH_BYTES_PER_ITEM == 1_048_576
    assert projections.MAX_GOVERNED_GRAPH_EDGES == 262_144

    for name, value in (
        ("catalog_items", projections.MAX_GOVERNED_CATALOG_ITEMS + 1),
        ("searchable_bytes", projections.MAX_GOVERNED_SEARCH_BYTES_PER_ITEM + 1),
        ("graph_edges", projections.MAX_GOVERNED_GRAPH_EDGES + 1),
    ):
        with pytest.raises(projections.ProjectionCapacityExceeded, match=name):
            projections.require_supported_capacity(**{name: value})


@pytest.mark.parametrize(
    "fields",
    (
        {"body": "é" * 524_289},
        {"body": "a" * 600_000, "title": "b" * 600_000},
    ),
)
def test_searchable_item_capacity_is_aggregate_utf8_bytes(
    fields: dict[str, str],
) -> None:
    with pytest.raises(projections.ProjectionCapacityExceeded, match="searchable bytes"):
        projections.build_projection_variant(
            item_identity="oversized",
            content_hash="f" * 64,
            decision=Decision(level=6),
            projector_schema_version=1,
            full_search_fields=fields,
        )


def test_projection_variant_id_has_one_fixed_cross_runtime_preimage() -> None:
    decision = Decision(
        level=4,
        options={"bridge": "bridge-alpha", "notice": "Restricted"},
        bridge="bridge-alpha",
        bridge_abstraction="Approved abstraction",
        release_grant_id="release-grant-alpha",
        release_strip=(
            bridges.StripIdentity(
                path="Knowledge Base/Notes/source.md",
                ref="exomem://memory/source",
                title="Source",
            ),
        ),
        release_dependency_digest="b" * 64,
    )
    variant = projections.build_projection_variant(
        item_identity="exomem://memory/item-alpha",
        content_hash="c" * 64,
        decision=decision,
        projector_schema_version=3,
        full_search_fields={"body": "hidden", "title": "Hidden"},
    )

    assert variant is not None
    assert variant.search_fields == {"bridge": "Approved abstraction"}
    assert variant.value_jcs == (
        b'{"bridge_dependency_content_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"bridge_id":"release-grant-alpha",'
        b'"content_hash":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        b'"decision_level":4,"item_identity":"exomem://memory/item-alpha",'
        b'"options":{"bridge":"bridge-alpha","notice":"Restricted"},'
        b'"projector_schema_version":3,'
        b'"release_strip":[{"path":"Knowledge Base/Notes/source.md",'
        b'"ref":"exomem://memory/source","title":"Source"}]}'
    )
    assert variant.projection_variant_id == hashlib.sha256(
        b"exomem.authorization-projection.v1\0" + variant.value_jcs
    ).hexdigest()
    assert variant.projection_variant_id == (
        "73443edfba4a88720c4fa335e5f7288bed37242fec60e8c58c166f3a809bdcc4"
    )


def test_release_strip_set_uses_cross_runtime_utf16_tuple_order() -> None:
    variant = projections.build_projection_variant(
        item_identity="item-strip-order",
        content_hash="8" * 64,
        decision=Decision(
            level=6,
            release_strip=(
                bridges.StripIdentity(path="\ue000", ref="ref-b", title="B"),
                bridges.StripIdentity(path="\U00010000", ref="ref-a", title="A"),
            ),
        ),
        projector_schema_version=1,
        full_search_fields={"body": "permitted"},
    )

    assert variant is not None
    value = json.loads(variant.value_jcs)
    assert [item["path"] for item in value["release_strip"]] == [
        "\U00010000",
        "\ue000",
    ]


def test_projection_variant_constructor_refuses_value_or_level_shape_mismatch() -> None:
    variant = projections.build_projection_variant(
        item_identity="item-bound",
        content_hash="9" * 64,
        decision=Decision(level=1, options={"notice": "Restricted"}),
        projector_schema_version=1,
        full_search_fields={"body": "hidden"},
    )
    assert variant is not None

    with pytest.raises(
        projections.ProjectionCanonicalizationError,
        match="item_identity",
    ):
        projections.ProjectionVariant(
            projection_variant_id=variant.projection_variant_id,
            item_identity="different-item",
            content_hash=variant.content_hash,
            decision_level=variant.decision_level,
            value_jcs=variant.value_jcs,
            search_fields=variant.search_fields,
        )
    with pytest.raises(
        projections.ProjectionCanonicalizationError,
        match="search field shape",
    ):
        projections.ProjectionVariant(
            projection_variant_id=variant.projection_variant_id,
            item_identity=variant.item_identity,
            content_hash=variant.content_hash,
            decision_level=variant.decision_level,
            value_jcs=variant.value_jcs,
            search_fields={"body": "Restricted"},
        )


@pytest.mark.parametrize(
    ("decision", "expected"),
    (
        (Decision(level=0), None),
        (
            Decision(level=1, options={"notice": "Restricted"}),
            {"notice": "Restricted"},
        ),
        (
            Decision(level=2, options={"constraint": "Internal review only"}),
            {"constraint": "Internal review only"},
        ),
        (
            Decision(level=3, options={"abstract": "Approved summary"}),
            {"abstract": "Approved summary"},
        ),
    ),
)
def test_low_level_fixed_search_projection(
    decision: Decision,
    expected: dict[str, str] | None,
) -> None:
    variant = projections.build_projection_variant(
        item_identity="item-low",
        content_hash="d" * 64,
        decision=decision,
        projector_schema_version=1,
        full_search_fields={"body": "hidden raw body"},
    )

    assert (None if variant is None else variant.search_fields) == expected


def test_l5_projection_is_query_independent_first_600_code_points() -> None:
    body = " ".join(["visible"] * 90) + " hidden-later-term"
    variant = projections.build_projection_variant(
        item_identity="item-l5",
        content_hash="e" * 64,
        decision=Decision(level=5),
        projector_schema_version=1,
        full_search_fields={"body": body, "title": "Secret title"},
    )

    assert variant is not None
    assert set(variant.search_fields) == {"body"}
    excerpt = variant.search_fields["body"]
    assert len(excerpt.removesuffix(" …")) <= 600
    assert excerpt.endswith(" …")
    assert "hidden-later-term" not in excerpt
    assert excerpt == projections.fixed_excerpt(body)


@pytest.mark.parametrize(
    ("decision", "expected_level", "expected_fields"),
    (
        (Decision(level=1), None, None),
        (
            Decision(level=2, options={"notice": "Safe lower notice"}),
            1,
            {"notice": "Safe lower notice"},
        ),
        (
            Decision(level=3, options={"constraint": "Safe lower constraint"}),
            2,
            {"constraint": "Safe lower constraint"},
        ),
        (
            Decision(level=4, options={"abstract": "Safe lower abstract"}),
            3,
            {"abstract": "Safe lower abstract"},
        ),
    ),
)
def test_missing_level_content_lowers_only_within_the_same_decision(
    decision: Decision,
    expected_level: int | None,
    expected_fields: dict[str, str] | None,
) -> None:
    variant = projections.build_projection_variant(
        item_identity="item-missing",
        content_hash="f" * 64,
        decision=decision,
        projector_schema_version=1,
        full_search_fields={"body": "hidden"},
    )
    assert (None if variant is None else variant.decision_level) == expected_level
    assert (None if variant is None else variant.search_fields) == expected_fields


def test_variants_deduplicate_and_refuse_the_257th_unique_output() -> None:
    variants = []
    for index in range(projections.MAX_PROJECTION_VARIANTS_PER_ITEM + 1):
        variant = projections.build_projection_variant(
            item_identity="item-overflow",
            content_hash="1" * 64,
            decision=Decision(level=1, options={"notice": f"notice-{index}"}),
            projector_schema_version=1,
            full_search_fields={"body": "hidden"},
        )
        assert variant is not None
        variants.append(variant)

    assert len(projections.deduplicate_variants(variants[:256])) == 256
    assert len(projections.deduplicate_variants([variants[0], variants[0]])) == 1
    with pytest.raises(projections.ProjectionVariantOverflow, match="256"):
        projections.deduplicate_variants(variants)


def test_same_variant_id_with_different_search_projection_is_corrupt() -> None:
    variant = projections.build_projection_variant(
        item_identity="item-corrupt",
        content_hash="2" * 64,
        decision=Decision(level=6),
        projector_schema_version=1,
        full_search_fields={"body": "first"},
    )
    assert variant is not None
    corrupt = projections.ProjectionVariant(
        projection_variant_id=variant.projection_variant_id,
        item_identity=variant.item_identity,
        content_hash=variant.content_hash,
        decision_level=variant.decision_level,
        value_jcs=variant.value_jcs,
        search_fields={"body": "second"},
    )

    with pytest.raises(projections.ProjectionVariantMismatch):
        projections.deduplicate_variants((variant, corrupt))


def test_enumerator_covers_finite_audience_purpose_and_session_grant_domain() -> None:
    scope = Scope(
        id="scope-alpha",
        source="scopes/alpha.yaml",
        constraint="Approved readers only",
        default_deny=True,
    )
    policy = Policy(
        fingerprint="3" * 64,
        scopes={scope.id: scope},
        rules=(
            Rule(
                id="rule-alpha",
                source="rules/alpha.yaml",
                scope_ids=(scope.id,),
                audience="audience-alpha",
                ceiling=3,
                options={
                    "notice": "Restricted",
                    "constraint": "Approved readers only",
                    "abstract": "Approved summary",
                    "bridge": "bridge-alpha",
                },
            ),
            Rule(
                id="cap-review",
                source="rules/cap-review.yaml",
                scope_ids=(scope.id,),
                audience="audience-alpha",
                ceiling=6,
                purpose="review",
                purpose_condition="outside",
                kind="org_cap",
                options={
                    "notice": "Restricted",
                    "constraint": "Approved readers only",
                    "abstract": "Approved summary",
                    "bridge": "bridge-alpha",
                },
            ),
        ),
    )

    def resolve_bridge(_audience: str, _purpose: str | None, decision: Decision) -> Decision:
        if decision.level == 4 and decision.bridge == "bridge-alpha":
            return replace(
                decision,
                bridge_abstraction="Approved bridge summary",
                release_grant_id="release-alpha",
                release_dependency_digest="4" * 64,
            )
        return decision

    variants = projections.enumerate_projection_variants(
        item_identity="exomem://memory/enumerated",
        content_hash="5" * 64,
        scope_ids=(scope.id,),
        policy=policy,
        projector_schema_version=1,
        full_search_fields={"title": "Full title", "body": "Full body"},
        resolve_decision=resolve_bridge,
    )

    assert {variant.decision_level for variant in variants} == {2, 3, 4, 5, 6}
    assert any(variant.search_fields == {"bridge": "Approved bridge summary"} for variant in variants)
    assert any(variant.search_fields == {"body": "Full body"} for variant in variants)
    serialized = b"\n".join(variant.value_jcs for variant in variants)
    assert b"audience-alpha" not in serialized
    assert b"projection-unlisted" not in serialized
    assert b"review" not in serialized
    assert b"session" not in serialized


def test_finite_enumerator_matches_exhaustive_two_scope_grant_domain() -> None:
    first = Scope(
        id="scope-first",
        source="scopes/first.yaml",
        default_deny=True,
        constraint="First constraint",
    )
    second = Scope(
        id="scope-second",
        source="scopes/second.yaml",
        default_deny=True,
        constraint="Second constraint",
    )
    policy = Policy(
        fingerprint="a" * 64,
        scopes={first.id: first, second.id: second},
        rules=(
            Rule(
                id="rule-first",
                source="rules/first.yaml",
                scope_ids=(first.id,),
                audience="audience-a",
                ceiling=5,
                options={
                    "notice": "Restricted",
                    "constraint": "First constraint",
                    "abstract": "Shared abstract",
                },
            ),
            Rule(
                id="rule-second",
                source="rules/second.yaml",
                scope_ids=(second.id,),
                audience="audience-a",
                ceiling=4,
                purpose="review",
                purpose_condition="outside",
                options={
                    "notice": "Restricted",
                    "constraint": "Second constraint",
                    "abstract": "Shared abstract",
                },
            ),
        ),
    )
    kwargs = {
        "item_identity": "item-exhaustive",
        "content_hash": "b" * 64,
        "projector_schema_version": 1,
        "full_search_fields": {"title": "Full title", "body": "Full body"},
    }

    actual = projections.enumerate_projection_variants(
        scope_ids=(first.id, second.id),
        policy=policy,
        **kwargs,
    )

    expected_ids: set[str] = set()
    audiences = (
        OWNER_AUDIENCE,
        "audience-a",
        "projection-unlisted-audience-0",
    )
    purposes = (None, "review", "projection-unlisted-purpose-0")
    for audience, purpose, ceilings in itertools.product(
        audiences,
        purposes,
        itertools.product((None, *range(7)), repeat=2),
    ):
        grants = tuple(
            StandingGrant(
                id=f"grant-{index}-{ceiling}",
                source="exhaustive-test",
                scope_ids=(scope_id,),
                audience=audience,
                ceiling=ceiling,
            )
            for index, (scope_id, ceiling) in enumerate(
                zip((first.id, second.id), ceilings, strict=True)
            )
            if ceiling is not None
        )
        decision = decide(
            (first.id, second.id),
            audience=audience,
            purpose=purpose,
            policy=policy,
            active_grants=grants,
        )
        variant = projections.build_projection_variant(
            decision=decision,
            **kwargs,
        )
        if variant is not None:
            expected_ids.add(variant.projection_variant_id)

    assert {variant.projection_variant_id for variant in actual} == expected_ids


def test_enumerator_refuses_257_unique_reachable_outputs() -> None:
    scope = Scope(
        id="scope-overflow",
        source="scopes/overflow.yaml",
        default_deny=True,
    )
    rules = tuple(
        Rule(
            id=f"rule-{index}",
            source=f"rules/{index}.yaml",
            scope_ids=(scope.id,),
            audience=f"audience-{index}",
            ceiling=1,
            options={"notice": f"notice-{index}"},
        )
        for index in range(257)
    )
    policy = Policy(
        fingerprint="6" * 64,
        scopes={scope.id: scope},
        rules=rules,
    )

    with pytest.raises(projections.ProjectionVariantOverflow, match="256"):
        projections.enumerate_projection_variants(
            item_identity="item-overflow-enumerated",
            content_hash="7" * 64,
            scope_ids=(scope.id,),
            policy=policy,
            projector_schema_version=1,
            full_search_fields={"body": "full"},
        )
