from __future__ import annotations

import importlib
import time
from pathlib import Path

import pytest
import yaml

from exomem.entity_types import ENTITY_TYPE_REGISTRY


def _rr():
    return importlib.import_module("exomem.referent_resolution")


def _entity(path: str = "Knowledge Base/Entities/People/aria-vale.md", **kw):
    rr = _rr()
    values = {
        "path": path,
        "title": "Aria Vale",
        "entity_type": "person",
        "status": "active",
        "aliases": (),
        "tags": (),
        "relationship": "",
        "affiliation": "",
    }
    values.update(kw)
    return rr.EntityRecord(**values)


def _hit(path: str, **kw):
    rr = _rr()
    values = {
        "path": path,
        "type": "research-note",
        "title": "Topic",
        "status": "active",
        "rank": 1,
        "bm25_rank": 1,
        "vector_rank": None,
        "keyword_rank": 1,
    }
    values.update(kw)
    return rr.HitFact(**values)


def _resolve(query: str, *, entities=None, hits=(), edges=(), anchor_cap: int = 10):
    rr = _rr()
    cue = rr.detect_cue(query)
    assert cue is not None
    return rr.resolve_referents(
        cue=cue,
        hits=tuple(hits),
        entities=tuple((_entity(),) if entities is None else entities),
        edges=tuple(edges),
        anchor_cap=anchor_cap,
    )


def test_cue_fires_on_counted_plural_person_noun() -> None:
    cue = _rr().detect_cue("my two coastal friends")
    assert cue is not None
    assert cue.entity_type == "person"
    assert cue.expected_count == 2
    assert "coastal" in cue.descriptors
    assert cue.qualifiers == ("coastal",)


@pytest.mark.parametrize(
    ("query", "qualifiers"),
    [
        ("You know my two Japanese friends — what did they say?", ("japanese",)),
        ("my two verdant friends route timing", ("verdant",)),
        ("which friend was Maribell", ()),
        ("which crystal friend", ("crystal",)),
    ],
)
def test_cue_qualifiers_are_the_pre_nominal_run(
    query: str, qualifiers: tuple[str, ...]
) -> None:
    cue = _rr().detect_cue(query)
    assert cue is not None
    assert cue.qualifiers == qualifiers


def test_cue_descriptors_and_qualifiers_are_deduplicated() -> None:
    cue = _rr().detect_cue("my verdant verdant friend verdant route route")
    assert cue is not None
    assert cue.descriptors == ("verdant", "route")
    assert cue.qualifiers == ("verdant",)


def test_cue_count_outside_window_is_ignored() -> None:
    cue = _rr().detect_cue("two stories about a distant trusted old friend")
    assert cue is not None
    assert cue.expected_count is None


def test_cue_silent_without_entity_noun() -> None:
    assert _rr().detect_cue("two coastal memories from autumn") is None


def test_cue_organization_from_registry_aliases() -> None:
    cue = _rr().detect_cue("the two robotics companies")
    assert cue is not None
    assert cue.entity_type == "organization"
    assert cue.expected_count == 2


@pytest.mark.parametrize(
    ("entity_type", "noun"),
    [(definition.id, definition.id) for definition in ENTITY_TYPE_REGISTRY],
)
def test_cue_fires_for_every_registry_type_noun(entity_type: str, noun: str) -> None:
    cue = _rr().detect_cue(f"which {noun}")
    assert cue is not None
    assert cue.entity_type == entity_type


@pytest.mark.parametrize(
    ("entity_type", "noun"),
    [
        ("organization", "firm"),
        ("concept", "principle"),
        ("library", "framework"),
        ("decision", "choice"),
    ],
)
def test_cue_fires_for_supplementary_type_nouns(entity_type: str, noun: str) -> None:
    rr = _rr()
    assert noun not in rr._COUNT_WORDS
    assert noun not in rr._STOP_WORDS
    cue = rr.detect_cue(f"which {noun}")
    assert cue is not None
    assert cue.entity_type == entity_type


def test_cue_prefers_typed_noun_over_leading_interrogative() -> None:
    cue = _rr().detect_cue("Who is the coastal friend?")
    assert cue is not None
    assert cue.noun == "friend"
    assert cue.entity_type == "person"
    assert "coastal" in cue.descriptors


def test_cue_count_survives_interrogative_prefix() -> None:
    cue = _rr().detect_cue("Who are my two coastal friends?")
    assert cue is not None
    assert cue.noun == "friends"
    assert cue.expected_count == 2


