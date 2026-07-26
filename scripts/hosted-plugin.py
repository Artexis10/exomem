#!/usr/bin/env python
"""Render, verify, archive, and maintain Hosted client plugin candidates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from exomem import hosted_plugins  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("render", "regenerate", "check", "archive", "promote", "demote", "status"))
    parser.add_argument("--openai-app-id")
    parser.add_argument("--platform", choices=(*hosted_plugins.PLATFORMS, "all"))
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--reason")
    parser.add_argument("--operator-key-id")
    parser.add_argument("--operator-secret")
    args = parser.parse_args()
    try:
        if args.command == "render":
            print(hosted_plugins.render(
                REPO_ROOT, openai_app_id=args.openai_app_id, platform=args.platform or "claude"
            ))
        elif args.command == "regenerate":
            if args.platform not in (None, "claude"):
                parser.error("regenerate supports only the committed Claude candidate")
            print(hosted_plugins.regenerate_claude(REPO_ROOT))
        elif args.command == "check":
            hosted_plugins.check(
                REPO_ROOT, openai_app_id=args.openai_app_id, platform=args.platform or "claude"
            )
            print("Hosted generated artifacts are current")
        elif args.command == "archive":
            print(hosted_plugins.archive(
                REPO_ROOT, openai_app_id=args.openai_app_id, platform=args.platform or "claude"
            ))
        elif args.command == "status":
            hosted_plugins.check_compatibility_descriptor(REPO_ROOT)
            print(json.dumps(hosted_plugins.distribution_manifest(REPO_ROOT), sort_keys=True))
        elif args.command == "promote":
            if args.platform not in hosted_plugins.PLATFORMS or not args.evidence:
                parser.error("promote requires --platform and --evidence")
            hosted_plugins.promote(
                REPO_ROOT,
                args.platform,
                json.loads(args.evidence.read_text(encoding="utf-8")),
                trusted_key_id=args.operator_key_id,
                trusted_secret=args.operator_secret,
            )
        else:
            if args.platform not in hosted_plugins.PLATFORMS or not args.reason:
                parser.error("demote requires --platform and --reason")
            hosted_plugins.demote(REPO_ROOT, args.platform, args.reason)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
