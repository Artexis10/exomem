"""Stored originals are verified in the background, restored only from a matching copy, and reported.

Each test runs the real walk and pass on a temporary vault and injects the damage on disk.
The data is invented.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import sqlite3
import threading
import zipfile
from pathlib import Path

import pytest
from test_media_store import _offload
from test_media_store import selected_store as selected_store

from exomem import (
    adopt,
    archive_members,
    attention,
    foreground_priority,
    lexstore,
    local_import,
    media_store,
    original_fixity,
    preserve,
)
from exomem.governance.principal import library_scope

TRANSCRIPT = b"Field recording, side A: the river gauge read 2.4 m at dawn.\n"
DAMAGED = b"Field recording, side A: the river gauge read 9.9 m at dawn.\n"  # same size, other bytes


@pytest.fixture(autouse=True)
def _fixity_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_ORIGINAL_FIXITY", raising=False)
    # Local import journals live in the user's state folder; a test reads only its own.
    monkeypatch.setattr(local_import, "user_state_dir", lambda name: str(tmp_path / "user-state"))


def _stored(vault: Path, category: str, data: bytes = TRANSCRIPT) -> Path:
    with library_scope():
        captured = preserve.preserve(vault, scope="Field", category=category, filename="side-a.txt",
                                     content=data.decode())
    return vault / captured.path


def _member_blob(vault: Path, data: bytes = TRANSCRIPT) -> Path:
    """Preserve an archive kept as members whose one member holds `data`; return its pool blob."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("export/side-a.txt", data)
    archive_members.preserve_members(
        vault, guard=contextlib.nullcontext, scope="Device", category="Exports", filename="export.zip",
        stream=io.BytesIO(buffer.getvalue()), max_bytes=1024 * 1024, verified="upload",
    )
    family = vault / "Knowledge Base" / "Evidence" / "Device" / "Exports"
    return next(family.rglob(f"{hashlib.sha256(data).hexdigest()}.gz"))


def _text_source(vault: Path) -> Path:
    """Write an imported text Source page that fences its original; return the page."""
    text = "Meeting notes from the gauge survey.\nThe river rose overnight.\n"
    data = text.encode()
    planned = {"title": "Gauge survey", "original_path": "Notes/gauge-survey.md",
               "original_sha256": hashlib.sha256(data).hexdigest(), "original_bytes": len(data)}
    page = vault / "Knowledge Base" / "Sources" / "Gauge survey.md"
    page.write_text(adopt.render_text_source(planned, text, date_iso="2026-11-01", collection=None))
    return page


def _trash(vault: Path) -> dict[str, bytes]:
    root = vault / "Knowledge Base" / "_trash"
    return {path.name: path.read_bytes() for path in root.rglob("*") if path.is_file()} if root.is_dir() else {}


def _findings(vault: Path) -> list:
    findings, coverage = original_fixity.attention_findings(vault)
    assert coverage is None
    return findings


def test_a_pass_restores_a_damaged_original_from_a_matching_copy_and_keeps_the_changed_bytes_in_trash(
    vault: Path,
) -> None:
    original = _stored(vault, "Recordings")
    _member_blob(vault)  # an archive member with the same bytes: the copy the restore uses
    original_fixity.walk(vault)
    # The original, the archive's manifest (a stored original itself) and the member's pool blob.
    assert original_fixity.run_pass(vault).outcomes == {"verified": 3}

    original.write_bytes(DAMAGED)
    result = original_fixity.run_pass(vault, budget=0)  # no re-hash budget: only the changed file is read

    assert result.outcomes == {"restored": 1}
    assert original.read_bytes() == TRANSCRIPT
    trashed = _trash(vault)
    payload = next(name for name in trashed if not name.endswith(".meta.json"))
    assert trashed[payload] == DAMAGED
    metadata = json.loads(trashed[payload + ".meta.json"])
    assert metadata["original_path"] == original.relative_to(vault).as_posix()
    assert (metadata["reason"], metadata["cause"]) == ("original_fixity", "edited")
    assert metadata["expected_sha256"] == hashlib.sha256(TRANSCRIPT).hexdigest()
    assert metadata["found_sha256"] == hashlib.sha256(DAMAGED).hexdigest()
    [finding] = _findings(vault)
    assert (finding.severity, finding.meta["state"]) == ("info", "restored")
    assert [copy["source"] for copy in finding.meta["route"]] == ["pool_blob"]
    # A restore needs no operator: Cloud readiness counts only what still needs the owner.
    assert original_fixity.readiness(vault) == {"state": "measured", "unresolved": 0}


