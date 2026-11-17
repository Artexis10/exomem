"""The relayout plan, read through `exomem store-mode relayout --plan` from a generated nested vault.

Every expected value below is the fixture's own arithmetic or a walk of the generated files,
never the planner's output. The fixture sits in `relayout_fixture.py`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import unicodedata
from pathlib import Path

import pytest
from relayout_fixture import (
    BUNDLE,
    CHART,
    CHART_LINKER,
    KB,
    MEMBER_ARTIFACT,
    MEMO,
    RATE_LINKER,
    RATE_ORIGINAL,
    RATE_PAGE,
    REEL,
    REEL_FRAMES,
    add_attachment_links,
    add_container_member_and_companion,
    add_markdown_original_with_links,
    add_video_with_titled_pages,
    build_nested_vault,
)

from exomem import __main__ as cli
from exomem import obsidian_links

LINK_LAB = f"{KB}/Notes/Procedures/link-lab.md"
SCAN_A = f"{KB}/Evidence/Pricing/a/Scan.md"
PART_SOURCE = f"{KB}/Sources/Articles/2026-05-02-long-article.md"
PART_PAGE = f"{KB}/Sources/Articles/.2026-05-02-long-article.parts/part-02.md"
LIVE_PAGE = f"{KB}/Notes/Insights/Why churn rose"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    build_nested_vault(tmp_path)
    return tmp_path


def run(capsys, root: Path, *flags: str) -> tuple[int, str, str]:
    """The CLI as an operator runs it: exit code, standard output, standard error."""
    code = cli.main(["store-mode", "relayout", "--plan", *flags, "--vault", str(root)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def full_report(capsys, root: Path) -> dict:
    code, out, err = run(capsys, root, "--full")
    assert (code, err) == (0, "")
    return json.loads(out)


def items(listing: dict) -> list:
    return listing["items"]


def all_moves(report: dict) -> list[dict]:
    return items(report["moves"]["renamed"]) + items(report["moves"]["folder_only"])


def move_of(report: dict, current_path: str) -> dict:
    (found,) = [move for move in all_moves(report) if move["current_path"] == current_path]
    return found


def files_below_the_managed_folder(root: Path) -> set[str]:
    """Every file under the managed folder, from os.walk, leaving out the editor's own config folder."""
    found = set()
    for folder, directories, names in os.walk(root / KB):
        directories[:] = [name for name in directories if name != ".obsidian"]
        found.update((Path(folder) / name).relative_to(root).as_posix() for name in names)
    return found


def tree_state(root: Path) -> dict[str, tuple[str | None, int | None]]:
    """Every path with its content digest and modification time, so a touch counts as a write."""
    state: dict[str, tuple[str | None, int | None]] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file():
            state[rel] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        else:
            state[rel] = (None, None)
    return state


# --------------------------------------------------------------------------- #
# The report as a whole
# --------------------------------------------------------------------------- #


def test_the_plan_prints_its_outcome_first_and_states_what_the_counts_say(vault, capsys):
    code, out, err = run(capsys, vault)
    report = json.loads(out)
    counts = report["counts"]

    assert (code, err) == (0, "")
    assert next(iter(report)) == "outcome"
    assert out.startswith('{"outcome": "Relayout plan from nested to root')
    assert f"{counts['moved_files']} of {counts['files']} files move" in report["outcome"]
    assert report["outcome"].endswith("This plan wrote nothing.")


def test_every_file_below_the_managed_folder_is_planned_once_and_counted(vault, capsys):
    expected = files_below_the_managed_folder(vault)

    report = full_report(capsys, vault)
    counts = report["counts"]

    assert sorted(move["current_path"] for move in all_moves(report)) == sorted(expected)
    assert counts["files"] == counts["moved_files"] == len(expected)
    assert counts["markdown_files"] == sum(path.endswith(".md") for path in expected)
    assert items(report["hidden_entries_not_planned"]) == [f"{KB}/.obsidian"]


def test_no_two_files_get_the_same_new_path(vault, capsys):
    report = full_report(capsys, vault)

    new_paths = [
        unicodedata.normalize("NFC", move["new_path"]).casefold() for move in all_moves(report)
    ]

    assert len(new_paths) == len(set(new_paths))


def test_planning_writes_no_file_folder_or_timestamp(vault, capsys):
    before = tree_state(vault)

    run(capsys, vault, "--full")

    assert tree_state(vault) == before


