"""Write MCP server registration into agent clients' own config files.

The setup wizard has always shelled out to `claude mcp add` for Claude Code, but
every other client only ever got a printed snippet for the user to paste. That is
the difference between "installed" and "here are instructions", and it is why
Codex users ended up with no working registration.

These files belong to the user, so the rules here are deliberately conservative:
merge rather than overwrite, back up before touching anything, show exactly what
changed, and re-parse afterwards to prove we did not corrupt the file.
"""

from __future__ import annotations

import difflib
import os
import tomllib
from pathlib import Path

SERVER_NAME = "exomem"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def codex_config_path() -> Path:
    return codex_home() / "config.toml"


def _toml_string(value: str) -> str:
    """Quote a TOML basic string. Windows paths are full of backslashes, so
    escaping them (rather than emitting a literal string) is the safe choice."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_codex_block(command: str, args: list[str], env: dict[str, str]) -> str:
    """Render the `[mcp_servers.exomem]` section Codex expects."""
    lines = [f"[mcp_servers.{SERVER_NAME}]", f"command = {_toml_string(command)}"]
    rendered_args = ", ".join(_toml_string(a) for a in args)
    lines.append(f"args = [{rendered_args}]")
    if env:
        rendered_env = ", ".join(f"{k} = {_toml_string(v)}" for k, v in env.items())
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
        backup_path.write_text(original, encoding="utf-8", newline="")
        backup = str(backup_path)
    path.write_text(updated, encoding="utf-8", newline="")

    return {"action": action, "path": str(path), "diff": diff, "backup": backup}
