from __future__ import annotations

import asyncio
import json
import typing
from pathlib import Path

import pytest
from pydantic import TypeAdapter

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


def test_product_mcp_retrieval_schemas_are_safe(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _mcp_tools(_build_server(monkeypatch, vault))

    assert "find" not in tools
    assert "get" not in tools

    ask_schema = tools["ask_memory"]["outputSchema"]
    assert "explain" in tools["ask_memory"]["inputSchema"]["properties"]
    ask_result = ask_schema["properties"]["result"]
    encoded_ask_schema = json.dumps(ask_result, sort_keys=True)
    assert "retrieval_profile" in encoded_ask_schema
    assert "ranking_explanation" in encoded_ask_schema
    assert "unit_ref" in encoded_ask_schema
    assert "parent_path" in encoded_ask_schema

    read_schema = tools["read_memory"]["outputSchema"]
    assert read_schema["type"] == "object"
    assert read_schema["additionalProperties"] is True
    read_inputs = tools["read_memory"]["inputSchema"]["properties"]
    assert {"path", "unit_ref"} <= set(read_inputs)

    result = asyncio.run(
        _build_server(monkeypatch, vault).call_tool(
            "ask_memory",
            {
                "query": "metabolism",
                "mode": "keyword",
                "scope": "kb-only",
                "explain": True,
            },
            run_middleware=False,
        )
    ).structured_content["result"]
    assert result["retrieval_profile"]["effective_mode"] == "keyword"
    assert result["hits"][0]["ranking_explanation"]["final_rank"] == 1


def test_retrieval_return_annotation_models_page_unit_mixed_and_explanation() -> None:
    annotation = typing.get_type_hints(commands.op_ask_memory)["return"]
    schema = TypeAdapter(annotation).json_schema()
    encoded = json.dumps(schema, sort_keys=True)

    assert "FindEnvelope" in encoded
    assert "PageHit" in encoded
    assert "SemanticUnitHit" in encoded
    assert "RetrievalProfile" in encoded
    assert "RankingExplanation" in encoded
    assert all(
        field in encoded
        for field in (
            "unit_ref",
            "parent_path",
            "source_anchor",
            "retrieval_profile",
            "ranking_explanation",
        )
    )


def test_legacy_mcp_leaf_names_are_opt_in(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_MCP_LEGACY_COMPAT", raising=False)
    tools = _mcp_tools(_build_server(monkeypatch, vault))

    assert "note" not in tools
    assert "create_file" not in tools

    monkeypatch.setenv("EXOMEM_MCP_LEGACY_COMPAT", "1")
    compat_tools = _mcp_tools(_build_server(monkeypatch, vault))

    assert "remember" in compat_tools
    assert "manage_memory_file" in compat_tools
    assert "note" in compat_tools
    assert "create_file" in compat_tools
    assert compat_tools["note"]["description"].startswith(
        "[Deprecated compatibility alias; prefer product commands.]"
    )
