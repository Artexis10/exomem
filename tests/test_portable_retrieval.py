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
    return {t.name: t.to_mcp_tool().model_dump(mode="json") for t in tools}


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


def test_search_fetch_mcp_shape_is_portable(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _mcp_tools(_build_server(monkeypatch, vault))

    for name in ("search", "fetch"):
        annotations = tools[name]["annotations"]
        assert annotations["readOnlyHint"] is True
        assert annotations["destructiveHint"] is False
        assert annotations["openWorldHint"] is False

    search_schema = tools["search"]["outputSchema"]
    assert search_schema["type"] == "object"
    assert search_schema["required"] == ["results"]
    result_schema = search_schema["properties"]["results"]["items"]
    assert result_schema["required"] == ["id", "title", "url", "metadata"]
    assert set(result_schema["properties"]) == {"id", "title", "url", "metadata"}
    assert result_schema["properties"]["metadata"]["additionalProperties"] == {
        "type": "string"
    }

    fetch_schema = tools["fetch"]["outputSchema"]
    assert fetch_schema["type"] == "object"
    assert fetch_schema["required"] == ["id", "title", "text", "url"]
    assert set(fetch_schema["properties"]) == {"id", "title", "text", "url", "metadata"}
    assert fetch_schema["properties"]["metadata"]["additionalProperties"] == {
        "type": "string"
    }

    search_inputs = tools["search"]["inputSchema"]["properties"]
    fetch_inputs = tools["fetch"]["inputSchema"]["properties"]
    for forbidden in ("pack", "detail", "include_timings", "include_raw", "frontmatter_only"):
        assert forbidden not in search_inputs
        assert forbidden not in fetch_inputs
