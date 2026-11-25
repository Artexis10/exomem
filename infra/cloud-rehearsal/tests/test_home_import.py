"""The Home-import rehearsal's own decisions: the fixture it sends and every check it makes against the manifest.

Pure logic over a fixture written to a temporary folder and Source pages that the tests write. The run itself
needs a K3s cell and is exercised by the `home-import` workflow mode.
"""

from __future__ import annotations

import os

import pytest

from cloud_rehearsal import home_import
from cloud_rehearsal.home_import import FAIL, INCOMPLETE, PASS, Manifest, Member


@pytest.fixture
def manifest() -> Manifest:
    return Manifest(home_import.fixture_members(bulk=3))


def _front(member: Member, **changes) -> dict:
    """The front matter that the cell writes for a faithfully filed member."""
    front = {"imported_from": f"_Imports/x/{member.path}", "collection_path": member.path,
             "original_sha256": member.sha256, "original_bytes": len(member.data),
             "file_modified": home_import.modified_text(member.mtime),
             "origin_label": home_import.ORIGIN_LABEL, "upload_browser": home_import.ORIGIN_BROWSER}
    front.update(changes)
    return front


def _faithful(manifest: Manifest) -> dict[str, list[dict]]:
    return {member.path: [_front(member)] for member in manifest.filed}


# --- the fixture -----------------------------------------------------------------------------


def test_the_tree_holds_each_shape_with_unique_bytes_and_its_own_file_time(tmp_path, manifest) -> None:
    """Two files with the same bytes would file as one Source and a skipped duplicate: a false failure."""
    home_import.build_fixture(tmp_path, bulk=3)

    on_disk = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
    assert on_disk == {member.path for member in manifest.members}
    nonempty = [member.sha256 for member in manifest.members if member.data]
    assert len(set(nonempty)) == len(nonempty)
    assert len({member.mtime for member in manifest.members}) == len(manifest.members)
    assert all(int(os.stat(tmp_path / m.path).st_mtime) == m.mtime for m in manifest.members)
    with pytest.raises(UnicodeDecodeError):
        (tmp_path / "latin1.txt").read_bytes().decode("utf-8")
    assert (tmp_path / "empty.txt").stat().st_size == 0
    assert {m.path for m in manifest.left_by_cli} == {".hidden.md"}
    assert any(m.path.startswith("archive-export/") for m in manifest.filed)
    assert any(m.path.startswith("notes/nested/") for m in manifest.filed)


# --- the Sources ----------------------------------------------------------------------------


def test_a_faithful_set_of_sources_passes(manifest) -> None:
    assert home_import.check_sources(manifest, _faithful(manifest)) == []


def test_a_file_filed_twice_across_the_restart_is_reported(manifest) -> None:
    """The exactly-once claim: a call replayed under a new identity files a member again."""
    fronts = _faithful(manifest)
    twice = manifest.filed[0]
    fronts[twice.path] = [_front(twice), _front(twice)]

    assert home_import.check_sources(manifest, fronts) == [f"{twice.path}: 2 Sources, expected one"]


def test_a_file_the_fixture_says_stays_staged_but_has_a_source_is_reported(manifest) -> None:
    fronts = _faithful(manifest)
    fronts["empty.txt"] = [{"imported_from": "_Imports/x/empty.txt", "collection_path": "empty.txt"}]

    assert home_import.check_sources(manifest, fronts) == ["empty.txt: has a Source, but the fixture says it is not eligible"]


def test_a_source_that_lost_its_file_time_or_origin_is_reported(manifest) -> None:
    fronts = _faithful(manifest)
    first, second = manifest.filed[:2]
    fronts[first.path] = [{k: v for k, v in _front(first).items() if k != "file_modified"}]
    fronts[second.path] = [_front(second, origin_label="Other", upload_device="phone")]

    problems = home_import.check_sources(manifest, fronts)

    assert any(p.startswith(f"{first.path}: file_modified is None") for p in problems)
    assert any(p.startswith(f"{second.path}: origin_label is 'Other'") for p in problems)
    assert any(p.startswith(f"{second.path}: upload_device") for p in problems)


def test_source_fronts_reads_a_page_with_a_quoted_time_and_ignores_pages_with_no_origin(tmp_path, manifest) -> None:
    member = manifest.filed[0]
    sources = tmp_path / "Knowledge Base" / "Sources" / "Imported"
    sources.mkdir(parents=True)
    (sources / "a.md").write_text(
        f"---\ntype: source\nimported_from: _Imports/x/{member.path}\ncollection_path: {member.path}\n"
        f'file_modified: "{home_import.modified_text(member.mtime)}"\n---\n\n# a\n', encoding="utf-8")
    (sources / "index.md").write_text("---\ntype: index\n---\n\n# Sources\n", encoding="utf-8")

    fronts = home_import.source_fronts(tmp_path)

    assert list(fronts) == [member.path]
    assert fronts[member.path][0]["file_modified"] == home_import.modified_text(member.mtime)


# --- the left files and the counts ------------------------------------------------------------

CLI_LINES = ["  hidden: 1 file, 52 B (pass --include-hidden to take them)"]


def _left(manifest, **changes) -> list[dict]:
    return [{"path": m.path, "bytes": len(m.data), "code": m.reason, **changes} for m in manifest.left_by_cell]