def test_a_damaged_pool_blob_is_restored_from_a_stored_original_as_the_same_blob(vault: Path) -> None:
    blob = _member_blob(vault)
    _stored(vault, "Recordings")
    before = blob.read_bytes()
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)

    blob.write_bytes(gzip.compress(DAMAGED))
    assert original_fixity.run_pass(vault, budget=0).outcomes == {"restored": 1}

    # Equal members give equal blobs, so the restore writes back exactly the bytes the import wrote.
    assert blob.read_bytes() == before


def test_a_local_import_s_outside_file_restores_a_damaged_original(vault: Path, tmp_path: Path) -> None:
    folder = tmp_path / "Field disk"
    folder.mkdir()
    (folder / "side-a.txt").write_bytes(TRANSCRIPT)
    assert local_import.stage(folder, vault)["ok"]
    original = _stored(vault, "Recordings")
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)

    original.write_bytes(DAMAGED)
    assert original_fixity.run_pass(vault).outcomes == {"restored": 1}

    assert original.read_bytes() == TRANSCRIPT
    [finding] = _findings(vault)
    assert finding.meta["route"] == [{"source": "local_import", "path": str(folder / "side-a.txt")}]


def test_damage_without_a_matching_copy_changes_nothing_and_asks_the_owner(vault: Path) -> None:
    original = _stored(vault, "Recordings")
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)

    original.write_bytes(DAMAGED)
    assert original_fixity.run_pass(vault).outcomes == {"corrupt_no_copy": 1}

    assert original.read_bytes() == DAMAGED
    assert _trash(vault) == {}
    [finding] = _findings(vault)
    assert (finding.severity, finding.meta["state"], finding.meta["route"]) == ("error", "corrupt_no_copy", [])
    # The owner sees it without naming the category.
    assert attention.attention(vault, record_surfacing=False).summary.get("original_fixity") == 1
    assert original_fixity.readiness(vault) == {"state": "measured", "unresolved": 1}


def test_a_deleted_original_is_reported_with_its_copies_and_never_restored(vault: Path) -> None:
    original = _stored(vault, "Recordings")
    spare = _stored(vault, "Spares")
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)

    original.unlink()
    assert original_fixity.run_pass(vault, budget=0).outcomes == {"missing": 1}

    assert not original.exists()  # the deletion may be deliberate
    [finding] = _findings(vault)
    assert finding.meta["route"] == [{"source": "stored_original", "path": spare.relative_to(vault).as_posix()}]
    assert original_fixity.readiness(vault) == {"state": "measured", "unresolved": 1}


def test_an_evicted_original_is_never_downloaded(vault: Path, selected_store, monkeypatch) -> None:
    original = _stored(vault, "Recordings")
    _offload(vault, original)
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    assert media_store.evict(vault, original)
    opened: list[Path] = []
    real_store_for = media_store.store_for
    # Every read of the store, a download included, starts by opening it.
    monkeypatch.setattr(media_store, "store_for", lambda vault: (opened.append(vault), real_store_for(vault))[1])

    assert original_fixity.run_pass(vault).outcomes == {"evicted": 1}
    assert original_fixity.run_pass(vault).outcomes == {"evicted": 1}

    assert opened == []
    assert not original.exists()
    assert _findings(vault) == []


def test_a_pass_rehashes_unchanged_originals_only_within_its_budget_oldest_first(vault: Path) -> None:
    for category in ("North", "South", "East"):
        _stored(vault, category, TRANSCRIPT.replace(b"side A", category.encode().ljust(6)))
    original_fixity.walk(vault)

    def verified_at() -> dict[str, float | None]:
        with contextlib.closing(sqlite3.connect(original_fixity.table_path(vault))) as table:
            return dict(table.execute("SELECT path, verified_at FROM originals"))

    first = original_fixity.run_pass(vault, budget=2 * len(TRANSCRIPT))
    after_first = verified_at()
    second = original_fixity.run_pass(vault, budget=2 * len(TRANSCRIPT))
    after_second = verified_at()

    assert (first.rehashed_bytes, first.outcomes) == (2 * len(TRANSCRIPT), {"verified": 2})
    assert (second.rehashed_bytes, second.outcomes) == (2 * len(TRANSCRIPT), {"verified": 2})
    # The original the first pass left unverified goes first, then the oldest verification.
    [left] = [path for path, when in after_first.items() if when is None]
    oldest, newest = sorted((path for path in after_first if path != left), key=lambda path: after_first[path])
    assert after_second[newest] == after_first[newest]
    assert after_second[left] is not None and after_second[oldest] > after_first[oldest]


