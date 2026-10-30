"""A remote `backfill-ids` write runs as a governed maintenance job.

The workflow: the owner previews on `check_memory`, starts the previewed plan on
`repair_memory` with the preview's `plan_id` and a reason, then reads the job on
`check_memory` until it ends. These tests drive a real in-process MCP server over a
copy of the fixture vault, so the admission gate, the writer lease, the job store
and the runner thread are all real. Only a second writer (a thread holding the
mutation boundary) and a second process (a killed job owner) are staged.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastmcp import Client

from exomem import capabilities, commands, maintenance_jobs, writer_lease
from exomem import server as server_module
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.governance import principal as principal_module
from exomem.memory_refs import ID_FIELD

_DEADLINE_SECONDS = 60.0
_OTHER_ID = "0" * 64


@pytest.fixture
def server(vault, monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    yield server_module.build_server(require_auth=False)
    writer_lease.reset_managers_for_tests()


def _session(server, scenario):
    async def run():
        async with Client(server) as client:
            return await scenario(client)

    return asyncio.run(run())


async def _call(client, tool, operation):
    result = await client.call_tool(tool, {"operation": operation}, raise_on_error=False)
    return result.structured_content


async def _preview(client) -> dict:
    return await _call(client, "check_memory", {"kind": "backfill-ids"})


async def _start(client, plan_id: str, why: str = "give pages their identity") -> dict:
    return await _call(
        client,
        "repair_memory",
        {"kind": "backfill-ids", "dry_run": False, "plan_id": plan_id, "why": why},
    )


async def _status(client, run_id: str) -> dict:
    return await _call(client, "check_memory", {"kind": "maintenance-job", "run_id": run_id})


async def _list(client) -> dict:
    return await _call(client, "check_memory", {"kind": "maintenance-job"})


async def _cancel(client, run_id: str) -> dict:
    return await _call(
        client, "repair_memory", {"kind": "maintenance-job-cancel", "run_id": run_id, "why": "stop it"}
    )


async def _until(client, run_id: str, done) -> dict:
    """Read the job until `done(status)` holds."""
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while True:
        status = await _status(client, run_id)
        if done(status):
            return status
        assert time.monotonic() < deadline, f"job still at {status.get('state')}/{status.get('phase')}"
        await asyncio.sleep(0.05)


async def _until_terminal(client, run_id: str) -> dict:
    return await _until(client, run_id, lambda status: status.get("state") != "running")


@contextmanager
def _boundary_held(vault: Path) -> Iterator[None]:
    """Another writer holds the mutation boundary, as a long edit would."""
    holding, release = threading.Event(), threading.Event()

    def hold() -> None:
        with writer_lease.get_manager().mutation_guard(
            vault, request_id="other-writer", operation="semantic edit"
        ):
            holding.set()
            release.wait(_DEADLINE_SECONDS)

    thread = threading.Thread(target=hold)
    thread.start()
    assert holding.wait(_DEADLINE_SECONDS)
    try:
        yield
    finally:
        release.set()
        thread.join(_DEADLINE_SECONDS)


def _page_ids(vault: Path) -> dict[str, str | None]:
    """Each governed page's `exomem_id`, or None, keyed by vault-relative path."""
    ids: dict[str, str | None] = {}
    for path in sorted((vault / "Knowledge Base").rglob("*.md")):
        frontmatter, _, text = vault_module.parse_frontmatter(path.read_text(encoding="utf-8"))
        if text is not None:
            ids[path.relative_to(vault).as_posix()] = frontmatter.get(ID_FIELD)
    return ids


def _jobs(client_result: dict) -> list[dict]:
    return client_result["jobs"]


def test_remote_backfill_assigns_exactly_the_previewed_pages(server, vault) -> None:
    async def scenario(client):
        preview = await _preview(client)
        handle = await _start(client, preview["plan_id"])
        final = await _until_terminal(client, handle["run_id"])
        return preview, handle, final

    before = _page_ids(vault)
    preview, handle, final = _session(server, scenario)
    after = _page_ids(vault)

    assert handle["state"] == "running"
    assert handle["plan_id"] == preview["plan_id"]
    assert final["state"] == "completed"
    previewed = set(preview["would_update"])
    assert all(before[path] is None and after[path] is not None for path in previewed)
    assert {p: i for p, i in after.items() if p not in previewed} == {p: i for p, i in before.items() if p not in previewed}
    minted = {path: ref for chunk in final["receipt"]["chunks"] for path, ref in chunk["minted"].items()}
    assert set(minted) == previewed
    assert {path: f"exomem://memory/{after[path]}" for path in previewed} == minted
    assert final["progress"]["done"] == final["progress"]["total"] == len(previewed)
    assert final["committed"] == len(previewed)


