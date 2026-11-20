"""A pooled original is present: export, fixity, cache recovery, duplicates and containers read it as present.

In a pooled archive the bytes of an original live once under the pool, and its filing keeps only a companion that
binds the logical path, the digest and the size. No file exists at the logical path. Each test builds that state on a
temporary vault and runs the real consumer; the data is invented.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import uuid
import zipfile
from pathlib import Path

import pytest
from test_document_containers import container_bytes
from test_pooled_conversion import INVOICE, RECEIPT, _convert, _file
from test_upload_sessions import TUS
from test_upload_sessions import _client as upload_client
from test_vault_portability import _context

from exomem import (
    artifact_lifecycle,
    cloud_export_steps,
    duplicates,
    held_fs,
    local_import,
    media_jobs,
    media_processing,
    media_store,
    original_fixity,
    original_pool,
    originals,
    preserve,
    state_migration,
    state_paths,
    vault_portability,
)
from exomem.governance.principal import library_scope
from tests.test_cloud_export import CELL
from tests.test_cloud_export_steps import PLENTY, _evicted, _owner
from tests.test_media_store import _stored, selected_store  # noqa: F401
from tests.test_shared_originals import _pooled_capture

TRANSCRIPT = b"Field recording, side A: the river gauge read 2.4 m at dawn.\n"
DAMAGED = b"Field recording, side A: the river gauge read 9.9 m at dawn.\n"  # same size, other bytes


@pytest.fixture(autouse=True)
def _fixity_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_ORIGINAL_FIXITY", raising=False)
    # Local import journals live in the user's state folder; a test reads only its own.
    monkeypatch.setattr(local_import, "user_state_dir", lambda name: str(tmp_path / "user-state"))


def _pool(vault: Path, monkeypatch: pytest.MonkeyPatch, path: Path) -> str:
    """Move an ordinary filing's bytes into the shared pool and remove its file, as a conversion leaves it."""
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    if not original_pool.enabled(vault):
        original_pool.enable(vault)
    with original_pool.prepare(vault, digest, len(data), held_fs.Chunks(lambda: [data])) as staged:
        staged.publish()
    path.unlink()
    return digest


def _filed(vault: Path, category: str, data: bytes = TRANSCRIPT) -> Path:
    with library_scope():
        captured = preserve.preserve(vault, scope="Field", category=category, filename="side-a.txt",
                                     content=data.decode())
    return vault / captured.path


def _damage(vault: Path, digest: str, data: bytes = DAMAGED) -> None:
    """Replace the shared object's bytes; the default has the original's size, as bit rot would."""
    path = original_pool.object_path(vault, digest)
    path.chmod(0o600)
    path.write_bytes(data)


def _pause_and_pack(vault: Path, staging: Path, media_mode: str | None) -> tuple[cloud_export_steps.PauseResult, zipfile.ZipFile]:
    export_id = str(uuid.uuid4())
    with _owner():
        paused = cloud_export_steps.pause(vault, staging, export_id=export_id, cell_id=CELL, media_mode=media_mode,
                                          max_bytes=PLENTY)
        cloud_export_steps.pack(vault, staging, export_id=export_id, cell_id=CELL, part_bytes=64 * 1024**2,
                                max_parts=10, max_bytes=PLENTY)
    archive = staging / cloud_export_steps.EXPORT_DIR / cloud_export_steps.SPOOL_NAME / "part-00001"
    return paused, zipfile.ZipFile(archive)


def test_an_export_of_a_pooled_archive_ships_one_copy_and_plans_no_store_fetch(
        vault: Path, selected_store, tmp_path_factory, monkeypatch):  # noqa: F811
    """A pooled original that a media store also holds is counted as evicted, so a retained export is refused as
    unreachable, or a served one plans a store fetch and ships a second copy of bytes the pool already ships."""
    path = _stored(vault, "cached", 300)
    data = path.read_bytes()
    digest = _pool(vault, monkeypatch, path)
    assert originals.original_state(vault, path).state == "cached"
    logical = path.relative_to(vault).as_posix()
    shared = original_pool.object_path(vault, digest).relative_to(vault).as_posix()

    paused, archive = _pause_and_pack(vault, tmp_path_factory.mktemp("staging"), media_mode="retained")

    with archive:
        names = archive.namelist()
        assert (paused.evicted, paused.evicted_bytes) == (0, 0)
        assert logical not in names
        assert archive.read(shared) == data
        assert [name for name in names if archive.read(name) == data] == [shared]
        assert originals.sidecar_path(path).relative_to(vault).as_posix() in names


