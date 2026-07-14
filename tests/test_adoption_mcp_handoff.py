"""Adoption Studio MCP prompt/resource registrations (Lane B, design.md Decision 7).

A zero-argument `continue_adoption` prompt infers the newest open adoption run
and returns its copyable handoff `prompt_text`; MCP resources expose open runs
(`exomem://adoption/runs`) and one run by id (`exomem://adoption/run/<id>`). Both
soft-fail (no exception) when no run exists, per design.md Decision 7's
"agent handoff backbone" — the tool surface is the real mechanism; these are
progressive enhancements.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from exomem import adoption_run, find, server


def _build(monkeypatch: pytest.MonkeyPatch, vault: Path):
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    return server.build_server(require_auth=False)


def test_continue_adoption_prompt_is_registered(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _build(monkeypatch, vault)
    prompts = asyncio.run(mcp.list_prompts())
    names = {p.name for p in prompts}
    assert "continue_adoption" in names


def test_continue_adoption_soft_fails_with_no_runs(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _build(monkeypatch, vault)
    prompt = asyncio.run(mcp.get_prompt("continue_adoption", {}))
    rendered = asyncio.run(prompt.render({}))
    text = rendered.messages[0].content.text
    assert "No open Exomem adoption run" in text
    assert "adoption_studio" in text


def test_continue_adoption_infers_newest_open_run(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = vault / "Old Notes"
    old.mkdir(parents=True, exist_ok=True)
    (old / "a.md").write_text("# A\n\nSome legacy content.\n", encoding="utf-8")
    find.clear_cache()

    run = adoption_run.start(vault, path="Old Notes")
    run_id = run["run_id"]

    mcp = _build(monkeypatch, vault)
    prompt = asyncio.run(mcp.get_prompt("continue_adoption", {}))
    rendered = asyncio.run(prompt.render({}))
    text = rendered.messages[0].content.text
    assert run_id in text
    assert "work-item" in text
    assert "propose" in text


def test_continue_adoption_skips_done_and_cancelled_runs(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = vault / "Old Notes"
    old.mkdir(parents=True, exist_ok=True)
    (old / "a.md").write_text("# A\n\nSome legacy content.\n", encoding="utf-8")
    find.clear_cache()

    run = adoption_run.start(vault, path="Old Notes")
    adoption_run.cancel(vault, run_id=run["run_id"], why="testing")

    mcp = _build(monkeypatch, vault)
    prompt = asyncio.run(mcp.get_prompt("continue_adoption", {}))
    rendered = asyncio.run(prompt.render({}))
    text = rendered.messages[0].content.text
    assert "No open Exomem adoption run" in text


def test_adoption_runs_resource_lists_open_runs(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = vault / "Old Notes"
    old.mkdir(parents=True, exist_ok=True)
    (old / "a.md").write_text("# A\n\nSome legacy content.\n", encoding="utf-8")
    find.clear_cache()

    run = adoption_run.start(vault, path="Old Notes")
    run_id = run["run_id"]

    mcp = _build(monkeypatch, vault)
    resources = asyncio.run(mcp.list_resources())
    uris = {str(r.uri) for r in resources}
    assert "exomem://adoption/runs" in uris

    result = asyncio.run(mcp.read_resource("exomem://adoption/runs"))
    import json

    payload = json.loads(result.contents[0].content)
    assert any(row["run_id"] == run_id for row in payload["runs"])


def test_adoption_run_resource_reads_one_run_by_id(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = vault / "Old Notes"
    old.mkdir(parents=True, exist_ok=True)
    (old / "a.md").write_text("# A\n\nSome legacy content.\n", encoding="utf-8")
    find.clear_cache()

    run = adoption_run.start(vault, path="Old Notes")
    run_id = run["run_id"]

    mcp = _build(monkeypatch, vault)
    templates = asyncio.run(mcp.list_resource_templates())
    assert any(t.uri_template == "exomem://adoption/run/{run_id}" for t in templates)

    result = asyncio.run(mcp.read_resource(f"exomem://adoption/run/{run_id}"))
    import json

    payload = json.loads(result.contents[0].content)
    assert payload["run_id"] == run_id
    assert payload["phase"] == "selecting"


def test_adoption_run_resource_soft_fails_on_unknown_run(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _build(monkeypatch, vault)
    result = asyncio.run(mcp.read_resource("exomem://adoption/run/adr-does-not-exist"))
    import json

    payload = json.loads(result.contents[0].content)
    assert payload["error"]["code"] == "RUN_NOT_FOUND"
