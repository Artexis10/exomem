from __future__ import annotations

import pytest

from exomem import entity_types, relation_registry, vocabulary_evidence


def _entity_registry() -> entity_types.EntityTypeRegistry:
    return entity_types.load_entity_types(
        proposal={
            "schema_version": 1,
            "entity_types": {
                "place": {
                    "folder": "Places",
                    "label": "Place",
                    "aliases": ["location"],
                    "capture_guidance": "A durable location identity.",
                    "status": "active",
                },
                "venue": {
                    "folder": "Venues",
                    "label": "Venue",
                    "aliases": ["site"],
                    "capture_guidance": "A durable gathering location identity.",
                    "parent": "organization",
                    "status": "deprecated",
                },
            },
        }
    )


def _relation_registry() -> relation_registry.RelationRegistry:
    return relation_registry.load_registry(
        proposal={
            "schema_version": 1,
            "extensions": {
                "vault.supplies": {
                    "parent": "relates_to",
                    "description": "One party supplies goods to another.",
                    "direction": "directed",
                    "aliases": ["provides_goods"],
                },
                "vault.supports": {
                    "parent": "relates_to",
                    "description": "One party supports another without supplying goods.",
                    "direction": "directed",
                    "aliases": ["assists"],
                },
            },
        }
    )


def test_entity_type_evidence_returns_complete_core_and_exact_alias_and_folder() -> None:
    registry = _entity_registry()
    before = registry.active_definitions

    alias_result = vocabulary_evidence.resolve_entity_type(registry, requested_type="location")
    folder_result = vocabulary_evidence.resolve_entity_type(registry, requested_type="Places")

    assert {item["id"] for item in alias_result["core_vocabulary"]} == set(
        entity_types.ENTITY_TYPE_IDS
    )
    exact = alias_result["exact_matches"][0]
    assert exact["match"] == "alias"
    assert exact["requested_type"] == "location"
    assert exact["canonical"] == "place"
    assert exact["folder"] == "Places"
    assert exact["aliases"] == ["location"]
    assert exact["description"] == "A durable location identity."
    assert exact["status"] == "active"
    assert folder_result["exact_matches"][0]["match"] == "folder"
    assert registry.active_definitions == before
    assert alias_result["selected_type"] is None
    assert alias_result["proposed_type"] is None


def test_entity_type_evidence_keeps_nearby_and_deprecated_meanings_as_candidates() -> None:
    registry = _entity_registry()
    result = vocabulary_evidence.resolve_entity_type(registry, query="location")
    deprecated = vocabulary_evidence.resolve_entity_type(registry, requested_type="venue")

    candidates = {item["id"]: item for item in result["candidates"]}
    assert candidates["place"]["description"] == "A durable location identity."
    assert candidates["place"]["aliases"] == ["location"]
    assert candidates["venue"]["parent"] == "organization"
    assert candidates["venue"]["status"] == "deprecated"
    assert deprecated["exact_matches"][0]["status"] == "deprecated"
    assert result["selected_type"] is None


def test_entity_type_evidence_refuses_missing_or_stale_continuation() -> None:
    registry = _entity_registry()
    with pytest.raises(ValueError, match="ENTITY_TYPE_QUERY_REQUIRED"):
        vocabulary_evidence.resolve_entity_type(registry)
    with pytest.raises(ValueError, match="ENTITY_TYPE_LIMIT_INVALID"):
        vocabulary_evidence.resolve_entity_type(registry, query="place", limit=0)

    first = vocabulary_evidence.resolve_entity_type(registry, query="place", limit=1)
    token = first["extensions"]["continuation"]
    assert token is not None
    second = vocabulary_evidence.resolve_entity_type(
        registry, query="place", limit=1, continuation=token
    )
    assert second["extensions"]["returned"] == 1
    with pytest.raises(ValueError, match="VOCABULARY_CONTINUATION_STALE"):
        vocabulary_evidence.resolve_entity_type(
            registry, query="venue", limit=1, continuation=token
        )
    with pytest.raises(ValueError, match="VOCABULARY_CONTINUATION_INVALID"):
        vocabulary_evidence.resolve_entity_type(registry, query="place", limit=1, continuation="a")


