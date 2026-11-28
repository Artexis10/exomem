"""A remote `backfill-ids`, `fix` or `reconcile` write runs as a governed maintenance job.

The workflow: the owner previews on `check_memory`, which starts a read-only preview job whose
receipt holds the `plan_id`; the owner starts the previewed plan on `repair_memory` with that
`plan_id` and a reason, then reads the job on `check_memory` until it ends. These tests drive a real in-process MCP server over a
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
from types import SimpleNamespace

import pytest
from fastmcp import Client

from exomem import (
    activation_manifest,
    capabilities,
    commands,
    embeddings,
    maintenance_jobs,
    memory_refs,
    mutation_lock,
    query_data,
    request_budget,
    writer_lease,
)
from exomem import server as server_module
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.governance import principal as principal_module
from exomem.memory_refs import ID_FIELD

_DEADLINE_SECONDS = 50.0
_OTHER_ID = "0" * 64


@pytest.fixture
def server(vault, monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    yield server_module.build_server(require_auth=False)
    writer_lease.reset_managers_for_tests()


def test_a_source_preview_only_releases_its_complete_result_to_its_request(server, vault):
    started, release = threading.Event(), threading.Event()
    outcomes = [("rewritten", f"Notes/page-{i:03}.md", None) for i in range(225)]
    outcomes[0] = ("refused", outcomes[0][1], "é" * 13_000)
    outcomes[1] = ("refused", outcomes[1][1], "é" * 8_000)
    digest = "preview-digest"

    def build(_stop):
        started.set()
        assert release.wait(_DEADLINE_SECONDS)
        return {"preview_digest": digest}, outcomes

    other = {"binding": "other-owner", "request_key": "other-source", "snapshot_key": "previous-snapshot"}
    previous = maintenance_jobs.start_source_preview(
        vault, **other, build=lambda _stop: ({}, [("rewritten", "Notes/other.md", None)]),
    )
    assert _source_preview_terminal(vault, **other, run_id=previous["run_id"])["state"] == "completed"

    identity = {"binding": "owner-binding", "request_key": "source-request", "snapshot_key": "snapshot-one"}
    handle = maintenance_jobs.start_source_preview(vault, **identity, build=build)
    assert started.wait(_DEADLINE_SECONDS)
    run_id = handle["run_id"]
    status_args = {**identity, "run_id": run_id}
    try:
        assert handle == {"run_id": run_id, "state": "running"}
        assert maintenance_jobs.start_source_preview(vault, **identity, build=build) == handle
        with pytest.raises(OpError) as refusal:
            maintenance_jobs.start_source_preview(vault, **{**identity, "snapshot_key": "snapshot-two"}, build=build)
        assert refusal.value.code == "RECLASSIFICATION_PREVIEW_WARMING"
        assert maintenance_jobs.source_preview_status(vault, **status_args, cursor="0") == handle
        for different in ({"binding": "other-owner"}, {"request_key": "other-source"}):
            with pytest.raises(OpError, match="No Source preview matches this request") as refusal:
                maintenance_jobs.source_preview_status(vault, **{**status_args, **different})
            assert refusal.value.code == "NOT_FOUND"
        with principal_module.library_scope():
            assert maintenance_jobs.job_list(vault) == {"jobs": []}
            with pytest.raises(OpError) as refusal:
                maintenance_jobs.job_status(vault, run_id)
            assert refusal.value.code == "NOT_FOUND"
            with pytest.raises(OpError) as refusal:
                maintenance_jobs.request_cancel(vault, run_id, "Stop this job")
            assert refusal.value.code == "NOT_FOUND"
            ordinary = maintenance_jobs.start_leaf(
                SimpleNamespace(name="maintain_memory"), (vault,), {"mode": "backfill-ids"},
            )
            with pytest.raises(OpError) as refusal:
                ordinary()
            assert refusal.value.code == "MAINTENANCE_JOB_ACTIVE"
            assert run_id not in str(refusal.value.details)
    finally:
        release.set()

    status = _source_preview_terminal(vault, **status_args)
    assert status == {
        "run_id": run_id, "state": "completed", "result": {"preview_digest": digest}, "next_cursor": "0",
        "snapshot_current": True,
    }
    assert maintenance_jobs.start_source_preview(vault, **identity, build=build) == {
        "run_id": run_id, "state": "completed",
    }
    stale_args = {**status_args, "snapshot_key": "snapshot-two"}
    assert maintenance_jobs.source_preview_status(vault, **stale_args) == {**status, "snapshot_current": False}
    with principal_module.library_scope(), pytest.raises(OpError) as hidden:
        maintenance_jobs.job_status(vault, run_id, cursor="0")
    assert hidden.value.code == "NOT_FOUND"
    unknown_args = {**status_args, "snapshot_key": None}
    assert maintenance_jobs.source_preview_status(vault, **unknown_args) == {**status, "snapshot_current": None}
    received = []
    cursor = status["next_cursor"]
    while cursor is not None:
        page = maintenance_jobs.source_preview_status(vault, **stale_args, cursor=cursor)
        assert page.keys() == {"run_id", "state", "entries", "next_cursor", "snapshot_current"}
        assert page["snapshot_current"] is False
        assert len(page["entries"]) <= 128
        encoded = json.dumps(page["entries"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert len(encoded) <= query_data.MAX_RESPONSE_BYTES // 2 + 1
        received.extend((entry["kind"], entry["path"], entry.get("value")) for entry in page["entries"])
        cursor = page["next_cursor"]
        assert cursor is None or cursor == str(len(received))
    assert received == outcomes
    past_end = maintenance_jobs.source_preview_status(vault, **stale_args, cursor=str((1 << 63) - 1))
    assert past_end["entries"] == [] and past_end["next_cursor"] is None
    current = {**identity, "snapshot_key": "snapshot-two"}
    newer = maintenance_jobs.start_source_preview(vault, **current, build=build)
    assert newer["run_id"] != run_id
    assert _source_preview_terminal(vault, **current, run_id=newer["run_id"])["snapshot_current"] is True


def _source_preview_terminal(vault, **status_args):
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while (status := maintenance_jobs.source_preview_status(vault, **status_args))["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return status


def test_an_interrupted_source_preview_never_publishes_its_staged_result(server, vault):
    staging = threading.Event()

    def build(stop):
        def entries():
            yield "rewritten", "Notes/later.md", None
            staging.set()
            assert stop.wait(_DEADLINE_SECONDS)
            yield "rewritten", "Notes/last.md", None

        return {"preview_digest": "unpublished"}, entries()

    identity = {"binding": "owner-binding", "request_key": "source-request", "snapshot_key": "snapshot"}
    handle = maintenance_jobs.start_source_preview(vault, **identity, build=build)
    assert staging.wait(_DEADLINE_SECONDS)
    maintenance_jobs.stop_all(vault, wait=_DEADLINE_SECONDS)
    assert maintenance_jobs.source_preview_status(vault, **identity, run_id=handle["run_id"], cursor="0") == {
        "run_id": handle["run_id"], "state": "interrupted",
    }


@pytest.mark.parametrize("phase", ["staging", "publication"])
def test_a_source_preview_deadline_discards_the_report(server, vault, monkeypatch, phase):
    reached = threading.Event()
    original_open = maintenance_jobs._open

    def expire_work_budget():
        check = request_budget.current_checkpoint()
        if check is not None:
            reached.set()
            check.budget.deadline = 0

    def open_observed(*args, **kwargs):
        conn = original_open(*args, **kwargs)
        if maintenance_jobs._JOB.get() is not None:
            def observe(statement):
                if statement.lstrip().startswith("INSERT OR REPLACE INTO held_evidence"):
                    expire_work_budget()

            conn.set_trace_callback(observe)
        return conn

    if phase == "publication":
        monkeypatch.setattr(maintenance_jobs, "_open", open_observed)

    def build(_stop):
        def entries():
            yield "rewritten", "Notes/staged.md", None
            if phase == "staging":
                expire_work_budget()
            yield "rewritten", "Notes/remaining.md", None

        return {"preview_digest": "unpublished"}, entries(), {"Notes/staged.md": {"private": "proof"}}

    identity = {"binding": "owner-binding", "request_key": "source-request", "snapshot_key": "snapshot"}
    handle = maintenance_jobs.start_source_preview(vault, **identity, build=build)
    run_id = handle["run_id"]
    status = _source_preview_terminal(vault, **identity, run_id=run_id, cursor="0")

    assert reached.is_set()
    assert status == {"run_id": run_id, "state": "interrupted"}
    with maintenance_jobs._store(vault, create=False) as conn:
        for table in ("entries", "held_evidence"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)).fetchone()[0] == 0
    next_run = maintenance_jobs.start_source_preview(
        vault, **identity, build=lambda _stop: ({"preview_digest": "complete"}, ()),
    )
    assert _source_preview_terminal(vault, **identity, run_id=next_run["run_id"])["state"] == "completed"


def test_source_preview_publication_does_not_wait_on_a_busy_job_store(server, vault):
    building, can_publish = threading.Event(), threading.Event()
    identity = {"binding": "owner-binding", "request_key": "source-request", "snapshot_key": "snapshot"}

    def build(_stop):
        building.set()
        assert can_publish.wait(_DEADLINE_SECONDS)
        return {"preview_digest": "unpublished"}, [("rewritten", "Notes/staged.md", None)]

    handle = maintenance_jobs.start_source_preview(vault, **identity, build=build)
    run_id = handle["run_id"]
    assert building.wait(_DEADLINE_SECONDS)
    with maintenance_jobs._LIVE_LOCK, maintenance_jobs._store(vault, create=True) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            live = maintenance_jobs._registered_job(run_id)
            assert live is not None
            worker = live.thread
            can_publish.set()
            assert worker is not None
            worker.join(2)
            assert not worker.is_alive()
        finally:
            writer.rollback()
            can_publish.set()

    status = _source_preview_terminal(vault, **identity, run_id=run_id, cursor="0")
    assert status == {"run_id": run_id, "state": "interrupted"}
    next_run = maintenance_jobs.start_source_preview(
        vault, **identity, build=lambda _stop: ({"preview_digest": "complete"}, ()),
    )
    assert _source_preview_terminal(vault, **identity, run_id=next_run["run_id"])["state"] == "completed"


def test_a_source_preview_that_refuses_after_its_stop_ends_interrupted(server, vault):
    building = threading.Event()

    def build(stop):
        building.set()
        assert stop.wait(_DEADLINE_SECONDS)
        raise OpError("NOT_FOUND", "The build noticed its stop.")

    identity = {"binding": "owner-binding", "request_key": "source-request", "snapshot_key": "snapshot"}
    handle = maintenance_jobs.start_source_preview(vault, **identity, build=build)
    assert building.wait(_DEADLINE_SECONDS)
    maintenance_jobs.stop_all(vault, wait=_DEADLINE_SECONDS)
    assert maintenance_jobs.source_preview_status(vault, **identity, run_id=handle["run_id"]) == {
        "run_id": handle["run_id"], "state": "interrupted",
    }


def test_a_source_preview_claim_does_not_wait_for_a_busy_job_store(server, vault):
    def unexpected_build(_stop):
        pytest.fail("the busy store must leave the worker unstarted")

    with maintenance_jobs._store(vault, create=True) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with request_budget.checking(request_budget.ReadCheckpoint(request_budget.RequestBudget(seconds=0.5))):
            with pytest.raises(request_budget.ReadBudgetExhausted):
                maintenance_jobs.start_source_preview(
                    vault, binding="owner-binding", request_key="source-request", snapshot_key="snapshot",
                    build=unexpected_build,
                )


@pytest.mark.parametrize(("error", "code"), [
    (OpError("RECLASSIFICATION_PREVIEW_WARMING", "private worker detail"), "RECLASSIFICATION_PREVIEW_WARMING"),
    (OpError("RECLASSIFICATION_PREVIEW_UNAVAILABLE", "private worker detail"), "RECLASSIFICATION_PREVIEW_UNAVAILABLE"),
    (OpError("INTERNAL_WORKER_ROUTE", "private worker detail"), "RECLASSIFICATION_PREVIEW_FAILED"),
    (RuntimeError("private worker detail"), "RECLASSIFICATION_PREVIEW_FAILED"),
])
def test_a_source_preview_failure_releases_only_a_safe_reason(server, vault, error, code):
    def build(_stop):
        def entries():
            yield "rewritten", "Notes/staged.md", None
            raise error

        return {"preview_digest": "unpublished"}, entries()

    identity = {"binding": "owner-binding", "request_key": "source-request", "snapshot_key": "snapshot"}
    handle = maintenance_jobs.start_source_preview(vault, **identity, build=build)
    assert _source_preview_terminal(vault, **identity, run_id=handle["run_id"], cursor="0") == {
        "run_id": handle["run_id"], "state": "failed", "error_code": code,
    }


def test_held_evidence_stays_private_and_dies_with_the_job(server, vault, monkeypatch):
    sha = "ab" * 32
    record = {
        "path": "Notes/held.md", "sha256": sha, "size": 42,
        "witness": [1, 2, 42, 3, 4], "scopes": [["scope-a"]], "identity": '["release"]',
    }
    held = {"Notes/held.md": record}
    identity = {"binding": "owner-binding", "request_key": "req", "snapshot_key": "snap"}

    def build(_stop):
        return ({"preview_digest": "d"}, [("rewritten", "Notes/held.md", None)], held)

    handle = maintenance_jobs.start_source_preview(vault, **identity, build=build)
    status = _source_preview_terminal(vault, **identity, run_id=handle["run_id"])
    assert status["state"] == "completed"
    evidence = maintenance_jobs.source_preview_held_evidence(vault, handle["run_id"], ["Notes/held.md"])
    assert evidence == {
        "Notes/held.md": maintenance_jobs.HeldEvidence(
            path="Notes/held.md", sha256=sha, size=42, witness=(1, 2, 42, 3, 4),
            scopes=(frozenset({"scope-a"}),), identity='["release"]',
        ),
    }
    assert maintenance_jobs.source_preview_held_evidence(vault, handle["run_id"], ["Notes/other.md"]) == {}

    pages = [status]
    cursor = status.get("next_cursor")
    while cursor is not None:
        page = maintenance_jobs.source_preview_status(
            vault, **identity, run_id=handle["run_id"], cursor=cursor,
        )
        pages.append(page)
        cursor = page["next_cursor"]
    wire = json.dumps(pages, ensure_ascii=False)
    assert sha not in wire and "witness" not in wire and "scope-a" not in wire

    monkeypatch.setattr(maintenance_jobs, "RETAIN_SECONDS", 0)
    monkeypatch.setattr(maintenance_jobs, "RETAIN_NEWEST", 0)
    newer = maintenance_jobs.start_source_preview(
        vault, **{**identity, "snapshot_key": "snap-two"}, build=build,
    )
    assert _source_preview_terminal(
        vault, **{**identity, "snapshot_key": "snap-two"}, run_id=newer["run_id"],
    )["state"] == "completed"
    assert maintenance_jobs.source_preview_held_evidence(vault, handle["run_id"], ["Notes/held.md"]) == {}


def test_a_failed_source_preview_prunes_expired_terminal_jobs(server, vault, monkeypatch):
    def build(_stop):
        raise RuntimeError("preview failed")

    identity = {"binding": "owner-binding", "request_key": "source-request", "snapshot_key": "snapshot"}
    old = maintenance_jobs.start_source_preview(vault, **identity, build=build)
    assert _source_preview_terminal(vault, **identity, run_id=old["run_id"])["state"] == "failed"

    monkeypatch.setattr(maintenance_jobs, "RETAIN_SECONDS", 0)
    monkeypatch.setattr(maintenance_jobs, "RETAIN_NEWEST", 1)
    current = maintenance_jobs.start_source_preview(vault, **identity, build=build)
    assert _source_preview_terminal(vault, **identity, run_id=current["run_id"])["state"] == "failed"
    with pytest.raises(OpError) as expired:
        maintenance_jobs.source_preview_status(vault, **identity, run_id=old["run_id"])
    assert expired.value.code == "NOT_FOUND"


def _session(server, scenario):
    async def run():
        async with Client(server) as client:
            return await scenario(client)

    return asyncio.run(run())


#: The door operation that carries each maintenance kind, on the read door and on the write door.
_DOOR_OPERATIONS = {
    "backfill-ids": "maintenance.backfill_ids",
    "capture-metadata": "maintenance.capture_metadata",
    "convert-originals": "maintenance.convert_originals",
    "fix": "maintenance.fix",
    "path-variants": "maintenance.path_variants",
    "reconcile": "maintenance.reconcile",
    "maintenance-job": "maintenance.job",
    "maintenance-job-cancel": "maintenance.cancel_job",
}
_DOORS = {"check_memory": "inspect_memory", "repair_memory": "manage_memory"}


async def _call(client, tool, operation):
    """Call one maintenance kind through the door that carries it, as an agent does."""
    kind, arguments = operation["kind"], {key: value for key, value in operation.items() if key != "kind"}
    if tool == "check_memory" and kind != "reconcile":
        # A read of fix or backfill-ids implies the preview, so it never sends `dry_run`.
        arguments.pop("dry_run", None)
    wire = {"operation": _DOOR_OPERATIONS[kind], **({"arguments": arguments} if arguments else {})}
    result = await client.call_tool(_DOORS[tool], wire, raise_on_error=False)
    return result.structured_content


async def _preview_job(client, operation: dict) -> tuple[dict, dict]:
    """The owner's remote preview: the handle it returns at once, and the job's status once it ended."""
    handle = await _call(client, "check_memory", operation)
    return handle, await _until_terminal(client, handle["run_id"])


async def _report(client, operation: dict) -> dict:
    """The report of a preview that completed, as its job's receipt holds it."""
    _, final = await _preview_job(client, operation)
    assert final["state"] == "completed"
    return final["receipt"]["result"]