def test_a_changed_original_is_hashed_even_when_the_budget_is_spent(vault: Path) -> None:
    original = _stored(vault, "Recordings")
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)

    original.write_bytes(DAMAGED)
    result = original_fixity.run_pass(vault, budget=0)

    assert (result.rehashed_bytes, result.changed_bytes) == (0, len(DAMAGED))
    assert result.outcomes == {"corrupt_no_copy": 1}


def test_an_original_larger_than_one_budget_is_verified_by_a_later_pass(vault: Path) -> None:
    _stored(vault, "Recordings")
    original_fixity.walk(vault)
    half = len(TRANSCRIPT) // 2 + 1

    assert original_fixity.run_pass(vault, budget=half).outcomes == {}
    assert original_fixity.run_pass(vault, budget=half).outcomes == {"verified": 1}


@pytest.mark.parametrize("cause", ["edited", "corrupted"])
def test_an_edit_and_storage_damage_are_told_apart_and_both_restored(vault: Path, cause: str) -> None:
    original = _stored(vault, "Recordings")
    _member_blob(vault)
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)

    original.write_bytes(DAMAGED)
    if cause == "corrupted":
        # Storage damage leaves the stat as it was; recording the stat the file has now stands in for it.
        info = os.lstat(original)
        record = {"inode": info.st_ino, "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                  "ctime_ns": info.st_ctime_ns}
        with contextlib.closing(sqlite3.connect(original_fixity.table_path(vault))) as table, table:
            table.execute("UPDATE originals SET identity = ?", (json.dumps(record),))
    original_fixity.run_pass(vault)

    [finding] = _findings(vault)
    assert (finding.meta["state"], finding.meta["cause"]) == ("restored", cause)
    assert original.read_bytes() == TRANSCRIPT


def test_a_write_during_a_restore_is_kept_and_the_restore_is_left_to_the_next_pass(vault: Path, monkeypatch) -> None:
    original = _stored(vault, "Recordings")
    _stored(vault, "Spares")
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    racing = b"Field recording, side A: corrected by hand while the pass ran.\n"
    real_stage = original_fixity._stage

    def stage_then_write(*args, **kwargs):
        staged = real_stage(*args, **kwargs)
        original.write_bytes(racing)
        return staged

    monkeypatch.setattr(original_fixity, "_stage", stage_then_write)
    original.write_bytes(DAMAGED)
    result = original_fixity.run_pass(vault)

    assert (result.rescheduled, result.outcomes) == (1, {"verified": 1})
    assert original.read_bytes() == racing
    assert _trash(vault) == {}


def test_the_changed_bytes_of_a_raw_protected_original_keep_their_protection_in_trash(vault: Path) -> None:
    with library_scope():
        captured = [preserve.preserve(vault, scope="Field", category=category, filename="side-a.txt",
                                      content=TRANSCRIPT.decode(), raw_protection=True)
                    for category in ("Recordings", "Spares")]
    original = vault / captured[0].path
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)

    original.write_bytes(DAMAGED)
    assert original_fixity.run_pass(vault, budget=0).outcomes == {"restored": 1}

    payload = next(name for name in _trash(vault) if not name.endswith(".meta.json"))
    assert payload.startswith("__exomem_raw_v1__")


def test_a_damaged_text_original_is_reported_and_its_source_page_is_never_written(vault: Path) -> None:
    page = _text_source(vault)
    # A file with the same bytes, which must never overwrite the page.
    _stored(vault, "Notes", b"Meeting notes from the gauge survey.\nThe river rose overnight.\n")
    original_fixity.walk(vault)
    assert original_fixity.run_pass(vault).outcomes == {"verified": 2}

    damaged = page.read_bytes().replace(b"rose overnight", b"fell overnight")
    page.write_bytes(damaged)
    assert original_fixity.run_pass(vault, budget=0).outcomes == {"corrupt_no_copy": 1}

    assert page.read_bytes() == damaged
    [finding] = _findings(vault)
    assert finding.meta["cause"] == "edited"
    assert finding.meta["route"] == [{"source": "import_again", "path": "Notes/gauge-survey.md"}]


def test_a_line_added_after_a_text_original_s_fence_stays_reported_across_the_next_walk(vault: Path) -> None:
    page = _text_source(vault)
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)

    page.write_bytes(page.read_bytes() + b"An afterthought typed below the fence.\n")
    assert original_fixity.run_pass(vault, budget=0).outcomes == {"corrupt_no_copy": 1}
    original_fixity.walk(vault)
    original_fixity.run_pass(vault, budget=0)

    [finding] = _findings(vault)
    assert (finding.meta["state"], finding.path) == ("corrupt_no_copy", page.relative_to(vault).as_posix())


