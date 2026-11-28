"""The route the core skill gives for saving an owner skill works through the public tools.

The shipped skill tells an agent to save a repeated method as an owner skill with one
`manage_memory` `files.create` call and to revise it with `edit_note`. If either call is
refused for a skill page, the agent cannot follow the skill, so this test runs both and checks
that the result is a page the catalogue recognises as a skill.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from exomem import server as server_module
from exomem import vault_skills
from exomem.vault import parse_frontmatter

SKILL_PATH = "Knowledge Base/Skills/tasting-notes/SKILL.md"


def _tool(mcp, name: str, args: dict) -> dict:
    result = asyncio.run(mcp.call_tool(name, args))
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    return json.loads(next(c.text for c in result.content if getattr(c, "text", None)))


def test_an_owner_skill_saved_and_revised_through_the_public_tools_is_a_skill(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    mcp = server_module.build_server(require_auth=False)

    created = _tool(mcp, "manage_memory", {"operation": "files.create", "arguments": {
        "path": SKILL_PATH,
        "frontmatter": {"name": "tasting-notes",
                        "description": "Use when the user describes a coffee they tried."},
        "content": "# Tasting notes\n\nBuilds on evermatter-capture. Add one row per coffee to Tasting notes.\n",
    }})
    assert (vault / SKILL_PATH).is_file(), created

    revised = _tool(mcp, "edit_note", {
        "note": SKILL_PATH, "action": "replace_text", "why": "The user added the roast date.",
        "old_text": "one row per coffee", "new_text": "one row per coffee, with the roast date",
    })
    text = (vault / SKILL_PATH).read_text(encoding="utf-8")
    assert "with the roast date" in text, revised

    frontmatter, _, _ = parse_frontmatter(text)
    skill = vault_skills.recognise(SKILL_PATH, frontmatter)
    assert skill is not None and skill.name == "tasting-notes"