def test_start_without_plan_and_reason_is_refused_and_leaves_no_job(server, vault) -> None:
    async def scenario(client):
        refused = await _call(client, "repair_memory", {"kind": "backfill-ids", "dry_run": False})
        return refused, await _list(client)

    before = _page_ids(vault)
    refused, listed = _session(server, scenario)

    assert refused["error"]["code"] == "MAINTENANCE_PLAN_REQUIRED"
    assert refused["error"]["committed"] is False
    assert _jobs(listed) == []
    assert _page_ids(vault) == before


def test_start_succeeds_while_another_writer_holds_the_boundary(server, vault) -> None:
    async def scenario(client):
        preview = await _preview(client)
        with _boundary_held(vault):
            handle = await _start(client, preview["plan_id"])
            running = await _status(client, handle["run_id"])
        return preview, handle, running, await _until_terminal(client, handle["run_id"])

    preview, handle, running, final = _session(server, scenario)

    assert handle["state"] == "running"
    assert running["state"] == "running"
    assert final["state"] == "completed"
    assert all(_page_ids(vault)[path] is not None for path in preview["would_update"])


def test_a_page_edited_after_the_preview_stops_the_job_with_nothing_written(server, vault) -> None:
    async def scenario(client):
        preview = await _preview(client)
        (vault / preview["would_update"][0]).write_text(
            (vault / preview["would_update"][0]).read_text(encoding="utf-8") + "\nAn edit after the preview.\n",
            encoding="utf-8",
        )
        handle = await _start(client, preview["plan_id"])
        return preview, await _until_terminal(client, handle["run_id"])

    before = _page_ids(vault)
    preview, final = _session(server, scenario)

    assert final["state"] == "failed"
    assert final["error_code"] == "STALE_MAINTENANCE_PLAN"
    assert final["receipt"]["error"]["details"]["plan_id"] != preview["plan_id"]
    assert final["committed"] == 0
    assert _page_ids(vault) == before


def test_a_page_edited_while_the_job_waits_for_the_boundary_keeps_its_edit(server, vault) -> None:
    async def scenario(client):
        preview = await _preview(client)
        edited = vault / preview["would_update"][0]
        with _boundary_held(vault):
            handle = await _start(client, preview["plan_id"])
            await _until(client, handle["run_id"], lambda status: status.get("phase") == "committing")
            edited.write_text(edited.read_text(encoding="utf-8") + "\nAn edit during the wait.\n", encoding="utf-8")
        return preview, edited, await _until_terminal(client, handle["run_id"])

    preview, edited, final = _session(server, scenario)

    assert final["state"] == "completed"
    assert final["skipped_changed"] == 1
    assert final["committed"] == len(preview["would_update"]) - 1
    assert "An edit during the wait." in edited.read_text(encoding="utf-8")
    assert _page_ids(vault)[edited.relative_to(vault).as_posix()] is None


def test_a_different_start_while_a_job_runs_is_refused_with_the_running_job(server, vault) -> None:
    async def scenario(client):
        preview = await _preview(client)
        with _boundary_held(vault):
            running = await _start(client, preview["plan_id"])
            refused = await _start(client, _OTHER_ID)
            cancelled = await _cancel(client, running["run_id"])
        await _until_terminal(client, running["run_id"])
        return running, refused, cancelled

    running, refused, _ = _session(server, scenario)

    assert refused["error"]["code"] == "MAINTENANCE_JOB_ACTIVE"
    assert refused["error"]["run_id"] == running["run_id"]


def test_the_same_start_while_a_job_runs_returns_that_job(server, vault) -> None:
    async def scenario(client):
        preview = await _preview(client)
        with _boundary_held(vault):
            first = await _start(client, preview["plan_id"])
            again = await _start(client, preview["plan_id"])
        return first, again, await _until_terminal(client, first["run_id"]), await _list(client)

    first, again, final, listed = _session(server, scenario)

    assert again["run_id"] == first["run_id"]
    assert final["state"] == "completed"
    assert len(_jobs(listed)) == 1


