"""The large-import rehearsal's answer key: what the generator expects, and how a run is judged.

Only the logic that branches is pinned here: how a file's expected disposition follows from its
kind and size, how the byte budget scales to the disk, and how a run is compared with the key.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from cloud_rehearsal import large_import_fixture as fixture
from cloud_rehearsal.large_import_fixture import (
    FILED_ARTIFACT,
    FILED_TEXT,
    GIB,
    HIDDEN_FILE,
    HIDDEN_FOLDER,
    LEFT_BEHIND,
    MIB,
    OVER_CEILING,
    Entry,
    SizingError,
)

CEILING = 2 * MIB
LARGE_MIN = MIB
ID = "00000000-0000-4000-8000-000000000001"


def _layout(folder="notes", *, files=120, total=16 * MIB, include_hidden=True, seed="s"):
    return fixture.layout(folder, seed=seed, files=files, total_bytes=total, ceiling=CEILING,
                          large_min=LARGE_MIN, include_hidden=include_hidden)


def _entry(path, disposition, content=b"x"):
    return Entry(path, len(content), hashlib.sha256(content).hexdigest(), disposition)


# --- the generator's expected dispositions -------------------------------------------------


def test_layout_keeps_the_file_count_and_gives_each_kind_its_disposition():
    specs = _layout(files=120)

    assert len(specs) == 120
    by_path = {spec.path: spec for spec in specs}
    over = [spec for spec in specs if spec.disposition == OVER_CEILING]
    assert len(over) == 1 and over[0].size > CEILING
    large = [spec for spec in specs if spec.path.startswith("archive/backup-")]
    assert large and all(spec.disposition == FILED_ARTIFACT and LARGE_MIN <= spec.size <= CEILING for spec in large)
    assert all(spec.disposition == FILED_TEXT for spec in specs if spec.path.endswith(".md") and not spec.hidden)
    assert all(by_path[path].disposition == HIDDEN_FOLDER for path in by_path if path.startswith(".git/"))
    loose = [spec for spec in specs if spec.hidden == "file"]
    assert loose and all(spec.disposition == HIDDEN_FILE for spec in loose)


def test_a_file_exactly_at_the_ceiling_is_filed_and_one_byte_over_is_not():
    assert fixture.disposition_of("binary", CEILING, ceiling=CEILING, hidden=None, include_hidden=True) == FILED_ARTIFACT
    assert fixture.disposition_of("binary", CEILING + 1, ceiling=CEILING, hidden=None, include_hidden=True) == OVER_CEILING


def test_hidden_files_are_left_behind_unless_the_client_is_told_to_send_them():
    sent = _layout(include_hidden=True)
    kept = _layout(include_hidden=False)

    assert {spec.disposition for spec in sent if spec.hidden} == {HIDDEN_FOLDER, HIDDEN_FILE}
    assert {spec.disposition for spec in kept if spec.hidden} == {LEFT_BEHIND}
    assert [spec.path for spec in sent] == [spec.path for spec in kept]


def test_the_default_notes_folder_has_a_flat_attachment_folder_of_over_five_thousand():
    specs = fixture.layout("notes", seed="s", files=fixture.DEFAULT_FILES - fixture.DEFAULT_FILES // 100,
                           total_bytes=4_800_000_000, ceiling=200 * MIB, large_min=100 * MIB, include_hidden=True)

    flat = [spec for spec in specs if spec.path.startswith("attachments/") and spec.path.count("/") == 1]
    assert len(flat) > 5000
    assert len(specs) == fixture.DEFAULT_FILES - fixture.DEFAULT_FILES // 100
    assert sum(spec.size for spec in specs) == pytest.approx(4_800_000_000, rel=0.02)
    assert len({spec.path for spec in specs}) == len(specs)


def test_a_byte_budget_that_cannot_hold_the_fixed_files_is_refused():
    with pytest.raises(fixture.FixtureError):
        _layout(total=3 * MIB)


# --- the written tree and its manifest -----------------------------------------------------


def test_every_manifest_entry_matches_the_bytes_on_disk_and_a_seed_reproduces_them(tmp_path):
    specs = _layout(files=90)
    first = fixture.write_tree(tmp_path / "one", specs, seed="s")
    second = fixture.write_tree(tmp_path / "two", specs, seed="s")
    other = fixture.write_tree(tmp_path / "three", specs, seed="t")

    for entry in first:
        data = (tmp_path / "one" / entry.path).read_bytes()
        assert (entry.size, entry.sha256) == (len(data), hashlib.sha256(data).hexdigest())
    assert first == second
    assert {e.sha256 for e in first}.isdisjoint({e.sha256 for e in other})
    assert len({e.sha256 for e in first}) == len(first)


def test_notes_link_to_notes_and_attachments_that_exist(tmp_path):
    specs = _layout(files=90)
    entries = fixture.write_tree(tmp_path, specs, seed="s")
    stems = {e.path.rsplit("/", 1)[-1][:-3] for e in entries if e.path.startswith("notes/area-")}
    attachments = {e.path.rsplit("/", 1)[-1] for e in entries if e.path.startswith("attachments/")}

    texts = [(tmp_path / e.path).read_text() for e in entries if e.path.startswith("notes/area-")]
    # A regex here reads only `note_text`'s own fixed `[[x]]` output; this environment cannot import exomem's parser.
    links = {target for text in texts for target in re.findall(r"(?<!!)\[\[([^\]|#]+)\]\]", text)}
    embeds = {target for text in texts for target in re.findall(r"!\[\[([^\]|#]+)\]\]", text)}
    assert links and links <= stems
    assert embeds and embeds <= attachments


def test_a_manifest_round_trips_and_reports_its_own_hash(tmp_path):
    entries = [_entry("a.md", FILED_TEXT, b"a"), _entry("b.dat", FILED_ARTIFACT, b"bb")]
    digest = fixture.write_manifest(tmp_path / "m.jsonl", entries)

    assert fixture.read_manifest(tmp_path / "m.jsonl") == entries
    assert digest == hashlib.sha256((tmp_path / "m.jsonl").read_bytes()).hexdigest()


# --- sizing the data to the disk -----------------------------------------------------------


def _size(free_gib, **extra):
    return fixture.plan_sizes(free_bytes=int(free_gib * GIB), ceiling=200 * MIB, files=70_000,
                              target_bytes=6_000_000_000, **extra)


def test_a_roomy_disk_keeps_the_target_and_a_volume_under_two_copies():
    sizing = _size(120)

    assert not sizing.scaled
    assert sizing.notes_bytes + sizing.media_bytes == pytest.approx(6_000_000_000, rel=0.01)
    assert sizing.volume_gib * GIB < 2 * sizing.notes_bytes
    assert sizing.volume_gib * GIB > sizing.notes_bytes
    assert sizing.proves_one_copy_only


def test_a_tight_disk_scales_bytes_down_and_never_the_file_count():
    roomy, tight = _size(120), _size(25)

    assert tight.scaled and tight.notes_bytes < roomy.notes_bytes
    assert (tight.files_notes, tight.files_media) == (roomy.files_notes, roomy.files_media)
    assert tight.files_notes + tight.files_media == 70_000
    assert fixture.peak_disk_bytes(tight.notes_bytes) <= 25 * GIB


def test_a_disk_too_small_for_the_fixed_files_is_refused_with_the_shortfall():
    with pytest.raises(SizingError, match="free"):
        _size(2)


def test_scaled_data_that_no_longer_exceeds_one_copy_says_it_proves_nothing():
    sizing = _size(14)

    assert sizing.scaled and not sizing.proves_one_copy_only
    assert "two copies" in sizing.note
    assert sizing.volume_gib >= 4


# --- judging a run against the key ---------------------------------------------------------


KEY = [
    _entry("n/a.md", FILED_TEXT, b"note"),
    _entry("att/b.dat", FILED_ARTIFACT, b"bin1"),
    _entry("att/c.dat", FILED_ARTIFACT, b"bin2"),
    _entry("big.dat", OVER_CEILING, b"huge"),
    _entry(".git/HEAD", HIDDEN_FOLDER, b"ref"),
    _entry(".DS_Store", HIDDEN_FILE, b"ds"),
    _entry("left/.x", LEFT_BEHIND, b"x"),
]


def _sources(entries=KEY, *, drop=(), change=()):
    found = {}
    for entry in entries:
        if entry.disposition in (FILED_ARTIFACT, FILED_TEXT) and entry.path not in drop:
            found[f"_Imports/{ID}/{entry.path}"] = ["0" * 64 if entry.path in change else entry.sha256]
    return found


def test_every_filed_file_needs_a_source_with_its_hash_and_nothing_else_does():
    assert fixture.check_sources(KEY, ID, _sources()) == []

    missing = fixture.check_sources(KEY, ID, _sources(drop={"att/c.dat"}))
    wrong = fixture.check_sources(KEY, ID, _sources(change={"n/a.md"}))
    assert len(missing) == 1 and "att/c.dat" in missing[0]
    assert len(wrong) == 1 and "n/a.md" in wrong[0]


def test_what_stays_staged_is_exactly_the_text_the_over_ceiling_file_and_the_hidden_files():
    leftover = fixture.expected_leftover(KEY)
    assert set(leftover) == {"n/a.md", "big.dat", ".git/HEAD", ".DS_Store"}
    assert fixture.check_stage(KEY, leftover) == []

    unconsumed = {**leftover, "att/b.dat": (4, hashlib.sha256(b"bin1").hexdigest())}
    lost_text = {path: value for path, value in leftover.items() if path != "n/a.md"}
    altered = {**leftover, "big.dat": (4, "0" * 64)}
    assert "att/b.dat" in fixture.check_stage(KEY, unconsumed)[0]
    assert "n/a.md" in fixture.check_stage(KEY, lost_text)[0]
    assert "big.dat" in fixture.check_stage(KEY, altered)[0]


def test_files_the_client_leaves_behind_are_never_expected_in_the_stage():
    assert "left/.x" not in fixture.expected_stage(KEY)
    assert fixture.expected_by_reason(KEY) == {"text": 1, "over_ceiling": 1, "ineligible": 1, "outside_run": 1}


def _members(entries=KEY, *, drop=(), change=(), add=(), twice=()):
    members = [{"path": e.path, "size": e.size, "sha256": "0" * 64 if e.path in change else e.sha256}
               for e in entries if e.disposition != LEFT_BEHIND and e.path not in drop]
    members += [{"path": path, "size": 1, "sha256": "1" * 64} for path in add]
    members += [dict(m) for m in members if m["path"] in twice]
    return members


def test_the_receipt_lists_every_sent_file_with_its_hash_and_nothing_the_client_kept():
    assert fixture.check_members(KEY, _members()) == []

    assert "att/c.dat" in fixture.check_members(KEY, _members(drop={"att/c.dat"}))[0]
    assert "n/a.md" in fixture.check_members(KEY, _members(change={"n/a.md"}))[0]
    assert "left/.x" in fixture.check_members(KEY, _members(add={"left/.x"}))[0]
    assert "listed twice" in fixture.check_members(KEY, _members(twice={"big.dat"}))[0]


def _page(**reasons):
    by_reason = {reason: {"files": files, "bytes": 1} for reason, files in reasons.items()}
    return {"files": sum(reasons.values()), "by_reason": by_reason}


def test_the_last_page_residual_must_equal_the_key_by_reason_and_leave_nothing_unfiled():
    assert fixture.check_residual(KEY, _page(ineligible=1, over_ceiling=1, text=1, outside_run=1)) == []

    unconsumed = _page(ineligible=1, over_ceiling=1, text=1, kept=1, outside_run=1)
    miscounted = _page(ineligible=1, over_ceiling=1, outside_run=1)
    extra = _page(ineligible=1, over_ceiling=1, text=1, outside_run=4)
    assert any("kept" in problem for problem in fixture.check_residual(KEY, unconsumed))
    assert any("text" in problem for problem in fixture.check_residual(KEY, miscounted))
    assert any("left in the stage" in problem for problem in fixture.check_residual(KEY, extra))


def test_consumed_must_equal_the_artifacts_the_key_says_were_filed():
    assert fixture.count_consumable(KEY) == 2
    assert fixture.check_consumed(KEY, 2) == []
    assert "consumed 1" in fixture.check_consumed(KEY, 1)[0]