def test_a_pooled_original_whose_pool_object_is_gone_still_plans_its_store_fetch(
        vault: Path, selected_store, tmp_path_factory, monkeypatch):  # noqa: F811
    """Counting every pooled filing as held would drop an original whose pool object is absent from the export."""
    path = _stored(vault, "cached", 300)
    digest = _pool(vault, monkeypatch, path)
    original_pool.object_path(vault, digest).unlink()
    assert originals.original_state(vault, path).state == "evicted"

    staging = tmp_path_factory.mktemp("staging")
    with _owner():
        paused = cloud_export_steps.pause(vault, staging, export_id=str(uuid.uuid4()), cell_id=CELL,
                                          media_mode="served", max_bytes=PLENTY)

    assert (paused.evicted, paused.evicted_bytes) == (1, 300)


def test_a_pooled_original_does_not_hide_a_legacy_evicted_one_from_the_export(
        vault: Path, selected_store, tmp_path_factory, monkeypatch):  # noqa: F811
    """The pooled exemption must stay per original: a legacy evicted original in the same vault still ships from the store."""
    legacy, _reference, _data = _evicted(vault, "legacy", 200)
    pooled = _stored(vault, "pooled", 300)
    _pool(vault, monkeypatch, pooled)

    staging = tmp_path_factory.mktemp("staging")
    with _owner():
        paused = cloud_export_steps.pause(vault, staging, export_id=str(uuid.uuid4()), cell_id=CELL,
                                          media_mode="served", max_bytes=PLENTY)
        plan = json.loads((staging / cloud_export_steps.EXPORT_DIR / cloud_export_steps.PLAN_NAME).read_text())

    assert (paused.evicted, paused.evicted_bytes) == (1, 200)
    assert [entry["path"] for entry in plan["evicted"]] == [legacy.relative_to(vault).as_posix()]


def test_a_restored_pooled_archive_resolves_its_original_from_the_restored_pool(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """A restore that drops the pool marker or object leaves every pooled filing with a companion and no bytes."""
    added = _pooled_capture(vault, tmp_path, monkeypatch, "side-a.txt", TRANSCRIPT)
    exported = vault_portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())
    target = tmp_path / "restored"

    vault_portability.prepare_restore(exported.archive_path, target, context=_context(lifecycle_state="restore-staging"))

    assert not (target / added.artifact_path).exists()
    assert original_pool.enabled(target)
    restored = originals.original_state(target, added.artifact_path)
    assert (restored.state, restored.binary_sha256) == ("local", hashlib.sha256(TRANSCRIPT).hexdigest())
    assert restored.local_path is not None and restored.local_path.read_bytes() == TRANSCRIPT


def test_fixity_verifies_a_healthy_pooled_original_from_its_pool_object(vault: Path, tmp_path: Path, monkeypatch) -> None:
    """A pass that stats the logical path reports a healthy pooled original missing, and restores or alarms for it."""
    added = _pooled_capture(vault, tmp_path, monkeypatch, "side-a.txt", TRANSCRIPT)
    original_fixity.walk(vault)

    result = original_fixity.run_pass(vault)

    assert result.outcomes == {"verified": 1}
    assert not (vault / added.artifact_path).exists()
    assert original_fixity.readiness(vault) == {"state": "measured", "unresolved": 0}


