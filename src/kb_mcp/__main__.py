"""`python -m kb_mcp` entry point.

Subcommands:
- (default) serve the MCP server — `python -m kb_mcp [--transport ...]`
- `init` — bootstrap a fresh Knowledge Base into a vault
- `install-skill` — install the knowledge-base skill into Claude Code
- `install-hook` — wire the capture-reliability Stop hook into Claude Code
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import server


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "init":
        return _init_main(raw[1:])
    if raw and raw[0] == "install-skill":
        return _install_skill_main(raw[1:])
    if raw and raw[0] == "install-hook":
        return _install_hook_main(raw[1:])
    return _serve_main(raw)


def _serve_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kb-mcp")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "streamable-http"),
        default="http",
        help="MCP transport to serve (default: http). stdio for local Claude Code use.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for HTTP transports (default: 127.0.0.1; fronted by Tailscale Funnel).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Bind port for HTTP transports (default: 8765).",
    )
    args = parser.parse_args(argv)

    try:
        server.run(transport=args.transport, host=args.host, port=args.port)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"kb-mcp failed: {e}", file=sys.stderr)
        return 1
    return 0


def _init_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="kb-mcp init",
        description="Bootstrap a fresh Knowledge Base scaffold into a vault.",
    )
    parser.add_argument(
        "--vault",
        help="Vault root to scaffold (default: $KB_MCP_VAULT_PATH, else current dir).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overlay the scaffold even if Knowledge Base/ exists (existing files kept).",
    )
    args = parser.parse_args(argv)

    from . import init as init_module

    vault = args.vault or os.environ.get("KB_MCP_VAULT_PATH") or "."
    try:
        report = init_module.init_vault(Path(vault), force=args.force)
    except FileExistsError as e:
        print(f"kb-mcp init: {e}", file=sys.stderr)
        return 1
    print(f"Initialized Knowledge Base at {report['kb']}")
    print(f"  {len(report['created'])} files created + the typed folder tree.")
    print("Next:")
    print("  1. Point Claude Code at this vault (see SETUP-FRIEND.md).")
    print("  2. Install the skill so Claude knows how to use it: python -m kb_mcp install-skill")
    print("  3. Adapt Knowledge Base/_Schema/project-keys.yaml to your own projects.")
    return 0


def _install_skill_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="kb-mcp install-skill",
        description=(
            "Install the knowledge-base skill into Claude Code's skills folder. "
            "The MCP server is the hands; the skill is the brain that tells Claude "
            "when to capture and how to file — without it, the tools sit unused."
        ),
    )
    parser.add_argument(
        "--target",
        help="Skill folder to install into (default: ~/.claude/skills/knowledge-base).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing install at the target.",
    )
    parser.add_argument(
        "--link",
        action="store_true",
        help="Symlink instead of copy, so the install tracks repo updates "
        "(falls back to copy if the OS refuses the symlink).",
    )
    args = parser.parse_args(argv)

    from . import install_skill as install_module

    target = Path(args.target) if args.target else None
    try:
        report = install_module.install_skill(target, force=args.force, link=args.link)
    except (FileExistsError, FileNotFoundError) as e:
        print(f"kb-mcp install-skill: {e}", file=sys.stderr)
        return 1
    print(
        f"Installed the knowledge-base skill ({report['mode']}, "
        f"{report['files']} files):"
    )
    print(f"  {report['target']}")
    print("Restart Claude Code to load it. Then just talk - it captures at")
    print('natural stopping points, or say "find my notes on X".')
    return 0


def _install_hook_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="kb-mcp install-hook",
        description=(
            "Wire the capture-reliability Stop hook into Claude Code so auto-save "
            "fires on its own at stepping-stones instead of waiting to be told. "
            "Language-agnostic (no English-keyword gating) and cheap (gated + "
            "cooldown). Re-running is idempotent."
        ),
    )
    parser.add_argument(
        "--hook-dir",
        help="Where to write the hook script (default: ~/.claude/hooks).",
    )
    parser.add_argument(
        "--settings",
        help="settings.json to wire (default: ~/.claude/settings.json).",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Write the script but don't touch settings.json; print the snippet to add.",
    )
    args = parser.parse_args(argv)

    from . import install_hook as hook_module

    try:
        report = hook_module.install_hook(
            hook_dir=args.hook_dir,
            settings_path=args.settings,
            wire=not args.print_only,
        )
    except FileNotFoundError as e:
        print(f"kb-mcp install-hook: {e}", file=sys.stderr)
        return 1

    print("Installed the capture-reliability hook script:")
    print(f"  {report['script']}")
    if report["wired"]:
        print(f"Wired into {report['settings']} as a Stop hook.")
        print("Restart Claude Code to activate it. Triggers log to")
        print("  ~/.claude/kb-capture-nudge.log")
    else:
        print("Add this to your settings.json (merge into hooks.Stop):")
        print(hook_module.snippet(report["command"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
