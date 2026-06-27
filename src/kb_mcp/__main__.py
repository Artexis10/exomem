"""`python -m kb_mcp` entry point.

Subcommands:
- (default) serve the MCP server — `python -m kb_mcp [--transport ...]`
- `init` — bootstrap a fresh Knowledge Base into a vault
- `install-skill` — install the knowledge-base skill into Claude Code
- `install-hook` — wire the KB capture + retrieval hooks into Claude Code
- `backfill-media` — make pre-existing Evidence binaries searchable (sidecar + OCR/ASR/PDF + CLIP)
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
    if raw and raw[0] == "backfill-media":
        return _backfill_media_main(raw[1:])
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
        default=None,
        help="Bind address for HTTP transports (default: $KB_MCP_HOST, else 127.0.0.1; "
        "fronted by Cloudflare Tunnel). Set 0.0.0.0 to also serve a direct Tailscale/LAN route.",
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


def _backfill_media_main(argv: list[str]) -> int:
    import logging

    parser = argparse.ArgumentParser(
        prog="kb-mcp backfill-media",
        description="Make pre-existing Evidence binaries searchable: write a sidecar if "
        "missing, extract text (OCR/ASR/PDF), and CLIP-embed images. Idempotent; CPU or GPU.",
    )
    parser.add_argument(
        "--vault", default=os.environ.get("KB_MCP_VAULT_PATH"),
        help="vault root containing 'Knowledge Base/' (default: $KB_MCP_VAULT_PATH)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    parser.add_argument("--no-ocr", action="store_true", help="skip text extraction (sidecar + CLIP only)")
    parser.add_argument("--no-clip", action="store_true", help="skip CLIP image embedding")
    args = parser.parse_args(argv)
    if not args.vault:
        print("backfill-media: set --vault or KB_MCP_VAULT_PATH", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from . import backfill

    backfill.backfill_media(
        Path(args.vault).expanduser(),
        do_ocr=not args.no_ocr,
        do_clip=not args.no_clip,
        dry_run=args.dry_run,
        log_fn=print,
    )
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
    print("  1. Point Claude Code at this vault (see SETUP-LOCAL.md).")
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
            "Wire the KB capture + retrieval hooks into Claude Code: a Stop hook "
            "that captures conclusions at stepping-stones (write), and a "
            "UserPromptSubmit hook that reminds Claude to consult the KB before "
            "answering (read). Language-agnostic and cheap (gated + cooldown). "
            "Re-running is idempotent."
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

    print("Installed the KB hook scripts:")
    for item in report["installed"]:
        print(f"  {item['event']:<16} {item['script']}")
    if report["wired"]:
        print(f"Wired into {report['settings']}.")
        print("Restart Claude Code to activate. Triggers log to:")
        print("  ~/.claude/kb-capture-nudge.log   (write / capture)")
        print("  ~/.claude/kb-retrieve-nudge.log  (read / retrieval)")
    else:
        print("Add this to your settings.json (merge into hooks):")
        print(hook_module.snippet(report["installed"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
