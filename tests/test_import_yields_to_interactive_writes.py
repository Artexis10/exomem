"""An interactive write that meets a running import step commits; it is not refused busy.

An import apply holds the vault write boundary for its whole call, a minute of chunks. A
`create_note` from the agent's own session or from a CLI process used to wait out its bound and
fail with MUTATION_BUSY, though the import could have stopped at a chunk boundary at any point.
The import now ends its call there, returns its continuation and releases the boundary.

The `plan` step and the `file` call's chain of steps hold the boundary as well. Each ends at its
next item or step boundary when a writer waits, and no step starts an item once the boundary's own
overdue limit is near, which is what a writer is refused on.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from _public_call import public_call
from fastmcp import Client
from test_adoption_paging import PDF, _stage
from test_app_export_import import claude_conversation

from exomem import (
    adoption_plan,
    adoption_run,
    command_surface,
    export_expansion,
    import_stage,
    metrics,
    mutation_lock,
    request_budget,
    server,
    writer_lease,
)

DOCUMENTS = 8
CHUNK_ITEMS = 2
CHUNK_SECONDS = 1.5
# A write's whole wait is shorter than the import's call, so only an early release can serve it.
CAPTURE_WAIT_SECONDS = 3
BOUNDARY_ATTEMPT_SECONDS = 2

# The CLI process loads its modules before the gate opens, so that it reaches the boundary while
# the first chunk still runs. Everything after the gate is the shipped CLI entry point.
GATED_CLI = (
    "import pathlib, sys, time\n"
    "import exomem.commands, exomem.product_invoke, exomem.writer_lease\n"
    "from exomem.__main__ import main\n"
    "gate = pathlib.Path(sys.argv[1])\n"
    "while not gate.exists():\n"
    "    time.sleep(0.01)\n"
    "sys.exit(main(sys.argv[2:]))\n"
)

NOTE = (
    "## Observations\n\n- [constraint] Keep {name} writes off the import's clock.\n\n"
    "## Relations\n\n- links_to [[Knowledge Base/Notes/Patterns/retry-with-full-jitter-backoff]]\n"
)


def _busy_total() -> int:
    return sum(
        counter["value"]
        for counter in metrics.snapshot()["counters"]
        if counter["name"] == "exomem_mutation_busy_total"
    )


def _journal(logs: Path) -> list[dict]:
    path = logs / "mutations.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_agent_and_cli_writes_commit_while_an_import_apply_runs(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(logs))
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", str(CAPTURE_WAIT_SECONDS))
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", str(BOUNDARY_ATTEMPT_SECONDS))
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(adoption_run, "APPLY_CHUNK_ITEMS", CHUNK_ITEMS)
    writer_lease.reset_managers_for_tests()

    folder = vault / "Legacy"
    folder.mkdir()
    for number in range(DOCUMENTS):
        (folder / f"document-{number}.md").write_text(
            f"# Document {number}\n\nOriginal material {number}.\n", encoding="utf-8"
        )
    run = adoption_run.start(vault, path="Legacy", consume=False)
    run_id = run["run_id"]
    adoption_run.select(vault, run_id=run_id, include=["Legacy"])
    plan_id = adoption_run.plan(vault, run_id=run_id)["plan"]["plan_id"]
    sources_before = set((vault / "Knowledge Base" / "Sources").rglob("*.md"))

    chunk_running = threading.Event()
    real_commit = adoption_run._commit_chunk

    def slow_commit(*args, **kwargs):
        result = real_commit(*args, **kwargs)
        chunk_running.set()
        time.sleep(CHUNK_SECONDS)
        return result

    monkeypatch.setattr(adoption_run, "_commit_chunk", slow_commit)

    gate = tmp_path / "cli-gate"
    cli = subprocess.Popen(
        [
            sys.executable, "-c", GATED_CLI, str(gate), "create_note", "--json",
            "--title", "Cli write beside the import", "--content", NOTE.format(name="CLI"),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(adoption_run.__file__).parents[1])},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    apply_args = {
        "operation": {"kind": "apply", "run_id": run_id, "plan_id": plan_id},
        "response_detail": "full",
    }
    busy_before = _busy_total()

    async def journey() -> tuple[list[dict], dict]:
        async with Client(server.build_server(require_auth=False)) as client:
            started = asyncio.create_task(client.call_tool(*public_call("import_memory", apply_args), raise_on_error=False))
            assert await asyncio.to_thread(chunk_running.wait, 30), "the import never reached a chunk"
            gate.touch()
            agent_write = asyncio.create_task(client.call_tool(*public_call("create_note", {
                "title": "Agent write beside the import", "content": NOTE.format(name="agent"),
            }), raise_on_error=False))
            replies = [(await started).structured_content]
            note = (await agent_write).structured_content
            # The agent's next call comes a turn later, after the writes it waited for.
            await asyncio.to_thread(cli.wait, 60)
            while replies[-1].get("continuation"):
                operation = {**apply_args["operation"], "continuation": replies[-1]["continuation"]}
                result = await client.call_tool(
                    *public_call("import_memory", {**apply_args, "operation": operation}), raise_on_error=False
                )
                replies.append(result.structured_content)
            return replies, note

    try:
        replies, note = asyncio.run(journey())
    finally:
        cli.kill()
    cli_out, cli_err = cli.communicate()

    refused = "MUTATION_BUSY" in json.dumps([replies, note]) + cli_out + cli_err
    assert not refused, (note.get("error"), cli_out, cli_err)
    assert [row for row in _journal(logs) if row["error_code"] == "MUTATION_BUSY"] == []
    assert _busy_total() == busy_before

    assert "error" not in note and note.get("id"), note
    assert note["title"] == "Agent write beside the import", note
    assert cli.returncode == 0, (cli_out, cli_err)
    assert json.loads(cli_out)["success"] is True, cli_out
    for name in ("Agent write beside the import", "Cli write beside the import"):
        written = [
            path for path in (vault / "Knowledge Base" / "Notes").rglob("*.md")
            if name in path.read_text(encoding="utf-8")
        ]
        assert len(written) == 1, (name, written)

    assert len(replies) > 1 and replies[0].get("continuation"), "the first call ran the whole import"
    assert replies[0]["stop_reason"] == adoption_run.STOP_FOR_WAITING_WRITE
    final = replies[-1]
    assert final["progress"] == {"applied": DOCUMENTS, "failed": 0, "remaining": 0, "total": DOCUMENTS}
    sources = set((vault / "Knowledge Base" / "Sources").rglob("*.md")) - sources_before
    assert len(sources) == DOCUMENTS
    assert sum(len(reply["paths"]) for reply in replies) == DOCUMENTS


# --------------------------------------------------------------------------- #
# The `plan` step and the `file` call's chain of steps
# --------------------------------------------------------------------------- #
STAGED_FILES = 6
# The boundary's overdue limit, scaled down from 30 s: a writer is refused `holder_overdue` once a hold reaches it.
OVERDUE_SECONDS = 3.0
# Kept back from a call's end for what follows its last item; scaled down with the limit.
RESERVE_SECONDS = 1.0


@pytest.fixture
def scaled_boundary(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A vault whose boundary turns overdue after `OVERDUE_SECONDS`, and the log folder that records its refusals."""
    logs = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(logs))
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", str(CAPTURE_WAIT_SECONDS))
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", str(BOUNDARY_ATTEMPT_SECONDS))
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        mutation_lock.VaultMutationCoordinator.__init__.__kwdefaults__, "long_holder_seconds", OVERDUE_SECONDS
    )
    monkeypatch.setattr(request_budget, "DELIVERY_RESERVE_SECONDS", RESERVE_SECONDS)
    writer_lease.reset_managers_for_tests()
    yield logs
    writer_lease.reset_managers_for_tests()


