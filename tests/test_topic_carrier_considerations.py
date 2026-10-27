"""Topic-carrier considerations: what the owner is offered, where, and when it is not repeated.

Each check runs the producer, the review owner and the delivery step against a real fixture vault
and its real catalogue. They name what only the whole path catches: an item offered twice, a
deferral that does not hold, an advisory that displaces another notice, a ranking that runs on the
write path, or a caller that sees evidence the owner alone may read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from test_context_catalogue import INSIGHTS, KB, _built, _page, _write
from test_planning_mutation import _manifest
from test_topic_carriers import AREA, _carrier, _save

from exomem import context_catalogue as cc
from exomem import (
    planning,
    review_state,
    vocabulary_delivery,
    vocabulary_notifications,
    vocabulary_review,
)
from exomem import topic_carrier_considerations as tcc
from exomem.governance.principal import RequestPrincipal, library_scope, request_scope

TAG = "quay-works"
FOLDER = "Areas/Harbour"


def _world(vault: Path) -> None:
    """A folder of five unfiled pages, a tag on four, a tag that is a status key on three, a tag on two."""
    _write(
        vault,
        f"{KB}/_Schema/source-taxonomy.yaml",
        "schema_version: 1\n"
        "domains:\n"
        "  harbour: {label: Harbour, path_label: Harbour, description: Quays and moorings.}\n"
        "  fleet: {label: Fleet, path_label: Fleet, aliases: [boats]}\n",
    )
    for index in range(5):
        _page(vault, f"berth-{index}", f"summary: Berth {index} rules.\n", folder=AREA)
    for index in range(4):
        _page(vault, f"quay-{index}", f"tags: [{TAG}, Rigging]\n")
    for index in range(3):
        _page(vault, f"state-{index}", "tags: [active]\n")
    for index in range(2):
        _page(vault, f"pair-{index}", "tags: [two-pages]\n")


@pytest.fixture
def world(vault: Path) -> Path:
    _world(vault)
    _built(vault)
    return vault


def _ranked(vault: Path) -> dict[tuple[str, str], tcc.Candidate]:
    with library_scope():
        return {(found.kind, found.match): found for found in tcc.rank(vault)}


def _add_pages(vault: Path, names: list[str], frontmatter: str, folder: str = INSIGHTS) -> None:
    paths = [_page(vault, name, frontmatter, folder=folder) for name in names]
    cc.ContextCatalogue(vault).update(paths)


def _decision(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_ref": item["ref"], "fingerprint": item["fingerprint"], "family": item["family"],
        "choice": None, "registry_hashes": item["registry_hashes"], "target_versions": item["target_versions"],
        "outcome": "defer", "rationale": "Leave this carrier unfiled for now.",
    }


# -- what is offered ----------------------------------------------------------------------


def test_review_ranks_carriers_by_the_unfiled_entries_they_would_file(world: Path) -> None:
    ranked = _ranked(world)

    assert ranked[("folder", FOLDER)].unfiled == 5
    assert ranked[("tag", TAG)].unfiled == 4
    assert list(ranked).index(("folder", FOLDER)) < list(ranked).index(("tag", TAG))


def test_a_carrier_below_the_minimum_entry_count_is_not_offered(world: Path) -> None:
    assert ("tag", "two-pages") not in _ranked(world)


def test_a_parent_folder_is_offered_only_when_it_holds_unfiled_entries_its_child_does_not(world: Path) -> None:
    assert ("folder", "Areas") not in _ranked(world)

    _add_pages(world, ["dock-office"], "", folder=f"{KB}/Areas")

    assert _ranked(world)[("folder", "Areas")].unfiled == 6


def test_a_declared_carrier_that_files_its_entries_is_no_longer_offered(world: Path) -> None:
    _save(world, {"harbour-area": _carrier("folder", FOLDER, ["harbour"])})

    assert ("folder", FOLDER) not in _ranked(world)


def test_a_caller_other_than_the_owner_gets_no_consideration(world: Path) -> None:
    with request_scope(RequestPrincipal(audience_id="client-a", surface="mcp")):
        assert tcc.rank(world) == []
        assert tcc.after_write(world, f"{AREA}/berth-0.md") is None


# -- the write path ------------------------------------------------------------------------


def test_the_write_path_offers_only_the_written_pages_own_carrier(world: Path) -> None:
    # The folder of five outranks the tag of four across the vault, yet a tag page offers its tag.
    with library_scope():
        written = tcc.after_write(world, f"{INSIGHTS}/quay-0.md")

    assert written is not None
    assert written.logical_identity == f"tag:{TAG}"


def test_the_write_path_never_ranks_the_vault(world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a whole-vault pass ran on the write path")

    monkeypatch.setattr(cc.ContextCatalogue, "entries", refuse)
    monkeypatch.setattr(cc.ContextCatalogue, "membership_edges", refuse)
    with library_scope():
        assert tcc.after_write(world, f"{INSIGHTS}/quay-0.md") is not None


def test_a_carrier_is_announced_once_and_not_again_while_unchanged(world: Path) -> None:
    with library_scope():
        first = vocabulary_delivery._with_carrier_advisory(world, {"state": "committed"}, f"{INSIGHTS}/quay-0.md")  # noqa: SLF001 - the step after_commit runs
        second = vocabulary_delivery._with_carrier_advisory(world, {"state": "committed"}, f"{INSIGHTS}/quay-1.md")  # noqa: SLF001

    assert first["vocabulary_advisory"]["family"] == tcc.FAMILY
    assert first["vocabulary_advisory"]["decisions"] == ["defer"]
    assert "vocabulary_advisory" not in second


def test_the_advisory_never_displaces_an_occupied_slot(world: Path) -> None:
    held = {"state": "committed", "vocabulary_advisory": {"ref": "held"}}
    with library_scope():
        unchanged = vocabulary_delivery._with_carrier_advisory(world, held, f"{INSIGHTS}/quay-0.md")  # noqa: SLF001
        later = vocabulary_delivery._with_carrier_advisory(world, {"state": "committed"}, f"{INSIGHTS}/quay-0.md")  # noqa: SLF001

    assert unchanged == held
    # The occupied slot reserved nothing, so the notice is still owed once the slot is free.
    assert later["vocabulary_advisory"]["family"] == tcc.FAMILY


def test_a_quiet_family_silences_the_advisory_but_explicit_review_still_lists_it(world: Path) -> None:
    review_state.ReviewStateStore(world).set_disposition(
        "vocabulary_topic_carriers", "quiet", why="too_frequent: the owner reviews in batches"
    )
    with library_scope():
        quiet = vocabulary_delivery._with_carrier_advisory(world, {"state": "committed"}, f"{INSIGHTS}/quay-0.md")  # noqa: SLF001
        listed = vocabulary_review.review(world, limit=16)

    assert "vocabulary_advisory" not in quiet
    assert any(row["signal"] == tcc.SIGNAL for row in listed["items"])


# -- defer and material change ---------------------------------------------------------------


def _listed(vault: Path) -> list[dict[str, Any]]:
    with library_scope():
        return [row for row in vocabulary_review.review(vault, limit=16)["items"] if row["signal"] == tcc.SIGNAL]


def test_a_deferred_carrier_is_not_listed_again_until_its_evidence_changes_materially(world: Path) -> None:
    tag_row = next(row for row in _listed(world) if row["logical_identity"] == f"tag:{TAG}")
    with library_scope():
        vocabulary_review.decide(world, ref=tag_row["ref"], decision=_decision(tag_row))
    assert all(row["logical_identity"] != f"tag:{TAG}" for row in _listed(world))

    # Four and five unfiled pages share a power-of-two bucket, so one more page changes nothing.
    _add_pages(world, ["quay-4"], f"tags: [{TAG}]\n")
    assert all(row["logical_identity"] != f"tag:{TAG}" for row in _listed(world))

    # Eight unfiled pages is the next bucket, so the carrier is offered again.
    _add_pages(world, ["quay-5", "quay-6", "quay-7"], f"tags: [{TAG}]\n")
    assert any(row["logical_identity"] == f"tag:{TAG}" for row in _listed(world))


def test_one_new_page_in_the_same_bucket_leaves_the_fingerprint_unchanged(world: Path) -> None:
    with library_scope():
        before = next(found for found in tcc.rank(world) if found.match == TAG)
        fingerprint = tcc.item(world, before).fingerprint
    _add_pages(world, ["quay-4"], f"tags: [{TAG}]\n")
    with library_scope():
        after = next(found for found in tcc.rank(world) if found.match == TAG)

    assert after.unfiled == before.unfiled + 1
    assert tcc.item(world, after).fingerprint == fingerprint


# -- the evidence --------------------------------------------------------------------------


def test_the_evidence_carries_the_samples_the_agent_needs_to_infer_a_topic(world: Path) -> None:
    with library_scope():
        packet = tcc.evidence(world, f"folder:{FOLDER.casefold()}")

    assert packet["carrier"] == {"kind": "folder", "match": FOLDER}
    assert (packet["entries"], packet["unfiled"]) == (5, 5)
    assert [row["title"] for row in packet["sample"]["entries"]] == [f"berth-{index}" for index in range(5)]
    assert packet["sample"]["entries"][0]["description"] == "Berth 0 rules."
    assert {key for key in packet["domains"]["items"][0]} == {"key", "label", "description", "aliases"}
    assert "fleet" in {row["key"] for row in packet["domains"]["items"]}


def test_the_sample_is_bounded_and_ordered_by_a_stable_key(world: Path) -> None:
    _add_pages(world, [f"extra-{index:02d}" for index in range(10)], "", folder=AREA)
    with library_scope():
        packet = tcc.evidence(world, f"folder:{FOLDER.casefold()}")

    paths = [row["ref"] for row in packet["sample"]["entries"]]
    assert len(paths) == tcc.SAMPLE_ENTRIES
    assert paths == sorted(paths)


def test_a_tag_that_is_a_status_key_is_flagged_not_refused(world: Path) -> None:
    with library_scope():
        flagged = tcc.evidence(world, "tag:active")
        plain = tcc.evidence(world, f"tag:{TAG}")

    assert flagged["registry_key_collisions"] == ["statuses"]
    assert plain["registry_key_collisions"] == []


def test_the_evidence_lists_the_rejected_topics_and_never_offers_them(world: Path) -> None:
    _save(world, {"quay": _carrier("tag", TAG, [], rejected=["fleet"])})
    with library_scope():
        packet = tcc.evidence(world, f"tag:{TAG}")

    assert packet["rejected"] == ["fleet"]
    assert "fleet" not in {row["key"] for row in packet["domains"]["items"]}
    assert "harbour" in {row["key"] for row in packet["domains"]["items"]}


def test_the_evidence_counts_the_subject_domains_of_the_sources_its_entries_cite(world: Path) -> None:
    _write(world, f"{KB}/Sources/Articles/quay-survey.md", "---\ntype: source\ndomain: harbour\n---\n\nSurvey.\n")
    # KB-relative, the form a written wikilink takes when the Obsidian root is the knowledge base.
    _add_pages(world, ["cited"], f"tags: [{TAG}]\nsources:\n  - \"[[Sources/Articles/quay-survey]]\"\n")
    with library_scope():
        packet = tcc.evidence(world, f"tag:{TAG}")

    assert packet["source_domains"] == {"harbour": 1}


def test_evidence_for_a_carrier_that_no_longer_files_unfiled_entries_is_stale(world: Path) -> None:
    _save(world, {"harbour-area": _carrier("folder", FOLDER, ["harbour"])})

    with library_scope(), pytest.raises(ValueError, match="VOCABULARY_DECISION_STALE"):
        tcc.evidence(world, f"folder:{FOLDER.casefold()}")


# -- where it surfaces ---------------------------------------------------------------------


def test_a_folder_of_plan_items_is_offered_and_its_review_holds_through_a_defer(vault: Path) -> None:
    # Plan anchors carry a `plan:` id and a real path; a folder carrier reaches them by path.
    manifest = f"{KB}/Planning/Harbour/_collection.md"
    planning.create_collection(vault, manifest, _manifest() + "\nHarbour planning items.\n", why="seed a collection")
    for index in range(6):
        planning.add(
            vault, manifest, plan_id=str(UUID(int=index + 1)),
            item={"title": f"Harbour step {index}", "kind": "outcome"}, why="seed a plan item",
        )
    _built(vault)

    row = next(row for row in _listed(vault) if row["logical_identity"] == "folder:planning/harbour")
    with library_scope():
        context = vocabulary_review.context(vault, ref=row["ref"], expected_fingerprint=row["fingerprint"])
        vocabulary_review.decide(vault, ref=row["ref"], decision=_decision(row))

    assert context["carrier"]["unfiled"] >= 6
    assert all(listed["logical_identity"] != "folder:planning/harbour" for listed in _listed(vault))


def test_the_topic_carrier_family_has_a_review_disposition_owner() -> None:
    assert vocabulary_notifications.REVIEW_FAMILIES[tcc.FAMILY] == "vocabulary_topic_carriers"
