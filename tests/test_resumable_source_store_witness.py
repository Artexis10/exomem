"""Process stops between a Source's native original and SQLite custody publication."""

import hashlib
import json
from contextlib import closing

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_resumable_source_commit_witness import sent_source  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_read_write import stored  # noqa: F401

from exomem import artifact_lifecycle, original_pool, source_imports, upload_sessions
from exomem.collection_store import connection


def receipt_rows(root, capture):
    rel = (source_imports.receipt_root(root) / f"{capture.key}.json").relative_to(root).as_posix()
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute("SELECT body FROM store_pages WHERE path = ?", (rel,)).fetchall()


def test_sqlite_owner_removal_after_the_original_install_preserves_the_part(stored, sent_source):  # noqa: F811
    upload, data, worker = sent_source
    sources = stored / "Knowledge Base" / "Sources"
    before = set(sources.rglob("*.bin"))
    capture = source_imports.resumable_capture(stored, upload.record)
    stopped = worker("store-first")
    assert stopped.returncode == 74, stopped.stdout + stopped.stderr
    original, = set(sources.rglob("*.bin")) - before
    assert original.read_bytes() == data
    assert receipt_rows(stored, capture) == []

    for step in ("revoked", "resume"):
        restarted = worker(step)
        assert restarted.returncode == 0, restarted.stdout + restarted.stderr
        status = json.loads(restarted.stdout)
        assert status["state"] == upload_sessions.RETRYABLE and status["code"] == "SOURCE_OUTCOME_UNKNOWN"
        assert "receipt" not in status and upload.part.read_bytes() == data
    assert set(sources.rglob("*.bin")) - before == {original}
    assert receipt_rows(stored, capture) == []


def test_sqlite_commit_recovers_the_exact_original_with_an_orphan_intent(stored, sent_source):  # noqa: F811
    upload, data, worker = sent_source
    capture = source_imports.resumable_capture(stored, upload.record)
    stopped = worker("store-after")
    assert stopped.returncode == 75, stopped.stdout + stopped.stderr
    [(body,)] = receipt_rows(stored, capture)
    exact = json.loads(body)
    assert exact["input_digest"] == capture.input_digest
    assert exact["original_sha256"] == hashlib.sha256(data).hexdigest()
    pending = upload_sessions.pending_file_commit_witness(upload_sessions.file_commit_witness_path(stored, upload.id))
    assert pending.exists()

    restarted = worker("resume")
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    status = json.loads(restarted.stdout)
    assert status["state"] == upload_sessions.COMMITTED
    assert status["receipt"]["ref"] == exact["ref"]
    assert (stored / status["receipt"]["artifact_path"]).read_bytes() == data
    assert receipt_rows(stored, capture) == [(body,)]
    assert not pending.exists()


def test_sqlite_rollback_clears_the_intent_and_allows_safe_retry(stored, sent_source):  # noqa: F811
    upload, data, worker = sent_source
    sources = stored / "Knowledge Base" / "Sources"
    before = set(sources.rglob("*.bin"))
    capture = source_imports.resumable_capture(stored, upload.record)
    refused = worker("store-rollback")
    assert refused.returncode == 0, refused.stdout + refused.stderr
    status = json.loads(refused.stdout)
    assert status["state"] == upload_sessions.RETRYABLE and upload.part.read_bytes() == data
    assert receipt_rows(stored, capture) == []
    assert set(sources.rglob("*.bin")) == before
    assert not capture.publication_may_have_started(stored)

    restarted = worker("resume")
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    status = json.loads(restarted.stdout)
    assert status["state"] == upload_sessions.COMMITTED
    assert (stored / status["receipt"]["artifact_path"]).read_bytes() == data
    assert len(receipt_rows(stored, capture)) == 1


@pytest.fixture
def pooled(stored, monkeypatch):  # noqa: F811
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(stored)
    return stored


def test_sqlite_owner_removal_after_pool_publication_preserves_the_part(pooled, sent_source):  # noqa: F811
    upload, data, worker = sent_source
    digest = hashlib.sha256(data).hexdigest()
    capture = source_imports.resumable_capture(pooled, upload.record)
    stopped = worker("store-pool-first")
    assert stopped.returncode == 76, stopped.stdout + stopped.stderr
    assert original_pool.object_path(pooled, digest).read_bytes() == data
    assert receipt_rows(pooled, capture) == []

    revoked = worker("revoked")
    assert revoked.returncode == 0, revoked.stdout + revoked.stderr
    status = json.loads(revoked.stdout)
    assert status["state"] == upload_sessions.RETRYABLE and status["code"] == "SOURCE_OUTCOME_UNKNOWN"
    assert "receipt" not in status and upload.part.read_bytes() == data
