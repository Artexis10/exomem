"""A remote `backfill-ids`, `fix` or `reconcile` write runs as a governed maintenance job.

The workflow: the owner previews on `check_memory`, starts the previewed plan on
`repair_memory` with the preview's `plan_id` and a reason, then reads the job on
`check_memory` until it ends. These tests drive a real in-process MCP server over a
copy of the fixture vault, so the admission gate, the writer lease, the job store
and the runner thread are all real. Only a second writer (a thread holding the
mutation boundary) and a second process (a killed job owner) are staged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
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

from exomem import (
    activation_manifest,
    capabilities,
    commands,
    embeddings,
    maintenance_jobs,
    writer_lease,
)
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


# ---------------------------------------------------------------- fix

_NESTED_SIDECAR = """---
type: source
media_type: pdf
evidence_file: Knowledge Base/Evidence/Case/report.pdf
original_filename: report.pdf
{provenance}extracted_by: pymupdf
processing_state: completed
---

# Evidence: report.pdf

## Extracted text

The transcript body.

## Preserved notes

# Evidence: report.pdf

## Extracted text

The transcript body.
"""
_LINKER = (
    "---\ntype: insight\nstatus: active\ncreated: 2026-05-28\nupdated: 2026-05-28\ntags: []\n---\n"
    "# Linker\n\nSee [[Notes/Insights/progressive-disclosure-without-mode-fragmentation]].\n"
)
_FIXED_PAGES = ("Knowledge Base/Notes/Insights/linker.md", "Knowledge Base/Evidence/Case/report.pdf.md")


def _seed_fixable(vault: Path) -> None:
    """A page with a non-canonical wikilink, a nested media sidecar and a top index with a stale count."""
    kb = vault / "Knowledge Base"
    (kb / "Notes" / "Insights" / "linker.md").write_text(_LINKER, encoding="utf-8")
    binary = kb / "Evidence" / "Case" / "report.pdf"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary bytes")
    stat = binary.stat()
    # The sidecar already records its binary, so the service's media start-up leaves it alone.
    provenance = (
        f"binary_sha256: {hashlib.sha256(b'binary bytes').hexdigest()}\nbinary_size: {stat.st_size}\n"
        f"binary_mtime_ns: {stat.st_mtime_ns}\nbinary_ctime_ns: {stat.st_ctime_ns}\n"
    )
    (kb / "Evidence" / "Case" / "report.pdf.md").write_text(
        _NESTED_SIDECAR.format(provenance=provenance), encoding="utf-8"
    )
    top = kb / "index.md"
    top.write_text(
        top.read_text(encoding="utf-8").replace("- Sources: 4 (articles: 2, books: 1, sessions: 1)", "- Sources: 0"),
        encoding="utf-8",
    )


def _knowledge_base_bytes(vault: Path) -> dict[str, bytes]:
    root = vault / "Knowledge Base"
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


async def _fix_preview(client, **arguments) -> dict:
    return await _call(client, "check_memory", {"kind": "fix", **arguments})


async def _fix_start(client, plan_id: str, **arguments) -> dict:
    return await _call(
        client,
        "repair_memory",
        {"kind": "fix", "dry_run": False, "plan_id": plan_id, "why": "repair drift", **arguments},
    )


def test_remote_fix_writes_what_the_local_command_writes_and_names_every_path(server, vault, tmp_path) -> None:
    _seed_fixable(vault)
    twin = tmp_path / "twin"
    shutil.copytree(vault, twin)

    async def scenario(client):
        preview = await _fix_preview(client)
        handle = await _fix_start(client, preview["plan_id"])
        return preview, handle, await _until_terminal(client, handle["run_id"])

    preview, handle, final = _session(server, scenario)
    local = commands.op_audit_fix(twin)

    assert {row["path"] for row in preview["fixed"]} >= set(_FIXED_PAGES)
    assert handle["plan_id"] == preview["plan_id"] == local["plan_id"]
    assert final["state"] == "completed"
    assert _knowledge_base_bytes(vault) == _knowledge_base_bytes(twin)
    chunks = final["receipt"]["chunks"]
    assert {path for chunk in chunks for path in chunk.get("paths", []) if chunk.get("index_refresh") is None} == set(
        _FIXED_PAGES
    )
    [refresh] = [chunk for chunk in chunks if chunk.get("index_refresh") == "committed"]
    assert "Knowledge Base/index.md" in refresh["paths"]
    assert "- Sources: 4 (articles: 2, books: 1, sessions: 1)" in (vault / "Knowledge Base" / "index.md").read_text(
        encoding="utf-8"
    )
    assert final["committed"] == len(_FIXED_PAGES) + len(refresh["paths"])
    assert chunks[-1]["summary"]["fixed"] == preview["summary"]["fixed"]


def test_a_page_edited_after_the_fix_preview_stops_the_job_with_nothing_written(server, vault) -> None:
    _seed_fixable(vault)

    async def scenario(client):
        preview = await _fix_preview(client)
        linker = vault / _FIXED_PAGES[0]
        linker.write_text(linker.read_text(encoding="utf-8") + "\nAn edit after the preview.\n", encoding="utf-8")
        unchanged = _knowledge_base_bytes(vault)
        handle = await _fix_start(client, preview["plan_id"])
        return preview, unchanged, await _until_terminal(client, handle["run_id"])

    preview, unchanged, final = _session(server, scenario)

    assert final["state"] == "failed"
    assert final["error_code"] == "STALE_MAINTENANCE_PLAN"
    assert final["receipt"]["error"]["details"]["plan_id"] != preview["plan_id"]
    assert final["committed"] == 0
    assert final["receipt"]["chunks"] == []
    assert _knowledge_base_bytes(vault) == unchanged


def test_a_failed_embedding_rebuild_is_reported_in_the_receipt(server, vault, monkeypatch) -> None:
    def encoder_down(*_args, **_kwargs):
        raise RuntimeError("the encoder is down")

    monkeypatch.setattr(embeddings, "embed_texts", encoder_down)

    async def scenario(client):
        preview = await _fix_preview(client, rebuild_embeddings=True)
        handle = await _fix_start(client, preview["plan_id"], rebuild_embeddings=True)
        return await _until_terminal(client, handle["run_id"])

    final = _session(server, scenario)

    assert final["state"] == "completed"
    summary = final["receipt"]["chunks"][-1]["summary"]
    assert "the encoder is down" in summary["embeddings_error"]
    assert "embeddings_chunks" not in summary


def test_the_wide_boundary_switch_makes_a_job_hold_the_boundary_for_its_run(server, vault, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_WIDE_MUTATION_BOUNDARY", "1")

    async def scenario(client):
        preview = await _preview(client)
        with _boundary_held(vault):
            refused = await _start(client, preview["plan_id"])
        handle = await _start(client, preview["plan_id"])
        return preview, refused, await _until_terminal(client, handle["run_id"])

    preview, refused, final = _session(server, scenario)

    assert refused["error"]["code"] == "MUTATION_BUSY"
    assert final["state"] == "completed"
    assert all(_page_ids(vault)[path] is not None for path in preview["would_update"])


_ORPHAN_FRAMES = "Knowledge Base/Evidence/Case/gone.mp4.frames"
_SECOND_ORPHAN_FRAMES = "Knowledge Base/Evidence/Case/also-gone.mp4.frames"
_COUNT_ROW = "- Sources: 4 (articles: 2, books: 1, sessions: 1)"


def _seed_reconcile(vault: Path) -> None:
    """An index count row that drifted and a frames directory whose video is gone."""
    top = vault / "Knowledge Base" / "index.md"
    top.write_text(top.read_text(encoding="utf-8").replace(_COUNT_ROW, "- Sources: 0"), encoding="utf-8")
    (vault / _ORPHAN_FRAMES).mkdir(parents=True)


async def _reconcile_preview(client, **arguments) -> dict:
    return await _call(client, "check_memory", {"kind": "reconcile", "dry_run": True, **arguments})


async def _reconcile_start(client, plan_id: str, **arguments) -> dict:
    return await _call(
        client,
        "repair_memory",
        {"kind": "reconcile", "dry_run": False, "plan_id": plan_id, "why": "heal drift", **arguments},
    )


def test_remote_reconcile_heals_the_drift_and_removes_the_orphan_the_preview_named(server, vault) -> None:
    _seed_reconcile(vault)

    async def scenario(client):
        preview = await _reconcile_preview(client)
        after_preview = ((vault / _ORPHAN_FRAMES).is_dir(), (vault / "Knowledge Base" / "index.md").read_bytes())
        handle = await _reconcile_start(client, preview["plan_id"])
        return preview, after_preview, handle, await _until_terminal(client, handle["run_id"])

    preview, (orphan_kept, index_before), handle, final = _session(server, scenario)

    assert orphan_kept and b"- Sources: 0" in index_before
    assert preview["frame_orphans_removed"] == 1
    assert handle["plan_id"] == preview["plan_id"]
    assert final["state"] == "completed"
    assert not (vault / _ORPHAN_FRAMES).exists()
    assert _COUNT_ROW in (vault / "Knowledge Base" / "index.md").read_text(encoding="utf-8")
    summary = final["receipt"]["chunks"][-1]["summary"]
    assert summary["plan_id"] == preview["plan_id"]
    assert summary["frame_orphans_removed"] == 1
    assert summary["indexes_updated"] == len(preview["indexes_updated"]) >= 1


def test_an_orphan_that_appears_after_the_reconcile_preview_stops_the_job_with_nothing_written(
    server, vault
) -> None:
    _seed_reconcile(vault)

    async def scenario(client):
        preview = await _reconcile_preview(client)
        (vault / _SECOND_ORPHAN_FRAMES).mkdir()
        unchanged = _knowledge_base_bytes(vault)
        handle = await _reconcile_start(client, preview["plan_id"])
        return preview, unchanged, await _until_terminal(client, handle["run_id"])

    preview, unchanged, final = _session(server, scenario)

    assert final["state"] == "failed"
    assert final["error_code"] == "STALE_MAINTENANCE_PLAN"
    assert final["receipt"]["error"]["details"]["plan_id"] != preview["plan_id"]
    assert final["committed"] == 0
    assert _knowledge_base_bytes(vault) == unchanged
    assert (vault / _ORPHAN_FRAMES).is_dir() and (vault / _SECOND_ORPHAN_FRAMES).is_dir()
    # Establishing the activation manifest is a write too: a refused plan leaves it unmade.
    assert not activation_manifest.manifest_path(vault).exists()


def test_a_page_body_edit_after_the_reconcile_preview_does_not_stale_the_plan(server, vault) -> None:
    _seed_reconcile(vault)

    async def scenario(client):
        preview = await _reconcile_preview(client)
        page = next(iter(sorted((vault / "Knowledge Base" / "Notes").rglob("*.md"))))
        page.write_text(page.read_text(encoding="utf-8") + "\nAn edit after the preview.\n", encoding="utf-8")
        handle = await _reconcile_start(client, preview["plan_id"])
        return await _until_terminal(client, handle["run_id"])

    final = _session(server, scenario)

    assert final["state"] == "completed"
    assert not (vault / _ORPHAN_FRAMES).exists()


def test_a_reconcile_job_waits_for_a_busy_boundary_instead_of_failing(server, vault, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", "0.2")
    writer_lease.reset_managers_for_tests()
    _seed_reconcile(vault)

    async def scenario(client):
        preview = await _reconcile_preview(client)
        with _boundary_held(vault):
            handle = await _reconcile_start(client, preview["plan_id"])
            # Several of the manager's own waits pass while the other writer holds the boundary.
            await asyncio.sleep(1.0)
            waiting = await _status(client, handle["run_id"])
        return waiting, await _until_terminal(client, handle["run_id"])

    waiting, final = _session(server, scenario)

    assert waiting["state"] == "running"
    assert final["state"] == "completed"
    assert not (vault / _ORPHAN_FRAMES).exists()


def test_cancel_stays_narrow_under_the_wide_boundary_switch_while_a_job_holds_the_boundary(
    server, vault, monkeypatch
) -> None:
    monkeypatch.setenv("EXOMEM_WIDE_MUTATION_BOUNDARY", "1")
    real_checkpoint = maintenance_jobs.checkpoint
    holding, proceed = threading.Event(), threading.Event()

    def pausing_checkpoint(phase, *args, **kwargs):
        # The job's dispatch holds the wide boundary here, before its first chunk.
        if phase == "planning":
            holding.set()
            assert proceed.wait(_DEADLINE_SECONDS)
        return real_checkpoint(phase, *args, **kwargs)

    monkeypatch.setattr(maintenance_jobs, "checkpoint", pausing_checkpoint)

    async def scenario(client):
        preview = await _preview(client)
        handle = await _start(client, preview["plan_id"])
        assert await asyncio.to_thread(holding.wait, _DEADLINE_SECONDS)
        cancelled = await _cancel(client, handle["run_id"])
        proceed.set()
        return cancelled, await _until_terminal(client, handle["run_id"])

    try:
        cancelled, final = _session(server, scenario)
    finally:
        proceed.set()

    assert cancelled.get("error") is None
    assert final["state"] == "cancelled"
