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

from . import cloud_address

SERVER_NAME = "evermatter"
#: The server key an install made before the rename holds.
LEGACY_SERVER_NAME = "exomem"
# Every server key a reader accepts: a client stores the key it was given, so an old
# install keeps `exomem` while a new one holds `evermatter`. Writers emit SERVER_NAME only.
SERVER_NAMES = (SERVER_NAME, LEGACY_SERVER_NAME)
#: The tool-name prefix that each server key gives. A client builds a user-scope server's tools as
#: `mcp__<server>__<tool>`, and a plugin's as `mcp__plugin_<plugin>_<server>__<tool>`; users' rules
#: and hooks match those names. The plugin and its server shared the key `exomem` and now share
#: `evermatter`, so one name fills both slots.
LEGACY_TOOL_PREFIX = f"mcp__{LEGACY_SERVER_NAME}__"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
LEGACY_PLUGIN_TOOL_PREFIX = f"mcp__plugin_{LEGACY_SERVER_NAME}_{LEGACY_SERVER_NAME}__"
PLUGIN_TOOL_PREFIX = f"mcp__plugin_{SERVER_NAME}_{SERVER_NAME}__"
#: Each old tool-name prefix with the prefix that replaces it.
TOOL_PREFIX_RENAMES = {
    LEGACY_TOOL_PREFIX: TOOL_PREFIX,
    LEGACY_PLUGIN_TOOL_PREFIX: PLUGIN_TOOL_PREFIX,
}
#: What a user does after a client renames a URL server, which signs the user out once: Claude
#: Code and Codex both store the sign-in under the server key. `evermatter setup --migrate` prints
#: the step for each URL registration it renames. The plugin's notice hook holds the Claude Code
#: step too; it cannot import this module, and a test fails when the two differ.
SIGN_IN_STEPS = {
    "claude": f"run /mcp in Claude Code and choose {SERVER_NAME}",
    "codex": f"run codex mcp login {SERVER_NAME}",
}
#: The suffix that `_write_user_file` and `evermatter setup --migrate` give a file's backup copy.
BACKUP_SUFFIX = ".exomem-bak"


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


def default_claude_config_path(environ: dict[str, str]) -> Path:
    """Return Claude's user-state path, including configured relocation."""
    config_dir = environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser() / ".claude.json"
    return Path.home() / ".claude.json"


def read_json_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot inspect Claude MCP registrations in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"cannot inspect Claude MCP registrations in {path}: expected an object")
    return value


def project_entry(config: dict, project_dir: Path) -> dict:
    target = project_dir.resolve()
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return {}
    for raw_path, value in projects.items():
        if not isinstance(raw_path, str) or not isinstance(value, dict):
            continue
        try:
            if Path(raw_path).expanduser().resolve() == target:
                return value
        except OSError:
            continue
    return {}


def server_entry(servers: object) -> tuple[str, dict] | None:
    """The (key, entry) that an `mcpServers` map holds under an accepted server key, or None."""
    if not isinstance(servers, dict):
        return None
    for name in SERVER_NAMES:
        value = servers.get(name)
        if isinstance(value, dict):
            return name, value
    return None


def claude_registrations(*, config_path: Path, project_dir: Path) -> dict[str, tuple[str, dict]]:
    """Inventory registrations as scope -> (key, entry), without `claude mcp list/get` checks."""
    user_config = read_json_object(config_path)
    project_config = read_json_object(project_dir / ".mcp.json")
    found: dict[str, tuple[str, dict]] = {}
    candidates = (
        ("local", server_entry(project_entry(user_config, project_dir).get("mcpServers"))),
        ("project", server_entry(project_config.get("mcpServers"))),
        ("user", server_entry(user_config.get("mcpServers"))),
    )
    for scope, server in candidates:
        if server is not None:
            found[scope] = server
    return found


def claude_settings_files(*, claude_home: Path, project_dir: Path) -> tuple[Path, ...]:
    """The settings files Claude Code documents, from the user's down to the project's local one."""
    return (
        claude_home / "settings.json",
        claude_home / "settings.local.json",
        project_dir / ".claude" / "settings.json",
        project_dir / ".claude" / "settings.local.json",
    )


def legacy_plugin_enabled(*, claude_home: Path, project_dir: Path) -> bool:
    """Whether a settings file enables an `exomem` plugin, the one serving `mcp__plugin_exomem_*`.

    Reads `enabledPlugins`, not the install record: a marketplace rename moves the enabled entry
    to the new name while the old install can linger, and a plugin that is not enabled has no
    tools. A file it cannot read or parse is skipped, as `user_references` skips it.
    """
    for path in claude_settings_files(claude_home=claude_home, project_dir=project_dir):
        try:
            enabled = json.loads(path.read_text(encoding="utf-8")).get("enabledPlugins")
        except (OSError, UnicodeError, ValueError, AttributeError):
            continue
        if isinstance(enabled, dict) and any(
            value is True and str(plugin_id).partition("@")[0] == LEGACY_SERVER_NAME
            for plugin_id, value in enabled.items()
        ):
            return True
    return False


