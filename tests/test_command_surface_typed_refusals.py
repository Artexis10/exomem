"""Domain refusals reach MCP clients as the coded envelope, not a bare ToolError."""

from __future__ import annotations

import asyncio
from pathlib import Path

from exomem import server


def test_edit_refusal_over_mcp_carries_its_code(
    vault: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "load_dotenv", lambda *_a, **_k: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    rel = "Knowledge Base/Notes/Research/Taste/scratch-taste.md"
    page = vault / rel
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: research-note\nproject: taste\nstatus: active\n"
        "created: 2026-05-29\nupdated: 2026-05-29\ntags: [taste]\n---\n# Scratch\n\nbody.\n",
        encoding="utf-8",
    )
    mcp = server.build_server(require_auth=False)

    result = asyncio.run(
        mcp.call_tool(
            "edit_memory",
            {
                "path": rel,
                "why": "fix a typo",
                "operation": {
                    "kind": "replace_string",
                    "old_string": "text that is not on the page",
                    "new_string": "x",
                },
            },
            run_middleware=True,
        )
    )

    assert result.structured_content["success"] is False
    assert result.structured_content["error"]["code"] == "STRING_NOT_FOUND"
