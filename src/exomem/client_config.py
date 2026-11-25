"""Write Evermatter's entries into agent clients' own files.

Two entries live here: the MCP server registration, and the learning instruction
in each client's user-level instruction file. The setup wizard has always shelled
out to Claude's MCP registration CLI, but every other client only ever got a
printed snippet for the user to paste. That is the difference between "installed"
and "here are instructions", and it is why Codex users ended up with no working
registration.

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
# Every server key a reader accepts: a client stores the key it was given, so an old
# install keeps `exomem` while a new one may hold `evermatter`. Writers emit SERVER_NAME only.
SERVER_NAMES = ("exomem", "evermatter")


def normalize_mcp_url(raw: str) -> str:
    """Return one safe, canonical Evermatter MCP endpoint.

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

    def claude_add_argv(
        self, executable: str, *, scope: str, name: str = SERVER_NAME
    ) -> list[str]:
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
                name,
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
            return [*argv, "--scope", scope, name, self.url]
        raise AssertionError(f"unsupported Claude MCP transport: {self.transport}")

    def codex_add_argv(self, executable: str, *, name: str = SERVER_NAME) -> list[str]:
        argv = [executable, "mcp", "add", name]
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


def codex_mcp_name(path: Path) -> str | None:
    """The accepted server key that Codex's config registers, or None."""
    if not path.is_file():
        return None
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot inspect Codex MCP registration in {path}: {exc}") from exc
    servers = document.get("mcp_servers")
    if not isinstance(servers, dict):
        return None
    return next((name for name in SERVER_NAMES if name in servers), None)


def _toml_string(value: str) -> str:
    """Quote a TOML basic string. Windows paths are full of backslashes, so
    escaping them (rather than emitting a literal string) is the safe choice."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_codex_block(route: McpRoute, *, name: str = SERVER_NAME) -> str:
    """Render the `[mcp_servers.<name>]` section Codex expects."""
    lines = [f"[mcp_servers.{name}]"]
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
    """Return (start, end) line indices of a TOML section, or None if absent.

    The section runs through its own sub-tables (`[mcp_servers.x.env]`), so a
    replacement leaves none behind to clash with the new block.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break
    if start is None:
        return None
    subtable_prefixes = (header[:-1] + ".", "[" + header[:-1] + ".")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped.startswith(subtable_prefixes):
                continue
            end = j
            break
    return start, end


def merge_codex_mcp(block: str, *, path: Path | None = None, replace: bool = False) -> dict:
    """Merge the exomem MCP block into Codex's config.toml.

    An existing registration under either accepted server key counts as the
    registration: ``replace=True`` swaps its section, sub-tables included, for
    ``block`` instead of adding a second server. The caller renders ``block`` under
    the key it means to keep.

    Returns {"action": "created"|"added"|"replaced"|"exists", "path": str,
    "diff": str, "backup": str|None}.

    ``replace=False`` leaves an existing registration untouched and reports
    "exists", so the caller can ask before changing something the user set up.

    Raises:
        ValueError: the merge would produce invalid TOML (nothing is written).
    """
    path = Path(path) if path is not None else codex_config_path()

    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    bounds = next(
        (
            found
            for name in SERVER_NAMES
            if (found := _section_bounds(original, f"[mcp_servers.{name}]")) is not None
        ),
        None,
    )

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

    diff, backup = _write_user_file(path, original, updated)
    return {"action": action, "path": str(path), "diff": diff, "backup": backup}


def _write_user_file(path: Path, original: str, updated: str) -> tuple[str, str | None]:
    """Back up, diff and write one user-owned file. Returns (diff, backup path)."""
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
    return diff, backup


# The learning instruction sits between these two lines, so a re-run replaces only
# that span and uninstall removes only it. They are protocol tokens Evermatter owns.
LEARNING_BEGIN = "<!-- BEGIN exomem learning instruction (managed by exomem setup) -->"
LEARNING_END = "<!-- END exomem learning instruction -->"
EVERMATTER_LEARNING_BEGIN = (
    "<!-- BEGIN evermatter learning instruction (managed by evermatter setup) -->"
)
EVERMATTER_LEARNING_END = "<!-- END evermatter learning instruction -->"
# Every marker pair a reader accepts, for the same reason as SERVER_NAMES: a file keeps the
# pair it was written with. Writers emit LEARNING_BEGIN and LEARNING_END only.
LEARNING_PAIRS = (
    (LEARNING_BEGIN, LEARNING_END),
    (EVERMATTER_LEARNING_BEGIN, EVERMATTER_LEARNING_END),
)


def instruction_file(client: str, home: Path) -> Path:
    """The user-level instruction file a client reads from its config home.

    The names are each client's documented file, a closed set the clients fix.
    """
    return Path(home) / ("AGENTS.md" if client == "codex" else "CLAUDE.md")