def test_a_second_run_prints_the_same_plan_in_the_same_order(vault, capsys):
    first = full_report(capsys, vault)
    second = full_report(capsys, vault)
    # Stage timings are the only value that differs between runs.
    for report in (first, second):
        del report["diagnostics"]["read"]["timings_seconds"]

    assert second == first
    assert list(second) == list(first)


def test_the_counts_agree_with_the_listings_they_summarise(vault, capsys):
    report = full_report(capsys, vault)
    counts = report["counts"]
    moves = all_moves(report)

    assert counts["renamed_files"] == len(items(report["moves"]["renamed"]))
    assert counts["derived_names"] == sum(move["name_class"] == "derived" for move in moves)
    assert counts["fixed_names"] == sum(move["name_class"] == "fixed" for move in moves)
    assert counts["suffixed_names"] == sum(move["number"] > 1 for move in moves)
    assert counts["truncated_names"] == sum(move["truncated"] for move in moves)
    assert counts["corrected_names"] == sum(bool(move["corrections"]) for move in moves)
    assert counts["link_edits"] == len(items(report["link_edits"]))
    assert counts["pointer_edits"] == len(items(report["pointer_edits"]))


def test_a_long_listing_shows_twenty_entries_and_says_how_many_exist(vault, capsys):
    for number in range(30):
        (vault / KB / "Misc" / f"Extra {number:02}.md").write_text("A page.\n", encoding="utf-8")

    _code, short_out, _err = run(capsys, vault)
    short = json.loads(short_out)["moves"]["folder_only"]
    full = full_report(capsys, vault)["moves"]["folder_only"]

    assert full["of"] > 40
    assert (full["showing"], len(full["items"])) == (full["of"], full["of"])
    assert (short["showing"], short["of"], len(short["items"])) == (20, full["of"], 20)
    assert short["items"] == full["items"][:20]


def test_relayout_without_the_plan_flag_is_refused_before_it_reads_the_vault(vault, capsys):
    with pytest.raises(SystemExit) as refused:
        cli.main(["store-mode", "relayout", "--vault", str(vault)])

    assert refused.value.code == 2
    assert "--plan" in capsys.readouterr().err


def test_a_vault_already_in_the_root_layout_is_refused_with_what_to_do(vault, capsys):
    (vault / "_Schema").mkdir()
    (vault / "_Schema" / "vault-layout.yaml").write_text("layout: root\n", encoding="utf-8")

    code, out, err = run(capsys, vault)

    assert (code, out) == (1, "")
    first_line, *remediation = err.splitlines()
    assert first_line.startswith("RELAYOUT_NOTHING_TO_DO:")
    assert remediation


def test_a_registry_that_cannot_be_read_in_full_is_reported_in_the_outcome(vault, capsys):
    (vault / KB / "_Schema" / "note-types.yaml").write_text(
        "entries: [unclosed\n", encoding="utf-8"
    )

    report = full_report(capsys, vault)

    assert report["diagnostics"]["registry_findings"] == ["note_types_overlay_unavailable"]
    assert "Needs attention" in report["outcome"]


# --------------------------------------------------------------------------- #
# Names and folders
# --------------------------------------------------------------------------- #


def test_a_title_that_starts_with_a_dot_loses_the_dot_so_the_page_stays_visible(vault, capsys):
    move = move_of(full_report(capsys, vault), f"{KB}/Notes/Insights/hidden.md")

    assert move["new_path"] == "Insights/Hidden idea.md"
    assert move["corrections"] == ["leading_dots"]


def test_a_title_with_link_characters_loses_them_from_the_file_name(vault, capsys):
    move = move_of(full_report(capsys, vault), f"{KB}/Notes/Insights/rust-go.md")

    assert move["new_path"] == "Insights/Rust Go notes.md"
    assert move["corrections"] == ["link_characters"]


def test_two_titles_over_the_byte_cap_are_cut_and_still_get_distinct_names(vault, capsys):
    report = full_report(capsys, vault)

    first = move_of(report, f"{KB}/Notes/Insights/long-a.md")
    second = move_of(report, f"{KB}/Notes/Insights/long-b.md")

    for move in (first, second):
        assert len(Path(move["new_path"]).name.encode("utf-8")) <= 200
        assert move["truncated"] is True
    assert second["new_path"] != first["new_path"]
    assert second["new_path"].endswith(" (2).md")


def test_the_same_title_in_two_areas_takes_a_number_across_the_whole_vault(vault, capsys):
    report = full_report(capsys, vault)

    assert (
        move_of(report, f"{KB}/Notes/Failures/dup-2.md")["new_path"]
        == "Failures/Duplicate title.md"
    )
    assert (
        move_of(report, f"{KB}/Notes/Insights/dup-1.md")["new_path"]
        == "Insights/Duplicate title (2).md"
    )


