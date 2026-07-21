"""package-skills: build uploadable skill archives for clients with no filesystem.

Claude Code and Codex load skills from disk, so `exomem install-skill` just writes
them there. claude.ai and ChatGPT have no filesystem and no install API — a human
uploads an archive through a settings page. That upload is the only path those
clients offer, so the least we can do is generate exactly the right archives in
one command instead of hand-zipping one skill and forgetting the other nine.

Every archive is built from the same single source as every other channel,
`_scaffold/_Schema/`, so the uploaded skill can never drift from the installed one.

Layout: SKILL.md sits at the archive ROOT (not nested in a folder), which is the
layout the web uploaders expect.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import workflow_skills

_SKILL_SRC = Path(__file__).parent / "_scaffold" / "_Schema"
_HOOKS_SRC = Path(__file__).parent / "_hooks"

# Bundled alongside the core SKILL.md so the uploaded skill carries its own
# reference material; the web clients cannot reach the repo to resolve links.
_CORE_EXTRAS = ("references",)
_CORE_FILES = ("project-keys.yaml",)


def _core_payload(vault: Path | None) -> dict[str, str]:
    """Files for the core `exomem` skill archive, keyed by archive-relative path."""
    payload: dict[str, str] = {"SKILL.md": (_SKILL_SRC / "SKILL.md").read_text(encoding="utf-8")}

    for folder in _CORE_EXTRAS:
        for ref in sorted((_SKILL_SRC / folder).glob("*.md")):
            payload[f"{folder}/{ref.name}"] = ref.read_text(encoding="utf-8")

    for name in _CORE_FILES:
        # Overlay the real registry when a vault is given, so a personal upload
        # advertises real project scopes; otherwise ship the generic starter.
        override = (vault / "Knowledge Base" / "_Schema" / name) if vault else None
        source = override if (override and override.is_file()) else (_SKILL_SRC / name)
        if source.is_file():
            payload[name] = source.read_text(encoding="utf-8")

    return payload


def _workflow_payload(name: str) -> dict[str, str]:
    return {"SKILL.md": (workflow_skills.source_dir(name) / "SKILL.md").read_text(encoding="utf-8")}


def _write_zip(path: Path, payload: dict[str, str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname, content in payload.items():
            archive.writestr(arcname, content)
    return path.stat().st_size


# --- Claude Code plugin ------------------------------------------------------------
#
# A plugin is the only channel that installs MCP + skills + hooks in ONE user action:
# declared mcpServers auto-register and start when the plugin is enabled, so there is
# no separate `claude mcp add` step. Its skills/ and hooks/ trees must be committed
# (the marketplace installs straight from git), so they are GENERATED from the same
# package sources and guarded by a sync test rather than hand-maintained.

_PLUGIN_HOOK_EVENTS: tuple[tuple[str, str | None], ...] = (
    ("Stop", None),
    ("UserPromptSubmit", None),
    ("PreCompact", "manual|auto"),
    ("SessionEnd", None),
    ("SessionStart", "compact|resume"),
)
# Which wrapper serves which event. Mirrors install_hook._HOOK_SPECS and
# _CONTINUATION_EVENTS so the plugin and the CLI installer stay behaviourally identical.
_PLUGIN_HOOK_SCRIPTS: dict[str, str] = {
    "Stop": "exomem-capture-nudge.sh",
    "UserPromptSubmit": "exomem-retrieve-nudge.sh",
    "PreCompact": "exomem-continuation-checkpoint.sh",
    "SessionEnd": "exomem-continuation-checkpoint.sh",
    "SessionStart": "exomem-continuation-checkpoint.sh",
}


def _write_generated(path: Path, payload: dict) -> None:
    """Write generated JSON with LF endings on every platform."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")


def _package_version() -> str:
    try:
        return version("exomem")
    except PackageNotFoundError:
        return "0+unknown"


def _plugin_manifest() -> dict:
    return {
        "name": "exomem",
        "description": (
            "Governed long-term memory over a markdown vault: raw sources stay "
            "immutable, conclusions are compiled with provenance, and nothing is "
            "deleted - it is superseded. Requires EXOMEM_VAULT_PATH to point at "
            "your vault; run `exomem setup` if you do not have one yet."
        ),
        "version": _package_version(),
        # No "hooks" key: hooks/hooks.json is loaded automatically by convention,
        # and declaring it as well fails the install with "Duplicate hooks file
        # detected". The manifest key is only for ADDITIONAL hook files.
        # skills/ is likewise auto-discovered; declared here because it is the
        # documented form and does not collide.
        "skills": "./skills/",
        "mcpServers": {
            "exomem": {
                # `uvx` fetches and runs the published package, so the plugin works
                # on a machine that has never installed exomem.
                "command": "uvx",
                # --transport stdio is REQUIRED and easy to miss: the server defaults
                # to http, so omitting it starts a web server instead of speaking MCP.
                "args": ["exomem", "--transport", "stdio"],
                "env": {"EXOMEM_VAULT_PATH": "${EXOMEM_VAULT_PATH}"},
            }
        },
    }


