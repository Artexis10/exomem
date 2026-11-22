"""Exercise both sides of the delegated Source publication boundary in real processes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from exomem import auth_sessions, server_auth, server_transfer, source_imports, upload_handoff, upload_sessions
from exomem.governance import principal

_ISSUER = "https://capture.example"
_SIGNER = "synthetic-private-signing-key"

_WORKER = dedent(
    """
    import json
    import os
    import sys
    import time
    from pathlib import Path

    from exomem import auth_sessions, server_auth, server_transfer, source_imports, upload_sessions, vault, writer_lease

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
    if cut == "after":
        log_real = writer_lease.log_active_mutation_phase
        def stop_after_commit(phase, **fields):
            log_real(phase, **fields)
            if phase == "canonical_files_committed":
                os._exit(72)
        writer_lease.log_active_mutation_phase = stop_after_commit

    session = upload_sessions.open_session(root, session_id, secret)
    upload_sessions.start_commit(session, lambda part, record: server_transfer.commit_session(root, part, record))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = upload_sessions.open_session(root, session_id, secret).view()
        if status["state"] == upload_sessions.COMMITTED and session.part.exists():
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


@pytest.mark.parametrize(("cut", "exit_code"), [("before", 71), ("after", 72)])
def test_a_process_cut_reports_only_the_committed_source_receipt(vault, tmp_path, monkeypatch, cut, exit_code):
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
    sources = vault / "Knowledge Base" / "Sources"
    earlier_paths = set(sources.rglob("*.md"))

    def worker(cut_at: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", _WORKER, str(vault), str(login_dir), upload.id, secret, cut_at],
            capture_output=True, text=True, check=False, timeout=30,
            env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
        )

    stopped = worker(cut)
    assert stopped.returncode == exit_code, stopped.stdout + stopped.stderr
    assert len(list(source_imports.receipt_root(vault).glob("*.json"))) == 1
    installed, = set(sources.rglob("*.md")) - earlier_paths
    resumed = worker("resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    status = json.loads(resumed.stdout)
    assert set(sources.rglob("*.md")) - earlier_paths == {installed}
    if cut == "before":
        assert status["state"] == upload_sessions.RETRYABLE and status["code"] == "SOURCE_OUTCOME_UNKNOWN"
        assert "receipt" not in status
        assert upload.part.read_bytes() == data
    else:
        assert status["state"] == upload_sessions.COMMITTED
        receipt = status["receipt"]
        assert receipt["path"] == installed.relative_to(vault).as_posix()
        assert receipt["hash"] == digest and receipt["size"] == len(data)
        assert (vault / receipt["artifact_path"]).read_bytes() == data
        assert not upload.part.exists()