async def _preview(client) -> dict:
    """A backfill-ids preview's report, with the pages its job lists as planned under `would_update`."""
    handle, final = await _preview_job(client, {"kind": "backfill-ids"})
    assert final["state"] == "completed"
    planned = await _receipt_entries(client, handle["run_id"])
    return {**final["receipt"]["result"], "would_update": [entry["path"] for entry in planned if entry["kind"] == "planned"]}


async def _start(client, plan_id: str, why: str = "give pages their identity") -> dict:
    return await _call(
        client,
        "repair_memory",
        {"kind": "backfill-ids", "dry_run": False, "plan_id": plan_id, "why": why},
    )


async def _status(client, run_id: str) -> dict:
    return await _call(client, "check_memory", {"kind": "maintenance-job", "run_id": run_id})


async def _receipt_entries(client, run_id: str) -> list[dict]:
    """Every entry of a finished job's receipt, read page by page from the status read's `next_cursor`."""
    status = await _status(client, run_id)
    cursor = status["receipt"]["next_cursor"]
    entries: list[dict] = []
    while cursor is not None:
        page = await _call(client, "check_memory", {"kind": "maintenance-job", "run_id": run_id, "cursor": cursor})
        entries.extend(page["entries"])
        cursor = page["next_cursor"]
    return entries


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
        # A read can meet brief store contention under the short mutation timeout some tests set.
        if (status.get("error") or {}).get("code") != "MUTATION_BUSY" and done(status):
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
        return preview, handle, final, await _receipt_entries(client, handle["run_id"])

    before = _page_ids(vault)
    preview, handle, final, entries = _session(server, scenario)
    after = _page_ids(vault)

    assert handle["state"] == "running"
    assert handle["plan_id"] == preview["plan_id"]
    assert final["state"] == "completed"
    previewed = set(preview["would_update"])
    assert all(before[path] is None and after[path] is not None for path in previewed)
    assert {p: i for p, i in after.items() if p not in previewed} == {p: i for p, i in before.items() if p not in previewed}
    assert {entry["kind"] for entry in entries} == {"committed"}
    minted = {entry["path"]: entry["value"] for entry in entries}
    assert set(minted) == previewed
    assert {path: f"exomem://memory/{after[path]}" for path in previewed} == minted
    assert final["progress"]["done"] == final["progress"]["total"] == len(previewed)
    assert final["committed"] == len(previewed)