def test_a_source_page_keeps_its_date_prefix_and_takes_its_title(vault, capsys):
    move = move_of(full_report(capsys, vault), PART_SOURCE)

    assert move["new_path"] == "Sources/Articles/2026-05-02-Long article.md"


def test_a_part_page_follows_its_source_pages_new_name_and_keeps_its_own(vault, capsys):
    move = move_of(full_report(capsys, vault), PART_PAGE)

    assert move["new_path"] == "Sources/Articles/.2026-05-02-Long article.parts/part-02.md"
    assert (move["name_class"], move["fixed_because"]) == ("fixed", "extraction_part")


def test_a_page_named_after_the_file_beside_it_keeps_its_name_whatever_its_title(vault, capsys):
    add_video_with_titled_pages(vault)
    report = full_report(capsys, vault)

    folder = "Sources/Media/reel.mp4.frames"
    # The video, its page, the frames folder and the frame pages keep their names; only the prefix drops.
    page = move_of(report, f"{REEL}.md")
    assert (page["new_path"], page["fixed_because"]) == (
        "Sources/Media/reel.mp4.md",
        "sidecar_of_a_file",
    )
    assert move_of(report, REEL)["new_path"] == "Sources/Media/reel.mp4"
    for name in REEL_FRAMES:
        sidecar = move_of(report, f"{REEL}.frames/{name}.md")
        assert (sidecar["new_path"], sidecar["fixed_because"]) == (
            f"{folder}/{name}.md",
            "sidecar_of_a_file",
        )
        assert move_of(report, f"{REEL}.frames/{name}")["new_path"] == f"{folder}/{name}"


def test_a_page_in_a_notes_folder_that_no_type_owns_keeps_its_path_and_is_reported(vault, capsys):
    report = full_report(capsys, vault)

    assert (
        move_of(report, f"{KB}/Notes/Misc stuff/Idea.md")["new_path"] == "Notes/Misc stuff/Idea.md"
    )
    assert items(report["unregistered_notes_folders"]) == [{"folder": "Misc stuff", "files": 1}]


def test_the_obsidian_config_location_is_reported_for_each_place_it_can_be(tmp_path, capsys):
    """Where the editor's config sits decides whether its settings survive the move."""

    def vault_with(name: str, *, at_root: bool, in_wrapper: bool) -> Path:
        root = tmp_path / name
        build_nested_vault(root)
        if at_root:
            shutil.copytree(root / KB / ".obsidian", root / ".obsidian")
        if not in_wrapper:
            shutil.rmtree(root / KB / ".obsidian")
        return root

    cases = {
        "managed_root": vault_with("wrapper", at_root=False, in_wrapper=True),
        "vault_root": vault_with("root", at_root=True, in_wrapper=False),
        "both": vault_with("both", at_root=True, in_wrapper=True),
        "absent": vault_with("absent", at_root=False, in_wrapper=False),
    }

    reported = {
        name: full_report(capsys, root)["layout"]["obsidian_config"] for name, root in cases.items()
    }

    assert reported == {name: name for name in cases}


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #


def test_each_link_to_a_moved_page_gets_the_shortest_text_that_stays_unique(vault, capsys):
    report = full_report(capsys, vault)

    edits = [
        (edit["page"], edit["old"], edit["new"], edit["full_path"])
        for edit in items(report["link_edits"])
    ]

    assert edits == [
        (SCAN_A, LIVE_PAGE, "Insights/Why churn rose", True),
        (LINK_LAB, LIVE_PAGE, "Insights/Why churn rose", True),
        # The anchor and the alias stay as the author wrote them.
        (LINK_LAB, "Why churn rose#Findings|churn", "Insights/Why churn rose#Findings|churn", True),
        (LINK_LAB, f"{KB}/Scratch", "Scratch", True),
    ]
    assert report["counts"]["links_with_full_path_text"] == 4


def test_links_to_a_markdown_original_and_to_its_md_md_page_each_keep_naming_their_own_file(
    vault, capsys
):
    add_markdown_original_with_links(vault)
    report = full_report(capsys, vault)

    edits = {
        edit["old"]: edit["new"]
        for edit in items(report["link_edits"])
        if edit["page"] == RATE_LINKER
    }

    # `[[rate-notes.md.md]]` already names the page after the move, so it has no edit.
    assert edits == {
        RATE_ORIGINAL: "rate-notes",
        RATE_PAGE: "rate-notes.md.md",
        "Evidence/Pricing/a/rate-notes": "rate-notes",
    }
    assert report["diagnostics"]["link_self_check_failures"] == []
    assert "planned links that failed the self-check" not in report["outcome"]