def test_cancel_before_the_first_chunk_ends_the_job_cancelled_with_nothing_written(server, vault) -> None:
    async def scenario(client):
        preview = await _preview(client)
        with _boundary_held(vault):
            handle = await _start(client, preview["plan_id"])
            cancel = await _cancel(client, handle["run_id"])
        return cancel, await _until_terminal(client, handle["run_id"])

    before = _page_ids(vault)
    cancel, final = _session(server, scenario)

    assert cancel["cancel_requested"] is True
    assert final["state"] == "cancelled"
    assert final["committed"] == 0
    assert final["receipt"]["chunks"] == []
    assert _page_ids(vault) == before


def test_a_service_stopping_ends_a_running_job_interrupted_with_nothing_written(server, vault) -> None:
    async def scenario(client):
        preview = await _preview(client)
        with _boundary_held(vault):
            handle = await _start(client, preview["plan_id"])
            maintenance_jobs.stop_all(wait=0.0)
        return await _until_terminal(client, handle["run_id"])

    before = _page_ids(vault)
    final = _session(server, scenario)

    assert final["state"] == "interrupted"
    assert final["error_code"] == "SERVICE_STOPPING"
    assert final["receipt"]["chunks"] == []
    assert _page_ids(vault) == before


def test_cancelling_an_ended_job_returns_it_unchanged(server, vault) -> None:
    async def scenario(client):
        preview = await _preview(client)
        handle = await _start(client, preview["plan_id"])
        final = await _until_terminal(client, handle["run_id"])
        return final, await _cancel(client, handle["run_id"])

    final, cancel = _session(server, scenario)

    assert final["state"] == "completed"
    assert cancel["state"] == "completed"
    assert cancel["cancel_requested"] is False


def test_only_the_owner_starts_reads_lists_or_cancels_a_job(server, vault) -> None:
    command = next(item for item in commands.product_commands_for("mcp") if item.name == "maintain_memory")
    descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="test", tier2_enabled=True, product_commands=("maintain_memory",)
    )

    def as_other(**arguments):
        stranger = principal_module.RequestPrincipal(audience_id="someone-else", surface="mcp")
        with (
            capabilities.active_surface(descriptor),
            principal_module.request_scope(stranger),
            pytest.raises(OpError) as raised,
        ):
            writer_lease.invoke_command(command, vault, **arguments)
        return raised.value

    async def scenario(client):
        preview = await _preview(client)
        with _boundary_held(vault):
            running = await _start(client, preview["plan_id"])
            run_id = running["run_id"]
            refusals = [
                as_other(mode="backfill-ids", dry_run=False, plan_id=preview["plan_id"], why="not yours"),
                as_other(mode="maintenance-job", run_id=run_id),
                as_other(mode="maintenance-job"),
                as_other(mode="maintenance-job-cancel", run_id=run_id, why="not yours"),
            ]
            still_running = await _status(client, run_id)
            await _cancel(client, run_id)
        await _until_terminal(client, run_id)
        return run_id, refusals, still_running

    run_id, refusals, still_running = _session(server, scenario)

    assert {refusal.code for refusal in refusals} == {"MAINTENANCE_OWNER_REQUIRED"}
    assert all(run_id not in json.dumps([refusal.message, refusal.details]) for refusal in refusals)
    assert still_running["cancel_requested"] is False


def test_a_job_whose_process_died_reads_interrupted(server, vault, tmp_path) -> None:
    owner = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from exomem import capabilities, commands, writer_lease
        from exomem.governance import principal

        command = next(c for c in commands.product_commands_for("mcp") if c.name == "maintain_memory")
        descriptor = capabilities.ActiveSurfaceDescriptor(
            surface="mcp", profile="test", tier2_enabled=True, product_commands=("maintain_memory",)
        )
        with capabilities.active_surface(descriptor), principal.request_scope(principal.owner_principal(surface="mcp")):
            handle = writer_lease.invoke_command(
                command, Path(sys.argv[1]), mode="backfill-ids", dry_run=False, plan_id=sys.argv[2], why="x"
            )
        print(handle["run_id"], flush=True)
        time.sleep(600)
        """
    )

    async def scenario(client):
        preview = await _preview(client)
        with _boundary_held(vault):
            process = subprocess.Popen(
                [sys.executable, "-c", owner, str(vault), preview["plan_id"]],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                run_id = process.stdout.readline().strip()
                assert run_id
                running = await _status(client, run_id)
            finally:
                process.kill()
                process.wait(_DEADLINE_SECONDS)
        return running, await _status(client, run_id)

    before = _page_ids(vault)
    running, gone = _session(server, scenario)

    assert running["state"] == "running"
    assert gone["state"] == "interrupted"
    assert gone["error_code"] == "OWNER_GONE"
    assert _page_ids(vault) == before