def test_a_linked_note_never_stops_the_walk_or_the_pass(vault: Path) -> None:
    _stored(vault, "Recordings")
    notes = vault / "Knowledge Base"
    (notes / "Tide log.md").write_text("# Tide log\n\nHigh water at 06:10.\n")
    (notes / "Tide log link.md").symlink_to(notes / "Tide log.md")
    (notes / "Gauge log.md").write_text("# Gauge log\n\nThe gauge read 2.4 m.\n")
    os.link(notes / "Gauge log.md", notes / "Gauge log copy.md")

    assert original_fixity.walk(vault) == 1
    assert original_fixity.run_pass(vault).outcomes == {"verified": 1}


def test_a_restore_that_cannot_put_its_copy_in_place_tries_again_at_the_next_pass(vault: Path) -> None:
    original = _stored(vault, "Recordings")
    _stored(vault, "Spares")
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    trash = vault / "Knowledge Base" / "_trash"
    trash.write_bytes(b"")  # a file where the Trash folder goes, so the changed bytes have nowhere to go

    original.write_bytes(DAMAGED)
    assert original_fixity.run_pass(vault, budget=0).outcomes == {"corrupt_no_copy": 1}

    assert original.read_bytes() == DAMAGED
    [finding] = _findings(vault)
    [copy] = finding.meta["route"]
    assert (copy["source"], bool(copy.get("code"))) == ("stored_original", True)
    trash.unlink()
    assert original_fixity.run_pass(vault, budget=0).outcomes == {"restored": 1}
    assert original.read_bytes() == TRANSCRIPT


def test_a_replace_that_keeps_failing_keeps_one_trash_copy_of_the_changed_bytes(
        vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = _stored(vault, "Recordings")
    _stored(vault, "Spares")
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    rename = original_fixity._rename

    def locked(*args, replace: bool = False, **kwargs):
        if replace:
            raise PermissionError("held open by another program")
        return rename(*args, replace=replace, **kwargs)

    monkeypatch.setattr(original_fixity, "_rename", locked)
    original.write_bytes(DAMAGED)
    for _ in range(3):
        assert original_fixity.run_pass(vault, budget=0).outcomes == {"corrupt_no_copy": 1}

    payloads = [data for name, data in _trash(vault).items() if not name.endswith(".meta.json")]
    assert payloads == [DAMAGED]
    monkeypatch.setattr(original_fixity, "_rename", rename)
    assert original_fixity.run_pass(vault, budget=0).outcomes == {"restored": 1}
    assert original.read_bytes() == TRANSCRIPT
    assert [data for name, data in _trash(vault).items() if not name.endswith(".meta.json")] == [DAMAGED]


def test_a_text_source_whose_fenced_text_never_matched_raises_no_alarm_until_it_matches(vault: Path) -> None:
    # Source pages rendered before 2026-10-25 fenced the text trimmed, so the hash of the file never matches.
    text = "Meeting notes from the gauge survey.\nThe river rose overnight.\n"
    planned = {"title": "Gauge survey", "original_path": "Notes/gauge-survey.md",
               "original_sha256": hashlib.sha256(text.encode()).hexdigest(), "original_bytes": len(text.encode())}
    page = vault / "Knowledge Base" / "Sources" / "Gauge survey.md"
    page.write_text(adopt.render_text_source(planned, text.rstrip(), date_iso="2026-10-01", collection=None))
    original_fixity.walk(vault)

    assert original_fixity.run_pass(vault).outcomes == {"unmatched": 1}
    assert original_fixity.run_pass(vault, budget=0).outcomes == {}
    assert _findings(vault) == []
    assert original_fixity.readiness(vault)["unresolved"] == 0
    assert original_fixity.status(vault)["counts"]["unmatched"] == 1


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="requires unprivileged POSIX permissions")
def test_a_binding_that_cannot_be_read_keeps_its_original_for_the_next_pass(vault: Path) -> None:
    blob = _member_blob(vault)
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    family = vault / "Knowledge Base" / "Evidence" / "Device" / "Exports"
    manifest = next(family.rglob(f"*{archive_members.MANIFEST_SUFFIX}"))

    blob.unlink()
    manifest.chmod(0)
    try:
        result = original_fixity.run_pass(vault, budget=0)
    finally:
        manifest.chmod(0o644)

    assert (result.rescheduled, result.outcomes) == (1, {})
    assert original_fixity.status(vault)["last_pass_rescheduled"] == 1
    assert original_fixity.run_pass(vault, budget=0).outcomes.get("missing") == 1