def test_a_planned_link_text_that_misses_its_page_is_reported_with_what_the_rule_made_of_it(
    vault, capsys, monkeypatch
):
    """The self-check must say which text failed, for which page, and what Obsidian would open instead."""
    real_forms = obsidian_links.link_forms

    def forms_with_an_unpinned_name(index):
        forms = real_forms(index)
        # A short form that two files satisfy: the writer's guarantee is broken for this page only.
        forms["Insights/Why churn rose.md"] = obsidian_links.LinkForm("Why churn rose")
        return forms

    monkeypatch.setattr(obsidian_links, "link_forms", forms_with_an_unpinned_name)
    report = full_report(capsys, vault)

    failures = report["diagnostics"]["link_self_check_failures"]
    assert len(failures) == 2
    assert failures[0] == {
        "page": SCAN_A,
        "text": LIVE_PAGE,
        "target": f"{LIVE_PAGE}.md",
        "new_target": "Insights/Why churn rose.md",
        "planned_text": "Why churn rose",
        "rule_result": "ambiguous",
        "matches": ["Insights/Why churn rose.md", "_trash/Notes/Insights/Why churn rose.md"],
    }
    assert "planned links that failed the self-check: 2" in report["outcome"]


def test_embeds_of_a_moved_image_are_resolved_and_the_path_form_is_rewritten(vault, capsys):
    before = full_report(capsys, vault)["counts"]
    add_attachment_links(vault)
    report = full_report(capsys, vault)
    counts = report["counts"]

    chart_edits = [
        (edit["old"], edit["new"], edit["attachment"])
        for edit in items(report["link_edits"])
        if edit["page"] == CHART_LINKER
    ]
    left = report["links_left_as_written"]["unresolved"]

    # The full-path embed would break after the move, so it gets the image's new short text.
    assert chart_edits == [("Notes/Insights/chart.png", "chart.png", True)]
    assert move_of(report, CHART)["new_path"] == "Insights/chart.png"
    assert not [note for note in items(left) if note["page"] == CHART_LINKER]
    # Three live links on the new page: two embeds of the image and a title that holds a dot.
    assert (counts["links_to_attachments"], counts["link_edits_to_attachments"]) == (2, 1)
    assert counts["links"] == before["links"] + 3
    assert counts["links_unresolved"] == before["links_unresolved"]


def test_a_link_inside_code_and_a_path_written_as_plain_text_are_left_alone(vault, capsys):
    counts = full_report(capsys, vault)["counts"]

    # The link page holds 10 live links; its code span repeats an eleventh that must not count.
    assert counts["links"] == 10
    assert counts["link_edits"] == 4


def test_unresolved_ambiguous_and_folder_links_keep_their_bytes_and_are_counted(vault, capsys):
    report = full_report(capsys, vault)
    left = report["links_left_as_written"]
    counts = report["counts"]

    assert [note["text"] for note in items(left["unresolved"])] == [
        "Missing page",
        "Pricing/a/Scan",
    ]
    assert [note["text"] for note in items(left["ambiguous"])] == ["Scan"]
    assert [note["text"] for note in items(left["to_folders"])] == [f"{KB}/Notes/Procedures/"]
    assert (counts["links_unresolved"], counts["links_ambiguous"], counts["links_to_folders"]) == (
        2,
        1,
        1,
    )
    assert counts["links_unchanged"] == 1
    assert counts["links_to_pages_outside_the_managed_folder"] == 1


def test_a_dead_link_that_a_trailing_path_would_revive_is_named_among_the_alternatives(
    vault, capsys
):
    revived = items(full_report(capsys, vault)["alternatives"]["o7"]["resolved_by_trailing_path"])

    assert revived == [{"page": LINK_LAB, "text": "Pricing/a/Scan"}]


def test_edits_in_an_append_only_area_are_counted_apart(vault, capsys):
    report = full_report(capsys, vault)

    per_page = {row["page"]: row for row in items(report["link_edits_per_page"])}

    assert per_page[SCAN_A] == {"page": SCAN_A, "edits": 1, "append_only_area": True}
    assert per_page[LINK_LAB]["append_only_area"] is False
    assert report["counts"]["link_edit_pages"] == 2
    assert report["counts"]["link_edit_pages_in_append_only_areas"] == 1