def test_fixity_reports_a_deleted_pool_object_missing_and_writes_no_logical_file(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """A repair that writes the logical path would turn a lost shared object into an ordinary file beside the pool."""
    added = _pooled_capture(vault, tmp_path, monkeypatch, "side-a.txt", TRANSCRIPT)
    original_fixity.walk(vault)
    assert original_fixity.run_pass(vault).outcomes == {"verified": 1}

    original_pool.object_path(vault, hashlib.sha256(TRANSCRIPT).hexdigest()).unlink()
    result = original_fixity.run_pass(vault, budget=0)

    assert result.outcomes == {"missing": 1}
    assert not (vault / added.artifact_path).exists()
    findings, coverage = original_fixity.attention_findings(vault)
    assert coverage is None
    [finding] = findings
    assert (finding.severity, finding.meta["state"]) == ("error", "missing")
    assert original_fixity.readiness(vault) == {"state": "measured", "unresolved": 1}


def test_fixity_reports_a_damaged_pool_object_and_writes_no_logical_file(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """Damage to a shared object is found by reading it, and a restore would write an ordinary file at a pooled path."""
    added = _pooled_capture(vault, tmp_path, monkeypatch, "side-a.txt", TRANSCRIPT)
    original_fixity.walk(vault)
    assert original_fixity.run_pass(vault).outcomes == {"verified": 1}

    _damage(vault, hashlib.sha256(TRANSCRIPT).hexdigest())
    result = original_fixity.run_pass(vault)

    assert result.outcomes == {"corrupt_no_copy": 1}
    assert not (vault / added.artifact_path).exists()
    assert original_fixity.readiness(vault) == {"state": "measured", "unresolved": 1}


def test_fixity_restores_a_damaged_pool_object_inside_the_pool_from_its_store_copy(
        vault: Path, selected_store, monkeypatch) -> None:  # noqa: F811
    """A damaged shared object stayed corrupt_no_copy although the media store held verified bytes: the repair never
    put them back into the pool, and writing them at the logical path would leave an ordinary file beside the pool."""
    path = _stored(vault, "cached", 300)
    digest = _pool(vault, monkeypatch, path)
    original_fixity.walk(vault)
    assert original_fixity.run_pass(vault).outcomes == {"verified": 1}

    _damage(vault, digest, b"y" * 300)
    result = original_fixity.run_pass(vault)

    assert result.outcomes == {"restored": 1}
    assert original_pool.object_path(vault, digest).read_bytes() == b"x" * 300
    assert not path.exists()
    trash = vault / "Knowledge Base" / "_trash"
    kept = [found.read_bytes() for found in trash.rglob(f"*{path.name}") if found.is_file()]
    assert kept == [b"y" * 300]  # the changed bytes, kept as a fixity repair keeps them
    assert original_fixity.run_pass(vault).outcomes == {"verified": 1}


def test_fixity_reports_a_pool_object_of_the_wrong_size_as_damaged_instead_of_retiring_its_row(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """A truncated shared object read as no pooled original: unreadable each pass and never reported as damaged, and
    retiring the row would stop watching it altogether."""
    added = _pooled_capture(vault, tmp_path, monkeypatch, "side-a.txt", TRANSCRIPT)
    original_fixity.walk(vault)
    assert original_fixity.run_pass(vault).outcomes == {"verified": 1}

    _damage(vault, hashlib.sha256(TRANSCRIPT).hexdigest(), TRANSCRIPT[:-1])
    result = original_fixity.run_pass(vault)

    assert (result.unreadable, result.outcomes) == (0, {"corrupt_no_copy": 1})
    assert not (vault / added.artifact_path).exists()
    with contextlib.closing(sqlite3.connect(original_fixity.table_path(vault))) as table:
        assert table.execute("SELECT COUNT(*) FROM originals WHERE path = ?", (added.artifact_path,)).fetchone() == (1,)


def test_a_legacy_filing_beside_a_pooled_one_keeps_its_fixity_outcomes(vault: Path, tmp_path: Path, monkeypatch) -> None:
    """The pooled reading must not reach an ordinary filing: a deleted legacy file is still missing and stays deleted."""
    legacy = _filed(vault, "Legacy", TRANSCRIPT.replace(b"side A", b"side B"))
    _pooled_capture(vault, tmp_path, monkeypatch, "side-a.txt", TRANSCRIPT)
    original_fixity.walk(vault)
    assert original_fixity.run_pass(vault).outcomes == {"verified": 2}

    legacy.unlink()

    assert original_fixity.run_pass(vault, budget=0).outcomes == {"missing": 1}
    assert not legacy.exists()


def test_cache_recovery_reads_a_pooled_cached_original_from_its_pool_object(
        vault: Path, selected_store, monkeypatch):  # noqa: F811
    """Recovery that opens the logical path raises MISSING for a pooled original, so its cache row is never recorded."""
    path = _stored(vault, "cached", 300)
    digest = _pool(vault, monkeypatch, path)
    ledger = media_jobs.MediaJobStore(vault)
    with contextlib.closing(ledger._connect()) as connection, connection:
        connection.execute("DELETE FROM media_cache")
    snapshot = originals.original_state(vault, path)
    assert snapshot.state == "cached" and snapshot.local_path is not None

    assert media_store.recover_cache(vault, path, snapshot)

    with contextlib.closing(ledger._connect()) as connection:
        cached = [tuple(row) for row in connection.execute("SELECT sha256, evicted FROM media_cache")]
    assert cached == [(digest, 0)]
    assert not path.exists()


def test_a_duplicate_upload_of_a_pooled_original_is_found(vault: Path, tmp_path: Path, monkeypatch, duplicate_index) -> None:
    """A re-hash of the logical path reads a pooled original as absent, so the same bytes are filed a second time."""
    first = _pooled_capture(vault, tmp_path, monkeypatch, "side-a.txt", TRANSCRIPT)
    from exomem import lexstore

    assert lexstore.get_store(vault).rebuild_atomic()

    found = duplicates.find_duplicate(
        vault, sha256=first.artifact_hash, lane=duplicates.SOURCES, protected=False, keeps_original=True)

    assert found.hit is not None and found.hit.path == first.path
    assert not (vault / first.artifact_path).exists()


def test_a_duplicate_upload_of_a_damaged_pooled_original_is_not_taken_for_a_copy(
        vault: Path, tmp_path: Path, monkeypatch, duplicate_index) -> None:
    """A pooled original counts only after its bytes hash again; damaged bytes must not stand in for the upload."""
    first = _pooled_capture(vault, tmp_path, monkeypatch, "side-a.txt", TRANSCRIPT)
    from exomem import lexstore

    assert lexstore.get_store(vault).rebuild_atomic()
    _damage(vault, first.artifact_hash)

    found = duplicates.find_duplicate(
        vault, sha256=first.artifact_hash, lane=duplicates.SOURCES, protected=False, keeps_original=True)

    assert found.hit is None


def test_a_duplicate_upload_of_an_evicted_original_is_found_by_its_binding(
        vault: Path, selected_store, monkeypatch, duplicate_index) -> None:  # noqa: F811
    """An evicted original has no bytes to hash; reading it as absent files the same upload again."""
    from exomem import lexstore

    path, _reference, data = _evicted(vault, "evicted", 300)
    assert lexstore.get_store(vault).rebuild_atomic()
    companion = originals.sidecar_path(path).relative_to(vault).as_posix()

    found = duplicates.find_duplicate(
        vault, sha256=hashlib.sha256(data).hexdigest(), lane=duplicates.EVIDENCE, protected=False, keeps_original=True)

    assert found.hit is not None and found.hit.path == companion


def test_a_session_for_a_pooled_filings_name_is_refused_before_any_byte_is_sent(vault, monkeypatch) -> None:
    """A pooled filing has no file at its name, so a pre-check that tests the file lets the whole upload run first."""
    path = _filed(vault, "Uploads", b"{}")
    _pool(vault, monkeypatch, path)
    client = upload_client(vault, monkeypatch)
    metadata = {"filename": path.name, "scope": "Field", "category": "Uploads",
                "sha256": hashlib.sha256(b"device_days").hexdigest()}

    refused = client.request(
        "POST", "/upload/sessions",
        headers={**TUS, "Upload-Length": "11", "Upload-Metadata": ",".join(
            f"{key} {base64.b64encode(value.encode()).decode()}" for key, value in metadata.items())})

    assert (refused.status_code, refused.json()["code"]) == (409, "ARTIFACT_EXISTS")


def _pooled_member(vault: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes):
    """A captured container whose one payload member is then pooled: its pool object holds the member's bytes."""
    stored = preserve.preserve_stream(
        vault, scope="Examples", category="Documents", filename="bundle.asice",
        stream=io.BytesIO(container_bytes([("one.pdf", payload)])))
    member = vault / stored.document_container["payloads"][0]
    assert member.read_bytes() == payload
    digest = _pool(vault, monkeypatch, member)
    return stored, member, digest


def test_explicit_container_processing_keeps_a_pooled_member_without_an_ordinary_copy(vault: Path, monkeypatch) -> None:
    """Recovery that treats a pooled member as absent writes a second, ordinary copy of bytes the pool already holds."""
    stored, member, _digest = _pooled_member(vault, monkeypatch, b"pdf")

    media_processing.process_container(vault, stored.path)

    assert not member.exists()
    assert originals.available(vault, member)


def test_explicit_container_processing_refuses_a_pooled_member_whose_bytes_changed(vault: Path, monkeypatch) -> None:
    """A pooled member still has to prove the container's identity; unchecked, damaged bytes pass as the member."""
    stored, member, digest = _pooled_member(vault, monkeypatch, b"pdf")
    _damage(vault, digest, b"xyz")

    with pytest.raises(media_processing.MediaProcessingError) as refused:
        media_processing.process_container(vault, stored.path)

    assert refused.value.code == "CONTAINER_CONFLICT"
    assert not member.exists()


def _converted_filings(vault: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Two filings of one receipt and one protected invoice after conversion, as their originals' and pages' bytes."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    filed = [(*_file(vault, "receipts", "receipt.png", RECEIPT), RECEIPT),
             (*_file(vault, "expenses", "receipt-copy.png", RECEIPT), RECEIPT),
             (*_file(vault, "contracts", "letter.pdf", INVOICE, protected=True), INVOICE)]
    preview = _convert(vault)
    _convert(vault, dry_run=False, plan_id=preview["plan_id"])
    assert not any((vault / original).exists() for original, _page, _data in filed)
    return ({original: data for original, _page, data in filed},
            {page: (vault / page).read_bytes() for _original, page, _data in filed})


def _tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_a_compatibility_export_writes_each_pooled_original_as_an_ordinary_file_at_its_logical_path(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """Catches a compatibility export that ships the shared pool or its format marker instead of ordinary files, leaves
    a filing without bytes at its path, or alters a page, so a pre-feature reader finds pages that name nothing."""
    originals_bytes, pages = _converted_filings(vault, monkeypatch)
    assert len(list(original_pool.pool_root(vault).rglob("*.blob"))) == 2
    source = _tree(vault)

    exported = vault_portability.export_compatible_vault(vault, tmp_path / "exports", context=_context())

    pool = original_pool.pool_root(vault).relative_to(vault).as_posix()
    with zipfile.ZipFile(exported.archive_path) as archive:
        assert {path: archive.read(path) for path in originals_bytes} == originals_bytes
        assert {page: archive.read(page) for page in pages} == pages
        assert not [name for name in archive.namelist() if name.startswith(f"{pool}/")]
        marker = state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID.encode()
        assert not [name for name in archive.namelist() if marker in archive.read(name)]
    assert _tree(vault) == source


class _PoolReadFailure:
    """A shared object whose disk returns its bytes once, then fails the next read, as a failing disk does."""

    def __init__(self, source) -> None:
        self._source = source
        self._reads = 0

    def __getattr__(self, name):
        return getattr(self._source, name)

    @property
    def stream(self):
        return self

    def read(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._reads > 1:
            raise OSError(5, "the pool disk failed a read")
        return self._source.stream.read(size)


def test_an_interrupted_compatibility_export_leaves_the_source_unchanged_and_publishes_nothing(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """Catches an export that, stopped part-way through writing originals, publishes the partial archive as complete
    or changes the pooled source; a rerun must then produce the complete archive."""
    originals_bytes, _pages = _converted_filings(vault, monkeypatch)
    source = _tree(vault)
    exports = tmp_path / "exports"
    real_open = original_pool.open_object

    @contextlib.contextmanager
    def failing_open(*args, **kwargs):
        with real_open(*args, **kwargs) as opened:
            yield _PoolReadFailure(opened)

    monkeypatch.setattr(original_pool, "open_object", failing_open)
    with pytest.raises(OSError):
        vault_portability.export_compatible_vault(vault, exports, context=_context())

    assert _tree(vault) == source
    assert sorted(path.name for path in exports.iterdir()) == []
    monkeypatch.setattr(original_pool, "open_object", real_open)
    exported = vault_portability.export_compatible_vault(vault, exports, context=_context())
    with zipfile.ZipFile(exported.archive_path) as archive:
        assert {path: archive.read(path) for path in originals_bytes} == originals_bytes


def test_a_compatibility_export_refuses_a_damaged_shared_object_and_publishes_nothing(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """Catches a compatibility export that ships damaged shared bytes as a filing's original, so the only route back to
    an older release hands over an archive whose original no longer matches its page."""
    added = _pooled_capture(vault, tmp_path, monkeypatch, "side-a.txt", TRANSCRIPT)
    _damage(vault, hashlib.sha256(TRANSCRIPT).hexdigest())
    damaged = _tree(vault)
    exports = tmp_path / "exports"

    with pytest.raises(vault_portability.PortabilityError) as refused:
        vault_portability.export_compatible_vault(vault, exports, context=_context())

    assert refused.value.code == "DIGEST_MISMATCH"
    assert refused.value.member == added.artifact_path
    assert not exports.exists() or list(exports.iterdir()) == []
    assert _tree(vault) == damaged


def test_a_compatibility_export_refuses_a_shared_object_that_vanishes_and_publishes_nothing(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """Catches a compatibility export that skips an original it can no longer read and still publishes, so an archive
    that lost bytes after the export began is delivered as complete."""
    added = _pooled_capture(vault, tmp_path, monkeypatch, "side-a.txt", TRANSCRIPT)
    source = _tree(vault)
    exports = tmp_path / "exports"
    real_open = original_pool.open_object

    @contextlib.contextmanager
    def vanishing_open(*args, **kwargs):
        if list(exports.glob("*.zip.partial")):  # The archive is being written: the object is gone.
            raise held_fs.HeldFsError("MISSING", "the shared object vanished")
        with real_open(*args, **kwargs) as opened:
            yield opened

    monkeypatch.setattr(original_pool, "open_object", vanishing_open)
    with pytest.raises(vault_portability.PortabilityError) as refused:
        vault_portability.export_compatible_vault(vault, exports, context=_context())

    assert refused.value.code == "ORIGINAL_UNAVAILABLE"
    assert refused.value.member == added.artifact_path
    assert list(exports.iterdir()) == []
    assert _tree(vault) == source


def test_a_compatibility_export_refuses_a_filing_whose_original_is_gone_and_publishes_nothing(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """Catches a compatibility export that leaves out an original it cannot find and still publishes, so the way back to
    an older release delivers a page that names bytes the archive does not hold."""
    added = _pooled_capture(vault, tmp_path, monkeypatch, "side-a.txt", TRANSCRIPT)
    original_pool.object_path(vault, hashlib.sha256(TRANSCRIPT).hexdigest()).unlink()
    source = _tree(vault)
    exports = tmp_path / "exports"

    with pytest.raises(vault_portability.PortabilityError) as refused:
        vault_portability.export_compatible_vault(vault, exports, context=_context())

    assert refused.value.code == "ORIGINAL_UNAVAILABLE"
    assert refused.value.member == added.artifact_path
    assert not exports.exists() or list(exports.iterdir()) == []
    assert _tree(vault) == source


def test_a_whole_vault_export_carries_a_page_that_a_trash_retirement_left_without_bytes(
        vault: Path, selected_store, tmp_path: Path) -> None:  # noqa: F811
    """Catches an export that opens the original of a page that a Trash retirement left at its path, and so fails
    every whole-vault export of an archive in which an offloaded filing was deleted."""
    from test_pooled_move_trash import LOOSE
    from test_vault_store_media import offloaded

    from exomem import delete_file

    offloaded(vault, LOOSE)
    delete_file.delete_file(vault, path=LOOSE, confirm=True)
    page = (vault / f"{LOOSE}.md").read_bytes()

    exported = vault_portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())

    with zipfile.ZipFile(exported.archive_path) as archive:
        assert archive.read(f"{LOOSE}.md") == page
        assert LOOSE not in archive.namelist()

def test_a_compatibility_export_invalidates_a_trash_retirement_and_keeps_its_pages(
        vault: Path, selected_store, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    """Catches a compatibility export that hands a retirement witness to a reader that keeps none current, so a
    capable runtime that later reads the archive honours a witness the reader's edits made stale."""
    from test_pooled_move_trash import LOOSE, _evicted_in_trash

    from exomem import trash_metadata

    trashed = _evicted_in_trash(vault, monkeypatch)
    metadata = trashed.trash_path + trash_metadata.METADATA_SUFFIX
    placement = json.loads((vault / metadata).read_bytes())
    pages = {page: (vault / page).read_bytes() for page in (f"{LOOSE}.md", f"{trashed.trash_path}.md")}
    assert "original_retirement" in placement

    exported = vault_portability.export_compatible_vault(vault, tmp_path / "exports", context=_context())

    with zipfile.ZipFile(exported.archive_path) as archive:
        assert json.loads(archive.read(metadata)) == {key: value for key, value in placement.items()
                                                      if key != "original_retirement"}
        assert {page: archive.read(page) for page in pages} == pages
    assert json.loads((vault / metadata).read_bytes()) == placement


# The release before shared storage calls this module `hosted_portability`; the restore is its own documented flow.
_OLDER_RESTORE = """
import hashlib, json, sys
from pathlib import Path
from exomem.hosted_portability import PortabilityContext, prepare_restore, publish_prepared_restore
archive, work = Path(sys.argv[1]), Path(sys.argv[2])
context = PortabilityContext(cell_id="cell-alpha-7f3c", vault_id="vault-alpha-91d2", operation_id="operation-restore-001",
    created_at=sys.argv[3], operator_authorized=True, lifecycle_state="restore-staging", routing_stopped=True,
    active_mutations=0, background_writers_stopped=True, reads_allowed=True)
prepared = prepare_restore(archive, work / "staging", context=context)
root = publish_prepared_restore(prepared, work / "restored", rebuild_derived=lambda _root: None).live_root
print(json.dumps({path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(root.rglob("*")) if path.is_file()}))
"""


def test_a_release_without_shared_storage_restores_a_compatibility_export_exactly(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """Catches a compatibility export that the release before shared storage cannot restore, or restores with a page or
    original changed, which is the only route back for an archive that ran on shared storage."""
    older = os.environ.get("EXOMEM_TEST_OLDER_READER_PYTHON")
    if not older:
        pytest.skip("requires the earlier release that CI installs")
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    ordinary, ordinary_page = _file(vault, "receipts", "receipt.png", RECEIPT)
    protected, protected_page = _file(vault, "contracts", "receipt-private.png", RECEIPT, protected=True)
    preview = _convert(vault)
    _convert(vault, dry_run=False, plan_id=preview["plan_id"])
    assert len(list(original_pool.pool_root(vault).rglob("*.blob"))) == 1  # One object holds both filings.
    assert not (vault / ordinary).exists() and not (vault / protected).exists()
    expected = {ordinary: RECEIPT, protected: RECEIPT, ordinary_page: (vault / ordinary_page).read_bytes(),
                protected_page: (vault / protected_page).read_bytes()}
    context = _context()

    exported = vault_portability.export_compatible_vault(vault, tmp_path / "exports", context=context)
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["EXOMEM_STATE_ROOT"] = str(tmp_path / "older-state")
    result = subprocess.run([older, "-c", _OLDER_RESTORE, str(exported.archive_path), str(tmp_path / "older"),
                             context.created_at], env=environment, capture_output=True, text=True, timeout=300)

    assert result.returncode == 0, result.stderr
    restored = json.loads(result.stdout.splitlines()[-1])
    assert {path: restored.get(path) for path in expected} == {
        path: hashlib.sha256(data).hexdigest() for path, data in expected.items()}
    assert not [path for path in restored if original_pool.pool_root(vault).name in path]


def _restore(archive: Path, tmp_path: Path) -> vault_portability.PublishedRestore:
    prepared = vault_portability.prepare_restore(archive, tmp_path / "staging",
                                                 context=_context(lifecycle_state="restore-staging"))
    return vault_portability.publish_prepared_restore(prepared, tmp_path / "restored")


def test_a_capable_restore_brings_back_the_pool_bindings_format_and_cloud_reference(
        vault: Path, selected_store, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    """Catches a restore that serves a pooled archive without its format requirement, so an older runtime that starts
    on it finds pages that name nothing; or one that drops a shared object, a binding or a retained Cloud reference."""
    cached = _stored(vault, "cached", 300)
    cached_bytes = cached.read_bytes()
    _pool(vault, monkeypatch, cached)
    stored = cached.relative_to(vault).as_posix()
    originals_bytes, pages = _converted_filings(vault, monkeypatch)
    originals_bytes[stored] = cached_bytes
    pages[f"{stored}.md"] = (vault / f"{stored}.md").read_bytes()
    reference = originals.original_state(vault, stored).reference
    objects = sorted(path.relative_to(vault) for path in original_pool.pool_root(vault).rglob("*.blob"))
    exported = vault_portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())

    published = _restore(exported.archive_path, tmp_path)

    restored = published.live_root
    values, config = selected_store
    config.write_text(json.dumps({"vaults": {str(vault): values, str(restored): values}}))
    assert published.derived_state == "pending"
    assert state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(restored)
    state_migration.require_vault_state_ready(restored)
    assert sorted(path.relative_to(restored) for path in original_pool.pool_root(restored).rglob("*.blob")) == objects
    with library_scope():
        assert {path: media_store.read_original_bytes(restored, path).data for path in originals_bytes} == originals_bytes
    assert not any((restored / path).exists() for path in originals_bytes)
    assert {page: (restored / page).read_bytes() for page in pages} == pages
    assert reference is not None and originals.original_state(restored, stored).reference == reference


def test_a_pooled_restore_that_fails_after_recording_its_format_succeeds_when_retried(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """Catches a failed pooled restore whose rollback leaves the state it already moved out of the archive behind, so
    the owner's retry of the same restore refuses, or publishes a tree that lacks those bytes."""
    originals_bytes, pages = _converted_filings(vault, monkeypatch)
    # Review state lives in external state and travels in the archive; publication moves it back out of the tree.
    review = json.dumps({"schema_version": 1, "records": {"review": "dismissed"}}).encode()
    (state_paths.vault_state_dir(vault) / ".review-state.json").write_bytes(review)
    exported = vault_portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())
    prepared = vault_portability.prepare_restore(exported.archive_path, tmp_path / "staging",
                                                 context=_context(lifecycle_state="restore-staging"))
    # The journalled restore of an armed archive covers this failure; an unarmed one has no journal.
    assert prepared.manifest["schema_version"] != vault_portability.ARMED_MANIFEST_SCHEMA_VERSION
    record = state_migration.restore_shared_originals_offline

    def lost_after_recording(*args, **kwargs):
        record(*args, **kwargs)
        raise OSError("disk lost after the format was recorded")

    with monkeypatch.context() as failing:
        failing.setattr(state_migration, "restore_shared_originals_offline", lost_after_recording)
        with pytest.raises(vault_portability.PortabilityError, match="PUBLICATION_FAILED"):
            vault_portability.publish_prepared_restore(prepared, tmp_path / "restored")

    restored = vault_portability.publish_prepared_restore(prepared, tmp_path / "restored").live_root

    state_migration.require_vault_state_ready(restored)
    assert state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(restored)
    with library_scope():
        assert {path: media_store.read_original_bytes(restored, path).data for path in originals_bytes} == originals_bytes
    assert {page: (restored / page).read_bytes() for page in pages} == pages
    assert (state_paths.vault_state_dir(restored) / ".review-state.json").read_bytes() == review


def test_a_rerun_of_a_failed_pooled_restore_publishes_into_a_new_folder_and_the_old_folder_names_that_way_out(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """Catches an operator rerun of the documented restore, which needs a new staging folder, that cannot publish
    after an attempt failed past its state migration, or that meets the attempt's leftover state in the old
    restore folder with an error that does not say what to change."""
    originals_bytes, pages = _converted_filings(vault, monkeypatch)
    review = json.dumps({"schema_version": 1, "records": {"review": "dismissed"}}).encode()
    (state_paths.vault_state_dir(vault) / ".review-state.json").write_bytes(review)
    exported = vault_portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())

    def prepared(staging: str) -> vault_portability.PreparedRestore:
        return vault_portability.prepare_restore(exported.archive_path, tmp_path / staging,
                                                 context=_context(lifecycle_state="restore-staging"))

    record = state_migration.restore_shared_originals_offline

    def lost_after_recording(*args, **kwargs):
        record(*args, **kwargs)
        raise OSError("disk lost after the format was recorded")

    with monkeypatch.context() as failing:
        failing.setattr(state_migration, "restore_shared_originals_offline", lost_after_recording)
        with pytest.raises(vault_portability.PortabilityError, match="PUBLICATION_FAILED"):
            vault_portability.publish_prepared_restore(prepared("staging"), tmp_path / "restored")

    restored = vault_portability.publish_prepared_restore(prepared("staging-2"), tmp_path / "restored-2").live_root

    state_migration.require_vault_state_ready(restored)
    with library_scope():
        assert {path: media_store.read_original_bytes(restored, path).data for path in originals_bytes} == originals_bytes
    assert {page: (restored / page).read_bytes() for page in pages} == pages
    assert (state_paths.vault_state_dir(restored) / ".review-state.json").read_bytes() == review
    with pytest.raises(vault_portability.PortabilityError, match="PUBLICATION_FAILED") as refused:
        vault_portability.publish_prepared_restore(prepared("staging-3"), tmp_path / "restored")
    assert "new restore folder" in str(refused.value)
    assert not (tmp_path / "restored").exists()


def test_a_grant_on_one_restored_filing_does_not_reveal_another_that_shares_its_object(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """Catches a restore that resolves access through the shared object, so a reader granted one protected filing
    reads every filing that shares its bytes. The destination owns governance, so the owner grants after restore."""
    from exomem import commands
    from exomem.governance import raw_protection
    from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
    from exomem.writer_lease import invoke_command

    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    granted, _page = _file(vault, "contracts", "first.pdf", INVOICE, protected=True)
    other, _page = _file(vault, "contracts", "second.pdf", INVOICE, protected=True)
    _convert(vault, dry_run=False)
    assert len(list(original_pool.pool_root(vault).rglob("*.blob"))) == 1
    exported = vault_portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context())
    restored = _restore(exported.archive_path, tmp_path).live_root
    govern = next(command for command in commands.PRODUCT_COMMANDS if command.name == "govern_memory")
    reader = RequestPrincipal("external", surface="mcp", issuer_family="test-issuer")

    with request_scope(owner_principal(surface="library")):
        assert invoke_command(govern, restored, operation="grant", scope="standing",
                              grant_id="01ARZ3NDEKTSV4RRFFQ69G5FB0", path=granted, audience=reader.audience_id,
                              raw_release={"version": 1, "surface": reader.surface, "issuer_family": reader.issuer_family,
                                           "purpose": None, "includes_location": True},
                              )["diagnostics"]["operational"]["ok"]

    assert raw_protection.permits(restored, granted, reader) is True
    assert raw_protection.permits(restored, other, reader) is False


def test_a_suffixless_original_evicted_before_pooling_hydrates_to_its_file(
        vault: Path, selected_store, monkeypatch):  # noqa: F811
    """Reading it back must not leave it pool-only, or its companion reads as a note and a restricted caller
    sees its bare name."""
    from exomem import media_jobs, media_store
    from tests.test_media_store import _offload

    captured = preserve.preserve(vault, scope="Test", category="legal", filename="LICENSE", content="x" * 300)
    path = vault / captured.path
    preserve.update_sidecar_extraction(vault, vault / captured.sidecar_path, text="An observation.", engine="text")
    media_jobs.MediaJobStore(vault).retire_ocr(path, "text")
    _offload(vault, path)
    assert media_store.evict(vault, path)
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)

    relative = path.relative_to(vault).as_posix()
    assert media_store.read_original_bytes(vault, relative).data == b"x" * 300

    assert path.is_file()
    assert originals.pooled(vault, relative) is None


def test_a_suffixless_original_of_a_pooled_archive_is_never_offloaded(
        vault: Path, selected_store, monkeypatch):  # noqa: F811
    """An evicted original whose bytes a pooled twin holds reads as pool-only, so its bare name would show."""
    from exomem import media_store

    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    plain = vault / preserve.preserve(vault, scope="Test", category="legal", filename="LICENSE", content="x" * 300).path
    twin = preserve.preserve(vault, scope="Test", category="legal", filename="terms.txt", content="x" * 300).path

    assert originals.original_state(vault, plain).offloadable is False
    assert originals.original_state(vault, vault / twin).offloadable is True
    assert not media_store.evict(vault, plain)
    assert plain.is_file()