def _read_user_text(path: Path) -> str | None:
    """The file's exact text, line endings included, or None when it is absent."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"cannot read {path} as UTF-8 text ({exc}); nothing was changed") from exc


def _learning_span(lines: list[str], path: Path) -> tuple[int, int] | None:
    """Line indexes of the BEGIN and END markers, or None when neither is present."""
    begin_markers = {begin for begin, _ in LEARNING_PAIRS}
    end_markers = {end for _, end in LEARNING_PAIRS}
    begins = [(i, line.strip()) for i, line in enumerate(lines) if line.strip() in begin_markers]
    ends = [(i, line.strip()) for i, line in enumerate(lines) if line.strip() in end_markers]
    if not begins and not ends:
        return None
    if (
        len(begins) == 1
        and len(ends) == 1
        and begins[0][0] < ends[0][0]
        and (begins[0][1], ends[0][1]) in LEARNING_PAIRS
    ):
        return begins[0][0], ends[0][0]
    # Guessing which marker is stray could delete the user's own lines, so refuse.
    raise ValueError(
        f"{path} does not hold exactly one Evermatter learning-instruction span. "
        f"Keep one {LEARNING_BEGIN!r} line above one {LEARNING_END!r} line "
        f"(or the {EVERMATTER_LEARNING_BEGIN!r} and {EVERMATTER_LEARNING_END!r} pair), "
        "or delete every marker line, then run the command again."
    )


def _plan_learning_instruction(path: Path, text: str) -> tuple[str, str, str]:
    """(action, original, updated) for placing ``text``; action is "current" when nothing changes.

    A file without the span gets it appended after one blank line; a file with the
    span gets only the span replaced. A CRLF file keeps CRLF.
    """
    existing = _read_user_text(path)
    original = existing or ""
    newline = "\r\n" if "\r\n" in original else "\n"
    span = newline.join([LEARNING_BEGIN, text, LEARNING_END]) + newline
    lines = original.splitlines(keepends=True)
    bounds = _learning_span(lines, path)
    if bounds is None:
        if original:
            body = original if original.endswith("\n") else original + newline
            updated, action = body + newline + span, "added"
        else:
            updated, action = span, "created" if existing is None else "added"
    else:
        start, end = bounds
        updated = "".join(lines[:start]) + span + "".join(lines[end + 1 :])
        action = "updated"
    return ("current" if updated == original else action), original, updated


def learning_instruction_change(path: Path, text: str) -> str:
    """What `place_learning_instruction` would do: "created", "added", "updated" or "current".

    Writes nothing. Raises ValueError when the file is not UTF-8 or its markers are unmatched.
    """
    return _plan_learning_instruction(Path(path), text)[0]


def place_learning_instruction(path: Path, text: str) -> dict:
    """Put ``text`` between the learning markers in ``path``, changing nothing outside them.

    Returns {"action": "created"|"added"|"updated"|"current", "path", "diff", "backup"};
    "current" writes nothing.

    Raises:
        ValueError: the file is not UTF-8 or its markers are unmatched (nothing is written).
    """
    path = Path(path)
    action, original, updated = _plan_learning_instruction(path, text)
    if action == "current":
        return {"action": action, "path": str(path), "diff": "", "backup": None}
    diff, backup = _write_user_file(path, original, updated)
    return {"action": action, "path": str(path), "diff": diff, "backup": backup}


def remove_learning_instruction(path: Path) -> dict:
    """Remove the learning span from ``path`` and nothing else.

    Also drops the one blank line `place_learning_instruction` put before a span at
    the end of the file. A file left empty held only the span, so it is deleted.
    Returns {"action": "removed"|"absent", "path", "diff", "backup"}.

    Raises:
        ValueError: the file is not UTF-8 or its markers are unmatched (nothing is written).
    """
    path = Path(path)
    absent = {"action": "absent", "path": str(path), "diff": "", "backup": None}
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return absent
    # Placing refuses a file it cannot decode, so a file without either marker holds
    # nothing of Evermatter's: report it absent rather than fail an unrelated uninstall.
    if not any(marker.encode() in raw for pair in LEARNING_PAIRS for marker in pair):
        return absent
    original = _read_user_text(path)
    lines = original.splitlines(keepends=True)
    bounds = _learning_span(lines, path)
    if bounds is None:
        return absent
    start, end = bounds
    before, after = lines[:start], lines[end + 1 :]
    if not after and before and not before[-1].strip():
        before = before[:-1]
    updated = "".join(before + after)
    # Unlinking a symlink would leave the span in its target, so empty the target instead.
    if not updated and not path.is_symlink():
        path.unlink()
        return {"action": "removed", "path": str(path), "diff": "", "backup": None}
    diff, backup = _write_user_file(path, original, updated)
    return {"action": "removed", "path": str(path), "diff": diff, "backup": backup}
