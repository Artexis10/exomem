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
import sqlite3
import uuid
import zipfile
from pathlib import Path

import pytest
from test_document_containers import container_bytes
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


def test_fixity_names_a_pool_object_of_the_wrong_size_instead_of_retiring_its_row(
        vault: Path, tmp_path: Path, monkeypatch) -> None:
    """A truncated shared object makes the binding read fail; retiring the row would stop watching the damaged original."""
    added = _pooled_capture(vault, tmp_path, monkeypatch, "side-a.txt", TRANSCRIPT)
    original_fixity.walk(vault)
    assert original_fixity.run_pass(vault).outcomes == {"verified": 1}

    _damage(vault, hashlib.sha256(TRANSCRIPT).hexdigest(), TRANSCRIPT[:-1])
    result = original_fixity.run_pass(vault)

    assert (result.unreadable, result.outcomes) == (1, {})
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

