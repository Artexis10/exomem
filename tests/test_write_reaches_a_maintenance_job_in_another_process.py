"""An agent write queued behind a drain thread still reaches a maintenance job in another process.

A holder steps aside for a writer it can see. The writer that is first in line for the boundary in
its process raises the cross-process signal; a writer behind a drain thread of its own process used
to raise none, so a maintenance job in another process kept the boundary until its phase ended and
the write failed with MUTATION_BUSY.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import threading
import time
from pathlib import Path

import pytest
from fastmcp import Client
from test_import_yields_to_interactive_writes import NOTE, _busy_total, _journal

from exomem import server, writer_lease

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
    assert note["state"] == "committed", note
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