def _settle(monkeypatch: pytest.MonkeyPatch, settled: threading.Event) -> None:
    """The writers have committed: stop slowing the import and give its calls the default room again."""
    settled.set()
    monkeypatch.setitem(mutation_lock.VaultMutationCoordinator.__init__.__kwdefaults__, "long_holder_seconds", 30.0)
    monkeypatch.setattr(request_budget, "DELIVERY_RESERVE_SECONDS", 5.0)


def _slowed(monkeypatch: pytest.MonkeyPatch, module, name: str, seconds: float, *, running: threading.Event, settled: threading.Event) -> None:
    """Make each call of `module.name` take `seconds` until `settled`, and say when the first one starts."""
    real = getattr(module, name)

    def slow(*args, **kwargs):
        running.set()
        if not settled.is_set():
            time.sleep(seconds)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, slow)


async def _file(client: Client, key: str, staged: str, continuation: str | None = None) -> dict:
    """One `imports.file` call under its own idempotency key; the reply is what the agent reads."""
    operation = {"kind": "file", "path": staged, **({"continuation": continuation} if continuation else {})}
    result = await client.call_tool(
        *public_call("import_memory", {"operation": operation}), raise_on_error=False,
        meta={command_surface.MCP_IDEMPOTENCY_META_KEY: key},
    )
    return result.structured_content