def test_remote_backfill_skips_readonly_files_and_canonical_views(vault, tmp_path, monkeypatch) -> None:
    """A protected candidate must not abort a job after earlier chunks committed."""
    from test_collection_store_service import _served_vault, coordinator, offline_create
    from test_collection_store_writer import manifest_path

    setup = tmp_path / "store"
    root = _served_vault(setup, monkeypatch)
    shutil.copytree(vault / "Knowledge Base/_Schema", root / "Knowledge Base/_Schema", dirs_exist_ok=True)
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    with coordinator(tmp_path / "coordinator.sqlite"):
        assert offline_create(root, setup / "c.md", manifest_path(), why="store fixture") == 0
        # Use the real published view so receipt visibility follows canonical records.
        protected = {
            "Knowledge Base/Records/Work/Items/_summary.md": (
                root / "Knowledge Base/Records/Work/Items/_summary.md"
            ).read_text(encoding="utf-8"),
            "Knowledge Base/Reference/locked.md": "---\ntitle: Reference\n---\nReference body.\n",
        }
        eligible = {
            "Knowledge Base/Notes/ordinary.md": "---\ntitle: Ordinary\n---\nNote body.\n",
            "Knowledge Base/Sources/original.md": "---\ntitle: Original\n---\nSource body.\n",
            # An untrusted stamp cannot appoint a file as a canonical view.
            "Knowledge Base/Notes/stamped.md": "---\ntitle: Stamped\nexomem_view: true\n---\nNote body.\n",
        }
        for relative, text in {**protected, **eligible}.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (root / "Knowledge Base/_access.yaml").write_text("readonly:\n  - Reference\n", encoding="utf-8")
        before = {relative: (root / relative).read_bytes() for relative in protected}

        async def scenario(client):
            preview_handle, preview_final = await _preview_job(client, {"kind": "backfill-ids"})
            assert preview_final["state"] == "completed"
            preview_entries = await _receipt_entries(client, preview_handle["run_id"])
            handle = await _start(client, preview_final["receipt"]["result"]["plan_id"])
            final = await _until_terminal(client, handle["run_id"])
            return preview_entries, final, await _receipt_entries(client, handle["run_id"])

        try:
            preview_entries, final, entries = _session(server_module.build_server(require_auth=False), scenario)
        finally:
            writer_lease.reset_managers_for_tests()

    planned = {entry["path"] for entry in preview_entries if entry["kind"] == "planned"}
    skipped = {entry["path"]: entry["value"] for entry in preview_entries if entry["kind"] == "skipped"}
    assert set(eligible) <= planned
    assert planned.isdisjoint(protected)
    assert all(skipped[relative] for relative in protected)
    assert final["state"] == "completed", final
    assert final["receipt"]["outcome_unknown"] == 0
    assert final["committed"] == len(planned)
    assert {entry["path"] for entry in entries if entry["kind"] == "committed"} == planned
    assert all(_page_ids(root)[relative] is not None for relative in eligible)
    assert {relative: (root / relative).read_bytes() for relative in protected} == before


