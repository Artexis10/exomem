"""Focused contracts for recurring-identity lifecycle and entity families."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from exomem import (
    attention,
    audit,
    embeddings,
    entity_candidates,
    entity_recurrence,
    entity_types,
    epistemic_graph,
    find,
    referent_resolution,
    runtime_resources,
    vault,
)


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    find.clear_cache()
    return path


def _note(
    root: Path,
    index: int,
    body: str,
    *,
    source: str | None = None,
) -> Path:
    sources = f"sources: ['[[{source}]]']\n" if source else ""
    return _write(
        root,
        f"Knowledge Base/Notes/note-{index:02d}.md",
        "---\n"
        "type: insight\n"
        f"title: Note {index}\n"
        "status: active\n"
        f"{sources}"
        "---\n"
        f"# Note {index}\n\n{body}\n",
    )


def _entity(
    root: Path,
    filename: str,
    *,
    title: str,
    aliases: tuple[str, ...] = (),
    entity_type: str = "organization",
    folder: str = "Organizations",
) -> Path:
    alias_line = f"aliases: {list(aliases)!r}\n" if aliases else ""
    return _write(
        root,
        f"Knowledge Base/Entities/{folder}/{filename}.md",
        "---\n"
        "type: entity\n"
        f"title: {title}\n"
        f"entity_type: {entity_type}\n"
        "status: active\n"
        f"{alias_line}"
        "---\n"
        f"# {title}\n",
    )


def _findings(root: Path) -> list:
    report = audit.audit(root, categories=["entity_recurrence"])
    return [item for item in report.findings if item.category == "entity_recurrence"]


def test_reusable_plain_text_facets_surface_while_frequency_twin_stays_quiet(
    tmp_path: Path,
) -> None:
    positive = (
        "juniper circle is an organization.",
        "organization: juniper circle.",
        "Membership: juniper circle.",
    )
    incidental = (
        "We discussed river passage again.",
        "The notes mention river passage again.",
        "A summary repeated river passage again.",
    )
    for index, (signal, twin) in enumerate(zip(positive, incidental, strict=True)):
        _note(tmp_path, index, f"{signal}\n\n{twin}")

    findings = _findings(tmp_path)

    assert len(findings) == 1
    item = findings[0]
    assert item.meta["candidate"] == "juniper circle"
    assert item.meta["candidate_state"] == "promotion"
    assert item.meta["grammar_version"] == "identity-frames-v1"
    assert item.meta["page_count"] == 3
    assert item.meta["origin_count"] == 3
    assert item.meta["facet_count"] == 3
    assert "river passage" not in repr(item.meta).casefold()


def test_origin_and_facet_gates_are_independent(tmp_path: Path) -> None:
    for index, body in enumerate(
        (
            "cobalt workshop is an organization.",
            "organization: cobalt workshop.",
            "Membership: cobalt workshop.",
        )
    ):
        _note(tmp_path, index, body, source="Sources/shared-record")

    assert _findings(tmp_path) == []

    other = tmp_path / "independent"
    for index in range(3):
        _note(other, index, "amber workshop is an organization.")

    assert _findings(other) == []


def test_overlapping_source_sets_are_one_derivative_origin(tmp_path: Path) -> None:
    for index, (unique_source, body) in enumerate(
        (
            ("Sources/alpha", "cobalt workshop is an organization."),
            ("Sources/beta", "organization: cobalt workshop."),
            ("Sources/gamma", "Membership: cobalt workshop."),
        )
    ):
        _write(
            tmp_path,
            f"Knowledge Base/Notes/derived-{index}.md",
            "---\n"
            "type: insight\n"
            f"title: Derived {index}\n"
            "status: active\n"
            f"sources: ['[[Sources/shared]]', '[[{unique_source}]]']\n"
            "---\n\n"
            f"{body}\n",
        )

    assert _findings(tmp_path) == []


def test_unrelated_source_bridge_does_not_collapse_identity_origins(
    tmp_path: Path,
) -> None:
    for index, (source, body) in enumerate(
        (
            ("Sources/alpha", "amber guild is an organization."),
            ("Sources/beta", "organization: amber guild."),
            ("Sources/gamma", "Membership: amber guild."),
        )
    ):
        _note(tmp_path, index, body, source=source)
    _write(
        tmp_path,
        "Knowledge Base/Notes/unrelated-bridge.md",
        "---\n"
        "type: insight\n"
        "title: Unrelated Bridge\n"
        "status: active\n"
        "sources: ['[[Sources/alpha]]', '[[Sources/beta]]']\n"
        "---\n\n"
        "This page contributes no amber guild identity frame.\n",
    )

    findings = _findings(tmp_path)

    assert len(findings) == 1
    assert findings[0].meta["candidate"] == "amber guild"
    assert findings[0].meta["origin_count"] == 3


def test_frozen_predicate_table_carries_every_exact_id_and_digest() -> None:
    table = getattr(entity_recurrence, "PREDICATE_TABLE", None)
    digest = getattr(entity_recurrence, "PREDICATE_TABLE_DIGEST", None)

    assert table is not None
    assert digest is not None and len(digest) == 64
    assert table["typed-copula"]["was"] == "copula.was"
    assert table["typed-label"]["—"] == "label.em_dash"
    assert table["relation"]["belongs to"] == "membership.belongs_to"
    assert table["relation"]["organizes"] == "stewardship.organizes"
    assert table["body-field"]["affiliation"] == "field.membership"
    assert table["body-field"]["organizes"] == "field.organises"
    assert all(
        predicate_id
        for values in table.values()
        for predicate_id in values.values()
    )


def test_unlisted_frame_predicate_and_field_cannot_grow_v1_implicitly() -> None:
    rows = entity_recurrence.extract_identity_frames(
        "I collaborates with amber guild.\nTeam: amber guild.",
        path="Knowledge Base/Notes/unlisted.md",
        origin="page:unlisted",
        entity_types=entity_types.core_registry(),
        registry=entity_recurrence.RegistryIndex(entries=(), identities=frozenset()),
    )

    assert rows == ()


def test_lowercase_non_latin_and_all_five_frames_are_collected(tmp_path: Path) -> None:
    _entity(tmp_path, "anchor-lab", title="Anchor Lab")
    rows = (
        "северный круг is an organization.",
        "organization — северный круг.",
        "I work with северный круг.",
        "северный круг works with Anchor Lab.",
        "Membership: северный круг.",
    )
    for index, body in enumerate(rows):
        _note(tmp_path, index, body)

    findings = _findings(tmp_path)

    assert len(findings) == 1
    item = findings[0]
    assert item.meta["candidate"] == "северный круг"
    assert {facet["frame_type"] for facet in item.meta["material_facets"]} == {
        "body-field",
        "identity-relation",
        "subject-relation",
        "typed-copula",
        "typed-label",
    }
    assert all(facet["predicate_id"] for facet in item.meta["material_facets"])


def test_lexical_matching_uses_nfkc_casefold_for_length_changing_cues() -> None:
    street = _definition("StreetKinds", "Street Kind", parent="concept")
    street["cue_nouns"] = ["straße"]
    idea = _definition("IdeaKinds", "Idea Kind", parent="concept")
    idea["cue_nouns"] = ["İdea"]
    registry = entity_types.load_entity_types(
        proposal={
            "schema_version": 1,
            "entity_types": {"street-kind": street, "idea-kind": idea},
        }
    )

    rows = entity_recurrence.extract_identity_frames(
        "amber guild is a Straße.\ncobalt circle is an İdea.",
        path="Knowledge Base/Notes/unicode-cues.md",
        origin="page:unicode-cues",
        entity_types=registry,
        registry=entity_recurrence.RegistryIndex(entries=(), identities=frozenset()),
    )

    assert [(row.identity, row.cue) for row in rows] == [
        ("amber guild", "strasse"),
        ("cobalt circle", "i̇dea"),
    ]


def test_span_rejection_classes_and_maximal_span_do_not_create_candidates(
    tmp_path: Path,
) -> None:
    rejected = (
        "I work with 12345.",
        "I work with https://example.invalid.",
        "I work with person@example.invalid.",
        "I work with /srv/private/file.",
        "I work with 2026-08-31.",
        "I work with September 1 2026.",
        "I work with 8 pm.",
        "I work with `inline name`.",
        "I work with this.",
        "organization: organization.",
        "the organization is an organization.",
        "I work with one two three four five six seven eight nine.",
        "I work with amber consortium beside the station.",
    )
    for offset, text in enumerate(rejected):
        for copy in range(3):
            _note(tmp_path, offset * 3 + copy, text)

    assert _findings(tmp_path) == []


@pytest.mark.parametrize(
    "candidate",
    [
        "the organization",
        "September 1 2026",
        "1 September 2026",
        "8 pm",
        "8am",
        "8 o'clock pm",
        "1st of September 2026",
        "[[amber guild]]",
    ],
)
def test_article_cue_natural_datetime_and_unaliased_link_targets_are_rejected(
    candidate: str,
) -> None:
    rows = entity_recurrence.extract_identity_frames(
        f"I work with {candidate}.",
        path="Knowledge Base/Notes/rejected-natural.md",
        origin="page:rejected-natural",
        entity_types=entity_types.core_registry(),
        registry=entity_recurrence.RegistryIndex(entries=(), identities=frozenset()),
    )

    assert rows == ()


def test_determiner_cue_and_oclock_time_reject_without_hiding_real_identity() -> None:
    rows = entity_recurrence.extract_identity_frames(
        "I work with this organization.\n"
        "I work with 8 o'clock.\n"
        "I work with amber guild.",
        path="Knowledge Base/Notes/rejected-categorical.md",
        origin="page:rejected-categorical",
        entity_types=entity_types.core_registry(),
        registry=entity_recurrence.RegistryIndex(entries=(), identities=frozenset()),
    )

    assert [row.identity for row in rows] == ["amber guild"]


@pytest.mark.parametrize(
    ("rejected", "stable_identity"),
    [
        ("some organization", "somewhere organization"),
        ("another organization", "another organization guild"),
        ("quarter past eight", "quarter past eight studio"),
        ("08h30", "08h30 studio"),
        ("next Monday", "next monday club"),
        ("September first 2026", "september first guild"),
    ],
)
def test_structural_generic_and_datetime_rejection_preserves_stable_identities(
    rejected: str,
    stable_identity: str,
) -> None:
    rows = entity_recurrence.extract_identity_frames(
        f"I work with {rejected}.\nI work with {stable_identity}.",
        path="Knowledge Base/Notes/structural-rejection.md",
        origin="page:structural-rejection",
        entity_types=entity_types.core_registry(),
        registry=entity_recurrence.RegistryIndex(entries=(), identities=frozenset()),
    )

    assert [row.identity for row in rows] == [stable_identity]


def test_empty_wikilink_display_masks_with_separator_and_preserves_legacy_target(
    tmp_path: Path,
) -> None:
    rows = entity_recurrence.extract_identity_frames(
        "I work with amber[[Hidden|]]guild.\n"
        "I work with [[Visible Target|amber guild]].",
        path="Knowledge Base/Notes/empty-display.md",
        origin="page:empty-display",
        entity_types=entity_types.core_registry(),
        registry=entity_recurrence.RegistryIndex(entries=(), identities=frozenset()),
    )

    assert [row.identity for row in rows] == ["amber guild"]

    for index in range(3):
        _note(tmp_path, index, "See [[Hidden|]].")
    findings = _findings(tmp_path)
    assert len(findings) == 1
    assert findings[0].meta["candidate"] == "Hidden"
    assert findings[0].meta["reasons"] == ["unresolved_identity_recurs"]


def test_hydration_batches_bind_full_disconnected_set_and_close_from_links(
    tmp_path: Path,
) -> None:
    _entity(tmp_path, "juniper", title="Juniper Circle")
    for index in range(10):
        body = (
            "juniper circle is an organization."
            if index % 2 == 0
            else "Membership: juniper circle."
        )
        _note(tmp_path, index, body)

    first = _findings(tmp_path)

    assert len(first) == 1
    meta = first[0].meta
    assert meta["candidate_state"] == "hydration"
    assert meta["resolved_entity"]["title"] == "Juniper Circle"
    assert len(meta["disconnected_contexts"]) == 8
    assert meta["disconnected_context_count"] == 10
    assert meta["remaining_disconnected_count"] == 2
    assert len(meta["batch_fingerprint"]) == 64
    assert len(meta["signal_version"]) == 64

    first_signal = meta["signal_version"]
    first_batch = [row["path"] for row in meta["disconnected_contexts"]]
    for rel in first_batch:
        path = tmp_path / rel
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "juniper circle", "[[Juniper Circle|juniper circle]]"
            ),
            encoding="utf-8",
        )
    find.clear_cache()

    second = _findings(tmp_path)
    assert len(second) == 1
    second_meta = second[0].meta
    assert second_meta["disconnected_context_count"] == 2
    assert second_meta["remaining_disconnected_count"] == 0
    # Only redundant copies of the same two material contexts remain, so the
    # lifecycle signal is stable while the actionable path batch advances.
    assert second_meta["signal_version"] == first_signal
    assert second_meta["batch_fingerprint"] != meta["batch_fingerprint"]
    assert not set(first_batch) & {
        row["path"] for row in second_meta["disconnected_contexts"]
    }

    for rel in [row["path"] for row in second_meta["disconnected_contexts"]]:
        path = tmp_path / rel
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "juniper circle", "[[Juniper Circle|juniper circle]]"
            ),
            encoding="utf-8",
        )
    find.clear_cache()
    assert _findings(tmp_path) == []


def test_hydration_signal_binds_a_context_outside_the_returned_batch(
    tmp_path: Path,
) -> None:
    _entity(tmp_path, "juniper", title="Juniper Circle")
    bodies = (
        "juniper circle is an organization.",
        "juniper circle was an organization.",
        "organization: juniper circle.",
        "organization — juniper circle.",
        "I work with juniper circle.",
        "I use juniper circle.",
        "I attend juniper circle.",
        "I maintain juniper circle.",
        "I build juniper circle.",
        "Membership: juniper circle.",
    )
    for index, body in enumerate(bodies):
        _note(tmp_path, index, body)
    first = _findings(tmp_path)[0].meta
    first_paths = [row["path"] for row in first["disconnected_contexts"]]
    outside = tmp_path / "Knowledge Base/Notes/note-09.md"
    outside.write_text(
        outside.read_text(encoding="utf-8").replace(
            "juniper circle", "[[Juniper Circle|juniper circle]]"
        ),
        encoding="utf-8",
    )
    find.clear_cache()

    second = _findings(tmp_path)[0].meta

    assert [row["path"] for row in second["disconnected_contexts"]] == first_paths
    assert second["signal_version"] != first["signal_version"]
    assert second["batch_fingerprint"] != first["batch_fingerprint"]


def test_unrelated_page_link_does_not_connect_qualifying_hydration_contexts(
    tmp_path: Path,
) -> None:
    _entity(tmp_path, "juniper", title="Juniper Circle")
    for index, body in enumerate(
        (
            "juniper circle is an organization.",
            "organization: juniper circle.",
            "juniper circle was an organization.",
        )
    ):
        _note(tmp_path, index, f"{body} See [[Juniper Circle]].")

    findings = _findings(tmp_path)

    assert len(findings) == 1
    assert findings[0].meta["candidate_state"] == "hydration"
    assert findings[0].meta["disconnected_context_count"] == 3


def test_separate_accepted_relations_close_exact_hydration_paths(
    tmp_path: Path,
) -> None:
    _entity(tmp_path, "juniper", title="Juniper Circle")
    for index, body in enumerate(
        (
            "juniper circle is an organization.",
            "organization: juniper circle.",
            "Membership: juniper circle.",
        )
    ):
        _note(
            tmp_path,
            index,
            f"{body}\n\n## Relations\n\n- relates_to [[Juniper Circle]]",
        )

    assert _findings(tmp_path) == []


def test_cross_page_copy_of_existing_facet_does_not_change_signal_version(
    tmp_path: Path,
) -> None:
    bodies = (
        "amber guild is an organization.",
        "organization: amber guild.",
        "Membership: amber guild.",
    )
    for index, body in enumerate(bodies):
        _note(tmp_path, index, body)
    first = _findings(tmp_path)[0].meta

    _note(tmp_path, 3, bodies[0])
    second = _findings(tmp_path)[0].meta

    assert second["page_count"] == 4
    assert second["facet_count"] == first["facet_count"] == 3
    assert second["signal_version"] == first["signal_version"]


def test_multiple_alias_matches_are_ambiguous_and_select_no_target(tmp_path: Path) -> None:
    _entity(tmp_path, "one", title="First Circle", aliases=("shared circle",))
    _entity(tmp_path, "two", title="Second Circle", aliases=("shared circle",))
    for index, body in enumerate(
        (
            "shared circle is an organization.",
            "organization: shared circle.",
            "Membership: shared circle.",
        )
    ):
        _note(tmp_path, index, body)

    findings = _findings(tmp_path)

    assert len(findings) == 1
    meta = findings[0].meta
    assert meta["candidate_state"] == "ambiguous"
    assert meta["resolved_entity"] is None
    assert [item["title"] for item in meta["resolution_candidates"]] == [
        "First Circle",
        "Second Circle",
    ]


def _definition(
    folder: str,
    label: str,
    *,
    parent: str | None,
    status: str = "active",
) -> dict[str, object]:
    item: dict[str, object] = {
        "folder": folder,
        "label": label,
        "aliases": [],
        "cue_nouns": [label.casefold()],
        "capture_guidance": f"A stable synthetic {label.casefold()} identity.",
        "status": status,
    }
    if parent is not None:
        item["parent"] = parent
    return item


def test_registry_derives_core_parented_and_parentless_families_deterministically(
    tmp_path: Path,
) -> None:
    proposal = {
        "schema_version": 1,
        "entity_types": {
            "community": _definition(
                "Communities", "Community", parent="organization"
            ),
            "venue": _definition("Venues", "Venue", parent="concept"),
            "initiative": _definition("Initiatives", "Initiative", parent=None),
            "retired-kind": _definition(
                "Retired", "Retired", parent="concept", status="deprecated"
            ),
        },
    }
    registry_path = tmp_path / "Knowledge Base/_Schema/entity-types.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(yaml.safe_dump(proposal, sort_keys=False), encoding="utf-8")

    registry = entity_types.load_entity_types(tmp_path)

    assert registry.family_of("organization") == "organization"
    assert registry.family_of("community") == "organization"
    assert registry.family_of("venue") == "concept"
    assert registry.family_of("initiative") == "initiative"
    assert registry.family_members("organization") == ("community", "organization")
    assert registry.family_members("concept") == ("concept", "venue")
    assert "retired-kind" not in registry.family_members("concept")
    assert registry.matches_family("community", "organization") is True
    assert registry.matches_family("community", "concept") is False
    assert registry.extensions["community"].folder == "Communities"


def test_registry_parent_change_invalidates_family_projection_cache(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "Knowledge Base/_Schema/entity-types.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    first_proposal = {
        "schema_version": 1,
        "entity_types": {
            "community": _definition(
                "Communities", "Community", parent="organization"
            )
        },
    }
    registry_path.write_text(
        yaml.safe_dump(first_proposal, sort_keys=False), encoding="utf-8"
    )
    first = entity_types.load_entity_types(tmp_path)
    changed = {
        **first_proposal,
        "entity_types": {
            "community": _definition("Communities", "Community", parent="concept")
        },
    }
    registry_path.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")

    second = entity_types.load_entity_types(tmp_path)

    assert second is not first
    assert first.family_of("community") == "organization"
    assert second.family_of("community") == "concept"
    assert second.fingerprint != first.fingerprint


def test_exact_entity_candidate_family_filter_admits_child_but_leaf_stays_exact(
    tmp_path: Path,
) -> None:
    proposal = {
        "schema_version": 1,
        "entity_types": {
            "community": _definition(
                "Communities", "Community", parent="organization"
            )
        },
    }
    registry_path = tmp_path / "Knowledge Base/_Schema/entity-types.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(yaml.safe_dump(proposal, sort_keys=False), encoding="utf-8")
    _entity(
        tmp_path,
        "juniper",
        title="Juniper Circle",
        entity_type="community",
        folder="Communities",
    )

    family = entity_candidates.resolve_entity_candidate(
        tmp_path,
        name="Juniper Circle",
        entity_family="organization",
    )
    exact_parent = entity_candidates.resolve_entity_candidate(
        tmp_path,
        name="Juniper Circle",
        entity_type="organization",
    )

    assert family["status"] == "match"
    assert family["candidates"][0]["entity_type"] == "community"
    assert family["candidates"][0]["entity_family"] == "organization"
    assert exact_parent["status"] == "no_match"


def test_referent_family_cue_admits_child_without_weakening_identity_ambiguity(
    tmp_path: Path,
) -> None:
    proposal = {
        "schema_version": 1,
        "entity_types": {
            "community": _definition(
                "Communities", "Community", parent="organization"
            )
        },
    }
    registry = entity_types.load_entity_types(proposal=proposal)
    cue = referent_resolution.ReferentCue(
        entity_type="organization",
        noun="organization",
        expected_count=None,
        descriptors=("synthetic",),
        qualifiers=(),
        query="the synthetic organization",
    )
    entity = referent_resolution.EntityRecord(
        path="Knowledge Base/Entities/Communities/juniper.md",
        title="Juniper Circle",
        entity_type="community",
        entity_family="organization",
        status="active",
        tags=("synthetic",),
    )
    hit = referent_resolution.HitFact(
        path=entity.path,
        type="entity",
        title=entity.title,
        status="active",
        rank=1,
        bm25_rank=1,
    )

    result = referent_resolution.resolve_referents(
        cue=cue,
        hits=[hit],
        entities=[entity],
        edges=[],
        registry=registry,
    )

    assert result.status == "resolved"
    assert result.resolved[0].entity_type == "community"
    assert result.resolved[0].entity_family == "organization"
    assert result.as_dict()["resolved"][0]["entity_family"] == "organization"


def test_multiple_exact_family_aware_referents_are_ambiguous() -> None:
    proposal = {
        "schema_version": 1,
        "entity_types": {
            "community": _definition(
                "Communities", "Community", parent="organization"
            )
        },
    }
    registry = entity_types.load_entity_types(proposal=proposal)
    cue = referent_resolution.ReferentCue(
        entity_type="organization",
        noun="organization",
        expected_count=None,
        descriptors=(),
        qualifiers=(),
        query="shared circle organization",
    )
    entities = [
        referent_resolution.EntityRecord(
            path=f"Knowledge Base/Entities/Communities/{name}.md",
            title=title,
            entity_type="community",
            entity_family="organization",
            status="active",
            aliases=("shared circle",),
        )
        for name, title in (("one", "First Circle"), ("two", "Second Circle"))
    ]

    result = referent_resolution.resolve_referents(
        cue=cue,
        hits=[],
        entities=entities,
        edges=[],
        registry=registry,
    )

    assert result.status == "ambiguous"
    assert [item.title for item in result.resolved] == ["First Circle", "Second Circle"]


def test_graph_traversal_uses_explicit_entity_families_and_reports_leaf_metadata(
    tmp_path: Path,
) -> None:
    proposal = {
        "schema_version": 1,
        "entity_types": {
            "community": _definition(
                "Communities", "Community", parent="organization"
            )
        },
    }
    registry_path = tmp_path / "Knowledge Base/_Schema/entity-types.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(yaml.safe_dump(proposal, sort_keys=False), encoding="utf-8")
    community = _entity(
        tmp_path,
        "juniper",
        title="Juniper Circle",
        entity_type="community",
        folder="Communities",
    )
    organization = _entity(tmp_path, "anchor", title="Anchor Lab")
    concept = _entity(
        tmp_path,
        "pattern",
        title="Pattern One",
        entity_type="concept",
        folder="Concepts",
    )
    seed = _note(
        tmp_path,
        0,
        "See [[Juniper Circle]], [[Anchor Lab]], and [[Pattern One]].",
    )
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()

    family = epistemic_graph.graph_context(
        tmp_path,
        path=seed.relative_to(tmp_path).as_posix(),
        depth=1,
        entity_type_families=["organization"],
    )
    generic = epistemic_graph.graph_context(
        tmp_path,
        path=seed.relative_to(tmp_path).as_posix(),
        depth=1,
        node_types=["file"],
    )

    family_nodes = {node["path"]: node for node in family["nodes"]}
    assert community.relative_to(tmp_path).as_posix() in family_nodes
    assert organization.relative_to(tmp_path).as_posix() in family_nodes
    assert concept.relative_to(tmp_path).as_posix() not in family_nodes
    assert family_nodes[community.relative_to(tmp_path).as_posix()]["metadata"] == {
        **family_nodes[community.relative_to(tmp_path).as_posix()]["metadata"],
        "entity_type": "community",
        "entity_family": "organization",
    }
    assert concept.relative_to(tmp_path).as_posix() in {
        node["path"] for node in generic["nodes"]
    }


def test_graph_entity_family_metadata_refreshes_after_registry_parent_change(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "Knowledge Base/_Schema/entity-types.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    first = {
        "schema_version": 1,
        "entity_types": {
            "community": _definition(
                "Communities", "Community", parent="organization"
            )
        },
    }
    registry_path.write_text(yaml.safe_dump(first, sort_keys=False), encoding="utf-8")
    community = _entity(
        tmp_path,
        "juniper",
        title="Juniper Circle",
        entity_type="community",
        folder="Communities",
    )
    index = epistemic_graph.EpistemicGraphIndex(tmp_path)
    index.rebuild_all()

    before = next(
        node for node in index.nodes() if node["path"] == community.relative_to(tmp_path).as_posix()
    )
    assert before["metadata"]["entity_family"] == "organization"

    changed = {
        "schema_version": 1,
        "entity_types": {
            "community": _definition("Communities", "Community", parent="concept")
        },
    }
    registry_path.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")
    after = next(
        node for node in index.nodes() if node["path"] == community.relative_to(tmp_path).as_posix()
    )

    assert after["metadata"]["entity_type"] == "community"
    assert after["metadata"]["entity_family"] == "concept"


@pytest.mark.parametrize(
    ("frame", "token", "expected"),
    [
        ("typed-copula", "is", "copula.is"),
        ("typed-copula", "was", "copula.was"),
        ("typed-copula", "are", "copula.are"),
        ("typed-copula", "were", "copula.were"),
        ("typed-label", ":", "label.colon"),
        ("typed-label", "—", "label.em_dash"),
        ("subject-relation", "member of", "membership.member_of"),
        ("subject-relation", "members of", "membership.members_of"),
        ("subject-relation", "joined", "membership.joined"),
        ("subject-relation", "belongs to", "membership.belongs_to"),
        ("subject-relation", "belong to", "membership.belong_to"),
        ("subject-relation", "works at", "work.works_at"),
        ("subject-relation", "work at", "work.work_at"),
        ("subject-relation", "works with", "work.works_with"),
        ("subject-relation", "work with", "work.work_with"),
        ("subject-relation", "uses", "use.uses"),
        ("subject-relation", "use", "use.use"),
        ("subject-relation", "attends", "attendance.attends"),
        ("subject-relation", "attend", "attendance.attend"),
        ("subject-relation", "lives in", "location.lives_in"),
        ("subject-relation", "live in", "location.live_in"),
        ("subject-relation", "based in", "location.based_in"),
        ("subject-relation", "buys from", "commerce.buys_from"),
        ("subject-relation", "buy from", "commerce.buy_from"),
        ("subject-relation", "maintains", "stewardship.maintains"),
        ("subject-relation", "maintain", "stewardship.maintain"),
        ("subject-relation", "builds", "stewardship.builds"),
        ("subject-relation", "build", "stewardship.build"),
        ("subject-relation", "organises", "stewardship.organises"),
        ("subject-relation", "organise", "stewardship.organise"),
        ("subject-relation", "organizes", "stewardship.organizes"),
        ("subject-relation", "organize", "stewardship.organize"),
        ("body-field", "member of", "field.membership"),
        ("body-field", "membership", "field.membership"),
        ("body-field", "affiliation", "field.membership"),
        ("body-field", "works at", "field.work_at"),
        ("body-field", "works with", "field.work_with"),
        ("body-field", "uses", "field.uses"),
        ("body-field", "attends", "field.attends"),
        ("body-field", "location", "field.location"),
        ("body-field", "based in", "field.location"),
        ("body-field", "buys from", "field.buys_from"),
        ("body-field", "maintains", "field.maintains"),
        ("body-field", "builds", "field.builds"),
        ("body-field", "organises", "field.organises"),
        ("body-field", "organizes", "field.organises"),
    ],
)
def test_every_frozen_token_extracts_its_exact_predicate_id(
    frame: str,
    token: str,
    expected: str,
) -> None:
    registry = entity_types.core_registry()
    entities = entity_recurrence.RegistryIndex(entries=(), identities=frozenset())
    if frame == "typed-copula":
        body = f"amber guild {token} an organization."
    elif frame == "typed-label":
        body = f"organization {token} amber guild."
    elif frame == "subject-relation":
        body = f"I {token} amber guild."
    else:
        body = f"{token}: amber guild."

    rows = entity_recurrence.extract_identity_frames(
        body,
        path="Knowledge Base/Notes/frame.md",
        origin="page:frame",
        entity_types=registry,
        registry=entities,
    )

    assert len(rows) == 1
    assert rows[0].frame_type == frame
    assert rows[0].predicate_id == expected
    assert rows[0].identity == "amber guild"


@pytest.mark.parametrize(
    "candidate",
    [
        "12345",
        "https://example.invalid",
        "person@example.invalid",
        "/srv/private/file",
        "2026-08-31",
        "12:30",
        "this",
        "the and of",
        "one two three four five six seven eight nine",
    ],
)
def test_rejected_span_classes_contribute_no_frame(candidate: str) -> None:
    rows = entity_recurrence.extract_identity_frames(
        f"I work with {candidate}.",
        path="Knowledge Base/Notes/rejected.md",
        origin="page:rejected",
        entity_types=entity_types.core_registry(),
        registry=entity_recurrence.RegistryIndex(entries=(), identities=frozenset()),
    )

    assert rows == ()


def test_maximal_span_is_not_speculatively_shortened() -> None:
    rows = entity_recurrence.extract_identity_frames(
        "I work with amber guild beside station.",
        path="Knowledge Base/Notes/maximal.md",
        origin="page:maximal",
        entity_types=entity_types.core_registry(),
        registry=entity_recurrence.RegistryIndex(entries=(), identities=frozenset()),
    )

    assert [row.identity for row in rows] == ["amber guild beside station"]


def test_two_independently_qualifying_incompatible_family_components_are_ambiguous(
    tmp_path: Path,
) -> None:
    rows = (
        "shared label is an organization.",
        "organization: shared label.",
        "shared label was an organization.",
        "shared label is a concept.",
        "concept: shared label.",
        "shared label was a concept.",
    )
    for index, body in enumerate(rows):
        _note(tmp_path, index, body)

    findings = _findings(tmp_path)

    assert len(findings) == 1
    assert findings[0].meta["candidate_state"] == "ambiguous"
    assert len(findings[0].meta["incompatible_components"]) == 2


def test_incompatible_component_projection_has_an_exact_top_level_cap(
    tmp_path: Path,
) -> None:
    for component in range(12):
        anchor = f"Anchor {component:02d}"
        _entity(tmp_path, f"anchor-{component:02d}", title=anchor)
        for offset, predicate in enumerate(("works with", "uses", "attends")):
            _note(
                tmp_path,
                component * 3 + offset,
                f"shared label {predicate} {anchor}.",
            )

    findings = _findings(tmp_path)

    assert len(findings) == 1
    meta = findings[0].meta
    assert meta["candidate_state"] == "ambiguous"
    assert meta["incompatible_component_count"] == 12
    assert meta["returned_incompatible_component_count"] == 8
    assert meta["incompatible_components_truncated"] == 4
    assert len(meta["incompatible_components"]) == 8
    assert [
        component["resolved_entity_refs"][0]
        for component in meta["incompatible_components"]
    ] == [
        f"Knowledge Base/Entities/Organizations/anchor-{index:02d}.md"
        for index in range(8)
    ]


def test_incompatible_component_provenance_has_exact_nested_caps(
    tmp_path: Path,
) -> None:
    _entity(tmp_path, "anchor-a", title="Anchor A")
    _entity(tmp_path, "anchor-b", title="Anchor B")
    predicates = list(entity_recurrence.PREDICATE_TABLE["relation"])[:18]
    for index, predicate in enumerate(predicates):
        _note(tmp_path, index, f"shared label {predicate} Anchor A.")
        _note(tmp_path, index + 18, f"Anchor A {predicate} shared label.")
    for offset, predicate in enumerate(("works with", "uses", "attends")):
        _note(tmp_path, 36 + offset, f"shared label {predicate} Anchor B.")

    findings = _findings(tmp_path)

    assert len(findings) == 1
    meta = findings[0].meta
    assert meta["candidate_state"] == "ambiguous"
    component = meta["incompatible_components"][0]
    assert component["page_count"] == 36
    assert component["returned_page_count"] == len(component["pages"]) == 8
    assert component["pages_truncated"] == 28
    assert component["origin_count"] == 36
    assert component["returned_origin_count"] == len(component["origins"]) == 8
    assert component["origins_truncated"] == 28
    assert component["facet_count"] == 36
    assert component["returned_facet_count"] == len(component["facet_hashes"]) == 8
    assert component["facets_truncated"] == 28
    assert component["resolved_entity_ref_count"] == 1
    assert component["returned_resolved_entity_ref_count"] == 1
    assert component["resolved_entity_refs_truncated"] == 0


def test_small_incompatible_fragment_does_not_weaken_component_gate(
    tmp_path: Path,
) -> None:
    rows = (
        "shared label is an organization.",
        "organization: shared label.",
        "shared label was an organization.",
        "shared label is a concept.",
    )
    for index, body in enumerate(rows):
        _note(tmp_path, index, body)

    findings = _findings(tmp_path)

    assert len(findings) == 1
    assert findings[0].meta["candidate_state"] == "promotion"
    assert findings[0].meta["incompatible_components"] == []


def test_material_fingerprint_ignores_same_page_copies_and_binds_registry(
    tmp_path: Path,
) -> None:
    for index, body in enumerate(
        (
            "amber guild is an organization.",
            "organization: amber guild.",
            "amber guild was an organization.",
        )
    ):
        _note(tmp_path, index, body)
    first = _findings(tmp_path)[0].meta
    copied = tmp_path / "Knowledge Base/Notes/note-00.md"
    copied.write_text(
        copied.read_text(encoding="utf-8")
        + "\namber guild is an organization.\n",
        encoding="utf-8",
    )
    find.clear_cache()
    second = _findings(tmp_path)[0].meta

    assert second["signal_version"] == first["signal_version"]
    assert second["evidence_fingerprint"] == first["evidence_fingerprint"]

    proposal = {
        "schema_version": 1,
        "entity_types": {
            "community": _definition(
                "Communities", "Community", parent="organization"
            )
        },
    }
    registry_path = tmp_path / "Knowledge Base/_Schema/entity-types.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(yaml.safe_dump(proposal, sort_keys=False), encoding="utf-8")
    find.clear_cache()
    third = _findings(tmp_path)[0].meta

    assert third["registry_fingerprint"] != first["registry_fingerprint"]
    assert third["signal_version"] != first["signal_version"]


def test_collection_never_invokes_model_embedding_or_write_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        type(
            "Page",
            (),
            {
                "rel_path": f"Knowledge Base/Notes/note-{index}.md",
                "title": f"Note {index}",
                "status": "active",
                "frontmatter": {"type": "insight", "status": "active"},
                "body": body,
            },
        )()
        for index, body in enumerate(
            (
                "amber guild is an organization.",
                "organization: amber guild.",
                "Membership: amber guild.",
            )
        )
    ]
    registry = entity_types.core_registry()
    entities = entity_recurrence.registry_index(pages, entity_types=registry)

    def forbidden(*_args, **_kwargs):
        pytest.fail("recurrence collection crossed model, embedding, or write authority")

    monkeypatch.setattr(embeddings, "get_model", forbidden)
    monkeypatch.setattr(embeddings, "embed_texts", forbidden)
    monkeypatch.setattr(runtime_resources, "model_execution", forbidden)
    monkeypatch.setattr(vault, "batch_atomic_write", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)

    candidates = entity_recurrence.collect(
        pages,
        vault_root=tmp_path,
        resolver=vault.WikilinkResolver.from_entries(tmp_path, ()),
        registry=entities,
        entity_types=registry,
        indexable=lambda _path: True,
        attachment_probe=lambda _path: False,
    )

    assert [item.identity for item in candidates] == ["amber guild"]


def test_explicit_attention_projects_one_current_row_and_default_stays_quiet(
    tmp_path: Path,
) -> None:
    for index, body in enumerate(
        (
            "amber guild is an organization.",
            "organization: amber guild.",
            "Membership: amber guild.",
        )
    ):
        _note(tmp_path, index, body)

    explicit = attention.attention(tmp_path, categories=["entity_recurrence"])
    default = attention.attention(tmp_path)

    rows = [item for item in explicit.items if "entity_recurrence" in item.categories]
    assert len(rows) == 1
    assert rows[0].reasons[0]["meta"]["candidate_state"] == "promotion"
    assert all("entity_recurrence" not in item.categories for item in default.items)
