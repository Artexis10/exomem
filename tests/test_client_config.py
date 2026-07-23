"""Writing MCP registration into a client's own config file.

config.toml belongs to the user and may hold every other MCP server they run, so
these tests care most about what we must NOT do: clobber unrelated content, touch
an existing registration without being asked, or leave invalid TOML behind.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from exomem import client_config

WINDOWS_VAULT = "C:" + r"\vault"

BLOCK = client_config.render_codex_block(
    "exomem", ["--transport", "stdio"], {"EXOMEM_VAULT_PATH": WINDOWS_VAULT}
)


def test_rendered_block_is_valid_toml_with_windows_paths() -> None:
    """Backslashes in a Windows vault path must be escaped, not emitted raw."""
    parsed = tomllib.loads(BLOCK)

    server = parsed["mcp_servers"]["exomem"]
    assert server["command"] == "exomem"
    assert server["args"] == ["--transport", "stdio"]
    assert server["env"]["EXOMEM_VAULT_PATH"] == WINDOWS_VAULT


def test_rendered_block_pins_stdio_transport() -> None:
    """The server defaults to http; a config without this starts a web server."""
    assert '"--transport", "stdio"' in BLOCK


def test_creates_the_file_when_codex_has_no_config_yet(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"

    outcome = client_config.merge_codex_mcp(BLOCK, path=path)

    assert outcome["action"] == "created"
    assert outcome["backup"] is None
    assert "exomem" in tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]


def test_preserves_every_other_server_and_setting(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'model = "gpt-5"\n\n'
        "[mcp_servers.other]\n"
        'command = "other-server"\n'
        'args = ["--flag"]\n',
        encoding="utf-8",
    )

    outcome = client_config.merge_codex_mcp(BLOCK, path=path)

    assert outcome["action"] == "added"
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["model"] == "gpt-5"
    assert parsed["mcp_servers"]["other"]["command"] == "other-server"
    assert parsed["mcp_servers"]["other"]["args"] == ["--flag"]
    assert parsed["mcp_servers"]["exomem"]["command"] == "exomem"


def test_existing_registration_is_reported_not_silently_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[mcp_servers.exomem]\ncommand = \"hand-tuned\"\nargs = []\n", encoding="utf-8"
    )

    outcome = client_config.merge_codex_mcp(BLOCK, path=path)

    assert outcome["action"] == "exists"
    assert outcome["backup"] is None
    # Untouched: the caller decides whether to replace it.
    assert "hand-tuned" in path.read_text(encoding="utf-8")


def test_replace_swaps_only_our_section_and_backs_the_file_up(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[mcp_servers.exomem]\n"
        'command = "hand-tuned"\n'
        "args = []\n"
        "\n"
        "[mcp_servers.keeper]\n"
        'command = "keep-me"\n',
        encoding="utf-8",
    )

    outcome = client_config.merge_codex_mcp(BLOCK, path=path, replace=True)

    assert outcome["action"] == "replaced"
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["mcp_servers"]["exomem"]["command"] == "exomem"
    assert parsed["mcp_servers"]["keeper"]["command"] == "keep-me"
    # The previous content survives on disk for recovery.
    assert "hand-tuned" in Path(outcome["backup"]).read_text(encoding="utf-8")


def test_diff_shows_the_user_what_changed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('model = "gpt-5"\n', encoding="utf-8")

    outcome = client_config.merge_codex_mcp(BLOCK, path=path)

    assert "+[mcp_servers.exomem]" in outcome["diff"]


def test_refuses_to_write_when_the_result_would_not_parse(tmp_path: Path) -> None:
    """A corrupted config.toml would break every MCP server the user runs."""
    path = tmp_path / "config.toml"
    original = 'model = "gpt-5"\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="invalid TOML"):
        client_config.merge_codex_mcp("[mcp_servers.exomem\ncommand = ", path=path)

    assert path.read_text(encoding="utf-8") == original


def test_codex_home_follows_the_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))

    assert client_config.codex_config_path() == tmp_path / "elsewhere" / "config.toml"