def test_a_finished_job_reads_as_totals_and_its_paths_come_page_by_page_under_the_response_bound(
    server, vault, monkeypatch
) -> None:
    # A small bound makes the fixture vault's few pages span several receipt pages.
    monkeypatch.setattr(query_data, "MAX_RESPONSE_BYTES", 600)

    async def scenario(client):
        preview = await _preview(client)
        handle = await _start(client, preview["plan_id"])
        final = await _until_terminal(client, handle["run_id"])
        pages, cursor = [], final["receipt"]["next_cursor"]
        while cursor is not None:
            pages.append(
                await _call(client, "check_memory", {"kind": "maintenance-job", "run_id": handle["run_id"], "cursor": cursor})
            )
            cursor = pages[-1]["next_cursor"]
        return preview, final, pages

    preview, final, pages = _session(server, scenario)

    previewed = set(preview["would_update"])
    assert final["committed"] == len(previewed)
    assert not any(path in json.dumps(final) for path in previewed)
    assert len(pages) >= 2
    assert all(len(json.dumps(page)) <= 600 for page in pages)
    assert {entry["path"] for page in pages for entry in page["entries"]} == previewed


def test_a_write_that_fails_inside_a_batch_leaves_that_batch_outcome_unknown_in_the_receipt(
    server, vault, monkeypatch
) -> None:
    def disk_gone(*_args, **_kwargs):
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(maintenance_jobs, "BATCH_PAGES", 2)
    monkeypatch.setattr(vault_module, "batch_atomic_write", disk_gone)

    async def scenario(client):
        preview = await _preview(client)
        handle = await _start(client, preview["plan_id"])
        final = await _until_terminal(client, handle["run_id"])
        return preview, final, await _receipt_entries(client, handle["run_id"])

    preview, final, entries = _session(server, scenario)

    assert final["state"] == "failed"
    assert final["receipt"]["outcome_unknown"] == 2 == len(entries)
    assert {entry["kind"] for entry in entries} == {"outcome_unknown"}
    assert {entry["path"] for entry in entries} <= set(preview["would_update"])


def _plain_pages(vault: Path, count: int) -> list[tuple[str, Path, str]]:
    """`count` pages as `commit_pages` takes them: `(vault-relative path, file, content hash)`."""
    pages = []
    for index in range(count):
        path = vault / "Knowledge Base" / "Notes" / f"hold-{index}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntype: insight\n---\n# Hold {index}\n", encoding="utf-8")
        text = path.read_text(encoding="utf-8")
        pages.append((path.relative_to(vault).as_posix(), path, vault_module.content_hash(text)))
    return pages


def test_a_job_lets_a_writer_in_after_the_page_it_is_writing_instead_of_after_its_whole_chunk(
    server, vault, monkeypatch
) -> None:
    # Only a waiting writer ends a hold here: the time budget is out of reach.
    monkeypatch.setattr(maintenance_jobs, "HOLD_SECONDS", 3600.0)
    monkeypatch.setattr(maintenance_jobs, "BATCH_PAGES", 1)
    pages = _plain_pages(vault, 4)
    boundaries, rewritten, entered = [], [], []
    real_hold = maintenance_jobs.chunk_hold

    @contextmanager
    def watched(*args, **kwargs):
        with real_hold(*args, **kwargs) as boundary:
            boundaries.append(boundary)
            yield boundary

    monkeypatch.setattr(maintenance_jobs, "chunk_hold", watched)
    writer_queued = threading.Event()

    def rewrite(rel: str, text: str) -> str:
        rewritten.append(rel)
        if len(rewritten) == 1:  # the first page is under the hold: a writer now queues for the boundary
            assert writer_queued.wait(_DEADLINE_SECONDS)
        return text + "\nedited\n"

    def other_writer() -> None:
        with writer_lease.get_manager().mutation_guard(vault, request_id="other-writer", operation="edit"):
            entered.append(len(rewritten))

    job = threading.Thread(target=lambda: maintenance_jobs.commit_pages(vault, "test_pages", pages, rewrite))
    writer = threading.Thread(target=other_writer)
    job.start()
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while not boundaries:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    writer.start()
    while boundaries[0].waiter_count() == 0:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    writer_queued.set()
    job.join(_DEADLINE_SECONDS)
    writer.join(_DEADLINE_SECONDS)

    assert entered == [1]
    assert len(rewritten) == len(pages)