def test_a_faithful_leftover_report_passes(manifest) -> None:
    assert home_import.check_leftovers(manifest, _left(manifest), ["empty.txt"], CLI_LINES) == []


def test_a_zero_byte_file_that_the_cell_does_not_list_is_reported(manifest) -> None:
    problems = home_import.check_leftovers(manifest, [], ["empty.txt"], CLI_LINES)

    assert problems == ["empty.txt: the cell does not list it as left"]


def test_a_hidden_file_that_reached_the_stage_or_has_no_cli_reason_is_reported(manifest) -> None:
    problems = home_import.check_leftovers(manifest, _left(manifest), ["empty.txt", ".hidden.md"], [])

    assert any(p.startswith(".hidden.md: reached the stage") for p in problems)
    assert any(p.startswith(".hidden.md: the CLI's output has no left-behind line") for p in problems)
    assert any("still holds files" in p for p in problems)


def test_the_cli_lines_under_left_behind_are_taken_from_its_log() -> None:
    log = ("Selected 40 files, 4.1 KB.\nLeft behind (each entry counts under the first reason that applies):\n"
           "  hidden: 1 file, 52 B (pass --include-hidden to take them)\nUploading window 1\n")

    assert home_import.left_behind_lines(log) == ["  hidden: 1 file, 52 B (pass --include-hidden to take them)"]


def _view(manifest, **progress) -> dict:
    return {"state": "done", "stop": None, "needs": [], "progress": {
        "total": len(manifest.filed) + len(manifest.left_by_cell), "filed": len(manifest.filed), "skipped_duplicates": 0,
        "failed": 0, "left_out": len(manifest.left_by_cell), "remaining": 0, **progress}}


def _listing(manifest, unfiled=None) -> dict:
    count = len(manifest.left_by_cell) if unfiled is None else unfiled
    return {"adopted_files": len(manifest.filed), "import_windows": {"windows": [{"unfiled": {"count": count}}]}}


def test_counts_that_add_up_to_the_tree_pass(manifest) -> None:
    assert home_import.check_counts(manifest, _view(manifest), _listing(manifest)) == []


def test_a_view_that_counts_one_file_too_few_does_not_add_up_to_the_tree(manifest) -> None:
    short = len(manifest.filed) - 1

    problems = home_import.check_counts(manifest, _view(manifest, filed=short), _listing(manifest))

    assert any("filed is" in p for p in problems)
    assert any("do not add up" in p for p in problems)


def test_a_view_with_a_wrong_total_or_a_zero_rate_for_filed_work_is_reported(manifest) -> None:
    view = {**_view(manifest, total=len(manifest.members)), "rate": {"files_per_minute": 0.0, "over_calls": 2}}

    problems = home_import.check_counts(manifest, view, _listing(manifest))

    assert any(p.startswith("the filing view's total is") for p in problems)
    assert any("reports a rate of 0.0" in p for p in problems)


def test_the_left_out_list_of_the_filing_view_is_the_files_that_stay_staged_with_their_reasons(manifest) -> None:
    faithful = {"by_reason": {"unselected": 1}, "items": [{"path": "empty.txt", "code": "unselected"}], "truncated": False}

    assert home_import.check_left_out(manifest, faithful) == []
    assert len(home_import.check_left_out(manifest, {**faithful, "items": [], "truncated": True})) == 2
    assert home_import.check_left_out(manifest, None) == ["the filing view of a done stage has no left_out, found None"]


def test_a_stage_still_parked_is_not_done(manifest) -> None:
    view = {**_view(manifest), "state": "parked", "stop": {"code": "IMPORT_FILING_REPLY_UNREADABLE"}}

    problems = home_import.check_counts(manifest, view, _listing(manifest))

    assert any("expected 'done'" in p for p in problems) and any("names a stop" in p for p in problems)


# --- the origin, the client and the verdict ------------------------------------------------------


def test_a_series_file_that_does_not_ask_the_cell_to_file_or_lacks_the_origin_is_reported() -> None:
    asked = {"filing": "cell", "cell_value": "cell", "origin": {"label": home_import.ORIGIN_LABEL, "browser_family": "cli"}}

    assert home_import.check_origin(asked) == []
    assert home_import.check_origin({**asked, "filing": None}) == ["the series file's filing is None, expected 'cell'"]
    assert home_import.check_origin({**asked, "origin": None}) == ["the series file has no origin, found None"]
    assert len(home_import.check_origin({**asked, "origin": {"label": "x", "browser_family": "safari"}})) == 2


def test_a_file_call_from_the_harness_client_is_reported_and_status_reads_are_not() -> None:
    assert home_import.check_no_client_file_call(["inspect_memory.filing", "inspect_memory.listing"]) == []
    assert home_import.check_no_client_file_call(["inspect_memory.filing", "manage_memory.file"]) == [
        "the harness client called manage_memory.file, which can file"]


def test_a_restart_that_did_not_land_in_filing_is_incomplete_and_never_a_pass() -> None:
    assert home_import.verdict([], interrupted=True) == PASS
    assert home_import.verdict([], interrupted=False) == INCOMPLETE
    assert home_import.verdict(["wrong"], interrupted=True) == FAIL