def dead_references(
    *, claude_home: Path, codex_home: Path, claude_config_path: Path, project_dir: Path
) -> tuple[list[UserReference], list[Path]]:
    """The user's rules and hooks that name an old prefix in a client with nothing behind it.

    `mcp__exomem__` is dead in a client with no `exomem` server, and
    `mcp__plugin_exomem_exomem__` in a Claude Code with no `exomem` plugin enabled. A rule with
    its server behind it still works, so it is not listed. Returns the dead rules and every file
    read. Raises ``ValueError`` when a client's registrations cannot be read, because then no
    rule can be judged. A Claude Code scope that holds both keys reports only `evermatter`, so a
    rule there counts as dead.
    """
    references, read = user_references(
        claude_home=claude_home, codex_home=codex_home, project_dir=project_dir
    )
    server_alive = {
        "claude": any(
            key == LEGACY_SERVER_NAME
            for key, _ in claude_registrations(
                config_path=claude_config_path, project_dir=project_dir
            ).values()
        ),
        "codex": codex_mcp_name(codex_home / "config.toml") == LEGACY_SERVER_NAME,
    }
    plugin_alive = legacy_plugin_enabled(claude_home=claude_home, project_dir=project_dir)
    def answered(ref: UserReference) -> bool:
        return plugin_alive if ref.prefix == LEGACY_PLUGIN_TOOL_PREFIX else server_alive[ref.client]

    return [ref for ref in references if not answered(ref)], read


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
    """Merge the Evermatter MCP block into Codex's config.toml.

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


def rename_codex_mcp_server(*, path: Path | None = None) -> dict:
    """Rename Codex's legacy `exomem` server section, sub-tables included, to ``SERVER_NAME``.

    Only the section headers change, so the server's settings and the tool approvals
    under its sub-tables move with it. Returns {"action", "path", "diff", "backup"} where
    ``action`` is "renamed", "current" (no legacy key, nothing written), "conflict" (both keys
    hold a section, nothing written), "kept" (the legacy server is a URL on an Evermatter Cloud
    host, which moves with the Cloud plugin; nothing written, ``url`` holds it) or "manual" (the legacy key is not a plain
    section the rename can move, such as an inline table, nothing written). A renamed URL
    server carries its ``url`` in the outcome: Codex stores the sign-in under the server key,
    so the user signs in again.

    Raises:
        ValueError: the config is not valid TOML, or the rename would make it invalid
            (nothing is written).
    """
    path = Path(path) if path is not None else codex_config_path()
    outcome = {"path": str(path), "diff": "", "backup": None}
    if not path.is_file():
        return {"action": "current", **outcome}
    try:
        original = path.read_text(encoding="utf-8")
        servers = tomllib.loads(original).get("mcp_servers")
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot inspect Codex MCP registration in {path}: {exc}") from exc
    if not isinstance(servers, dict) or LEGACY_SERVER_NAME not in servers:
        return {"action": "current", **outcome}
    if SERVER_NAME in servers:
        return {"action": "conflict", **outcome}
    registered = servers[LEGACY_SERVER_NAME]
    url = str(registered["url"]) if isinstance(registered, dict) and "url" in registered else None
    if url is not None and cloud_address.is_cloud_url(url):
        return {"action": "kept", "url": url, **outcome}
    legacy = _section_bounds(original, f"[mcp_servers.{LEGACY_SERVER_NAME}]")
    if legacy is None:
        return {"action": "manual", **outcome}
    start, end = legacy
    old, new = f"mcp_servers.{LEGACY_SERVER_NAME}", f"mcp_servers.{SERVER_NAME}"
    # Line-based, because Python has no format-preserving TOML writer and a parse-and-dump would
    # drop the user's comments and ordering. tomllib re-validates the result before it is written.
    lines = original.splitlines(keepends=True)
    for index in range(start, end):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            lines[index] = lines[index].replace(old, new, 1)
    updated = "".join(lines)
    try:
        renamed = tomllib.loads(updated)["mcp_servers"]
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"refusing to write invalid TOML to {path}: {e}") from e
    if SERVER_NAME not in renamed or LEGACY_SERVER_NAME in renamed:
        return {"action": "manual", **outcome}  # a header the line scan misread
    diff, backup = _write_user_file(path, original, updated)
    return {"action": "renamed", "path": str(path), "diff": diff, "backup": backup, "url": url}


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
        backup_path = path.with_suffix(path.suffix + BACKUP_SUFFIX)
        shutil.copy2(path, backup_path)
        backup = str(backup_path)
    path.write_text(updated, encoding="utf-8", newline="")
    return diff, backup


# The learning instruction sits between these two lines, so a re-run replaces only
# that span and uninstall removes only it. They are protocol tokens Evermatter owns.
LEARNING_BEGIN = "<!-- BEGIN evermatter learning instruction (managed by evermatter setup) -->"
LEARNING_END = "<!-- END evermatter learning instruction -->"
LEGACY_LEARNING_BEGIN = "<!-- BEGIN exomem learning instruction (managed by exomem setup) -->"
LEGACY_LEARNING_END = "<!-- END exomem learning instruction -->"
# Every marker pair a reader accepts, for the same reason as SERVER_NAMES: a file keeps the
# pair it was written with. Writers emit LEARNING_BEGIN and LEARNING_END only.
LEARNING_PAIRS = (
    (LEARNING_BEGIN, LEARNING_END),
    (LEGACY_LEARNING_BEGIN, LEGACY_LEARNING_END),
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
        f"(or the {LEGACY_LEARNING_BEGIN!r} and {LEGACY_LEARNING_END!r} pair), "
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


def migrate_learning_instruction(path: Path, text: str) -> dict:
    """Move a managed span written under the legacy marker pair to the current pair.

    The span's text is replaced with ``text``, as a re-run of setup does. A file without
    a span is left alone, and so is a span that already carries the current pair.
    Returns {"action": "migrated"|"current"|"absent", "path", "diff", "backup"}.

    Raises:
        ValueError: the file is not UTF-8 or its markers are unmatched (nothing is written).
    """
    path = Path(path)
    original = _read_user_text(path)
    bounds = _learning_span(original.splitlines(keepends=True), path) if original else None
    if bounds is None:
        return {"action": "absent", "path": str(path), "diff": "", "backup": None}
    if original.splitlines()[bounds[0]].strip() == LEARNING_BEGIN:
        return {"action": "current", "path": str(path), "diff": "", "backup": None}
    outcome = place_learning_instruction(path, text)
    return {**outcome, "action": "migrated"}


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


@dataclass(frozen=True)
class UserReference:
    """A rule or hook the user wrote that still names an old tool prefix."""

    client: Literal["claude", "codex"]
    path: Path
    kind: Literal["permission rule", "hook", "setting"]
    name: str
    prefix: str  # the old prefix it names: LEGACY_TOOL_PREFIX or LEGACY_PLUGIN_TOOL_PREFIX

    @property
    def replacement(self) -> str:
        """The prefix the rule should name now."""
        return TOOL_PREFIX_RENAMES[self.prefix]


def _strings(node: object, trail: tuple[str, ...] = ()):
    """Every string value in a parsed settings document, with the keys above it."""
    if isinstance(node, str):
        yield trail, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, (*trail, str(key)))
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value, trail)


def user_references(
    *, claude_home: Path, codex_home: Path, project_dir: Path
) -> tuple[list[UserReference], list[Path]]:
    """List the user's permission rules and hooks that still name an old tool prefix.

    The old prefixes are `mcp__exomem__` and, in Claude Code only, the plugin's
    `mcp__plugin_exomem_exomem__`. Setup writes no rule or hook that names a tool, so every
    hit is the user's own. It reads the settings files each client documents, and a file it
    cannot read or parse is skipped: the client reports that file itself. Nothing is written.
    Returns the references and the files it read, so a caller can say what it checked.
    """
    files = (
        *(
            ("claude", path)
            for path in claude_settings_files(claude_home=claude_home, project_dir=project_dir)
        ),
        ("codex", codex_home / "config.toml"),
        ("codex", codex_home / "hooks.json"),
    )
    found: list[UserReference] = []
    read: list[Path] = []
    for client, path in files:
        try:
            raw = path.read_text(encoding="utf-8")
            document = tomllib.loads(raw) if path.suffix == ".toml" else json.loads(raw)
        except (OSError, UnicodeError, ValueError):  # tomllib and json errors are ValueErrors
            continue
        read.append(path)
        # The tokens are the client-defined tool-name prefixes, a closed set (protocol tokens,
        # not a reading of what the rule meant). Codex has no plugin prefix of this shape.
        prefixes = (LEGACY_TOOL_PREFIX,)
        if client == "claude":
            prefixes += (LEGACY_PLUGIN_TOOL_PREFIX,)
        for trail, text in _strings(document):
            named = next(
                (
                    prefix
                    for prefix in prefixes
                    if prefix in text or text.strip() == prefix.removesuffix("__")
                ),
                None,
            )
            if named is None:
                continue
            root = trail[0] if trail else ""
            kind = (
                "permission rule" if root == "permissions" else "hook" if root == "hooks" else "setting"
            )
            found.append(
                UserReference(
                    client=client, path=path, kind=kind, name=text.strip()[:200], prefix=named
                )
            )
    return found, read