def test_relation_evidence_composes_resolver_without_selecting_nearby_meaning() -> None:
    result = vocabulary_evidence.relation_evidence(
        _relation_registry(), requested_relation="provides_goods"
    )

    assert result["exact_matches"][0]["canonical"] == "vault.supplies"
    assert {item["canonical"] for item in result["candidates"]} == {
        "vault.supplies",
        "vault.supports",
    }
    assert result["selected_relation"] is None
    assert result["proposed_relation"] is None


def test_relation_evidence_distinguishes_zero_from_unavailable_recurrence() -> None:
    registry = _relation_registry()

    zero = vocabulary_evidence.relation_evidence(
        registry, query="supply", observations=(), projection_status="current"
    )
    warming = vocabulary_evidence.relation_evidence(
        registry, query="supply", observations=(), projection_status="warming"
    )

    assert zero["evidence_availability"] == {
        "projection": {"status": "current"},
        "recurrence": {"status": "available", "observations": 0},
    }
    assert warming["evidence_availability"] == {
        "projection": {"status": "warming"},
        "recurrence": {"status": "unavailable", "reason": "projection_warming"},
    }
    assert warming["unregistered_pressure"] == []


def test_relation_evidence_continuations_are_bound_to_registry_and_query() -> None:
    registry = _relation_registry()
    first = vocabulary_evidence.relation_evidence(registry, query="supply", limit=1)
    token = first["extensions"]["continuation"]

    assert token is not None
    second = vocabulary_evidence.relation_evidence(
        registry, query="supply", limit=1, continuation=token
    )
    assert second["extensions"]["returned"] == 1
    with pytest.raises(ValueError, match="VOCABULARY_CONTINUATION_STALE"):
        vocabulary_evidence.relation_evidence(
            registry, query="support", limit=1, continuation=token
        )
    changed_registry = relation_registry.load_registry(
        proposal={
            "schema_version": 1,
            "extensions": {
                "vault.supplies": {
                    "parent": "relates_to",
                    "description": "One party supplies goods to another.",
                    "direction": "directed",
                    "aliases": ["provides_goods", "stocks"],
                },
            },
        }
    )
    with pytest.raises(ValueError, match="VOCABULARY_CONTINUATION_STALE"):
        vocabulary_evidence.relation_evidence(
            changed_registry, query="supply", limit=1, continuation=token
        )


def test_relation_observation_continuation_binds_projection_generation() -> None:
    registry = _relation_registry()
    observations = (
        {"raw_relation": "alpha", "count": 3},
        {"raw_relation": "beta", "count": 2},
        {"raw_relation": "gamma", "count": 1},
    )
    first = vocabulary_evidence.relation_evidence(
        registry,
        query="supply",
        limit=1,
        observations=observations,
        projection_generation="17",
    )
    token = first["observations"]["continuation"]

    assert first["evidence_availability"]["projection"] == {
        "status": "current",
        "generation": "17",
    }
    assert token is not None
    second = vocabulary_evidence.relation_evidence(
        registry,
        query="supply",
        limit=1,
        observations=observations,
        continuation=token,
        projection_generation="17",
    )
    assert second["unregistered_pressure"] == [{"raw_relation": "beta", "count": 2}]

    with pytest.raises(ValueError, match="VOCABULARY_CONTINUATION_STALE"):
        vocabulary_evidence.relation_evidence(
            registry,
            query="supply",
            limit=1,
            observations=(
                {"raw_relation": "new", "count": 4},
                *observations,
            ),
            continuation=token,
            projection_generation="18",
        )


def test_relation_observation_continuation_is_unavailable_without_generation() -> None:
    result = vocabulary_evidence.relation_evidence(
        _relation_registry(),
        query="supply",
        limit=1,
        observations=(
            {"raw_relation": "alpha", "count": 2},
            {"raw_relation": "beta", "count": 1},
        ),
    )

    assert result["observations"]["continuation"] is None
    assert result["evidence_availability"]["observation_continuation"] == {
        "status": "unavailable",
        "reason": "projection_generation_required",
    }


def test_relation_extension_continuation_does_not_depend_on_projection_generation() -> None:
    registry = _relation_registry()
    first = vocabulary_evidence.relation_evidence(
        registry, query="supply", limit=1, projection_generation="17"
    )
    token = first["extensions"]["continuation"]

    assert token is not None
    second = vocabulary_evidence.relation_evidence(
        registry,
        query="supply",
        limit=1,
        continuation=token,
        projection_generation="18",
    )
    assert second["extensions"]["returned"] == 1
