"""The raw-data import rehearsal's own decisions: per-item parity with local, and the pre-existing page check.

Pure logic over small invented keys. The run itself needs a K3s cell and is exercised by the `raw-import` workflow.
"""

from __future__ import annotations

from cloud_rehearsal import raw_import
from cloud_rehearsal.raw_import import FAIL, INCOMPLETE, PASS


def _key(*note_paths):
    return {"items": [{"kind": "note", "path": path, "lang": "en", "nfd_filename": False, "links": [], "embeds": []}
                      for path in note_paths]}


def _arm(**pages):
    """A scored arm's `per_item` block: key path -> number of pages it came out as."""
    return {"pages_per_item": pages, "junk_pages": {}, "links_kept": {}, "embeds_kept": {}}


def _verdict(cloud, local):
    groups = raw_import.compare_arms(_key("a.md", "b.md"), cloud, local)
    return raw_import.target_verdict(groups), groups["notes"]


# --- parity ----------------------------------------------------------------------------------


def test_a_note_that_local_kept_and_cloud_did_not_is_a_loss():
    verdict, notes = _verdict(_arm(**{"a.md": 1, "b.md": 0}), _arm(**{"a.md": 1, "b.md": 1}))
    assert verdict == FAIL
    assert notes["loss"] == {"count": 1, "listed": ["b.md"]}


def test_a_note_that_only_cloud_kept_is_reported_but_does_not_fail_the_run():
    verdict, notes = _verdict(_arm(**{"a.md": 1, "b.md": 1}), _arm(**{"a.md": 1, "b.md": 0}))
    assert verdict == PASS
    assert notes["cloud_extra"] == {"count": 1, "listed": ["b.md"]}


def test_an_arm_that_was_not_scored_leaves_every_item_unknown_and_never_passes():
    verdict, notes = _verdict(None, _arm(**{"a.md": 1, "b.md": 1}))
    assert verdict == INCOMPLETE
    assert notes["unknown"]["count"] == 2


def test_a_loss_outranks_an_unknown_in_the_same_target():
    groups = raw_import.compare_arms(_key("a.md", "b.md"), _arm(**{"a.md": 0}), _arm(**{"a.md": 1, "b.md": 1}))
    assert raw_import.target_verdict(groups) == FAIL


def test_a_second_page_for_an_item_that_local_filed_once_is_a_loss():
    groups = raw_import.compare_arms(_key("a.md"), _arm(**{"a.md": 2}), _arm(**{"a.md": 1}))
    assert groups["no_duplicate_pages"]["loss"]["count"] == 1
    assert groups["notes"]["loss"]["count"] == 0


def test_a_target_that_was_never_judged_keeps_the_run_from_passing():
    assert raw_import.worst([PASS, None]) == INCOMPLETE
    assert raw_import.worst([PASS, INCOMPLETE, FAIL]) == FAIL


# --- the vault that already held pages -------------------------------------------------------


def test_untouched_pre_existing_pages_pass_even_with_new_pages_beside_them():
    before = {"Knowledge Base/old.md": "h1"}
    after = {**before, "Knowledge Base/Sources/new.md": "h2"}
    assert raw_import.check_existing_pages(before, after) == []


def test_a_changed_missing_or_copied_pre_existing_page_each_gets_its_own_problem():
    before = {"Knowledge Base/a.md": "h1", "Knowledge Base/b.md": "h2", "Knowledge Base/c.md": "h3"}
    after = {"Knowledge Base/a.md": "changed", "Knowledge Base/c.md": "h3", "Knowledge Base/Sources/c copy.md": "h3"}
    assert raw_import.check_existing_pages(before, after) == [
        "changed: Knowledge Base/a.md",
        "missing: Knowledge Base/b.md",
        "duplicated: Knowledge Base/Sources/c copy.md repeats Knowledge Base/c.md",
    ]


def test_a_failed_page_check_fails_and_an_unrun_one_is_incomplete():
    groups = raw_import.compare_arms(_key("a.md"), _arm(**{"a.md": 1}), _arm(**{"a.md": 1}))
    assert raw_import.target_verdict(groups, ["changed: Knowledge Base/a.md"]) == FAIL
    assert raw_import.target_verdict(groups, None) == INCOMPLETE


# --- where the product wrote the originals ---------------------------------------------------


def test_the_staged_folder_in_front_of_each_original_is_read_from_the_pages_even_for_decomposed_names():
    nfd_name = "résumé.md"
    values = [f"_Imports/abc/notes/{nfd_name}", "_Imports/abc/ideas/x.md", "unrelated/other.md"]
    assert raw_import.imported_from_prefix(values, ["notes/résumé.md", "ideas/x.md"]) == "_Imports/abc"
    assert raw_import.imported_from_prefix(["other.md"], ["ideas/x.md"]) is None