def test_a_phase_that_grows_with_the_vault_lets_a_writer_in_and_still_applies_every_item(
    vault, monkeypatch
) -> None:
    """A reconcile phase held the boundary through all of its items, 33 minutes on a large vault.

    Every writer behind it was refused busy after its wait. The phase now runs in short holds that
    end for a waiting writer, and it applies each item exactly once across them.
    """
    # Only a waiting writer ends a hold here: the time budget is out of reach.
    monkeypatch.setattr(maintenance_jobs, "HOLD_SECONDS", 3600.0)
    monkeypatch.setattr(maintenance_jobs, "current_run_id", lambda: "reconcile-run")
    items = list(range(60))
    applied: list[int] = []
    entered: list[int] = []

    def apply(chunk) -> None:
        applied.extend(chunk)
        time.sleep(0.05)

    def writer() -> None:
        with writer_lease.get_manager().mutation_guard(vault, request_id="writer", operation="edit"):
            entered.append(len(applied))

    job = threading.Thread(
        target=lambda: maintenance_jobs.apply_in_holds(vault, "reconcile_units", items, apply, batch=1)
    )
    job.start()
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while not applied:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    other = threading.Thread(target=writer)
    other.start()
    other.join(_DEADLINE_SECONDS)
    job.join(_DEADLINE_SECONDS)

    # The writer got in while the phase had most of its items left, not after the last one.
    assert len(entered) == 1 and entered[0] < len(items) // 2, entered
    assert applied == items


def test_a_job_whose_time_budget_is_spent_still_writes_a_page_every_hold(server, vault, monkeypatch) -> None:
    monkeypatch.setattr(maintenance_jobs, "HOLD_SECONDS", 0.0)
    monkeypatch.setattr(maintenance_jobs, "BATCH_PAGES", 1)
    pages = _plain_pages(vault, 3)

    committed, skipped = maintenance_jobs.commit_pages(vault, "test_pages", pages, lambda rel, text: text + "\nedited\n")

    assert committed == [rel for rel, _, _ in pages]
    assert skipped == []
    assert all(path.read_text(encoding="utf-8").endswith("edited\n") for _, path, _ in pages)


def test_start_without_plan_and_reason_is_refused_and_leaves_no_job(server, vault) -> None:
    async def scenario(client):
        refused = await _call(client, "repair_memory", {"kind": "backfill-ids", "dry_run": False})
        return refused, await _list(client)

    before = _page_ids(vault)
    refused, listed = _session(server, scenario)

    assert refused["error"]["code"] == "MAINTENANCE_PLAN_REQUIRED"
    assert refused["error"]["message"].endswith("Nothing was saved.")
    assert _jobs(listed) == []
    assert _page_ids(vault) == before


@pytest.mark.skipif(sys.platform in {"win32", "darwin"}, reason="needs a case-sensitive volume")
def test_a_remote_merge_of_folder_splits_runs_as_the_owners_previewed_job(server, vault) -> None:
    """Defect: a request-bound merge moves about one file per call on a large vault, so the owner cannot finish it."""
    evidence = vault / "Knowledge Base" / "Evidence"
    for folder, names in (("Garden", ("a", "b", "c")), ("garden", ("d", "e"))):
        (evidence / folder).mkdir(parents=True)
        for name in names:
            (evidence / folder / f"{name}.md").write_text("---\ntype: note\n---\n\nBody.\n", encoding="utf-8")

    async def scenario(client):
        handle, previewed = await _preview_job(client, {"kind": "path-variants"})
        planned = await _receipt_entries(client, handle["run_id"])
        started = await _call(client, "repair_memory", {
            "kind": "path-variants", "dry_run": False,
            "plan_id": previewed["receipt"]["result"]["plan_id"], "why": "merge the garden split",
        })
        merged = await _until_terminal(client, started["run_id"])
        return previewed, planned, started, merged, await _receipt_entries(client, started["run_id"])

    previewed, planned, started, merged, entries = _session(server, scenario)

    assert previewed["state"] == "completed"
    assert {(entry["path"], entry["value"]) for entry in planned if entry["kind"] == "planned"} == {
        *((f"Knowledge Base/Evidence/garden/{name}.md", f"Knowledge Base/Evidence/Garden/{name}.md") for name in ("d", "e")),
        ("Knowledge Base/Evidence/garden", "removed once empty"),
    }
    assert started["state"] == "running"
    assert merged["state"] == "completed" and merged["receipt"]["notes"]["outcome"] == "committed"
    assert {entry["path"] for entry in entries if entry["kind"] == "committed"} == {
        f"Knowledge Base/Evidence/Garden/{name}.md" for name in ("d", "e")
    }
    assert not (evidence / "garden").exists()


@pytest.mark.skipif(sys.platform in {"win32", "darwin"}, reason="needs a case-sensitive volume")
def test_a_remote_merge_lists_each_refused_move_with_its_reason(server, vault, monkeypatch) -> None:
    """Defect: a remote owner sees only a count of skipped moves, never which files or why."""
    from exomem import path_variants

    evidence = vault / "Knowledge Base" / "Evidence"
    for folder, names in (("Garden", ("a", "b", "c")), ("garden", ("d", "e"))):
        (evidence / folder).mkdir(parents=True)
        for name in names:
            (evidence / folder / f"{name}.md").write_text("---\ntype: note\n---\n\nBody.\n", encoding="utf-8")
    moving = path_variants._move
    refused = "Knowledge Base/Evidence/garden/e.md"

    def move(root, unit):
        # The ordinary move refuses this one, as it does a page an append-only referrer cannot repoint.
        if unit["from"] == refused:
            return {"path": refused, "code": "APPEND_ONLY", "reason": "a referrer cannot be repointed"}
        return moving(root, unit)

    monkeypatch.setattr(path_variants, "_move", move)

    async def scenario(client):
        _, previewed = await _preview_job(client, {"kind": "path-variants"})
        started = await _call(client, "repair_memory", {
            "kind": "path-variants", "dry_run": False,
            "plan_id": previewed["receipt"]["result"]["plan_id"], "why": "merge the garden split",
        })
        merged = await _until_terminal(client, started["run_id"])
        return merged, await _receipt_entries(client, started["run_id"])

    merged, entries = _session(server, scenario)

    assert merged["receipt"]["notes"]["outcome"] == "partial"
    assert [(entry["path"], entry["value"]) for entry in entries if entry["kind"] == "skipped"] == [
        (refused, "APPEND_ONLY: a referrer cannot be repointed")
    ]
    assert (evidence / "garden" / "e.md").is_file()


def test_a_remote_conversion_runs_only_as_the_owners_previewed_job(server, vault, monkeypatch) -> None:
    """Catches a remote conversion that starts without the reviewed plan, or that the job framework cannot run."""
    import datetime as dt

    from exomem import artifact_lifecycle, originals, preserve

    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))

    with principal_module.library_scope():
        filed = preserve.preserve_bytes(vault, scope="work", category="receipts", filename="receipt.png",
                                        data=b"receipt photo bytes" * 64, today=dt.date(2026, 11, 16))

    async def scenario(client):
        refused = await _call(client, "repair_memory", {"kind": "convert-originals", "dry_run": False})
        _, preview = await _preview_job(client, {"kind": "convert-originals"})
        started = await _call(client, "repair_memory", {
            "kind": "convert-originals", "dry_run": False, "plan_id": preview["receipt"]["result"]["plan_id"],
            "why": "store each original once"})
        return refused, preview, await _until_terminal(client, started["run_id"])

    refused, preview, applied = _session(server, scenario)

    assert refused["error"]["code"] == "MAINTENANCE_PLAN_REQUIRED"
    assert preview["receipt"]["result"]["would_convert"] == 1
    assert applied["state"] == "completed", applied
    assert applied["receipt"]["result"]["converted"] == 1
    assert not (vault / filed.path).exists() and originals.pooled(vault, filed.path) is not None


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


