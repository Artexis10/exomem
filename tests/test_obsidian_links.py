"""Obsidian's link rule and the shortest link text, as pure functions over a basename index."""

from __future__ import annotations

import time
import unicodedata

import pytest

from exomem import obsidian_links as links
from exomem.obsidian_links import Resolution

SCANS = [
    "Evidence/alpha/receipts/Scan.md",
    "Evidence/beta/receipts/Scan.md",
    "Evidence/beta/notes/Scan.md",
    "Insights/Why churn rose in Q3.md",
]


def test_unique_trailing_path_resolves_to_its_file():
    """A path link that Exomem's resolver leaves unresolved today resolves in Obsidian by its tail."""
    match = links.resolve_link("alpha/receipts/Scan", links.build_index(SCANS))

    assert match.outcome is Resolution.RESOLVED
    assert match.path == "Evidence/alpha/receipts/Scan.md"


def test_shared_trailing_path_is_ambiguous_and_names_both_files():
    """A tail that two files satisfy must not pick a file: the writer would link the wrong page."""
    index = links.build_index(SCANS)

    shared = links.resolve_link("receipts/Scan", index)
    bare = links.resolve_link("Scan", index)

    assert shared.outcome is Resolution.AMBIGUOUS
    assert shared.paths == ("Evidence/alpha/receipts/Scan.md", "Evidence/beta/receipts/Scan.md")
    assert bare.outcome is Resolution.AMBIGUOUS
    assert len(bare.paths) == 3


def test_text_that_names_no_file_is_unresolved():
    index = links.build_index(SCANS)

    assert links.resolve_link("Missing", index).outcome is Resolution.UNRESOLVED
    assert links.resolve_link("gamma/receipts/Scan", index).outcome is Resolution.UNRESOLVED
    # `Evidence/Scan` is a prefix pair, not a tail of any path.
    assert links.resolve_link("Evidence/Scan", index).outcome is Resolution.UNRESOLVED


def test_name_matches_without_regard_to_case_unicode_form_or_md_suffix():
    """Obsidian compares names loosely; a strict match would call a working link unresolved."""
    composed = "Entities/Café notes.md"
    index = links.build_index([composed, "Evidence/Scan.md"])
    decomposed = unicodedata.normalize("NFD", "café NOTES")

    assert links.resolve_link(decomposed, index).path == composed
    assert links.resolve_link("evidence/scan.MD", index).path == "Evidence/Scan.md"


def test_unique_basename_links_by_name_alone():
    index = links.build_index(SCANS)

    assert links.link_form("Insights/Why churn rose in Q3.md", index) == links.LinkForm(
        "Why churn rose in Q3"
    )


def test_link_form_takes_the_shortest_unique_trailing_path():
    index = links.build_index(SCANS)

    assert links.link_form("Evidence/alpha/receipts/Scan.md", index).text == "alpha/receipts/Scan"
    assert links.link_form("Evidence/beta/notes/Scan.md", index).text == "notes/Scan"
    # `receipts/Scan` names two files, so the beta receipt needs its group as well.
    assert links.link_form("Evidence/beta/receipts/Scan.md", index).text == "beta/receipts/Scan"


def test_every_link_form_resolves_to_exactly_its_own_file():
    """The writer's promise: a link it emits never resolves to another file, whatever the homonyms."""
    paths = [
        *SCANS,
        "Scan.md",
        "Sources/Articles/2026-05-02-Pricing basics.md",
        "_trash/Insights/Why churn rose in Q3.md",
        "Entities/People/Scan.md",
    ]
    index = links.build_index(paths)

    forms = links.link_forms(index)

    assert set(forms) == set(paths)
    for path, form in forms.items():
        assert form == links.link_form(path, index), path
        assert links.resolve_link(form.text, index).path == path, path
    # No trailing path is unique for the root-level homonym, and for a page whose trashed copy
    # sits under the same folder name: each takes its full vault path.
    assert sorted(path for path, form in forms.items() if form.full_path) == [
        "Insights/Why churn rose in Q3.md",
        "Scan.md",
    ]


