#!/usr/bin/env python
"""Generate docs/capabilities.md from the product command registry."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "capabilities.md"
SRC_PATH = REPO_ROOT / "src"


def _load_commands():
    sys.path.insert(0, str(SRC_PATH))
    from exomem import commands

    return commands


def _cell(value: object) -> str:
    text = str(value).replace("\n", " ").strip()
    return text.replace("|", r"\|")


def _surfaces(command) -> str:
    labels = []
    if "mcp" in command.surfaces:
        labels.append("MCP")
    if "rest" in command.surfaces:
        labels.append("REST")
    if "cli" in command.surfaces:
        labels.append("CLI")
    return ", ".join(labels) or "-"


def _params(command) -> str:
    if not command.params:
        return "-"
    rendered = []
    for param in command.params:
        suffix = "*" if param.required else ""
        rendered.append(f"{param.name}{suffix}")
    return ", ".join(rendered)


def _cli_positional(command) -> str:
    for param in command.params:
        if param.cli_positional:
            return param.name
    return "-"


def _routes(command) -> str:
    return ", ".join(command.routes) or "-"


def _summary(command) -> str:
    for line in command.doc.splitlines():
        text = line.strip()
        if text:
            return text
    return "-"


def build_capabilities_markdown() -> str:
    commands = _load_commands()
    registry = list(commands.PRODUCT_COMMANDS)
    tier_1 = sum(command.tier == 1 for command in registry)
    tier_2 = sum(command.tier == 2 for command in registry)
    mcp_generated = list(commands.product_commands_for("mcp", expose_tier2=True))
    rest_commands = list(commands.product_commands_for("rest", expose_tier2=True))
    cli_commands = list(commands.product_commands_for("cli", expose_tier2=True))
    hand_registered = sorted(commands.HAND_REGISTERED_EXCEPTIONS)

    lines = [
        "# Capabilities",
        "",
        "This file is generated from `src/exomem/commands.py`.",
        "Run `uv run python scripts/generate-capabilities.py` to refresh it.",
        "Run `uv run python scripts/generate-capabilities.py --check` to verify it is current.",
        "",
        "## Summary",
        "",
        f"- Product commands: {len(registry)}",
        f"- Tier 1 commands: {tier_1}",
        f"- Tier 2 commands: {tier_2}",
        f"- Registry-generated MCP commands: {len(mcp_generated)}",
        f"- REST commands: {len(rest_commands)}",
        f"- CLI commands: {len(cli_commands)}",
        f"- Hand-registered MCP tools: {', '.join(hand_registered) or 'none'}",
        "",
        "## Hosted Cell Capability Boundary",
        "",
        "Hosted operation keeps this registry-derived command surface inside one private,",
        "single-vault cell per tenant. The public gateway authenticates the account, resolves",
        "exactly one cell from server-side state, checks provider-neutral entitlements, and then",
        "forwards a compatible registry command. Public callers cannot select a tenant, cell,",
        "private endpoint, credential, or vault path.",
        "",
        "Exomem cells own command execution, mutation safety, private readiness, bounded feature",
        "enforcement, and canonical export/restore. They do not own accounts, public sessions,",
        "Paddle, provisioning, encrypted backup retention, KMS, or destructive external deletion.",
        "Paddle is never a cell request-path or package runtime dependency; Substrate projects its",
        "billing state into internal provider-neutral capabilities before routing.",
        "",
        "See [hosted-operations.md](hosted-operations.md) and the",
        "[Substrate control-plane contract](substrate-control-plane-contract.md).",
        "",
        "## Product Command Registry",
        "",
        "| Command | Tier | Surfaces | Mode | Destructive | CLI positional | Routes | Parameters | Summary |",
        "| --- | ---: | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for command in registry:
        mode = "read" if command.read_only else "write"
        destructive = "yes" if command.name in commands.DESTRUCTIVE_OPS else "no"
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(command.name),
                    str(command.tier),
                    _cell(_surfaces(command)),
                    mode,
                    destructive,
                    _cell(_cli_positional(command)),
                    _cell(_routes(command)),
                    _cell(_params(command)),
                    _cell(_summary(command)),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Hand-registered MCP Tools",
            "",
            "`HAND_REGISTERED_EXCEPTIONS` lists product tools that cannot be generated by the generic MCP registry loop.",
            "The default product surface currently has no hand-registered MCP exceptions.",
            "Artifact transfer is exposed through `transfer_artifact`; canonical token helpers remain implementation details.",
            "",
            "## Notes",
            "",
            "- A `*` suffix in the parameter list means the parameter is required.",
            "- Tier 2 commands are advanced file and data operations exposed only when the surface enables tier 2.",
            "- Destructive commands are writes that can replace, move, delete, or bulk-fix content.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if docs/capabilities.md is stale")
    args = parser.parse_args()

    generated = build_capabilities_markdown()
    if args.check:
        existing = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
        if existing != generated:
            print("docs/capabilities.md is stale. Run: uv run python scripts/generate-capabilities.py", file=sys.stderr)
            return 1
        print("docs/capabilities.md is current")
        return 0

    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(generated, encoding="utf-8", newline="\n")
    print(f"wrote {DOC_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
