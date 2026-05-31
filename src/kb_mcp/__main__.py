"""`python -m kb_mcp` entry point.

Two subcommands:
- (default) serve the MCP server — `python -m kb_mcp [--transport ...]`
- `init` — bootstrap a fresh Knowledge Base into a vault
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
    print("  2. Adapt Knowledge Base/_Schema/project-keys.yaml to your own projects.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
