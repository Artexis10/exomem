"""A write refusal that reaches a caller is in the mutation journal once, and only
a refusal that reaches a caller moves `exomem_mutation_busy_total`.

Each case holds the vault's mutation boundary from another thread with
`VaultMutationCoordinator.hold`, as a graph drain or another write would, and
drives the real path the refusal travels to its caller.
"""

from __future__ import annotations

import functools
import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_upload_endpoint import _client as upload_client

from exomem import metrics, readiness, server_transfer, upload_sessions, writer_lease
from exomem.cli_ops import OpError
from exomem.mutation_lock import VaultMutationCoordinator


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", "0.05")
    # A keyed write waits out a busy boundary before it refuses; keep that wait short.
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "0.2")
    metrics.reset()
    yield
    metrics.reset()
    readiness.reset()
    writer_lease.reset_managers_for_tests()


@pytest.fixture
def held(vault: Path):
    """The vault's boundary, held by a background holder from another thread."""
    manager = writer_lease.get_manager()
    entered, release = threading.Event(), threading.Event()

    def hold() -> None:
        coordinator = VaultMutationCoordinator(manager.config.state_dir, vault)
        with coordinator.hold(timeout_seconds=2.0, operation="graph_drain", holder_kind="background"):
            entered.set()
            assert release.wait(30)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(15)
    yield manager
    release.set()
    holder.join(30)


def _journal(tmp_path: Path) -> list[dict]:
    path = tmp_path / "logs" / "mutations.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _busy_total() -> int:
    return sum(
        counter["value"]
        for counter in metrics.snapshot()["counters"]
        if counter["name"] == "exomem_mutation_busy_total"
    )


def _write_command():
    return SimpleNamespace(name="mutate", read_only=False, leaf=lambda _vault: "committed")


@pytest.mark.parametrize("key", [None, "keyed-write"], ids=["unkeyed", "keyed"])
def test_a_write_refused_busy_is_journaled_and_counted_once(vault, held, tmp_path, key) -> None:
    """The keyed write waits and refuses from the capture loop; the unkeyed one refuses at once."""
    with pytest.raises(OpError) as refused:
        held.invoke(_write_command(), (vault,), {}, idempotency_key=key, mutation_request_id="req-busy")

    assert refused.value.code == "MUTATION_BUSY"
    (row,) = _journal(tmp_path)
    assert (row["request_id"], row["command"], row["outcome"], row["error_code"]) == (
        "req-busy", "mutate", "failed", "MUTATION_BUSY"
    )
    assert _busy_total() == 1


def test_a_background_holder_refused_does_not_move_the_busy_counter(vault, held, tmp_path) -> None:
    with pytest.raises(OpError) as background:
        with held.mutation_guard(vault, operation="graph_drain", holder_kind="background"):
            pytest.fail("the boundary was held by another thread")
    assert background.value.code == "MUTATION_BUSY"
    assert _busy_total() == 0
    assert _journal(tmp_path) == []

    with pytest.raises(OpError):
        held.invoke(_write_command(), (vault,), {}, mutation_request_id="req-after-drain")
    assert _busy_total() == 1


def test_a_write_refused_while_the_corpus_warms_is_journaled(vault, tmp_path) -> None:
    manager = writer_lease.get_manager()
    readiness.begin_warm()

    with pytest.raises(OpError) as warming:
        manager.invoke(_write_command(), (vault,), {}, mutation_request_id="req-warming")

    assert warming.value.code == "MUTATION_WARMING"
    (row,) = _journal(tmp_path)
    assert (row["request_id"], row["outcome"], row["error_code"]) == ("req-warming", "failed", "MUTATION_WARMING")
    assert row["boundary_wait_ms"] is None and row["boundary_hold_ms"] is None
    assert _busy_total() == 0