def test_forms_for_a_basename_group_of_twenty_thousand_files_arrive_in_seconds():
    """A planner that read the whole group per file would take minutes at this size; a single pass takes under a second."""
    paths = [f"Evidence/batch-{number}/Scan.md" for number in range(20_000)]
    index = links.build_index(paths)

    started = time.perf_counter()
    forms = links.link_forms(index)
    elapsed = time.perf_counter() - started

    assert forms["Evidence/batch-7/Scan.md"] == links.LinkForm("batch-7/Scan")
    assert len({form.text for form in forms.values()}) == len(paths)
    assert elapsed < 10  # measured near 0.1 s; the margin absorbs a loaded host


def test_root_level_homonym_takes_its_full_path_and_still_resolves_to_itself():
    """`[[Name]]` names both files by the strict rule, but it is also the root file's full path: exact match wins."""
    index = links.build_index(["Name.md", "Sub/Name.md"])

    root_form = links.link_form("Name.md", index)

    assert root_form == links.LinkForm("Name", full_path=True)
    assert links.link_form("Sub/Name.md", index) == links.LinkForm("Sub/Name")
    assert links.resolve_link(root_form.text, index).path == "Name.md"
    assert links.resolve_link("Sub/Name", index).path == "Sub/Name.md"


def test_trashed_copy_under_the_same_folder_name_leaves_the_live_page_its_full_path():
    """A path text that a trashed copy also ends with is ambiguous by suffix, so the live page needs its exact path."""
    index = links.build_index(["Insights/X.md", "_trash/Insights/X.md"])

    live = links.link_form("Insights/X.md", index)
    trashed = links.link_form("_trash/Insights/X.md", index)

    assert live == links.LinkForm("Insights/X", full_path=True)
    assert trashed == links.LinkForm("_trash/Insights/X")
    assert links.resolve_link(live.text, index).path == "Insights/X.md"
    assert links.resolve_link(trashed.text, index).path == "_trash/Insights/X.md"


def test_the_exact_vault_path_wins_over_other_files_that_end_with_the_same_text():
    """Without exact-first, a text that is a file's full path would turn ambiguous the day a deeper homonym appears."""
    index = links.build_index(["Insights/X.md", "Archive/Insights/X.md", "Elsewhere/Insights/X.md"])

    exact = links.resolve_link("Insights/X", index)
    suffix_only = links.resolve_link("Archive/Insights/X", index)
    bare = links.resolve_link("X", index)

    assert exact.path == "Insights/X.md"
    assert suffix_only.path == "Archive/Insights/X.md"
    assert bare.outcome is Resolution.AMBIGUOUS and len(bare.paths) == 3


def test_trashed_homonym_counts_but_a_dot_folder_does_not():
    """Obsidian lists machinery folders, so a trashed page can capture a bare name; a hidden folder cannot."""
    index = links.build_index(
        [
            "Insights/Budget.md",
            "_trash/Budget.md",
            ".obsidian/plugins/Budget.md",
            "Planning/.cache/Budget.md",
        ]
    )

    assert links.resolve_link("Budget", index).outcome is Resolution.AMBIGUOUS
    assert links.link_form("Insights/Budget.md", index) == links.LinkForm("Insights/Budget")


def test_link_form_of_a_file_missing_from_the_index_fails_loudly():
    """A planner that forgot to index a target would otherwise emit a form for a file Obsidian cannot see."""
    with pytest.raises(ValueError):
        links.link_form("Insights/Lost.md", links.build_index(SCANS))


def test_anchor_and_alias_keep_their_bytes_when_the_page_text_changes():
    page = "Why churn rose in Q3"
    expected = {
        "Knowledge Base/Notes/Insights/slug": page,
        "Knowledge Base/Notes/Insights/slug#Findings": f"{page}#Findings",
        "Knowledge Base/Notes/Insights/slug|shown text": f"{page}|shown text",
        "Knowledge Base/Notes/Insights/slug#Findings|shown text": f"{page}#Findings|shown text",
        "Knowledge Base/Notes/Insights/slug | shown text": f"{page}| shown text",
    }
    for inner, text in expected.items():
        assert links.link_text(inner, page) == text, inner
