from __future__ import annotations

import argparse

from .differ import compare_runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    command = parser.add_subparsers(dest="command", required=True)
    gate = command.add_parser("gate")
    gate.add_argument("--left", required=True)
    gate.add_argument("--right", required=True)
    gate.add_argument("--mode", required=True, choices=("blocking", "report"))
    gate.add_argument("--out", default=".")
    args = parser.parse_args(argv)
    result = compare_runs(args.left, args.right, mode=args.mode, out=args.out)
    return 1 if result.blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