def _plugin_hooks() -> dict:
    """Render hooks.json.

    The event map MUST sit under a top-level "hooks" key. Emitting the events at
    the document root parses as valid JSON but the plugin then fails to load with
    `expected record, received undefined` at path ["hooks"] -- caught only by
    actually installing the plugin, not by any schema we control.

    The script path is quoted because ${CLAUDE_PLUGIN_ROOT} expands to a real
    install path, which on Windows and macOS routinely contains spaces.
    """
    events: dict[str, list] = {}
    for event, matcher in _PLUGIN_HOOK_EVENTS:
        entry: dict = {}
        if matcher is not None:
            entry["matcher"] = matcher
        entry["hooks"] = [
            {
                "type": "command",
                "command": (
                    f'bash "${{CLAUDE_PLUGIN_ROOT}}/hooks/{_PLUGIN_HOOK_SCRIPTS[event]}"'
                ),
            }
        ]
        events.setdefault(event, []).append(entry)
    return {
        "description": "Exomem capture, retrieval, and continuation hooks.",
        "hooks": events,
    }


def sync_plugin(plugin_root: Path) -> dict:
    """Regenerate the Claude Code plugin tree from the packaged sources.

    Writes ``skills/`` (all ten), ``hooks/`` (wrappers + generated hooks.json) and
    ``.claude-plugin/plugin.json``. Destructive by design for those subtrees: the
    committed plugin must be a faithful mirror, never a half-merge.
    """
    plugin_root = Path(plugin_root).expanduser()
    skills_dir = plugin_root / "skills"
    hooks_dir = plugin_root / "hooks"
    manifest_dir = plugin_root / ".claude-plugin"

    for stale in (skills_dir, hooks_dir):
        if stale.exists():
            shutil.rmtree(stale)

    # Core skill: drop workflow-skills/, which ship as siblings below. Leaving them
    # nested would expose every SKILL.md at two paths at once.
    shutil.copytree(
        _SKILL_SRC,
        skills_dir / "exomem",
        ignore=shutil.ignore_patterns("workflow-skills", "__pycache__", "relation-reviews"),
    )
    names = ["exomem"]
    for skill in workflow_skills.list_skills():
        name = str(skill["name"])
        shutil.copytree(workflow_skills.source_dir(name), skills_dir / name)
        names.append(name)

    hooks_dir.mkdir(parents=True, exist_ok=True)
    for script in sorted(_HOOKS_SRC.iterdir()):
        if script.is_file() and script.suffix in (".sh", ".py"):
            shutil.copyfile(script, hooks_dir / script.name)
    # newline="\n" pins the output byte-for-byte across platforms. Without it
    # write_text emits CRLF on Windows and LF elsewhere, so the committed manifest
    # and a regenerated one would differ purely by host -- and the sync test that
    # guards this tree would fail depending on who ran it.
    _write_generated(hooks_dir / "hooks.json", _plugin_hooks())

    manifest_dir.mkdir(parents=True, exist_ok=True)
    _write_generated(manifest_dir / "plugin.json", _plugin_manifest())

    return {"plugin_root": str(plugin_root), "skills": names, "version": _package_version()}


def package_skills(out_dir: Path | None = None, *, vault: Path | None = None) -> dict:
    """Write one archive per skill into ``out_dir``.

    Args:
        out_dir: Destination directory (default: ``<cwd>/dist/skills``).
        vault: Vault root whose real ``project-keys.yaml`` should be overlaid into
            the core skill. Omit for the generic, shareable archive.

    Returns:
        {"out_dir": str, "archives": [{"name", "path", "bytes"}], "count": int}.

    Raises:
        FileNotFoundError: the bundled scaffold is missing (broken install).
    """
    if not (_SKILL_SRC / "SKILL.md").is_file():
        raise FileNotFoundError(
            f"bundled skill missing at {_SKILL_SRC} (SKILL.md not found) — "
            "is the exomem install intact?"
        )

    out_dir = Path(out_dir).expanduser() if out_dir is not None else Path.cwd() / "dist" / "skills"
    vault = Path(vault).expanduser() if vault is not None else None

    builds: list[tuple[str, dict[str, str]]] = [("exomem", _core_payload(vault))]
    for skill in workflow_skills.list_skills():
        name = str(skill["name"])
        builds.append((name, _workflow_payload(name)))

    archives = []
    for name, payload in builds:
        target = out_dir / f"{name}.zip"
        size = _write_zip(target, payload)
        archives.append({"name": name, "path": str(target), "bytes": size})

    return {"out_dir": str(out_dir), "archives": archives, "count": len(archives)}
