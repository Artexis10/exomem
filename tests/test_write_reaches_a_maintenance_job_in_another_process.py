"""Writes reach a holder of the vault's mutation boundary in another process.

A maintenance job steps aside for a writer it can see. The writer that is first in line for the
boundary in its process raises the cross-process signal; a writer behind a drain thread of its own
process used to raise none, so a maintenance job in another process kept the boundary until its
phase ended and the write failed with MUTATION_BUSY.

A holder that does not step aside, such as a long command in a second process, keeps the boundary
for its whole turn. A write that carries a retry identity (MCP, REST with an `Idempotency-Key`, a
Studio action, the CLI, `/upload`, tus) waits that turn out and commits once; a REST write with no
key is refused after one bounded wait, with one refusal-journal row.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastmcp import Client
from test_import_yields_to_interactive_writes import NOTE, _busy_total, _journal
from test_upload_sessions import _client, _create, _patch, _wait_for

from exomem import server, upload_sessions, upload_tokens, writer_lease

ITEMS = 60
UNIT_SECONDS = 0.1
# The write's whole wait is shorter than the job's phase, so only the job stepping aside can serve it.
CAPTURE_WAIT_SECONDS = 3
BOUNDARY_ATTEMPT_SECONDS = 2


def _foreign_maintenance_phase(vault_root: str, applied_path: str, started, items: int) -> None:
    """A reconcile phase of a maintenance job, as the CLI runs it in its own process."""
    from exomem import maintenance_jobs

    # A job names its holds `maintenance-job` through its run id, and a hold ends for a waiting writer.
    maintenance_jobs.current_run_id = lambda: "foreign-run"
    maintenance_jobs.HOLD_SECONDS = 3600.0

    def apply(chunk) -> None:
        with open(applied_path, "a", encoding="utf-8") as applied:
            applied.write(f"{chunk[0]}\n")
        started.set()
        time.sleep(UNIT_SECONDS)

    maintenance_jobs.apply_in_holds(
        Path(vault_root), "reconcile_units", list(range(items)), apply, batch=1
    )


def test_a_write_behind_a_drain_thread_commits_while_a_job_in_another_process_holds(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(logs))
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", str(CAPTURE_WAIT_SECONDS))
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", str(BOUNDARY_ATTEMPT_SECONDS))
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    writer_lease.reset_managers_for_tests()
    applied_path = tmp_path / "applied.txt"

    context = multiprocessing.get_context("spawn")
    started = context.Event()
    job = context.Process(
        target=_foreign_maintenance_phase, args=(str(vault), str(applied_path), started, ITEMS)
    )
    # The drain thread waits longer than the write does, so it is still first in line when the write queues.
    drain_manager = writer_lease.LeaseManager(
        writer_lease.get_manager().config, mutation_timeout_seconds=30.0
    )
    drained: list[str] = []

    def drain() -> None:
        with drain_manager.consistency_guard(vault, operation="drain_turn", holder_kind="derived-worker"):
            drained.append("held")

    async def journey() -> dict:
        async with Client(server.build_server(require_auth=False)) as client:
            drain_thread = threading.Thread(target=drain)
            drain_thread.start()
            await asyncio.sleep(0.5)  # the drain thread is polling the boundary behind the job
            began = time.monotonic()
            result = await client.call_tool("create_note", {
                "title": "Agent write behind a drain thread", "content": NOTE.format(name="agent"),
            }, raise_on_error=False)
            await asyncio.to_thread(drain_thread.join, 30)
            note = result.structured_content
            note["elapsed"] = time.monotonic() - began
            return note

    busy_before = _busy_total()
    job.start()
    try:
        assert started.wait(60), "the maintenance job never took the boundary"
        note = asyncio.run(journey())
    finally:
        job.join(timeout=60)
        if job.is_alive():
            job.kill()

    assert "MUTATION_BUSY" not in str(note), note
    assert [row for row in _journal(logs) if row["error_code"] == "MUTATION_BUSY"] == []
    assert _busy_total() == busy_before
    assert "error" not in note and note.get("id"), note
    assert note["title"] == "Agent write behind a drain thread", note
    assert note["elapsed"] < CAPTURE_WAIT_SECONDS, note["elapsed"]
    assert drained == ["held"]
    written = [
        path for path in (vault / "Knowledge Base" / "Notes").rglob("*.md")
        if "Agent write behind a drain thread" in path.read_text(encoding="utf-8")
    ]
    assert len(written) == 1, written
    # The job finished every item once across its holds.
    assert job.exitcode == 0
    assert sorted(int(line) for line in applied_path.read_text(encoding="utf-8").split()) == list(range(ITEMS))


# A second process keeps the boundary this long, and does not step aside for a writer.
HOLD_SECONDS = 4.0
# One acquire attempt ends well before the hold does, so a write that carries a retry identity
# must wait the holder out; its own wait is longer than the hold.
ACQUIRE_ATTEMPT_SECONDS = 1.0
KEYED_WAIT_SECONDS = 15


def _foreign_holder(vault_root: str, started, hold_seconds: float) -> None:
    """A long command in its own process: it takes the boundary and keeps it for its whole turn."""
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig.from_env())
    with manager.mutation_guard(Path(vault_root), request_id="foreign-holder", operation="semantic edit"):
        started.set()
        time.sleep(hold_seconds)


@pytest.fixture
def held_elsewhere(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The vault with its own state root and journal, its boundary held by a second process; yields the journal directory.

    Everything the clients read from the environment is set before the holder starts, because it inherits it.
    """
    logs = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(logs))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", str(KEYED_WAIT_SECONDS))
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", str(ACQUIRE_ATTEMPT_SECONDS))
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    writer_lease.reset_managers_for_tests()
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    holder = context.Process(target=_foreign_holder, args=(str(vault), started, HOLD_SECONDS))
    holder.start()
    try:
        assert started.wait(60), "the second process never took the boundary"
        yield logs
    finally:
        holder.join(timeout=60)
        if holder.is_alive():
            holder.kill()
    assert holder.exitcode == 0


