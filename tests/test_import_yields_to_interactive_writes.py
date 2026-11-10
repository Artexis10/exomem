"""An interactive write that meets a running import apply commits; it is not refused busy.

An import apply holds the vault write boundary for its whole call, a minute of chunks. A
`create_note` from the agent's own session or from a CLI process used to wait out its bound and
fail with MUTATION_BUSY, though the import could have stopped at a chunk boundary at any point.
The import now ends its call there, returns its continuation and releases the boundary.
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

from exomem import adoption_run, metrics, server, writer_lease

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
