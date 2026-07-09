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
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return server_module.build_server(require_auth=False)


def _mcp_tools(mcp) -> dict[str, dict]:
    tools = asyncio.run(mcp.list_tools())
    return {t.name: t.to_mcp_tool().model_dump(mode="json") for t in tools}


def test_find_compact_is_metadata_only(vault: Path) -> None:
    out = commands.op_find(vault, query="metabolism", detail="compact")

    assert isinstance(out, list)
    assert out, "fixture vault should match 'metabolism'"
    for hit in out:
        assert {"path", "type", "scope", "title", "updated"} <= set(hit)
        serialized = json.dumps(hit)
        assert "excerpt" not in serialized
        assert "signals" not in serialized
        assert "body" not in serialized


def test_get_can_return_bounded_body(vault: Path) -> None:
    path = vault / "Knowledge Base" / "Notes" / "Insights" / "long-safe-get.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        "title: Long Safe Get\n"
        "status: active\n"
        "created: 2026-07-08\n"
        "updated: 2026-07-08\n"
        "sources: []\n"
        "tags: [chatgpt-safe]\n"
        "---\n\n"
        + ("alpha beta gamma delta\n" * 200),
        encoding="utf-8",
    )
    find_module.clear_cache()

    out = commands.op_get(
        vault,
        path="Knowledge Base/Notes/Insights/long-safe-get.md",
        max_body_chars=600,
    )

    assert out["path"] == "Knowledge Base/Notes/Insights/long-safe-get.md"
    assert len(out["body"]) <= 600
    assert out["body"].endswith("[truncated]")
    assert out["body_truncated"] is True
    assert out["body_chars"] == len(out["body"])


def test_find_get_mcp_output_schemas_are_concrete(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _mcp_tools(_build_server(monkeypatch, vault))

    find_schema = tools["find"]["outputSchema"]
    find_result = find_schema["properties"]["result"]
    first_shape = find_result["anyOf"][0]
    hit_schema = first_shape["items"]
    assert {"path", "type", "scope", "title", "updated"} <= set(
        hit_schema["required"]
    )
    assert "excerpt" in hit_schema["properties"]
    assert hit_schema["type"] == "object"

    get_schema = tools["get"]["outputSchema"]
    assert get_schema["type"] == "object"
    assert get_schema["required"] == ["path", "frontmatter"]
    assert "body" in get_schema["properties"]
    assert "body_truncated" in get_schema["properties"]
    assert "max_body_chars" in tools["get"]["inputSchema"]["properties"]
