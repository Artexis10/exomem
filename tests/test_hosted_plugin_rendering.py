from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import hosted_plugins


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_openai_candidate_requires_registered_app_release_input(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="registered OpenAI app"):
        hosted_plugins.render(REPO_ROOT, tmp_path / "generated")


def test_rendered_packages_are_deterministic_and_remote_only(tmp_path: Path) -> None:
    first = hosted_plugins.render(REPO_ROOT, tmp_path / "first", openai_app_id="asdk_app_releaseinput123")
    second = hosted_plugins.render(REPO_ROOT, tmp_path / "second", openai_app_id="asdk_app_releaseinput123")

    def contents(root: Path) -> dict[str, bytes]:
        return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    assert contents(first) == contents(second)
    claude_mcp = json.loads((first / "claude/.mcp.json").read_text(encoding="utf-8"))
    openai_app = json.loads((first / "openai/.app.json").read_text(encoding="utf-8"))
    assert claude_mcp["mcpServers"]["exomem"] == {
        "type": "http", "url": "https://substratesystems.io/api/exomem/mcp/v1"
    }
    assert openai_app["apps"]["exomem"] == {"id": "asdk_app_releaseinput123", "required": True}
    assert openai_app["authentication"]["policy"] == "ON_INSTALL"
    assert ".codex-plugin/plugin.json" in contents(first)
    assert "uvx" not in b"\n".join(contents(first).values()).decode("utf-8")
