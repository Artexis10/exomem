"""Topic carriers: an owner declares which topics a project key, a folder or a tag gives its entries.

The workflow under test is an agent saving carriers through `schema_memory` and the next
context map filing each carrier's entries under those topics, with no page write. The checks
name what only a real vault catches: a declaration the save must refuse, a join that reads
the wrong field, an overlay a caller may not read, and a catalogue built before tags existed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_context_catalogue import INSIGHTS, KB, _built, _page, _write
from test_governance_egress import _external
from test_working_set_withheld_twins import _withhold

from exomem import commands, project_keys
from exomem import context_catalogue as cc
from exomem import context_views as cv
from exomem.governance import egress
from exomem.governance.principal import library_scope, request_scope
from exomem.vocabulary import registry

AREA = f"{KB}/Areas/Harbour"
RESEARCH = f"{KB}/Notes/Research"
EQUIPMENT = f"{KB}/Notes/Equipment"


def _world(vault: Path) -> None:
    """Two domains, three project keys, a note per carrier kind and a home."""
    _write(
        vault,
        f"{KB}/_Schema/source-taxonomy.yaml",
        "schema_version: 1\n"
        "domains:\n"
        "  harbour: {label: Harbour, path_label: Harbour, description: Quays and moorings.}\n"
        "  fleet: {label: Fleet, path_label: Fleet, aliases: [boats]}\n",
    )
    _write(
        vault,
        f"{KB}/_Schema/project-keys.yaml",
        "projects:\n"
        "  harbour-ops: {folder: Harbour Ops, category: domain}\n"
        "  fleet-ops: {folder: Fleet Ops, category: domain, domain: fleet}\n",
    )
    _page(vault, "tide-work", "project: harbour-ops\n")
    _page(vault, "dock-log", "tags: [Quay-Works]\n")
    _page(vault, "berth-plan", folder=AREA)
    _write(
        vault,
        f"{KB}/_Schema/homes.yaml",
        "schema_version: 1\nentries:\n  equipment:\n    label: Equipment\n    attributes:\n"
        "      folder: Notes/Equipment\n      topics: [fleet]\n",
    )
    _page(vault, "pump", folder=EQUIPMENT)


def _carrier(kind: str, match: str, topics: list[str], *, rejected: list[str] | None = None) -> dict[str, Any]:
    return {
        "description": f"{kind} {match}",
        "attributes": {
            "kind": kind,
            "match": match,
            "topics": topics,
            "origin": {topic: "inferred" for topic in topics},
            "rejected": rejected or [],
        },
    }


def _inspect(vault: Path) -> dict[str, Any]:
    with library_scope():
        return commands.op_schema_memory(vault, subject="topic-carriers", operation="inspect")


def _save(vault: Path, entries: dict[str, Any], *, expected_hash: str | None = None) -> dict[str, Any]:
    with library_scope():
        return commands.op_schema_memory(
            vault,
            subject="topic-carriers",
            operation="save",
            proposal={"upsert": entries},
            expected_hash=expected_hash or _inspect(vault)["content_hash"],
            why="the agent files these entries under the topic its evidence names",
        )


def _codes(result: dict[str, Any]) -> set[str]:
    return {finding["code"] for finding in result["findings"]}


def _topics(catalogue: cc.ContextCatalogue) -> dict[str, tuple[str, ...]]:
    return {path.rsplit("/", 1)[-1].removesuffix(".md"): keys for path, keys in catalogue.memberships().topics.items()}


@pytest.fixture
def world(vault: Path) -> cc.ContextCatalogue:
    _world(vault)
    return _built(vault)


# -- the registry refuses what cannot file anything ---------------------------------------


def test_a_stale_hash_is_refused_and_writes_no_file(vault: Path, world: cc.ContextCatalogue) -> None:
    with pytest.raises(registry.RegistryError, match="STALE_REGISTRY"):
        _save(vault, {"quay": _carrier("tag", "quay-works", ["harbour"])}, expected_hash="0" * 64)

    assert not (vault / KB / "_Schema" / "topic-carriers.yaml").exists()


def test_a_topic_the_domains_registry_does_not_resolve_is_refused(vault: Path, world: cc.ContextCatalogue) -> None:
    result = _save(vault, {"quay": _carrier("tag", "quay-works", ["no-such-domain"])})

    assert result["valid"] is False
    assert "CARRIER_TOPIC_UNRESOLVED" in _codes(result)


def test_an_alias_is_refused_with_the_key_it_resolves_to(vault: Path, world: cc.ContextCatalogue) -> None:
    result = _save(vault, {"quay": _carrier("tag", "quay-works", ["boats"])})

    assert "CARRIER_TOPIC_NOT_CANONICAL" in _codes(result)
    assert "'fleet'" in result["findings"][0]["detail"]


def test_a_rejected_topic_is_refused_when_declared_again(vault: Path, world: cc.ContextCatalogue) -> None:
    _save(vault, {"quay": _carrier("tag", "quay-works", [], rejected=["fleet"])})

    with pytest.raises(registry.RegistryError, match="CARRIER_TOPIC_REJECTED"):
        _save(vault, {"quay": {"attributes": {"topics": ["fleet"], "origin": {"fleet": "inferred"}}}})


def test_a_second_carrier_for_the_same_tag_is_refused(vault: Path, world: cc.ContextCatalogue) -> None:
    _save(vault, {"quay": _carrier("tag", "quay-works", ["harbour"])})

    with pytest.raises(registry.RegistryError, match="CARRIER_DUPLICATE"):
        _save(vault, {"quay-again": _carrier("tag", "quay-works", ["fleet"])})


def test_a_hand_written_project_domain_reads_reviewed(vault: Path, world: cc.ContextCatalogue) -> None:
    declared = _inspect(vault)["hand_declared"]

    assert declared == [
        {"kind": "project", "match": "fleet-ops", "topics": ["fleet"], "origin": {"fleet": "reviewed"},
         "source": f"{KB}/_Schema/project-keys.yaml"}
    ]


def test_a_caller_denied_the_project_registry_sees_no_hand_declared_rows(vault: Path, world: cc.ContextCatalogue) -> None:
    _withhold(vault, project_keys.registry_relpath(), ceiling=0)
    with request_scope(_external()):
        result = commands.op_schema_memory(vault, subject="topic-carriers", operation="inspect")
        result = egress.postfilter("schema_memory", result, vault, request_kwargs={"subject": "topic-carriers"})

    assert "hand_declared" not in result
    assert "fleet-ops" not in str(result)


def test_a_project_carrier_repeating_a_hand_written_domain_is_refused(vault: Path, world: cc.ContextCatalogue) -> None:
    result = _save(vault, {"fleet-ops": _carrier("project", "fleet-ops", ["fleet"])})

    assert "CARRIER_PROJECT_DECLARED" in _codes(result)


def test_a_project_carrier_for_an_unknown_key_is_refused(vault: Path, world: cc.ContextCatalogue) -> None:
    result = _save(vault, {"ghost": _carrier("project", "ghost-ops", ["harbour"])})

    assert "CARRIER_PROJECT_UNKNOWN" in _codes(result)


def test_saving_a_project_carrier_leaves_the_project_key_file_unchanged(vault: Path, world: cc.ContextCatalogue) -> None:
    keys = vault / KB / "_Schema" / "project-keys.yaml"
    before = keys.read_bytes()

    _save(vault, {"harbour-ops": _carrier("project", "harbour-ops", ["harbour"])})

    assert keys.read_bytes() == before


def test_a_type_folder_is_refused_and_names_its_type(vault: Path, world: cc.ContextCatalogue) -> None:
    result = _save(vault, {"insights": _carrier("folder", "Notes/Insights", ["harbour"])})

    assert "CARRIER_FOLDER_TYPE" in _codes(result)
    assert "note type" in result["findings"][0]["detail"]


def test_an_ancestor_of_type_folders_is_refused(vault: Path, world: cc.ContextCatalogue) -> None:
    result = _save(vault, {"notes": _carrier("folder", "Notes", ["harbour"])})

    assert "CARRIER_FOLDER_ANCESTOR" in _codes(result)


def test_a_home_folder_is_refused(vault: Path, world: cc.ContextCatalogue) -> None:
    result = _save(vault, {"equipment": _carrier("folder", "Notes/Equipment", ["harbour"])})

    assert "CARRIER_FOLDER_TYPE" in _codes(result)
    assert "home equipment" in result["findings"][0]["detail"]


def test_a_folder_inside_a_project_partitioned_type_folder_is_refused(vault: Path, world: cc.ContextCatalogue) -> None:
    _page(vault, "study", folder=f"{RESEARCH}/Harbour Ops")
    _built(vault)

    result = _save(vault, {"study": _carrier("folder", "Notes/Research/Harbour Ops", ["harbour"])})

    assert "CARRIER_FOLDER_PARTITIONED" in _codes(result)


def test_a_folder_without_entries_is_refused(vault: Path, world: cc.ContextCatalogue) -> None:
    result = _save(vault, {"empty": _carrier("folder", "Areas/Nowhere", ["harbour"])})

    assert "CARRIER_FOLDER_EMPTY" in _codes(result)


def test_a_nested_tag_matches_only_itself(vault: Path, world: cc.ContextCatalogue) -> None:
    _page(vault, "deep-dive", "tags: [quay/works]\n")
    _built(vault)
    _save(vault, {"quay": _carrier("tag", "quay-works", ["harbour"])})

    topics = _topics(cc.ContextCatalogue(vault))

    assert topics["dock-log"] == ("harbour",)
    assert topics["deep-dive"] == ()


# -- an entry joins the topics of its carriers --------------------------------------------


def test_a_project_carrier_files_the_entries_of_its_key(vault: Path, world: cc.ContextCatalogue) -> None:
    _save(vault, {"harbour-ops": _carrier("project", "harbour-ops", ["harbour"])})

    assert _topics(cc.ContextCatalogue(vault))["tide-work"] == ("harbour",)


def test_a_folder_carrier_files_the_entries_in_its_folder(vault: Path, world: cc.ContextCatalogue) -> None:
    _save(vault, {"harbour-area": _carrier("folder", "Areas/Harbour", ["harbour"])})

    assert _topics(cc.ContextCatalogue(vault))["berth-plan"] == ("harbour",)


def test_a_tag_carrier_files_a_note_in_a_type_folder_that_has_no_project_key(
    vault: Path, world: cc.ContextCatalogue
) -> None:
    _save(vault, {"quay": _carrier("tag", "quay-works", ["harbour"])})

    assert _topics(cc.ContextCatalogue(vault))["dock-log"] == ("harbour",)


def test_a_home_files_the_entries_in_its_folder(vault: Path, world: cc.ContextCatalogue) -> None:
    assert _topics(world)["pump"] == ("fleet",)


def test_a_source_citation_gives_no_membership(vault: Path, world: cc.ContextCatalogue) -> None:
    _write(vault, f"{KB}/Sources/Articles/harbour-guide.md", "---\ntype: source\ndomain: harbour\n---\n\nGuide.\n")
    _page(vault, "citing-note", "sources: [harbour-guide]\n")
    catalogue = _built(vault)

    assert _topics(catalogue)["citing-note"] == ()


def test_a_deprecated_carrier_files_nothing(vault: Path, world: cc.ContextCatalogue) -> None:
    _save(vault, {"quay": _carrier("tag", "quay-works", ["harbour"])})
    with library_scope():
        commands.op_schema_memory(
            vault, subject="topic-carriers", operation="save", proposal={"deprecate": {"quay": None}},
            expected_hash=_inspect(vault)["content_hash"], why="the tag no longer names a subject",
        )

    assert _topics(cc.ContextCatalogue(vault))["dock-log"] == ()


def test_a_carrier_save_changes_the_owners_cached_map_with_no_page_write(
    vault: Path, world: cc.ContextCatalogue
) -> None:
    def served_topic_rows() -> set[str]:
        packet = cv.activation_packet(vault, 40_000, generation={})
        return {item["ref"] for item in packet["map"]["items"] if item["kind"] == "topic"}

    assert cv.topic_ref("harbour") not in served_topic_rows()
    pages_before = sorted(path.read_bytes() for path in (vault / KB / "Notes").rglob("*.md"))

    _save(vault, {"quay": _carrier("tag", "quay-works", ["harbour"])})

    assert cv.topic_ref("harbour") in served_topic_rows()
    assert sorted(path.read_bytes() for path in (vault / KB / "Notes").rglob("*.md")) == pages_before


def test_a_replace_changes_one_carrier_and_leaves_another_alone(vault: Path, world: cc.ContextCatalogue) -> None:
    _save(vault, {
        "quay": _carrier("tag", "quay-works", ["harbour"]),
        "harbour-area": _carrier("folder", "Areas/Harbour", ["harbour"]),
    })
    untouched = _inspect(vault)["entries"]
    untouched = next(row for row in untouched if row["key"] == "harbour-area")

    _save(vault, {"quay": {"attributes": {
        "topics": ["fleet"], "origin": {"fleet": "reviewed"}, "rejected": ["harbour"],
    }}})

    topics = _topics(cc.ContextCatalogue(vault))
    assert topics["dock-log"] == ("fleet",)
    assert topics["berth-plan"] == ("harbour",)
    assert next(row for row in _inspect(vault)["entries"] if row["key"] == "harbour-area") == untouched


def test_a_carrier_cannot_change_its_kind_or_match(vault: Path, world: cc.ContextCatalogue) -> None:
    _save(vault, {"quay": _carrier("tag", "quay-works", ["harbour"])})

    with pytest.raises(registry.RegistryError, match="IMMUTABLE_REGISTRY_MEANING"):
        _save(vault, {"quay": {"attributes": {"match": "other-tag"}}})


# -- a caller denied an overlay gets no membership from it --------------------------------


def _owner_like(*, carriers: bool, homes: bool) -> cv.CallerView:
    return cv.CallerView(None, None, "twin", True, True, carriers_admitted=carriers, homes_admitted=homes)


def _joined(catalogue: cc.ContextCatalogue, caller: cv.CallerView) -> dict[str, tuple[str, ...]]:
    return {row.path.rsplit("/", 1)[-1].removesuffix(".md"): row.topics for row in cv._world(catalogue, caller).rows}  # noqa: SLF001 - the join a map serves


def test_a_caller_denied_the_carriers_overlay_gets_no_membership_from_it(
    vault: Path, world: cc.ContextCatalogue
) -> None:
    _save(vault, {"quay": _carrier("tag", "quay-works", ["harbour"])})
    catalogue = cc.ContextCatalogue(vault)

    assert _joined(catalogue, _owner_like(carriers=True, homes=True))["dock-log"] == ("harbour",)
    assert _joined(catalogue, _owner_like(carriers=False, homes=True))["dock-log"] == ()


def test_a_caller_denied_the_homes_overlay_gets_no_membership_from_it(
    vault: Path, world: cc.ContextCatalogue
) -> None:
    catalogue = cc.ContextCatalogue(vault)

    assert _joined(catalogue, _owner_like(carriers=True, homes=True))["pump"] == ("fleet",)
    assert _joined(catalogue, _owner_like(carriers=True, homes=False))["pump"] == ()


# -- tags enter the catalogue in place ----------------------------------------------------


def test_a_tag_only_edit_moves_the_catalogue_generation(vault: Path, world: cc.ContextCatalogue) -> None:
    before = world.generation()
    page = vault / INSIGHTS / "dock-log.md"
    page.write_text(page.read_text(encoding="utf-8").replace("Quay-Works", "Quay-Works, Berths"), encoding="utf-8")

    cc.ContextCatalogue(vault).update([f"{INSIGHTS}/dock-log.md"])

    assert cc.ContextCatalogue(vault).generation() > before


def _legacy(vault: Path) -> None:
    """A catalogue as it stood before `entry_tags`: rows kept, no tags, no fill marker."""
    with sqlite3.connect(cc.sidecar_path(vault)) as conn:
        # Rows stored before tags joined the digest carry the older digest, so a fold rewrites each one.
        conn.execute("UPDATE entries SET entry_digest = 'before-entry-tags'")
        conn.execute("DELETE FROM entry_tags")
        conn.execute("DELETE FROM catalogue_meta WHERE key = 'entry_tags_filled'")


def test_the_tag_fill_stores_every_tag_without_a_wipe(vault: Path, world: cc.ContextCatalogue) -> None:
    _save(vault, {"quay": _carrier("tag", "quay-works", ["harbour"])})
    world.close()
    _legacy(vault)
    catalogue = cc.ContextCatalogue(vault)
    rows, generation = len(catalogue.entries()), catalogue.generation()

    assert catalogue.tags_filled() is False
    assert _topics(catalogue)["dock-log"] == ()  # the old rows keep serving, with no tag to join
    catalogue.update(fill_tags=True)

    assert catalogue.tags_filled() is True
    assert len(catalogue.entries()) == rows
    assert catalogue.generation() > generation
    assert _topics(catalogue)["dock-log"] == ("harbour",)


def test_a_request_never_runs_the_tag_fill_and_serves_the_catalogue_stale(
    vault: Path, world: cc.ContextCatalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.close()
    _legacy(vault)
    scheduled: list[Path] = []
    monkeypatch.setattr(cc, "_managed", lambda: True)
    monkeypatch.setattr(cc, "_schedule", lambda root, freshness_stamp="": scheduled.append(root))

    state, catalogue, unlisted = cc.ensure_unlisted(vault, freshness_stamp="fresh")

    assert (state, unlisted, scheduled) == (cc.READY, True, [vault.absolute()])
    assert catalogue is not None and catalogue.tags_filled() is False


# -- coverage -----------------------------------------------------------------------------


def test_coverage_counts_carriers_by_what_they_reach_and_names_unknown_tags(
    vault: Path, world: cc.ContextCatalogue
) -> None:
    _save(vault, {
        "harbour-area": _carrier("folder", "Areas/Harbour", ["harbour"]),
        "harbour-ops": _carrier("project", "harbour-ops", ["harbour"]),
        "quay": _carrier("tag", "quay-works", ["harbour"]),
        "moved": _carrier("tag", "no-page-has-this", ["harbour"]),
    })
    catalogue = cc.ContextCatalogue(vault)

    with library_scope():
        filled = catalogue.owner_coverage()["carriers"]
    catalogue.close()
    _legacy(vault)
    with library_scope():
        filling = cc.ContextCatalogue(vault).owner_coverage()["carriers"]

    assert filled == {"declared": 4, "filing": 3, "no_resolved_topic": 0, "dangling": 1, "unknown": 0}
    assert filling == {"declared": 4, "filing": 2, "no_resolved_topic": 0, "dangling": 0, "unknown": 2}


def test_coverage_counts_unresolved_carrier_and_home_topics(vault: Path, world: cc.ContextCatalogue) -> None:
    _write(
        vault,
        f"{KB}/_Schema/homes.yaml",
        "schema_version: 1\nentries:\n  equipment:\n    label: Equipment\n    attributes:\n"
        "      folder: Notes/Equipment\n      topics: [gone-domain]\n",
    )
    _save(vault, {"quay": _carrier("tag", "quay-works", ["harbour"])})
    _write(
        vault,
        f"{KB}/_Schema/source-taxonomy.yaml",
        "schema_version: 1\ndomains:\n  fleet: {label: Fleet, path_label: Fleet}\n",
    )

    with library_scope():
        coverage = cc.ContextCatalogue(vault).owner_coverage()

    assert (coverage["unresolved_declarations"]["carrier_topics"], coverage["unresolved_declarations"]["home_topics"]) == (1, 1)
    # A carrier that matches entries but resolves no topic still counts in one bucket of `declared`.
    assert coverage["carriers"] == {"declared": 1, "filing": 0, "no_resolved_topic": 1, "dangling": 0, "unknown": 0}
