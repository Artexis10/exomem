from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import hosted_plugins


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_openai_candidate_requires_registered_app_release_input(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="registered OpenAI app"):
        hosted_plugins.render(
            REPO_ROOT, tmp_path / "generated", platform="openai", staging_root=tmp_path
        )


def test_claude_candidate_can_render_and_check_without_openai_registration(tmp_path: Path) -> None:
    rendered = hosted_plugins.render(
        REPO_ROOT, tmp_path / "generated", platform="claude", staging_root=tmp_path
    )

    assert (rendered / "claude/.claude-plugin/plugin.json").is_file()
    assert not (rendered / "openai").exists()


def test_rendered_packages_are_deterministic_and_remote_only(tmp_path: Path) -> None:
    first = hosted_plugins.render(REPO_ROOT, tmp_path / "first", openai_app_id="asdk_app_releaseinput123", platform="all", staging_root=tmp_path)
    second = hosted_plugins.render(REPO_ROOT, tmp_path / "second", openai_app_id="asdk_app_releaseinput123", platform="all", staging_root=tmp_path)

    def contents(root: Path) -> dict[str, bytes]:
        return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    assert contents(first) == contents(second)
    claude_mcp = json.loads((first / "claude/.mcp.json").read_text(encoding="utf-8"))
    openai_app = json.loads((first / "openai/.app.json").read_text(encoding="utf-8"))
    assert claude_mcp["mcpServers"]["exomem"] == {
        "type": "http", "url": "https://substratesystems.io/api/exomem/mcp/v1"
    }
    openai_plugin = json.loads((first / "openai/.codex-plugin/plugin.json").read_text(encoding="utf-8"))
    assert openai_plugin["skills"] == "./skills/"
    assert openai_plugin["mcpServers"] == "./.mcp.json"
    assert openai_plugin["apps"] == "./.app.json"
    assert openai_app == {"apps": {"exomem": {"id": "asdk_app_releaseinput123", "category": "productivity"}}}
    marketplace = json.loads((first / "openai/marketplace.json").read_text(encoding="utf-8"))
    assert marketplace["plugins"][0]["policy"]["authentication"] == "ON_INSTALL"
    assert marketplace["interface"]["defaultPrompt"] == ["Use governed long-term memory."]
    hosted_plugins.validate_openai_candidate(first / "openai")
    assert "uvx" not in b"\n".join(contents(first).values()).decode("utf-8")