def test_status_reads_unknown_until_the_first_pass_when_stale_and_off_with_the_switch(
    vault: Path, monkeypatch
) -> None:
    _stored(vault, "Recordings")
    assert original_fixity.status(vault) == {"state": "unknown"}
    assert original_fixity.readiness(vault) == {"state": "unknown"}
    assert original_fixity.attention_findings(vault) == ([], "unknown")

    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    status = original_fixity.status(vault)
    assert (status["state"], status["coverage"], status["counts"]["verified"]) == ("measured", "complete", 1)

    # A thread that stopped long ago leaves its last count, which is no answer now.
    with contextlib.closing(sqlite3.connect(original_fixity.table_path(vault))) as table, table:
        table.execute("UPDATE meta SET value = '1000.0' WHERE key = 'pass_at'")
    stale = {"state": "unknown", "last_pass_at": "1970-01-01T00:16:40+00:00"}
    assert (original_fixity.status(vault), original_fixity.readiness(vault)) == (stale, stale)
    assert original_fixity.attention_findings(vault) == ([], "unknown")

    monkeypatch.setenv("EXOMEM_DISABLE_ORIGINAL_FIXITY", "1")
    assert original_fixity.status(vault) == {"state": "off"}
    assert original_fixity.readiness(vault) == {"state": "off"}
    assert original_fixity.attention_findings(vault) == ([], "unknown")
    assert original_fixity.start(vault) is None


def test_a_lost_table_is_rebuilt_from_the_bindings_and_still_finds_the_damage(vault: Path) -> None:
    original = _stored(vault, "Recordings")
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)

    original.write_bytes(DAMAGED)
    for leftover in original_fixity.table_path(vault).parent.iterdir():
        leftover.unlink()
    found = original_fixity.walk(vault)
    original_fixity.run_pass(vault, budget=0)
    assert original_fixity.status(vault)["counts"]["unverified"] == found
    original_fixity.run_pass(vault)

    # The expected hash comes from the binding, so a rebuilt table never adopts the damaged bytes.
    [finding] = _findings(vault)
    assert (finding.meta["state"], finding.meta["cause"]) == ("corrupt_no_copy", "unknown")


def test_the_walk_finds_every_bound_original_with_the_lexical_index_absent(vault: Path) -> None:
    _stored(vault, "Recordings")
    _member_blob(vault, b"A second member that only the archive holds.\n")
    lexical = lexstore.lexical_path(vault)
    for leftover in lexical.parent.glob(lexical.name + "*"):
        leftover.unlink()

    found = original_fixity.walk(vault)
    original_fixity.run_pass(vault)

    assert not lexical.exists()
    assert original_fixity.status(vault)["counts"]["verified"] == found >= 2


def test_a_binding_rebound_since_the_walk_moves_nothing(vault: Path) -> None:
    original = _stored(vault, "Recordings")
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)

    # The owner replaces the original and its binding together, as a governed replacement does.
    sidecar = original.with_name(original.name + ".md")
    sidecar.write_text(sidecar.read_text().replace(hashlib.sha256(TRANSCRIPT).hexdigest(),
                                                   hashlib.sha256(DAMAGED).hexdigest()))
    original.write_bytes(DAMAGED)
    assert original_fixity.run_pass(vault).outcomes == {}

    assert original.read_bytes() == DAMAGED
    assert _trash(vault) == {}
    assert _findings(vault) == []
    original_fixity.walk(vault)
    assert original_fixity.run_pass(vault).outcomes == {"verified": 1}


def test_a_stopped_pass_marks_its_counts_partial(vault: Path) -> None:
    _stored(vault, "Recordings")
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    stop = threading.Event()

    with pytest.raises(foreground_priority.BulkCancelled), foreground_priority.bulk(stop=stop):
        stop.set()  # the service stops while the pass runs
        original_fixity.run_pass(vault)

    status = original_fixity.status(vault)
    assert (status["state"], status["coverage"]) == ("measured", "partial")


def test_another_audience_never_receives_the_path_of_a_copy_outside_the_vault(vault: Path) -> None:
    from test_governance_egress import _external, write_rule, write_scope

    from exomem.governance.principal import request_scope

    page = _text_source(vault)
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    page.write_bytes(page.read_bytes().replace(b"rose overnight", b"fell overnight"))
    original_fixity.run_pass(vault)
    write_scope(vault)
    write_rule(vault, ceiling=0)

    with request_scope(_external()):
        [finding] = _findings(vault)

    assert finding.meta["route"] == [{"source": "import_again"}]
    assert finding.paths == [page.relative_to(vault).as_posix()]