def _pages_with_text(folder: Path, text: str) -> list[Path]:
    return [path for path in folder.rglob("*.md") if text in path.read_text(encoding="utf-8")]


def _files_with_bytes(vault: Path, data: bytes) -> list[Path]:
    return [path for path in vault.rglob("*") if path.is_file() and path.stat().st_size == len(data) and path.read_bytes() == data]


def _bearer() -> dict[str, str]:
    return {"Authorization": "Bearer sekret"}


def _write_over_mcp(vault: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    async def journey() -> dict:
        async with Client(server.build_server(require_auth=False)) as client:
            result = await client.call_tool("create_note", {
                "title": "Mcp write beside a foreign holder", "content": NOTE.format(name="mcp"),
            }, raise_on_error=False)
            return result.structured_content

    reply = asyncio.run(journey())
    assert "error" not in reply and reply.get("id"), reply
    return "Knowledge Base/Notes", "Mcp write beside a foreign holder"


def _write_over_rest_with_a_key(vault: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    response = _client(vault, monkeypatch).request(
        "POST", "/api/manage_memory_file",
        json={"operation": "create", "path": "Handbooks/rest-beside-a-foreign-holder.md",
              "content": "# Rest write beside a foreign holder\n\nWritten with a key.\n"},
        headers={**_bearer(), "Idempotency-Key": "cross-process-rest"},
    )
    assert response.status_code == 200, response.text
    return "Knowledge Base/Handbooks", "Rest write beside a foreign holder"


def _write_from_studio(vault: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    # Studio posts each action to `/api/<command>` with a fresh key of its own (`studio/api.v1.js`).
    response = _client(vault, monkeypatch).request(
        "POST", "/api/remember",
        json={"title": "Studio write beside a foreign holder", "content": NOTE.format(name="studio")},
        headers={**_bearer(), "Accept": "application/json", "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 200, response.text
    return "Knowledge Base/Notes", "Studio write beside a foreign holder"


def _write_from_the_cli(vault: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    cli = subprocess.run(
        [sys.executable, "-m", "exomem", "create_note", "--json",
         "--title", "Cli write beside a foreign holder", "--content", NOTE.format(name="CLI")],
        env={**os.environ, "PYTHONPATH": str(Path(server.__file__).parents[1])},
        capture_output=True, text=True, timeout=120,
    )
    assert cli.returncode == 0 and json.loads(cli.stdout)["success"] is True, (cli.stdout, cli.stderr)
    return "Knowledge Base/Notes", "Cli write beside a foreign holder"


def _upload_one_part(vault: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    token = upload_tokens.mint("sekret", scope=upload_tokens.upload_scope("evidence"))
    data = b"bytes posted to /upload while a second process holds the boundary"
    response = _client(vault, monkeypatch).request(
        "POST", "/upload",
        files={"file": ("held.bin", data, "application/octet-stream")},
        data={"scope": "Held", "category": "Wait", "title": "Held upload"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201, response.text
    return data


def _upload_in_a_tus_session(vault: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    client = _client(vault, monkeypatch)
    data = b'{"samples": [{"heart_rate": 62}], "note": "sent over tus beside a foreign holder"}'
    location, secret = _create(client, data)
    session_id = location.rsplit("/", 1)[1]
    assert _patch(client, location, secret, 0, data).status_code == 204
    # No request reads the session from here on: the server commits it.
    _wait_for(
        lambda: upload_sessions.open_session(vault, session_id, secret).record["state"] == upload_sessions.COMMITTED,
        "the session's commit", seconds=30,
    )
    return data


@pytest.mark.parametrize("door, write, kind", [
    pytest.param("mcp", _write_over_mcp, "note", id="mcp-with-no-key"),
    pytest.param("rest", _write_over_rest_with_a_key, "note", id="rest-with-an-idempotency-key"),
    pytest.param("studio", _write_from_studio, "note", id="a-studio-action"),
    pytest.param("cli", _write_from_the_cli, "note", id="the-cli"),
    pytest.param("upload", _upload_one_part, "bytes", id="upload"),
    pytest.param("tus", _upload_in_a_tus_session, "bytes", id="tus"),
])
def test_a_write_waits_out_a_holder_in_another_process_and_commits_once(
    vault: Path, held_elsewhere: Path, monkeypatch: pytest.MonkeyPatch, door: str, write, kind: str
) -> None:
    """Catches a door that gives up on a busy boundary its holder keeps past one acquire attempt, or commits twice."""
    busy_before = _busy_total()
    began = time.monotonic()

    written = write(vault, monkeypatch)

    # The boundary was held: the write could only commit once the holder let go.
    assert time.monotonic() - began >= HOLD_SECONDS - 1.5, door
    if kind == "note":
        folder, text = written
        assert len(_pages_with_text(vault / folder, text)) == 1, door
    else:
        assert len(_files_with_bytes(vault, written)) == 1, door
    assert [row for row in _journal(held_elsewhere) if row["error_code"] == "MUTATION_BUSY"] == []
    assert _busy_total() == busy_before


def test_a_rest_write_with_no_key_is_refused_retryable_after_one_acquire_wait_with_one_journal_row(
    vault: Path, held_elsewhere: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an unkeyed write that waits out the holder, which no retry identity could make safe, or that leaves no journal row."""
    client = _client(vault, monkeypatch)
    began = time.monotonic()

    response = client.request(
        "POST", "/api/manage_memory_file",
        json={"operation": "create", "path": "Handbooks/rest-without-a-key.md",
              "content": "# Rest write without a key\n\nNot written.\n"},
        headers=_bearer(),
    )

    elapsed = time.monotonic() - began
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert (error["code"], error["status"], error["committed"]) == ("MUTATION_BUSY", "retryable", False), error
    # One acquire attempt, not the holder's whole turn.
    assert ACQUIRE_ATTEMPT_SECONDS <= elapsed < HOLD_SECONDS - 1, elapsed
    assert [row["error_code"] for row in _journal(held_elsewhere)] == ["MUTATION_BUSY"]
    assert not (vault / "Knowledge Base" / "Handbooks" / "rest-without-a-key.md").exists()
