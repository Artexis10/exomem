#!/usr/bin/env python3
"""Byte breakdown of the served compact bootstrap, by level, surface and section.

Read-only and deterministic: builds each payload against a throwaway empty vault
and measures `len(json.dumps(payload))`, the same measure the budget tests use.

    uv run python scripts/bootstrap-byte-breakdown.py            # totals matrix
    uv run python scripts/bootstrap-byte-breakdown.py --sections # + per-section table
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import tempfile

from exomem import capabilities, commands, prominence

LEVELS = tuple(prominence.CANON)
#: name -> (EXOMEM_SURFACE value or None, hosted profile or None)
SURFACES = {
    "default": (None, None),
    "claude-code": ("claude-code", None),
    "hosted-v1": (None, "hosted-alpha-agent-v1"),
    "hosted-v3": (None, "hosted-alpha-agent-v3"),
    "hosted-v4": (None, "hosted-alpha-agent-v4"),
    "hosted-v5": (None, "hosted-alpha-agent-v5"),
}


def size(value: object) -> int:
    return len(json.dumps(value))


def payload(level: str, surface: str, profile: str = "compact") -> dict:
    env, hosted = SURFACES[surface]
    os.environ["EXOMEM_PROMINENCE"] = level
    if env:
        os.environ["EXOMEM_SURFACE"] = env
    else:
        os.environ.pop("EXOMEM_SURFACE", None)
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "Knowledge Base").mkdir()
    if hosted is None:
        return commands.op_bootstrap(root, profile=profile)
    registry = commands.product_commands_for_profile(hosted, "rest")
    descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="hosted-agent",
        profile=hosted,
        tier2_enabled=commands.PRODUCT_SURFACE_PROFILES[hosted].expose_tier2,
        product_commands=tuple(command.name for command in registry),
    )
    with capabilities.active_surface(descriptor):
        return commands.op_bootstrap(root, profile=profile)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sections", action="store_true", help="also print per-section bytes")
    parser.add_argument("--surface", default="default", choices=sorted(SURFACES))
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)

    print("total bytes (compact)")
    print("surface".ljust(14) + "".join(level.rjust(9) for level in LEVELS))
    for surface in SURFACES:
        print(surface.ljust(14) + "".join(str(size(payload(lvl, surface))).rjust(9) for lvl in LEVELS))
    if args.sections:
        cols = {lvl: payload(lvl, args.surface) for lvl in LEVELS}
        total = {lvl: size(p) for lvl, p in cols.items()}
        keys = sorted({k for p in cols.values() for k in p}, key=lambda k: -size(cols["maximal"].get(k, 0)))
        print(f"\nper section, surface={args.surface}")
        print("section".ljust(26) + "".join(f"{lvl:>16}" for lvl in LEVELS))
        for key in keys:
            row = ""
            for lvl in LEVELS:
                n = size(cols[lvl][key]) if key in cols[lvl] else 0
                row += f"{n:>9} {100 * n / total[lvl]:5.1f}%"
            print(key.ljust(26) + row)
        print("TOTAL".ljust(26) + "".join(f"{total[lvl]:>16}" for lvl in LEVELS))


if __name__ == "__main__":
    main()
