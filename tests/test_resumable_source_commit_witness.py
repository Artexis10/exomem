"""Exercise both sides of the delegated Source publication boundary in real processes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from textwrap import dedent

import pytest

from exomem import (
    auth_sessions,
    server_auth,
    server_transfer,
    source_imports,
    upload_handoff,
    upload_sessions,
)
from exomem.governance import principal
from exomem.state_paths import vault_state_dir

_ISSUER = "https://capture.example"
_SIGNER = "synthetic-private-signing-key"

_WORKER = dedent(
    """
    import json
    import os
    import sys
    import time
    from pathlib import Path

    from exomem import (
        auth_sessions, private_state, server_auth, server_transfer, source_imports, upload_sessions,
        vault, writer_lease,
    )
    from exomem.collection_store import connection, preview
    from exomem.collection_store.writer import CollectionWriter
    from exomem.vault_store import mode, write as store_write

    root, login_dir = (Path(value) for value in sys.argv[1:3])
    session_id, secret, cut = sys.argv[3:]
    issuer = os.environ["EXOMEM_BASE_URL"]
    server_auth.build_session_authority = lambda **_: auth_sessions.SessionAuthority.local(
        directory=login_dir, signing_root=os.environ["EXOMEM_JWT_SIGNING_KEY"],
        issuer=issuer, audience=issuer + "/mcp",
    )
    if cut == "before":
        after_real = vault._after_batch_destination_published
        def stop_before_validation(path):
            after_real(path)
            if Path(path).parent == source_imports.receipt_root(root):
                os._exit(71)
        vault._after_batch_destination_published = stop_before_validation
    if cut == "first":
        after_real = vault._after_batch_destination_published
        def stop_after_first_source_file(path):
            after_real(path)
            if Path(path).is_relative_to(vault.kb_root(root) / "Sources"):
                os._exit(74)
        vault._after_batch_destination_published = stop_after_first_source_file
    if cut == "store-first":
        after_real = vault._after_batch_destination_published
        def stop_after_original(path):
            after_real(path)
            published = Path(path)
            if published.is_relative_to(vault.kb_root(root) / "Sources") and published.suffix == ".bin":
                os._exit(74)
        vault._after_batch_destination_published = stop_after_original
    if cut == "store-pool-first":
        from exomem import original_pool
        publish_real = original_pool.PreparedOriginal.publish
        def stop_after_pool_publish(self):
            publish_real(self)
            os._exit(76)
        original_pool.PreparedOriginal.publish = stop_after_pool_publish
    if cut in ("store-after", "store-rollback"):
        commit_real = store_write.commit
        def interrupt_store_receipt(writer, change):
            if not any(source_imports.is_receipt_path(root, put.rel) for put in change.puts):
                return commit_real(writer, change)
            if cut == "store-rollback":
                raise OSError("injected store failure before the Source transaction")
            commit_real(writer, change)
            os._exit(75)
        store_write.commit = interrupt_store_receipt
    if cut == "after":
        log_real = writer_lease.log_active_mutation_phase
        def stop_after_commit(phase, **fields):
            log_real(phase, **fields)
            if phase == "canonical_files_committed":
                os._exit(72)
        writer_lease.log_active_mutation_phase = stop_after_commit
    if cut == "revoked":
        os.environ.pop("EXOMEM_OWNER_OAUTH_SUBJECT")
    if cut == "resume-at-floor":
        from types import SimpleNamespace
        from exomem import media_capacity
        config = Path(os.environ["MEDIA_CONFIG_DIR"]) / "media.json"
        config.write_text(json.dumps({"vaults": {str(root.resolve()): {"store": "test", "upload_floor_bytes": 1024**2}}}))
        os.environ["EXOMEM_MEDIA_STORE_CONFIG"] = str(config)
        # One byte above the floor: no commit of the held original fits.
        media_capacity.shutil.disk_usage = lambda _path: SimpleNamespace(total=100 * 1024**2, free=1024**2 + 1)
        # The final part's reservation, which a parked session must not keep holding.
        from contextlib import closing
        from exomem.state_paths import vault_state_dir
        credits = vault_state_dir(root) / "upload-commit-credit.sqlite"
        credits.parent.mkdir(parents=True, exist_ok=True)
        with closing(media_capacity._open_credits(credits, create=True)) as conn:
            media_capacity._set_credit(conn, session_id, 4096)
    if cut in ("failed-sync", "uncertain-cleanup", "interrupted-seal"):
        witness = upload_sessions.file_commit_witness_path(root, session_id)
        sync_real = private_state.sync_directory
        failed = False
        def fail_witness_sync(path):
            global failed
            if path == witness.parent and witness.is_file() and not failed:
                failed = True
                if cut == "interrupted-seal":
                    os._exit(73)
                raise OSError("witness-directory sync failed")
            sync_real(path)
        private_state.sync_directory = fail_witness_sync
        if cut == "uncertain-cleanup":
            unlink_real = Path.unlink
            def fail_witness_cleanup(path, *args, **kwargs):
                if path == witness and failed:
                    raise OSError("witness unlink failed")
                return unlink_real(path, *args, **kwargs)
            Path.unlink = fail_witness_cleanup

    def commit(part, record):
        if not mode.store_mode_active(root):
            return server_transfer.commit_session(root, part, record)
        with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
            token = preview._BOUND.set((root.resolve(), CollectionWriter(root, handle), False))
            try:
                return server_transfer.commit_session(root, part, record)
            finally:
                preview._BOUND.reset(token)

    session = upload_sessions.open_session(root, session_id, secret)
    upload_sessions.start_commit(session, commit)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = upload_sessions.open_session(root, session_id, secret).view()
        if status["state"] in (upload_sessions.COMMITTED, upload_sessions.FAILED) and session.part.exists():
            # The receipt is durable before the daemon removes its staged bytes. Let it finish.
            time.sleep(0.05)
            continue
        if status["state"] in (upload_sessions.COMMITTED, upload_sessions.FAILED, upload_sessions.RETRYABLE):
            print(json.dumps(status))
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("upload worker did not settle")
    """
)


@pytest.fixture
def sent_source(vault, tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", _SIGNER)
    monkeypatch.setenv("EXOMEM_BASE_URL", _ISSUER)
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "42")
    monkeypatch.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", "github:42")
    monkeypatch.setenv("EXOMEM_UPLOAD_MAX_BYTES", "8")
    login_dir = tmp_path / "logins"
    authority = auth_sessions.SessionAuthority.local(
        directory=login_dir, signing_root=_SIGNER, issuer=_ISSUER, audience=f"{_ISSUER}/mcp",
    )
    monkeypatch.setattr(server_auth, "build_session_authority", lambda **_: authority)
    _, login = asyncio.run(authority.issue(
        client_id="agent", scopes=["mcp"],
        identity=auth_sessions.SessionIdentity(github_user_id=42, github_login="example"),
    ))
    owner = principal.RequestPrincipal(
        audience_id="owner", surface="mcp", issuer_family="mcp-oauth:synthetic", remote_owner=True,
        client_binding=principal.ClientBinding(_ISSUER, login.client_id),
        origin_session=principal.OriginSessionBinding(login.session_id, login.generation, login.audience),
    )
    data = b"Recorded original.\n"
    digest = hashlib.sha256(data).hexdigest()
    with principal.request_scope(owner):
        handoff = upload_handoff.prepare_upload(vault, lane="source", expected_sha256=digest, size_bytes=len(data))
        ticket = asyncio.run(upload_handoff.claim_upload(
            vault, handoff["token"], resumable=True, size_bytes=len(data), sha256=digest,
        ))
        target = {
            "lane": "source", "filename": "recording.bin", "title": "Recorded original",
            "source_type": "session", "raw_protection": False,
        }
        target["destination"] = server_transfer._source_session_destination(vault, target, size=len(data), sha256=digest)
    upload, secret = upload_sessions.create(
        vault, binding=ticket["binding"], length=len(data), sha256=digest,
        target=target, max_bytes=512, source_handoff=ticket["source_handoff"],
    )
    patch = upload_sessions.Patch(upload, 0, len(data))
    patch.write(data)
    assert patch.close() == (len(data), True)
    def worker(cut_at: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", _WORKER, str(vault), str(login_dir), upload.id, secret, cut_at],
            capture_output=True, text=True, check=False, timeout=30,
            env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
        )

    return upload, data, worker


@pytest.mark.parametrize(("cut", "exit_code"), [("before", 71), ("after", 72), ("interrupted-seal", 73)])
def test_a_process_cut_reports_only_the_committed_source_receipt(vault, sent_source, cut, exit_code):
    upload, data, worker = sent_source
    sources = vault / "Knowledge Base" / "Sources"
    earlier_paths = set(sources.rglob("*.md"))
    stopped = worker(cut)
    assert stopped.returncode == exit_code, stopped.stdout + stopped.stderr
    assert len(list(source_imports.receipt_root(vault).glob("*.json"))) == 1
    installed, = set(sources.rglob("*.md")) - earlier_paths
    resumed = worker("resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    status = json.loads(resumed.stdout)
    assert set(sources.rglob("*.md")) - earlier_paths == {installed}
    if cut != "after":
        assert status["state"] == upload_sessions.RETRYABLE and status["code"] == "SOURCE_OUTCOME_UNKNOWN"
        assert "receipt" not in status
        assert upload.part.read_bytes() == data
    else:
        assert status["state"] == upload_sessions.COMMITTED
        receipt = status["receipt"]
        assert receipt["path"] == installed.relative_to(vault).as_posix()
        assert receipt["hash"] == hashlib.sha256(data).hexdigest() and receipt["size"] == len(data)
        assert (vault / receipt["artifact_path"]).read_bytes() == data
        assert not upload.part.exists()


@pytest.mark.parametrize("fault", ["failed-sync", "uncertain-cleanup"])
def test_witness_sync_failure_allows_retry_only_after_proven_cleanup(vault, sent_source, fault):
    upload, data, worker = sent_source
    sources = vault / "Knowledge Base" / "Sources"
    earlier_paths = set(sources.rglob("*.md"))
    refused = worker(fault)
    assert refused.returncode == 0, refused.stdout + refused.stderr
    status = json.loads(refused.stdout)
    assert status["state"] == upload_sessions.RETRYABLE and "receipt" not in status
    assert set(sources.rglob("*.md")) == earlier_paths
    assert not list(source_imports.receipt_root(vault).glob("*.json"))
    assert upload.part.read_bytes() == data
    capture = source_imports.resumable_capture(vault, upload.record)
    assert capture.publication_may_have_started(vault) == (fault == "uncertain-cleanup")
    retried = worker("resume")
    assert retried.returncode == 0, retried.stdout + retried.stderr
    status = json.loads(retried.stdout)
    if fault == "uncertain-cleanup":
        assert status["state"] == upload_sessions.RETRYABLE and status["code"] == "SOURCE_OUTCOME_UNKNOWN"
        assert "receipt" not in status and upload.part.read_bytes() == data
        assert set(sources.rglob("*.md")) == earlier_paths
    else:
        assert status["state"] == upload_sessions.COMMITTED
        receipt = status["receipt"]
        assert set(sources.rglob("*.md")) - earlier_paths == {vault / receipt["path"]}
        assert (vault / receipt["artifact_path"]).read_bytes() == data


def test_displaced_owner_binding_does_not_destroy_the_pending_receipt(vault, sent_source):
    upload, data, worker = sent_source
    stopped = worker("after")
    assert stopped.returncode == 72, stopped.stdout + stopped.stderr
    canonical, = source_imports.receipt_root(vault).glob("*.json")
    receipt = json.loads(canonical.read_text(encoding="utf-8"))

    revoked = worker("revoked")
    assert revoked.returncode == 0, revoked.stdout + revoked.stderr
    status = json.loads(revoked.stdout)
    assert status["state"] == upload_sessions.RETRYABLE and status["code"] == "SOURCE_OUTCOME_UNKNOWN"
    assert "receipt" not in status and upload.part.read_bytes() == data

    restored = worker("resume")
    assert restored.returncode == 0, restored.stdout + restored.stderr
    status = json.loads(restored.stdout)
    assert status["state"] == upload_sessions.COMMITTED
    assert status["receipt"]["ref"] == receipt["ref"]
    assert status["receipt"]["path"] == receipt["page"]
    assert (vault / status["receipt"]["artifact_path"]).read_bytes() == data
    assert list(source_imports.receipt_root(vault).glob("*.json")) == [canonical]


def test_owner_removal_after_first_native_install_keeps_the_bytes(vault, sent_source):
    upload, data, worker = sent_source
    sources = vault / "Knowledge Base" / "Sources"
    earlier_paths = set(sources.rglob("*.md"))
    stopped = worker("first")
    assert stopped.returncode == 74, stopped.stdout + stopped.stderr
    installed, = set(sources.rglob("*.md")) - earlier_paths
    assert not list(source_imports.receipt_root(vault).glob("*.json"))

    displaced = worker("revoked")
    assert displaced.returncode == 0, displaced.stdout + displaced.stderr
    status = json.loads(displaced.stdout)
    assert status["state"] == upload_sessions.RETRYABLE and status["code"] == "SOURCE_OUTCOME_UNKNOWN"
    assert "receipt" not in status and upload.part.read_bytes() == data

    restored = worker("resume")
    assert restored.returncode == 0, restored.stdout + restored.stderr
    status = json.loads(restored.stdout)
    assert status["state"] == upload_sessions.RETRYABLE and status["code"] == "SOURCE_OUTCOME_UNKNOWN"
    assert "receipt" not in status and upload.part.read_bytes() == data
    assert set(sources.rglob("*.md")) - earlier_paths == {installed}
    assert not list(source_imports.receipt_root(vault).glob("*.json"))


def test_owner_removal_before_native_publication_is_a_definitive_failure(vault, sent_source):
    upload, _data, worker = sent_source
    sources = vault / "Knowledge Base" / "Sources"
    earlier_paths = set(sources.rglob("*.md"))
    refused = worker("revoked")
    assert refused.returncode == 0, refused.stdout + refused.stderr
    status = json.loads(refused.stdout)
    assert status["state"] == upload_sessions.FAILED and status["code"] == "UPLOAD_HANDOFF_UNAVAILABLE"
    assert "receipt" not in status and not upload.part.exists()
    assert set(sources.rglob("*.md")) == earlier_paths


@pytest.mark.parametrize(("cut", "exit_code"), [("after", 72), ("first", 74)])
def test_a_source_resumed_at_the_space_floor_keeps_its_receipt_or_unknown_outcome(
    vault, sent_source, tmp_path, monkeypatch, cut, exit_code,
):
    # Publication already spent the original's space, so a full disk must not report it as not preserved.
    monkeypatch.setenv("MEDIA_CONFIG_DIR", str(tmp_path))
    upload, data, worker = sent_source
    stopped = worker(cut)
    assert stopped.returncode == exit_code, stopped.stdout + stopped.stderr
    resumed = worker("resume-at-floor")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    status = json.loads(resumed.stdout)
    if cut == "after":
        assert status["state"] == upload_sessions.COMMITTED
        assert status["receipt"]["hash"] == hashlib.sha256(data).hexdigest()
        assert (vault / status["receipt"]["artifact_path"]).read_bytes() == data
    else:
        assert status["state"] == upload_sessions.RETRYABLE and status["code"] == "SOURCE_OUTCOME_UNKNOWN"
        assert upload.part.read_bytes() == data
    credits = vault_state_dir(vault) / "upload-commit-credit.sqlite"
    with closing(sqlite3.connect(credits)) as conn:
        # The resumed worker's log names any credit cleanup that failed and was deferred to expiry.
        leftover = conn.execute("SELECT peak FROM credits WHERE session = ?", (upload.id,)).fetchone()
        assert leftover is None, resumed.stderr