def test_cue_fires_for_vault_defined_type_noun(tmp_path: Path) -> None:
    path = tmp_path / "Knowledge Base" / "_Schema" / "entity-types.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entity_types": {
                    "place": {
                        "folder": "Places",
                        "label": "Place",
                        "aliases": ["location"],
                        "cue_nouns": ["venue"],
                        "capture_guidance": "A stable place identity.",
                        "parent": "concept",
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    from exomem import entity_types

    registry = entity_types.load_entity_types(tmp_path)

    cue = _rr().detect_cue("which local venue", registry=registry)

    assert cue is not None
    assert cue.entity_type == "place"
    assert cue.noun == "venue"


def test_vault_defined_type_restricts_candidates_to_that_type(tmp_path: Path) -> None:
    from exomem import entity_types

    registry = entity_types.load_entity_types(
        proposal={
            "schema_version": 1,
            "entity_types": {
                "place": {
                    "folder": "Places",
                    "label": "Place",
                    "aliases": ["location"],
                    "cue_nouns": ["venue"],
                    "capture_guidance": "A stable place identity.",
                    "parent": "concept",
                }
            },
        }
    )
    cue = _rr().detect_cue("which local venue", registry=registry)
    assert cue is not None
    place = _entity(
        "Knowledge Base/Entities/Places/aster-hall.md",
        title="Aster Hall",
        entity_type="place",
        tags=("local",),
    )
    person = _entity(tags=("local",), relationship="venue")

    result = _rr().resolve_referents(
        cue=cue,
        hits=(_hit(place.path, type="entity"), _hit(person.path, type="entity", rank=2)),
        entities=(person, place),
        edges=(),
    )

    assert [item.path for item in result.resolved] == [place.path]
    assert result.reasons["type_mismatch"] == 1


def test_exact_name_resolves_alone() -> None:
    out = _resolve("which person was Aria Vale")
    assert out.status == "resolved"
    assert [item.path for item in out.resolved] == [_entity().path]
    assert {e.kind for e in out.resolved[0].evidence} == {"exact_name"}


def test_fuzzy_name_needs_second_kind() -> None:
    entity = _entity(title="Maribel", relationship="friend")
    fuzzy_only = _resolve("which person was Maribell", entities=(entity,))
    assert fuzzy_only.resolved == ()
    assert {e.kind for e in fuzzy_only.candidates[0].evidence} == {"fuzzy_name"}
    corroborated = _resolve("which friend was Maribell", entities=(entity,))
    assert [item.path for item in corroborated.resolved] == [entity.path]


def test_partial_name_token_is_fuzzy_name_evidence() -> None:
    entity = _entity(title="Aster Vale", relationship="friend")
    out = _resolve("which friend was Aster", entities=(entity,))
    evidence = {item.kind: item.detail for item in out.resolved[0].evidence}
    assert evidence["fuzzy_name"] == {
        "query_token": "aster",
        "name_token": "aster",
        "distance": 0,
    }
    assert "attribute" in evidence


def test_fuzzy_name_with_qualifier_attribute_resolves() -> None:
    entity = _entity(title="Maribel", relationship="friend", tags=("coastal",))
    out = _resolve("which coastal friend was Maribell", entities=(entity,))
    assert [item.path for item in out.resolved] == [entity.path]


def test_partial_name_token_with_qualifier_attribute_resolves() -> None:
    entity = _entity(title="Aster Vale", relationship="friend", tags=("coastal",))
    out = _resolve("which coastal friend was Aster", entities=(entity,))
    evidence = {item.kind: item.detail for item in out.resolved[0].evidence}
    assert evidence["fuzzy_name"] == {
        "query_token": "aster",
        "name_token": "aster",
        "distance": 0,
    }
    assert evidence["attribute"] == {"matched": ["coastal", "friend"]}


def test_no_qualifier_attribute_fuzzy_name_and_retrieval_resolve() -> None:
    entity = _entity(title="Maribel", relationship="friend")
    out = _resolve(
        "which friend was Maribell",
        entities=(entity,),
        hits=(_hit(entity.path, type="entity"),),
    )
    assert [item.path for item in out.resolved] == [entity.path]
    assert {item.kind for item in out.resolved[0].evidence} == {
        "attribute",
        "fuzzy_name",
        "retrieval",
    }


def test_graph_edge_from_non_entity_anchor_is_graph_evidence() -> None:
    rr = _rr()
    entity = _entity(relationship="friend")
    anchor = "Knowledge Base/Notes/Research/coastal-trip.md"
    out = _resolve(
        "which coastal friend",
        entities=(entity,),
        hits=(_hit(anchor, descriptor_tokens=("coastal",)),),
        edges=(rr.EdgeFact(anchor, entity.path, "about_entity", "outbound", "entity"),),
    )
    assert {e.kind for e in out.resolved[0].evidence} == {"attribute", "graph"}


def test_counted_slot_requires_descriptor_evidence_when_cue_has_descriptors() -> None:
    rr = _rr()
    evidenced = _entity(
        "Knowledge Base/Entities/People/evidenced.md",
        title="Aster Vale",
        relationship="friend",
        tags=("japanese",),
    )
    distractor = _entity(
        "Knowledge Base/Entities/People/distractor.md",
        title="Beryl Moss",
        relationship="friend",
    )
    anchor = "Knowledge Base/Notes/Research/trip-timing.md"

    out = _resolve(
        "my two japanese friends route timing",
        entities=(evidenced, distractor),
        hits=(
            _hit(evidenced.path, type="entity"),
            _hit(anchor, rank=2, descriptor_tokens=("friends", "route", "timing")),
        ),
        edges=(
            rr.EdgeFact(anchor, distractor.path, "relates_to", "outbound", "epistemic"),
        ),
    )

    assert out.status == "partial"
    assert [item.path for item in out.resolved] == [evidenced.path]
    assert [item.path for item in out.candidates] == [distractor.path]
    assert out.unresolved_count == 1


def test_descriptor_bearing_anchor_lets_graph_evidence_resolve() -> None:
    rr = _rr()
    entity = _entity(relationship="friend")
    anchor = "Knowledge Base/Notes/Research/crystal-project.md"

    out = _resolve(
        "which crystal friend",
        entities=(entity,),
        hits=(_hit(anchor, descriptor_tokens=("crystal", "project")),),
        edges=(
            rr.EdgeFact(anchor, entity.path, "about_entity", "outbound", "entity"),
        ),
    )

    assert [item.path for item in out.resolved] == [entity.path]


def test_either_of_two_pre_nominal_qualifiers_satisfies_the_gate() -> None:
    japanese = _entity(
        "Knowledge Base/Entities/People/japanese.md",
        title="Aster Vale",
        relationship="friend",
        tags=("japanese",),
    )
    hiking = _entity(
        "Knowledge Base/Entities/People/hiking.md",
        title="Beryl Moss",
        relationship="friend",
        tags=("hiking",),
    )
    out = _resolve(
        "my two japanese hiking friends route timing details",
        entities=(japanese, hiking),
        hits=(
            _hit(japanese.path, type="entity"),
            _hit(hiking.path, type="entity", rank=2),
        ),
    )
    assert [item.path for item in out.resolved] == sorted((japanese.path, hiking.path))


def test_descriptorless_cue_keeps_existing_two_kind_resolution() -> None:
    rr = _rr()
    entity = _entity(relationship="friend")
    anchor = "Knowledge Base/Notes/Research/shared-project.md"

    out = _resolve(
        "which friend",
        entities=(entity,),
        hits=(_hit(anchor),),
        edges=(
            rr.EdgeFact(anchor, entity.path, "about_entity", "outbound", "entity"),
        ),
    ).as_dict()

    assert out == {
        "status": "resolved",
        "entity_type": "person",
        "resolved": [
            {
                "path": entity.path,
                "title": entity.title,
                "entity_type": "person",
                "evidence": [
                    {"kind": "attribute", "matched": ["friend"]},
                    {
                        "kind": "graph",
                        "seed": anchor,
                        "relation_type": "about_entity",
                        "direction": "outbound",
                        "tier": 0,
                    },
                ],
            }
        ],
        "candidates": [],
        "reasons": {"inactive": 0, "type_mismatch": 0},
    }


def test_exact_name_resolution_ignores_the_descriptor_gate() -> None:
    entity = _entity()
    out = _resolve("which japanese friend was Aria Vale", entities=(entity,))

    assert [item.path for item in out.resolved] == [entity.path]
    assert {item.kind for item in out.resolved[0].evidence} == {"exact_name"}


def test_exact_name_keeps_pre_change_first_graph_edge() -> None:
    rr = _rr()
    entity = _entity(relationship="friend")
    first = "Knowledge Base/Notes/Research/a-topic.md"
    qualifier_bearing = "Knowledge Base/Notes/Research/z-crystal.md"
    out = _resolve(
        "which crystal friend was Aria Vale",
        entities=(entity,),
        hits=(
            _hit(first, descriptor_tokens=("topic",)),
            _hit(qualifier_bearing, rank=2, descriptor_tokens=("crystal",)),
        ),
        edges=(
            rr.EdgeFact(first, entity.path, "relates_to", "outbound", "epistemic"),
            rr.EdgeFact(
                qualifier_bearing,
                entity.path,
                "relates_to",
                "outbound",
                "epistemic",
            ),
        ),
    )
    graph = next(item for item in out.resolved[0].evidence if item.kind == "graph")
    assert graph.detail["seed"] == first


def test_fuzzy_name_is_not_descriptor_bearing() -> None:
    entity = _entity(title="Maribel", relationship="friend")
    out = _resolve("which japanese friend was Maribell", entities=(entity,))

    assert out.resolved == ()
    assert [item.path for item in out.candidates] == [entity.path]
    assert {item.kind for item in out.candidates[0].evidence} == {
        "attribute",
        "fuzzy_name",
    }


def test_links_to_edge_is_tier_one_graph_evidence() -> None:
    rr = _rr()
    entity = _entity(tags=("coastal",))
    anchor = "Knowledge Base/Notes/Research/harbour.md"
    out = _resolve(
        "which coastal friend",
        entities=(entity,),
        hits=(_hit(anchor),),
        edges=(rr.EdgeFact(anchor, entity.path, "links_to", "outbound", "link"),),
    )
    graph = next(e for e in out.resolved[0].evidence if e.kind == "graph")
    assert graph.detail["tier"] == 1


def test_superseded_anchor_does_not_corroborate() -> None:
    rr = _rr()
    entity = _entity(tags=("coastal",))
    anchor = "Knowledge Base/Notes/Research/old-trip.md"
    out = _resolve(
        "which coastal friend",
        entities=(entity,),
        hits=(_hit(anchor, status="superseded"),),
        edges=(rr.EdgeFact(anchor, entity.path, "about_entity", "outbound", "entity"),),
    )
    assert out.resolved == ()


def test_anchor_beyond_cap_does_not_corroborate() -> None:
    rr = _rr()
    entity = _entity(tags=("coastal",))
    hits = tuple(_hit(f"Knowledge Base/Notes/Research/a-{i}.md", rank=i + 1) for i in range(11))
    out = _resolve(
        "which coastal friend",
        entities=(entity,),
        hits=hits,
        edges=(rr.EdgeFact(hits[-1].path, entity.path, "about_entity", "outbound", "entity"),),
        anchor_cap=10,
    )
    assert out.resolved == ()


def test_qualifier_anchor_scan_is_hoisted_for_large_graph_fanout() -> None:
    rr = _rr()
    anchor = "Knowledge Base/Notes/Research/large-topic.md"
    entities = tuple(
        _entity(
            f"Knowledge Base/Entities/People/fanout-{index:03d}.md",
            title=f"Fanout Person {index:03d}",
            relationship="friend",
        )
        for index in range(500)
    )
    edges = tuple(
        rr.EdgeFact(anchor, entity.path, "relates_to", "outbound", "epistemic")
        for entity in entities
    )
    started = time.perf_counter()
    out = _resolve(
        "my two verdant friends route timing details",
        entities=entities,
        hits=(_hit(anchor, descriptor_tokens=tuple(f"topic{index}" for index in range(1000))),),
        edges=edges,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert out.resolved == ()
    assert elapsed_ms < 250


def test_attribute_overlap_matches_stem_or_prefix() -> None:
    stemmed = _entity(tags=("robotics",), relationship="colleague")
    out = _resolve("which robotic colleague", entities=(stemmed,))
    evidence = next(e for e in out.candidates[0].evidence if e.kind == "attribute")
    assert set(evidence.detail["matched"]) == {"colleague", "robotic"}


def test_attribute_evidence_reads_type_specific_frontmatter(tmp_path) -> None:
    rel = "Knowledge Base/Entities/Libraries/aster-render.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "type: entity\n"
        "title: Aster Render\n"
        "entity_type: library\n"
        "status: active\n"
        "language: Rust\n"
        "---\n\n"
        "# Aster Render\n\nA synthetic rendering dependency.\n",
        encoding="utf-8",
    )
    registry = importlib.import_module("exomem.entity_registry")
    registry.clear_entity_registry_cache()
    entity = registry.load_entity_registry(
        tmp_path, freshness_key=("type-specific-attribute",)
    )[rel]
    out = _resolve(
        "which rust library",
        entities=(entity,),
        hits=(_hit(entity.path, type="entity"),),
    )
    evidence = {item.kind: item.detail for item in out.resolved[0].evidence}
    assert evidence["attribute"] == {"matched": ["rust"]}


def test_attribute_evidence_ignores_url_shaped_frontmatter(tmp_path) -> None:
    rel = "Knowledge Base/Entities/Libraries/aster-parse.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "type: entity\n"
        "title: Aster Parse\n"
        "entity_type: library\n"
        "status: active\n"
        "language: Rust\n"
        "repo: https://github.com/synthetic/aster-parse\n"
        "---\n\n"
        "# Aster Parse\n\nA synthetic parsing dependency.\n",
        encoding="utf-8",
    )
    registry = importlib.import_module("exomem.entity_registry")
    registry.clear_entity_registry_cache()
    entity = registry.load_entity_registry(tmp_path, freshness_key=("url-attribute",))[rel]
    assert entity.attributes == ("Rust",)
    out = _resolve(
        "which github library",
        entities=(entity,),
        hits=(_hit(entity.path, type="entity"),),
    )
    assert out.resolved == ()
    assert [item.path for item in out.candidates] == [entity.path]
    assert {item.kind for item in out.candidates[0].evidence} == {"retrieval"}


def test_retrieval_presence_alone_is_candidate_not_resolved() -> None:
    entity = _entity()
    out = _resolve("which person", entities=(entity,), hits=(_hit(entity.path, type="entity"),))
    assert out.resolved == ()
    assert [item.path for item in out.candidates] == [entity.path]


def test_type_mismatch_without_exact_name_is_dropped() -> None:
    entity = _entity(entity_type="organization", tags=("robotics",))
    out = _resolve("which robotics friend", entities=(entity,), hits=(_hit(entity.path),))
    assert out.resolved == () and out.candidates == ()
    assert out.reasons["type_mismatch"] == 1


def test_inactive_entity_never_resolves_and_is_counted_in_reasons() -> None:
    entity = _entity(status="superseded")
    out = _resolve("which person was Aria Vale", entities=(entity,))
    assert out.resolved == () and out.candidates == ()
    assert out.reasons["inactive"] == 1


def test_partial_reports_unresolved_count() -> None:
    entity = _entity(tags=("coastal",), relationship="friend")
    out = _resolve(
        "my two coastal friends",
        entities=(entity,),
        hits=(_hit(entity.path, type="entity"),),
    )
    assert out.status == "partial"
    assert out.unresolved_count == 1


def test_over_count_is_ambiguous_and_lists_all() -> None:
    entities = tuple(
        _entity(
            f"Knowledge Base/Entities/People/p-{i}.md",
            title=f"Person {i}",
            tags=("coastal",),
            relationship="friend",
        )
        for i in range(3)
    )
    hits = tuple(_hit(entity.path, type="entity", rank=i + 1) for i, entity in enumerate(entities))
    out = _resolve("my two coastal friends", entities=entities, hits=hits)
    assert out.status == "ambiguous"
    assert [item.path for item in out.resolved] == sorted(item.path for item in entities)


def test_no_count_zero_resolved_is_unresolved() -> None:
    out = _resolve("which person", entities=())
    assert out.status == "unresolved"


def test_candidates_are_capped_deterministically_with_omitted_count() -> None:
    rr = _rr()
    entities = tuple(
        _entity(
            f"Knowledge Base/Entities/People/candidate-{index:02d}.md",
            title=f"Candidate {index:02d}",
            relationship="friend",
        )
        for index in reversed(range(rr.REFERENT_CANDIDATE_CAP + 7))
    )
    block = _resolve("my two friends", entities=entities).as_dict()
    assert [item["path"] for item in block["candidates"]] == sorted(
        entity.path for entity in entities
    )[: rr.REFERENT_CANDIDATE_CAP]
    assert block["omitted_candidate_count"] == 7


def test_block_contains_no_floats() -> None:
    entity = _entity(tags=("coastal",), relationship="friend")
    block = _resolve("my two coastal friends", entities=(entity,)).as_dict()

    def walk(value):
        if isinstance(value, dict):
            return all(walk(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return all(walk(v) for v in value)
        return not isinstance(value, float)

    assert walk(block)


def test_resolution_is_permutation_invariant() -> None:
    rr = _rr()
    a = _entity(
        "Knowledge Base/Entities/People/a.md",
        title="Aster",
        tags=("coastal",),
        relationship="friend",
    )
    b = _entity(
        "Knowledge Base/Entities/People/b.md",
        title="Beryl",
        tags=("coastal",),
        relationship="friend",
    )
    h1 = _hit(a.path, type="entity", rank=2)
    h2 = _hit(b.path, type="entity", rank=1)
    cue = rr.detect_cue("my two coastal friends")
    first = rr.resolve_referents(cue=cue, hits=(h1, h2), entities=(a, b), edges=()).as_dict()
    second = rr.resolve_referents(cue=cue, hits=(h2, h1), entities=(b, a), edges=()).as_dict()
    assert first == second
