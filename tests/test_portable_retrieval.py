from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from exomem import commands
from exomem import find as find_module
from exomem import server as server_module


def _build_server(monkeypatch: pytest.MonkeyPatch, vault: Path):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return server_module.build_server(require_auth=False)


def _mcp_tools(mcp) -> dict[str, dict]:
    tools = asyncio.run(mcp.list_tools())
    return {t.name: t.to_mcp_tool().model_dump(mode="json", by_alias=True) for t in tools}


def test_search_returns_metadata_only(vault: Path) -> None:
    out = commands.op_search(vault, query="jitter", limit=5)

    assert set(out) == {"results"}
    assert out["results"]
    serialized = json.dumps(out)
    assert "breathing room" not in serialized
    assert "excerpt" not in serialized
    assert "signals" not in serialized
    assert "pack" not in serialized
    assert "timings" not in serialized
    assert "body" not in serialized

    first = out["results"][0]
    assert set(first) == {"id", "title", "url", "metadata"}
    assert first["id"].startswith("Knowledge Base/")
    assert first["metadata"]["path"] == first["id"]
    assert all(isinstance(value, str) for value in first["metadata"].values())


def test_fetch_returns_bounded_document_text(vault: Path) -> None:
    rel = Path("Knowledge Base/Notes/Patterns/long-fetch-test.md")
    body = "# Long Fetch\n\n" + "alpha beta gamma delta " * 120
    (vault / rel).write_text(
        "---\n"
        "type: pattern\n"
        "status: active\n"
        "title: Long Fetch\n"
        "tags: [portable, retrieval]\n"
        "---\n\n"
        + body,
        encoding="utf-8",
    )
    find_module.clear_cache()

    out = commands.op_fetch(vault, id=rel.as_posix(), max_chars=600)

    assert out["id"] == rel.as_posix()
    assert out["title"] == "Long Fetch"
    assert len(out["text"]) <= 600
    assert out["text"].endswith("[truncated]")
    assert out["metadata"]["truncated"] == "true"
    assert out["metadata"]["path"] == rel.as_posix()
    assert "frontmatter" not in out
    assert "content_hash" not in out
    assert "content" not in out


def test_product_mcp_retrieval_surface_replaces_portable_primitives(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _mcp_tools(_build_server(monkeypatch, vault))

    assert "search" not in tools
    assert "fetch" not in tools

    for name in ("search_memory", "read_memory"):
        annotations = tools[name]["annotations"]
        assert annotations["readOnlyHint"] is True
        assert annotations["destructiveHint"] is False
        assert annotations["openWorldHint"] is False

    ask_inputs = tools["search_memory"]["inputSchema"]["properties"]
    read_operation = tools["read_memory"]["inputSchema"]["properties"]["operation"]
    assert read_operation["discriminator"]["propertyName"] == "kind"
    page = next(branch for branch in read_operation["oneOf"] if branch["properties"]["kind"]["const"] == "page")
    read_inputs = {**page["properties"], **read_operation.get("properties", {})}
    assert {"query", "detail", "deep", "include_timings", "explain"} <= set(
        ask_inputs
    )
    assert "pack" not in ask_inputs
    assert {"path", "frontmatter_only", "include_raw", "links", "unit_ref"} <= set(
        read_inputs
    )
    assert {"kind", "path"} <= set(page["required"])

    mcp = _build_server(monkeypatch, vault)
    search = asyncio.run(mcp.call_tool(
        "search_memory", {"query": "jitter", "mode": "keyword"}, run_middleware=True
    )).structured_content["result"]
    assert search["hits"]
    path = search["hits"][0]["path"]
    read = asyncio.run(mcp.call_tool(
        "read_memory", {"operation": {"kind": "page", "path": path}}, run_middleware=True
    )).structured_content
    expected = commands.op_read_memory(vault, path=path)
    assert read == json.loads(json.dumps(expected, default=str))