def _read_job_retry(run_id: str) -> dict:
    """The call that reads the running job: the refusal's only carrier of its run id."""
    return {
        "tool": "inspect_memory",
        "arguments": {"operation": "maintenance.job", "arguments": {"run_id": run_id}},
    }


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
    assert refused["error"]["retry"] == _read_job_retry(running["run_id"])


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
    # The preview job that gave the plan_id is a job too; the write joined one job and made no second.
    assert [job["run_id"] for job in _jobs(listed) if not job["preview"]] == [first["run_id"]]


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
    assert final["receipt"]["chunks"] == 0
    assert final["receipt"]["next_cursor"] is None
    assert _page_ids(vault) == before


def test_a_service_stopping_ends_a_running_job_interrupted_with_nothing_written(server, vault) -> None:
    async def scenario(client):
        preview = await _preview(client)
        with _boundary_held(vault):
            handle = await _start(client, preview["plan_id"])
            maintenance_jobs.stop_all(vault, wait=0.0)
        return await _until_terminal(client, handle["run_id"])

    before = _page_ids(vault)
    final = _session(server, scenario)

    assert final["state"] == "interrupted"
    assert final["error_code"] == "SERVICE_STOPPING"
    assert final["receipt"]["chunks"] == 0
    assert final["receipt"]["next_cursor"] is None
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
    return await _report(client, {"kind": "fix", **arguments})


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
        final = await _until_terminal(client, handle["run_id"])
        return preview, handle, final, await _receipt_entries(client, handle["run_id"])

    preview, handle, final, entries = _session(server, scenario)
    local = commands.op_audit_fix(twin)

    assert {row["path"] for row in preview["fixed"]} >= set(_FIXED_PAGES)
    assert handle["plan_id"] == preview["plan_id"] == local["plan_id"]
    assert final["state"] == "completed"
    assert _knowledge_base_bytes(vault) == _knowledge_base_bytes(twin)
    # The index refresh commits last, in a chunk of its own after the page chunks.
    last_chunk = max(entry["chunk"] for entry in entries)
    refreshed = {entry["path"] for entry in entries if entry["chunk"] == last_chunk}
    assert {entry["path"] for entry in entries if entry["chunk"] != last_chunk} == set(_FIXED_PAGES)
    assert "Knowledge Base/index.md" in refreshed
    assert "- Sources: 4 (articles: 2, books: 1, sessions: 1)" in (vault / "Knowledge Base" / "index.md").read_text(
        encoding="utf-8"
    )
    assert final["committed"] == len(_FIXED_PAGES) + len(refreshed)
    assert final["receipt"]["notes"]["summary"]["fixed"] == preview["summary"]["fixed"]


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
    assert final["receipt"]["chunks"] == 0
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
    summary = final["receipt"]["notes"]["summary"]
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
    assert (final["state"], final["error_code"]) == ("completed", None)
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
    return await _report(client, {"kind": "reconcile", "dry_run": True, **arguments})


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
    summary = final["receipt"]["notes"]["summary"]
    assert summary["plan_id"] == preview["plan_id"]
    assert summary["frame_orphans_removed"] == 1
    assert summary["indexes_updated"] == len(preview["indexes_updated"]) >= 1
    # The job releases the boundary before the graph join, as the CLI does, so the graph it started converges.
    assert final["receipt"]["notes"]["graph"] == "completed"


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


def test_a_reconcile_job_waits_out_a_busy_boundary_in_mid_run_instead_of_failing(
    server, vault, monkeypatch
) -> None:
    # Short enough for the job's hold to meet the busy boundary at once; long enough for the job
    # store's brief reserved-identity contention, which shares this timeout.
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", "1.0")
    writer_lease.reset_managers_for_tests()
    _seed_reconcile(vault)
    armed, refused = threading.Event(), threading.Event()
    other_writers: list[threading.Thread] = []
    real_refused = mutation_lock.VaultMutationCoordinator._refused

    def noted_refusal(coordinator, wait_start):
        if coordinator.kind == mutation_lock.COORDINATOR_VAULT_BOUNDARY and maintenance_jobs.current_run_id():
            refused.set()
        return real_refused(coordinator, wait_start)

    real_checkpoint = maintenance_jobs.checkpoint

    def busy_before_the_orphans_hold(phase, *args, **kwargs):
        # The indexes phase has committed: another writer takes the boundary and keeps it until
        # the job's next hold has met it busy at least once.
        if phase == "orphans" and armed.is_set() and maintenance_jobs.current_run_id():
            holding = threading.Event()

            def hold() -> None:
                with writer_lease.get_manager().mutation_guard(
                    vault, request_id="other-writer", operation="semantic edit"
                ):
                    holding.set()
                    refused.wait(_DEADLINE_SECONDS)

            other_writers.append(threading.Thread(target=hold))
            other_writers[-1].start()
            assert holding.wait(_DEADLINE_SECONDS)
        return real_checkpoint(phase, *args, **kwargs)

    monkeypatch.setattr(mutation_lock.VaultMutationCoordinator, "_refused", noted_refusal)
    monkeypatch.setattr(maintenance_jobs, "checkpoint", busy_before_the_orphans_hold)

    async def scenario(client):
        preview = await _reconcile_preview(client)
        armed.set()
        handle = await _reconcile_start(client, preview["plan_id"])
        return await _until_terminal(client, handle["run_id"])

    try:
        final = _session(server, scenario)
    finally:
        refused.set()
        for thread in other_writers:
            thread.join(_DEADLINE_SECONDS)

    assert len(other_writers) == 1
    assert refused.is_set()
    assert (final["state"], final["error_code"]) == ("completed", None)
    assert _COUNT_ROW in (vault / "Knowledge Base" / "index.md").read_text(encoding="utf-8")
    assert not (vault / _ORPHAN_FRAMES).exists()


