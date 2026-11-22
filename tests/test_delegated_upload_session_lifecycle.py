"""Durable offsets, shared admission, and expiry at the Source session boundary."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from test_upload_sessions import _listening

from exomem import server, upload_sessions

_APPEND = """
import os
import sys
from pathlib import Path

from exomem import upload_sessions

session = upload_sessions.open_session(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
data = Path(sys.argv[4]).read_bytes()
patch = upload_sessions.Patch(session, 0, len(data))
patch.write(data)
if sys.argv[5] == "cut":
    os._exit(73)
print("written", flush=True)
sys.stdin.readline()
patch.close()
"""
# A single write must exceed Python's maximum automatic file buffer.
_PART = b"a" * (8 * 1024 * 1024 + 1)


def _create(vault: Path, data: bytes, *, source: bool = True) -> tuple[upload_sessions.Session, str]:
    return upload_sessions.create(
        vault,
        binding="one-binding",
        length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        target={"filename": "sample.bin"},
        max_bytes=upload_sessions.DEFAULT_MAX_BYTES,
        source_handoff={"binding": "one-binding"} if source else None,
    )


def _committed(vault: Path, session: upload_sessions.Session, secret: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            current = upload_sessions.open_session(vault, session.id, secret)
        except upload_sessions.SessionError as exc:
            if exc.status != 410:
                raise
        else:
            if current.record["state"] == upload_sessions.COMMITTED and not current.part.exists():
                return current.view()["receipt"]
        time.sleep(0.02)
    pytest.fail("the commit did not settle")


def test_status_reports_only_accepted_bytes_while_another_process_appends(vault, tmp_path, monkeypatch):
    data = _PART
    session, secret = _create(vault, data + b"tail")
    payload = tmp_path / "payload"
    payload.write_bytes(data)
    monkeypatch.setattr(upload_sessions, "_LOCK_WAIT_SECONDS", 0.1)

    with subprocess.Popen(
        [sys.executable, "-c", _APPEND, str(vault), session.id, secret, str(payload), "pause"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as writer:
        try:
            assert writer.stdout.readline().strip() == "written"
            assert session.part.stat().st_size == len(data)

            current = upload_sessions.open_session(vault, session.id, secret)
            assert current.view()["offset"] == 0
            with pytest.raises(upload_sessions.SessionError) as refused:
                upload_sessions.Patch(current, len(data), 4)
            assert refused.value.code == "UPLOAD_SESSION_BUSY"
        finally:
            _, errors = writer.communicate(input="continue\n", timeout=10)
        assert writer.returncode == 0, errors

    accepted = upload_sessions.open_session(vault, session.id, secret)
    assert accepted.view()["offset"] == len(data)
    assert session.part.read_bytes() == data


def test_recovery_syncs_a_complete_part_before_acknowledging_or_committing(vault, tmp_path, monkeypatch):
    data = _PART
    session, secret = _create(vault, data)
    payload = tmp_path / "payload"
    payload.write_bytes(data)
    stopped = subprocess.run(
        [sys.executable, "-c", _APPEND, str(vault), session.id, secret, str(payload), "cut"],
        capture_output=True, text=True, timeout=10,
    )
    assert stopped.returncode == 73, stopped.stderr
    assert session.part.read_bytes() == data

    original_fsync = os.fsync
    part_id = session.part.stat().st_ino
    entered = threading.Event()
    captured = []

    def part_sync_unavailable(descriptor: int) -> None:
        if os.fstat(descriptor).st_ino == part_id:
            raise OSError("cannot sync upload part")
        original_fsync(descriptor)

    def commit(part: Path, record: dict) -> dict:
        captured.append(part.read_bytes())
        entered.set()
        return {"hash": record["sha256"]}

    with monkeypatch.context() as outage:
        outage.setattr(os, "fsync", part_sync_unavailable)
        uploading = upload_sessions.open_session(vault, session.id, secret)
        upload_sessions.start_commit(uploading, commit)
        assert uploading.view()["offset"] == 0
        assert not entered.is_set()

    recovered = upload_sessions.open_session(vault, session.id, secret)
    assert upload_sessions.held_offset(recovered) == len(data)
    upload_sessions.start_commit(recovered, commit)
    assert entered.wait(10)
    assert _committed(vault, session, secret) == {"hash": hashlib.sha256(data).hexdigest()}
    assert captured == [data]


def test_expiry_preserves_the_bytes_and_receipt_of_an_admitted_commit(vault, monkeypatch):
    data = b"sample"
    session, secret = _create(vault, data)
    patch = upload_sessions.Patch(session, 0, len(data))
    patch.write(data)
    patch.close()
    entered = threading.Event()
    release = threading.Event()
    captured = []
    witness = upload_sessions.file_commit_witness_path(vault, session.id)
    pending = upload_sessions.pending_file_commit_witness(witness)

    def commit(part: Path, record: dict) -> dict:
        witness.write_bytes(b"receipt proof")
        pending.touch()
        entered.set()
        if not release.wait(10):
            raise RuntimeError("the expiry probe stalled")
        captured.append(part.read_bytes())
        return {"hash": record["sha256"]}

    upload_sessions.start_commit(session, commit)
    assert entered.wait(10)
    try:
        later = session.expires + 1
        monkeypatch.setattr(upload_sessions, "_now", lambda: later)
        with pytest.raises(upload_sessions.SessionError) as expired:
            upload_sessions.open_session(vault, session.id, secret)
        assert expired.value.status == 410
        upload_sessions.startup_sweep(vault)
        assert session.part.read_bytes() == data
        assert witness.read_bytes() == b"receipt proof"
        assert pending.exists()
    finally:
        release.set()
    assert _committed(vault, session, secret) == {"hash": hashlib.sha256(data).hexdigest()}
    assert captured == [data]
    assert not witness.exists()
    assert not pending.exists()


def test_open_sessions_share_admission_with_legacy_and_release_the_slot_on_commit(vault):
    data = b"sample"
    for _ in range(upload_sessions.MAX_LIVE_PER_BINDING - 1):
        _create(vault, data, source=False)
    session, secret = _create(vault, data)
    with pytest.raises(upload_sessions.SessionError) as refused:
        _create(vault, data)
    assert refused.value.code == "UPLOAD_SESSION_QUOTA"

    patch = upload_sessions.Patch(session, 0, len(data))
    patch.write(data)
    patch.close()
    upload_sessions.start_commit(session, lambda part, record: {"hash": record["sha256"]})

    assert _committed(vault, session, secret) == {"hash": hashlib.sha256(data).hexdigest()}
    _create(vault, data)


def test_serving_runtime_reclaims_abandoned_parts_and_receipts_without_a_request(vault, monkeypatch):
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(upload_sessions, "_SWEEP_INTERVAL_SECONDS", 0.02, raising=False)
    monkeypatch.setattr(upload_sessions, "_SWEEP_BATCH_PAUSE_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(upload_sessions, "_SWEEP_BATCH_SIZE", 2, raising=False)
    mcp = server.build_server(require_auth=False)

    with _listening(mcp.http_app()) as (host, port), httpx.Client(
        base_url=f"http://{host}:{port}", trust_env=False, timeout=10,
    ) as client:
        assert client.get("/health").status_code == 200
        activation = mcp._exomem_local_runtime_activation
        startup_deadline = time.monotonic() + 10
        while activation._thread is None:
            assert time.monotonic() < startup_deadline, "the runtime did not activate"
            time.sleep(0.02)
        activation._thread.join(20)
        assert not activation._thread.is_alive()

        data = b"sample"
        legacy, _ = _create(vault, data, source=False)
        delegated, _ = _create(vault, data)
        for abandoned in (legacy, delegated):
            patch = upload_sessions.Patch(abandoned, 0, 3)
            patch.write(data[:3])
            patch.close()
            assert abandoned.part.read_bytes() == data[:3]

        finished, secret = _create(vault, data)
        patch = upload_sessions.Patch(finished, 0, len(data))
        patch.write(data)
        patch.close()
        upload_sessions.start_commit(finished, lambda part, record: {"hash": record["sha256"]})
        assert _committed(vault, finished, secret) == {"hash": hashlib.sha256(data).hexdigest()}
        receipt, = finished.store.rglob(f"{finished.id}.json")

        active, secret = _create(vault, data)
        patch = upload_sessions.Patch(active, 0, len(data))
        patch.write(data)
        patch.close()
        entered, release = threading.Event(), threading.Event()

        def commit(part: Path, record: dict) -> dict:
            entered.set()
            if not release.wait(15):
                raise RuntimeError("the expiry probe stalled")
            assert part.read_bytes() == data
            return {"hash": record["sha256"]}

        upload_sessions.start_commit(active, commit)
        assert entered.wait(10)
        try:
            later = upload_sessions._now() + upload_sessions.TTL_SECONDS + 1
            monkeypatch.setattr(upload_sessions, "_now", lambda: later)
            deadline = time.monotonic() + 4
            while legacy.part.exists() or delegated.part.exists() or receipt.exists():
                assert time.monotonic() < deadline, "the serving runtime left expired uploads on disk"
                time.sleep(0.02)
            assert active.part.read_bytes() == data
        finally:
            release.set()
        assert _committed(vault, active, secret) == {"hash": hashlib.sha256(data).hexdigest()}
