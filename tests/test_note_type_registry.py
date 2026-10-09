"""The note-type registry, end to end (`add-note-type-registry`, S4a).

An owner registers a vault-defined compiled type with its own folder. Ranking,
the semantic write gate and activation then treat its page as compiled. A
restore removes the type again; the page keeps its bytes and becomes
unregistered note-type debt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import activation, commands, lexstore
from exomem import find as find_module

MEETING_PAGE = "Knowledge Base/Notes/Meetings/2026-10-quillwort-sync.md"
MEETING_SOURCE = (
    "---\ntype: meeting-note\nstatus: active\ncreated: 2026-10-01\nupdated: 2026-10-01\n---\n\n"
    "# Quillwort sync\n\nThe quillwort crew agreed the tide schedule.\n"
)
MEETING_TYPE = {
    "label": "Meeting note",
    "description": "The agreed outcome of one meeting.",
    "attributes": {"role": "compiled", "folder": "Notes/Meetings"},
}


def _type_factor(vault: Path) -> float:
    explained = commands.op_find(
        vault,
        query="quillwort",
        mode="hybrid",
        graph=False,
        rerank=False,
        scope="kb-only",
        detail="compact",
        explain=True,
    )
    [hit] = [hit for hit in explained["hits"] if hit["path"] == MEETING_PAGE]
    [step] = [step for step in hit["ranking_explanation"]["multipliers"] if step["name"] == "type"]
    return step["factor"]


def _gate_codes(vault: Path) -> set[str]:
    validation = commands.op_manage_memory_file(
        vault,
        operation="create",
        path="Knowledge Base/Notes/Meetings/2026-10-second-sync.md",
        content="# Second sync\n\nThe crew met again.\n",
        frontmatter={"type": "meeting-note", "status": "active"},
        validate_only=True,
    )
    return {finding["code"] for finding in validation["contract_result"]["blocking_findings"]}


def _eligible(vault: Path) -> bool:
    page = find_module._parse_page(vault / MEETING_PAGE, 0.0, vault)
    return activation.is_eligible_compiled_page(vault, page)


def _debt(vault: Path) -> set[str]:
    # maintain_memory's audit is owner-only, so a restricted caller reaches the
    # audit library entry; every case reads debt the same way.
    from exomem import audit

    return {
        finding.path
        for finding in audit.audit(vault, categories=["frontmatter_compliance"]).findings
        if (finding.meta or {}).get("code") == "unregistered_note_type"
    }


def test_a_vault_compiled_type_takes_compiled_behaviour_until_restored(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance.principal import library_scope

    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    page = vault / MEETING_PAGE
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(MEETING_SOURCE, encoding="utf-8")
    original = page.read_bytes()

    with library_scope():
        assert _type_factor(vault) == 1.0
        assert "missing_semantic_unit" not in _gate_codes(vault)
        assert not _eligible(vault)
        assert MEETING_PAGE in _debt(vault)

        catalogue = lexstore.catalog_semantic_identity(vault)
        inspected = commands.op_schema_memory(vault, subject="note-types", operation="inspect")
        saved = commands.op_schema_memory(
            vault,
            subject="note-types",
            operation="save",
            proposal={"upsert": {"meeting-note": MEETING_TYPE}},
            expected_hash=inspected["content_hash"],
            why="meeting outcomes are compiled conclusions",
        )
        assert saved["valid"] and saved["saved"]
        assert lexstore.catalog_semantic_identity(vault) != catalogue
        # Bootstrap lists no note-type keys, but it still announces a new one.
        announced = commands.op_bootstrap(vault, section="vocabulary")["vocabulary"]
        assert announced["note-types"] == {"new": ["meeting-note"]}

        assert _type_factor(vault) == pytest.approx(1.15)
        assert "missing_semantic_unit" in _gate_codes(vault)
        assert _eligible(vault)
        assert MEETING_PAGE not in _debt(vault)

        history = commands.op_schema_memory(vault, subject="note-types", operation="history")
        restored = commands.op_schema_memory(
            vault,
            subject="note-types",
            operation="restore",
            version=history["versions"][0]["version"],
            expected_hash=history["content_hash"],
            why="the owner keeps meetings as plain pages",
        )
        assert restored["removed_keys"] == ["meeting-note"]
        assert lexstore.catalog_semantic_identity(vault) == catalogue
        assert "note-types" not in commands.op_bootstrap(vault, section="vocabulary")["vocabulary"]

        assert _type_factor(vault) == 1.0
        assert "missing_semantic_unit" not in _gate_codes(vault)
        assert not _eligible(vault)
        assert MEETING_PAGE in _debt(vault)
    assert page.read_bytes() == original


def _save(vault: Path, delta: dict, why: str) -> dict:
    from exomem.governance.principal import library_scope

    with library_scope():
        inspected = commands.op_schema_memory(vault, subject="note-types", operation="inspect")
        return commands.op_schema_memory(
            vault,
            subject="note-types",
            operation="save",
            proposal=delta,
            expected_hash=inspected["content_hash"],
            why=why,
        )


def test_a_saved_type_keeps_its_role_folder_and_period(vault: Path) -> None:
    from exomem import note_types
    from exomem.vocabulary import registry

    _save(vault, {"upsert": {"meeting-note": MEETING_TYPE}}, "meetings are conclusions")
    overlay = note_types.registry_path(vault).read_bytes()

    for attributes in (
        {"role": "entity"},
        {"folder": "Notes/Minutes"},
        {"time_bounded": True},
    ):
        with pytest.raises(registry.RegistryError, match="IMMUTABLE_REGISTRY_MEANING"):
            _save(vault, {"upsert": {"meeting-note": {"attributes": attributes}}}, "rewrite")
    assert note_types.registry_path(vault).read_bytes() == overlay


@pytest.mark.parametrize(
    ("delta", "code"),
    [
        ({"memo": {"attributes": {"role": "summary"}}}, "INVALID_NOTE_TYPE_ROLE"),
        (
            {"minutes": {"attributes": {"role": "compiled", "folder": "Notes/Meetings"}}},
            "NOTE_TYPE_FOLDER_TAKEN",
        ),
        (
            {"minutes": {"attributes": {"role": "compiled", "folder": "Notes/Insights"}}},
            "NOTE_TYPE_FOLDER_TAKEN",
        ),
        ({"minutes": {"attributes": {"role": "compiled"}}}, "INVALID_NOTE_TYPE_FOLDER"),
        (
            {"minutes": {"attributes": {"role": "compiled", "folder": "Notes/Meetings/Weekly"}}},
            "INVALID_NOTE_TYPE_FOLDER",
        ),
        ({"memo": {"attributes": {"folder": "Notes/Memos"}}}, "INVALID_NOTE_TYPE_FOLDER"),
        # The write gate never routes `data`, so no page of the type could be written.
        (
            {"data-note": {"attributes": {"role": "compiled", "folder": "Notes/Data"}}},
            "NOTE_TYPE_FOLDER_RESERVED",
        ),
        ({"memo": {"attributes": {"multiplier": 2.0}}}, "INVALID_REGISTRY_DELTA"),
        ({"insight": {"attributes": {"role": "entity"}}}, "IMMUTABLE_REGISTRY_MEANING"),
    ],
)
def test_a_save_refuses_a_meaning_outside_the_closed_schema(
    vault: Path, delta: dict, code: str
) -> None:
    from exomem import note_types
    from exomem.vocabulary import registry

    _save(vault, {"upsert": {"meeting-note": MEETING_TYPE}}, "meetings are conclusions")
    overlay = note_types.registry_path(vault).read_bytes()

    with pytest.raises(registry.RegistryError, match=code):
        _save(vault, {"upsert": delta}, "attempt a meaning the schema does not hold")
    assert note_types.registry_path(vault).read_bytes() == overlay


def test_a_registered_folder_routes_like_a_shipped_one(vault: Path) -> None:
    from exomem.governance.principal import library_scope

    def misfiled_insight_codes() -> set[str]:
        with library_scope():
            validation = commands.op_manage_memory_file(
                vault,
                operation="create",
                path="Knowledge Base/Notes/Meetings/2026-10-misfiled.md",
                content="# Misfiled\n\nAn insight filed with the meetings.\n",
                frontmatter={"type": "insight", "status": "active"},
                validate_only=True,
            )
        return {finding["code"] for finding in validation["contract_result"]["blocking_findings"]}

    # Before the save the folder belongs to no type, so the insight is off its route.
    assert "COMPILED_DESTINATION_MISMATCH" in misfiled_insight_codes()
    _save(vault, {"upsert": {"meeting-note": MEETING_TYPE}}, "meetings are conclusions")
    # After it the folder routes to `meeting-note`, so the insight has the wrong type.
    after = misfiled_insight_codes()
    assert "COMPILED_TYPE_MISMATCH" in after
    assert "COMPILED_DESTINATION_MISMATCH" not in after


def test_the_shipped_pack_reproduces_each_former_type_set() -> None:
    from exomem import note_types

    compiled = {"research-note", "insight", "failure", "pattern", "experiment", "production-log"}
    former_sets = {
        note_types.ranks_as_compiled: compiled | {"entity"},
        note_types.compiled: compiled,
        note_types.governed_endpoint: compiled | {"entity"},
        note_types.connectable: compiled | {"entity", "source"},
        note_types.stale_reviewed: {"research-note", "insight", "failure", "pattern", "entity"},
        note_types.sources_required: {"research-note", "insight", "failure", "pattern"},
        note_types.ranks_as_source: {"source"},
        note_types.raw: {"source", "evidence"},
    }
    for predicate, members in former_sets.items():
        assert {note_type.key for note_type in note_types.shipped(predicate)} == members, predicate
    assert {
        note_type.key: note_type.folder for note_type in note_types.shipped(note_types.compiled)
    } == {
        "research-note": "Notes/Research",
        "insight": "Notes/Insights",
        "failure": "Notes/Failures",
        "pattern": "Notes/Patterns",
        "experiment": "Notes/Experiments",
        "production-log": "Notes/Productions",
    }


def _withhold_note_types(vault: Path) -> None:
    """A governed policy that withholds the note-type overlay from `external`."""
    from exomem import find as find_module
    from exomem.governance import egress, membership, policy

    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "note-types.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nname: Note types\n"
        'paths: ["_Schema/note-types.yaml"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "note-types-external.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
        f"audience: external\nceiling: {egress.LEVEL_NONE}\n",
        encoding="utf-8",
    )
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()


def test_shipped_types_classify_the_same_under_any_overlay(vault: Path) -> None:
    from exomem import note_types
    from exomem.governance.principal import RequestPrincipal, library_scope, request_scope

    shipped_insight = note_types.shipped_registry().types["insight"]
    overlay = note_types.registry_path(vault)
    _withhold_note_types(vault)
    outsider = RequestPrincipal(audience_id="external", surface="mcp")

    with library_scope():
        absent = note_types.Basis(vault).resolve("insight")
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        "schema_version: 1\nentries:\n  insight:\n    attributes: {role: entity}\n",
        encoding="utf-8",
    )
    with library_scope():
        invalid_basis = note_types.Basis(vault)
        invalid = invalid_basis.resolve("insight")
        inspected = commands.op_schema_memory(vault, subject="note-types", operation="inspect")
    with request_scope(outsider):
        denied_basis = note_types.Basis(vault)
        denied = denied_basis.resolve("insight")

    assert absent.note_type == invalid.note_type == denied.note_type == shipped_insight
    assert inspected["findings"][0]["code"] == "invalid_note_type_registry"
    # A shipped type never reads the overlay, so no private identity enters a result.
    assert invalid_basis.dependency[0] == denied_basis.dependency[0] == "public"


def test_an_invalid_overlay_refuses_a_vault_type_write_until_fixed(vault: Path) -> None:
    from exomem import note_types
    from exomem.cli_ops import OpError
    from exomem.governance.principal import library_scope

    overlay = note_types.registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("schema_version: 1\nentries: [meeting-note]\n", encoding="utf-8")

    with library_scope(), pytest.raises(OpError) as refused:
        _gate_codes(vault)

    # The owner can fix the overlay, so the refusal points at its findings.
    assert refused.value.code == "NOTE_TYPE_DEFINITION_UNAVAILABLE"
    assert 'schema_memory(subject="note-types", operation="inspect")' in refused.value.remediation


def test_an_invalid_overlay_never_refuses_a_move_for_a_page_it_only_relinks(vault: Path) -> None:
    from exomem import note_types
    from exomem.governance.principal import library_scope

    target = vault / "Knowledge Base" / "Projects" / "tide-table.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nstatus: active\n---\n\n# Tide table\n\nThe tides.\n", encoding="utf-8")
    page = vault / MEETING_PAGE
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(MEETING_SOURCE + "\nSee [[tide-table]].\n", encoding="utf-8")
    overlay = note_types.registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("schema_version: 1\nentries: [meeting-note]\n", encoding="utf-8")

    with library_scope():
        commands.op_manage_memory_file(
            vault,
            operation="move",
            old_path="Knowledge Base/Projects/tide-table.md",
            new_path="Knowledge Base/Projects/tide-schedule.md",
        )

    # The meeting page needs the broken overlay, but the move only rewrites its link.
    assert "[[tide-schedule]]" in page.read_text(encoding="utf-8")


def test_a_denied_caller_cannot_tell_a_private_type_from_an_unknown_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance.principal import RequestPrincipal, request_scope

    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    page = vault / MEETING_PAGE
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(MEETING_SOURCE, encoding="utf-8")
    # An untyped twin that sorts first on a tie: a boosted meeting page would overtake it.
    twin = vault / "Knowledge Base" / "Inbox" / "quillwort-twin.md"
    twin.parent.mkdir(parents=True, exist_ok=True)
    twin.write_text(MEETING_SOURCE.replace("type: meeting-note\n", ""), encoding="utf-8")
    _withhold_note_types(vault)
    outsider = RequestPrincipal(audience_id="external", surface="mcp")

    observed = []
    for delta in (None, {"upsert": {"meeting-note": MEETING_TYPE}}):
        if delta is not None:
            _save(vault, delta, "meetings are conclusions")
        with request_scope(outsider):
            read = commands.op_read_memory(vault, path=MEETING_PAGE)
            # A restricted find serves no explanation, so compare what it ranks.
            ranked = [
                (hit["path"], hit.get("score"))
                for hit in commands.op_find(vault, query="quillwort", mode="hybrid", graph=False)
            ]
            # The writer judges the page against the shipped pack: no type, no obligation.
            codes = _gate_codes(vault)
            debt = _debt(vault)
        observed.append((ranked, codes, debt))
        assert "quillwort crew" in read["body"]
        assert [path for path, _score in ranked][:2] == [
            "Knowledge Base/Inbox/quillwort-twin.md",
            MEETING_PAGE,
        ]

    assert observed[0] == observed[1]
    assert "missing_semantic_unit" not in observed[0][1]
    assert observed[0][2] == set()


def test_note_type_usage_is_unavailable_without_a_graph(vault: Path) -> None:
    inspected = commands.op_schema_memory(vault, subject="note-types", operation="inspect")

    assert inspected["counts"] == "unavailable"
    assert inspected["reason"]
    assert all("count" not in entry for entry in inspected["entries"])


def test_product_page_types_are_never_note_type_debt(vault: Path) -> None:
    from exomem import collection_profiles, records

    # Every `type:` value a product writer emits, and an untyped page. Writers
    # that hold their type in a constant are read from it, so a new collection
    # profile or held-record type fails here until the pack defines it.
    product_types = (
        "research-note",
        "insight",
        "failure",
        "pattern",
        "experiment",
        "production-log",
        "entity",
        "source",
        "evidence",
        "collection",
        "collection-summary",
        "dataset",
        "adoption-manifest",
        "adoption-run-manifest",
        *(profile.item_type for profile in collection_profiles.PROFILES.values()),
        records._HELD_TYPE,
        None,
    )
    folder = vault / "Knowledge Base" / "Projects" / "Typed"
    folder.mkdir(parents=True, exist_ok=True)
    for index, page_type in enumerate(product_types):
        frontmatter = f"type: {page_type}\n" if page_type else "status: active\n"
        (folder / f"page-{index}.md").write_text(
            f"---\n{frontmatter}---\n\nA product page.\n", encoding="utf-8"
        )

    assert _debt(vault) == set()
