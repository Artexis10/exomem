"""Writing MCP registration into a client's own config file.

config.toml belongs to the user and may hold every other MCP server they run, so
these tests care most about what we must NOT do: clobber unrelated content, touch
an existing registration without being asked, or leave invalid TOML behind.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

from exomem import client_config

WINDOWS_VAULT = "C:" + r"\vault"

def _stdio_block() -> str:
    route = client_config.McpRoute.stdio(
        ["exomem", "--transport", "stdio"],
        {"EXOMEM_VAULT_PATH": WINDOWS_VAULT},
    )
    return client_config.render_codex_block(route)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://kb.example.com", "https://kb.example.com/mcp"),
        ("https://kb.example.com/", "https://kb.example.com/mcp"),
        ("https://kb.example.com/mcp", "https://kb.example.com/mcp"),
        ("http://localhost:8123", "http://localhost:8123/mcp"),
        ("http://127.0.0.1:8123/mcp", "http://127.0.0.1:8123/mcp"),
        ("http://[::1]:8123", "http://[::1]:8123/mcp"),
    ],
)
def test_normalize_mcp_url_canonicalizes_only_origin_or_exact_endpoint(
    raw: str, expected: str
) -> None:
    assert client_config.normalize_mcp_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://kb.example.com",
        "https://user:pass@kb.example.com",
        "https://kb.example.com/other",
        "https://kb.example.com/mcp/",
        "https://kb.example.com/mcp?q=1",
        "https://kb.example.com/mcp#fragment",
        "https://kb.example.com:not-a-port",
        "https://kb.example.com:",
        "http://[::1]:",
        "  https://kb.example.com",
        "https://kb.example.com/\nmcp",
        "kb.example.com",
    ],
)
def test_normalize_mcp_url_rejects_unsafe_or_noncanonical_values(raw: str) -> None:
    with pytest.raises(ValueError, match="MCP URL"):
        client_config.normalize_mcp_url(raw)


def test_http_route_renders_native_client_forms_without_credentials() -> None:
    route = client_config.McpRoute.http("https://kb.example.com/mcp")

    assert route.claude_add_argv("claude", scope="user") == [
        "claude",
        "mcp",
        "add",
        "--transport",
        "http",
        "--scope",
        "user",
        "exomem",
        "https://kb.example.com/mcp",
    ]
    assert route.codex_add_argv("codex") == [
        "codex",
        "mcp",
        "add",
        "exomem",
        "--url",
        "https://kb.example.com/mcp",
    ]
    parsed = tomllib.loads(client_config.render_codex_block(route))
    assert parsed["mcp_servers"]["exomem"] == {
        "url": "https://kb.example.com/mcp"
    }


def test_stdio_route_uses_json_so_claude_preserves_child_flags() -> None:
    route = client_config.McpRoute.stdio(
        ["exomem", "--transport", "stdio"], {"EXOMEM_VAULT_PATH": WINDOWS_VAULT}
    )

    argv = route.claude_add_argv("claude", scope="project")
    assert argv[:6] == [
        "claude",
        "mcp",
        "add-json",
        "--scope",
        "project",
        "exomem",
    ]
    assert json.loads(argv[6]) == {
        "type": "stdio",
        "command": "exomem",
        "args": ["--transport", "stdio"],
        "env": {"EXOMEM_VAULT_PATH": WINDOWS_VAULT},
    }


def test_rendered_block_is_valid_toml_with_windows_paths() -> None:
    """Backslashes in a Windows vault path must be escaped, not emitted raw."""
    parsed = tomllib.loads(_stdio_block())

    server = parsed["mcp_servers"]["exomem"]
    assert server["command"] == "exomem"
    assert server["args"] == ["--transport", "stdio"]
    assert server["env"]["EXOMEM_VAULT_PATH"] == WINDOWS_VAULT


def test_rendered_block_pins_stdio_transport() -> None:
    """The server defaults to http; a config without this starts a web server."""
    assert '"--transport", "stdio"' in _stdio_block()


def test_creates_the_file_when_codex_has_no_config_yet(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"

    outcome = client_config.merge_codex_mcp(_stdio_block(), path=path)

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

    outcome = client_config.merge_codex_mcp(_stdio_block(), path=path)

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

    outcome = client_config.merge_codex_mcp(_stdio_block(), path=path)

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

    outcome = client_config.merge_codex_mcp(_stdio_block(), path=path, replace=True)

    assert outcome["action"] == "replaced"
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["mcp_servers"]["exomem"]["command"] == "exomem"
    assert parsed["mcp_servers"]["keeper"]["command"] == "keep-me"
    # The previous content survives on disk for recovery.
    assert "hand-tuned" in Path(outcome["backup"]).read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="Windows mode bits do not model POSIX 0600")
def test_codex_backup_preserves_restrictive_config_permissions(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mcp_servers.exomem]\ncommand = "hand-tuned"\nargs = []\n',
        encoding="utf-8",
    )
    path.chmod(0o600)

    outcome = client_config.merge_codex_mcp(_stdio_block(), path=path, replace=True)

    backup = Path(outcome["backup"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_diff_shows_the_user_what_changed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('model = "gpt-5"\n', encoding="utf-8")

    outcome = client_config.merge_codex_mcp(_stdio_block(), path=path)

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