async def _file_to_done(client: Client, staged: str, reply: dict, prefix: str) -> dict:
    for number in range(60):
        if reply.get("filing") != "working":
            return reply
        reply = await _file(client, f"{prefix}-{number}", staged, reply["continuation"])
    raise AssertionError("filing never ended in 60 calls")


def _remember(name: str) -> dict:
    return {"title": f"{name} write beside the import", "content": NOTE.format(name=name)}


def _notes_titled(vault: Path, name: str) -> list[Path]:
    return [
        path for path in (vault / "Knowledge Base" / "Notes").rglob("*.md")
        if f"{name} write beside the import" in path.read_text(encoding="utf-8")
    ]


def _staged_folder(vault: Path) -> str:
    return _stage(vault, {f"doc{number}.pdf": PDF + bytes([number]) for number in range(STAGED_FILES)})


def test_agent_and_cli_writes_commit_while_a_slow_plan_runs(
    vault: Path, scaled_boundary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a `plan` loop that holds the boundary for its whole budget while writers wait for it."""
    staged = _staged_folder(vault)
    running, settled = threading.Event(), threading.Event()
    _slowed(monkeypatch, adoption_plan, "plan_item", 0.6, running=running, settled=settled)
    gate = tmp_path / "cli-gate"
    cli = subprocess.Popen(
        [
            sys.executable, "-c", GATED_CLI, str(gate), "create_note", "--json",
            "--title", "Cli write beside the import", "--content", NOTE.format(name="CLI"),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(adoption_run.__file__).parents[1])},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    busy_before = _busy_total()

    async def journey() -> tuple[dict, dict, dict]:
        async with Client(server.build_server(require_auth=False)) as client:
            filing = asyncio.create_task(_file(client, "file-0", staged))
            assert await asyncio.to_thread(running.wait, 30), "the import never reached a plan item"
            gate.touch()
            agent_write = await client.call_tool(*public_call("create_note", _remember("agent")), raise_on_error=False)
            first = await filing
            await asyncio.to_thread(cli.wait, 60)
            _settle(monkeypatch, settled)
            return first, agent_write.structured_content, await _file_to_done(client, staged, first, "next")

    try:
        first, note, done = asyncio.run(journey())
    finally:
        cli.kill()
    cli_out, cli_err = cli.communicate()

    assert "MUTATION_BUSY" not in json.dumps([first, note, done]) + cli_out + cli_err, (note, cli_out, cli_err)
    assert [row for row in _journal(scaled_boundary) if row["error_code"] == "MUTATION_BUSY"] == []
    assert _busy_total() == busy_before
    assert len(_notes_titled(vault, "agent")) == 1 and len(_notes_titled(vault, "Cli")) == 1
    assert cli.returncode == 0 and json.loads(cli_out)["success"] is True, (cli_out, cli_err)
    # The first call ended partway through the page, so the writers did not wait out its plan.
    assert first["filing"] == "working" and first["progress"]["page"]["planned"] < STAGED_FILES, first
    assert done.get("filing") == "done" and done["progress"]["filed"] == STAGED_FILES, done


def test_a_second_file_call_is_served_while_a_slow_plan_runs(
    vault: Path, scaled_boundary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the agent's next `file` call refused busy because the call before it planned past the overdue limit."""
    staged = _staged_folder(vault)
    running = threading.Event()
    _slowed(monkeypatch, adoption_plan, "plan_item", 0.6, running=running, settled=threading.Event())

    async def journey() -> tuple[dict, dict]:
        async with Client(server.build_server(require_auth=False)) as client:
            filing = asyncio.create_task(_file(client, "file-a", staged))
            assert await asyncio.to_thread(running.wait, 30), "the import never reached a plan item"
            second = await _file(client, "file-b", staged)
            return await filing, second

    first, second = asyncio.run(journey())

    assert "MUTATION_BUSY" not in json.dumps([first, second]), (first, second)
    assert first["filing"] == "working" and first["progress"]["page"]["planned"] < STAGED_FILES, first
    # The second call took its turn and carried the plan on from where the first left it.
    assert second["filing"] == "working" and second["progress"]["page"]["planned"] > first["progress"]["page"]["planned"], second


def test_a_file_call_ends_between_its_steps_when_a_writer_waits(
    vault: Path, scaled_boundary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a `file` call that runs its next step, and the first item of it, with a writer already waiting."""
    staged = _staged_folder(vault)
    running, never = threading.Event(), threading.Event()
    # The selection takes 2 s and the first plan item 1.5 s: only a call that ends after the selection
    # releases the boundary before the overdue limit.
    _slowed(monkeypatch, adoption_run, "select", 2.0, running=running, settled=never)
    _slowed(monkeypatch, adoption_plan, "plan_item", 1.5, running=threading.Event(), settled=never)

    async def journey() -> tuple[dict, dict]:
        async with Client(server.build_server(require_auth=False)) as client:
            filing = asyncio.create_task(_file(client, "file-0", staged))
            assert await asyncio.to_thread(running.wait, 30), "the import never reached its selection"
            note = await client.call_tool(*public_call("create_note", _remember("agent")), raise_on_error=False)
            return await filing, note.structured_content

    first, note = asyncio.run(journey())

    assert "MUTATION_BUSY" not in json.dumps([first, note]), (first, note)
    assert len(_notes_titled(vault, "agent")) == 1
    assert first["progress"]["page"] == {"number": 1, "phase": "selecting", "planned": 0}, first


def test_a_file_call_that_no_writer_waits_for_ends_before_the_overdue_limit(
    vault: Path, scaled_boundary: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Catches a plan that holds the boundary past its overdue limit because its request budget is longer."""
    staged = _stage(vault, {f"doc{number}.pdf": PDF + bytes([number]) for number in range(20)})
    settled = threading.Event()
    _slowed(monkeypatch, adoption_plan, "plan_item", 0.25, running=threading.Event(), settled=settled)

    async def journey() -> dict:
        async with Client(server.build_server(require_auth=False)) as client:
            return await _file(client, "file-0", staged)

    with caplog.at_level("WARNING", logger=mutation_lock.__name__):
        first = asyncio.run(journey())

    assert not [record for record in caplog.records if "held too long" in record.getMessage()]
    assert first["filing"] == "working" and 0 < first["progress"]["page"]["planned"] < 20, first


def test_a_finish_call_ends_for_a_waiting_write_and_the_next_call_removes_the_rest(
    vault: Path, scaled_boundary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a `finish` that removes every staged copy in one hold, or one that strands the rest when it yields."""
    staged = _stage(vault, {f"note{number}.md": f"# Note {number}\n\nInvented text {number}.\n".encode() for number in range(STAGED_FILES)})
    page = adoption_run.start(vault, path=staged, collection="Field Notes")
    adoption_run.select(vault, run_id=page["run_id"], include=[staged])
    plan_id = adoption_run.plan(vault, run_id=page["run_id"])["plan"]["plan_id"]
    reply = adoption_run.apply(vault, run_id=page["run_id"], plan_id=plan_id)
    while reply.get("continuation"):
        reply = adoption_run.apply(vault, run_id=page["run_id"], plan_id=plan_id, continuation=reply["continuation"])
    assert reply["progress"]["remaining"] == 0

    from exomem import adoption_consume

    running, removals = threading.Event(), []
    real_remove = adoption_consume.remove_staged

    def slow_remove(*args, **kwargs):
        running.set()
        removals.append(args[2]["original_path"])
        time.sleep(0.6)
        return real_remove(*args, **kwargs)

    monkeypatch.setattr(adoption_consume, "remove_staged", slow_remove)

    async def journey() -> tuple[dict, dict, dict]:
        async with Client(server.build_server(require_auth=False)) as client:
            finish_args = {"operation": {"kind": "finish", "run_id": page["run_id"]}}
            finishing = asyncio.create_task(client.call_tool(*public_call("import_memory", finish_args), raise_on_error=False))
            assert await asyncio.to_thread(running.wait, 30), "the import never reached a staged copy"
            note = await client.call_tool(*public_call("create_note", _remember("agent")), raise_on_error=False)
            first = (await finishing).structured_content
            _settle(monkeypatch, threading.Event())
            second = await client.call_tool(*public_call("import_memory", finish_args), raise_on_error=False)
            return first, note.structured_content, second.structured_content

    first, note, second = asyncio.run(journey())

    assert "MUTATION_BUSY" not in json.dumps([first, note, second]), (first, note, second)
    assert len(_notes_titled(vault, "agent")) == 1
    assert first["stop_reason"] == adoption_run.STOP_FOR_WAITING_WRITE and first["phase"] == "applied", first
    finished = adoption_run.AdoptionRunStore(vault).load(page["run_id"])
    assert finished["phase"] == "done" and finished["finish"]["consumed"] == STAGED_FILES, (second, finished["finish"])
    assert sorted(removals) == sorted(set(removals)) and len(removals) == STAGED_FILES, removals
    assert not any((vault / staged / f"note{number}.md").exists() for number in range(STAGED_FILES))


def test_a_slow_first_read_of_the_sources_ends_for_a_waiting_write_and_the_plan_goes_on(
    vault: Path, scaled_boundary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a plan whose read of the earlier imports cannot stop, on a vault with many Sources the process has not read."""
    staged = _staged_folder(vault)
    running, settled = threading.Event(), threading.Event()
    monkeypatch.setattr(adoption_plan, "INDEX_READ_FLOOR", 1)
    # Five Source pages at 0.9 s each take longer than the room that the scaled overdue limit leaves a call.
    _slowed(monkeypatch, adoption_plan, "page_import_entries", 0.9, running=running, settled=settled)

    async def journey() -> tuple[dict, dict, dict]:
        async with Client(server.build_server(require_auth=False)) as client:
            filing = asyncio.create_task(_file(client, "file-0", staged))
            assert await asyncio.to_thread(running.wait, 30), "the plan never read an earlier Source"
            note = await client.call_tool(*public_call("create_note", _remember("agent")), raise_on_error=False)
            first = await filing
            _settle(monkeypatch, settled)
            return first, note.structured_content, await _file_to_done(client, staged, first, "next")

    first, note, done = asyncio.run(journey())

    assert "MUTATION_BUSY" not in json.dumps([first, note, done]), (first, note, done)
    assert len(_notes_titled(vault, "agent")) == 1
    assert first["filing"] == "working" and first["progress"]["page"]["planned"] == 0, first
    assert done.get("filing") == "done" and done["progress"]["filed"] == STAGED_FILES, done


def test_a_file_call_ends_an_export_expansion_for_a_waiting_write_and_the_next_call_goes_on(
    vault: Path, scaled_boundary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an export expansion that reads every conversation of a large export in one hold while a writer waits."""
    conversations = [claude_conversation(number) for number in range(12)]
    staged = _stage(vault, {"conversations.json": json.dumps(conversations).encode()})
    running, settled = threading.Event(), threading.Event()
    # Twelve conversations at 0.4 s take longer than the room that the scaled overdue limit leaves a call.
    _slowed(monkeypatch, export_expansion, "_take", 0.4, running=running, settled=settled)

    async def journey() -> tuple[dict, dict, dict, int]:
        async with Client(server.build_server(require_auth=False)) as client:
            filing = asyncio.create_task(_file(client, "file-0", staged))
            assert await asyncio.to_thread(running.wait, 30), "the import never reached a conversation"
            note = await client.call_tool(*public_call("create_note", _remember("agent")), raise_on_error=False)
            first = await filing
            read = len(import_stage.read_expansion(vault, staged.rsplit("/", 1)[1])["rows"])
            _settle(monkeypatch, settled)
            return first, note.structured_content, await _file_to_done(client, staged, first, "next"), read

    first, note, done, read = asyncio.run(journey())

    assert "MUTATION_BUSY" not in json.dumps([first, note, done]), (first, note, done)
    assert len(_notes_titled(vault, "agent")) == 1
    # The first call stopped as soon as the writer waited: one more conversation at most, not the room of the call.
    assert 0 < read <= 3 and first["filing"] == "working", (read, first)
    assert done.get("filing") == "done" and done["progress"]["filed"] == len(conversations) + 1, done
