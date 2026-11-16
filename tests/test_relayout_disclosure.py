"""The relayout plan's disclosure diff, read through `exomem store-mode relayout --plan --full`.

The vault is the generated nested vault of `relayout_fixture.py`, with scopes, an access file,
videos and collection pages added where a case needs them. Every expected value is the
fixture's own arithmetic: which page a scope selects, which page the plan renames, which
folder an access prefix names.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from relayout_fixture import (
    GHOST,
    KB,
    RECORD_PAGE,
    RETIRED_LINKER,
    SCOPE_PARTITION,
    TALK,
    add_governed_content,
    add_links_from_collection_and_part_pages,
    build_nested_vault,
)
from test_relayout_plan import PART_PAGE, full_report, tree_state


@pytest.fixture
def plain_vault(tmp_path: Path) -> Path:
    build_nested_vault(tmp_path)
    return tmp_path


@pytest.fixture
def governed_vault(plain_vault: Path) -> Path:
    add_governed_content(plain_vault)
    return plain_vault


def disclosure_of(capsys, root: Path) -> dict:
    return full_report(capsys, root)["disclosure"]


def scope_named(disclosure: dict, name: str) -> dict:
    (found,) = [item for item in disclosure["scopes"]["items"]["items"] if item["name"] == name]
    return found


def paths_of(sample: list[dict]) -> set[str]:
    return {entry["path"] for entry in sample}


def test_a_vault_with_no_policy_and_no_access_file_reports_none_and_zero_differences(
    plain_vault, capsys
):
    """No policy is a stated state with zero differences, never an unknown that an operator must chase."""
    report = full_report(capsys, plain_vault)
    disclosure = report["disclosure"]

    assert (disclosure["scopes"]["state"], disclosure["access_tiers"]["state"]) == ("none", "none")
    assert disclosure["figures"] == {
        "differences": 0,
        "scopes_changed": 0,
        "tiers_changed": 0,
        "broken_inheritance_edges": 0,
        "untranslatable_selectors": 0,
    }
    assert "Disclosure differences: 0 (" in report["outcome"]


def test_a_scope_that_selects_pages_by_path_loses_the_pages_the_plan_renames(
    governed_vault, capsys
):
    """A path glob and a path ref are written against today's names, so a renamed page leaves its scope."""
    disclosure = disclosure_of(capsys, governed_vault)

    renamed = scope_named(disclosure, "renamed")
    assert (renamed["members_before"], renamed["members_after"]) == (2, 0)
    assert renamed["entering"] == 0
    assert paths_of(renamed["leaving_sample"]) == {
        f"{KB}/Notes/Insights/dup-1.md",
        f"{KB}/Notes/Insights/rust-go.md",
    }


def test_a_scope_over_a_renamed_area_keeps_every_member_once_its_paths_are_translated(
    governed_vault, capsys
):
    """A scope over the Insights area and a scope over a project keep their members: the planner translates their paths."""
    disclosure = disclosure_of(capsys, governed_vault)

    for name in ("area", "project"):
        scope = scope_named(disclosure, name)
        assert scope["members_before"] == scope["members_after"] > 0
        assert (scope["entering"], scope["leaving"]) == (0, 0)


def test_the_plan_lists_each_selector_that_names_a_page_whose_name_changes(governed_vault, capsys):
    """The operator edits these two selectors by hand; the plan cannot translate a name that the title rule picks."""
    disclosure = disclosure_of(capsys, governed_vault)

    listed = {
        (item["field"], item["value"]): item["examples"]
        for item in disclosure["untranslatable_selectors"]["items"]
    }
    assert listed == {
        ("paths", f"{KB}/Notes/Insights/dup-*"): [f"{KB}/Notes/Insights/dup-1.md"],
        ("refs", f"{KB}/Notes/Insights/rust-go.md"): [f"{KB}/Notes/Insights/rust-go.md"],
    }


def test_a_page_that_moves_without_a_frame_pointer_leaves_the_media_scope_of_its_video(
    governed_vault, capsys
):
    """A frame takes its video page's scopes only while its pointer names the video; an unrepointed one loses them."""
    disclosure = disclosure_of(capsys, governed_vault)

    media = scope_named(disclosure, "media")
    assert paths_of(media["leaving_sample"]) == {f"{GHOST}.frames/frame-001.md"}


def test_an_inheritance_edge_that_breaks_names_its_reason_and_what_the_page_gets_instead(
    governed_vault, capsys
):
    """Two frames break in different ways. A frame whose video page keeps its name (clip) does not appear."""
    disclosure = disclosure_of(capsys, governed_vault)

    broken = {
        item["page"]: (item["reason"], item["effect"])
        for item in disclosure["broken_inheritance_edges"]["items"]
    }
    assert broken == {
        f"{GHOST}.frames/frame-001.md": (
            "frame_pointer_not_repointed",
            "page_takes_its_own_scopes_only",
        ),
        f"{TALK}.frames/frame-001.md": (
            "parent_page_absent_after_the_move",
            "membership_unresolved_so_access_is_refused",
        ),
    }
    # The talk frame cannot be placed in any scope after the move; it is counted, not dropped.
    assert disclosure["files_not_evaluated"]["membership_unresolved_after"] == 1


