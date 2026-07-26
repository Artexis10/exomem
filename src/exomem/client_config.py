"""Write MCP server registration into agent clients' own config files.

The setup wizard has always shelled out to Claude's MCP registration CLI, but
every other client only ever got a printed snippet for the user to paste. That is
the difference between "installed" and "here are instructions", and it is why
Codex users ended up with no working registration.

These files belong to the user, so the rules here are deliberately conservative:
merge rather than overwrite, back up before touching anything, show exactly what
changed, and re-parse afterwards to prove we did not corrupt the file.
"""

from __future__ import annotations

import difflib
import ipaddress
import json
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

SERVER_NAME = "exomem"


def normalize_mcp_url(raw: str) -> str:
    """Return one safe, canonical Exomem MCP endpoint.

    Public clear-text HTTP is rejected because native clients send OAuth tokens
    to this endpoint. Plain HTTP remains useful for literal loopback addresses.
    """
    if (
        not isinstance(raw, str)
        or not raw
        or raw != raw.strip()
        or any(ord(character) <= 32 or ord(character) == 127 for character in raw)
    ):
        raise ValueError("MCP URL must be a non-empty URL without surrounding whitespace")
    try:
        parsed = urlsplit(raw)
        port = parsed.port  # Force validation of malformed ports.
    except ValueError as exc:
        raise ValueError(f"MCP URL is invalid: {exc}") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("MCP URL must use an http:// or https:// origin")
    if parsed.netloc.endswith(":"):
        raise ValueError("MCP URL contains an empty port")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("MCP URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("MCP URL must not contain a query string or fragment")
    if parsed.path not in {"", "/", "/mcp"}:
        raise ValueError("MCP URL must be a bare origin or the exact /mcp endpoint")

    host = parsed.hostname
    is_loopback = host.casefold() == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    if parsed.scheme == "http" and not is_loopback:
        raise ValueError("MCP URL must use HTTPS unless it points to loopback")

    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host if port is None else f"{rendered_host}:{port}"
    return urlunsplit((parsed.scheme, netloc, "/mcp", "", ""))


@dataclass(frozen=True)
class McpRoute:
    """One native MCP client route, either shared HTTP or full stdio."""

    transport: Literal["http", "stdio"]
    url: str | None = None
    command: tuple[str, ...] = ()
    env: dict[str, str] | None = None

    @classmethod
    def http(cls, url: str) -> McpRoute:
        return cls(transport="http", url=normalize_mcp_url(url), env={})

    @classmethod
    def stdio(cls, command: list[str], env: dict[str, str]) -> McpRoute:
        if not command or not command[0]:
            raise ValueError("stdio MCP route requires a command")
        return cls(transport="stdio", command=tuple(command), env=dict(env))

    def claude_add_argv(self, executable: str, *, scope: str) -> list[str]:
        if self.transport == "stdio":
            payload = json.dumps(
                self.as_claude_config(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            return [
                executable,
                "mcp",
                "add-json",
                "--scope",
                scope,
                SERVER_NAME,
                payload,
            ]
        argv = [
            executable,
            "mcp",
            "add",
            "--transport",
            self.transport,
        ]
        if self.transport == "http":
            assert self.url is not None
            return [*argv, "--scope", scope, SERVER_NAME, self.url]
        raise AssertionError(f"unsupported Claude MCP transport: {self.transport}")

    def codex_add_argv(self, executable: str) -> list[str]:
        argv = [executable, "mcp", "add", SERVER_NAME]
        if self.transport == "http":
            assert self.url is not None
            return [*argv, "--url", self.url]
        for key, value in (self.env or {}).items():
            argv.extend(["--env", f"{key}={value}"])
        return [*argv, "--", *self.command]

    def as_claude_config(self) -> dict[str, object]:
        if self.transport == "http":
            return {"type": "http", "url": self.url}
        return {
            "type": "stdio",
            "command": self.command[0],
            "args": list(self.command[1:]),
            "env": dict(self.env or {}),
        }


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def codex_config_path() -> Path:
    return codex_home() / "config.toml"


def codex_mcp_exists(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot inspect Codex MCP registration in {path}: {exc}") from exc
    servers = document.get("mcp_servers")
    return isinstance(servers, dict) and SERVER_NAME in servers


def _toml_string(value: str) -> str:
    """Quote a TOML basic string. Windows paths are full of backslashes, so
    escaping them (rather than emitting a literal string) is the safe choice."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_codex_block(route: McpRoute) -> str:
    """Render the `[mcp_servers.exomem]` section Codex expects."""
    lines = [f"[mcp_servers.{SERVER_NAME}]"]
    if route.transport == "http":
        assert route.url is not None
        lines.append(f"url = {_toml_string(route.url)}")
        return "\n".join(lines) + "\n"

    lines.append(f"command = {_toml_string(route.command[0])}")
    rendered_args = ", ".join(_toml_string(a) for a in route.command[1:])
    lines.append(f"args = [{rendered_args}]")
    if route.env:
        rendered_env = ", ".join(f"{k} = {_toml_string(v)}" for k, v in route.env.items())
        lines.append(f"env = {{ {rendered_env} }}")
    return "\n".join(lines) + "\n"


def _section_bounds(text: str, header: str) -> tuple[int, int] | None:
    """Return (start, end) line indices of a TOML section, or None if absent."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = j
            break
    return start, end


def merge_codex_mcp(block: str, *, path: Path | None = None, replace: bool = False) -> dict:
    """Merge the exomem MCP block into Codex's config.toml.

    Returns {"action": "created"|"added"|"replaced"|"exists", "path": str,
    "diff": str, "backup": str|None}.

    ``replace=False`` leaves an existing registration untouched and reports
    "exists", so the caller can ask before changing something the user set up.

    Raises:
        ValueError: the merge would produce invalid TOML (nothing is written).
    """
    path = Path(path) if path is not None else codex_config_path()
    header = f"[mcp_servers.{SERVER_NAME}]"

    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    bounds = _section_bounds(original, header)

    if bounds is not None and not replace:
        return {"action": "exists", "path": str(path), "diff": "", "backup": None}

    if bounds is None:
        separator = "" if (not original or original.endswith("\n\n")) else (
            "\n" if original.endswith("\n") else "\n\n"
        )
        updated = original + separator + block
        action = "created" if not original else "added"
    else:
        start, end = bounds
        lines = original.splitlines(keepends=True)
        updated = "".join(lines[:start]) + block + "".join(lines[end:])
        action = "replaced"

    # Prove the result parses BEFORE writing. A corrupted config.toml would break
    # every MCP server the user has, not just ours.
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"refusing to write invalid TOML to {path}: {e}") from e

    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )

    backup = None
    path.parent.mkdir(parents=True, exist_ok=True)
    if original:
        backup_path = path.with_suffix(path.suffix + ".exomem-bak")
        shutil.copy2(path, backup_path)
        backup = str(backup_path)
    path.write_text(updated, encoding="utf-8", newline="")

    return {"action": action, "path": str(path), "diff": diff, "backup": backup}