def _paused_reconcile_job(server, vault, monkeypatch, write) -> tuple[dict, tuple[bool, bool], object]:
    """Run a reconcile job that pauses in its lexical refresh while `write()` runs in another thread.

    The lexical refresh holds no boundary of its own: the indexes phase before it has committed,
    and the orphans phase after it has not run. Returns the job's final status, what the vault
    showed at the pause (index healed, orphan still there), and what `write()` returned or raised.
    """
    _seed_reconcile(vault)
    armed, reached, proceed = threading.Event(), threading.Event(), threading.Event()
    real_checkpoint = maintenance_jobs.checkpoint

    def pausing_checkpoint(phase, *args, **kwargs):
        if phase == "lexical" and armed.is_set() and maintenance_jobs.current_run_id():
            reached.set()
            assert proceed.wait(_DEADLINE_SECONDS)
        return real_checkpoint(phase, *args, **kwargs)

    monkeypatch.setattr(maintenance_jobs, "checkpoint", pausing_checkpoint)

    def outcome() -> object:
        try:
            return write()
        except OpError as error:
            return error

    async def scenario(client):
        preview = await _reconcile_preview(client)
        armed.set()
        handle = await _reconcile_start(client, preview["plan_id"])
        assert await asyncio.to_thread(reached.wait, _DEADLINE_SECONDS)
        seen = (
            _COUNT_ROW in (vault / "Knowledge Base" / "index.md").read_text(encoding="utf-8"),
            (vault / _ORPHAN_FRAMES).is_dir(),
        )
        written = await asyncio.to_thread(outcome)
        proceed.set()
        return await _until_terminal(client, handle["run_id"]), seen, written

    try:
        return _session(server, scenario)
    finally:
        proceed.set()


def _ordinary_write(vault: Path, rel: str):
    """A foreground write through the manager, as any write command makes it."""

    def leaf(vault_root: Path) -> dict[str, str]:
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(vault_root / rel, "# Written during a reconcile job\n")],
            vault_root=vault_root,
            post_commit_fanout=False,
        )
        return {"status": "committed"}

    command = SimpleNamespace(name="canonical_mutation", read_only=False, leaf=leaf)
    return lambda: writer_lease.get_manager().invoke(command, (vault,), {"response_detail": "full"})