# --------------------------------------------------------------------------- #
# Pointer fields
# --------------------------------------------------------------------------- #


def test_pointer_fields_follow_their_files_and_ingested_into_follows_its_page(vault, capsys):
    report = full_report(capsys, vault)

    edits = [
        (edit["page"], edit["field"], edit["old"], edit["new"])
        for edit in items(report["pointer_edits"])
    ]

    assert edits == [
        (
            SCAN_A,
            "evidence_file",
            f"{KB}/Evidence/Pricing/a/scan.pdf",
            "Evidence/Pricing/a/scan.pdf",
        ),
        (
            SCAN_A,
            "ingested_into",
            f"[[{KB}/Notes/Research/Pricing/pricing-experiments]]",
            "[[Pricing experiments]]",
        ),
        (PART_PAGE, "part_of", "../2026-05-02-long-article.md", "../2026-05-02-Long article.md"),
        (
            PART_SOURCE,
            "next_part",
            ".2026-05-02-long-article.parts/part-02.md",
            ".2026-05-02-Long article.parts/part-02.md",
        ),
        (
            f"{KB}/Sources/Media/clip.mp4.frames/frame-001.md",
            "parent_media",
            f"{KB}/Sources/Media/clip.mp4",
            "Sources/Media/clip.mp4",
        ),
    ]
    assert report["counts"]["pointers_left"] == 0
    assert report["diagnostics"]["pointer_failures"] == []


def test_the_paths_of_a_container_binding_and_a_companion_follow_the_files_they_name(vault, capsys):
    add_container_member_and_companion(vault)
    report = full_report(capsys, vault)

    edits = {
        (edit["page"], edit["field"]): (edit["old"], edit["new"])
        for edit in items(report["pointer_edits"])
    }
    member_page = f"{MEMBER_ARTIFACT}.md"
    new_path = {move["current_path"]: move["new_path"] for move in all_moves(report)}

    for key in ("parent_path", "parent_page", "artifact_path"):
        old, new = edits[(member_page, f"container_member.{key}")]
        assert new == new_path[old]
    assert (
        edits[(member_page, "container_member.parent_path")][1]
        == "Evidence/Pricing/bundle/pack.asice"
    )
    assert edits[(member_page, "container_member.artifact_path")][0] == MEMBER_ARTIFACT
    assert edits[(f"{MEMO}.md", "governance_companion.artifact_path")] == (
        MEMO,
        "Evidence/Pricing/b/memo.pdf",
    )
    assert BUNDLE in new_path


# --------------------------------------------------------------------------- #
# Content outside the managed folder
# --------------------------------------------------------------------------- #


def test_links_from_outside_the_managed_folder_are_listed_with_those_that_would_break(
    vault, capsys
):
    report = full_report(capsys, vault)

    links = items(report["outside_content"]["links_into_managed_folder"])

    assert [(link["text"], link["new_target"], link["breaks"]) for link in links] == [
        (LIVE_PAGE, "Insights/Why churn rose.md", True),
        # The trashed page keeps the live page's name, so the bare name is ambiguous after the move.
        ("Why churn rose", "Insights/Why churn rose.md", True),
        ("Pricing experiments", "Research/Pricing/Pricing experiments.md", False),
    ]
    assert report["counts"]["outside_links_that_break"] == 2


def test_a_new_top_level_name_that_an_outside_folder_already_holds_is_reported(vault, capsys):
    report = full_report(capsys, vault)

    assert items(report["top_level_collisions"]) == [
        {"name": "Insights", "outside_entry": "Insights", "files": 6}
    ]
    assert "Needs attention" in report["outcome"]


# --------------------------------------------------------------------------- #
# What each owner choice would change
# --------------------------------------------------------------------------- #


def test_the_alternatives_count_what_each_owner_choice_would_change(vault, capsys):
    alternatives = full_report(capsys, vault)["alternatives"]

    assert items(alternatives["o1_flatten"]["extra_pages_moved"]) == [
        f"{KB}/Notes/Productions/Video/launch-clip.md",
        f"{KB}/Notes/Research/Pricing/pricing-experiments.md",
    ]
    per_folder = alternatives["o3_per_folder"]
    assert (per_folder["suffixed_names_vault_wide"], per_folder["suffixed_names_per_folder"]) == (
        2,
        1,
    )
    drift = alternatives["o5_drift"]
    assert (drift["derived_pages"], drift["name_differs_from_title"]) == (11, 6)
