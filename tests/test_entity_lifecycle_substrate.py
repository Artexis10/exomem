"""Focused contracts for recurring-identity lifecycle and entity families."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from exomem import (
    attention,
    audit,
    entity_candidates,
    entity_recurrence,
    entity_types,
    epistemic_graph,
    find,
    referent_resolution,
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


def test_span_rejection_classes_and_maximal_span_do_not_create_candidates(
    tmp_path: Path,
) -> None:
    rejected = (
        "I work with 12345.",
        "I work with https://example.invalid.",
        "I work with person@example.invalid.",
        "I work with /srv/private/file.",
        "I work with 2026-08-31.",
        "I work with `inline name`.",
        "I work with this.",
        "organization: organization.",
        "I work with one two three four five six seven eight nine.",
        "I work with amber consortium beside the station.",
    )
    for offset, text in enumerate(rejected):
        for copy in range(3):
            _note(tmp_path, offset * 3 + copy, text)

    assert _findings(tmp_path) == []


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
                "juniper circle", "[[Juniper Circle]]"
            ),
            encoding="utf-8",
        )
    find.clear_cache()

    second = _findings(tmp_path)
    assert len(second) == 1
    second_meta = second[0].meta
    assert second_meta["disconnected_context_count"] == 2
    assert second_meta["remaining_disconnected_count"] == 0
    assert second_meta["signal_version"] != first_signal
    assert not set(first_batch) & {
        row["path"] for row in second_meta["disconnected_contexts"]
    }

    for rel in [row["path"] for row in second_meta["disconnected_contexts"]]:
        path = tmp_path / rel
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "juniper circle", "[[Juniper Circle]]"
            ),
            encoding="utf-8",
        )
    find.clear_cache()
    assert _findings(tmp_path) == []


def test_hydration_signal_binds_a_context_outside_the_returned_batch(
    tmp_path: Path,
) -> None:
    _entity(tmp_path, "juniper", title="Juniper Circle")
    for index in range(10):
        _note(
            tmp_path,
            index,
            "juniper circle is an organization."
            if index % 2 == 0
            else "Membership: juniper circle.",
        )
    first = _findings(tmp_path)[0].meta
    first_paths = [row["path"] for row in first["disconnected_contexts"]]
    outside = tmp_path / "Knowledge Base/Notes/note-09.md"
    outside.write_text(
        outside.read_text(encoding="utf-8").replace(
            "juniper circle", "[[Juniper Circle]]"
        ),
        encoding="utf-8",
    )
    find.clear_cache()

    second = _findings(tmp_path)[0].meta

    assert [row["path"] for row in second["disconnected_contexts"]] == first_paths
    assert second["signal_version"] != first["signal_version"]
    assert second["batch_fingerprint"] != first["batch_fingerprint"]


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