def test_an_ordinary_write_commits_between_two_holds_of_a_reconcile_job(server, vault, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", "1.0")
    writer_lease.reset_managers_for_tests()
    rel = "Knowledge Base/Records/written-during-reconcile.md"

    final, seen, written = _paused_reconcile_job(server, vault, monkeypatch, _ordinary_write(vault, rel))

    # The write ran and returned while the job waited at the pause, between its two holds.
    assert isinstance(written, dict), written
    assert written["diagnostics"]["operational"]["status"] == "committed"
    assert (vault / rel).is_file()
    assert seen == (True, True)
    assert (final["state"], final["error_code"]) == ("completed", None)
    assert not (vault / _ORPHAN_FRAMES).exists()


def test_the_wide_boundary_switch_makes_a_reconcile_job_hold_the_boundary_for_its_run(
    server, vault, monkeypatch
) -> None:
    monkeypatch.setenv("EXOMEM_WIDE_MUTATION_BOUNDARY", "1")
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", "1.0")
    writer_lease.reset_managers_for_tests()
    rel = "Knowledge Base/Records/written-during-reconcile.md"

    final, seen, written = _paused_reconcile_job(server, vault, monkeypatch, _ordinary_write(vault, rel))

    assert isinstance(written, OpError) and written.code == "MUTATION_BUSY"
    assert not (vault / rel).exists()
    assert seen == (True, True)
    assert (final["state"], final["error_code"]) == ("completed", None)
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


def test_a_status_read_that_meets_the_job_ending_never_reports_it_interrupted(
    server, vault, monkeypatch
) -> None:
    # `_owner` is the first call after the read has taken the job's row. Ending the cancelled job
    # there stages the interleaving a poll meets by chance. If the read waits on the job's thread
    # (the join times out), the job had not left the live registry before the read classified it.
    real_checkpoint, real_owner = maintenance_jobs.checkpoint, maintenance_jobs._owner
    holding, proceed, armed = threading.Event(), threading.Event(), threading.Event()
    job_threads: list[threading.Thread] = []

    def pausing_checkpoint(phase, *args, **kwargs):
        if phase == "planning":
            job_threads.append(threading.current_thread())
            holding.set()
            assert proceed.wait(_DEADLINE_SECONDS)
        return real_checkpoint(phase, *args, **kwargs)

    def owner_once_the_job_has_ended():
        if armed.is_set():
            armed.clear()
            proceed.set()
            job_threads[0].join(1.0)
        return real_owner()

    monkeypatch.setattr(maintenance_jobs, "checkpoint", pausing_checkpoint)
    monkeypatch.setattr(maintenance_jobs, "_owner", owner_once_the_job_has_ended)

    async def scenario(client):
        preview = await _preview(client)
        handle = await _start(client, preview["plan_id"])
        assert await asyncio.to_thread(holding.wait, _DEADLINE_SECONDS)
        await _cancel(client, handle["run_id"])
        armed.set()
        read = await _status(client, handle["run_id"])
        return read, await _until_terminal(client, handle["run_id"])

    try:
        read, final = _session(server, scenario)
    finally:
        proceed.set()

    assert read["state"] in {"running", "cancelled"}
    assert final["state"] == "cancelled"


# ---------------------------------------------------------------- previews run as read-only jobs


@contextmanager
def _preview_held(monkeypatch) -> Iterator[threading.Event]:
    """A backfill-ids preview job stays running, waiting in its leaf, until the block ends.

    Yields the event the job sets once it waits there.
    """
    reached, proceed = threading.Event(), threading.Event()
    real_backfill_ids = memory_refs.backfill_ids

    def waiting(*args, **kwargs):
        reached.set()
        assert proceed.wait(_DEADLINE_SECONDS)
        return real_backfill_ids(*args, **kwargs)

    monkeypatch.setattr(memory_refs, "backfill_ids", waiting)
    try:
        yield reached
    finally:
        proceed.set()


def _as_caller(principal, surface: str, **arguments):
    """One `maintain_memory` call as `principal` on `surface`, straight through the dispatcher."""
    command = next(item for item in commands.product_commands_for("mcp") if item.name == "maintain_memory")
    descriptor = capabilities.ActiveSurfaceDescriptor(
        surface=surface, profile="test", tier2_enabled=True, product_commands=("maintain_memory",)
    )
    with capabilities.active_surface(descriptor), principal_module.request_scope(principal):
        return writer_lease.invoke_command(command, arguments.pop("vault"), **arguments)


def test_a_remote_backfill_preview_is_a_read_only_job_whose_receipt_starts_the_write(server, vault) -> None:
    expected = memory_refs.backfill_ids(vault, dry_run=True)

    async def scenario(client):
        handle, final = await _preview_job(client, {"kind": "backfill-ids"})
        planned = await _receipt_entries(client, handle["run_id"])
        untouched = _page_ids(vault)
        started = await _start(client, final["receipt"]["result"]["plan_id"])
        return handle, final, planned, untouched, await _until_terminal(client, started["run_id"])

    before = _page_ids(vault)
    handle, final, planned, untouched, written = _session(server, scenario)

    assert (handle["state"], handle["preview"], handle["plan_id"]) == ("running", True, None)
    assert final["state"] == "completed"
    assert final["receipt"]["result"]["plan_id"] == expected["plan_id"]
    assert (final["planned"], final["committed"]) == (len(expected["would_update"]), 0)
    assert [entry["path"] for entry in planned if entry["kind"] == "planned"] == expected["would_update"]
    assert untouched == before
    assert written["state"] == "completed"
    assert written["committed"] == len(expected["would_update"])
    assert all(_page_ids(vault)[path] is not None for path in expected["would_update"])


def test_a_remote_fix_preview_is_a_read_only_job_whose_receipt_starts_the_write(server, vault) -> None:
    _seed_fixable(vault)
    expected = commands.op_audit_fix(vault, dry_run=True)

    async def scenario(client):
        handle, final = await _preview_job(client, {"kind": "fix"})
        planned = await _receipt_entries(client, handle["run_id"])
        untouched = _knowledge_base_bytes(vault)
        started = await _fix_start(client, final["receipt"]["result"]["plan_id"])
        return handle, final, planned, untouched, await _until_terminal(client, started["run_id"])

    before = _knowledge_base_bytes(vault)
    handle, final, planned, untouched, written = _session(server, scenario)

    assert (handle["state"], handle["preview"], handle["plan_id"]) == ("running", True, None)
    assert final["state"] == "completed"
    assert final["receipt"]["result"]["plan_id"] == expected["plan_id"]
    assert {entry["kind"] for entry in planned} == {"planned"}
    assert {entry["path"] for entry in planned} == {row["path"] for row in expected["fixed"]} >= set(_FIXED_PAGES)
    assert untouched == before
    assert written["state"] == "completed"
    assert written["committed"] >= len(_FIXED_PAGES)


def test_a_fix_preview_receipt_lets_the_owner_page_what_the_plan_does_to_a_page(server, vault) -> None:
    _seed_fixable(vault)
    linker = "Knowledge Base/Notes/Insights/linker.md"

    async def scenario(client):
        handle, _ = await _preview_job(client, {"kind": "fix"})
        return await _receipt_entries(client, handle["run_id"])

    planned = {entry["path"]: entry for entry in _session(server, scenario)}

    assert planned[linker]["kind"] == "planned"
    assert "wikilink" in planned[linker]["value"]


def test_a_backfill_preview_receipt_pages_the_identity_issues_it_found(server, vault) -> None:
    twins = sorted(path for path in _page_ids(vault) if _page_ids(vault)[path] is None)[:2]
    shared = None
    for rel in twins:
        page = vault / rel
        updated, _ = memory_refs.add_id_to_markdown(page.read_text(encoding="utf-8"))
        frontmatter, _, _ = vault_module.parse_frontmatter(updated)
        shared = shared or frontmatter[ID_FIELD]
        page.write_text(updated.replace(frontmatter[ID_FIELD], shared), encoding="utf-8")

    async def scenario(client):
        handle, _ = await _preview_job(client, {"kind": "backfill-ids"})
        return await _receipt_entries(client, handle["run_id"])

    issues = [entry for entry in _session(server, scenario) if entry["kind"] == "identity_issue"]

    assert [entry["path"] for entry in issues] == twins
    assert all(entry["value"] == f"duplicate: {shared}" for entry in issues)


def test_a_remote_reconcile_preview_is_a_read_only_job_whose_receipt_starts_the_write(server, vault) -> None:
    _seed_reconcile(vault)

    async def scenario(client):
        handle, final = await _preview_job(client, {"kind": "reconcile", "dry_run": True})
        planned = await _receipt_entries(client, handle["run_id"])
        untouched = ((vault / _ORPHAN_FRAMES).is_dir(), (vault / "Knowledge Base" / "index.md").read_bytes())
        started = await _reconcile_start(client, final["receipt"]["result"]["plan_id"])
        return handle, final, planned, untouched, await _until_terminal(client, started["run_id"])

    handle, final, planned, (orphan_kept, index_before), written = _session(server, scenario)

    assert (handle["state"], handle["preview"], handle["plan_id"]) == ("running", True, None)
    assert final["state"] == "completed"
    assert [(entry["kind"], entry["path"], entry["value"]) for entry in planned] == [
        ("planned", _ORPHAN_FRAMES, "frames")
    ]
    assert orphan_kept and b"- Sources: 0" in index_before
    assert written["state"] == "completed"
    assert written["receipt"]["notes"]["summary"]["plan_id"] == final["receipt"]["result"]["plan_id"]
    assert not (vault / _ORPHAN_FRAMES).exists()


def test_a_preview_by_a_caller_other_than_the_owner_returns_its_report_without_a_job(server, vault) -> None:
    stranger = principal_module.RequestPrincipal(audience_id="someone-else", surface="mcp")

    async def scenario(client):
        report = _as_caller(stranger, "mcp", vault=vault, mode="backfill-ids")
        return report, await _list(client)

    report, listed = _session(server, scenario)

    assert report["plan_id"] and report["would_update"]
    assert "run_id" not in report
    assert _jobs(listed) == []


def test_a_local_preview_returns_its_report_without_a_job(server, vault) -> None:
    async def scenario(client):
        report = _as_caller(principal_module.owner_principal(surface="cli"), "cli", vault=vault, mode="backfill-ids")
        return report, await _list(client)

    report, listed = _session(server, scenario)

    assert report["plan_id"] and report["would_update"]
    assert "run_id" not in report
    assert _jobs(listed) == []


def test_a_writer_takes_the_mutation_boundary_while_a_preview_job_runs(server, vault, monkeypatch) -> None:
    async def scenario(client):
        with _preview_held(monkeypatch) as in_leaf:
            handle = await _call(client, "check_memory", {"kind": "backfill-ids"})
            assert await asyncio.to_thread(in_leaf.wait, _DEADLINE_SECONDS)
            with writer_lease.get_manager().mutation_guard(vault, request_id="other-writer", operation="semantic edit"):
                during = await _status(client, handle["run_id"])
        return during, await _until_terminal(client, handle["run_id"])

    during, final = _session(server, scenario)

    assert during["state"] == "running"
    assert final["state"] == "completed"


def test_an_identical_running_preview_returns_its_handle_and_a_write_start_is_refused(
    server, vault, monkeypatch
) -> None:
    async def scenario(client):
        with _preview_held(monkeypatch):
            first = await _call(client, "check_memory", {"kind": "backfill-ids"})
            again = await _call(client, "check_memory", {"kind": "backfill-ids"})
            refused = await _start(client, _OTHER_ID)
            listed = await _list(client)
        return first, again, refused, listed, await _until_terminal(client, first["run_id"])

    first, again, refused, listed, final = _session(server, scenario)

    assert again["run_id"] == first["run_id"]
    assert [job["run_id"] for job in _jobs(listed)] == [first["run_id"]]
    assert refused["error"]["code"] == "MAINTENANCE_JOB_ACTIVE"
    assert refused["error"]["retry"] == _read_job_retry(first["run_id"])
    assert final["state"] == "completed"


def test_a_cancelled_preview_lists_no_planned_pages(server, vault, monkeypatch) -> None:
    async def scenario(client):
        with _preview_held(monkeypatch):
            handle = await _call(client, "check_memory", {"kind": "backfill-ids"})
            cancel = await _cancel(client, handle["run_id"])
        return cancel, await _until_terminal(client, handle["run_id"])

    cancel, final = _session(server, scenario)

    assert cancel["cancel_requested"] is True
    assert final["state"] == "cancelled"
    assert (final["planned"], final["receipt"]["next_cursor"]) == (0, None)