def test_the_access_tier_of_a_page_that_the_plan_renames_changes_when_the_prefix_names_the_file(
    governed_vault, capsys
):
    """An access prefix that names a single file stops matching once the file has its title as a name."""
    tiers = disclosure_of(capsys, governed_vault)["access_tiers"]

    assert tiers["state"] == "evaluated"
    assert tiers["files_whose_tier_changes"] == 1
    (change,) = tiers["changes"]
    assert (change["from"], change["to"]) == ("readonly", "read-write")
    assert paths_of(change["sample"]) == {f"{KB}/Notes/Insights/hidden.md"}
    prefixes = tiers["prefixes"]
    assert (prefixes["values"], prefixes["maps"], prefixes["does_not_map"]) == (3, 2, 1)
    (unmapped,) = prefixes["values_that_do_not_map"]["items"]
    assert (unmapped["value"], unmapped["files_after"]) == ("Notes/Insights/hidden.md", 0)
    # The excluded folder keeps its single page, so its tier does not move.
    assert tiers["files_by_tier_today"]["excluded"] == tiers["files_by_tier_after"]["excluded"] == 1


def test_flattening_the_partition_folders_changes_a_scope_only_under_that_alternative(
    governed_vault, capsys
):
    """The plan keeps partition folders; a scope over one of them changes membership only if they would flatten."""
    report = full_report(capsys, governed_vault)

    partition = scope_named(report["disclosure"], "partition")
    assert partition["members_before"] == partition["members_after"] == 2
    flattened = report["alternatives"]["o1_flatten"]["scopes_whose_members_change"]
    assert flattened["count"] == 1
    (changed,) = flattened["scopes"]["items"]
    assert (changed["scope"], changed["files_that_change_membership"]) == (SCOPE_PARTITION, 2)


def test_a_policy_that_does_not_compile_is_unknown_and_never_an_empty_diff(governed_vault, capsys):
    """An empty difference would tell the operator to proceed on a policy that the plan could not read."""
    broken = governed_vault / KB / "_Governance" / "scopes" / "broken.yaml"
    broken.write_text(": : [\n", encoding="utf-8")

    report = full_report(capsys, governed_vault)

    disclosure = report["disclosure"]
    assert disclosure["scopes"]["state"] == "unknown"
    assert "invalid_yaml" in disclosure["scopes"]["detail"]
    # A compile warning does not block a policy, so it is not the reason given.
    assert "SCOPE_CANNOT_SELECT_MEDIA" not in disclosure["scopes"]["detail"]
    figures = disclosure["figures"]
    assert figures["scopes_changed"] == figures["untranslatable_selectors"] == "unknown"
    assert figures["differences"] == "unknown"
    assert "Disclosure differences: unknown (" in report["outcome"]
    # What does not depend on the policy is still read.
    assert disclosure["access_tiers"]["state"] == "evaluated"
    assert figures["tiers_changed"] == 2
    assert figures["broken_inheritance_edges"] == 2


def test_an_access_file_that_does_not_parse_is_unknown_and_never_all_open(governed_vault, capsys):
    """An unreadable access file must not read as no restriction: the diff is unknown for the tiers."""
    (governed_vault / KB / "_access.yaml").write_text("readonly: [unclosed\n", encoding="utf-8")

    report = full_report(capsys, governed_vault)

    disclosure = report["disclosure"]
    assert disclosure["access_tiers"]["state"] == "unknown"
    assert disclosure["figures"]["tiers_changed"] == "unknown"
    assert disclosure["figures"]["differences"] == "unknown"
    assert "Disclosure differences: unknown (" in report["outcome"]
    # The scopes do not read that file, so they are still evaluated.
    assert disclosure["scopes"]["state"] == "evaluated"
    assert disclosure["figures"]["scopes_changed"] == 2


def test_the_edits_of_collection_and_part_pages_are_listed_apart_and_written_nowhere(
    plain_vault, capsys
):
    """The collection store and the part writer own these pages; the plan names their edits and writes none."""
    add_links_from_collection_and_part_pages(plain_vault)
    before = tree_state(plain_vault)

    report = full_report(capsys, plain_vault)

    assert tree_state(plain_vault) == before
    foreign = report["links_in_collection_and_part_pages"]
    assert foreign["pages_scanned"] == 2
    assert {edit["page"] for edit in foreign["edits"]["items"]} == {RECORD_PAGE, PART_PAGE}
    counts = report["counts"]
    assert counts["link_edits_in_collection_and_part_pages"] == 2
    # The headline count of edits is the pages that Exomem owns, so these two are not in it.
    assert counts["link_edits"] == 4
    assert "2 more link edits" in report["outcome"]


def test_a_link_to_a_missing_collection_page_is_counted_as_retired_and_not_as_unresolved(
    plain_vault, tmp_path_factory, capsys
):
    """A link into a collection layer that resolves to no page is a retired collection path, not a link to repair."""
    baseline = full_report(capsys, plain_vault)["counts"]["links_unresolved"]
    with_links = tmp_path_factory.mktemp("with_collection_links")
    build_nested_vault(with_links)
    add_links_from_collection_and_part_pages(with_links)

    report = full_report(capsys, with_links)

    assert report["counts"]["links_unresolved"] == baseline
    assert report["counts"]["links_to_retired_collection_paths"] == 2
    assert {item["page"] for item in report["to_retired_collection_paths"]["items"]} == {
        RECORD_PAGE,
        RETIRED_LINKER,
    }