def test_an_upload_refused_busy_is_journaled_with_its_boundary_wait(vault, held, tmp_path, monkeypatch) -> None:
    client = upload_client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")

    response = client.post(
        "/upload",
        files={"file": ("held.bin", b"bytes", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers={"Authorization": "Bearer sekret"},
    )

    assert response.status_code == 409 and response.json()["code"] == "MUTATION_BUSY"
    (row,) = _journal(tmp_path)
    assert (row["tool"], row["outcome"], row["error_code"]) == ("upload", "failed", "MUTATION_BUSY")
    assert row["boundary_wait_ms"] > 0
    assert _busy_total() == 1


def _await_state(vault: Path, session: upload_sessions.Session, secret: str, state: str) -> None:
    deadline = time.monotonic() + 30
    while upload_sessions.open_session(vault, session.id, secret).record["state"] != state:
        assert time.monotonic() < deadline, f"the session never reached {state}"
        time.sleep(0.05)


def _verified_session(vault: Path, data: bytes, **target) -> tuple[upload_sessions.Session, str]:
    digest = hashlib.sha256(data).hexdigest()
    session, secret = upload_sessions.create(
        vault, binding="bearer:test", length=len(data), sha256=digest, max_bytes=upload_sessions.DEFAULT_MAX_BYTES,
        target={"filename": "held.bin", "scope": "S", "category": "C", "description": None,
                "raw_protection": False, "archive": None, **target},
    )
    patch = upload_sessions.Patch(session, 0, len(data))
    patch.write(data)
    assert patch.close() == (len(data), True)
    return session, secret


def test_an_upload_session_parked_retryable_on_a_busy_boundary_is_journaled(vault, held, tmp_path) -> None:
    session, secret = _verified_session(vault, b"evidence bytes")

    upload_sessions.start_commit(session, functools.partial(server_transfer.commit_session, vault))
    _await_state(vault, session, secret, upload_sessions.RETRYABLE)

    (row,) = _journal(tmp_path)
    assert (row["tool"], row["outcome"], row["error_code"]) == ("upload_session", "failed", "MUTATION_BUSY")
    assert row["boundary_wait_ms"] > 0
    assert _busy_total() == 1


def _source_session(vault: Path) -> tuple[upload_sessions.Session, str]:
    data = b"source bytes"
    digest = hashlib.sha256(data).hexdigest()
    target = {"lane": "source", "filename": "held.bin", "title": "Held capture", "source_type": "session",
              "domain": None, "url": None, "content_type": None}
    target["destination"] = server_transfer._source_session_destination(vault, target, size=len(data), sha256=digest)
    return _verified_session(vault, data, **target)


def test_a_source_session_parked_on_a_busy_boundary_leaves_one_row_and_one_count(vault, held, tmp_path) -> None:
    """The Source commit runs through the dispatcher, which records the refusal before the park."""
    session, secret = _source_session(vault)

    upload_sessions.start_commit(session, functools.partial(server_transfer.commit_session, vault))
    _await_state(vault, session, secret, upload_sessions.RETRYABLE)

    (row,) = _journal(tmp_path)
    assert (row["tool"], row["error_code"]) == ("upload_source_session", "MUTATION_BUSY")
    assert _busy_total() == 1


def test_a_source_session_resumed_during_warm_up_leaves_one_row(vault, tmp_path) -> None:
    """A restart resumes parked sessions while the corpus warms; the park wraps the gate's refusal."""
    session, secret = _source_session(vault)
    readiness.begin_warm()

    upload_sessions.start_commit(session, functools.partial(server_transfer.commit_session, vault))
    _await_state(vault, session, secret, upload_sessions.RETRYABLE)

    (row,) = _journal(tmp_path)
    assert (row["tool"], row["error_code"]) == ("upload_source_session", "MUTATION_WARMING")
    assert _busy_total() == 0


@pytest.mark.parametrize(
    ("make_session", "tool"),
    [(lambda vault: _verified_session(vault, b"evidence bytes"), "upload_session"),
     (_source_session, "upload_source_session")],
    ids=["evidence", "source"],
)
def test_polling_a_parked_session_does_not_count_the_same_park_again(vault, held, tmp_path, make_session, tool) -> None:
    """The Evidence lane records its park itself; the Source lane records through the dispatcher."""
    session, secret = make_session(vault)
    committer = functools.partial(server_transfer.commit_session, vault)

    upload_sessions.start_commit(session, committer)
    _await_state(vault, session, secret, upload_sessions.RETRYABLE)
    for _ in range(2):  # each poll of a parked session retries it, and the retry parks for the same reason
        upload_sessions.start_commit(upload_sessions.open_session(vault, session.id, secret), committer)
        _await_state(vault, session, secret, upload_sessions.RETRYABLE)

    (row,) = _journal(tmp_path)
    assert (row["tool"], row["error_code"]) == (tool, "MUTATION_BUSY")
    assert _busy_total() == 1
