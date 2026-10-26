"""Vault homes, end to end (`add-note-type-registry`, organisation PR A).

An owner declares a home: a folder under Notes plus the topic and project keys it
serves. Typed creation then files a compiled note of any type there, keeps the
type's own obligations, and never changes who can see the page.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from exomem import audit, commands
from exomem import note as note_module
from exomem import vault as vault_module
from exomem.governance.principal import library_scope

TODAY = dt.date(2026, 10, 26)
KB = "Knowledge Base"
HOME = f"{KB}/Notes/Equipment"
UNIT = "## Observations\n\n- [finding] The pump holds its pressure.\n"
EQUIPMENT = {
    "label": "Equipment",
    "description": "Notes about the workshop equipment.",
    "attributes": {"folder": "Notes/Equipment", "topics": ["equipment"]},
}


def _save(vault: Path, entries: dict, why: str = "the owner groups these notes") -> dict:
    with library_scope():
        inspected = commands.op_schema_memory(vault, subject="homes", operation="inspect")
        return commands.op_schema_memory(
            vault,
            subject="homes",
            operation="save",
            proposal={"upsert": entries},
            expected_hash=inspected["content_hash"],
            why=why,
        )


def _remember(vault: Path, **arguments) -> dict:
    arguments.setdefault("content", UNIT)
    arguments.setdefault("status", "draft")
    with library_scope():
        return commands.op_remember(vault, **arguments)


def _gate_codes(vault: Path, path: str, frontmatter: dict, content: str = UNIT) -> set[str]:
    with library_scope():
        validation = commands.op_manage_memory_file(
            vault,
            operation="create",
            path=path,
            content=content,
            frontmatter=frontmatter,
            validate_only=True,
        )
    return {finding["code"] for finding in validation["contract_result"]["blocking_findings"]}


def _frontmatter(vault: Path, rel: str) -> dict:
    return vault_module.parse_frontmatter((vault / rel).read_text(encoding="utf-8"))[0]


def _files(vault: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()}


# --------------------------------------------------------------------------- #
# Placement: the outcome
# --------------------------------------------------------------------------- #


def test_a_topic_home_holds_notes_of_several_types_with_their_own_obligations(vault: Path) -> None:
    _save(vault, {"equipment": EQUIPMENT})

    insight = _remember(vault, title="Pump pressure holds", note_type="insight", topics=["equipment"])
    failure = _remember(
        vault, title="Pump seal failed", note_type="failure", severity="moderate", topics=["equipment"]
    )

    assert insight["path"].startswith(f"{HOME}/")
    assert failure["path"].startswith(f"{HOME}/")
    assert _frontmatter(vault, insight["path"])["type"] == "insight"
    failure_metadata = _frontmatter(vault, failure["path"])
    assert (failure_metadata["type"], failure_metadata["severity"]) == ("failure", "moderate")
    # The home does not relax the write gate: a bare insight still owes a semantic unit.
    bare = _gate_codes(vault, f"{HOME}/bare.md", {"type": "insight", "status": "active"}, "Just prose.\n")
    assert "missing_semantic_unit" in bare


def test_a_research_note_goes_to_the_home_that_serves_its_project(vault: Path) -> None:
    project_home = {
        "label": "Rigs",
        "description": "Everything about the rigs.",
        "attributes": {"folder": "Notes/Rigs", "projects": ["project-alpha"]},
    }
    _save(vault, {"rigs": project_home})

    research = _remember(
        vault, title="Rig findings", note_type="research-note", project="project-alpha"
    )
    # The plural `projects` never places a page.
    insight = _remember(
        vault, title="Rig lesson", note_type="insight", projects=["project-alpha"]
    )

    assert research["path"].startswith(f"{KB}/Notes/Rigs/")
    assert insight["path"].startswith(f"{KB}/Notes/Insights/")


def test_the_first_topic_with_a_home_places_the_page(vault: Path) -> None:
    _save(vault, {"equipment": EQUIPMENT})

    page = _remember(
        vault, title="Pump health", note_type="insight", topics=["health", "equipment"]
    )

    assert page["path"].startswith(f"{HOME}/")


def test_a_home_keeps_the_type_stem(vault: Path) -> None:
    _save(vault, {"equipment": EQUIPMENT})

    experiment = note_module.note(
        vault,
        content=UNIT,
        note_type="experiment",
        title="Pump soak",
        domain="health",
        started="2026-09-14",
        duration="2 weeks",
        topics=["equipment"],
        validate_only=True,
        today=TODAY,
    )

    assert experiment.destination == f"{HOME}/2026-09-pump-soak.md"


@pytest.mark.parametrize(
    ("arguments", "destination"),
    [
        (
            {"note_type": "research-note", "project": "project-alpha"},
            f"{KB}/Notes/Research/Project Alpha/the-title.md",
        ),
        ({"note_type": "insight"}, f"{KB}/Notes/Insights/the-title.md"),
        (
            {
                "note_type": "experiment",
                "domain": "health",
                "started": "2026-09-14",
                "duration": "2 weeks",
            },
            f"{KB}/Notes/Experiments/Health/2026-09-the-title.md",
        ),
        (
            {"note_type": "production-log", "medium": "posts"},
            f"{KB}/Notes/Productions/Posts/2026-10-the-title.md",
        ),
    ],
)
def test_without_homes_each_type_lands_where_it_always_did(
    vault: Path, arguments: dict, destination: str
) -> None:
    draft = note_module.note(
        vault, content=UNIT, title="The title", validate_only=True, today=TODAY, **arguments
    )

    assert draft.destination == destination


def test_a_topic_without_a_home_leaves_placement_alone(vault: Path) -> None:
    page = _remember(vault, title="Pump health", note_type="failure", topics=["equipment"])

    assert page["path"].startswith(f"{KB}/Notes/Failures/")


def _replacement_draft(vault: Path, old_path: str, note_type: str):
    with library_scope():
        return commands.op_replace_memory(
            vault,
            old_path=old_path,
            content=UNIT,
            title="Pump pressure v2",
            note_type=note_type,
            reason="the pump was replaced",
            validate_only=True,
        )


def test_a_superseding_page_stays_in_its_predecessors_home(vault: Path) -> None:
    _save(vault, {"equipment": EQUIPMENT})
    old = _remember(vault, title="Pump pressure v1", note_type="insight", topics=["equipment"])

    draft = _replacement_draft(vault, old["path"], "failure")

    assert draft["destination"] == f"{HOME}/pump-pressure-v2.md"


def test_a_superseding_page_follows_its_type_when_the_predecessor_is_no_home_page(
    vault: Path,
) -> None:
    _save(vault, {"equipment": EQUIPMENT})
    old = _remember(vault, title="Pump pressure v1", note_type="insight")

    draft = _replacement_draft(vault, old["path"], "failure")

    assert draft["destination"] == f"{KB}/Notes/Failures/pump-pressure-v2.md"


# --------------------------------------------------------------------------- #
# Route meaning
# --------------------------------------------------------------------------- #


def test_a_compiled_page_is_accepted_in_a_research_folder_of_another_type(vault: Path) -> None:
    codes = _gate_codes(
        vault,
        f"{KB}/Notes/Research/Project Alpha/lesson.md",
        {"type": "insight", "status": "active"},
    )

    assert not codes & {"COMPILED_TYPE_MISMATCH", "COMPILED_DESTINATION_MISMATCH"}


def test_a_type_folder_accepts_a_compiled_page_of_another_type(vault: Path) -> None:
    codes = _gate_codes(
        vault, f"{KB}/Notes/Insights/misfiled.md", {"type": "failure", "status": "active"}
    )

    assert not codes & {"COMPILED_TYPE_MISMATCH", "COMPILED_DESTINATION_MISMATCH"}


def test_an_untyped_page_in_a_type_folder_is_refused(vault: Path) -> None:
    codes = _gate_codes(vault, f"{KB}/Notes/Insights/untyped.md", {"status": "active"})

    assert "COMPILED_TYPE_MISMATCH" in codes


def test_a_compiled_page_outside_notes_is_refused(vault: Path) -> None:
    codes = _gate_codes(
        vault, f"{KB}/Projects/Rigs/lesson.md", {"type": "insight", "status": "active"}
    )

    assert "COMPILED_DESTINATION_MISMATCH" in codes


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


def test_a_home_saves_announces_and_restores_through_schema_memory(vault: Path) -> None:
    from exomem import homes
    from exomem.vocabulary import registry

    assert _save(vault, {"equipment": EQUIPMENT})["saved"]
    overlay = yaml.safe_load(homes.registry_path(vault).read_text(encoding="utf-8"))
    assert overlay["entries"]["equipment"]["attributes"]["folder"] == "Notes/Equipment"
    with library_scope():
        announced = commands.op_bootstrap(vault, section="vocabulary")["vocabulary"]
        assert announced["homes"] == {"new": ["equipment"]}

        history = commands.op_schema_memory(vault, subject="homes", operation="history")
        restored = commands.op_schema_memory(
            vault,
            subject="homes",
            operation="restore",
            version=history["versions"][0]["version"],
            expected_hash=history["content_hash"],
            why="the owner keeps these notes by type",
        )
    assert restored["removed_keys"] == ["equipment"]
    assert registry.load(homes.SPEC, vault).active == ()
    assert _remember(
        vault, title="Pump health", note_type="insight", topics=["equipment"]
    )["path"].startswith(f"{KB}/Notes/Insights/")


@pytest.mark.parametrize(
    ("entry", "code"),
    [
        # The write gate never routes a `Sources` segment, so no page could be written there.
        ({"attributes": {"folder": "Notes/Sources/Rigs", "topics": ["rigs"]}}, "HOME_FOLDER_RESERVED"),
        ({"attributes": {"folder": "Elsewhere/Rigs", "topics": ["rigs"]}}, "INVALID_HOME_FOLDER"),
        ({"attributes": {"folder": "Notes/../Rigs", "topics": ["rigs"]}}, "INVALID_HOME_FOLDER"),
        # A shipped type folder already has its own owner.
        ({"attributes": {"folder": "Notes/Insights", "topics": ["rigs"]}}, "HOME_FOLDER_TAKEN"),
        ({"attributes": {"folder": "Notes/Equipment", "topics": ["rigs"]}}, "HOME_FOLDER_TAKEN"),
        ({"attributes": {"folder": "Notes/Rigs", "topics": ["equipment"]}}, "HOME_TOPIC_TAKEN"),
        ({"attributes": {"folder": "Notes/Rigs", "topics": ["Not A Key"]}}, "INVALID_HOME_ATTRIBUTE"),
        ({"attributes": {"folder": "Notes/Rigs"}}, "INVALID_HOME_ATTRIBUTE"),
    ],
)
def test_a_save_refuses_a_home_that_could_not_serve(vault: Path, entry: dict, code: str) -> None:
    from exomem import homes
    from exomem.vocabulary import registry

    _save(vault, {"equipment": EQUIPMENT})
    overlay = homes.registry_path(vault).read_bytes()

    with pytest.raises(registry.RegistryError, match=code):
        _save(vault, {"rigs": {"label": "Rigs", **entry}})
    assert homes.registry_path(vault).read_bytes() == overlay


def test_a_save_names_the_home_that_already_serves_a_key(vault: Path) -> None:
    from exomem.vocabulary import registry

    _save(vault, {"equipment": {**EQUIPMENT, "attributes": {**EQUIPMENT["attributes"], "projects": ["rig"]}}})

    with pytest.raises(registry.RegistryError, match=r"HOME_PROJECT_TAKEN.*equipment"):
        _save(vault, {"rigs": {"label": "Rigs", "attributes": {"folder": "Notes/Rigs", "projects": ["rig"]}}})


def test_a_home_folder_cannot_move_once_saved(vault: Path) -> None:
    from exomem.vocabulary import registry

    _save(vault, {"equipment": EQUIPMENT})

    with pytest.raises(registry.RegistryError, match="IMMUTABLE_REGISTRY_MEANING"):
        _save(vault, {"equipment": {"attributes": {"folder": "Notes/Gear"}}})


# --------------------------------------------------------------------------- #
# Scope safety
# --------------------------------------------------------------------------- #


def test_a_home_under_a_wider_governance_scope_is_refused_and_writes_nothing(vault: Path) -> None:
    scope = vault / KB / "_Governance" / "scopes" / "equipment.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: ["Knowledge Base/Notes/Equipment/**"]\n',
        encoding="utf-8",
    )
    _save(vault, {"equipment": EQUIPMENT})
    before = _files(vault)

    with pytest.raises(ValueError, match="HOME_CHANGES_SCOPE"):
        _remember(vault, title="Pump pressure", note_type="insight", topics=["equipment"])

    assert _files(vault) == before


def test_a_home_in_the_same_governance_scope_as_the_default_is_accepted(vault: Path) -> None:
    scope = vault / KB / "_Governance" / "scopes" / "notes.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: ["Knowledge Base/Notes/**"]\n',
        encoding="utf-8",
    )
    _save(vault, {"equipment": EQUIPMENT})

    page = _remember(vault, title="Pump pressure", note_type="insight", topics=["equipment"])

    assert page["path"].startswith(f"{HOME}/")


def test_a_home_that_moves_the_page_into_another_vocabulary_instance_is_refused(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.vocabulary import instances

    # Only the home's folder is bound to a private vocabulary instance; governance scopes agree.
    monkeypatch.setattr(
        instances,
        "page_scope",
        lambda root, path, frontmatter, **kwargs: "private" if path.startswith(HOME) else None,
    )
    _save(vault, {"equipment": EQUIPMENT})
    before = _files(vault)

    with pytest.raises(ValueError, match="HOME_CHANGES_SCOPE"):
        _remember(vault, title="Pump pressure", note_type="insight", topics=["equipment"])

    assert _files(vault) == before


# --------------------------------------------------------------------------- #
# The creation draft
# --------------------------------------------------------------------------- #


def test_the_draft_reports_where_the_page_goes_and_how_a_topic_becomes_a_home(vault: Path) -> None:
    with library_scope():
        draft = commands.op_remember(
            vault, title="Pump pressure", content=UNIT, note_type="insight",
            status="draft", topics=["equipment"], validate_only=True,
        )
        expected_hash = commands.op_schema_memory(
            vault, subject="homes", operation="inspect"
        )["content_hash"]

    placement = draft["placement"]
    assert (placement["folder"], placement["basis"]) == (f"{KB}/Notes/Insights", "type_default")
    call = placement["make_home"]
    assert call["tool"] == "schema_memory"
    assert call["arguments"]["subject"] == "homes"
    assert call["arguments"]["expected_hash"] == expected_hash
    assert call["arguments"]["proposal"]["upsert"]["equipment"]["attributes"] == {
        "folder": "Notes/Equipment",
        "topics": ["equipment"],
    }

    # The reported call is a working call: running it makes the next draft go to the home.
    with library_scope():
        saved = commands.op_schema_memory(
            vault, **call["arguments"], why="the pump notes share one folder"
        )
    assert saved["saved"]
    with library_scope():
        after = commands.op_remember(
            vault, title="Pump pressure", content=UNIT, note_type="insight",
            status="draft", topics=["equipment"], validate_only=True,
        )
    assert (after["placement"]["folder"], after["placement"]["basis"]) == (HOME, "home:equipment")
    assert "make_home" not in after["placement"]


def test_a_draft_without_topics_offers_no_home(vault: Path) -> None:
    with library_scope():
        draft = commands.op_remember(
            vault, title="Pump pressure", content=UNIT, note_type="insight",
            status="draft", validate_only=True,
        )

    assert draft["placement"] == {"folder": f"{KB}/Notes/Insights", "basis": "type_default"}


def test_a_near_miss_topic_whose_candidate_has_a_home_asks_for_a_decision(vault: Path) -> None:
    _save(vault, {"equipment": EQUIPMENT})
    arguments = {
        "title": "Pump pressure", "content": UNIT, "note_type": "insight",
        "status": "draft", "topics": ["equipmnt"], "validate_only": True,
    }

    with library_scope():
        asked = commands.op_remember(vault, **arguments)
        evidence = asked["vocabulary_preparation"]
        assert asked["vocabulary_decision"] == "required"
        assert "equipment" in {item["canonical"] for item in evidence["candidates"]}
        reused = commands.op_remember(
            vault,
            vocabulary_decision={
                "evidence_fingerprint": evidence["evidence_fingerprint"],
                "outcome": "reuse",
                "canonical": "equipment",
            },
            **arguments,
        )

    assert reused["destination"].startswith(f"{HOME}/")
    assert reused["placement"]["basis"] == "home:equipment"


def test_a_near_miss_topic_without_a_home_still_registers_with_a_warning(vault: Path) -> None:
    with library_scope():
        draft = commands.op_remember(
            vault, title="Pump pressure", content=UNIT, note_type="insight",
            status="draft", topics=["equipmnt"], validate_only=True,
        )

    assert "vocabulary_preparation" not in draft
    assert draft["placement"]["basis"] == "type_default"


# --------------------------------------------------------------------------- #
# Indexes and audit
# --------------------------------------------------------------------------- #

NOTES_INDEX = (
    "# Notes — Index\n\n## By type\n\n"
    "### Insights — distilled cross-cutting lessons (4)\n\n"
    "### Failures — documented failure modes (2)\n"
)


def test_a_homes_pages_are_counted_and_get_an_index_section(vault: Path) -> None:
    notes_index = vault / KB / "Notes" / "index.md"
    notes_index.write_text(NOTES_INDEX, encoding="utf-8")
    top_index = vault / KB / "index.md"
    top_index.write_text(
        top_index.read_text(encoding="utf-8").replace(
            "- Notes (insight): 4\n", "- Notes (insight): 4\n- Notes (equipment): 0\n"
        ),
        encoding="utf-8",
    )
    _save(vault, {"equipment": EQUIPMENT})

    for title in ("Pump pressure", "Pump flow"):
        _remember(vault, title=title, note_type="insight", topics=["equipment"])

    index = notes_index.read_text(encoding="utf-8")
    assert "### Equipment — Notes about the workshop equipment. (2)" in index
    assert "### Insights — distilled cross-cutting lessons (4)" in index
    top = top_index.read_text(encoding="utf-8")
    assert "- Notes (equipment): 2" in top
    assert "- Notes (insight): 4" in top
    # 4 research + 4 insight + 2 failure + 5 pattern + 1 production + 2 in the home.
    assert "- Notes: 18" in top
    with library_scope():
        assert audit.audit(vault, categories=["index_drift"]).findings == []


def test_index_drift_reports_a_stale_home_count(vault: Path) -> None:
    top_index = vault / KB / "index.md"
    top_index.write_text(
        top_index.read_text(encoding="utf-8").replace(
            "- Notes (insight): 4\n", "- Notes (insight): 4\n- Notes (equipment): 7\n"
        ),
        encoding="utf-8",
    )
    _save(vault, {"equipment": EQUIPMENT})
    page = vault / HOME / "pump.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: insight\nstatus: draft\n---\n\n# Pump\n\n" + UNIT, encoding="utf-8")

    with library_scope():
        details = [f.detail for f in audit.audit(vault, categories=["index_drift"]).findings]

    assert any("notes:equipment" in detail and "actual is 1" in detail for detail in details), details
